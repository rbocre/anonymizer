"""SQLite-Log aller Requests (Original, anonymisiert, Antwort, Mapping)."""

import json
import os
import sqlite3
import threading
import time
import uuid

DB_PATH = os.getenv("DB_PATH", "/data/anonymizer.db")

_local = threading.local()


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        c = sqlite3.connect(DB_PATH)
        c.execute("PRAGMA journal_mode=WAL")
        c.row_factory = sqlite3.Row
        _local.conn = c
    return _local.conn


def init():
    _conn().execute(
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
    _conn().commit()


def create_entry(model, stream, original_body, anon_body, entities, mapping) -> str:
    rid = uuid.uuid4().hex[:12]
    _conn().execute(
        "INSERT INTO requests (id, ts, model, stream, original_body, anon_body,"
        " entities, mapping, entity_count) VALUES (?,?,?,?,?,?,?,?,?)",
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


def list_entries(limit=100):
    rows = _conn().execute(
        "SELECT id, ts, model, stream, status, duration_ms, entity_count"
        " FROM requests ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_entry(rid):
    row = _conn().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("original_body", "anon_body", "entities", "mapping"):
        if d.get(k):
            d[k] = json.loads(d[k])
    return d


def clear():
    _conn().execute("DELETE FROM requests")
    _conn().commit()
