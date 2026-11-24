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

"""Own persistent robot task records and their launcher processes."""

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
from uuid import uuid4

import tomlkit

from rpent.evaluation import write_json_atomic
from rpent.mcp.task.config import TaskDeployment
from rpent.mcp.task.reports import collect_run_evidence
from rpent.robots.robot_spec import TaskEvidence
from rpent.utils.logging import get_logger

logger = get_logger("task_mcp")
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
    lease; each robot must still have one owner. Interrupted records require
    operator reconciliation rather than automatic motion replay.
    """

    def __init__(
        self,
        repo_root: Path,
        jobs_dir: Path,
        deployment: TaskDeployment,
        *,
        stop_timeout_s: float = 30.0,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.jobs_dir = jobs_dir.resolve()
        self.deployment = deployment
        self._mcp_config = tomlkit.dumps(
            {
                "robot": deployment.robot.model_dump(),
                "mcp_servers": {
                    name: config.model_dump(exclude_none=True)
                    for name, config in deployment.servers.items()
                },
                "service_adapters": deployment.service_adapters,
                "capabilities": {
                    name: config.model_dump(exclude_defaults=True)
                    for name, config in deployment.capabilities.items()
                },
            }
        )
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
            output_dir = directory / "output"
            output_dir.mkdir()
            mcp_config = directory / "mcp.toml"
            mcp_config.write_text(self._mcp_config, encoding="utf-8")
            log_file = directory / "stdout.log"
            log_file.touch()
            job = _Job(
                record={
                    "run_id": uuid4().hex,
                    "robot_id": self.deployment.robot.id,
                    "instruction": instruction,
                    "status": "starting",
                    "created_at": _now(),
                    "finished_at": None,
                    "pid": None,
                    "exit_code": None,
                    "output_dir": str(output_dir),
                    "log_file": str(log_file),
                    "finish": None,
                    "error": None,
                    "stats": None,
                    "evidence": {},
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
                environment = dict(
                    os.environ,
                    PYTHONUNBUFFERED="1",
                    RPENT_DISPATCH_ADAPTER=self.deployment.robot.adapter,
                    RPENT_DISPATCH_MCP_CONFIG=str(mcp_config),
                    RPENT_DISPATCH_OUTPUT_DIR=str(output_dir),
                    RPENT_MCP_RUN_ID=job.record["run_id"],
                )
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
        record = job.record
        try:
            output = Path(record["output_dir"])
            transcripts = sorted(output.glob("transcript_*.json"))
            transcript = _read_json(transcripts[-1]) if transcripts else {}
            read_evidence = self.deployment.execution.read_evidence
            initial = (
                read_evidence(output, transcript) if read_evidence else TaskEvidence()
            )
            evidence = collect_run_evidence(
                self.deployment.servers,
                output,
                record["run_id"],
                timeout_s=min(self._stop_timeout_s, 18),
                initial=initial,
            )
        except Exception:
            logger.exception("Cannot collect result for task in %s", job.directory)
            transcript = {}
            evidence = TaskEvidence(
                requires_attention=True,
                stop_error="cannot collect terminal task evidence",
            )
        with self._lock:
            try:
                finish = transcript.get("finish") or {}
                record.update(
                    finish=transcript.get("finish"),
                    error=transcript.get("error") or evidence.error,
                    stats=transcript.get("stats"),
                    evidence=evidence.data,
                    navigation=evidence.data.get("navigation"),
                )
                if (
                    record["forced_stop"]
                    or evidence.requires_attention
                    or (exit_code < 0 and not record["stop_requested"])
                    or (
                        record["stop_requested"]
                        and self.deployment.robot.environment == "real"
                        and self.deployment.execution.read_evidence is None
                    )
                ):
                    status = "requires_attention"
                elif record["stop_requested"]:
                    status = "stopped"
                elif (
                    exit_code != 0
                    or record["error"]
                    or finish.get("status") in {"failure", "stuck"}
                ):
                    status = "failed"
                elif evidence.verification_required:
                    status = "verification_required"
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
                        evidence.stop_error
                        or "process cleanup or remote motion state needs verification"
                    )
                if status == "failed" and not record["error"]:
                    record["error"] = (
                        finish.get("summary") or f"process exited with code {exit_code}"
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
                    status="requires_attention",
                    exit_code=exit_code,
                    finished_at=_now(),
                    error="cannot record terminal task evidence",
                )
                logger.exception("Cannot record result for task in %s", job.directory)
                try:
                    self._persist(job)
                except OSError:
                    logger.exception("Cannot persist task state in %s", job.directory)
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
