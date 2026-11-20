# Anleitung für Studierende: Eigene Guardrail bauen

In dieser Übung baust du eine **Guardrail**: eine Schutzschicht zwischen dir und einem Large Language Model (LLM). Sie entscheidet, welche Daten das LLM zu sehen bekommt. Personendaten werden maskiert, entfernt oder die Anfrage wird ganz blockiert.

```
Jan (dein Chat-Programm)
   │  deine Eingabe im Klartext
   ▼
Anonymizer mit DEINER Guardrail   ◄── hier stellst du die Regeln ein
   │  nur noch Platzhalter wie [PERSON_1]
   ▼
LLM (z. B. azure_ai/gpt-oss-120b)
   │  Antwort mit Platzhaltern
   ▼
Anonymizer setzt die Originalwerte wieder ein  ──►  Jan
```

Du siehst bei jeder Anfrage im Web-UI, was du eingegeben hast und was beim LLM tatsächlich angekommen ist.

> **Wichtig:** Verwende ausschliesslich **erfundene Testdaten** (Namen, Telefonnummern, IBAN usw.). Deine Eingaben werden zur Auswertung im Klartext gespeichert und nach 14 Tagen gelöscht. Die Lehrperson kann alle Anfragen einsehen.

---

## 1. Zugang erstellen

1. Öffne **https://<ADRESSE>** (immer mit `https://`).
2. Unter «Neu hier? Zugang erstellen» deinen Namen und den **Kurs-Code** eingeben. Den Code erhältst du im Unterricht.
3. Du erhältst einen persönlichen Key, der mit `ano_` beginnt.
   **Er wird nur ein einziges Mal angezeigt.** Kopiere ihn sofort und bewahre ihn sicher auf (z. B. im Passwort-Manager).
4. Mit «Weiter» bist du angemeldet. Später meldest du dich mit diesem Key an.

Key verloren? Die Lehrperson kann einen neuen erzeugen. Deine Guardrail bleibt dabei erhalten.

## 2. Jan einrichten

Jan ist ein Chat-Programm für deinen Rechner: https://jan.ai

1. Jan installieren und öffnen.
2. **Settings → Model Providers** → einen neuen **OpenAI-kompatiblen Provider** hinzufügen, z. B. mit dem Namen «Guardrail».
3. Eintragen:

   | Feld | Wert |
   |---|---|
   | Base URL | `https://<ADRESSE>/v1` |
   | API Key | dein `ano_…`-Key |

4. Bei **Models** mit «+» das Modell von Hand eintragen: `azure_ai/gpt-oss-120b` (oder das Modell, das im Web-UI unter «Einrichtung (Jan)» steht).
5. Neuen Chat starten und oben **das Modell dieses Providers** auswählen.
6. Schreib «Hallo». Kommt eine Antwort, funktioniert alles.

Im Web-UI unter **Einrichtung (Jan)** findest du die gleichen Angaben zum Kopieren.

## 3. Die Guardrail einstellen

Im Web-UI unter **Guardrail**. Änderungen gelten erst nach **«Guardrail speichern»**.

### 3.1 Eingebaute Erkennung

Häkchen setzen für die Arten von Personendaten, die maskiert werden sollen (E-Mail, Telefonnummer, IBAN, AHV-Nummer usw.). Ausgegraute Typen sind auf diesem Server nicht verfügbar.

Der **Mindest-Score** bestimmt, wie sicher sich die Erkennung sein muss:

- **tief** (z. B. 0,3): Es wird mehr erkannt, aber es gibt auch Fehlalarme.
- **hoch** (z. B. 0,8): Weniger Fehlalarme, dafür rutschen echte Daten durch.

Beispiel: Eine Telefonnummer ohne Zusatz erhält nur einen Score von 0,4. Bei einer Schwelle von 0,5 wird sie **nicht** maskiert.

### 3.2 Eigene Regeln

Eine eigene Regel besteht aus drei Teilen:

| Teil | Bedeutung | Beispiel |
|---|---|---|
| Name | Grossbuchstaben und `_`, wird zum Platzhalter | `KUNDENNUMMER` → `[KUNDENNUMMER_1]` |
| Muster | regulärer Ausdruck (Regex) | `KD-\d{6}` |
| Aktion | was bei einem Treffer passiert | siehe unten |

Die drei Aktionen:

| Aktion | Was das LLM sieht | Antwort in Jan |
|---|---|---|
| **maskieren** | `[KUNDENNUMMER_1]` | Platzhalter wird wieder durch den Originalwert ersetzt |
| **entfernen** | einen festen Text, z. B. `[ENTFERNT]` | bleibt `[ENTFERNT]`, das Original ist weg |
| **blockieren** | gar nichts, die Anfrage wird abgelehnt | Jan zeigt deine Block-Meldung |

Optionen pro Regel:

- **aktiv**: Regel ein- oder ausschalten, ohne sie zu löschen.
- **Gross-/Kleinschreibung ignorieren**: `passwort` trifft dann auch `Passwort` und `PASSWORT`.

### 3.3 Ausnahmen und Block-Meldung

- **Wörter, die nie maskiert werden**: z. B. Firmennamen, die fälschlich als Person erkannt werden.
- **Meldung bei Blockierung**: dieser Text erscheint in Jan, wenn eine Block-Regel greift.

### 3.4 Testfeld

Rechts kannst du einen Text eingeben und siehst sofort, was das LLM daraus erhalten würde, **ohne** das LLM aufzurufen. Es gilt immer der aktuelle Stand im Editor, auch wenn er noch nicht gespeichert ist.

## 4. Ergebnis prüfen

Unter **Meine Requests** siehst du jede Anfrage aus Jan:

- **Erkannt**: welche Daten mit welcher Regel ersetzt wurden
- **Eingang** (links) und **Ausgang an das LLM** (rechts)
- **Antwort** vom LLM mit Platzhaltern und die zurückübersetzte Antwort, die Jan anzeigt
- Status **blocked**: Die Anfrage hat das LLM nie erreicht.

Du siehst nur deine eigenen Anfragen.

## 5. Regex-Spickzettel

| Muster | Bedeutung | Beispiel trifft |
|---|---|---|
| `\d` | eine Ziffer | `7` |
| `\d{6}` | genau 6 Ziffern | `123456` |
| `\d{2,4}` | 2 bis 4 Ziffern | `12`, `1234` |
| `[A-Z]` | ein Grossbuchstabe | `K` |
| `\s` | ein Leerzeichen | ` ` |
| `\s?` | optionales Leerzeichen | ` ` oder nichts |
| `[\s./-]?` | optional Leerzeichen, Punkt, Schrägstrich oder Bindestrich | `062/123.45.67` |
| `a\|b` | a oder b | `Tel` oder `Telefon` |
| `(?:…)` | Gruppe, z. B. für `\|` | `(?:Herr\|Frau)\s[A-Z]\w+` |
| `\b` | Wortgrenze | `\bPIN\b` trifft «PIN», nicht «PINGU» |
| `(?<!\d)` / `(?!\d)` | davor / danach keine Ziffer | verhindert Treffer mitten in längeren Zahlen |

Fertige Beispiele zum Ausprobieren:

| Name | Muster | Aktion |
|---|---|---|
| `KUNDENNUMMER` | `KD-\d{6}` | maskieren |
| `TELEFON_CH` | `(?<!\d)(?:\+41\s?(?:\(0\)\s?\|0)?\d{2}\|0\d{2})[\s./-]?\d{3}[\s./-]?\d{2}[\s./-]?\d{2}(?!\d)` | maskieren |
| `PROJEKTNAME` | `Projekt\s+Falke` (Gross-/Kleinschreibung ignorieren) | entfernen |
| `PASSWORT` | `passwort\s*[:=]` (Gross-/Kleinschreibung ignorieren) | blockieren |

Regeln werden beim Speichern geprüft: Ein Muster, das auf einen leeren Text passt (z. B. `a*`), wird abgelehnt. Braucht eine Regel zu lange, wird die Anfrage aus Sicherheitsgründen **blockiert**.

## 6. Übungsaufgaben

1. **Ausgangslage:** Schicke ohne eigene Regeln diesen Text über Jan und vergleiche unter «Meine Requests» Eingang und Ausgang:
   > Hallo, ich bin Anna Muster, erreichbar unter anna.muster@example.ch oder 079 123 45 67. Meine Kundennummer ist KD-123456, IBAN CH93 0076 2011 6238 5295 7.

   Welche Daten kommen beim LLM noch an?
2. **Schwelle:** Verschiebe den Mindest-Score zwischen 0,3 und 0,8 und beobachte im Testfeld die Telefonnummer. Erkläre das Ergebnis.
3. **Eigene Regel:** Schreib eine Regel, die die Kundennummer maskiert. Prüfe, ob die Antwort in Jan die echte Kundennummer enthält.
4. **Blockieren:** Baue eine Regel, die Anfragen mit Passwörtern blockiert. Teste sie mit «Mein Passwort: 1234» und «mein passwort=1234». Was ändert die Option «Gross-/Kleinschreibung ignorieren»?
5. **Umgehen:** Versuche, deine eigene Block-Regel zu umgehen (z. B. «P a s s w o r t», «Kennwort», «pw:»). Verbessere die Regel. Wo stösst ein Regex an Grenzen?
6. **Abwägung:** Schreib eine Regel, die zu viel trifft (z. B. alle Zahlen mit 6 Ziffern). Welche harmlosen Inhalte werden dadurch zerstört, und was heisst das für die Qualität der LLM-Antworten?
7. **Reflexion:** Diese Guardrail läuft auf einem Server zwischen dir und dem LLM. Was passiert, wenn du in Jan stattdessen direkt auf das LLM zugreifst? Wer muss eine Guardrail kontrollieren, damit sie wirksam ist?

## 7. Probleme und Lösungen

| Meldung oder Verhalten | Ursache | Lösung |
|---|---|---|
| «Ungültiger oder fehlender API-Key» / 401 | falscher Key in Jan | in Jan den `ano_…`-Key prüfen, nicht einen anderen Key |
| «Couldn't reach the provider» | Base URL falsch | `https://<ADRESSE>/v1` (mit `https` und `/v1`) |
| «Zu viele Anfragen» / 429 | mehr als 30 Anfragen pro Minute | kurz warten |
| Jan zeigt deine Block-Meldung | eine Block-Regel hat getroffen | gewollt; unter «Meine Requests» steht, welche Regel |
| Regel greift nicht | nicht gespeichert, Regel inaktiv, Gross-/Kleinschreibung | speichern, «aktiv» prüfen, im Testfeld ausprobieren |
| Modellliste in Jan bleibt leer | Laden der Modelle dauert lange | Modell von Hand eintragen |
| Key verloren | | Lehrperson um einen neuen Key bitten |
