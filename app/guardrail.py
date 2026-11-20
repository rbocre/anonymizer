"""Guardrail-Konfiguration pro User (Multi-User-Modus).

Eine Konfiguration ist bewusst deklarativ (kein eigener Code): eingebaute
Entitätstypen ein/aus, Mindest-Score, Ignore-Liste und eigene Regex-Regeln mit
einer Aktion. So können Studierende Guardrails bauen, ohne dass auf dem Server
fremder Code ausgeführt wird.

Aktionen eigener Regeln:
  mask   -> nummerierter Platzhalter [NAME_1], Antwort wird zurückübersetzt
  redact -> fester Ersatztext (z. B. [ENTFERNT]), nicht umkehrbar
  block  -> Request wird abgelehnt und erreicht das LLM nicht
"""

import copy
import os
import re

import regex

# Eingebaute Typen. "ner": braucht ein NER-Modell (NER_ENGINE=gliner|spacy).
BUILTIN_ENTITIES = [
    {"type": "PERSON", "label": "Personennamen", "ner": True},
    {"type": "LOCATION", "label": "Orte, Städte, Länder", "ner": True},
    {"type": "ADDRESS", "label": "Adressen", "ner": True, "gliner_only": True},
    {"type": "DATE_OF_BIRTH", "label": "Geburtsdaten", "ner": True, "gliner_only": True},
    {"type": "CREDENTIAL", "label": "Passwörter, API-Keys", "ner": True, "gliner_only": True},
    {"type": "EMAIL_ADDRESS", "label": "E-Mail-Adressen", "ner": False},
    {"type": "PHONE_NUMBER", "label": "Telefonnummern", "ner": False},
    {"type": "IBAN_CODE", "label": "IBAN", "ner": False},
    {"type": "CREDIT_CARD", "label": "Kreditkartennummern", "ner": False},
    {"type": "IP_ADDRESS", "label": "IP-Adressen", "ner": False},
    {"type": "AHV_NUMBER", "label": "AHV-Nummern (756.xxxx.xxxx.xx)", "ner": False},
]
BUILTIN_TYPES = {e["type"] for e in BUILTIN_ENTITIES}

ACTIONS = ("mask", "redact", "block")
MAX_RULES = int(os.getenv("GUARDRAIL_MAX_RULES", "20"))
MAX_PATTERN_LEN = 300
MAX_IGNORE_WORDS = 100
REGEX_TIMEOUT = float(os.getenv("GUARDRAIL_REGEX_TIMEOUT", "0.2"))

RULE_NAME_RE = re.compile(r"^[A-Z][A-Z_]{1,29}$")

DEFAULT_CONFIG = {
    "entities": [
        "PERSON",
        "LOCATION",
        "EMAIL_ADDRESS",
        "PHONE_NUMBER",
        "IBAN_CODE",
        "CREDIT_CARD",
        "IP_ADDRESS",
        "AHV_NUMBER",
    ],
    "score_threshold": 0.4,
    "ignore_words": [],
    "rules": [],
    "block_message": "Diese Anfrage wurde von deiner Guardrail blockiert.",
}


def default_config() -> dict:
    return copy.deepcopy(DEFAULT_CONFIG)


class GuardrailConfigError(ValueError):
    pass


def validate_config(raw: dict) -> dict:
    """Prüft und normalisiert eine Konfiguration aus dem Web-UI.
    Wirft GuardrailConfigError mit einer verständlichen Meldung."""
    if not isinstance(raw, dict):
        raise GuardrailConfigError("Konfiguration muss ein Objekt sein.")
    cfg = default_config()

    entities = raw.get("entities", cfg["entities"])
    if not isinstance(entities, list) or not all(isinstance(e, str) for e in entities):
        raise GuardrailConfigError("«entities» muss eine Liste von Typen sein.")
    unknown = [e for e in entities if e not in BUILTIN_TYPES]
    if unknown:
        raise GuardrailConfigError(f"Unbekannte Typen: {', '.join(unknown)}")
    order = [e["type"] for e in BUILTIN_ENTITIES]
    cfg["entities"] = sorted(set(entities), key=order.index)

    try:
        thr = float(raw.get("score_threshold", cfg["score_threshold"]))
    except (TypeError, ValueError):
        raise GuardrailConfigError("Mindest-Score muss eine Zahl sein.")
    if not 0 <= thr <= 1:
        raise GuardrailConfigError("Mindest-Score muss zwischen 0 und 1 liegen.")
    cfg["score_threshold"] = round(thr, 3)

    words = raw.get("ignore_words", [])
    if isinstance(words, str):
        words = [w for w in words.split(",")]
    if not isinstance(words, list):
        raise GuardrailConfigError("«ignore_words» muss eine Liste sein.")
    words = [str(w).strip() for w in words if str(w).strip()]
    if len(words) > MAX_IGNORE_WORDS or any(len(w) > 60 for w in words):
        raise GuardrailConfigError(
            f"Höchstens {MAX_IGNORE_WORDS} Ignore-Wörter mit je max. 60 Zeichen."
        )
    cfg["ignore_words"] = words

    msg = str(raw.get("block_message") or cfg["block_message"]).strip()
    cfg["block_message"] = msg[:300]

    rules = raw.get("rules", [])
    if not isinstance(rules, list):
        raise GuardrailConfigError("«rules» muss eine Liste sein.")
    if len(rules) > MAX_RULES:
        raise GuardrailConfigError(f"Höchstens {MAX_RULES} eigene Regeln.")
    seen = set()
    cfg["rules"] = []
    for i, r in enumerate(rules, 1):
        cfg["rules"].append(_validate_rule(r, i, seen))
    return cfg


def _validate_rule(r: dict, i: int, seen: set) -> dict:
    if not isinstance(r, dict):
        raise GuardrailConfigError(f"Regel {i}: ungültiges Format.")
    name = str(r.get("name", "")).strip().upper()
    if not RULE_NAME_RE.match(name):
        raise GuardrailConfigError(
            f"Regel {i}: Name «{name}» ungültig. Erlaubt: 2–30 Zeichen, "
            "Grossbuchstaben und _, z. B. KUNDENNUMMER."
        )
    if name in BUILTIN_TYPES:
        raise GuardrailConfigError(f"Regel {i}: Name «{name}» ist ein eingebauter Typ.")
    if name in seen:
        raise GuardrailConfigError(f"Regel {i}: Name «{name}» kommt doppelt vor.")
    seen.add(name)

    pattern = str(r.get("pattern", ""))
    if not pattern or len(pattern) > MAX_PATTERN_LEN:
        raise GuardrailConfigError(
            f"Regel «{name}»: Muster fehlt oder ist länger als {MAX_PATTERN_LEN} Zeichen."
        )
    ignore_case = bool(r.get("ignore_case", False))
    try:
        compiled = regex.compile(pattern, regex.IGNORECASE if ignore_case else 0)
    except regex.error as e:
        raise GuardrailConfigError(f"Regel «{name}»: ungültiger regulärer Ausdruck ({e}).")
    if compiled.search("") is not None:
        raise GuardrailConfigError(
            f"Regel «{name}»: Muster passt auf einen leeren Text und würde überall treffen."
        )

    action = str(r.get("action", "mask")).lower()
    if action not in ACTIONS:
        raise GuardrailConfigError(f"Regel «{name}»: Aktion muss mask, redact oder block sein.")
    replacement = str(r.get("replacement") or "[ENTFERNT]")[:50]
    return {
        "name": name,
        "pattern": pattern,
        "ignore_case": ignore_case,
        "action": action,
        "replacement": replacement,
        "enabled": bool(r.get("enabled", True)),
    }


def compile_rules(cfg: dict) -> list[tuple[dict, "regex.Pattern"]]:
    out = []
    for r in cfg.get("rules", []):
        if not r.get("enabled", True):
            continue
        flags = regex.IGNORECASE if r.get("ignore_case") else 0
        out.append((r, regex.compile(r["pattern"], flags)))
    return out
