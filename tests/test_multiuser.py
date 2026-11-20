"""Sandbox-Test für den Multi-User-Betrieb: Registrierung, eigene Guardrail
(Regex-Regeln mask/redact/block), zentraler Upstream-Key, Log-Trennung pro User.
Läuft ohne NER-Modelle (NER_ENGINE=none) und mit gemocktem Upstream."""

import asyncio
import json
import os
import sys

os.environ.update(
    {
        "DB_PATH": "/tmp/test_anonymizer_multi.db",
        "MULTI_USER": "true",
        "NER_ENGINE": "none",
        "UPSTREAM_BASE_URL": "http://upstream.test/v1",
        "UPSTREAM_API_KEY": "sk-central-secret",
        "ADMIN_TOKEN": "admin-test",
        "REGISTRATION_CODE": "KURS26",
        "RATE_LIMIT_PER_MINUTE": "100",
    }
)
for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(var, None)
if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

import app.main as main_mod
from app import store
from app.guardrail import GuardrailConfigError, validate_config


def test_validate_config():
    ok = validate_config(
        {
            "entities": ["EMAIL_ADDRESS"],
            "rules": [{"name": "kundennummer", "pattern": r"KD-\d{6}", "action": "mask"}],
        }
    )
    assert ok["rules"][0]["name"] == "KUNDENNUMMER"
    for bad, why in [
        ({"rules": [{"name": "X", "pattern": "a"}]}, "zu kurzer Name"),
        ({"rules": [{"name": "EMAIL_ADDRESS", "pattern": "a"}]}, "eingebauter Typ"),
        ({"rules": [{"name": "LEER", "pattern": "a*"}]}, "matcht leer"),
        ({"rules": [{"name": "KAPUTT", "pattern": "(a"}]}, "ungültige Regex"),
        ({"rules": [{"name": "AKTION", "pattern": "a", "action": "delete"}]}, "Aktion"),
        ({"entities": ["FOO"]}, "unbekannter Typ"),
        ({"score_threshold": 2}, "Schwelle"),
    ]:
        try:
            validate_config(bad)
        except GuardrailConfigError:
            continue
        raise AssertionError(f"hätte abgelehnt werden müssen: {why}")
    print("PASS test_validate_config")


async def test_multiuser_flow():
    store.init()
    captured = []

    async def upstream(request: httpx.Request):
        if request.url.path.endswith("/models"):
            captured.append({"auth": request.headers.get("authorization"), "models": True})
            return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})
        body = json.loads(request.content)
        captured.append({"auth": request.headers.get("authorization"), "body": body})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "Ok [KUNDENNUMMER_1]."}}]},
        )

    main_mod.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # Registrierung
        r = await c.post("/api/register", json={"name": "Anna", "code": "falsch"})
        assert r.status_code == 403
        r = await c.post("/api/register", json={"name": "Anna", "code": "KURS26"})
        assert r.status_code == 200, r.text
        key_a = r.json()["key"]
        assert key_a.startswith("ano_")
        r = await c.post("/api/register", json={"name": "anna", "code": "KURS26"})
        assert r.status_code == 409, "Name doppelt (case-insensitive)"
        r = await c.post(
            "/api/admin/users", json={"name": "Ben"}, headers={"x-admin-token": "admin-test"}
        )
        key_b = r.json()["key"]
        ha = {"Authorization": f"Bearer {key_a}"}
        hb = {"Authorization": f"Bearer {key_b}"}

        # Ohne / mit falschem Key
        r = await c.post("/v1/chat/completions", json={"model": "m1", "messages": []})
        assert r.status_code == 401
        r = await c.get("/v1/models", headers={"Authorization": "Bearer ano_falsch"})
        assert r.status_code == 401

        # Anna: eigene Guardrail
        cfg = {
            "entities": ["EMAIL_ADDRESS"],
            "rules": [
                {"name": "KUNDENNUMMER", "pattern": r"KD-\d{6}", "action": "mask"},
                {"name": "PROJEKT", "pattern": r"projekt\s+falke", "ignore_case": True,
                 "action": "redact", "replacement": "[GEHEIM]"},
                {"name": "PASSWORT", "pattern": r"passwort\s*[:=]", "ignore_case": True,
                 "action": "block"},
            ],
            "block_message": "Keine Passwörter an das LLM!",
        }
        r = await c.put("/api/me/guardrail", json=cfg, headers=ha)
        assert r.status_code == 200, r.text
        r = await c.put("/api/me/guardrail", json={"rules": [{"name": "X", "pattern": "a"}]}, headers=ha)
        assert r.status_code == 400 and "ungültig" in r.json()["detail"]

        # Testfeld
        r = await c.post(
            "/api/me/preview",
            json={"text": "Kunde KD-123456 im Projekt Falke, Mail a@b.ch"},
            headers=ha,
        )
        p = r.json()
        assert not p["blocked"]
        assert p["anonymized"] == "Kunde [KUNDENNUMMER_1] im [GEHEIM], Mail [EMAIL_ADDRESS_1]", p
        r = await c.post("/api/me/preview", json={"text": "Mein Passwort: 1234"}, headers=ha)
        assert r.json()["blocked"] and r.json()["rule"] == "PASSWORT"

        # Chat: mask + De-Anonymisierung, zentraler Key, user-Feld
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "Kunde KD-123456"}]},
            headers=ha,
        )
        assert r.status_code == 200, r.text
        assert r.json()["choices"][0]["message"]["content"] == "Ok KD-123456."
        up = captured[-1]
        assert up["auth"] == "Bearer sk-central-secret", "zentraler Key statt User-Key"
        assert key_a not in json.dumps(up), "User-Key darf nicht zum Upstream"
        assert up["body"]["messages"][0]["content"] == "Kunde [KUNDENNUMMER_1]"
        assert up["body"]["user"] == "anonymizer:Anna"

        # Chat: Block erreicht den Upstream nicht
        n_before = len(captured)
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "passwort = geheim"}]},
            headers=ha,
        )
        assert r.status_code == 400
        assert r.json()["error"]["type"] == "guardrail_blocked"
        assert r.json()["error"]["message"] == "Keine Passwörter an das LLM!"
        assert len(captured) == n_before

        # Ben hat die Standard-Guardrail: Kundennummer bleibt, E-Mail wird maskiert
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "KD-123456 an x@y.ch"}]},
            headers=hb,
        )
        assert r.status_code == 200
        assert captured[-1]["body"]["messages"][0]["content"] == "KD-123456 an [EMAIL_ADDRESS_1]"

        # Modelle über zentralen Key
        r = await c.get("/v1/models", headers=hb)
        assert r.status_code == 200 and captured[-1]["auth"] == "Bearer sk-central-secret"

        # Log-Trennung
        la = (await c.get("/api/me/requests", headers=ha)).json()
        lb = (await c.get("/api/me/requests", headers=hb)).json()
        assert len(la) == 2 and len(lb) == 1, (len(la), len(lb))
        assert {e["status"] for e in la} == {"ok", "blocked"}
        r = await c.get(f"/api/me/requests/{la[0]['id']}", headers=hb)
        assert r.status_code == 404, "Ben darf Annas Request nicht sehen"
        r = await c.get("/api/admin/requests", headers={"x-admin-token": "admin-test"})
        assert len(r.json()) == 3
        r = await c.get("/api/admin/requests", headers={"x-admin-token": "falsch"})
        assert r.status_code == 401

        # Deaktivieren und Key neu erzeugen
        users = (await c.get("/api/admin/users", headers={"x-admin-token": "admin-test"})).json()
        ben = next(u for u in users if u["name"] == "Ben")
        await c.patch(f"/api/admin/users/{ben['id']}", json={"active": False},
                      headers={"x-admin-token": "admin-test"})
        r = await c.get("/api/me", headers=hb)
        assert r.status_code == 403
        r = await c.post(f"/api/admin/users/{ben['id']}/regenerate",
                         headers={"x-admin-token": "admin-test"})
        new_b = r.json()["key"]
        r = await c.get("/api/me", headers=hb)
        assert r.status_code == 401, "alter Key ungültig"
        await c.patch(f"/api/admin/users/{ben['id']}", json={"active": True},
                      headers={"x-admin-token": "admin-test"})
        r = await c.get("/api/me", headers={"Authorization": f"Bearer {new_b}"})
        assert r.status_code == 200 and r.json()["name"] == "Ben"
    print("PASS test_multiuser_flow")


def test_regex_timeout_blocks():
    from app.anonymizer import AnonymizationSession, GuardrailBlocked

    cfg = validate_config({"entities": [], "rules": [{"name": "LANGSAM", "pattern": r"(\w+\s?)+$"}]})
    s = AnonymizationSession(cfg)
    try:
        s.anonymize("a " * 5000 + "!")
    except GuardrailBlocked as b:
        assert b.reason == "timeout"
        print("PASS test_regex_timeout_blocks")
        return
    raise AssertionError("Timeout hätte blockieren müssen")


if __name__ == "__main__":
    test_validate_config()
    test_regex_timeout_blocks()
    asyncio.run(test_multiuser_flow())
    print("\nALLE TESTS BESTANDEN")
