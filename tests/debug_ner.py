"""Debug: zeigt für einen Text, was spaCy/Presidio erkennen und warum.

Aufruf:  python tests/debug_ner.py "Schreib eine kurze Mail an Roman Brun aus Basel."
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.anonymizer import AnonymizationSession, detect_language, get_analyzer

text = (
    sys.argv[1]
    if len(sys.argv) > 1
    else "Schreib eine kurze Mail an Roman Brun (roman.brun@gmail.com) aus Basel."
)

lang = detect_language(text)
print(f"Sprache: {lang}\n")

nlp = get_analyzer().nlp_engine.nlp[lang]
doc = nlp(text)

print(f"{'Token':<20} {'pos_':<8} {'tag_':<8} {'ent_type_':<10}")
print("-" * 50)
for t in doc:
    print(f"{t.text:<20} {t.pos_:<8} {t.tag_:<8} {t.ent_type_:<10}")

print("\nPresidio-Ergebnisse (roh):")
for r in get_analyzer().analyze(text=text, language=lang):
    print(f"  {r.entity_type:<15} '{text[r.start:r.end]}' score={r.score:.2f}")

print("\nNach Filterung (AnonymizationSession):")
s = AnonymizationSession()
print(" ", s.anonymize(text))
for e in s.entities:
    print(f"  {e['type']:<15} '{e['original']}' -> {e['placeholder']}")
