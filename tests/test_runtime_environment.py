"""Runtime checks must stay offline; repair must not use a GPU or download models."""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import remote_bootstrap as bootstrap
from src import runtime_environment as runtime


class EnvironmentTests(unittest.TestCase):
    def test_missing_venv_does_not_use_system_packages(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(runtime, "find_uv", return_value={"ready": True}):
            root = Path(folder)
            stamp = root / "version"
            stamp.write_text("current")
            with patch.object(runtime.subprocess, "run") as command:
                result = runtime.inspect_environment(root / "missing", stamp, "current", root / "tools")
            self.assertFalse(result["ready"])
            self.assertFalse(result["venv"]["ready"])
            command.assert_not_called()

    def test_real_empty_venv_reports_missing_packages_despite_current_stamp(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / "venv")], check=True)
            stamp = root / "version"
            stamp.write_text("current")
            with patch.object(runtime, "find_uv", return_value={"ready": True}):
                result = runtime.inspect_environment(root / "venv", stamp, "current", root / "tools")
            self.assertTrue(result["venv"]["ready"])
            self.assertFalse(result["packages"]["ready"])
            self.assertIn("torch", result["packages"]["missing"])

    def test_existing_uv_is_reused_without_network(self):
        with patch.object(runtime, "find_uv", return_value={"ready": True, "path": "/existing/uv"}), patch.object(runtime.urllib.request, "urlopen") as fetch:
            self.assertEqual(runtime.ensure_uv(Path("/unused")), "/existing/uv")
        fetch.assert_not_called()

    def test_uv_install_is_private_and_does_not_change_shell_profiles(self):
        with tempfile.TemporaryDirectory() as folder:
            tools = Path(folder) / "tools"
            reply = MagicMock()
            reply.__enter__.return_value.read.return_value = b"#!/bin/sh\n# fixture"
            with patch.object(runtime, "find_uv", side_effect=[{"ready": False}, {"ready": True, "path": str(tools / "uv")}]), \
                 patch.object(runtime.urllib.request, "urlopen", return_value=reply), patch.object(runtime.subprocess, "run") as command:
                runtime.ensure_uv(tools, {"TMPDIR": folder})
            self.assertEqual(command.call_args.kwargs["env"]["UV_UNMANAGED_INSTALL"], str(tools))
            self.assertEqual(command.call_args.kwargs["env"]["UV_NO_MODIFY_PATH"], "1")
            self.assertEqual(list(Path(folder).iterdir()), [tools])

    def test_deleted_venv_is_reinstalled_even_when_version_stamp_remains(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "runtime-version").write_text(bootstrap.RUNTIME_VERSION)
            missing = {"ready": False, "venv": {"ready": False}, "packages": {"ready": False}}
            with patch.object(bootstrap, "ensure_uv", return_value="/tools/uv"), patch.object(bootstrap, "inspect_environment", return_value=missing), patch.object(bootstrap.subprocess, "run") as command:
                bootstrap.prepare_runtime(root, lambda *args, **kwargs: None)
            commands = [call.args[0] for call in command.call_args_list]
            self.assertIn("venv", commands[0])
            self.assertTrue(any("torch==2.8.0+cu128" in arguments for arguments in commands))
            self.assertTrue(any(bootstrap.UPSTREAM in arguments for arguments in commands))

    def test_runtime_only_install_never_downloads_or_starts_gpu(self):
        with tempfile.TemporaryDirectory() as folder:
            device = {"id": "e" * 32, "remote_port": 18400, "model": "owner/model", "api_key": "fixture",
                      "model_source": "folder", "model_path": "/missing/model"}
            output = io.StringIO()
            checked = {"ready": True, "venv": {"ready": True}, "packages": {"ready": True}}
            with patch.object(bootstrap.Path, "home", return_value=Path(folder)), \
                 patch.object(bootstrap.sys, "stdin", io.StringIO(json.dumps({"action": "runtime", "device": device}))), \
                 patch.object(bootstrap.sys, "stdout", output), patch.object(bootstrap, "prepare_runtime", return_value=Path(folder) / "python"), \
                 patch.object(bootstrap, "inspect_environment", return_value=checked), \
                 patch.object(bootstrap, "prepare_model", side_effect=RuntimeError("model absent")) as model, \
                 patch.object(bootstrap.subprocess, "Popen") as launch, patch.object(bootstrap.subprocess, "check_output") as gpu:
                bootstrap.main()
            self.assertFalse(model.call_args.kwargs["install"])
            self.assertIn('"phase": "idle"', output.getvalue())
            self.assertIn('"model_ready": false', output.getvalue())
            launch.assert_not_called()
            gpu.assert_not_called()
            root = Path(folder) / ".local/share/cosmoss/workers" / device["id"]
            self.assertFalse((root / "tmp").exists())
            self.assertFalse((root / "cache").exists())

    def test_environment_inspection_does_not_install_or_claim_worker_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            device = {"id": "d" * 32, "remote_port": 18400, "model": "owner/model", "api_key": "fixture"}
            with patch.object(bootstrap.Path, "home", return_value=Path(folder)), \
                 patch.object(bootstrap.sys, "stdin", io.StringIO(json.dumps({"action": "environment", "device": device}))), \
                 patch.object(bootstrap.sys, "stdout", io.StringIO()), patch.object(bootstrap, "inspect_environment", return_value={"ready": False}), \
                 patch.object(bootstrap, "prepare_runtime") as install:
                bootstrap.main()
            install.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])
