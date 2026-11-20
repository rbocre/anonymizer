"""PII-Erkennung und (De-)Anonymisierung mit Microsoft Presidio (de + en)."""

import os
import re
import threading
from dataclasses import dataclass

from langdetect import DetectorFactory, detect

DetectorFactory.seed = 0

DEFAULT_LANGUAGE = os.getenv("DEFAULT_LANGUAGE", "de")
SCORE_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", "0.4"))

# NER-Engine: "gliner" (GLiNER2-PII-Modell, empfohlen), "spacy" (Presidio-NER)
# oder "none" (nur Muster wie E-Mail/IBAN/Telefon + eigene Regeln, sehr leicht)
NER_ENGINE = os.getenv("NER_ENGINE", "gliner").lower()
ENTITIES = [
    e.strip()
    for e in os.getenv(
        "ENTITIES",
        "PERSON,EMAIL_ADDRESS,PHONE_NUMBER,IBAN_CODE,CREDIT_CARD,"
        "IP_ADDRESS,LOCATION,AHV_NUMBER",
    ).split(",")
    if e.strip()
]

PLACEHOLDER_RE = re.compile(r"\[([A-Z_]+)_(\d+)\]")

# NER-basierte Entitätstypen: anfällig für False Positives (Verben/Nomen am
# Satzanfang). Diese Treffer nur akzeptieren, wenn der Span ein PROPN enthält.
NER_TYPES = {"PERSON", "LOCATION", "NRP", "ORGANIZATION"}
NER_REQUIRE_PROPN = os.getenv("NER_REQUIRE_PROPN", "true").lower() in ("1", "true", "yes")

# Wörter, die nie als PII gelten (kommagetrennt, case-insensitive)
NER_IGNORE_WORDS = {
    w.strip().lower() for w in os.getenv("NER_IGNORE_WORDS", "").split(",") if w.strip()
}

_SENT_END = {".", "!", "?", ":", ";"}

# Typen, die Presidio liefern kann (Pattern-Recognizer bzw. spaCy-NER)
_PRESIDIO_TYPES = {
    "PERSON",
    "LOCATION",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "IBAN_CODE",
    "CREDIT_CARD",
    "IP_ADDRESS",
    "AHV_NUMBER",
}
# Typen, die (auch) über GLiNER kommen
_GLINER_TYPES = {
    "PERSON",
    "LOCATION",
    "ADDRESS",
    "DATE_OF_BIRTH",
    "CREDENTIAL",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "IBAN_CODE",
    "CREDIT_CARD",
    "IP_ADDRESS",
}

_hanta = None


def _get_hanta():
    """HanTa: deutscher Morphologie-Tagger. Erkennt Imperative ('Schreib',
    'Erstelle') zuverlässig als Verben, wo spaCy sie als PROPN fehlklassifiziert."""
    global _hanta
    if _hanta is None:
        with _lock:
            if _hanta is None:
                try:
                    from HanTa import HanoverTagger

                    _hanta = HanoverTagger.HanoverTagger("morphmodel_ger.pgz")
                except Exception:
                    _hanta = False
    return _hanta or None

_analyzer = None
_lock = threading.Lock()


def get_analyzer():
    """Lazily build the Presidio AnalyzerEngine (loading spaCy models is slow)."""
    global _analyzer
    if _analyzer is None:
        with _lock:
            if _analyzer is None:
                _analyzer = _build_analyzer()
    return _analyzer


def _blank_nlp_engine():
    """Leichte spaCy-Engine ohne Modelle: Im GLiNER-Modus übernimmt GLiNER2 das
    NER; Presidio braucht nur noch Tokenisierung für die Pattern-Recognizer."""
    import spacy
    from presidio_analyzer.nlp_engine import SpacyNlpEngine
    from presidio_analyzer.nlp_engine.ner_model_configuration import (
        NerModelConfiguration,
    )

    engine = object.__new__(SpacyNlpEngine)
    engine.models = [
        {"lang_code": "de", "model_name": "blank_de"},
        {"lang_code": "en", "model_name": "blank_en"},
    ]
    engine.ner_model_configuration = NerModelConfiguration()
    engine.nlp = {"de": spacy.blank("de"), "en": spacy.blank("en")}
    return engine


def _build_analyzer():
    from presidio_analyzer import (
        AnalyzerEngine,
        Pattern,
        PatternRecognizer,
        RecognizerRegistry,
    )
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    if NER_ENGINE in ("gliner", "none"):
        nlp_engine = _blank_nlp_engine()
    else:
        nlp_engine = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [
                    {"lang_code": "de", "model_name": "de_core_news_md"},
                    {"lang_code": "en", "model_name": "en_core_web_md"},
                ],
            }
        ).create_engine()

    registry = RecognizerRegistry(supported_languages=["de", "en"])
    registry.load_predefined_recognizers(languages=["de", "en"], nlp_engine=nlp_engine)

    # Schweizer AHV-Nummer, z.B. 756.1234.5678.97
    ahv = Pattern(name="ahv", regex=r"\b756\.?\d{4}\.?\d{4}\.?\d{2}\b", score=0.9)
    for lang in ("de", "en"):
        registry.add_recognizer(
            PatternRecognizer(
                supported_entity="AHV_NUMBER",
                name=f"AhvRecognizer_{lang}",
                patterns=[ahv],
                supported_language=lang,
            )
        )

    return AnalyzerEngine(
        nlp_engine=nlp_engine,
        registry=registry,
        supported_languages=["de", "en"],
        default_score_threshold=SCORE_THRESHOLD,
    )


def preload():
    """Beim App-Start aufrufen: lädt Analyzer + ggf. GLiNER-Modell, damit der
    erste Request nicht minutenlang hängt (GLiNER: Download beim ersten Start)."""
    get_analyzer()
    if NER_ENGINE == "gliner":
        from . import gliner_engine

        gliner_engine.get_model()


def detect_language(text: str) -> str:
    try:
        lang = detect(text)
        return lang if lang in ("de", "en") else DEFAULT_LANGUAGE
    except Exception:
        return DEFAULT_LANGUAGE


def ner_available() -> bool:
    return NER_ENGINE in ("gliner", "spacy")


class GuardrailBlocked(Exception):
    """Eine Block-Regel hat angeschlagen; der Request darf nicht weiter."""

    def __init__(self, rule: str, reason: str, message: str):
        super().__init__(message)
        self.rule = rule
        self.reason = reason
        self.message = message


@dataclass
class _Hit:
    entity_type: str
    start: int
    end: int
    score: float
    action: str = "mask"  # mask | redact
    replacement: str = ""
    source: str = "builtin"  # builtin | rule


class AnonymizationSession:
    """Konsistentes Mapping Original <-> Platzhalter über einen ganzen Request.

    Ohne config gelten die globalen Einstellungen aus der .env (Einzel-User-
    Betrieb). Mit config (Multi-User) gelten Entitäten, Schwelle, Ignore-Liste
    und eigene Regex-Regeln des jeweiligen Users."""

    def __init__(self, config: dict | None = None):
        self.mapping: dict[str, str] = {}  # placeholder -> original
        self._reverse: dict[str, str] = {}  # original -> placeholder
        self._counters: dict[str, int] = {}
        self.entities: list[dict] = []
        self.config = config
        if config is None:
            self.entity_types = list(ENTITIES)
            self.threshold = SCORE_THRESHOLD
            self.ignore_words = set(NER_IGNORE_WORDS)
            self.rules = []
            self.block_message = ""
        else:
            from .guardrail import compile_rules

            self.entity_types = list(config.get("entities", []))
            self.threshold = float(config.get("score_threshold", SCORE_THRESHOLD))
            self.ignore_words = set(NER_IGNORE_WORDS) | {
                w.lower() for w in config.get("ignore_words", [])
            }
            self.rules = compile_rules(config)
            self.block_message = config.get("block_message", "")

    # ------------------------------------------------------------ rules

    def _rule_hits(self, text: str) -> list[_Hit]:
        from .guardrail import REGEX_TIMEOUT

        hits = []
        for rule, pat in self.rules:
            try:
                matches = list(pat.finditer(text, timeout=REGEX_TIMEOUT))
            except TimeoutError:
                # fail-closed: eine zu langsame Regel blockiert statt durchzulassen
                raise GuardrailBlocked(
                    rule["name"],
                    "timeout",
                    f"Regel {rule['name']} war zu langsam (Timeout) – Anfrage blockiert.",
                )
            for m in matches:
                if m.end() <= m.start():
                    continue
                if rule["action"] == "block":
                    self.entities.append(
                        {
                            "type": rule["name"],
                            "original": m.group(0),
                            "placeholder": "BLOCK",
                            "score": 1.0,
                            "lang": "",
                            "source": "rule",
                            "action": "block",
                        }
                    )
                    raise GuardrailBlocked(
                        rule["name"],
                        "match",
                        self.block_message or f"Blockiert durch Regel {rule['name']}.",
                    )
                hits.append(
                    _Hit(
                        rule["name"],
                        m.start(),
                        m.end(),
                        2.0,  # eigene Regeln gewinnen bei Überlappung
                        rule["action"],
                        rule.get("replacement", "[ENTFERNT]"),
                        "rule",
                    )
                )
        return hits

    # ------------------------------------------------------------ builtin

    def _builtin_hits(self, text: str, lang: str) -> list[_Hit]:
        types = set(self.entity_types)
        if not types:
            return []
        presidio_types = [t for t in self.entity_types if t in _PRESIDIO_TYPES]
        if NER_ENGINE != "spacy":
            presidio_types = [t for t in presidio_types if t not in NER_TYPES]
        results = []
        if presidio_types:
            results = get_analyzer().analyze(text=text, language=lang, entities=presidio_types)
            results = [r for r in results if r.score >= self.threshold]
        if NER_ENGINE == "gliner" and (self.config is None or types & _GLINER_TYPES):
            # GLiNER2 übernimmt das NER; Presidio liefert nur Pattern-Treffer
            # (blank-Engine hat kein NER). Kontextmodell -> keine HanTa-Filter nötig.
            # Einzel-User-Betrieb: alle GLiNER-Labels wie bisher; Multi-User:
            # nur die vom User gewählten Typen.
            from . import gliner_engine

            results += [
                r
                for r in gliner_engine.analyze(text)
                if self.config is None or r.entity_type in types
            ]
        elif NER_ENGINE == "spacy":
            results = self._filter_ner_false_positives(text, lang, results)
        if self.ignore_words:
            results = [
                r for r in results if text[r.start : r.end].strip().lower() not in self.ignore_words
            ]
        return [_Hit(r.entity_type, r.start, r.end, r.score) for r in results]

    # ------------------------------------------------------------ main

    def anonymize(self, text: str) -> str:
        if not text or not text.strip():
            return text
        lang = detect_language(text)
        results = self._rule_hits(text) + self._builtin_hits(text, lang)
        # Überlappungen: höherer Score gewinnt, dann längerer Treffer
        results.sort(key=lambda r: (-r.score, -(r.end - r.start)))
        chosen = []
        for r in results:
            if not any(r.start < c.end and c.start < r.end for c in chosen):
                chosen.append(r)
        chosen.sort(key=lambda r: r.start, reverse=True)

        out = text
        for r in chosen:
            original = text[r.start : r.end]
            key = original.strip()
            if r.action == "redact":
                self.entities.append(
                    {
                        "type": r.entity_type,
                        "original": key,
                        "placeholder": r.replacement,
                        "score": 1.0,
                        "lang": lang,
                        "source": r.source,
                        "action": "redact",
                    }
                )
                out = out[: r.start] + r.replacement + out[r.end :]
                continue
            if key in self._reverse:
                placeholder = self._reverse[key]
            else:
                n = self._counters.get(r.entity_type, 0) + 1
                self._counters[r.entity_type] = n
                placeholder = f"[{r.entity_type}_{n}]"
                self._reverse[key] = placeholder
                self.mapping[placeholder] = key
                self.entities.append(
                    {
                        "type": r.entity_type,
                        "original": key,
                        "placeholder": placeholder,
                        "score": round(min(r.score, 1.0), 2),
                        "lang": lang,
                        "source": r.source,
                        "action": "mask",
                    }
                )
            out = out[: r.start] + placeholder + out[r.end :]
        return out

    def _filter_ner_false_positives(self, text, lang, results):
        """Verwirft NER-Treffer, die keine Namen sind (z.B. Verben am Satzanfang)."""
        if not NER_REQUIRE_PROPN or not any(r.entity_type in NER_TYPES for r in results):
            return results
        NER_IGNORE_WORDS = self.ignore_words
        nlp = get_analyzer().nlp_engine.nlp.get(lang)
        if nlp is None:
            return results
        has_tagger = "tagger" in nlp.pipe_names or "morphologizer" in nlp.pipe_names
        disable = [p for p in ("ner", "parser") if p in nlp.pipe_names]
        with nlp.select_pipes(disable=disable):
            doc = nlp(text)
        hanta_tags = self._hanta_tags(doc) if lang == "de" else None
        kept = []
        for r in results:
            if r.entity_type in NER_TYPES:
                if text[r.start : r.end].strip().lower() in NER_IGNORE_WORDS:
                    continue
                span = doc.char_span(r.start, r.end, alignment_mode="expand")
                if span is None:
                    continue
                if has_tagger and not any(t.pos_ == "PROPN" for t in span):
                    continue
                if len(span) == 1:
                    # Deutsch: HanTa ist bei Imperativen zuverlässiger als spaCy
                    if hanta_tags is not None:
                        tag = hanta_tags.get(span[0].i, "")
                        if tag.startswith(("VV", "VM", "VA")):
                            continue
                    elif has_tagger and self._is_disguised_verb(nlp, doc, span[0]):
                        continue
            kept.append(r)
        return kept

    @staticmethod
    def _hanta_tags(doc) -> dict | None:
        """Tagt den Text satzweise mit HanTa; Rückgabe: Token-Index -> STTS-Tag."""
        tagger = _get_hanta()
        if tagger is None:
            return None
        tags: dict[int, str] = {}
        sentence: list = []
        for tok in list(doc) + [None]:
            if tok is not None and not tok.is_space:
                sentence.append(tok)
            if (tok is None or tok.text in _SENT_END) and sentence:
                try:
                    result = tagger.tag_sent([t.text for t in sentence])
                    for t, (_, _, tag) in zip(sentence, result):
                        tags[t.i] = tag
                except Exception:
                    pass
                sentence = []
        return tags

    # Verb-Tags: TIGER (deutsch) VV*/VM*/VA*, Penn Treebank (englisch) VB*
    _VERB_TAG_PREFIXES = ("VV", "VM", "VA", "VB")

    @classmethod
    def _is_disguised_verb(cls, nlp, doc, tok) -> bool:
        """Grossgeschriebene Einzelwörter (z.B. Imperativ 'Schreib' am Satzanfang)
        werden vom Morphologizer oft als PROPN fehlklassifiziert. Zwei Checks:
        1. Feingranulares Tag (tag_) sagt Verb -> Verb.
        2. Wort kleingeschrieben nachtaggen -> wird es ein Verb, ist es kein Name."""
        if tok.tag_.startswith(cls._VERB_TAG_PREFIXES) or tok.pos_ in ("VERB", "AUX"):
            return True
        if tok.text.lower() == tok.text:
            return False
        # Kleinschreib-Test nur am Satz-/Textanfang, sonst sind Namen zu oft betroffen
        if tok.i != 0:
            prev = doc[tok.i - 1]
            if prev.text not in _SENT_END and not prev.is_space:
                return False
        lowered = doc.text[: tok.idx] + tok.text.lower() + doc.text[tok.idx + len(tok.text) :]
        disable = [p for p in ("ner", "parser") if p in nlp.pipe_names]
        with nlp.select_pipes(disable=disable):
            d2 = nlp(lowered)
        s2 = d2.char_span(tok.idx, tok.idx + len(tok.text), alignment_mode="expand")
        return s2 is not None and any(
            t.pos_ in ("VERB", "AUX") or t.tag_.startswith(cls._VERB_TAG_PREFIXES)
            for t in s2
        )

    def deanonymize(self, text: str) -> str:
        if not text:
            return text
        for placeholder, original in self.mapping.items():
            text = text.replace(placeholder, original)
        return text


class StreamDeanonymizer:
    """Ersetzt Platzhalter in einem Token-Stream; puffert unvollständige Platzhalter
    über Chunk-Grenzen hinweg."""

    MAX_HOLD = 48

    def __init__(self, session: AnonymizationSession):
        self.session = session
        self.buf = ""

    def feed(self, text: str) -> str:
        self.buf += text
        replaced = self.session.deanonymize(self.buf)
        idx = replaced.rfind("[")
        if idx != -1 and "]" not in replaced[idx:]:
            tail = replaced[idx:]
            # Nur zurückhalten, wenn es wie ein Platzhalter-Anfang aussieht
            if re.fullmatch(r"\[[A-Z_0-9]*", tail) and len(tail) <= self.MAX_HOLD:
                emit, self.buf = replaced[:idx], tail
                return emit
        emit, self.buf = replaced, ""
        return emit

    def flush(self) -> str:
        out = self.session.deanonymize(self.buf)
        self.buf = ""
        return out
