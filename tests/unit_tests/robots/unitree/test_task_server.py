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

"""Exercise the task runner's owned subprocesses and startup validation."""

import json
import os
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rpent.mcp.task.config import load_deployment
from rpent.mcp.task.runner import (
    TaskConflict,
    TaskRunner,
)
from rpent.mcp.task.server import main

CHILD = r"""
import json
import os
import signal
import sys
import time
from pathlib import Path

root = Path.cwd()
mode = os.environ.get("RPENT_TEST_MODE", "success")
output = Path(os.environ["RPENT_DISPATCH_OUTPUT_DIR"])
with (root / "launches.jsonl").open("a") as handle:
    handle.write(json.dumps({"args": sys.argv[1:], "tty": sys.stdin.isatty()}) + "\n")
if mode in ("wait", "force"):
    def stop(sig, frame):
        print("SIGINT cleanup completed", flush=True)
        raise SystemExit(130)
    signal.signal(signal.SIGINT, signal.SIG_IGN if mode == "force" else stop)
    if mode == "force":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / "ready").touch()
    while True:
        time.sleep(.05)
finish = None if mode in ("budget", "incomplete", "crash") else {
    "status": "failure" if mode == "failure" else "success", "summary": "done"
}
messages = [{
    "role": "tool", "name": "mcp__mujoco__follow_plan",
    "content": json.dumps({"structured_content": {
        "ok": True, "navigation_completed": True, "pose": {"x": goal}, "walk_id": f"walk_{goal}"
    }}),
} for goal in range(3)]
if mode == "uncertain":
    messages.append({
        "role": "tool", "name": "mcp__vln__run_vln_navigation",
        "content": json.dumps({"structured_content": {"ok": True, "submission_uncertain": True}}),
    })
(output / "transcript_unitree_fake.json").write_text(json.dumps({
    "finish": finish, "error": None, "messages": messages,
    "stats": {"turns_used": 16 if mode == "budget" else 9}
}))
if mode == "budget":
    print("Request budget of 16 reached.", flush=True)
if mode == "crash":
    raise SystemExit(7)
"""


class TaskServerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rpent runner ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "child.py").write_text(CHILD)
        (self.root / "run_rpent.sh").write_text(
            f'exec {shlex.quote(sys.executable)} {shlex.quote(str(self.root / "child.py"))} "$@"\n'
        )
        self.jobs = self.root / "jobs"
        config = self.root / "mcp.toml"
        config.write_text(
            '[robot]\nid = "go2-test"\ntype = "unitree.go2"\n'
            'environment = "simulation"\nadapter = "unitree"\n'
            "[mcp_servers]\n"
        )
        self.deployment = load_deployment(config)
        self.mode = patch.dict(os.environ, {"RPENT_TEST_MODE": "success"})
        self.mode.start()
        self.addCleanup(self.mode.stop)

    def runner(self, **kwargs):
        runner = TaskRunner(self.root, self.jobs, self.deployment, **kwargs)
        self.addCleanup(runner.close)
        return runner

    def wait_ready(self):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if (self.root / "ready").exists():
                return
            time.sleep(0.01)
        self.fail("owned child did not become ready")

    def test_real_child_has_tty_literal_arguments_and_success_artifacts(self):
        runner = self.runner()
        text = '去广告牌 "quoted"; $(touch injected); `touch injected2`\n再去母婴室'
        result = runner.wait(runner.start(text), 6)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["navigation"]["completed_navigation_goals"], 3)
        self.assertEqual(result["finish"]["summary"], "done")
        self.assertEqual(result["stats"]["turns_used"], 9)
        launch = json.loads((self.root / "launches.jsonl").read_text())
        self.assertTrue(launch["tty"])
        self.assertEqual(launch["args"], [text])
        self.assertFalse((self.root / "injected").exists())
        self.assertFalse((self.root / "injected2").exists())

    def test_exit_code_zero_is_not_automatically_success(self):
        for mode, expected in (
            ("budget", "budget_exhausted"),
            ("incomplete", "incomplete"),
            ("failure", "failed"),
            ("crash", "failed"),
            ("uncertain", "requires_attention"),
        ):
            with self.subTest(mode=mode):
                os.environ["RPENT_TEST_MODE"] = mode
                runner = self.runner()
                result = runner.wait(runner.start("test"), 6)
                self.assertEqual(result["status"], expected)
                if mode == "crash":
                    self.assertEqual(result["exit_code"], 7)
                    self.assertIn("7", result["error"])
                runner.close()

    def test_runner_uses_the_existing_launch_script(self):
        repo = Path(__file__).resolve().parents[4]
        shutil.copyfile(repo / "run_rpent.sh", self.root / "run_rpent.sh")
        python = self.root / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text(
            f"#!{sys.executable}\n"
            + r"""
import argparse, json, os, sys
from pathlib import Path
assert sys.stdin.isatty()
assert sys.argv[1:3] == ["-m", "rpent.cli.main"]
parser = argparse.ArgumentParser()
for option in ("--output-dir", "--task"):
    parser.add_argument(option)
args, _ = parser.parse_known_args(sys.argv[3:])
Path("actual-args.json").write_text(json.dumps(vars(args)))
(Path(args.output_dir) / "transcript_unitree_fake.json").write_text(json.dumps({
    "finish": {"status": "success", "summary": "done"}, "error": None
}))
"""
        )
        python.chmod(0o755)
        config = self.root / "test.env"
        config.write_text("# No model or network is used in this test.\n")
        with patch.dict(
            os.environ,
            {
                "RPENT_CONFIG_FILE": str(config),
                "RPENT_VENV_DIR": str(python.parent.parent),
                "RPENT_RUNS_DIR": str(self.root / "actual-runs"),
            },
        ):
            runner = self.runner()
            text = '先去广告牌；保留 "引号" 和空格'
            result = runner.wait(runner.start(text), 6)
        self.assertEqual(result["status"], "succeeded")
        args = json.loads((self.root / "actual-args.json").read_text())
        self.assertEqual(args["task"], text)
        self.assertEqual(args["output_dir"], result["output_dir"])

    def test_only_one_child_and_stop_uses_graceful_sigint(self):
        os.environ["RPENT_TEST_MODE"] = "wait"
        runner = self.runner()
        job = runner.start("wait for cancellation")
        self.wait_ready()
        with self.assertRaises(TaskConflict):
            runner.start("second instruction")
        self.assertTrue(runner.health()["busy"])
        runner.cancel(job)
        result = runner.wait(job, 6)
        self.assertEqual(result["status"], "stopped")
        self.assertFalse(result["forced_stop"])
        self.assertIn("SIGINT cleanup completed", Path(result["log_file"]).read_text())
        runner.cancel(job)
        self.assertFalse(runner.health()["busy"])
        os.environ["RPENT_TEST_MODE"] = "success"
        self.assertEqual(
            runner.wait(runner.start("next instruction"), 6)["status"], "succeeded"
        )

    def test_forced_stop_requires_attention_and_blocks_new_motion(self):
        os.environ["RPENT_TEST_MODE"] = "force"
        runner = self.runner(stop_timeout_s=0.1)
        job = runner.start("test")
        self.wait_ready()
        runner.cancel(job)
        result = runner.wait(job, 6)
        self.assertEqual(result["status"], "requires_attention")
        self.assertTrue(result["forced_stop"])
        with self.assertRaises(TaskConflict):
            runner.start("next")

    def test_shutdown_drains_owned_child_and_releases_lease(self):
        os.environ["RPENT_TEST_MODE"] = "wait"
        runner = self.runner()
        job = runner.start("test")
        self.wait_ready()
        with self.assertRaises(BlockingIOError):
            TaskRunner(self.root, self.jobs, self.deployment)
        runner.close()
        self.assertEqual(runner.wait(job, 6)["status"], "stopped")
        restored = self.runner()
        self.assertFalse(restored.health()["requires_attention"])

    def test_interrupted_legacy_record_still_blocks_new_motion(self):
        old = self.jobs / "previous"
        old.mkdir(parents=True)
        path = old / "job.json"
        path.write_text(
            json.dumps({"task_id": "previous", "task_name": "old", "status": "running"})
        )
        restored = self.runner()
        self.assertEqual(json.loads(path.read_text())["status"], "interrupted")
        with self.assertRaises(TaskConflict):
            restored.start("test")
        self.assertTrue(restored.health()["requires_attention"])
        self.assertFalse((self.root / "launches.jsonl").exists())

    def test_failed_launch_is_recorded_and_releases_runner(self):
        runner = self.runner()
        with patch("subprocess.Popen", side_effect=OSError("test launch failure")):
            with self.assertRaisesRegex(OSError, "test launch failure"):
                runner.start("go")
        path = next(self.jobs.glob("*/job.json"))
        record = json.loads(path.read_text())
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"], "test launch failure")
        self.assertFalse(runner.health()["busy"])
        self.assertEqual(runner.wait(runner.start("next"), 6)["status"], "succeeded")

    def test_public_binding_requires_server_side_token(self):
        with patch.dict(os.environ, {"RPENT_RUNNER_TOKEN": ""}):
            with self.assertRaises(SystemExit) as caught:
                main(["--host", "0.0.0.0"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
