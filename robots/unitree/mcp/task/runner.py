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
import hashlib
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
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


def validate_task_id(value: str) -> str:
    """Check the stable identifier shared by the runner and its launcher."""
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or any(char.isspace() or ord(char) < 32 for char in value)
        or any(char in value for char in ("/", "\\", "\x7f"))
    ):
        raise ValueError("task identifiers must be nonempty single path segments")
    return value


class RunRequest(BaseModel):
    """Accept a stable task ID and a complete instruction."""

    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: str = Field(min_length=1, max_length=256)
    task_name: str = Field(min_length=1, max_length=20000)

    @field_validator("task_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return validate_task_id(value)

    @field_validator("task_name")
    @classmethod
    def nonblank_task(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task_name must not be blank")
        return value


class TaskConflict(ValueError):
    """A task cannot start without changing or duplicating an existing run."""


@dataclass
class _Job:
    record: dict[str, Any]
    directory: Path
    process: subprocess.Popen | None = None
    terminal_fd: int | None = None
    done: threading.Event = field(default_factory=threading.Event)


class TaskRunner:
    """Own one child at a time, its TTY, cancellation, and persistent task IDs.

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
        self._jobs: dict[str, _Job] = {}
        self._closed = False
        self._stop_timeout_s = stop_timeout_s
        self._lease = (self.jobs_dir / ".lock").open("a")
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for path in sorted(self.jobs_dir.glob("*/job.json")):
                record = _read_json(path)
                task_id = validate_task_id(record["task_id"])
                if task_id in self._jobs:
                    raise ValueError(f"duplicate persisted task: {task_id}")
                if record["status"] in ACTIVE_STATUSES:
                    record.update(
                        status="interrupted",
                        error="service exited before the child result was recorded; verify the old process and simulator",
                    )
                    write_json_atomic(path, record)
                job = _Job(record, path.parent)
                job.done.set()
                self._jobs[task_id] = job
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
            record["navigation"] = {"real_robot": real_robot}
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

    def _snapshot(self, job: _Job) -> dict[str, Any]:
        if job.record["status"] in ACTIVE_STATUSES:
            self._refresh(job)
        return copy.deepcopy(job.record)

    def start(self, request: RunRequest) -> tuple[dict[str, Any], bool]:
        """Start once per task ID; return the record and whether it is new."""
        with self._lock:
            if self._closed:
                raise TaskConflict("service is shutting down")
            existing = self._jobs.get(request.task_id)
            if existing is not None:
                if existing.record["task_name"] != request.task_name:
                    raise TaskConflict("task_id already belongs to different task text")
                return self._snapshot(existing), False
            blocked = [
                job.record["task_id"]
                for job in self._jobs.values()
                if job.record["status"] in ACTIVE_STATUSES | ATTENTION_STATUSES
            ]
            if blocked:
                raise TaskConflict(
                    "runner is busy or needs reconciliation: " + ", ".join(blocked)
                )
            directory = (
                self.jobs_dir / hashlib.sha256(request.task_id.encode()).hexdigest()
            )
            directory.mkdir(exist_ok=True)
            log_file = directory / "stdout.log"
            log_file.touch()
            job = _Job(
                record={
                    **request.model_dump(),
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
            self._jobs[request.task_id] = job
            self._persist(job)  # Persist the ID before any possible execution.
            slave = None
            try:
                job.terminal_fd, slave = pty.openpty()
                environment = dict(os.environ, PYTHONUNBUFFERED="1")
                with log_file.open("ab", buffering=0) as output:
                    job.process = subprocess.Popen(
                        [
                            "bash",
                            str(self.repo_root / "run_rpent.sh"),
                            request.task_id,
                            request.task_name,
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
            job.record.update(status="running", pid=job.process.pid)
            threading.Thread(target=self._watch, args=(job,), daemon=True).start()
            self._persist(job)
            logger.info("Started task %s (pid=%s)", request.task_id, job.process.pid)
            return self._snapshot(job), True

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
                    self.logs(record["task_id"], 65536),
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
                    record["task_id"],
                    status,
                    exit_code,
                )
            except Exception:
                job.record.update(
                    status="requires_attention", exit_code=exit_code, finished_at=_now()
                )
                logger.exception(
                    "Cannot record result for task %s", job.record["task_id"]
                )
            finally:
                if job.terminal_fd is not None:
                    os.close(job.terminal_fd)
                    job.terminal_fd = None
                job.done.set()

    def get(self, task_id: str) -> dict[str, Any]:
        """Return local process status and the latest available navigation evidence."""
        with self._lock:
            return self._snapshot(self._jobs[task_id])

    def logs(self, task_id: str, tail_bytes: int = 16384) -> str:
        """Read a bounded UTF-8 tail of the owned child's combined output."""
        with self._lock:
            path = Path(self._jobs[task_id].record["log_file"])
            with path.open("rb") as handle:
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

    def stop(self, task_id: str) -> dict[str, Any]:
        """Request SIGINT cleanup once; escalate only after the grace period."""
        with self._lock:
            job = self._jobs[task_id]
            if (
                job.process is not None
                and job.record["status"] in ACTIVE_STATUSES
                and not job.record["stop_requested"]
            ):
                job.record.update(status="stopping", stop_requested=True)
                try:
                    self._persist(job)
                finally:
                    self._signal(job, signal.SIGINT)
                    threading.Thread(
                        target=self._escalate_stop, args=(job,), daemon=True
                    ).start()
            return self._snapshot(job)

    def health(self) -> dict[str, Any]:
        """Report runner availability without making model or simulator requests."""
        with self._lock:
            active = [
                task_id
                for task_id, job in self._jobs.items()
                if job.record["status"] in ACTIVE_STATUSES
            ]
            attention = [
                task_id
                for task_id, job in self._jobs.items()
                if job.record["status"] in ATTENTION_STATUSES
            ]
            return {
                "ok": not self._closed,
                "busy": bool(active),
                "active_task_ids": active,
                "attention_task_ids": attention,
            }

    def close(self) -> None:
        """Drain owned children before releasing the single-service lease."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = [
                job
                for job in self._jobs.values()
                if job.record["status"] in ACTIVE_STATUSES
            ]
            for job in active:
                self.stop(job.record["task_id"])
        for job in active:
            job.done.wait(self._stop_timeout_s + 5)
        self._lease.close()
