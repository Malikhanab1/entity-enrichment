# finetuning/serve.py
#
# FastAPI inference server for the fine-tuned lead-classifier.
#
# Loads the merged model from lead-model-merged/ — this contains the full
# Qwen2.5-0.5B backbone + LoRA weights merged in + the trained `score`
# classification head. Loading from the raw adapter directory (finetuning/)
# causes score.weight to be randomly re-initialized because it lives outside
# the LoRA adapter files and is only preserved in the merged checkpoint.
#
# Usage:
#   uv run python serve.py
#
# Endpoints:
#   POST /classify   { "title": "...", "text": "..." }
#                 -> { "lead": 0|1, "confidence": 0.0-1.0 }
#   GET  /health  -> { "status": "ok", "model": "...", "device": "..." }

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from functools import partial

import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from transformers import AutoModelForSequenceClassification, AutoTokenizer

# ── Config ────────────────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Point at the merged model directory, not the raw adapter directory.
# The merged model has score.weight saved as part of the checkpoint.
# Override with CLASSIFIER_ADAPTER_DIR if your merged model lives elsewhere.
ADAPTER_DIR = os.environ.get(
    "CLASSIFIER_ADAPTER_DIR",
    os.path.join(_SCRIPT_DIR, "lead-model-merged"),
)

PORT = int(os.environ.get("CLASSIFIER_PORT", 8001))
MAX_CHARS = int(os.environ.get("CLASSIFIER_MAX_CHARS", 3000))
MAX_TOKENS = int(os.environ.get("CLASSIFIER_MAX_TOKENS", 512))

# Predictions below this threshold are treated as non-leads regardless of
# the predicted class — a low-confidence lead prediction is not actionable.
CONFIDENCE_THRESHOLD = float(os.environ.get("CLASSIFIER_CONFIDENCE_THRESHOLD", 0.55))

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("classifier")

# ── Model state (loaded once at startup) ─────────────────────────────────────
_tokenizer = None
_model = None
_device = None

# Semaphore so burst traffic doesn't fire dozens of simultaneous forward
# passes at a CPU-only model — they don't parallelise, they just thrash.
# Raise CLASSIFIER_MAX_CONCURRENCY if you have a GPU that can batch.
_MAX_CONCURRENCY = int(os.environ.get("CLASSIFIER_MAX_CONCURRENCY", 1))
_sem: asyncio.Semaphore | None = None


def _load_model_sync():
    """Blocking model load — called once from the lifespan, off the event loop."""
    global _tokenizer, _model, _device

    if not os.path.isdir(ADAPTER_DIR):
        raise RuntimeError(
            f"Model directory not found: {ADAPTER_DIR}\n"
            "Run merge_and_convert.py first, or set CLASSIFIER_ADAPTER_DIR."
        )

    _device = "cuda" if torch.cuda.is_available() else "cpu"

    log.info(f"Loading tokenizer from {ADAPTER_DIR} ...")
    _tokenizer = AutoTokenizer.from_pretrained(ADAPTER_DIR)

    log.info(f"Loading merged SEQ_CLS model from {ADAPTER_DIR} on {_device} ...")
    # Use plain AutoModelForSequenceClassification — NOT AutoPeftModel —
    # because the LoRA weights are already merged into the checkpoint.
    # AutoPeftModel on a merged checkpoint re-wraps with PEFT and then
    # can't find score.weight in the adapter files, so it reinitializes it.
    _model = AutoModelForSequenceClassification.from_pretrained(
        ADAPTER_DIR,
        dtype=torch.float16 if _device == "cuda" else torch.float32,
        ignore_mismatched_sizes=True,
    )
    _model.to(_device)
    _model.eval()
    log.info(
        f"Model ready  (device={_device}  threshold={CONFIDENCE_THRESHOLD}  concurrency={_MAX_CONCURRENCY})"
    )


def _infer_sync(title: str, text: str) -> tuple[int, float]:
    """
    Pure-CPU/GPU forward pass — intentionally synchronous and side-effect-free
    so it is safe to run inside asyncio.get_event_loop().run_in_executor().
    Returns (predicted_class, confidence).
    """
    combined = f"Title: {title}\nText: {text}"

    inputs = _tokenizer(
        combined,
        return_tensors="pt",
        truncation=True,
        max_length=MAX_TOKENS,
        padding=False,
    ).to(_device)

    with torch.no_grad():
        outputs = _model(**inputs)
        probs = torch.softmax(outputs.logits, dim=-1)[0]
        predicted_class = int(torch.argmax(probs).item())
        confidence = float(probs[predicted_class].item())

    return predicted_class, confidence


# ── FastAPI app ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sem

    # Run the blocking model load in a thread so we don't stall the event loop.
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _load_model_sync)

    # Create the semaphore here so it belongs to the correct event loop.
    _sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    yield


app = FastAPI(title="lead-classifier", lifespan=lifespan)


class ClassifyRequest(BaseModel):
    title: str
    text: str


class ClassifyResponse(BaseModel):
    lead: int  # 0 or 1
    confidence: float  # probability of the predicted class, 0.0–1.0


@app.get("/health")
async def health():
    return {"status": "ok", "model": ADAPTER_DIR, "device": str(_device)}


@app.post("/classify", response_model=ClassifyResponse)
async def classify(req: ClassifyRequest):
    if _model is None or _tokenizer is None:
        raise HTTPException(status_code=503, detail="Model not loaded yet")

    title = req.title[:MAX_CHARS]
    text = req.text[:MAX_CHARS]

    loop = asyncio.get_event_loop()

    # Acquire the concurrency slot — awaiting here yields control back to the
    # event loop so other requests (and /health checks) stay responsive while
    # we wait for a free inference slot.
    async with _sem:
        predicted_class, confidence = await loop.run_in_executor(
            None, partial(_infer_sync, title, text)
        )

    # Low-confidence predictions are unreliable — treat as non-leads rather
    # than passing noisy positives downstream. All requests get a response;
    # we never drop or 500 a request just because the model was uncertain.
    if confidence < CONFIDENCE_THRESHOLD:
        log.warning(
            f"low-confidence  lead={predicted_class}  confidence={confidence:.3f}  "
            f"title={title[:60]!r}  → returning lead=0"
        )
        return ClassifyResponse(lead=0, confidence=confidence)

    log.info(
        f"lead={predicted_class}  confidence={confidence:.3f}  title={title[:60]!r}"
    )
    return ClassifyResponse(lead=predicted_class, confidence=confidence)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
