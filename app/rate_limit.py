"""Project adapter over the shared rate-limit engine.

The engine (`rate_limit_engine.py`) is vendored unmodified from
`shared/rate-limit/engine/fastapi/`. This adapter only supplies the project slug
and its single LLM bucket; the engine handles X-Forwarded-For, central config,
event mode, the kill switch (503), and fail-open behaviour.

Bucket "agent": one real-AI agent run in the public demo (issuing a real-AI
pass). Bucket "agent_call": every real model call the proxy forwards for the
public demo. Together they are the demo's only cost control on real AI; each
pass's own money limit is the product being demonstrated, not cost control.
"""

from __future__ import annotations

import os

from fastapi import Request

from .rate_limit_engine import RateLimiter


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


limiter = RateLimiter(
    # Slug = full project folder name (matches the central control sheet rows).
    project="ail-ay2627-s1-philbert-leash",
    buckets={"agent": _env_int("RATE_LIMIT_AGENT_MAX", 5), "agent_call": _env_int("RATE_LIMIT_AGENT_CALL_MAX", 40)},
    redis_url=os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("UPSTASH_REDIS_URL", ""),
    redis_token=os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("UPSTASH_REDIS_TOKEN", ""),
    default_window=_env_int("RATE_LIMIT_WINDOW_SECONDS", 432000),
)


def check_rate_limit(request: Request, bucket: str) -> None:
    limiter.enforce(request, bucket)
