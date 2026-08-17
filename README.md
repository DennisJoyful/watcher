# click-tt Tournament Watcher

Überwacht den offiziellen click-tt Turnierkalender (TTVN-Race) auf:
- **Neue Turniere** in den nächsten Monaten
- **Anmeldungen von Personen** aus einer Watchlist

Läuft komplett kostenlos auf GitHub Actions, alle 6 Stunden. Ersetzt die frühere racebuddy-Version durch die offizielle Quelle.

## Setup (einmalig)

### 1. Repo & Dateien

Dateien in ein **privates** GitHub-Repo hochladen. Struktur:

```
.github/workflows/watch.yml
watcher.py
config.yaml
README.md
```

### 2. Discord-Webhook

Server-Einstellungen → Integrationen → Webhooks → Neuer Webhook → URL kopieren.

### 3. GitHub-Secret

Repo → Settings → Secrets and variables → Actions → **New repository secret**
- Name: `DISCORD_WEBHOOK`
- Wert: die kopierte URL

### 4. config.yaml anpassen

Watchlist eintragen (Format egal, siehe Kommentare in der Datei).

### 5. Actions-Rechte aktivieren

Settings → Actions → General → "Workflow permissions" → **Read and write permissions** → Save

### 6. Ersten Lauf starten

Actions → "click-tt Watch" → "Run workflow"

Beim allerersten Lauf gibt es **keine Benachrichtigungen** – nur Baseline-Aufbau. Danach läuft's alle 6h automatisch.

## Konfiguration

- **`webhook`**: alternativ zum GitHub-Secret hier eintragen (Secret gewinnt)
- **`federation`**: TTVN (Niedersachsen). Für andere Verbände die Domain der URL im `watcher.py` anpassen
- **`circuit`**: z.B. "TTVN-Race 26". Kann in click-tt geändert werden
- **`months_advance`**: 3 = aktueller + 3 weitere Monate
- **`watchlist`**: Liste von Namen (Substring-Match, case-insensitive, Reihenfolge Vor-/Nachname egal)

## Wie Anmeldungen erkannt werden

click-tt zeigt Teilnehmer auf separaten "tournamentPlayerList"-Seiten (verlinkt von der Turnier-Detailseite). Das Skript:

1. Holt die Turnier-Übersicht des Monats
2. Für jedes Turnier: holt die Detailseite und die verlinkten Teilnehmerlisten
3. Vergleicht die Teilnehmer-Namen mit der Watchlist (in beiden Reihenfolgen)
4. Meldet Neuzugänge im Vergleich zum letzten Lauf

Namen auf click-tt sind im Format "Nachname, Vorname" – das Skript berücksichtigt auch die umgekehrte Eingabe ("Vorname Nachname" in der config).

## Fehlerdiagnose

Actions-Tab → letzten Lauf → "Run watcher" → Log:

- `INFO: → N Turniere gefunden`: Übersicht wurde geparst
- `INFO: Turnier XYZ (Verein): 8 TN, Watchlist-Treffer: [...]`: Watchlist wurde geprüft
- `INFO: Neue Turnier-IDs: [...]`: neue Turniere seit letztem Lauf

Bei 0 Turnieren oder Errors: state.json löschen und neu starten.
