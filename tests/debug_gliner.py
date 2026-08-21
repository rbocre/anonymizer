"""Debug mit echtem GLiNER-Modell (lokal/VPS, braucht HF-Zugriff beim ersten Lauf).

Aufruf:  python tests/debug_gliner.py "Schreib eine Mail an Roman Brun aus Basel."
"""

import os
import sys

os.environ.setdefault("NER_ENGINE", "gliner")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import gliner_engine
from app.anonymizer import AnonymizationSession

text = (
    sys.argv[1]
    if len(sys.argv) > 1
    else "Schreib eine kurze Mail an Roman Brun (roman.brun@gmail.com) aus Basel, "
    "Tel +41 79 123 45 67, AHV 756.1234.5678.97."
)

print(f"Modell: {gliner_engine.GLINER_MODEL}")
print(f"Labels: {gliner_engine.GLINER_LABELS}")
print(f"Threshold: {gliner_engine.GLINER_THRESHOLD}\n")

print("GLiNER-Rohtreffer:")
for r in gliner_engine.analyze(text):
    print(f"  {r.entity_type:<15} '{text[r.start:r.end]}' score={r.score:.2f}")

print("\nGesamtergebnis (GLiNER + Pattern-Recognizer):")
s = AnonymizationSession()
print(" ", s.anonymize(text))
for e in s.entities:
    print(f"  {e['type']:<15} '{e['original']}' -> {e['placeholder']}")
