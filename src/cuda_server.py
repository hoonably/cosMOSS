"""Single-GPU MOSS worker, launched with a GPU UUID in CUDA_VISIBLE_DEVICES."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse


MODEL_ID = os.environ.get("MOSS_MODEL_NAME", "OpenMOSS-Team/MOSS-Transcribe-Diarize")
MODEL_PATH = os.environ.get("MOSS_MODEL_PATH", "")
DEVICE_ID = os.environ.get("COSMOSS_DEVICE_ID", "")
GPU_UUID = os.environ.get("CUDA_VISIBLE_DEVICES", "")
API_TOKEN = os.environ.get("COSMOSS_WORKER_TOKEN", "")
OPTIONS = json.loads(os.environ.get("COSMOSS_MODEL_OPTIONS", "{}"))
MODEL: Any = None
PROCESSOR: Any = None
DTYPE: Any = None
GPU_LOCK: Any = None
LOCK = asyncio.Lock()
JOBS: set[asyncio.Task] = set()
DEFAULT_PROMPT = (
    "请将音频转写为文本，每一段需以起始时间戳和说话人编号（[S01]、[S02]、[S03]…）开头，"
    "正文为对应的语音内容，并在段末标注结束时间戳，以清晰标明该段语音范围。"
)


def load_model() -> None:
    global MODEL, PROCESSOR, DTYPE, GPU_LOCK
    import fcntl
    import torch
    from transformers import AutoModelForCausalLM, AutoProcessor

    if not GPU_UUID.startswith("GPU-") or not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("The selected CUDA GPU is unavailable; CPU fallback is disabled")
    locks = Path.home() / ".local" / "share" / "cosmoss" / "gpu-locks"
    locks.mkdir(parents=True, exist_ok=True)
    GPU_LOCK = (locks / (GPU_UUID + ".lock")).open("a")
    try:
        fcntl.flock(GPU_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise RuntimeError("Another cosMOSS worker under this user already owns this GPU") from error
    precision = OPTIONS.get("precision", "auto")
    if precision == "bf16" and not torch.cuda.is_bf16_supported():
        raise RuntimeError("선택한 GPU는 BF16을 지원하지 않습니다. 자동 또는 FP16을 선택해 주세요.")
    DTYPE = torch.float16 if precision == "fp16" else (torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16)
    if not MODEL_PATH or not Path(MODEL_PATH).is_dir():
        raise RuntimeError("A prepared model path is required; prepare the device model first")
    kwargs = dict(trust_remote_code=True, dtype=DTYPE,
                  attn_implementation=OPTIONS.get("attention", "sdpa"), local_files_only=True)
    if precision in {"int8", "int4"}:
        from transformers import BitsAndBytesConfig
        capability = torch.cuda.get_device_capability()
        if capability < ((7, 5) if precision == "int8" else (6, 0)):
            raise RuntimeError("선택한 GPU는 이 양자화 방식을 지원하지 않습니다. 원본 모델을 선택해 주세요.")
        kwargs.update(device_map={"": "cuda:0"}, quantization_config=BitsAndBytesConfig(
            load_in_8bit=precision == "int8", load_in_4bit=precision == "int4",
            llm_int8_skip_modules=["whisper_encoder", "vq_adaptor", "lm_head"],
            bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=DTYPE, bnb_4bit_use_double_quant=True))
        MODEL = AutoModelForCausalLM.from_pretrained(MODEL_PATH, **kwargs).eval()
    else:
        MODEL = AutoModelForCausalLM.from_pretrained(MODEL_PATH, **kwargs).to(dtype=DTYPE).to("cuda:0").eval()
    MODEL.generation_config.use_cache = OPTIONS.get("use_cache", True)
    PROCESSOR = AutoProcessor.from_pretrained(MODEL_PATH, trust_remote_code=True, local_files_only=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(load_model)
    yield
    if JOBS:
        await asyncio.gather(*JOBS, return_exceptions=True)


app = FastAPI(title="cosMOSS CUDA worker", lifespan=lifespan)


@app.middleware("http")
async def authorize(request: Request, call_next):
    if not API_TOKEN or not secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer " + API_TOKEN):
        return JSONResponse({"detail": "Worker authentication required"}, status_code=401)
    return await call_next(request)


@app.get("/healthz")
def health() -> dict[str, Any]:
    return {"ok": MODEL is not None, "app": "cosMOSS CUDA worker", "device_id": DEVICE_ID,
            "gpu_uuid": GPU_UUID, "model": MODEL_ID, "model_path": MODEL_PATH, "options": OPTIONS}


@app.get("/v1/models")
def models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "OpenMOSS"}]}


def generate(path: str, prompt: str, max_tokens: int) -> dict[str, Any]:
    import torch
    from moss_transcribe_diarize.inference_utils import build_transcription_messages, generate_transcription

    started = time.monotonic()
    result = generate_transcription(
        MODEL, PROCESSOR, build_transcription_messages(path, prompt=prompt),
        max_new_tokens=max_tokens, do_sample=False, device=torch.device("cuda:0"), dtype=DTYPE,
    )
    return {"text": result["text"], "duration": round(time.monotonic() - started, 2)}


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...), model: str = Form(MODEL_ID), prompt: str = Form(""),
    language: str = Form(""), hotwords: str = Form(""), max_completion_tokens: int = Form(32768, ge=1, le=32768),
    x_cosmoss_device_id: str = Header(""), x_cosmoss_gpu_uuid: str = Header(""),
) -> dict[str, Any]:
    if x_cosmoss_device_id != DEVICE_ID or x_cosmoss_gpu_uuid != GPU_UUID:
        raise HTTPException(409, "The requested GPU does not match this worker")
    if model != MODEL_ID:
        raise HTTPException(400, "The requested model is not loaded")
    prompt = prompt or DEFAULT_PROMPT
    if language:
        prompt += f"\n语言提示：主要语音为{language}。请按语音原语言转写，不要翻译。"
    if hotwords and "热词提示" not in prompt:
        prompt += f"\n热词提示：{hotwords}"
    handle, filename = tempfile.mkstemp(suffix=".wav")
    try:
        with os.fdopen(handle, "wb") as destination:
            while chunk := await file.read(1024 * 1024):
                destination.write(chunk)
    except BaseException:
        Path(filename).unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    async def job():
        try:
            async with LOCK:
                return await asyncio.to_thread(generate, filename, prompt, max_completion_tokens)
        finally:
            Path(filename).unlink(missing_ok=True)

    # Retain the GPU lock and temporary file until the inference thread really
    # finishes, even if the client disconnects from this JSON-only worker.
    task = asyncio.create_task(job())
    JOBS.add(task)
    task.add_done_callback(lambda finished: (JOBS.discard(finished), None if finished.cancelled() else finished.exception()))
    try:
        return await asyncio.shield(task)
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error
