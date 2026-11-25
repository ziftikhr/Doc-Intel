"""Text -> vector. Two implementations behind one small protocol:

* ``GeminiEmbedder``  - real semantic embeddings (``gemini-embedding-001``, 768 dims).
  Chosen over ``gemini-embedding-2`` after measuring both on this project's invoices:
  001 honours the query/document task types and ranked the right invoice first for
  6/6 test queries, 2 ignored the task types and got 4/6.
* ``HashingEmbedder`` - offline fallback: hashed words + character trigrams. It is
  lexical, not semantic ("laptop" will not match "MacBook"), but needs no network or
  key, so the system and its tests run anywhere.

Every stored vector records which model made it (``embedding_model``); search only
compares vectors from the same model, so switching models never mixes spaces.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import time
import unicodedata
from typing import Optional, Protocol

from docintel.settings import EMBEDDING_DIM, Settings

log = logging.getLogger(__name__)


class EmbeddingError(Exception):
    pass


class Embedder(Protocol):
    model: str
    dim: int
    # Semantic matches below this cosine similarity are treated as unrelated, and so
    # are matches more than ``relative_margin`` below the best one (None = no relative
    # cut). Both are model-specific: Gemini similarities sit in a narrow band
    # (relevant ~0.54-0.67, unrelated up to ~0.61), so a fixed cut-off alone cannot work.
    min_similarity: float
    relative_margin: Optional[float]

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def estimate_tokens(text: str) -> int:
    """Rough token count (about 3 characters per token for invoice text: numbers and
    codes tokenise worse than prose). Used only to pace requests, so erring high is fine."""
    return len(text) // 3 + 1


def batches_by_tokens(texts: list[str], max_items: int, max_tokens: int) -> list[list[str]]:
    """Split texts into consecutive batches of at most ``max_items`` texts and roughly
    ``max_tokens`` tokens (a single oversized text still gets its own batch)."""
    out: list[list[str]] = []
    batch: list[str] = []
    used = 0
    for t in texts:
        n = estimate_tokens(t)
        if batch and (len(batch) >= max_items or used + n > max_tokens):
            out.append(batch)
            batch, used = [], 0
        batch.append(t)
        used += n
    if batch:
        out.append(batch)
    return out


_RETRY_RE = re.compile(r"retry in ([\d.]+)\s*s|retryDelay['\"]?:\s*['\"]?([\d.]+)s", re.I)


def _rate_limit_wait(message: str) -> Optional[float]:
    """Seconds to wait if this is a per-minute rate limit; None if it is not (or is a
    daily / zero quota, which waiting a minute will not fix)."""
    m = message.lower()
    if "429" not in m and "resource_exhausted" not in m and "rate" not in m:
        return None
    if "per day" in m or "perday" in m or "limit: 0," in m:
        return None
    hit = _RETRY_RE.search(message)
    return float(hit.group(1) or hit.group(2)) if hit else 30.0


class GeminiEmbedder:
    min_similarity = 0.50
    relative_margin = 0.06

    def __init__(self, model: str = "gemini-embedding-001", api_key: Optional[str] = None,
                 dim: int = EMBEDDING_DIM, batch_size: int = 50, timeout_s: float = 60,
                 tokens_per_minute: int = 30_000, max_wait_s: float = 600, sleep=time.sleep,
                 clock=time.monotonic):
        from google import genai
        from google.genai import types

        self.model, self.dim, self.batch_size = model, dim, batch_size
        # Free tier allows 30k embedding tokens per minute. A large upload (a 1,000-page
        # batch is ~600k tokens) must be sent at that pace, or every request is refused
        # and the document is left with keyword search only. Budget 80% of it, since
        # the token estimate is approximate.
        self.tokens_per_minute = tokens_per_minute
        self.batch_tokens = max(1000, int(tokens_per_minute * 0.8) // 3)
        self.max_wait_s = max_wait_s
        self._sleep, self._clock = sleep, clock
        self._sent: list[tuple[float, int]] = []  # (time, tokens) in the last minute
        self._types = types
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.client = genai.Client(api_key=key, http_options=types.HttpOptions(
            timeout=int(timeout_s * 1000), retry_options=types.HttpRetryOptions(attempts=3)))

    def _pace(self, tokens: int) -> None:
        """Wait until sending ``tokens`` more keeps us under the per-minute budget."""
        budget = int(self.tokens_per_minute * 0.8)
        while True:
            now = self._clock()
            self._sent = [(t, n) for t, n in self._sent if now - t < 60]
            used = sum(n for _, n in self._sent)
            if not self._sent or used + tokens <= budget:
                return
            self._sleep(max(0.5, 60 - (now - self._sent[0][0])))

    def _call(self, contents, task: str):
        types = self._types
        waited = 0.0
        while True:
            try:
                return self.client.models.embed_content(
                    model=self.model, contents=contents,
                    config=types.EmbedContentConfig(task_type=task, output_dimensionality=self.dim))
            except Exception as e:  # noqa: BLE001
                wait = _rate_limit_wait(str(e))
                if wait is None or waited + wait > self.max_wait_s:
                    msg = str(e)
                    quota = msg.find("Quota exceeded")  # name the limit, not just "429"
                    detail = msg[quota:quota + 300] if quota >= 0 else msg[:300]
                    raise EmbeddingError(f"{type(e).__name__}: {detail}") from e
                log.info("embedding rate limit hit; waiting %.0f s", wait + 1)
                self._sleep(wait + 1)
                waited += wait + 1

    def _embed(self, texts: list[str], task: str) -> list[list[float]]:
        types = self._types
        out: list[list[float]] = []
        for batch in batches_by_tokens(texts, self.batch_size, self.batch_tokens):
            tokens = sum(estimate_tokens(t) for t in batch)
            self._pace(tokens)
            # One Content per text: a plain list of strings is merged into a single input
            # by gemini-embedding-2 and would return one vector for the whole batch.
            contents = [types.Content(parts=[types.Part(text=t or " ")]) for t in batch]
            resp = self._call(contents, task)
            self._sent.append((self._clock(), tokens))
            vectors = [e.values for e in (resp.embeddings or [])]
            if len(vectors) != len(batch) or any(len(v) != self.dim for v in vectors):
                raise EmbeddingError(f"expected {len(batch)} vectors of {self.dim} dims, got "
                                     f"{[len(v) for v in vectors]}")
            # Reduced-dimension Gemini vectors are not unit length; cosine needs them to be.
            out.extend(_normalize(list(v)) for v in vectors)
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "RETRIEVAL_QUERY")[0]


_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


class HashingEmbedder:
    model = "hashing-v1"
    min_similarity = 0.12
    relative_margin = None

    def __init__(self, dim: int = EMBEDDING_DIM):
        self.dim = dim

    def _features(self, text: str) -> list[tuple[str, float]]:
        s = unicodedata.normalize("NFKC", text).casefold()
        words = _TOKEN.findall(s)
        feats = [(f"w:{w}", 1.0) for w in words]
        for w in words:
            padded = f"#{w}#"
            feats.extend((f"c:{padded[i:i + 3]}", 0.5) for i in range(len(padded) - 2))
        return feats

    def _vector(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for feat, weight in self._features(text):
            h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += weight if (h >> 63) & 1 else -weight
        return _normalize(v)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def make_embedder(settings: Settings) -> tuple[Optional[Embedder], str]:
    """Return (embedder, description). ``None`` means keyword search only."""
    provider = settings.embedding_provider.lower()
    has_key = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    if provider == "auto":
        provider = "gemini" if has_key else "hashing"
    if provider in ("none", ""):
        return None, "disabled"
    if provider == "hashing":
        return HashingEmbedder(), "hashing-v1 (offline, lexical)"
    if provider == "gemini":
        if not has_key:
            return None, "gemini selected but GEMINI_API_KEY is not set"
        try:
            return GeminiEmbedder(settings.embedding_model), settings.embedding_model
        except ImportError:
            return None, "google-genai is not installed"
    return None, f"unknown embedding provider {provider!r}"
