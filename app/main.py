"""Anonymizer – OpenAI-kompatibler Proxy zwischen OpenWebUI und OpenRouter.

OpenWebUI -> Anonymizer (/v1/...) -> OpenRouter
Alle Nachrichteninhalte (inkl. per RAG injizierter Kontexte) werden vor dem
Weiterleiten anonymisiert; Antworten werden de-anonymisiert zurückgegeben.
"""

import copy
import json
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from . import store
from .anonymizer import AnonymizationSession, StreamDeanonymizer, preload

UPSTREAM = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
FALLBACK_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")
TIMEOUT = httpx.Timeout(connect=15, read=600, write=60, pool=15)

app = FastAPI(title="Anonymizer")
STATIC = Path(__file__).parent / "static"

client = httpx.AsyncClient(timeout=TIMEOUT)


@app.on_event("startup")
async def startup():
    store.init()
    # Modelle direkt laden, damit der erste Request nicht hängt
    # (GLiNER: beim allerersten Start wird das Modell von HuggingFace geladen)
    preload()


@app.on_event("shutdown")
async def shutdown():
    await client.aclose()


# ---------------------------------------------------------------- helpers


def _upstream_headers(request: Request) -> dict:
    headers = {"Content-Type": "application/json"}
    auth = request.headers.get("authorization")
    if not auth and FALLBACK_API_KEY:
        auth = f"Bearer {FALLBACK_API_KEY}"
    if auth:
        headers["Authorization"] = auth
    for h in ("http-referer", "x-title"):
        if request.headers.get(h):
            headers[h] = request.headers[h]
    return headers


def _anonymize_body(body: dict) -> tuple[dict, AnonymizationSession]:
    """Anonymisiert alle Message-Inhalte (system/user/assistant, auch RAG-Kontext)."""
    session = AnonymizationSession()
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


def _check_dashboard_auth(request: Request):
    if not DASHBOARD_TOKEN:
        return
    token = request.query_params.get("token") or request.headers.get("x-dashboard-token")
    if token != DASHBOARD_TOKEN:
        raise HTTPException(status_code=401, detail="Ungültiger Dashboard-Token")


# ---------------------------------------------------------------- proxy


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    anon_body, session = _anonymize_body(body)
    rid = store.create_entry(
        model=body.get("model"),
        stream=body.get("stream", False),
        original_body=body,
        anon_body=anon_body,
        entities=session.entities,
        mapping=session.mapping,
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
    resp = await client.get(f"{UPSTREAM}/models", headers=_upstream_headers(request))
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


def _ms(started: float) -> int:
    return int((time.time() - started) * 1000)


# ---------------------------------------------------------------- dashboard


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/requests")
async def api_requests(request: Request, limit: int = 100):
    _check_dashboard_auth(request)
    return store.list_entries(limit)


@app.get("/api/requests/{rid}")
async def api_request_detail(rid: str, request: Request):
    _check_dashboard_auth(request)
    entry = store.get_entry(rid)
    if not entry:
        raise HTTPException(status_code=404)
    return entry


@app.delete("/api/requests")
async def api_clear(request: Request):
    _check_dashboard_auth(request)
    store.clear()
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}
