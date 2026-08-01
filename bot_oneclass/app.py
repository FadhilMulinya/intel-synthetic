"""
app.py -- FastAPI/uvicorn wrapper around predict.classify() for deploying
the bot/human classifier as an HTTP service.

Deliberately thin: all real logic (fetch, features, model, uncertain band)
stays in predict.py, which is also usable standalone from the CLI. This
file only adds an HTTP layer + input validation + a health check, so
predict.py doesn't quietly develop two divergent code paths.

RUN LOCALLY
-----------
    pip install -r requirements.txt fastapi uvicorn
    uvicorn app:app --host 0.0.0.0 --port 8000 --reload
    # (MODEL_PATH resolves relative to this file's location, not cwd, so
    # this works whether you run it from bot_oneclass/ or its parent dir)

    # from another terminal:
    curl "http://localhost:8000/classify/ckb1qzda0cr08m85hc8jlnfp3zer7xulejywt49kt2rr0vthywaa50xwsq9j8mmc2fzdz24pkq4rrfe3nkywsflvtn2v9wc9"
    curl http://localhost:8000/health

DEPLOYMENT
----------
See the accompanying Dockerfile (in the ckb_model/ parent dir) for a
container-based deploy -- build context is ckb_model/ so it can COPY both
requirements.txt and the rest of bot_oneclass/ in one shot.
"""
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

import predict as pr

app = FastAPI(
    title="CKB Bot/Human Classifier",
    description=(
        "Behavioral classifier for CKB addresses. Returns bot_probability "
        "plus a verdict of bot / human / uncertain -- see /health for model "
        "metadata, and README.md in this repo for label-provenance caveats "
        "before trusting any single verdict as ground truth."
    ),
    version="1.0",
)

# Load once at process start, not per-request -- joblib.load() + the sklearn
# bundle is not free, and predict.classify() currently reloads it internally.
# We keep using classify()'s own loading for correctness (single source of
# truth), but warm it once here so the first real request isn't slow AND so
# a broken/missing model.joblib fails fast at startup, not on first request.
_startup_ok = {"loaded": False, "error": None}


@app.on_event("startup")
def _warm_model():
    try:
        import joblib
        joblib.load(pr.MODEL_PATH)
        _startup_ok["loaded"] = True
    except Exception as e:  # noqa: BLE001 -- deliberately broad, this is a health signal
        _startup_ok["error"] = str(e)


class ClassifyResponse(BaseModel):
    address: str
    n_tx_fetched: int
    bot_probability: Optional[float] = None
    verdict: str
    warning: Optional[str] = None
    reason: Optional[str] = None


@app.get("/health")
def health():
    status = "ok" if _startup_ok["loaded"] else "degraded"
    body = {"status": status, "model_path": pr.MODEL_PATH}
    if _startup_ok["error"]:
        body["error"] = _startup_ok["error"]
    return JSONResponse(body, status_code=200 if _startup_ok["loaded"] else 503)


@app.get("/classify/{address}", response_model=ClassifyResponse)
def classify_address(address: str, max_tx: int = 300):
    """Classify a single CKB address. Mirrors `python3 predict.py <address> --json`
    exactly -- same function, same model, same uncertain-band logic."""
    if max_tx < 2 or max_tx > 1000:
        raise HTTPException(status_code=400, detail="max_tx must be between 2 and 1000")
    try:
        result = pr.classify(address, max_tx=max_tx)
    except pr.ApiError as e:
        # upstream explorer API issue (rate limit, address not found, timeout)
        # -- surface as 502, not 500, since it's not our code that's broken
        raise HTTPException(status_code=502, detail=str(e))
    except pr.ModelLoadError as e:
        # our deployment is misconfigured (missing/corrupt model.joblib) --
        # surface as 500, distinct from an upstream API problem
        raise HTTPException(status_code=500, detail=str(e))
    return result


@app.get("/")
def root():
    return {
        "service": "ckb-bot-oneclass",
        "endpoints": ["/health", "/classify/{address}", "/docs"],
    }
