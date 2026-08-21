"""Testet den NER-False-Positive-Filter mit simulierten spaCy-Fehlern.

Reproduziert exakt den beobachteten Fall: spaCy-NER meldet 'Schreib' und
'erwaehne' fälschlich als PERSON/LOCATION. Der Filter (HanTa-Verb-Check)
muss sie verwerfen, echte Namen aber behalten."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from presidio_analyzer import RecognizerResult

import app.anonymizer as anon_mod
from tests.test_sandbox import build_blank_analyzer

TEXT = (
    "Schreib eine kurze Mail an Roman Brun (roman.brun@gmail.com) aus Basel "
    "und erwaehne seine Nummer +41 79 123 45 67."
)


def fake_results():
    """Nachgestellte spaCy-NER-Ausgabe inkl. der beobachteten False Positives."""

    def r(entity, needle):
        start = TEXT.index(needle)
        return RecognizerResult(entity, start, start + len(needle), 0.85)

    return [
        r("PERSON", "Schreib"),        # False Positive (Imperativ)
        r("PERSON", "Roman Brun"),     # echt
        r("LOCATION", "Basel"),        # echt
        r("LOCATION", "erwaehne"),     # False Positive (Verb)
    ]


def main():
    anon_mod._analyzer = build_blank_analyzer()
    s = anon_mod.AnonymizationSession()
    kept = s._filter_ner_false_positives(TEXT, "de", fake_results())
    kept_texts = sorted(TEXT[r.start : r.end] for r in kept)
    print("Behalten:", kept_texts)
    assert kept_texts == ["Basel", "Roman Brun"], kept_texts
    print("PASS: Verben verworfen, Namen behalten")

    # Gegenprobe: Namen am Satzanfang dürfen NICHT verworfen werden
    cases = [
        ("Roman Brun wohnt in Basel.", [("PERSON", "Roman Brun"), ("LOCATION", "Basel")], ["Basel", "Roman Brun"]),
        ("Anna schickt dir morgen die Datei.", [("PERSON", "Anna")], ["Anna"]),
        ("Kowalski meldet sich nicht.", [("PERSON", "Kowalski")], ["Kowalski"]),
        ("Erstelle mir eine Liste.", [("PERSON", "Erstelle")], []),
        ("Fasse zusammen, was Marco Rossi sagte.", [("PERSON", "Fasse"), ("PERSON", "Marco Rossi")], ["Marco Rossi"]),
        ("Frag Anna nach dem Termin!", [("PERSON", "Frag"), ("PERSON", "Anna")], ["Anna"]),
    ]
    for text, hits, expected in cases:
        results = []
        for entity, needle in hits:
            start = text.index(needle)
            results.append(RecognizerResult(entity, start, start + len(needle), 0.85))
        s2 = anon_mod.AnonymizationSession()
        kept = sorted(text[r.start : r.end] for r in s2._filter_ner_false_positives(text, "de", results))
        assert kept == sorted(expected), f"{text!r}: {kept} != {sorted(expected)}"
        print(f"PASS: {text!r} -> {kept}")

    print("\nALLE FILTER-TESTS BESTANDEN")


if __name__ == "__main__":
    main()
