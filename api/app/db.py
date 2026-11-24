"""Postgres access: connection pool, schema, and per-user data helpers.

Every helper that touches user data takes `user_id` as its first argument and
filters on it in SQL. Tools never receive user_id from the model; it is injected
from the request by the graph, which is how users are kept apart.
"""
import json
import os
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        url = os.environ["DATABASE_URL"]
        _pool = ConnectionPool(
            conninfo=url,
            min_size=1,
            max_size=int(os.getenv("DB_POOL_SIZE", "8")),
            kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
            check=ConnectionPool.check_connection,
            open=True,
        )
    return _pool


@contextmanager
def conn():
    with pool().connection() as c:
        yield c


SCHEMA = """
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id     TEXT PRIMARY KEY,
    profile     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS campaigns (
    id           BIGSERIAL PRIMARY KEY,
    user_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    platform     TEXT NOT NULL,
    objective    TEXT,
    daily_budget NUMERIC(12,2) NOT NULL DEFAULT 0,
    variants     JSONB NOT NULL DEFAULT '[]'::jsonb,
    status       TEXT NOT NULL DEFAULT 'draft',
    ab_result    JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS campaigns_user_idx ON campaigns(user_id);
CREATE TABLE IF NOT EXISTS chat_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    thread_id   TEXT NOT NULL,
    message     TEXT NOT NULL,
    reply       TEXT NOT NULL,
    tool_calls  JSONB NOT NULL DEFAULT '[]'::jsonb,
    tokens_in   INT NOT NULL DEFAULT 0,
    tokens_out  INT NOT NULL DEFAULT 0,
    latency_ms  INT NOT NULL DEFAULT 0,
    blocked     BOOLEAN NOT NULL DEFAULT false,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_log_user_idx ON chat_log(user_id, thread_id);
"""


def init_schema():
    with conn() as c:
        c.execute(SCHEMA)


# ---------- long-term memory (per user) ----------

def get_profile(user_id: str) -> dict:
    with conn() as c:
        row = c.execute("SELECT profile FROM user_profiles WHERE user_id=%s", (user_id,)).fetchone()
    return row["profile"] if row else {}


def merge_profile(user_id: str, updates: dict) -> dict:
    clean = {k: v for k, v in updates.items() if v not in (None, "", [], {})}
    with conn() as c:
        row = c.execute(
            """INSERT INTO user_profiles(user_id, profile) VALUES (%s, %s::jsonb)
               ON CONFLICT (user_id) DO UPDATE
               SET profile = user_profiles.profile || EXCLUDED.profile, updated_at = now()
               RETURNING profile""",
            (user_id, json.dumps(clean)),
        ).fetchone()
    return row["profile"]


def forget_profile_keys(user_id: str, keys: list[str]) -> dict:
    with conn() as c:
        row = c.execute(
            "UPDATE user_profiles SET profile = profile - %s::text[], updated_at=now() WHERE user_id=%s RETURNING profile",
            (keys, user_id),
        ).fetchone()
    return row["profile"] if row else {}


# ---------- campaigns (per user) ----------

def _campaign_out(r: dict) -> dict:
    r = dict(r)
    r["daily_budget"] = float(r["daily_budget"])
    for k in ("created_at", "updated_at"):
        if r.get(k):
            r[k] = r[k].isoformat()
    return r


def create_campaign(user_id, name, platform, objective, daily_budget, variants) -> dict:
    with conn() as c:
        r = c.execute(
            """INSERT INTO campaigns(user_id, name, platform, objective, daily_budget, variants)
               VALUES (%s,%s,%s,%s,%s,%s::jsonb) RETURNING *""",
            (user_id, name, platform, objective, daily_budget, json.dumps(variants)),
        ).fetchone()
    return _campaign_out(r)


def list_campaigns(user_id: str) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM campaigns WHERE user_id=%s ORDER BY id DESC LIMIT 50", (user_id,)
        ).fetchall()
    return [_campaign_out(r) for r in rows]


def get_campaign(user_id: str, campaign_id: int) -> dict | None:
    with conn() as c:
        r = c.execute(
            "SELECT * FROM campaigns WHERE user_id=%s AND id=%s", (user_id, campaign_id)
        ).fetchone()
    return _campaign_out(r) if r else None


def update_campaign(user_id: str, campaign_id: int, **fields) -> dict | None:
    sets, vals = [], []
    for k, v in fields.items():
        if k in ("variants", "ab_result"):
            sets.append(f"{k}=%s::jsonb")
            vals.append(json.dumps(v))
        else:
            sets.append(f"{k}=%s")
            vals.append(v)
    sets.append("updated_at=now()")
    with conn() as c:
        r = c.execute(
            f"UPDATE campaigns SET {', '.join(sets)} WHERE user_id=%s AND id=%s RETURNING *",
            (*vals, user_id, campaign_id),
        ).fetchone()
    return _campaign_out(r) if r else None


def delete_campaign(user_id: str, campaign_id: int) -> bool:
    with conn() as c:
        cur = c.execute("DELETE FROM campaigns WHERE user_id=%s AND id=%s", (user_id, campaign_id))
    return cur.rowcount > 0


# ---------- logging / admin ----------

def log_chat(**row):
    row.setdefault("error", None)
    row["tool_calls"] = json.dumps(row.get("tool_calls", []), default=str)
    with conn() as c:
        c.execute(
            """INSERT INTO chat_log(user_id, thread_id, message, reply, tool_calls, tokens_in, tokens_out,
                                    latency_ms, blocked, error)
               VALUES (%(user_id)s,%(thread_id)s,%(message)s,%(reply)s,%(tool_calls)s::jsonb,%(tokens_in)s,
                       %(tokens_out)s,%(latency_ms)s,%(blocked)s,%(error)s)""",
            row,
        )


def user_threads(user_id: str) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """SELECT thread_id, min(created_at) AS started, max(created_at) AS last,
                      count(*) AS turns, (array_agg(message ORDER BY id))[1] AS first_message
               FROM chat_log WHERE user_id=%s GROUP BY thread_id ORDER BY max(created_at) DESC LIMIT 30""",
            (user_id,),
        ).fetchall()
    return [{**r, "started": r["started"].isoformat(), "last": r["last"].isoformat()} for r in rows]


def thread_history(user_id: str, thread_id: str) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT message, reply, tool_calls, created_at FROM chat_log WHERE user_id=%s AND thread_id=%s ORDER BY id",
            (user_id, thread_id),
        ).fetchall()
    return [{**r, "created_at": r["created_at"].isoformat()} for r in rows]


def admin_overview() -> dict:
    with conn() as c:
        k = c.execute(
            """SELECT count(DISTINCT user_id) AS users, count(DISTINCT user_id || '/' || thread_id) AS threads,
                      count(*) AS messages, coalesce(sum(tokens_in),0) AS tokens_in,
                      coalesce(sum(tokens_out),0) AS tokens_out,
                      coalesce(avg(latency_ms) FILTER (WHERE NOT blocked),0)::int AS avg_latency_ms,
                      count(*) FILTER (WHERE blocked) AS blocked,
                      count(*) FILTER (WHERE error IS NOT NULL) AS errors
               FROM chat_log"""
        ).fetchone()
        camp = c.execute(
            "SELECT count(*) AS total, count(*) FILTER (WHERE status='active') AS active FROM campaigns"
        ).fetchone()
        tools = c.execute(
            """SELECT t->>'name' AS name, count(*) AS n FROM chat_log, jsonb_array_elements(tool_calls) t
               GROUP BY 1 ORDER BY 2 DESC"""
        ).fetchall()
        daily = c.execute(
            """SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS day, count(*) AS messages,
                      sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out
               FROM chat_log WHERE created_at > now() - interval '14 days' GROUP BY 1 ORDER BY 1"""
        ).fetchall()
        users = c.execute(
            """SELECT l.user_id, count(*) AS messages, count(DISTINCT thread_id) AS threads,
                      sum(tokens_in + tokens_out) AS tokens, max(l.created_at) AS last_seen,
                      (SELECT count(*) FROM campaigns c WHERE c.user_id = l.user_id) AS campaigns
               FROM chat_log l GROUP BY l.user_id ORDER BY max(l.created_at) DESC LIMIT 50"""
        ).fetchall()
        recent = c.execute(
            """SELECT user_id, thread_id, message, reply, tool_calls, tokens_in, tokens_out, latency_ms,
                      blocked, error, created_at FROM chat_log ORDER BY id DESC LIMIT 40"""
        ).fetchall()
        camps = c.execute("SELECT * FROM campaigns ORDER BY id DESC LIMIT 50").fetchall()
    # Haiku 5.5 list price: $0.10 / MTok in, $0.50 / MTok out
    k = dict(k)
    k["est_cost_usd"] = round(k["tokens_in"] * 0.10 / 1e6 + k["tokens_out"] * 0.50 / 1e6, 4)
    return {
        "kpis": {**k, "campaigns": camp["total"], "active_campaigns": camp["active"]},
        "tools": tools,
        "daily": daily,
        "users": [{**u, "last_seen": u["last_seen"].isoformat()} for u in users],
        "recent": [{**r, "created_at": r["created_at"].isoformat()} for r in recent],
        "campaigns": [_campaign_out(r) for r in camps],
    }
