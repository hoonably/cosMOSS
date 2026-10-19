import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import model_cache


MODEL = "OpenMOSS-Team/MOSS-Transcribe-Diarize"


def cached_model(cache):
    repository = cache / "models--OpenMOSS-Team--MOSS-Transcribe-Diarize"
    snapshot = repository / "snapshots" / ("a" * 40)
    snapshot.mkdir(parents=True)
    (repository / "refs").mkdir()
    (repository / "refs/main").write_text("a" * 40)
    for name in ["config.json", "tokenizer_config.json", "preprocessor_config.json", "processor_config.json", "tokenizer.json"]:
        (snapshot / name).write_text("{}")
    (snapshot / "model.safetensors.index.json").write_text(json.dumps({
        "weight_map": {"weight": "model-00001.safetensors"}, "metadata": {"total_size": 16},
    }))
    (snapshot / "model-00001.safetensors").write_bytes(b"x" * 16)
    return snapshot


class ModelCacheTests(unittest.TestCase):
    def test_remote_huggingface_environment_and_default_cache_precedence(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(model_cache.Path, "home", return_value=Path("/home/tester")):
            self.assertEqual(model_cache.default_cache(), Path("/home/tester/.cache/huggingface/hub"))
            with patch.dict(os.environ, {"XDG_CACHE_HOME": "/scratch/cache"}):
                self.assertEqual(model_cache.default_cache(), Path("/scratch/cache/huggingface/hub"))
                with patch.dict(os.environ, {"HF_HOME": "/data/hf"}):
                    self.assertEqual(model_cache.default_cache(), Path("/data/hf/hub"))
                    with patch.dict(os.environ, {"HF_HUB_CACHE": "/shared/models"}):
                        self.assertEqual(model_cache.default_cache(), Path("/shared/models"))

    def test_complete_existing_model_is_reused_offline_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            snapshot = cached_model(cache)
            with patch.dict(os.environ, {"HF_HUB_CACHE": str(cache)}), \
                 patch.object(model_cache.subprocess, "run") as download, \
                 patch.object(model_cache, "writable_cache") as write:
                result = model_cache.ensure_model({"model": MODEL}, "/runtime/python", Mock())
            self.assertTrue(result["reused"])
            self.assertEqual(result["model_path"], str(snapshot.resolve()))
            download.assert_not_called()
            write.assert_not_called()

    def test_new_model_is_prefetched_to_the_same_cache_and_reused_afterwards(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "hf"
            device = {"model": MODEL, "model_cache": str(cache)}

            def download(target, python, destination, emit):
                self.assertEqual(target["model"], MODEL)
                self.assertEqual(destination, cache)
                return cached_model(cache).resolve()

            with patch.object(model_cache, "download_model", side_effect=download) as network:
                first = model_cache.ensure_model(device, "/runtime/python", Mock())
                second = model_cache.ensure_model(device, "/runtime/python", Mock())
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertEqual(first["model_path"], second["model_path"])
            self.assertEqual(network.call_count, 1)

    def test_existing_folder_is_used_in_place_without_downloading_or_cache_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = cached_model(root).resolve()
            device = {"model": MODEL, "model_source": "folder", "model_path": str(folder), "model_cache": str(root / "unused")}
            with patch.object(model_cache, "download_model") as network:
                result = model_cache.ensure_model(device, "/runtime/python", Mock())
            self.assertEqual(result['model_path'], str(folder))
            self.assertEqual(result['source'], 'folder')
            self.assertFalse((root / "unused").exists())
            network.assert_not_called()
            device['model_path'] = str(root / 'missing')
            with self.assertRaisesRegex(RuntimeError, '모델 폴더'):
                model_cache.ensure_model(device, "/runtime/python", Mock())

    def test_partial_snapshots_are_not_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            snapshot = cached_model(cache)
            (snapshot / "model-00001.safetensors").write_bytes(b"truncated")
            self.assertIsNone(model_cache.find_model(MODEL, [cache]))
            with patch.object(model_cache.subprocess, "run") as network, self.assertRaisesRegex(RuntimeError, "모델이 아직"):
                model_cache.ensure_model({"model": MODEL, "model_cache": str(cache)}, "/runtime/python", Mock(), install=False)
            network.assert_not_called()

    def test_explicit_shared_cache_is_optional_and_permission_errors_do_not_use_sudo(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(model_cache.os, "access", return_value=False), \
             patch.object(model_cache.subprocess, "run") as process, \
             self.assertRaisesRegex(RuntimeError, "쓰기 권한"):
            model_cache.writable_cache(Path(temporary))
        process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
