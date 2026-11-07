"""Database tables (SQLite locally, Neon Postgres in production).

Every row belongs to a "space". The operator's control room is the space
"main"; each public sandbox visitor gets their own space, with its own
tokens, alerts, kill switch, canary keys and hash chain. Nothing a visitor
does can touch another space.

Tables are created at startup with CREATE TABLE IF NOT EXISTS (metadata.create_all
checks first), so an empty database needs no manual migration.
"""
import threading

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
)

from . import config

if config.DATABASE_URL.startswith("sqlite"):
    engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False}, pool_pre_ping=True)
else:
    # Small pool: one Cloud Run instance, and Neon's free tier has a low connection
    # limit. Neon closes idle connections, so recycle them and ping before use.
    engine = create_engine(
        config.DATABASE_URL, pool_size=3, max_overflow=2, pool_pre_ping=True, pool_recycle=300
    )
metadata = MetaData()

MAIN = "main"

# One lock serialises every state change. The app runs as a single process
# (one uvicorn worker, Cloud Run --max-instances=1), so this keeps budgets and
# hash chains consistent.
write_lock = threading.Lock()

spaces = Table(
    "leash_spaces",
    metadata,
    Column("space", String(40), primary_key=True),
    Column("created_ms", BigInteger, nullable=False),
)

tokens = Table(
    "leash_tokens",
    metadata,
    Column("jti", String(32), primary_key=True),
    Column("space", String(40), nullable=False, index=True),
    Column("agent", String(64), nullable=False),
    Column("upstream", String(16), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("exp", BigInteger, nullable=False),  # unix seconds
    Column("max_calls", Integer, nullable=False),
    Column("budget_usd", Float, nullable=False),
    Column("calls_used", Integer, nullable=False, default=0),
    Column("spent_usd", Float, nullable=False, default=0.0),
    Column("revoked", Boolean, nullable=False, default=False),
    Column("revoked_reason", String(200), nullable=True),
)

audit = Table(
    "leash_audit",
    metadata,
    Column("space", String(40), primary_key=True),
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("ts_ms", BigInteger, nullable=False),
    Column("jti", String(32), nullable=True),
    Column("event", String(40), nullable=False),
    Column("detail", Text, nullable=False),  # canonical JSON
    Column("prev_hash", String(64), nullable=False),
    Column("hash", String(64), nullable=False),
)

alerts = Table(
    "leash_alerts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("space", String(40), nullable=False, index=True),
    Column("ts_ms", BigInteger, nullable=False),
    Column("kind", String(40), nullable=False),
    Column("message", Text, nullable=False),
    Column("jti", String(32), nullable=True),
)

canaries = Table(
    "leash_canaries",
    metadata,
    Column("key", String(80), primary_key=True),
    Column("space", String(40), nullable=False, index=True),
    Column("planted_in", String(120), nullable=False),
)

settings = Table(
    "leash_settings",
    metadata,
    Column("key", String(80), primary_key=True),
    Column("value", String(200), nullable=False),
)


def init_db() -> None:
    metadata.create_all(engine)


def get_setting(conn, space: str, key: str, default: str = "") -> str:
    full = f"{space}:{key}"
    row = conn.execute(settings.select().where(settings.c.key == full)).first()
    return row.value if row else default


def set_setting(conn, space: str, key: str, value: str) -> None:
    full = f"{space}:{key}"
    if conn.execute(settings.select().where(settings.c.key == full)).first():
        conn.execute(settings.update().where(settings.c.key == full).values(value=value))
    else:
        conn.execute(settings.insert().values(key=full, value=value))
