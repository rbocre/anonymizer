"""Sandbox-Test: volle Logik mit leerem spaCy-Modell (Pattern-Recognizer only)
und gemocktem OpenRouter-Upstream. NER (PERSON/LOCATION) braucht die echten
Modelle und wird im Docker-Container getestet."""

import asyncio
import json
import os
import sys

os.environ["DB_PATH"] = "/tmp/test_anonymizer.db"
for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(var, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spacy
from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerRegistry
from presidio_analyzer.nlp_engine import SpacyNlpEngine
from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    EmailRecognizer,
    IbanRecognizer,
    IpRecognizer,
    PhoneRecognizer,
)

import app.anonymizer as anon_mod


def build_blank_analyzer():
    engine = SpacyNlpEngine.__new__(SpacyNlpEngine)
    SpacyNlpEngine.__init__.__wrapped__(engine) if hasattr(SpacyNlpEngine.__init__, "__wrapped__") else None
    engine = object.__new__(SpacyNlpEngine)
    engine.models = [
        {"lang_code": "de", "model_name": "blank"},
        {"lang_code": "en", "model_name": "blank"},
    ]
    engine.nlp = {"de": spacy.blank("de"), "en": spacy.blank("en")}
    from presidio_analyzer.nlp_engine.ner_model_configuration import NerModelConfiguration

    engine.ner_model_configuration = NerModelConfiguration()

    registry = RecognizerRegistry(supported_languages=["de", "en"])
    ahv = Pattern(name="ahv", regex=r"\b756\.?\d{4}\.?\d{4}\.?\d{2}\b", score=0.9)
    for lang in ("de", "en"):
        registry.add_recognizer(EmailRecognizer(supported_language=lang))
        registry.add_recognizer(IbanRecognizer(supported_language=lang))
        registry.add_recognizer(CreditCardRecognizer(supported_language=lang))
        registry.add_recognizer(IpRecognizer(supported_language=lang))
        registry.add_recognizer(
            PhoneRecognizer(supported_language=lang, supported_regions=("CH", "DE", "US"))
        )
        registry.add_recognizer(
            PatternRecognizer(
                supported_entity="AHV_NUMBER",
                name=f"Ahv_{lang}",
                patterns=[ahv],
                supported_language=lang,
            )
        )
    return AnalyzerEngine(
        nlp_engine=engine,
        registry=registry,
        supported_languages=["de", "en"],
        default_score_threshold=0.4,
    )


def test_anonymization():
    anon_mod._analyzer = build_blank_analyzer()
    s = anon_mod.AnonymizationSession()
    t = (
        "Kontakt: roman.brun@gmail.com, Tel +41 79 123 45 67, "
        "IBAN CH93 0076 2011 6238 5295 7, AHV 756.1234.5678.97."
    )
    a = s.anonymize(t)
    print("ANON:", a)
    assert "roman.brun@gmail.com" not in a
    assert "756.1234.5678.97" not in a
    assert "[EMAIL_ADDRESS_1]" in a
    assert "[AHV_NUMBER_1]" in a
    # Konsistenz: gleiche E-Mail -> gleicher Platzhalter
    a2 = s.anonymize("Schreib an roman.brun@gmail.com bitte.")
    assert "[EMAIL_ADDRESS_1]" in a2
    # De-Anonymisierung
    assert s.deanonymize(a) == t
    print("PASS test_anonymization")


def test_stream_deanonymizer():
    anon_mod._analyzer = build_blank_analyzer()
    s = anon_mod.AnonymizationSession()
    s.anonymize("Mail: roman.brun@gmail.com")
    d = anon_mod.StreamDeanonymizer(s)
    # Platzhalter über Chunk-Grenzen zerrissen
    chunks = ["Deine Mail [EMA", "IL_ADD", "RESS_1] ist notiert. [unrelated] Ende"]
    out = "".join(d.feed(c) for c in chunks) + d.flush()
    assert out == "Deine Mail roman.brun@gmail.com ist notiert. [unrelated] Ende", out
    print("PASS test_stream_deanonymizer")


async def test_proxy():
    import httpx

    anon_mod._analyzer = build_blank_analyzer()
    import app.main as main_mod
    from app import store

    store.init()

    captured = {}

    async def upstream_handler(request: httpx.Request):
        body = json.loads(request.content)
        captured["body"] = body
        if body.get("stream"):
            # Antwort enthält den Platzhalter, zerrissen über zwei Chunks
            def sse():
                c1 = {"id": "x", "choices": [{"delta": {"role": "assistant", "content": "Hallo [EMAIL_"}, "index": 0}]}
                c2 = {"id": "x", "choices": [{"delta": {"content": "ADDRESS_1], alles klar."}, "index": 0}]}
                yield f"data: {json.dumps(c1)}\n\n".encode()
                yield f"data: {json.dumps(c2)}\n\n".encode()
                yield b"data: [DONE]\n\n"

            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"".join(sse()))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "Deine Mail ist [EMAIL_ADDRESS_1]."}}]},
        )

    main_mod.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # Non-Streaming
        r = await c.post(
            "/v1/chat/completions",
            json={
                "model": "test/model",
                "messages": [{"role": "user", "content": "Meine Mail ist roman.brun@gmail.com"}],
            },
            headers={"Authorization": "Bearer sk-test"},
        )
        assert r.status_code == 200, r.text
        # Upstream hat NUR anonymisierte Daten gesehen:
        upstream_msg = captured["body"]["messages"][0]["content"]
        assert "roman.brun@gmail.com" not in upstream_msg, upstream_msg
        assert "[EMAIL_ADDRESS_1]" in upstream_msg
        # Antwort an Client ist de-anonymisiert:
        content = r.json()["choices"][0]["message"]["content"]
        assert content == "Deine Mail ist roman.brun@gmail.com.", content
        print("PASS test_proxy non-streaming")

        # Streaming
        async with c.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "test/model",
                "stream": True,
                "messages": [{"role": "user", "content": "Meine Mail ist roman.brun@gmail.com"}],
            },
            headers={"Authorization": "Bearer sk-test"},
        ) as r:
            assert r.status_code == 200
            text = ""
            async for line in r.aiter_lines():
                if line.startswith("data:") and "[DONE]" not in line:
                    obj = json.loads(line[5:])
                    text += (obj["choices"][0]["delta"].get("content") or "")
        assert text == "Hallo roman.brun@gmail.com, alles klar.", repr(text)
        print("PASS test_proxy streaming")

    # Log-Einträge prüfen
    entries = store.list_entries()
    assert len(entries) >= 2
    detail = store.get_entry(entries[0]["id"])
    assert detail["entities"], "Entities wurden geloggt"
    assert detail["response_final"]
    print("PASS test_store")


if __name__ == "__main__":
    test_anonymization()
    test_stream_deanonymizer()
    asyncio.run(test_proxy())
    print("\nALLE TESTS BESTANDEN")
