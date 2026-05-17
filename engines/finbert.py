"""FinBERT financial sentiment engine — v20.3 (optional, GPU-aware).

OPT-IN. Disabled by default. Returns empty if torch+transformers aren't
installed, so requirements.txt is unaffected for the typical install.

To enable:
  pip install torch transformers
  # OR with CUDA (GPU):
  pip install torch --index-url https://download.pytorch.org/whl/cu121
  pip install transformers

Once installed, this engine wakes up automatically — it auto-detects CUDA
and falls back to CPU if no GPU is present. State-pack doc estimated +30%
accuracy vs VADER on financial text, with a ~10x throughput win on GPU
batched encoding.

Reads from the news engine's output (headlines per ticker) and emits a
finbert_score in [-1, +1] per ticker. Fusion treats it as an additive
factor next to the existing VADER-based news sentiment, NOT a replacement —
disagreements between VADER and FinBERT are themselves informative.
"""
from __future__ import annotations
import logging
from typing import Dict, List, Optional, Tuple

import pandas as pd

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("optedge.finbert")

_MODEL = None
_DEVICE = None
_TOKENIZER = None
_MODEL_LOAD_ATTEMPTED = False   # avoid retrying every iter when load fails
_LOADED_MODEL_NAME: Optional[str] = None

# v20.6: try multiple FinBERT variants in priority order. The first one
# whose weights load successfully wins. We list safetensors-compatible
# models first because torch >= 2.5 + transformers refuses to load legacy
# .bin files (CVE-2025-32434) unless torch ≥ 2.6.
#
# All three models output 3-class financial sentiment. Label indices vary
# per model — _LABEL_IDX maps each one back to (positive, negative, neutral).
FINBERT_MODEL_CANDIDATES = [
    # name                                          (pos_idx, neg_idx, neu_idx)
    # yiyanghkust/finbert-tone REMOVED — repo has no tokenizer files at all
    # (config.json exists but tokenizer_config.json / tokenizer.json / vocab all 404),
    # so it cannot load regardless of use_fast. Skip it entirely.
    ("mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis", (2, 0, 1)),  # primary
    ("ProsusAI/finbert",                             (0, 1, 2)),  # fallback
]
_LABEL_IDX = (0, 1, 2)   # filled when the model loads


def _ensure_loaded() -> bool:
    """Lazy-load the model. Returns False if torch/transformers not installed."""
    global _MODEL, _DEVICE, _TOKENIZER
    if _MODEL is not None:
        return True
    try:
        import torch
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
    except ImportError:
        return False
    # v20.5: detailed GPU diagnostics so the user can self-diagnose why CUDA
    # isn't picked up (most common cause: pip installed the CPU-only wheel).
    try:
        cuda_avail = torch.cuda.is_available()
        log.info("finbert: torch=%s cuda_available=%s cuda_device_count=%d",
                  torch.__version__, cuda_avail,
                  torch.cuda.device_count() if cuda_avail else 0)
        if cuda_avail:
            try:
                log.info("finbert: CUDA device 0 = %s (compute %s)",
                          torch.cuda.get_device_name(0),
                          torch.cuda.get_device_capability(0))
            except Exception:
                pass
        else:
            # Help the user diagnose
            cv = getattr(torch.version, "cuda", None)
            if cv is None:
                log.warning("finbert: this torch build is CPU-only (no torch.version.cuda).")
                log.warning("finbert: to enable GPU, reinstall torch with the CUDA wheel:")
                log.warning("finbert:   pip uninstall -y torch")
                log.warning("finbert:   pip install torch --index-url https://download.pytorch.org/whl/cu121")
            else:
                log.warning("finbert: torch reports CUDA %s but cuda.is_available()=False.", cv)
                log.warning("finbert: usually means the NVIDIA driver is older than the CUDA runtime "
                            "this torch was built against. Update NVIDIA driver, or install a torch "
                            "matching your driver:")
                log.warning("finbert:   nvidia-smi   (check your driver's CUDA support)")
                log.warning("finbert: torch wheels: cu118, cu121, cu124. Pick one ≤ your driver's CUDA.")
    except Exception as e:
        log.debug("finbert: gpu probe failed (%s)", e)

    global _LOADED_MODEL_NAME, _LABEL_IDX, _MODEL_LOAD_ATTEMPTED
    if _MODEL_LOAD_ATTEMPTED:
        # Already tried in a previous iter and failed — don't retry every loop
        return _MODEL is not None
    _MODEL_LOAD_ATTEMPTED = True

    _DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
    # Try each candidate model in order. First one to load wins.
    last_err = None
    for model_name, label_idx in FINBERT_MODEL_CANDIDATES:
        try:
            log.info("finbert: trying %s on %s …", model_name, _DEVICE)
            _TOKENIZER = AutoTokenizer.from_pretrained(model_name)
            # Use float16 on CUDA for ~2x throughput with no accuracy loss on sentiment.
            # `dtype` is the current kwarg (torch_dtype was deprecated in transformers ≥ 4.x)
            _dtype = torch.float16 if _DEVICE == "cuda" else torch.float32
            _MODEL = AutoModelForSequenceClassification.from_pretrained(
                model_name, dtype=_dtype
            ).to(_DEVICE)
            _MODEL.eval()
            _LOADED_MODEL_NAME = model_name
            _LABEL_IDX = label_idx
            log.info("finbert: loaded %s (device=%s)", model_name, _DEVICE)
            return True
        except Exception as e:
            last_err = e
            msg = str(e)[:200]
            log.warning("finbert: %s load failed — %s", model_name, msg)
            # Reset partial state before trying the next candidate
            _TOKENIZER = None
            _MODEL = None
            continue
    log.warning("finbert: all candidate models failed; last error: %s",
                 str(last_err)[:200] if last_err else "?")
    return False


def _score_texts(texts: List[str], batch_size: int = 32) -> List[float]:
    """Batched FinBERT inference. Returns per-text score in [-1, +1]:
    positive prob minus negative prob. Label indices come from the
    `_LABEL_IDX` mapping that was set when the model loaded."""
    if not texts or not _ensure_loaded():
        return [0.0] * len(texts)
    import torch
    pos_idx, neg_idx, _neu_idx = _LABEL_IDX
    scores: List[float] = []
    with torch.no_grad():
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            enc = _TOKENIZER(batch, padding=True, truncation=True,
                              max_length=128, return_tensors="pt").to(_DEVICE)
            out = _MODEL(**enc)
            probs = torch.softmax(out.logits, dim=-1)
            s = (probs[:, pos_idx] - probs[:, neg_idx]).cpu().tolist()
            scores.extend([float(x) for x in s])
    return scores


def run(news_df: Optional[pd.DataFrame] = None,
        per_ticker_cap: int = 10) -> pd.DataFrame:
    """Score recent headlines per ticker via FinBERT.

    Expects `news_df` with columns: ticker + one of (top_headline, headline,
    title, summary). v20.5: now matches the optedge news engine's
    `top_headline` column.
    """
    if news_df is None or not hasattr(news_df, "empty") or news_df.empty:
        return pd.DataFrame()
    if not _ensure_loaded():
        return pd.DataFrame()

    # Find the text column — check in order of preference
    text_col = None
    for cand in ("top_headline", "headline", "title", "summary"):
        if cand in news_df.columns:
            text_col = cand
            break
    if text_col is None or "ticker" not in news_df.columns:
        log.warning("finbert: news_df missing text column. Available: %s",
                     list(news_df.columns)[:10])
        return pd.DataFrame()
    log.info("finbert: scoring %d rows using column '%s'", len(news_df), text_col)

    # Group per ticker — optedge's news engine emits one row per ticker, but
    # this also handles the multi-row case from other producers.
    grouped = (news_df.groupby("ticker")[text_col]
                       .apply(lambda s: [str(x).strip() for x in s.dropna()
                                          if str(x).strip()][:per_ticker_cap]))

    rows = []
    n_skipped = 0
    for tk, headlines in grouped.items():
        if not headlines:
            n_skipped += 1
            continue
        scores = _score_texts(headlines)
        if not scores:
            continue
        mean_score = sum(scores) / len(scores)
        rows.append({
            "ticker": tk,
            "finbert_score": max(-1.0, min(1.0, mean_score)),
            "finbert_n_headlines": len(headlines),
            "finbert_device": _DEVICE or "cpu",
            "finbert_headline_preview": headlines[0][:80] if headlines else "",
        })
    if not rows:
        log.info("finbert: no scorable headlines (skipped %d tickers w/ empty text)",
                  n_skipped)
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    log.info("finbert: scored %d tickers on %s (skipped %d empty)",
              len(out), _DEVICE, n_skipped)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Smoke test
    fake = pd.DataFrame([
        {"ticker": "AAPL", "headline": "Apple posts record iPhone sales and raises guidance"},
        {"ticker": "AAPL", "headline": "Antitrust regulators open new probe into App Store"},
        {"ticker": "TSLA", "headline": "Tesla recalls 500K vehicles over braking defect"},
        {"ticker": "NVDA", "headline": "Nvidia beats earnings, data-center revenue up 60%"},
    ])
    print(run(fake))
