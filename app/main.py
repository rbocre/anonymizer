"""Anonymizer – OpenAI-kompatibler Proxy mit PII-Anonymisierung.

Einzel-User-Betrieb (MULTI_USER=false, wie bisher):
    Client (z. B. OpenWebUI) -> Anonymizer (/v1/...) -> Upstream (OpenRouter)
    Der API-Key des Clients wird an den Upstream durchgereicht.

Multi-User-Betrieb (MULTI_USER=true, z. B. für den Unterricht):
    Jan / OpenWebUI mit eigenem ano_…-Key -> Anonymizer -> Upstream (z. B. LiteLLM)
    Jeder User definiert im Web-UI seine eigene Guardrail und sieht nur seine
    eigenen Requests. Zum Upstream geht ein zentraler Key (UPSTREAM_API_KEY),
    der den Server nie verlässt.
"""

import asyncio
import copy
import json
import os
import time
from collections import defaultdict, deque
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import store
from .anonymizer import (
    NER_ENGINE,
    AnonymizationSession,
    GuardrailBlocked,
    StreamDeanonymizer,
    ner_available,
    preload,
)
from .guardrail import (
    BUILTIN_ENTITIES,
    MAX_RULES,
    GuardrailConfigError,
    default_config,
    validate_config,
)


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


UPSTREAM = (
    os.getenv("UPSTREAM_BASE_URL") or os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
).rstrip("/")
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY") or os.getenv("OPENROUTER_API_KEY", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN") or os.getenv("DASHBOARD_TOKEN", "")
MULTI_USER = _env_bool("MULTI_USER", False)
REGISTRATION_CODE = os.getenv("REGISTRATION_CODE", "")
FORWARD_USER = _env_bool("FORWARD_USER", True)
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
LOG_RETENTION_DAYS = float(os.getenv("LOG_RETENTION_DAYS", "0"))
ANON_CONCURRENCY = int(os.getenv("ANON_CONCURRENCY", "2"))
ALLOWED_MODELS = [m.strip() for m in os.getenv("ALLOWED_MODELS", "").split(",") if m.strip()]
TIMEOUT = httpx.Timeout(connect=15, read=600, write=60, pool=15)

app = FastAPI(title="Anonymizer")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

client = httpx.AsyncClient(timeout=TIMEOUT)
_anon_sem = asyncio.Semaphore(max(1, ANON_CONCURRENCY))
_rate: dict[str, deque] = defaultdict(deque)
_last_purge = 0.0


@app.on_event("startup")
async def startup():
    store.init()
    _maybe_purge(force=True)
    # Modelle direkt laden, damit der erste Request nicht hängt
    # (GLiNER: beim allerersten Start wird das Modell von HuggingFace geladen)
    preload()


@app.on_event("shutdown")
async def shutdown():
    await client.aclose()


# ---------------------------------------------------------------- helpers


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def _upstream_headers(request: Request) -> dict:
    headers = {"Content-Type": "application/json"}
    if MULTI_USER:
        # Zentraler Key; der ano_…-Key des Users verlässt den Server nie
        if UPSTREAM_API_KEY:
            headers["Authorization"] = f"Bearer {UPSTREAM_API_KEY}"
        return headers
    auth = request.headers.get("authorization")
    if not auth and UPSTREAM_API_KEY:
        auth = f"Bearer {UPSTREAM_API_KEY}"
    if auth:
        headers["Authorization"] = auth
    for h in ("http-referer", "x-title"):
        if request.headers.get(h):
            headers[h] = request.headers[h]
    return headers


def _anonymize_body(body: dict, config: dict | None = None) -> tuple[dict, AnonymizationSession]:
    """Anonymisiert alle Message-Inhalte (system/user/assistant, auch RAG-Kontext).
    Wirft GuardrailBlocked, wenn eine Block-Regel anschlägt."""
    session = AnonymizationSession(config)
    anon = copy.deepcopy(body)
    for msg in anon.get("messages", []):
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = session.anonymize(content)
        elif isinstance(content, list):  # multimodal
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    part["text"] = session.anonymize(part.get("text", ""))
    return anon, session


async def _anonymize_async(body, config):
    # CPU-lastig (v. a. GLiNER): im Threadpool, begrenzt parallel
    async with _anon_sem:
        return await run_in_threadpool(_anonymize_body, body, config)


def _openai_error(status: int, message: str, err_type: str, code: str | None = None):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "code": code or err_type}},
    )


def _require_user(request: Request) -> dict:
    """Multi-User: ano_…-Key aus dem Authorization-Header prüfen."""
    user = store.get_user_by_key(_bearer(request))
    if not user:
        raise HTTPException(status_code=401, detail="Ungültiger oder fehlender API-Key (ano_…).")
    if not user["active"]:
        raise HTTPException(status_code=403, detail="Dieser Zugang ist deaktiviert.")
    return user


def _check_rate(user_id: str) -> bool:
    if RATE_LIMIT_PER_MINUTE <= 0:
        return True
    now = time.time()
    q = _rate[user_id]
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MINUTE:
        return False
    q.append(now)
    return True


def _check_admin(request: Request):
    if not ADMIN_TOKEN:
        if MULTI_USER:
            raise HTTPException(status_code=503, detail="ADMIN_TOKEN ist nicht gesetzt.")
        return
    token = (
        request.query_params.get("token")
        or request.headers.get("x-admin-token")
        or request.headers.get("x-dashboard-token")
    )
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Ungültiger Admin-Token")


def _maybe_purge(force=False):
    global _last_purge
    if LOG_RETENTION_DAYS <= 0:
        return
    if force or time.time() - _last_purge > 3600:
        _last_purge = time.time()
        store.purge_older_than(LOG_RETENTION_DAYS)


def _user_config(user: dict | None) -> dict | None:
    if user is None:
        return None
    return user.get("config") or default_config()


# ---------------------------------------------------------------- proxy


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    user = _require_user(request) if MULTI_USER else None
    if user and not _check_rate(user["id"]):
        return _openai_error(
            429, f"Zu viele Anfragen (max. {RATE_LIMIT_PER_MINUTE}/Minute).", "rate_limit_exceeded"
        )
    body = await request.json()
    if user and ALLOWED_MODELS and body.get("model") not in ALLOWED_MODELS:
        return _openai_error(
            400,
            f"Modell «{body.get('model')}» ist nicht freigegeben. Erlaubt: {', '.join(ALLOWED_MODELS)}",
            "model_not_allowed",
        )
    _maybe_purge()
    uid = user["id"] if user else None
    if user:
        store.touch_user(uid)

    try:
        anon_body, session = await _anonymize_async(body, _user_config(user))
    except GuardrailBlocked as b:
        store.create_entry(
            model=body.get("model"),
            stream=body.get("stream", False),
            original_body=body,
            anon_body={},
            entities=[],
            mapping={},
            user_id=uid,
            status="blocked",
        )
        return _openai_error(400, b.message, "guardrail_blocked", b.rule)

    if user and FORWARD_USER:
        # Upstream (z. B. LiteLLM) kann so die Nutzung pro Person zuordnen
        anon_body["user"] = f"anonymizer:{user['name']}"

    rid = store.create_entry(
        model=body.get("model"),
        stream=body.get("stream", False),
        original_body=body,
        anon_body=anon_body,
        entities=session.entities,
        mapping=session.mapping,
        user_id=uid,
    )
    headers = _upstream_headers(request)
    url = f"{UPSTREAM}/chat/completions"
    started = time.time()

    if body.get("stream"):
        return StreamingResponse(
            _stream_upstream(url, headers, anon_body, session, rid, started),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        resp = await client.post(url, json=anon_body, headers=headers)
    except httpx.HTTPError as e:
        store.finish_entry(rid, "error", error=str(e), duration_ms=_ms(started))
        raise HTTPException(status_code=502, detail=f"Upstream-Fehler: {e}")

    if resp.status_code >= 400:
        store.finish_entry(
            rid, "error", error=resp.text[:2000], duration_ms=_ms(started)
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    data = resp.json()
    anon_texts, final_texts = [], []
    for choice in data.get("choices", []):
        msg = choice.get("message") or {}
        if isinstance(msg.get("content"), str):
            anon_texts.append(msg["content"])
            msg["content"] = session.deanonymize(msg["content"])
            final_texts.append(msg["content"])
    store.finish_entry(
        rid,
        "ok",
        response_anon="\n---\n".join(anon_texts),
        response_final="\n---\n".join(final_texts),
        duration_ms=_ms(started),
    )
    return JSONResponse(content=data)


async def _stream_upstream(url, headers, anon_body, session, rid, started):
    deanon = StreamDeanonymizer(session)
    anon_full: list[str] = []
    template = None
    status = "ok"
    error = None
    try:
        async with client.stream("POST", url, json=anon_body, headers=headers) as resp:
            if resp.status_code >= 400:
                text = (await resp.aread()).decode("utf-8", "replace")
                status, error = "error", text[:2000]
                yield f"data: {json.dumps({'error': {'message': text, 'code': resp.status_code}})}\n\n"
                return
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    yield line + "\n\n"  # z.B. ": OPENROUTER PROCESSING"
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    rest = deanon.flush()
                    if rest and template:
                        chunk = copy.deepcopy(template)
                        chunk["choices"][0]["delta"] = {"content": rest}
                        chunk["choices"][0]["finish_reason"] = None
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    yield line + "\n\n"
                    continue
                choices = obj.get("choices") or []
                if choices and isinstance(choices[0].get("delta"), dict):
                    template = template or copy.deepcopy(obj)
                    content = choices[0]["delta"].get("content")
                    if content:
                        anon_full.append(content)
                        choices[0]["delta"]["content"] = deanon.feed(content)
                yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"
    except httpx.HTTPError as e:
        status, error = "error", str(e)
        yield f"data: {json.dumps({'error': {'message': str(e)}})}\n\n"
    finally:
        anon_text = "".join(anon_full)
        store.finish_entry(
            rid,
            status,
            response_anon=anon_text,
            response_final=session.deanonymize(anon_text),
            duration_ms=_ms(started),
            error=error,
        )


@app.get("/v1/models")
async def models(request: Request):
    if MULTI_USER:
        _require_user(request)
    resp = await client.get(f"{UPSTREAM}/models", headers=_upstream_headers(request))
    if MULTI_USER and ALLOWED_MODELS and resp.status_code == 200:
        data = resp.json()
        data["data"] = [m for m in data.get("data", []) if m.get("id") in ALLOWED_MODELS]
        return JSONResponse(content=data)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


def _ms(started: float) -> int:
    return int((time.time() - started) * 1000)


# ---------------------------------------------------------------- pages


@app.get("/")
async def index():
    return FileResponse(STATIC / ("app.html" if MULTI_USER else "index.html"))


@app.get("/admin")
async def admin_page():
    return FileResponse(STATIC / ("admin.html" if MULTI_USER else "index.html"))


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- public info


@app.get("/api/info")
async def api_info():
    return {
        "multi_user": MULTI_USER,
        "registration_open": MULTI_USER and bool(REGISTRATION_CODE),
        "ner_engine": NER_ENGINE,
        "ner_available": ner_available(),
        "entities": [
            {**e, "available": (not e["ner"]) or (
                ner_available() and (NER_ENGINE == "gliner" or not e.get("gliner_only"))
            )}
            for e in BUILTIN_ENTITIES
        ],
        "max_rules": MAX_RULES,
        "default_config": default_config(),
        "allowed_models": ALLOWED_MODELS,
        "rate_limit_per_minute": RATE_LIMIT_PER_MINUTE,
        "log_retention_days": LOG_RETENTION_DAYS,
    }


_reg_attempts: dict[str, deque] = defaultdict(deque)


@app.post("/api/register")
async def api_register(request: Request):
    if not MULTI_USER or not REGISTRATION_CODE:
        raise HTTPException(status_code=403, detail="Registrierung ist nicht offen.")
    ip = request.client.host if request.client else "?"
    q = _reg_attempts[ip]
    now = time.time()
    while q and q[0] < now - 3600:
        q.popleft()
    if len(q) >= 10:
        raise HTTPException(status_code=429, detail="Zu viele Versuche, bitte später nochmals.")
    q.append(now)
    data = await request.json()
    if str(data.get("code", "")).strip() != REGISTRATION_CODE:
        raise HTTPException(status_code=403, detail="Kurs-Code ist falsch.")
    name = str(data.get("name", "")).strip()
    if not 2 <= len(name) <= 40:
        raise HTTPException(status_code=400, detail="Name muss 2–40 Zeichen lang sein.")
    try:
        user, key = store.create_user(name, default_config())
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"user": _public_user(user), "key": key}


def _public_user(u: dict) -> dict:
    return {k: u.get(k) for k in ("id", "name", "created", "last_used", "active", "key_hint")}


# ---------------------------------------------------------------- user API


def _multi_only():
    if not MULTI_USER:
        raise HTTPException(status_code=404, detail="Nur im Multi-User-Betrieb verfügbar.")


@app.get("/api/me")
async def api_me(request: Request):
    _multi_only()
    user = _require_user(request)
    return {**_public_user(user), "config": _user_config(user)}


@app.put("/api/me/guardrail")
async def api_me_guardrail(request: Request):
    _multi_only()
    user = _require_user(request)
    try:
        cfg = validate_config(await request.json())
    except GuardrailConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    store.update_user_config(user["id"], cfg)
    return {"ok": True, "config": cfg}


@app.post("/api/me/preview")
async def api_me_preview(request: Request):
    """Testfeld: zeigt, was die (ggf. noch ungespeicherte) Guardrail mit einem
    Text macht, ohne das LLM aufzurufen."""
    _multi_only()
    user = _require_user(request)
    data = await request.json()
    text = str(data.get("text", ""))[:5000]
    cfg = _user_config(user)
    if data.get("config") is not None:
        try:
            cfg = validate_config(data["config"])
        except GuardrailConfigError as e:
            raise HTTPException(status_code=400, detail=str(e))
    try:
        anon, session = await _anonymize_async(
            {"messages": [{"role": "user", "content": text}]}, cfg
        )
    except GuardrailBlocked as b:
        return {"blocked": True, "rule": b.rule, "reason": b.reason, "message": b.message}
    return {
        "blocked": False,
        "anonymized": anon["messages"][0]["content"],
        "entities": session.entities,
    }


@app.get("/api/me/requests")
async def api_me_requests(request: Request, limit: int = 100):
    _multi_only()
    user = _require_user(request)
    return store.list_entries(min(limit, 500), user_id=user["id"])


@app.get("/api/me/requests/{rid}")
async def api_me_request(rid: str, request: Request):
    _multi_only()
    user = _require_user(request)
    entry = store.get_entry(rid, user_id=user["id"])
    if not entry:
        raise HTTPException(status_code=404)
    return entry


@app.delete("/api/me/requests")
async def api_me_clear(request: Request):
    _multi_only()
    user = _require_user(request)
    store.clear(user_id=user["id"])
    return {"ok": True}


# ---------------------------------------------------------------- admin API


@app.get("/api/admin/users")
async def api_admin_users(request: Request):
    _check_admin(request)
    return [
        {**_public_user(u), "request_count": u.get("request_count", 0), "config": u.get("config")}
        for u in store.list_users()
    ]


@app.post("/api/admin/users")
async def api_admin_create_user(request: Request):
    _check_admin(request)
    data = await request.json()
    name = str(data.get("name", "")).strip()
    if not 2 <= len(name) <= 40:
        raise HTTPException(status_code=400, detail="Name muss 2–40 Zeichen lang sein.")
    try:
        user, key = store.create_user(name, default_config())
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"user": _public_user(user), "key": key}


@app.post("/api/admin/users/{uid}/regenerate")
async def api_admin_regenerate(uid: str, request: Request):
    _check_admin(request)
    if not store.get_user(uid):
        raise HTTPException(status_code=404)
    return {"key": store.regenerate_key(uid)}


@app.patch("/api/admin/users/{uid}")
async def api_admin_update_user(uid: str, request: Request):
    _check_admin(request)
    if not store.get_user(uid):
        raise HTTPException(status_code=404)
    data = await request.json()
    if "active" in data:
        store.set_user_active(uid, bool(data["active"]))
    return _public_user(store.get_user(uid))


@app.delete("/api/admin/users/{uid}")
async def api_admin_delete_user(uid: str, request: Request):
    _check_admin(request)
    store.delete_user(uid)
    return {"ok": True}


@app.get("/api/admin/requests")
async def api_admin_requests(request: Request, limit: int = 200, user_id: str | None = None):
    _check_admin(request)
    return store.list_entries(min(limit, 1000), user_id=user_id)


@app.get("/api/admin/requests/{rid}")
async def api_admin_request(rid: str, request: Request):
    _check_admin(request)
    entry = store.get_entry(rid)
    if not entry:
        raise HTTPException(status_code=404)
    return entry


@app.delete("/api/admin/requests")
async def api_admin_clear(request: Request):
    _check_admin(request)
    store.clear()
    return {"ok": True}


# Bisherige Dashboard-Routen (Einzel-User-Dashboard), geschützt wie Admin
@app.get("/api/requests")
async def api_requests(request: Request, limit: int = 100):
    _check_admin(request)
    return store.list_entries(limit)


@app.get("/api/requests/{rid}")
async def api_request_detail(rid: str, request: Request):
    _check_admin(request)
    entry = store.get_entry(rid)
    if not entry:
        raise HTTPException(status_code=404)
    return entry


@app.delete("/api/requests")
async def api_clear(request: Request):
    _check_admin(request)
    store.clear()
    return {"ok": True}
