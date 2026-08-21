"""Testet die GLiNER-Integration mit einem Fake-Modell (kein HF-Download nötig).

Prüft: Span-Verarbeitung, Label-Mapping, Schwellen, Chunking-Offsets,
Zusammenspiel mit den Presidio-Pattern-Recognizern und der Ignore-Liste.
Der echte Modell-Test läuft lokal/auf dem VPS (tests/debug_gliner.py).
"""

import os
import sys

os.environ["NER_ENGINE"] = "gliner"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.anonymizer as anon_mod
import app.gliner_engine as ge


class FakeGLiNER:
    """Simuliert GLiNER2.extract_entities mit dem dokumentierten Rückgabeformat."""

    def __init__(self):
        self.calls = []

    def extract_entities(self, text, labels, threshold=0.5, **kw):
        self.calls.append(text)
        entities = {}

        def add(label, needle, conf):
            start = text.find(needle)
            if start != -1:
                entities.setdefault(label, []).append(
                    {"text": needle, "confidence": conf, "start": start, "end": start + len(needle)}
                )

        add("person", "Roman Brun", 0.93)
        add("person", "Anna Meier", 0.88)
        add("city", "Basel", 0.81)
        add("person", "Niedrigwert", 0.2)  # unter Threshold -> muss rausfallen
        return {"entities": entities}


def test_analyze_mapping():
    ge._model = FakeGLiNER()
    text = "Schreib eine Mail an Roman Brun aus Basel. Niedrigwert bleibt."
    results = ge.analyze(text)
    got = sorted((r.entity_type, text[r.start : r.end]) for r in results)
    assert got == [("LOCATION", "Basel"), ("PERSON", "Roman Brun")], got
    print("PASS test_analyze_mapping (Label-Mapping, Threshold, Spans)")


def test_chunking_offsets():
    ge._model = FakeGLiNER()
    filler = "Das ist ein Satz ohne besondere Inhalte. " * 60  # > GLINER_MAX_CHUNK
    text = filler + "Am Ende steht Anna Meier."
    results = ge.analyze(text)
    hits = [(text[r.start : r.end], r.entity_type) for r in results]
    assert ("Anna Meier", "PERSON") in hits, hits
    assert len(ge._model.calls) > 1, "Text muss in Chunks zerlegt werden"
    print(f"PASS test_chunking_offsets ({len(ge._model.calls)} Chunks)")


def test_full_anonymize_with_patterns():
    """GLiNER (Fake) + Presidio-Pattern (echt, blank-Engine) im Zusammenspiel."""
    ge._model = FakeGLiNER()
    anon_mod._analyzer = None  # frisch bauen -> nutzt blank-Engine (NER_ENGINE=gliner)
    s = anon_mod.AnonymizationSession()
    text = (
        "Schreib eine Mail an Roman Brun (roman.brun@gmail.com) aus Basel, "
        "AHV 756.1234.5678.97."
    )
    out = s.anonymize(text)
    print("ANON:", out)
    assert "Roman Brun" not in out and "[PERSON_1]" in out
    assert "roman.brun@gmail.com" not in out and "[EMAIL_ADDRESS_1]" in out
    assert "756.1234.5678.97" not in out and "[AHV_NUMBER_1]" in out
    assert "Basel" not in out and "[LOCATION_1]" in out
    assert "Schreib" in out  # Imperativ bleibt stehen (GLiNER meldet ihn nicht)
    assert s.deanonymize(out) == text
    print("PASS test_full_anonymize_with_patterns")


def test_ignore_words():
    ge._model = FakeGLiNER()
    anon_mod._analyzer = None
    anon_mod.NER_IGNORE_WORDS = {"basel"}
    try:
        s = anon_mod.AnonymizationSession()
        out = s.anonymize("Roman Brun wohnt in Basel.")
        assert "Basel" in out and "[PERSON_1]" in out, out
    finally:
        anon_mod.NER_IGNORE_WORDS = set()
    print("PASS test_ignore_words")


if __name__ == "__main__":
    test_analyze_mapping()
    test_chunking_offsets()
    test_full_anonymize_with_patterns()
    test_ignore_words()
    print("\nALLE GLINER-TESTS BESTANDEN")
