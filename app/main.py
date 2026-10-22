"""Leash: a safety proxy that puts AI agents on a leash.

Agents never hold the real API key. They get short-lived, signed tokens
with hard limits; every decision is written to a tamper-evident log.

This build is the API for the public demo only (the pages live on Vercel):
    GET  /api/health                 health check
    POST /api/sb/session             open or reuse a visitor's sandbox
    *    /api/sb/...                 sandbox operations (X-Leash-Session header)
    POST /v1/chat/completions        the proxy: agents call it with a Leash pass
    POST /unprotected/v1/chat/...    the "no Leash" half of the demo (fake AI only)

Spaces: "main" holds server-wide records (unattributed attempts, global budget
alerts); "sb_<id>" is a visitor's sandbox. Rows in every table belong to
exactly one space.
"""
import hashlib
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from . import audit, config, db, guards, tokens, upstream
from .ratelimit import client_ip, limiter

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("leash")

# The sandbox id travels in a header, not a cookie: the pages are on a different
# origin (Vercel), where third-party cookies are unreliable. A custom header also
# can't be sent cross-site without a CORS preflight, which only CORS_ORIGINS pass.
SESSION_HEADER = "x-leash-session"
_SANDBOX_ID = re.compile(r"^[0-9a-f]{24}$")

# The agent run asks for a one-call "status report" pass after the main run.
# It doesn't count as a new run, so it is held to a single call instead.
REPORT_AGENT = "task-agent-report"


def _now_ms() -> int:
    return int(time.time() * 1000)


def is_sandbox(space: str) -> bool:
    return space.startswith("sb_")


def ensure_space(conn, space: str) -> None:
    """Create a space with its canary keys and the first link of its chain. Hold db.write_lock."""
    if not conn.execute(select(db.spaces.c.space).where(db.spaces.c.space == space)).first():
        conn.execute(db.spaces.insert().values(space=space, created_ms=_now_ms()))
    guards.plant_canaries(conn, space)
    if audit.count(conn, space) == 0:
        audit.append(conn, space, "LOG_STARTED", {"note": "Audit chain created."})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    with db.write_lock, db.engine.begin() as conn:
        ensure_space(conn, db.MAIN)
    log.info("Leash ready. Real AI enabled: %s. Public sandbox: %s", config.OPENAI_ENABLED, config.SANDBOX_ENABLED)
    yield


app = FastAPI(title="Leash", version="1.1.0", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

# API responses are JSON only; the pages' own CSP is set by the Vercel site.
CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = CSP
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if request.headers.get("x-forwarded-proto", request.url.scheme) == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# Added last so it wraps everything, including the security-headers middleware.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Leash-Session"],
    # The demo pages read these from proxy responses to draw the budget meters.
    expose_headers=["X-Leash-Calls-Remaining", "X-Leash-Budget-Remaining", "X-Leash-Call-Cost"],
    max_age=600,
)


def _alert(conn, space: str, kind: str, message: str, jti=None) -> None:
    conn.execute(db.alerts.insert().values(space=space, ts_ms=_now_ms(), kind=kind, message=message, jti=jti))


def _alert_once(conn, space: str, kind: str, message: str, jti: str) -> None:
    """Alert the first time a token hits this condition, not on every retry."""
    al = db.alerts.c
    seen = conn.execute(select(al.id).where(al.space == space, al.kind == kind, al.jti == jti).limit(1)).first()
    if not seen:
        _alert(conn, space, kind, message, jti=jti)


def _usd(amount: float) -> str:
    """Money for messages: cents normally, more digits for fractions of a cent."""
    return f"${amount:.2f}" if amount >= 0.01 or amount == 0 else f"${amount:.5f}"


# Real-AI spending through the server's own key, kept as running totals in the
# settings table. Unlike the passes, these survive a demo reset, so the caps
# below can't be reopened by deleting passes.
GLOBAL = "global"


def _spent(conn, space: str, name: str = "real_spent_usd") -> float:
    try:
        return float(db.get_setting(conn, space, name, "0") or 0)
    except ValueError:
        return 0.0


def _bump(conn, space: str, delta: float, name: str = "real_spent_usd") -> None:
    db.set_setting(conn, space, name, f"{max(0.0, _spent(conn, space, name) + delta):.8f}")


def _real_demo(conn, space: str) -> dict:
    """What's left of the real-AI budgets, as seen from a sandbox."""
    if not is_sandbox(space):
        return {"available": False}
    global_left = max(0.0, config.GLOBAL_REAL_BUDGET_USD - _spent(conn, GLOBAL))
    public_left = max(0.0, config.PUBLIC_REAL_BUDGET_USD - _spent(conn, GLOBAL, "public_real_spent_usd"))
    visitor_left = max(0.0, config.SANDBOX_REAL_PER_VISITOR_USD - _spent(conn, space))
    left = min(global_left, public_left, visitor_left)
    return {
        "available": config.OPENAI_ENABLED and left >= 0.0005,
        "model": config.OPENAI_MODEL or None,
        "left_usd": round(left, 6),
        "public_left_usd": round(min(public_left, global_left), 6),
        "public_budget_usd": config.PUBLIC_REAL_BUDGET_USD,
        "visitor_budget_usd": config.SANDBOX_REAL_PER_VISITOR_USD,
    }


def _blocked(status: int, code: str, message: str) -> JSONResponse:
    """OpenAI-shaped error, so existing agent SDKs surface it cleanly."""
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": "leash_blocked", "code": code}})


def _too_many(message: str = "Too many requests. Slow down and try again in a minute.") -> JSONResponse:
    return _blocked(429, "RATE_LIMITED", message)


@app.get("/api/health", include_in_schema=False)
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# The proxy: an OpenAI-compatible endpoint. Point any OpenAI SDK's base_url
# at <your-app>/v1 and pass a Leash token as the API key.
# ---------------------------------------------------------------------------
@app.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request):
    ip = client_ip(request)
    if not limiter.allow(f"proxy:{ip}", 300, 60) or not limiter.allow("proxy:all", 3000, 60):
        return _too_many()
    auth = request.headers.get("authorization", "")
    presented = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = None
    # Database work and the upstream call are blocking, so run them off the event loop.
    return await run_in_threadpool(_handle_proxy, presented, body, ip)


def _log_unattributed(conn, ip: str, event: str, detail: dict) -> None:
    """Failed attempts we can't tie to a space go to the server-wide log, rate-limited
    so the public can't flood it."""
    if limiter.allow(f"unattributed-log:{ip}", 20, 60):
        audit.append(conn, db.MAIN, event, detail)


def _handle_proxy(presented: str, body, ip: str):
    if not presented:
        return _blocked(401, "NO_TOKEN", "Send a Leash token as 'Authorization: Bearer lsh_...'.")
    fingerprint = hashlib.sha256(presented.encode()).hexdigest()

    # 1. Canary keys: decoys that only an injected or rogue agent would find.
    with db.write_lock, db.engine.begin() as conn:
        hit = guards.find_canary(conn, presented)
        if hit:
            space, place = hit
            audit.append(conn, space, "CANARY_TRIGGERED", {"planted_in": place, "key_sha256": fingerprint})
            _alert(
                conn,
                space,
                "CANARY_TRIGGERED",
                f"A trap key was used. It was planted in: {place}. Something read a place it should never have touched.",
            )
            return _blocked(403, "CANARY_TRIGGERED", "This key is a trap. The attempt has been recorded and reported.")

        # 2. Raw provider keys are never accepted. Agents must hold Leash tokens.
        if presented.startswith("sk-"):
            _log_unattributed(conn, ip, "RAW_KEY_REJECTED", {"key_sha256": fingerprint})
            return _blocked(401, "RAW_KEY_REJECTED", "Raw provider keys are not accepted here. Ask your operator for a Leash token.")

        # 3. Signature and expiry.
        try:
            claims = tokens.verify(presented)
        except tokens.TokenError as err:
            owner = err.claims["space"] if err.claims else None
            if owner and conn.execute(select(db.spaces.c.space).where(db.spaces.c.space == owner)).first():
                audit.append(
                    conn,
                    owner,
                    "CALL_BLOCKED",
                    {"agent": err.claims.get("agent"), "code": err.code, "reason": err.message},
                    jti=err.claims["jti"],
                )
            else:
                _log_unattributed(conn, ip, "CALL_BLOCKED", {"code": err.code, "reason": err.message, "token_sha256": fingerprint})
            return _blocked(401, err.code, err.message)

    jti, space, upstream_name = claims["jti"], claims["space"], claims["upstream"]
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return _blocked(400, "BAD_REQUEST", "Request body must be JSON with a 'messages' list.")
    prompt_text = guards.flatten_messages(body)
    prompt_sha = hashlib.sha256(prompt_text.encode()).hexdigest()

    # 4. Reserve the call under the lock: every limit is checked atomically.
    with db.write_lock, db.engine.begin() as conn:
        row = conn.execute(select(db.tokens).where(db.tokens.c.jti == jti)).first()
        if row is None or row.space != space:
            return _blocked(401, "UNKNOWN_TOKEN", "This token is signed correctly but is not on record (it may have been reset).")
        if is_sandbox(space) and audit.count(conn, space) >= config.SANDBOX_MAX_AUDIT:
            return _blocked(429, "SANDBOX_FULL", "This sandbox's log is full. Click 'Reset demo' to start fresh.")

        def block(status, code, message, extra=None):
            detail = {"agent": claims["agent"], "code": code, "reason": message}
            detail.update(extra or {})
            audit.append(conn, space, "CALL_BLOCKED", detail, jti=jti)
            return _blocked(status, code, message)

        if db.get_setting(conn, space, "kill_all") == "on":
            return block(403, "KILL_SWITCH", "The global kill switch is on. All agents are stopped.")
        if row.revoked:
            return block(403, "TOKEN_REVOKED", f"This token was revoked: {row.revoked_reason or 'by an operator'}.")

        leaks = guards.scan_for_secrets(conn, prompt_text)
        if leaks:
            message = f"Blocked an outgoing message containing {' and '.join(leaks)}. Possible attempt to leak a key."
            _alert(conn, space, "SECRET_BLOCKED", f"Agent '{claims['agent']}': {message}", jti=jti)
            return block(400, "SECRET_BLOCKED", message, {"found": leaks, "prompt_sha256": prompt_sha})

        if upstream_name == "openai":
            if not is_sandbox(space):
                return block(403, "UPSTREAM_DISABLED", "Only the public demo can use the server's own key.")
            if not config.OPENAI_ENABLED:
                return block(501, "UPSTREAM_DISABLED", "Real AI is not configured on this server.")
            if body.get("model") != config.OPENAI_MODEL:
                return block(403, "MODEL_NOT_ALLOWED", f"This token may only use model '{config.OPENAI_MODEL}'.")
            # The public demo spends real money, so every call is kept small.
            if len(prompt_text) > config.SANDBOX_REAL_MAX_PROMPT_CHARS:
                return block(413, "PROMPT_TOO_LONG", "Messages in the public demo are limited in length.")
            try:
                requested = int(body.get("max_tokens") or body.get("max_completion_tokens") or config.SANDBOX_REAL_MAX_TOKENS)
            except (TypeError, ValueError):
                requested = config.SANDBOX_REAL_MAX_TOKENS
            body.pop("max_completion_tokens", None)
            body["max_tokens"] = max(1, min(requested, config.SANDBOX_REAL_MAX_TOKENS))

        kind = "real" if upstream_name == "openai" else "fake"

        if row.calls_used >= row.max_calls:
            _alert_once(
                conn, space, "LIMIT_REACHED",
                f"Agent '{claims['agent']}' used all {row.max_calls} of its calls and was stopped.", jti,
            )
            return block(429, "CALL_LIMIT", f"Call limit reached ({row.max_calls} calls).")

        estimate = upstream.estimate_worst_case(kind, body, prompt_text)
        if row.spent_usd + estimate > row.budget_usd + 1e-9:
            _alert_once(
                conn, space, "LIMIT_REACHED",
                f"Agent '{claims['agent']}' reached its {_usd(row.budget_usd)} money limit and was stopped.", jti,
            )
            return block(
                402,
                "BUDGET_EXCEEDED",
                f"Budget reached: {_usd(row.spent_usd)} of {_usd(row.budget_usd)} spent; the next call could cost up to {_usd(estimate)}, which would go over.",
                {"spent_usd": round(row.spent_usd, 8), "budget_usd": row.budget_usd, "next_call_up_to_usd": round(estimate, 8)},
            )

        # Running totals to reserve this call against, besides the pass itself.
        counters = []
        if upstream_name == "openai":
            if _spent(conn, GLOBAL) + estimate > config.GLOBAL_REAL_BUDGET_USD:
                _alert(conn, db.MAIN, "GLOBAL_BUDGET", "The server-wide real-AI budget is used up. Real calls are paused.")
                return block(402, "GLOBAL_BUDGET", "The server-wide spending ceiling for real API calls has been reached.")
            if _spent(conn, GLOBAL, "public_real_spent_usd") + estimate > config.PUBLIC_REAL_BUDGET_USD:
                return block(402, "PUBLIC_BUDGET", "The public demo's real-AI budget is used up. The demo will use the free fake AI.")
            if _spent(conn, space) + estimate > config.SANDBOX_REAL_PER_VISITOR_USD:
                return block(
                    402,
                    "VISITOR_BUDGET",
                    f"You've used your {_usd(config.SANDBOX_REAL_PER_VISITOR_USD)} of real AI in this demo. Self-host Leash to run agents on your own key.",
                )
            counters = [(GLOBAL, "real_spent_usd"), (GLOBAL, "public_real_spent_usd"), (space, "real_spent_usd")]
            for counter_space, name in counters:
                _bump(conn, counter_space, estimate, name)

        conn.execute(
            db.tokens.update().where(db.tokens.c.jti == jti).values(calls_used=row.calls_used + 1, spent_usd=row.spent_usd + estimate)
        )

    # 5. Forward. The lock is released so a slow upstream can't freeze the dashboard.
    try:
        result = upstream.openai_completion(body) if kind == "real" else upstream.fake_completion(body)
    except upstream.UpstreamError as err:
        with db.write_lock, db.engine.begin() as conn:
            for counter_space, name in counters:
                _bump(conn, counter_space, -estimate, name)
            current = conn.execute(select(db.tokens).where(db.tokens.c.jti == jti)).first()
            if current is not None:
                conn.execute(db.tokens.update().where(db.tokens.c.jti == jti).values(spent_usd=max(0.0, current.spent_usd - estimate)))
                audit.append(conn, space, "UPSTREAM_ERROR", {"agent": claims["agent"], "status": err.status, "reason": err.message}, jti=jti)
        return _blocked(502, "UPSTREAM_ERROR", err.message)

    # 6. Settle the real cost and record the call.
    usage = result.get("usage") or {}
    actual = upstream.cost_of(kind, usage)
    with db.write_lock, db.engine.begin() as conn:
        for counter_space, name in counters:
            _bump(conn, counter_space, actual - estimate, name)
        current = conn.execute(select(db.tokens).where(db.tokens.c.jti == jti)).first()
        if current is None:  # the sandbox was reset mid-call
            return JSONResponse(result)
        spent = max(0.0, current.spent_usd - estimate + actual)
        conn.execute(db.tokens.update().where(db.tokens.c.jti == jti).values(spent_usd=spent))
        audit.append(
            conn,
            space,
            "CALL_ALLOWED",
            {
                "agent": claims["agent"],
                "upstream": upstream_name,
                "model": result.get("model"),
                "prompt_sha256": prompt_sha,
                "prompt_preview": prompt_text[:60],
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "cost_usd": round(actual, 6),
                "calls_used": current.calls_used,
                "spent_usd": round(spent, 6),
            },
            jti=jti,
        )
        remaining_calls = current.max_calls - current.calls_used
        remaining_budget = max(0.0, current.budget_usd - spent)

    response = JSONResponse(result)
    response.headers["X-Leash-Calls-Remaining"] = str(remaining_calls)
    response.headers["X-Leash-Budget-Remaining"] = f"{remaining_budget:.8f}"
    response.headers["X-Leash-Call-Cost"] = f"{actual:.8f}"
    return response


# ---------------------------------------------------------------------------
# The unprotected path, for the "before" half of the demo. It calls the fake
# upstream with no checks and no log, just like a raw long-lived key would.
# ---------------------------------------------------------------------------
@app.post("/unprotected/v1/chat/completions")
async def unprotected(request: Request):
    ip = client_ip(request)
    if not limiter.allow(f"unprotected:{ip}", 400, 60) or not limiter.allow("unprotected:all", 4000, 60):
        return _too_many()
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    return upstream.fake_completion(body if isinstance(body, dict) else {})


# ---------------------------------------------------------------------------
# Sandbox operations
# ---------------------------------------------------------------------------
class IssueRequest(BaseModel):
    agent: str = Field(min_length=1, max_length=64)
    # "fake" (the free fake AI) or "openai" (the server's own key, real AI).
    upstream: str = Field(default="fake", pattern="^(fake|openai)$")
    ttl_minutes: int = Field(default=15, ge=1)
    max_calls: int = Field(default=20, ge=1)
    budget_usd: float = Field(default=1.0, gt=0)


class RevokeRequest(BaseModel):
    reason: str = Field(default="Revoked by operator", max_length=200)


class KillRequest(BaseModel):
    on: bool


def op_config(space: str) -> dict:
    with db.engine.connect() as conn:
        real_demo = _real_demo(conn, space)
    return {
        "real_demo": real_demo,
        "mode": "sandbox",
        "fake_cost_per_call": config.FAKE_COST_PER_CALL,
        "global_real_budget_usd": config.GLOBAL_REAL_BUDGET_USD,
        "limits": {
            "max_ttl_minutes": config.SANDBOX_MAX_TTL_MINUTES,
            "max_calls": config.SANDBOX_MAX_CALLS,
            "max_budget_usd": config.SANDBOX_MAX_BUDGET_USD,
        },
    }


def op_issue(space: str, req: IssueRequest) -> dict:
    if req.upstream == "openai" and not config.OPENAI_ENABLED:
        raise HTTPException(400, "The server's own key isn't available here. Use the free fake AI.")
    ttl = min(req.ttl_minutes, config.SANDBOX_MAX_TTL_MINUTES)
    max_calls = min(req.max_calls, config.SANDBOX_MAX_CALLS)
    if req.upstream == "openai" and req.agent == REPORT_AGENT:
        max_calls = 1
    budget = min(req.budget_usd, config.SANDBOX_MAX_BUDGET_USD)
    with db.write_lock, db.engine.begin() as conn:
        if req.upstream == "openai":
            real = _real_demo(conn, space)
            if not real["available"]:
                raise HTTPException(409, "The real-AI budget for this demo is used up. Use the free fake AI instead.")
            budget = min(budget, real["left_usd"])
        t = db.tokens.c
        existing = conn.execute(select(func.count()).select_from(db.tokens).where(t.space == space)).scalar()
        if existing >= config.SANDBOX_MAX_TOKENS:
            raise HTTPException(429, "This sandbox has issued too many tokens. Click 'Reset demo' to start fresh.")
        token, claims = tokens.mint(space, req.agent, req.upstream, ttl, max_calls, budget)
        conn.execute(
            db.tokens.insert().values(
                jti=claims["jti"],
                space=space,
                agent=claims["agent"],
                upstream=claims["upstream"],
                created_ms=_now_ms(),
                exp=claims["exp"],
                max_calls=max_calls,
                budget_usd=budget,
                calls_used=0,
                spent_usd=0.0,
                revoked=False,
            )
        )
        audit.append(
            conn,
            space,
            "TOKEN_ISSUED",
            {"agent": req.agent, "upstream": req.upstream, "ttl_minutes": ttl, "max_calls": max_calls, "budget_usd": budget},
            jti=claims["jti"],
        )
    # The token itself is returned once and never stored. Only its id is kept.
    return {"token": token, "claims": claims}


def op_list_tokens(space: str) -> list:
    now = time.time()
    t = db.tokens.c
    with db.engine.connect() as conn:
        rows = conn.execute(select(db.tokens).where(t.space == space).order_by(t.created_ms.desc()).limit(50)).all()
    out = []
    for r in rows:
        if r.revoked:
            status = "revoked"
        elif now >= r.exp:
            status = "expired"
        elif r.calls_used >= r.max_calls:
            status = "call limit"
        elif r.spent_usd + 1e-9 >= r.budget_usd:
            status = "budget spent"
        else:
            status = "active"
        out.append(
            {
                "jti": r.jti,
                "agent": r.agent,
                "upstream": r.upstream,
                "upstream_label": "Practice AI (free)" if r.upstream == "fake" else "Server key",
                "status": status,
                "expires_in_s": max(0, int(r.exp - now)),
                "calls_used": r.calls_used,
                "max_calls": r.max_calls,
                "spent_usd": round(r.spent_usd, 6),
                "budget_usd": r.budget_usd,
                "revoked_reason": r.revoked_reason,
                "created_ms": r.created_ms,
            }
        )
    return out


def op_revoke(space: str, jti: str, reason: str) -> dict:
    t = db.tokens.c
    with db.write_lock, db.engine.begin() as conn:
        row = conn.execute(select(db.tokens).where(t.jti == jti, t.space == space)).first()
        if row is None:
            raise HTTPException(404, "No token with that id.")
        if not row.revoked:
            conn.execute(db.tokens.update().where(t.jti == jti).values(revoked=True, revoked_reason=reason))
            audit.append(conn, space, "TOKEN_REVOKED", {"agent": row.agent, "reason": reason}, jti=jti)
    return {"ok": True}


def op_kill(space: str, on: bool) -> dict:
    with db.write_lock, db.engine.begin() as conn:
        db.set_setting(conn, space, "kill_all", "on" if on else "off")
        audit.append(conn, space, "KILL_SWITCH_ON" if on else "KILL_SWITCH_OFF", {"by": "operator"})
        if on:
            _alert(conn, space, "KILL_SWITCH", "Global kill switch turned on. Every agent is stopped.")
    return {"kill_all": on}


def op_audit(space: str, limit: int) -> list:
    limit = max(1, min(limit, 500))
    a = db.audit.c
    with db.engine.connect() as conn:
        rows = conn.execute(select(db.audit).where(a.space == space).order_by(a.id.desc()).limit(limit)).all()
    out = []
    for r in rows:
        try:
            detail = json.loads(r.detail)
        except json.JSONDecodeError:
            detail = {"unreadable": r.detail[:80]}
        out.append({"id": r.id, "ts_ms": r.ts_ms, "jti": r.jti, "event": r.event, "detail": detail, "prev_hash": r.prev_hash, "hash": r.hash})
    return out


def op_verify(space: str) -> dict:
    with db.engine.connect() as conn:
        return audit.verify_chain(conn, space)


def op_alerts(space: str) -> list:
    al = db.alerts.c
    with db.engine.connect() as conn:
        rows = conn.execute(select(db.alerts).where(al.space == space).order_by(al.id.desc()).limit(30)).all()
    return [{"id": r.id, "ts_ms": r.ts_ms, "kind": r.kind, "message": r.message, "jti": r.jti} for r in rows]


def op_canaries(space: str) -> list:
    with db.engine.connect() as conn:
        return guards.list_canaries(conn, space)


def op_stats(space: str) -> dict:
    now = time.time()
    t, a = db.tokens.c, db.audit.c
    with db.engine.connect() as conn:
        active = conn.execute(
            select(func.count())
            .select_from(db.tokens)
            .where(t.space == space, t.revoked.is_(False), t.exp > now, t.calls_used < t.max_calls, t.spent_usd < t.budget_usd - 1e-9)
        ).scalar()
        allowed = conn.execute(select(func.count()).select_from(db.audit).where(a.space == space, a.event == "CALL_ALLOWED")).scalar()
        blocked = conn.execute(
            select(func.count())
            .select_from(db.audit)
            .where(a.space == space, a.event.in_(["CALL_BLOCKED", "CANARY_TRIGGERED", "RAW_KEY_REJECTED"]))
        ).scalar()
        spent = conn.execute(select(func.coalesce(func.sum(t.spent_usd), 0.0)).where(t.space == space)).scalar()
        alerts_count = conn.execute(select(func.count()).select_from(db.alerts).where(db.alerts.c.space == space)).scalar()
        kill_all = db.get_setting(conn, space, "kill_all") == "on"
    return {
        "active_tokens": active,
        "calls_allowed": allowed,
        "calls_blocked": blocked,
        "spent_usd": round(spent or 0.0, 6),
        "alerts": alerts_count,
        "kill_all": kill_all,
    }


def op_tamper(space: str, target: str = "cost", jti: str = "") -> dict:
    """Simulate an attacker with database access quietly editing history.

    target="cost": rewrite the latest allowed call to say it cost nothing
    (hiding a runaway bill). target="attack": rewrite the record of the
    latest blocked attack to say nothing happened (covering it up).
    """
    a = db.audit.c
    with db.write_lock, db.engine.begin() as conn:
        row = None
        if target == "attack":
            row = conn.execute(
                select(db.audit)
                .where(a.space == space, a.event.in_(["CANARY_TRIGGERED", "CALL_BLOCKED"]))
                .order_by((a.event == "CANARY_TRIGGERED").desc(), a.id.desc())
                .limit(1)
            ).first()
        if row is None and jti:
            row = conn.execute(
                select(db.audit).where(a.space == space, a.event == "CALL_ALLOWED", a.jti == jti).order_by(a.id.desc()).limit(1)
            ).first()
        if row is None:
            row = conn.execute(
                select(db.audit).where(a.space == space, a.event == "CALL_ALLOWED").order_by(a.id.desc()).limit(1)
            ).first()
        if row is None:
            row = conn.execute(select(db.audit).where(a.space == space).order_by(a.id.desc()).limit(1)).first()
        detail = json.loads(row.detail)
        before = dict(detail)
        if row.event == "CANARY_TRIGGERED":
            detail["planted_in"] = "nothing suspicious, routine check"
        elif row.event == "CALL_BLOCKED":
            detail["reason"] = "Nothing suspicious happened."
        elif "cost_usd" in detail:
            detail["cost_usd"] = 0.0
            detail["spent_usd"] = 0.0
        else:
            detail["note"] = "edited"
        conn.execute(
            db.audit.update()
            .where(a.space == space, a.id == row.id)
            .values(detail=json.dumps(detail, sort_keys=True, separators=(",", ":")))
        )
    return {"tampered_id": row.id, "event": row.event, "before": before, "after": detail}


def op_receipt(space: str, jti: str) -> dict:
    """Leash's own account of one pass: built from the sealed log, not from the agent."""
    a, t = db.audit.c, db.tokens.c
    with db.engine.connect() as conn:
        token = conn.execute(select(db.tokens).where(t.jti == jti, t.space == space)).first()
        if token is None:
            raise HTTPException(404, "No pass with that id.")
        rows = conn.execute(select(db.audit).where(a.space == space, a.jti == jti).order_by(a.id)).all()
        seal = audit.verify_chain(conn, space)
    allowed = [r for r in rows if r.event == "CALL_ALLOWED"]
    blocked = [r for r in rows if r.event == "CALL_BLOCKED"]
    last_block = json.loads(blocked[-1].detail) if blocked else {}
    logged_cost = sum(float(json.loads(r.detail).get("cost_usd") or 0) for r in allowed)
    return {
        "agent": token.agent,
        "calls_allowed": len(allowed),
        "calls_blocked": len(blocked),
        "spent_usd": round(token.spent_usd, 8),
        "logged_cost_usd": round(logged_cost, 8),
        "budget_usd": token.budget_usd,
        "stopped_by": last_block.get("code"),
        "stop_reason": last_block.get("reason"),
        "first_entry": rows[0].id if rows else None,
        "last_entry": rows[-1].id if rows else None,
        "last_seal": rows[-1].hash if rows else None,
        "seal": seal,
    }


def _wipe_space(conn, space: str) -> None:
    conn.execute(db.audit.delete().where(db.audit.c.space == space))
    conn.execute(db.tokens.delete().where(db.tokens.c.space == space))
    conn.execute(db.alerts.delete().where(db.alerts.c.space == space))
    conn.execute(db.settings.delete().where(db.settings.c.key == f"{space}:kill_all"))


def op_reset(space: str) -> dict:
    with db.write_lock, db.engine.begin() as conn:
        _wipe_space(conn, space)
        audit.append(conn, space, "LOG_STARTED", {"note": "Demo reset. New audit chain created."})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Sandbox API: no password, one isolated space per visitor, rate-limited,
# deleted after a few hours.
# ---------------------------------------------------------------------------
def _sandbox_checks(request: Request) -> None:
    if not config.SANDBOX_ENABLED:
        raise HTTPException(404, "The public sandbox is turned off.")
    if not limiter.allow(f"sandbox-api:{client_ip(request)}", 300, 60):
        raise HTTPException(429, "Too many requests. Slow down and try again in a minute.")


def sandbox_space(request: Request) -> str:
    _sandbox_checks(request)
    sid = request.headers.get(SESSION_HEADER, "")
    if _SANDBOX_ID.fullmatch(sid):
        space = f"sb_{sid}"
        with db.engine.connect() as conn:
            if conn.execute(select(db.spaces.c.space).where(db.spaces.c.space == space)).first():
                return space
    raise HTTPException(401, "Your sandbox session has expired. Reload the page.")


_last_cleanup = 0.0


def _cleanup_sandboxes(conn) -> None:
    """Delete sandboxes older than the time limit. Hold db.write_lock."""
    global _last_cleanup
    if time.time() - _last_cleanup < 60:
        return
    _last_cleanup = time.time()
    cutoff = _now_ms() - config.SANDBOX_TTL_HOURS * 3600 * 1000
    old = conn.execute(
        select(db.spaces.c.space).where(db.spaces.c.space.startswith("sb_", autoescape=True), db.spaces.c.created_ms < cutoff)
    ).scalars().all()
    for space in old:
        _wipe_space(conn, space)
        conn.execute(db.canaries.delete().where(db.canaries.c.space == space))
        conn.execute(db.settings.delete().where(db.settings.c.key.like(f"{space}:%")))
        conn.execute(db.spaces.delete().where(db.spaces.c.space == space))


sandbox_router = APIRouter(prefix="/api/sb")


@sandbox_router.post("/session")
def sandbox_session(request: Request):
    """Reuse the visitor's sandbox if it still exists, otherwise create one.

    The page keeps the returned session id and sends it back in X-Leash-Session."""
    _sandbox_checks(request)
    sid = request.headers.get(SESSION_HEADER, "")
    with db.write_lock, db.engine.begin() as conn:
        _cleanup_sandboxes(conn)
        if _SANDBOX_ID.fullmatch(sid):
            space = f"sb_{sid}"
            if conn.execute(select(db.spaces.c.space).where(db.spaces.c.space == space)).first():
                guards.plant_canaries(conn, space)  # adds any trap keys introduced since it was created
                return {"ok": True, "new": False, "session": sid}
        if not limiter.allow(f"sandbox-new:{client_ip(request)}", 20, 3600):
            raise HTTPException(429, "Too many new sandboxes from your network. Try again later.")
        active = conn.execute(
            select(func.count()).select_from(db.spaces).where(db.spaces.c.space.startswith("sb_", autoescape=True))
        ).scalar()
        if active >= config.SANDBOX_MAX_SPACES:
            # 503 is reserved for the rate-limit kill switch ("demo paused").
            raise HTTPException(423, "The sandbox is busy right now. Please try again in a few minutes.")
        sid = secrets.token_hex(12)
        ensure_space(conn, f"sb_{sid}")
    return {"ok": True, "new": True, "session": sid, "ttl_hours": config.SANDBOX_TTL_HOURS}


@sandbox_router.get("/config")
def _config(space: str = Depends(sandbox_space)):
    return op_config(space)


@sandbox_router.post("/tokens")
def _issue(req: IssueRequest, space: str = Depends(sandbox_space)):
    return op_issue(space, req)


@sandbox_router.get("/tokens")
def _tokens(space: str = Depends(sandbox_space)):
    return op_list_tokens(space)


@sandbox_router.post("/tokens/{jti}/revoke")
def _revoke(jti: str, req: RevokeRequest, space: str = Depends(sandbox_space)):
    return op_revoke(space, jti, req.reason)


@sandbox_router.get("/tokens/{jti}/receipt")
def _receipt(jti: str, space: str = Depends(sandbox_space)):
    return op_receipt(space, jti)


@sandbox_router.post("/kill")
def _kill(req: KillRequest, space: str = Depends(sandbox_space)):
    return op_kill(space, req.on)


@sandbox_router.get("/audit")
def _audit(limit: int = 60, space: str = Depends(sandbox_space)):
    return op_audit(space, limit)


@sandbox_router.get("/audit/verify")
def _verify(space: str = Depends(sandbox_space)):
    return op_verify(space)


@sandbox_router.get("/alerts")
def _alerts(space: str = Depends(sandbox_space)):
    return op_alerts(space)


@sandbox_router.get("/canaries")
def _canaries(space: str = Depends(sandbox_space)):
    return op_canaries(space)


@sandbox_router.get("/stats")
def _stats(space: str = Depends(sandbox_space)):
    return op_stats(space)


@sandbox_router.post("/demo/tamper")
def _tamper(target: str = "cost", jti: str = "", space: str = Depends(sandbox_space)):
    return op_tamper(space, "attack" if target == "attack" else "cost", jti[:32])


@sandbox_router.post("/demo/reset")
def _reset(space: str = Depends(sandbox_space)):
    return op_reset(space)


app.include_router(sandbox_router)
