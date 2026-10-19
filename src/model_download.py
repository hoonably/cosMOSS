"""Standalone HF cache downloader with byte progress, also sent over SSH.

Uses public HF APIs and per-file tqdm callbacks so resumed, parallel and Xet
transfers share one fixed repository total. No inference runtime is imported.
"""
import json
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class DownloadProgress:
    def __init__(self, sizes, emit):
        self.sizes = sizes
        self.received = {name: 0 for name in sizes}
        self.emit = emit
        self.lock = threading.Lock()
        self.finished = False

    def update(self, name, value):
        with self.lock:
            self.received[name] = max(0, min(value, self.sizes[name]))

    def snapshot(self):
        with self.lock:
            downloaded = sum(self.received.values())
            total = sum(self.sizes.values())
            # Only a successfully finished download may report 100%.
            percent = min(99.9, 100 * downloaded / total) if total else None
            return {"downloaded_bytes": downloaded, "total_bytes": total,
                    "percent": 100.0 if self.finished else percent, "complete": self.finished}

    def publish(self):
        self.emit(self.snapshot())


def download_model(model, cache, emit):
    from huggingface_hub import HfApi, hf_hub_download, snapshot_download
    from huggingface_hub.utils import tqdm

    info = HfApi().model_info(model, files_metadata=True)
    files = info.siblings
    sizes = {file.rfilename: file.size for file in files}
    if not all(isinstance(size, int) and size >= 0 for size in sizes.values()):
        raise RuntimeError("모델 파일의 전체 용량을 확인하지 못했습니다. 다시 시도해 주세요.")
    progress = DownloadProgress(sizes, emit)
    blobs = Path(cache) / ("models--" + model.replace("/", "--")) / "blobs"
    for file in files:
        lfs = file.lfs
        digest = (lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None)) if lfs else file.blob_id
        if digest and (blobs / digest).is_file() and (blobs / digest).stat().st_size == file.size:
            progress.update(file.rfilename, file.size)
    stopped = threading.Event()

    def report():
        while not stopped.wait(1):
            progress.publish()

    active_file = threading.local()

    class FileProgress(tqdm):
        def __init__(self, *args, **kwargs):
            self.filename = active_file.name
            self.observed = kwargs.get("initial", 0)
            kwargs["disable"] = True  # JSON events replace terminal bars.
            super().__init__(*args, **kwargs)
            progress.update(self.filename, self.observed)

        def update(self, n=1):
            # tqdm skips its own counter when disabled; track independently.
            self.observed += n or 0
            self.n = self.observed
            progress.update(self.filename, self.observed)

    public_callback = "tqdm_class" in inspect.signature(hf_hub_download).parameters
    original_tqdm = None
    if not public_callback:
        # Transformers 4.x requires Hub <1. Older Hub exposes byte bars only via
        # this module symbol. The hook is isolated to this downloader subprocess,
        # restored below, and captures each filename in its owning worker thread.
        from huggingface_hub import file_download
        original_tqdm = file_download.tqdm
        file_download.tqdm = FileProgress

    def fetch(file):
        active_file.name = file.rfilename
        options = {"tqdm_class": FileProgress} if public_callback else {}
        hf_hub_download(repo_id=model, filename=file.rfilename, revision=info.sha,
                        cache_dir=str(cache), **options)
        progress.update(file.rfilename, file.size)

    progress.publish()
    reporter = threading.Thread(target=report, daemon=True)
    reporter.start()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(fetch, files))
        path = snapshot_download(repo_id=model, revision=info.sha, cache_dir=str(cache), local_files_only=True)
        progress.finished = True
    finally:
        stopped.set()
        reporter.join()
        if original_tqdm is not None:
            file_download.tqdm = original_tqdm
        progress.publish()
    return str(path)


def main():
    import sys
    path = download_model(sys.argv[1], sys.argv[2],
                          lambda progress: print("COSMOSS_DOWNLOAD:" + json.dumps(progress), flush=True))
    print("COSMOSS_MODEL_PATH:" + path, flush=True)


if __name__ == "__main__":
    main()
