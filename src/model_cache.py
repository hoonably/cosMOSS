"""Remote model prefetch using the SSH account's normal Hugging Face cache."""

import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path


def default_cache():
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"]).expanduser()
    home = Path(os.environ.get("HF_HOME") or (Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "huggingface"))
    return home.expanduser() / "hub"


def complete_model(path):
    """Reject partial HF snapshots, including missing/truncated weight shards."""
    path = Path(path)
    try:
        configs = [json.loads((path / filename).read_text()) for filename in
                   ("config.json", "tokenizer_config.json", "preprocessor_config.json", "processor_config.json")]
        if not (path / "tokenizer.json").is_file() and not all((path / name).is_file() for name in ("vocab.json", "merges.txt")):
            return False
        for config in configs:
            for mapping in config.get("auto_map", {}).values():
                for entry in mapping if isinstance(mapping, list) else [mapping]:
                    if entry and not (path / (entry.split("--")[-1].rsplit(".", 1)[0] + ".py")).is_file():
                        return False
        index = path / "model.safetensors.index.json"
        if index.is_file():
            metadata = json.loads(index.read_text())
            weights = [path / name for name in set(metadata["weight_map"].values())]
            return bool(weights) and all(file.is_file() and file.stat().st_size > 0 for file in weights) and sum(
                file.stat().st_size for file in weights
            ) >= metadata.get("metadata", {}).get("total_size", 0)
        return (path / "model.safetensors").is_file() and (path / "model.safetensors").stat().st_size > 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def find_model(model, caches):
    for cache in caches:
        cache = Path(cache)
        repository = cache / ("models--" + model.replace("/", "--"))
        candidates = []
        reference = repository / "refs" / "main"
        if reference.is_file():
            revision = reference.read_text().strip()
            if revision and "/" not in revision and revision not in {".", ".."}:
                candidates.append(repository / "snapshots" / revision)
        snapshots = repository / "snapshots"
        if snapshots.is_dir():
            candidates.extend(sorted(snapshots.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True))
        for snapshot in candidates:
            if complete_model(snapshot):
                return snapshot.resolve(), cache.resolve()
    return None


def cache_candidates(device):
    return [Path(device["model_cache"]).expanduser() if device.get("model_cache") else default_cache()]


def writable_cache(cache):
    cache = Path(cache)
    try:
        cache.mkdir(parents=True, exist_ok=True, mode=0o755)
    except PermissionError:
        raise RuntimeError("모델 캐시 경로에 쓰기 권한이 없습니다: %s. 고급 설정에서 쓰기 가능한 경로를 지정해 주세요." % cache) from None
    if not cache.is_dir() or not os.access(cache, os.W_OK | os.X_OK):
        raise RuntimeError("모델 캐시 경로에 쓰기 권한이 없습니다: %s. 고급 설정에서 쓰기 가능한 경로를 지정해 주세요." % cache)
    return cache


def ensure_model(device, python, emit, install=True):
    cache = cache_candidates(device)[0]
    if not cache.is_absolute():
        raise RuntimeError("모델 캐시 경로는 절대 경로 또는 ~/로 시작해야 합니다.")
    emit("checking_model", source=device.get("model_source", "cache"), model_cache=str(cache))
    if device.get("model_source") == "folder":
        selected = Path(device.get("model_path", "")).expanduser()
        if not selected.is_absolute() or not complete_model(selected):
            raise RuntimeError("선택한 장치에 올바른 MOSS 모델 폴더가 없습니다. 설정, 토크나이저, 가중치 파일이 있는 경로를 입력해 주세요.")
        return {"model_path": str(selected.resolve()), "model_cache": str(cache), "source": "folder", "reused": True}
    found = find_model(device["model"], cache_candidates(device))
    if found:
        return {"model_path": str(found[0]), "model_cache": str(found[1]), "reused": True}
    if not install:
        raise RuntimeError("모델이 아직 준비되지 않았습니다. ‘모델 준비’를 눌러 주세요.")
    cache = writable_cache(cache)
    lock = cache / (".cosmoss-" + hashlib.sha256(device["model"].encode()).hexdigest() + ".lock")
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        found = find_model(device["model"], [cache])
        if found:
            return {"model_path": str(found[0]), "model_cache": str(found[1]), "reused": True}
        emit("downloading")
        model_path = download_model(device, python, cache, emit)
        if not model_path.is_relative_to(cache.resolve()) or not complete_model(model_path):
            raise RuntimeError("모델 다운로드가 완전하지 않습니다. 다시 준비해 주세요.")
        return {"model_path": str(model_path), "model_cache": str(cache.resolve()), "reused": False}


def download_model(device, python, cache, emit):
    script = globals().get("DOWNLOAD_SOURCE")
    if script is None:
        script = Path(__file__).with_name("model_download.py").read_text()
    environment = dict(globals().get("DOWNLOAD_ENVIRONMENT") or os.environ, HF_HUB_CACHE=str(cache))
    result = None
    with subprocess.Popen([str(python), "-u", "-c", script, device["model"], str(cache)],
                          stdout=subprocess.PIPE, text=True, env=environment) as process:
        for line in process.stdout:
            if line.startswith("COSMOSS_DOWNLOAD:"):
                emit("downloading", download=json.loads(line.split(":", 1)[1]))
            elif line.startswith("COSMOSS_MODEL_PATH:"):
                result = line.split(":", 1)[1].strip()
        if process.wait() or not result:
            raise RuntimeError("모델 다운로드에 실패했습니다. 연결과 저장 공간을 확인해 주세요.")
    return Path(result).resolve()
