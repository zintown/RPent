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

"""Check the Unitree launcher arguments, configuration and output directories."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]


class RunRpentScriptTests(unittest.TestCase):
    """Verify literal argv forwarding, new artifact directories, and usage errors."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        venv = self.root / "venv with spaces"
        (venv / "bin").mkdir(parents=True)
        python = venv / "bin/python"
        python.write_text(
            f"#!{sys.executable}\n"
            + """
import json
import os
import sys
from pathlib import Path
if sys.argv[1] == "-c":
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
if "LAUNCH_TEST_EXPECTED_KEY" in os.environ:
    assert os.environ.get("OPENAI_API_KEY") == os.environ["LAUNCH_TEST_EXPECTED_KEY"], "config key was not exported"
Path(os.environ["LAUNCH_TEST_ARGS"]).write_text(json.dumps(sys.argv[1:]))
"""
        )
        python.chmod(0o755)
        self.config = self.root / "test.env"
        self.config.write_text("# Isolate launch tests from real local credentials.\n")
        self.env = os.environ.copy()
        self.env.pop("OPENAI_API_KEY", None)
        self.env.pop("LAUNCH_TEST_EXPECTED_KEY", None)
        self.env.update(
            RPENT_CONFIG_FILE=str(self.config),
            RPENT_UNITREE_MODE="mujoco",
            RPENT_VENV_DIR=str(venv),
            RPENT_RUNS_DIR=str(self.root / "logs"),
            LAUNCH_TEST_ARGS=str(self.root / "args.json"),
        )

    def launch(self, *args, script=None):
        return subprocess.run(
            ["bash", str(script or REPO / "run_rpent.sh"), *args],
            env=self.env,
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=15,
        )

    def test_two_arguments_preserve_literal_task_and_create_fresh_run(self):
        marker = self.root / "must-not-exist"
        task = f'先去广告牌，确认到达后，再去厕所通道。\n不要执行 $(touch {marker}) `touch {marker}` "引号"'
        directories = []
        for _ in range(2):
            result = self.launch("task-123", task)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((self.root / "args.json").read_text())
            self.assertEqual(args[:2], ["-m", "rpent.cli.main"])
            parser = argparse.ArgumentParser()
            parser.add_argument("--task")
            parser.add_argument("--output-dir")
            parsed, _ = parser.parse_known_args(args[2:])
            self.assertEqual(parsed.task, task)
            directories.append(parsed.output_dir)
        self.assertNotEqual(*directories)
        self.assertTrue(all(Path(path).is_dir() for path in directories))
        self.assertFalse(marker.exists())

    def test_task_text_starting_with_dashes_stays_one_argument(self):
        result = self.launch("task-123", "--max-turns 999 这是任务文本")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.root / "args.json").read_text())
        parser = argparse.ArgumentParser()
        parser.add_argument("--task")
        parsed, _ = parser.parse_known_args(args[2:])
        self.assertEqual(parsed.task, "--max-turns 999 这是任务文本")

    def test_real_mode_passes_only_its_own_service_configuration(self):
        self.env.update(
            RPENT_UNITREE_MODE="real",
            RPENT_REAL_ENDPOINT="http://robot-gateway.test:9001",
            RPENT_ROBOT_INDEX="2",
            RPENT_VLN_ENDPOINT="ws://simulation-only.test:8050",
        )
        result = self.launch("go2-status", "查询在线状态")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.root / "args.json").read_text())
        self.assertEqual(args[args.index("--unitree-mode") + 1], "real")
        self.assertEqual(args[args.index("--robot-index") + 1], "2")
        self.assertEqual(
            args[args.index("--real-endpoint") + 1], "http://robot-gateway.test:9001"
        )
        self.assertFalse(
            any(arg.startswith(("--sim-", "--nav-", "--vln-")) for arg in args)
        )

    def test_missing_arguments_fail_before_launch(self):
        self.assertEqual(self.launch("task-only").returncode, 2)
        self.assertEqual(self.launch("task", " ").returncode, 2)
        self.assertFalse((self.root / "args.json").exists())

    def test_custom_config_exports_credentials_without_printing_them(self):
        key = "test-only-key-from-private-config"
        self.config.write_text(
            f"export OPENAI_API_KEY='{key}'\n"
            "export RPENT_MODEL='openai-chat:test-config-model'\n"
        )
        self.env["OPENAI_API_KEY"] = "test-only-parent-key"
        self.env["LAUNCH_TEST_EXPECTED_KEY"] = key
        result = self.launch("task-123", "去广告牌")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(key, result.stdout + result.stderr)
        args = json.loads((self.root / "args.json").read_text())
        self.assertEqual(
            args[args.index("--model") + 1], "openai-chat:test-config-model"
        )
        self.assertNotIn(key, json.dumps(args))

    def test_default_config_is_relative_to_script_from_another_working_directory(self):
        repo = self.root / "isolated repo"
        repo.mkdir()
        script = repo / "run_rpent.sh"
        script.write_bytes((REPO / "run_rpent.sh").read_bytes())
        key = "test-only-key-in-repository-config"
        (repo / ".env.local").write_text(
            f"export OPENAI_API_KEY='{key}'\n"
            "export RPENT_NAV_ENDPOINT='http://navigation-from-config.test'\n"
        )
        self.env.pop("RPENT_CONFIG_FILE")
        self.env["LAUNCH_TEST_EXPECTED_KEY"] = key
        result = self.launch("task-123", "去广告牌", script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(key, result.stdout + result.stderr)
        args = json.loads((self.root / "args.json").read_text())
        self.assertEqual(
            args[args.index("--nav-endpoint") + 1],
            "http://navigation-from-config.test",
        )

    def test_missing_explicit_config_fails_before_launch(self):
        self.config.unlink()
        result = self.launch("task-123", "去广告牌")
        self.assertEqual(result.returncode, 2)
        self.assertIn("configuration file not found", result.stderr)
        self.assertFalse((self.root / "args.json").exists())
