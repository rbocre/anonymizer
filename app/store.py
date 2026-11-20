"""SQLite-Speicher: Request-Log (Original, anonymisiert, Antwort, Mapping) und,
im Multi-User-Modus, User mit Key-Hash und eigener Guardrail-Konfiguration."""

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid

DB_PATH = os.getenv("DB_PATH", "/data/anonymizer.db")
KEY_PREFIX = "ano_"

_local = threading.local()


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        c = sqlite3.connect(DB_PATH)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.row_factory = sqlite3.Row
        _local.conn = c
    return _local.conn


def init():
    c = _conn()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY,
            ts REAL NOT NULL,
            model TEXT,
            stream INTEGER,
            status TEXT DEFAULT 'pending',
            duration_ms INTEGER,
            entity_count INTEGER DEFAULT 0,
            original_body TEXT,
            anon_body TEXT,
            entities TEXT,
            mapping TEXT,
            response_anon TEXT,
            response_final TEXT,
            error TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created REAL NOT NULL,
            last_used REAL,
            active INTEGER NOT NULL DEFAULT 1,
            key_hash TEXT NOT NULL UNIQUE,
            key_hint TEXT NOT NULL,
            config TEXT
        )
        """
    )
    # Migration bestehender DBs: user_id-Spalte im Request-Log
    cols = {r["name"] for r in c.execute("PRAGMA table_info(requests)")}
    if "user_id" not in cols:
        c.execute("ALTER TABLE requests ADD COLUMN user_id TEXT")
    c.execute("CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user_id, ts)")
    c.commit()


# ---------------------------------------------------------------- users


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(24)


def _user_dict(row) -> dict:
    d = dict(row)
    d.pop("key_hash", None)
    d["active"] = bool(d["active"])
    d["config"] = json.loads(d["config"]) if d.get("config") else None
    return d


def create_user(name: str, config: dict | None = None, active: bool = True) -> tuple[dict, str]:
    """Legt einen User an. Gibt (User, Klartext-Key) zurück; der Key wird nur
    gehasht gespeichert und ist danach nicht mehr abrufbar."""
    key = _new_key()
    uid = uuid.uuid4().hex[:12]
    try:
        _conn().execute(
            "INSERT INTO users (id, name, created, key_hash, key_hint, config, active)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                uid,
                name,
                time.time(),
                _hash_key(key),
                key[:8] + "…" + key[-4:],
                json.dumps(config, ensure_ascii=False) if config else None,
                int(active),
            ),
        )
        _conn().commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"Name «{name}» ist bereits vergeben.")
    return get_user(uid), key


def get_user(uid: str) -> dict | None:
    row = _conn().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return _user_dict(row) if row else None


def get_user_by_key(key: str) -> dict | None:
    if not key or not key.startswith(KEY_PREFIX):
        return None
    row = _conn().execute(
        "SELECT * FROM users WHERE key_hash=?", (_hash_key(key),)
    ).fetchone()
    return _user_dict(row) if row else None


def list_users() -> list[dict]:
    rows = _conn().execute(
        "SELECT u.*, (SELECT COUNT(*) FROM requests r WHERE r.user_id=u.id) AS request_count"
        " FROM users u ORDER BY u.name COLLATE NOCASE"
    ).fetchall()
    return [_user_dict(r) for r in rows]


def update_user_config(uid: str, config: dict):
    _conn().execute(
        "UPDATE users SET config=? WHERE id=?",
        (json.dumps(config, ensure_ascii=False), uid),
    )
    _conn().commit()


def set_user_active(uid: str, active: bool):
    _conn().execute("UPDATE users SET active=? WHERE id=?", (int(active), uid))
    _conn().commit()


def regenerate_key(uid: str) -> str:
    key = _new_key()
    _conn().execute(
        "UPDATE users SET key_hash=?, key_hint=? WHERE id=?",
        (_hash_key(key), key[:8] + "…" + key[-4:], uid),
    )
    _conn().commit()
    return key


def touch_user(uid: str):
    _conn().execute("UPDATE users SET last_used=? WHERE id=?", (time.time(), uid))
    _conn().commit()


def delete_user(uid: str):
    _conn().execute("DELETE FROM requests WHERE user_id=?", (uid,))
    _conn().execute("DELETE FROM users WHERE id=?", (uid,))
    _conn().commit()


# ---------------------------------------------------------------- requests


def create_entry(
    model, stream, original_body, anon_body, entities, mapping, user_id=None, status="pending"
) -> str:
    rid = uuid.uuid4().hex[:12]
    _conn().execute(
        "INSERT INTO requests (id, ts, model, stream, original_body, anon_body,"
        " entities, mapping, entity_count, user_id, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            rid,
            time.time(),
            model,
            int(bool(stream)),
            json.dumps(original_body, ensure_ascii=False),
            json.dumps(anon_body, ensure_ascii=False),
            json.dumps(entities, ensure_ascii=False),
            json.dumps(mapping, ensure_ascii=False),
            len(entities),
            user_id,
            status,
        ),
    )
    _conn().commit()
    return rid


def finish_entry(rid, status, response_anon=None, response_final=None, duration_ms=None, error=None):
    _conn().execute(
        "UPDATE requests SET status=?, response_anon=?, response_final=?,"
        " duration_ms=?, error=? WHERE id=?",
        (status, response_anon, response_final, duration_ms, error, rid),
    )
    _conn().commit()


def list_entries(limit=100, user_id=None):
    sql = (
        "SELECT r.id, r.ts, r.model, r.stream, r.status, r.duration_ms, r.entity_count,"
        " r.user_id, u.name AS user_name FROM requests r LEFT JOIN users u ON u.id = r.user_id"
    )
    args: list = []
    if user_id is not None:
        sql += " WHERE r.user_id=?"
        args.append(user_id)
    sql += " ORDER BY r.ts DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in _conn().execute(sql, args).fetchall()]


def get_entry(rid, user_id=None):
    row = _conn().execute(
        "SELECT r.*, u.name AS user_name FROM requests r"
        " LEFT JOIN users u ON u.id = r.user_id WHERE r.id=?",
        (rid,),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    if user_id is not None and d.get("user_id") != user_id:
        return None
    for k in ("original_body", "anon_body", "entities", "mapping"):
        if d.get(k):
            d[k] = json.loads(d[k])
    return d


def clear(user_id=None):
    if user_id is None:
        _conn().execute("DELETE FROM requests")
    else:
        _conn().execute("DELETE FROM requests WHERE user_id=?", (user_id,))
    _conn().commit()


def purge_older_than(days: float) -> int:
    if days <= 0:
        return 0
    cur = _conn().execute("DELETE FROM requests WHERE ts < ?", (time.time() - days * 86400,))
    _conn().commit()
    return cur.rowcount
