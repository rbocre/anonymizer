"""GLiNER2-basierte PII-Erkennung (fastino/gliner2-privacy-filter-PII-multi).

Kontextbasiertes NER-Modell (205M Parameter, 7 Sprachen inkl. Deutsch).
Erkennt Namen/Orte/etc. deutlich robuster als spaCy-NER – insbesondere keine
False Positives bei Imperativen ("Schreib", "Erstelle") am Satzanfang.
"""

import os
import re
import threading

from presidio_analyzer import RecognizerResult

GLINER_MODEL = os.getenv("GLINER_MODEL", "fastino/gliner2-privacy-filter-PII-multi")
GLINER_THRESHOLD = float(os.getenv("GLINER_THRESHOLD", "0.5"))
GLINER_MAX_CHUNK = int(os.getenv("GLINER_MAX_CHUNK", "1500"))

# GLiNER-Label -> Platzhalter-Typ
LABEL_MAP = {
    "person": "PERSON",
    "full_name": "PERSON",
    "first_name": "PERSON",
    "middle_name": "PERSON",
    "last_name": "PERSON",
    "email": "EMAIL_ADDRESS",
    "phone_number": "PHONE_NUMBER",
    "address": "ADDRESS",
    "street_address": "ADDRESS",
    "postal_code": "ADDRESS",
    "city": "LOCATION",
    "state_or_region": "LOCATION",
    "country": "LOCATION",
    "iban": "IBAN_CODE",
    "payment_card": "CREDIT_CARD",
    "card_number": "CREDIT_CARD",
    "bank_account": "BANK_ACCOUNT",
    "account_number": "BANK_ACCOUNT",
    "routing_number": "BANK_ACCOUNT",
    "ip_address": "IP_ADDRESS",
    "username": "USERNAME",
    "government_id": "GOVERNMENT_ID",
    "national_id_number": "GOVERNMENT_ID",
    "passport_number": "GOVERNMENT_ID",
    "drivers_license_number": "GOVERNMENT_ID",
    "tax_id": "GOVERNMENT_ID",
    "tax_number": "GOVERNMENT_ID",
    "date_of_birth": "DATE_OF_BIRTH",
    "password": "CREDENTIAL",
    "secret": "CREDENTIAL",
    "api_key": "CREDENTIAL",
    "access_token": "CREDENTIAL",
    "recovery_code": "CREDENTIAL",
}

DEFAULT_LABELS = (
    "person,email,phone_number,address,city,country,iban,payment_card,"
    "bank_account,ip_address,username,passport_number,national_id_number,"
    "date_of_birth,password,api_key"
)
GLINER_LABELS = [
    l.strip()
    for l in os.getenv("GLINER_LABELS", DEFAULT_LABELS).split(",")
    if l.strip()
]

# Optionale Schwellen pro Label, z.B. GLINER_LABEL_THRESHOLDS="person:0.55,city:0.7"
LABEL_THRESHOLDS = {}
for pair in os.getenv("GLINER_LABEL_THRESHOLDS", "").split(","):
    if ":" in pair:
        label, _, value = pair.partition(":")
        try:
            LABEL_THRESHOLDS[label.strip()] = float(value)
        except ValueError:
            pass

_model = None
_lock = threading.Lock()

_SENT_SPLIT = re.compile(r"(?<=[.!?\n])\s+")


def get_model():
    """Lädt das GLiNER2-Modell lazy (Download beim ersten Mal, danach HF-Cache)."""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from gliner2 import GLiNER2

                _model = GLiNER2.from_pretrained(GLINER_MODEL)
    return _model


def _chunks(text: str):
    """Zerlegt lange Texte (z.B. RAG-Kontexte) an Satzgrenzen in Stücke mit Offset."""
    if len(text) <= GLINER_MAX_CHUNK:
        yield 0, text
        return
    offset = 0
    current = ""
    for part in _SENT_SPLIT.split(text):
        if current and len(current) + len(part) + 1 > GLINER_MAX_CHUNK:
            yield offset, current
            offset = text.index(current, offset) + len(current)
            current = part
        else:
            current = f"{current} {part}".strip() if current else part
    if current:
        yield text.rindex(current), current


def _iter_entities(raw: dict):
    """Normalisiert die Rückgabe von extract_entities (mit/ohne 'entities'-Hülle)."""
    if not isinstance(raw, dict):
        return
    entities = raw.get("entities", raw)
    if not isinstance(entities, dict):
        return
    for label, items in entities.items():
        if isinstance(items, list):
            for item in items:
                yield label, item


def analyze(text: str) -> list[RecognizerResult]:
    """PII-Erkennung via GLiNER2; Rückgabe kompatibel zu Presidio-Ergebnissen."""
    model = get_model()
    results: list[RecognizerResult] = []
    for offset, chunk in _chunks(text):
        raw = model.extract_entities(
            chunk,
            GLINER_LABELS,
            threshold=min([GLINER_THRESHOLD, *LABEL_THRESHOLDS.values()] or [0.5]),
            include_confidence=True,
            include_spans=True,
        )
        for label, item in _iter_entities(raw):
            entity_type = LABEL_MAP.get(label, label.upper())
            required = LABEL_THRESHOLDS.get(label, GLINER_THRESHOLD)
            if isinstance(item, dict) and "start" in item and "end" in item:
                score = float(item.get("confidence", 1.0))
                if score < required:
                    continue
                results.append(
                    RecognizerResult(
                        entity_type, offset + int(item["start"]), offset + int(item["end"]), score
                    )
                )
            else:
                # Fallback ohne Spans: alle Vorkommen des Texts suchen
                value = item.get("text") if isinstance(item, dict) else item
                score = float(item.get("confidence", 1.0)) if isinstance(item, dict) else 1.0
                if not value or score < required:
                    continue
                start = chunk.find(value)
                while start != -1:
                    results.append(
                        RecognizerResult(
                            entity_type, offset + start, offset + start + len(value), score
                        )
                    )
                    start = chunk.find(value, start + 1)
    return results
