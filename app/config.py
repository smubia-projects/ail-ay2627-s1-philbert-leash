"""Configuration, all read from environment variables.

Nothing secret is ever hard-coded. Locally, put values in a .env file; on
Cloud Run they come from the DOTENV secret rendered by the deploy workflow.
"""
import hashlib
import hmac
import logging
import os
import secrets

log = logging.getLogger("leash")


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader so local runs work without extra packages."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# --- Master secret -----------------------------------------------------------
# Every other key is derived from this one, so one secret controls everything.
SIGNING_SECRET = os.environ.get("SIGNING_SECRET", "").strip()
if not SIGNING_SECRET and os.environ.get("DATABASE_URL", "").strip():
    # With a persistent database, a random per-process secret would break every
    # saved audit chain on the next cold start. Refuse to start instead.
    raise RuntimeError("SIGNING_SECRET must be set when DATABASE_URL is set (see .env.example).")
if not SIGNING_SECRET:
    SIGNING_SECRET = secrets.token_hex(32)
    log.warning(
        "SIGNING_SECRET is not set. Using a random one for this run only; "
        "all tokens and the audit chain will be invalid after a restart."
    )


def derive_key(purpose: str) -> bytes:
    """Key separation: a different HMAC key per job, all derived from the master.

    If one derived key were ever exposed, the others stay safe.
    """
    return hmac.new(SIGNING_SECRET.encode(), purpose.encode(), hashlib.sha256).digest()


TOKEN_KEY = derive_key("leash/token-signing/v1")
AUDIT_KEY = derive_key("leash/audit-chain/v1")
CANARY_KEY = derive_key("leash/canary/v1")

# --- Upstream (the real AI provider) ----------------------------------------
def _optional(name: str) -> str:
    """Treat blank, 'none' or '-' as not set."""
    value = os.environ.get(name, "").strip()
    return "" if value.lower() in ("", "none", "-") else value


# The real AI goes through OpenRouter's OpenAI-compatible API. The OPENAI_* names
# below are kept because the rest of the app calls this upstream "openai".
OPENAI_BASE_URL = (_optional("API_BASE_URL") or _optional("OPENROUTER_BASE_URL") or "https://api.apiyi.com/v1").rstrip("/")
OPENAI_API_KEY = (_optional("API_KEY") or _optional("OPENROUTER_API_KEY"))
OPENAI_MODEL = (_optional("MODEL_NAME") or _optional("OPENROUTER_MODEL")) or "gpt-5.4-nano"
OPENAI_ENABLED = bool(OPENAI_API_KEY and OPENAI_MODEL)

# Prices in USD per 1M tokens for MODEL_NAME (defaults: gpt-5.4-nano on apiyi). They size
# the worst-case estimate reserved before each call and settle each call from its token
# usage (a provider-reported `usage.cost` is used instead when present).
PRICE_INPUT_PER_1M = _float("PRICE_INPUT_PER_1M", 0.20)
PRICE_OUTPUT_PER_1M = _float("PRICE_OUTPUT_PER_1M", 1.25)
# Cap on output tokens per real call if the agent doesn't set one.
DEFAULT_MAX_COMPLETION_TOKENS = int(_float("DEFAULT_MAX_COMPLETION_TOKENS", 300))

# The fake upstream pretends every call costs this much.
FAKE_COST_PER_CALL = _float("FAKE_COST_PER_CALL", 0.05)

# --- Public sandbox -------------------------------------------------------------
# Lets anyone run the demo at /try in an isolated space, fake upstream only.
SANDBOX_ENABLED = os.environ.get("SANDBOX_ENABLED", "true").strip().lower() not in ("false", "0", "no", "off")
SANDBOX_TTL_HOURS = 3            # sandboxes are deleted after this long
SANDBOX_MAX_SPACES = 300         # active sandboxes at once
SANDBOX_MAX_AUDIT = 3000         # log entries per sandbox before it asks for a reset
SANDBOX_MAX_TOKENS = 60          # tokens per sandbox
SANDBOX_MAX_TTL_MINUTES = 10
SANDBOX_MAX_CALLS = 100
SANDBOX_MAX_BUDGET_USD = 5.00    # cap per pass (pretend money on the fake AI)
# Real AI in the public demo, paid from the server's own key. How much of it each visitor may use is the shared
# AI Lodge rate limiter's job (bucket "agent_call", one per real call); these keep each call and pass small.
SANDBOX_REAL_MAX_PASS_USD = _float("SANDBOX_REAL_MAX_PASS_USD", 0.01)  # largest budget a real-AI pass can carry
SANDBOX_REAL_MAX_TOKENS = 150        # output cap per public real call
SANDBOX_REAL_MAX_PROMPT_CHARS = 8000 # input cap per public real call

# --- CORS ------------------------------------------------------------------------
# The demo pages are served from Vercel, a different origin. Comma-separated list
# of the exact origins allowed to call this API, e.g. https://<project>.vercel.app
CORS_ORIGINS = [
    o.strip().rstrip("/")
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]

# --- Database -----------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or "sqlite:///./leash.db"
# Neon gives a plain postgresql:// URL (keep ?sslmode=require on it). Name the driver
# we install (psycopg2) explicitly, since newer SQLAlchemy defaults to another one.
for _prefix in ("postgres://", "postgresql://"):
    if DATABASE_URL.startswith(_prefix):
        DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len(_prefix):]
        break
