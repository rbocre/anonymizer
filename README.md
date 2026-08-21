# Anonymizer – Dokumentation
 
PII-anonymisierender Proxy zwischen OpenWebUI und OpenRouter.
Repo: **https://github.com/rbocre/anonymizer** (privat) · Produktion: VPS `<VPS_IP>` (Contabo)
 
---
 
## 1. Überblick
 
Der Anonymizer ist ein OpenAI-kompatibler HTTP-Proxy. OpenWebUI redet mit ihm, als wäre er OpenRouter – dadurch ist keine Änderung an OpenWebUI nötig ausser der API-URL. Persönliche Daten (PII) verlassen den eigenen Server nur als Platzhalter.
 
```
┌────────────┐   1. Request (Klartext)    ┌──────────────────┐   2. Request (anonymisiert)   ┌────────────┐
│  OpenWebUI │ ─────────────────────────► │    Anonymizer    │ ────────────────────────────► │ OpenRouter │
│  (VPS:443) │                            │   (VPS:8800)     │                               │   (Cloud)  │
│            │ ◄───────────────────────── │                  │ ◄──────────────────────────── │            │
└────────────┘   4. Antwort (Klartext)    └──────┬───────────┘   3. Antwort (mit Platzhaltern)└────────────┘
                                                 │
                                          ┌──────▼───────┐
                                          │  Dashboard   │  alle Requests einsehbar:
                                          │  + SQLite    │  Original vs. anonymisiert
                                          └──────────────┘
```
 
Nur der Anonymizer (und sein Dashboard) sieht die Klartext-Daten. OpenRouter und das dahinterliegende LLM sehen ausschliesslich Platzhalter wie `[PERSON_1]`.
 
## 2. Wie ein Request durchläuft
 
**Schritt 1 – OpenWebUI → Anonymizer.** OpenWebUI schickt einen normalen OpenAI-Chat-Request (`POST /v1/chat/completions`) an `http://<VPS_IP>:8800/v1`. Der Request enthält die komplette Chat-Historie als `messages`-Liste – inklusive System-Prompts und per RAG injizierter Dokument-Auszüge. Der OpenRouter-API-Key steckt im `Authorization`-Header.
 
**Schritt 2 – PII-Erkennung.** Der Anonymizer geht durch jede Message (system, user, assistant) und erkennt PII zweigleisig:
 
1. **Muster-Erkennung** (deterministisch, Presidio): E-Mail, Telefonnummer, IBAN, Kreditkarte, IP-Adresse, Schweizer AHV-Nummer (`756.xxxx.xxxx.xx`) – Sicherheitsnetz für strukturierte PII
2. **NER via GLiNER2** (`fastino/gliner2-privacy-filter-PII-multi`, 205M-Parameter-Kontextmodell, 7 Sprachen inkl. Deutsch): Namen, Orte, Adressen, Geburtsdaten, Ausweisnummern, Credentials u. v. m. (bis zu 42 PII-Typen, konfigurierbar). Da das Modell den Kontext versteht, entfallen die False-Positive-Probleme des früheren spaCy-NER (Imperative wie "Schreib" als Namen). Lange Texte (RAG-Kontexte) werden an Satzgrenzen in Chunks zerlegt.
Alternativ steht mit `NER_ENGINE=spacy` die alte Presidio/spaCy-Pipeline zur Verfügung (inkl. PROPN- und HanTa-Verb-Filter gegen deren False Positives).
 
**Schritt 3 – Ersetzung mit Mapping.** Jede gefundene Entität wird durch einen nummerierten Platzhalter ersetzt: `Roman Brun` → `[PERSON_1]`, `roman.brun@gmail.com` → `[EMAIL_ADDRESS_1]`. Das Mapping gilt pro Request über alle Messages hinweg – derselbe Name bekommt immer denselben Platzhalter. Da OpenWebUI bei jedem Chat-Turn die ganze Historie mitschickt, bleibt die Zuordnung auch über ein Gespräch konsistent.
 
**Schritt 4 – Weiterleitung an OpenRouter.** Der anonymisierte Request geht an `https://openrouter.ai/api/v1/chat/completions`. Der API-Key wird durchgereicht, nicht gespeichert.
 
**Schritt 5 – De-Anonymisierung der Antwort.** Antwortet das LLM mit Platzhaltern ("Sehr geehrter [PERSON_1] ..."), ersetzt der Anonymizer sie anhand des Mappings zurück, bevor die Antwort an OpenWebUI geht. Das funktioniert auch beim **Streaming**: Da Platzhalter über mehrere Stream-Chunks zerrissen sein können (`[PER` + `SON_1]`), hält ein Puffer potenzielle Platzhalter-Anfänge zurück, bis sie vollständig sind.
 
**Schritt 6 – Protokollierung.** Jeder Request landet in einer SQLite-DB (`/data`-Volume): Original-Body, anonymisierter Body, erkannte Entitäten mit Scores, Antwort vor/nach De-Anonymisierung, Status, Dauer.
 
## 3. Komponenten
 
| Komponente | Datei | Aufgabe |
|---|---|---|
| Proxy/API | `app/main.py` | FastAPI: `/v1/chat/completions` (stream + non-stream), `/v1/models` (Passthrough), Dashboard-API, `/health` |
| Anonymisierung | `app/anonymizer.py` | Engine-Auswahl (gliner/spacy), Presidio-Pattern, Session-Mapping, Streaming-Puffer |
| GLiNER-NER | `app/gliner_engine.py` | GLiNER2-PII-Modell: Label-Mapping, Schwellen, Chunking langer Texte |
| Persistenz | `app/store.py` | SQLite-Log aller Requests |
| Dashboard | `app/static/index.html` | Single-Page-GUI, 3s-Auto-Refresh |
| Deployment | `Dockerfile`, `docker-compose.yml` | Python 3.11-slim, spaCy-Modelle im Build, Volume `/data` |
| Tests | `tests/` | `test_sandbox.py` (Pipeline mit Mock-Upstream), `test_ner_filter.py` (False-Positive-Filter), `debug_ner.py` (zeigt Tagger-Entscheidungen für beliebigen Text) |
 
## 4. Dashboard
 
**URL:** `http://<VPS_IP>:8800/?token=<DASHBOARD_TOKEN>`
 
- Linke Spalte: alle Requests (Zeit, Modell, Anzahl PII, Status, Dauer)
- Detailansicht: erkannte Entitäten (Typ, Original, Platzhalter, Score, Sprache), Anfrage im Vergleich **Eingang (Original)** vs. **Ausgang (anonymisiert an OpenRouter)**, Antwort **von OpenRouter (mit Platzhaltern)** vs. **an OpenWebUI (de-anonymisiert)**
- "Log leeren" löscht alle Einträge
Der Token ist in der `.env` als `DASHBOARD_TOKEN=<DASHBOARD_TOKEN>` gesetzt und schützt Dashboard und API (`/api/requests`). Ohne bzw. mit falschem Token: HTTP 401.
 
## 5. Setup
 
### 5.1 VPS (Produktion, so deployt)
 
```bash
curl -fsSL https://get.docker.com | sh
git clone https://rbocre:TOKEN@github.com/rbocre/anonymizer.git   # Token: Contents:Read genügt
cd anonymizer
cp .env.example .env && nano .env      # DASHBOARD_TOKEN=<DASHBOARD_TOKEN>
docker compose up -d --build           # erster Build: einige Minuten (spaCy-Modelle)
curl http://localhost:8800/health      # -> {"status":"ok"}
```
 
**Firewall (zwei Ebenen!):**
 
- **Contabo-Cloud-Firewall** (Kundenpanel): sitzt VOR dem VPS. Offen: 22, 80, 443, 8800. Achtung: Diese hat den Zugriff anfangs blockiert – bei Erreichbarkeitsproblemen zuerst hier schauen.
- **ufw** (auf dem VPS): `allow OpenSSH`, `allow 80/tcp`, `allow 443/tcp`, `allow from <Heim-IP> to any port 8800`. Nach Regel-Änderungen: `sudo ufw status` prüfen. Nicht vergessen: Vor `ufw enable` muss SSH erlaubt sein.
**Härtung (empfohlen):** Da OpenWebUI auf demselben VPS läuft, kann Port 8800 extern komplett zu bleiben. Dashboard dann per SSH-Tunnel: `ssh -L 8800:localhost:8800 root@<VPS_IP>` → lokal `http://localhost:8800/?token=<DASHBOARD_TOKEN>`.
 
### 5.2 Lokal (Windows, Entwicklung)
 
```powershell
cd C:\Users\roman\Claude\Projects\Anonymizer
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m spacy download de_core_news_md
python -m spacy download en_core_web_md
 
# Starten:
$env:DB_PATH=".\anonymizer.db"
uvicorn app.main:app --port 8800 --reload
```
 
## 6. OpenWebUI-Anbindung (so konfiguriert)
 
Admin Panel → Settings → Connections → OpenAI API:
 
| Feld | Wert |
|---|---|
| URL | `http://<VPS_IP>:8800/v1` |
| Authentifizierung | Bearer + OpenRouter-Key (`sk-or-...`) |
| Modell-IDs | leer = alle OpenRouter-Modelle |
 
Die Modell-Liste (`/v1/models`) wird transparent von OpenRouter durchgereicht. Wichtig: keine parallele Direkt-Verbindung zu `openrouter.ai` aktiv lassen, sonst laufen Anfragen am Anonymizer vorbei.
 
**Testcall (PowerShell):**
 
```powershell
curl.exe http://<VPS_IP>:8800/v1/chat/completions `
  -H "Authorization: Bearer sk-or-DEIN-KEY" `
  -H "Content-Type: application/json" `
  -d '{\"model\": \"openai/gpt-4o-mini\", \"messages\": [{\"role\": \"user\", \"content\": \"Schreib eine kurze Mail an Roman Brun (roman.brun@gmail.com) aus Basel.\"}]}'
```
 
Erwartung im Dashboard: PERSON, LOCATION, EMAIL_ADDRESS erkannt – "Schreib" (Imperativ) NICHT.
 
## 7. Konfiguration (.env)
 
| Variable | Bedeutung | Aktuell/Default |
|---|---|---|
| `OPENROUTER_BASE_URL` | Upstream-API | `https://openrouter.ai/api/v1` |
| `OPENROUTER_API_KEY` | Fallback-Key (nur für Clients ohne eigenen Header, z. B. curl ohne `-H`) | leer |
| `DASHBOARD_TOKEN` | Schützt Dashboard + API | `<DASHBOARD_TOKEN>` |
| `NER_ENGINE` | `gliner` (Kontextmodell, empfohlen) oder `spacy` | `gliner` |
| `GLINER_MODEL` | HuggingFace-Modell | `fastino/gliner2-privacy-filter-PII-multi` |
| `GLINER_THRESHOLD` | Mindest-Konfidenz für GLiNER-Treffer | `0.5` |
| `GLINER_LABELS` | Welche PII-Typen GLiNER sucht (42 verfügbar) | Auswahl von 16, s. `.env.example` |
| `GLINER_LABEL_THRESHOLDS` | Schwellen pro Label, z. B. `person:0.55` | leer |
| `DEFAULT_LANGUAGE` | Fallback der Spracherkennung | `de` |
| `SCORE_THRESHOLD` | Mindest-Konfidenz für PII-Treffer (0–1); höher = weniger, dafür sicherere Treffer | `0.4` |
| `ENTITIES` | Aktive Entitätstypen | PERSON, EMAIL_ADDRESS, PHONE_NUMBER, IBAN_CODE, CREDIT_CARD, IP_ADDRESS, LOCATION, AHV_NUMBER |
| `NER_REQUIRE_PROPN` | NER-Treffer brauchen ein Eigennamen-Token | `true` |
| `NER_IGNORE_WORDS` | Wörter, die nie anonymisiert werden (Notausgang für False Positives) | leer |
 
Nach Änderungen: `docker compose up -d` (bzw. `--build` bei Code-Änderungen).
 
## 8. Wartung & Betrieb
 
| Aufgabe | Befehl / Vorgehen |
|---|---|
| Code-Update einspielen | `cd ~/anonymizer && git pull && docker compose up -d --build` |
| Logs ansehen | `cd ~/anonymizer && docker compose logs -f anonymizer` |
| Healthcheck | `curl http://localhost:8800/health` |
| Request-Log leeren | Dashboard → "Log leeren" (die SQLite-DB enthält Original-Prompts im Klartext!) |
| Dependencies aktualisieren | Versionen in `requirements.txt` anheben → neu bauen |
| Qualität überwachen | Dashboard beobachten: rutscht PII durch → `SCORE_THRESHOLD` senken; zu viele Fehl-Treffer → Threshold erhöhen oder Wort in `NER_IGNORE_WORDS` |
 
### Hinweise zum GLiNER-Betrieb
 
- **Erster Start**: Das Modell (~800 MB) wird von HuggingFace geladen und im `/data`-Volume gecacht (`HF_HOME=/data/hf`) – einmalig einige Minuten Wartezeit, danach schnell
- **Ressourcen**: CPU-Inferenz, ca. 1.5–2 GB RAM zusätzlich; pro Message einige hundert ms
- **Tuning**: Zu viele Namens-False-Positives → `GLINER_LABEL_THRESHOLDS=person:0.6`; fehlende Typen (z. B. Organisationen) → Label in `GLINER_LABELS` ergänzen
- **Test mit echtem Modell**: `python tests/debug_gliner.py "Dein Text"` zeigt Rohtreffer und Endergebnis
## 9. Bekannte Grenzen
 
- NER ist statistisch: sehr seltene Namen können durchrutschen; Dashboard im Auge behalten
- Bilder in Vision-Requests werden nicht anonymisiert, nur Textteile
- Formt das LLM einen Platzhalter um (z. B. `[PERSON_1]s` mit Genitiv-s), wird nur das exakte Muster zurückersetzt
- Die De-Anonymisierung ist Best-Effort: Platzhalter, die das LLM erfindet (z. B. `[PERSON_99]`), bleiben stehen
## 10. Troubleshooting (aus der Praxis)
 
| Symptom | Ursache / Lösung |
|---|---|
| `{"error":{"message":"User not found.","code":401}}` | OpenRouter-Key ungültig → auf openrouter.ai/settings/keys prüfen/neu erstellen |
| Dashboard/Proxy von aussen nicht erreichbar, lokal ok | Contabo-Cloud-Firewall: Port 8800 freigeben (sitzt vor ufw!) |
| OpenWebUI (443) plötzlich nicht erreichbar | `ufw enable` ohne 80/443-Regel → `sudo ufw allow 80/tcp && sudo ufw allow 443/tcp` |
| `curl -I` liefert 405 | Normal: Dashboard-Route erlaubt nur GET, nicht HEAD → `curl` ohne `-I` nutzen |
| `docker compose logs` sagt "no configuration file" | Im falschen Verzeichnis → zuerst `cd ~/anonymizer` |
| Harmloses Wort wird anonymisiert | In `.env`: `NER_IGNORE_WORDS=Wort1,Wort2` → `docker compose up -d` |
| Browser lädt Seite nicht, curl geht | Browser erzwingt https → explizit `http://` eintippen |
