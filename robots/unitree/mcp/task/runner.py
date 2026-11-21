# Copyright 2026 The RPent Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Own persistent Unitree task records and their launcher processes."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import pty
import re
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from rpent.evaluation import write_json_atomic
from rpent.utils.logging import get_logger

logger = get_logger("unitree_task_server")
ACTIVE_STATUSES = {"starting", "running", "stopping"}
ATTENTION_STATUSES = {"interrupted", "requires_attention"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


class TaskConflict(ValueError):
    """The runner cannot accept a new instruction."""


@dataclass
class _Job:
    record: dict[str, Any]
    directory: Path
    process: subprocess.Popen | None = None
    terminal_fd: int | None = None
    done: threading.Event = field(default_factory=threading.Event)


class TaskRunner:
    """Own one task process at a time and persist its execution evidence.

    A filesystem lease prevents two services using the same job directory.
    Standalone CLI runs and services using other directories are outside this
    lease; the simulator must still have one owner. Interrupted records require
    operator reconciliation rather than automatic motion replay.
    """

    def __init__(
        self,
        repo_root: Path,
        jobs_dir: Path,
        *,
        stop_timeout_s: float = 30.0,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.jobs_dir = jobs_dir.resolve()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._active: _Job | None = None
        self._requires_attention = False
        self._closed = False
        self._stop_timeout_s = stop_timeout_s
        self._lease = (self.jobs_dir / ".lock").open("a")
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for path in sorted(self.jobs_dir.glob("*/job.json")):
                record = _read_json(path)
                if record["status"] in ACTIVE_STATUSES:
                    record.update(
                        status="interrupted",
                        error="service exited before recording the task result; verify the old process and equipment",
                    )
                    write_json_atomic(path, record)
                if record["status"] in ATTENTION_STATUSES:
                    self._requires_attention = True
        except BaseException:
            self._lease.close()
            raise

    def _persist(self, job: _Job) -> None:
        write_json_atomic(job.directory / "job.json", job.record)

    def _refresh(self, job: _Job) -> None:
        """Read available evidence; state files describe completed tool calls."""
        record = job.record
        if record["output_dir"] is None:
            with Path(record["log_file"]).open("rb") as handle:
                prefix = handle.read(32768).decode("utf-8", errors="replace")
            match = re.search(r"^RPent output: (.+)$", prefix, re.MULTILINE)
            if match:
                output = Path(match[1].strip())
                record["output_dir"] = str((self.repo_root / output).resolve())
        if record["output_dir"] is None:
            return
        output = Path(record["output_dir"])
        state_time = 0
        try:
            state_path = output / "states.json"
            state = _read_json(state_path)
            steps = state.get("steps", [])
            if steps:
                record["navigation"] = steps[-1].get("state")
                state_time = state_path.stat().st_mtime_ns
        except (OSError, ValueError):
            pass  # The producer may still be replacing its file.
        # A cancelled tool also writes a walk report during its cleanup.
        reports = sorted(
            (output / "navigation" / "walks").glob("*.json"),
            key=lambda path: path.stat().st_mtime_ns,
        )
        if reports and reports[-1].stat().st_mtime_ns > state_time:
            try:
                report = _read_json(reports[-1])
                record["navigation"] = (record["navigation"] or {}) | {
                    key: report[key]
                    for key in (
                        "completed_navigation_goals",
                        "active_walk_id",
                        "submission_uncertain",
                        "pose",
                        "error",
                        "stop_error",
                    )
                    if key in report
                }
            except (OSError, ValueError):
                pass
        # The VLN tool persists compact progress while its one long call runs.
        vln_path = output / "navigation" / "vln" / "latest.json"
        try:
            if vln_path.stat().st_mtime_ns > state_time:
                vln = _read_json(vln_path)
                navigation = record["navigation"] or {}
                record["navigation"] = navigation | {
                    "vln": vln,
                    "active_control_id": vln.get("active_control_id"),
                    "submission_uncertain": vln.get("submission_uncertain", False),
                    "stop_error": vln.get("stop_error"),
                    "pose": vln.get("pose") or navigation.get("pose"),
                }
        except (OSError, ValueError):
            pass
        try:
            real_robot = _read_json(output / "real" / "latest.json")
            record["navigation"] = (record["navigation"] or {}) | {
                "real_robot": real_robot
            }
        except (OSError, ValueError):
            pass
        transcripts = sorted(output.glob("transcript_unitree*.json"))
        if transcripts:
            try:
                transcript = _read_json(transcripts[-1])
                record.update(
                    finish=transcript.get("finish"),
                    error=transcript.get("error"),
                    stats=transcript.get("stats"),
                )
            except (OSError, ValueError):
                pass

    def start(self, instruction: str) -> _Job:
        """Start a new instruction with an internally allocated run directory."""
        if not instruction.strip():
            raise ValueError("instruction must not be blank")
        with self._lock:
            if self._closed:
                raise TaskConflict("service is shutting down")
            if self._active is not None:
                raise TaskConflict("a task is already running or stopping")
            if self._requires_attention:
                raise TaskConflict(
                    "verify the previous task and equipment before starting another task"
                )
            directory = Path(mkdtemp(prefix="run-", dir=self.jobs_dir))
            log_file = directory / "stdout.log"
            log_file.touch()
            job = _Job(
                record={
                    "instruction": instruction,
                    "status": "starting",
                    "created_at": _now(),
                    "finished_at": None,
                    "pid": None,
                    "exit_code": None,
                    "output_dir": None,
                    "log_file": str(log_file),
                    "finish": None,
                    "error": None,
                    "stats": None,
                    "navigation": None,
                    "stop_requested": False,
                    "forced_stop": False,
                },
                directory=directory,
            )
            self._persist(job)
            slave = None
            try:
                job.terminal_fd, slave = pty.openpty()
                environment = dict(os.environ, PYTHONUNBUFFERED="1")
                with log_file.open("ab", buffering=0) as output:
                    job.process = subprocess.Popen(
                        [
                            "bash",
                            str(self.repo_root / "run_rpent.sh"),
                            instruction,
                        ],
                        cwd=self.repo_root,
                        env=environment,
                        stdin=slave,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
            except Exception as exc:
                if job.terminal_fd is not None:
                    os.close(job.terminal_fd)
                    job.terminal_fd = None
                job.record.update(status="failed", error=str(exc), finished_at=_now())
                job.done.set()
                self._persist(job)
                raise
            finally:
                if slave is not None:
                    os.close(slave)
            self._active = job
            job.record.update(status="running", pid=job.process.pid)
            threading.Thread(target=self._watch, args=(job,), daemon=True).start()
            try:
                self._persist(job)
            except OSError:
                self._request_stop(job)
                raise
            logger.info("Started task in %s (pid=%s)", directory, job.process.pid)
            return job

    def _watch(self, job: _Job) -> None:
        assert job.process is not None
        exit_code = job.process.wait()
        with self._lock:
            try:
                self._refresh(job)
                record = job.record
                navigation = record["navigation"] or {}
                vln = navigation.get("vln") or {}
                real_robot = navigation.get("real_robot") or {}
                finish = record["finish"] or {}
                if (
                    record["forced_stop"]
                    or navigation.get("active_walk_id")
                    or navigation.get("active_control_id")
                    or navigation.get("submission_uncertain")
                    or navigation.get("stop_error")
                    or real_robot.get("active_request_id")
                    or real_robot.get("submission_uncertain")
                    or real_robot.get("stop_error")
                    or (exit_code < 0 and not record["stop_requested"])
                ):
                    status = "requires_attention"
                elif record["stop_requested"]:
                    status = "stopped"
                elif (
                    exit_code == 0
                    and not record["error"]
                    and not navigation.get("error")
                    and vln.get("state") == "verification_required"
                    and not vln.get("error")
                ):
                    status = "verification_required"
                elif (
                    exit_code != 0
                    or record["error"]
                    or vln.get("error")
                    or vln.get("state") == "failed"
                    or real_robot.get("error")
                    or finish.get("status") in {"failure", "stuck"}
                ):
                    status = "failed"
                elif finish.get("status") == "success":
                    status = "succeeded"
                elif re.search(
                    r"Request budget of \d+ reached\.",
                    self._log_tail(job, 65536),
                ):
                    status = "budget_exhausted"
                else:
                    status = "incomplete"
                record.update(status=status, exit_code=exit_code, finished_at=_now())
                if status == "requires_attention" and not record["error"]:
                    record["error"] = (
                        navigation.get("stop_error")
                        or real_robot.get("stop_error")
                        or "process cleanup or remote motion state needs verification"
                    )
                if status == "failed" and not record["error"]:
                    record["error"] = (
                        real_robot.get("error")
                        or finish.get("summary")
                        or f"process exited with code {exit_code}"
                    )
                self._persist(job)
                logger.info(
                    "Task %s finished: %s (exit=%s)",
                    job.directory.name,
                    status,
                    exit_code,
                )
            except Exception:
                job.record.update(
                    status="requires_attention", exit_code=exit_code, finished_at=_now()
                )
                logger.exception("Cannot record result for task in %s", job.directory)
            finally:
                if job.terminal_fd is not None:
                    os.close(job.terminal_fd)
                    job.terminal_fd = None
                self._requires_attention = job.record["status"] in ATTENTION_STATUSES
                self._active = None
                job.done.set()
                if self._closed:
                    self._lease.close()

    def wait(self, job: _Job, timeout_s: float | None = None) -> dict[str, Any]:
        """Wait for the owned process to finish and return its recorded result."""
        if not job.done.wait(timeout_s):
            raise TimeoutError("task has not finished")
        return copy.deepcopy(job.record)

    @staticmethod
    def _log_tail(job: _Job, tail_bytes: int) -> str:
        with Path(job.record["log_file"]).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            return handle.read(tail_bytes).decode("utf-8", errors="replace")

    @staticmethod
    def _signal(job: _Job, sig: signal.Signals) -> None:
        assert job.process is not None
        if job.process.poll() is None:
            try:
                os.killpg(job.process.pid, sig)
            except ProcessLookupError:
                pass

    def _escalate_stop(self, job: _Job) -> None:
        assert job.process is not None
        try:
            job.process.wait(timeout=self._stop_timeout_s)
        except subprocess.TimeoutExpired:
            with self._lock:
                if job.process.poll() is not None:
                    return
                job.record["forced_stop"] = True
                self._signal(job, signal.SIGTERM)
            try:
                job.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._signal(job, signal.SIGKILL)

    def _request_stop(self, job: _Job) -> None:
        if job is not self._active or job.record["stop_requested"]:
            return
        job.record.update(status="stopping", stop_requested=True)
        try:
            self._persist(job)
        finally:
            self._signal(job, signal.SIGINT)
            threading.Thread(
                target=self._escalate_stop, args=(job,), daemon=True
            ).start()

    def cancel(self, job: _Job) -> None:
        """Request graceful interruption and wait for its bounded cleanup."""
        try:
            with self._lock:
                self._request_stop(job)
        finally:
            job.done.wait(self._stop_timeout_s + 5)

    def health(self) -> dict[str, bool]:
        """Report availability and whether earlier motion needs reconciliation."""
        with self._lock:
            return {
                "ok": not self._closed,
                "busy": self._active is not None,
                "requires_attention": self._requires_attention,
            }

    def close(self) -> None:
        """Drain the owned process before releasing the single-service lease."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = self._active
            if active is None:
                self._lease.close()
                return
        self.cancel(active)
