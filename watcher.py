#!/usr/bin/env python3
"""
click-tt Tournament Watcher
- Überwacht offizielle click-tt Turnier-Übersichten auf neue Turniere
- Überwacht Anmeldungen von Personen aus einer Watchlist
- Sendet Benachrichtigungen an einen Discord-Webhook
"""
import json
import os
import re
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote_plus
import urllib.request

import yaml

STATE_FILE = Path("state.json")
CONFIG_FILE = Path("config.yaml")


def http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        ct = resp.headers.get("Content-Type", "")
        charset = "utf-8"
        if "charset=" in ct:
            charset = ct.split("charset=")[-1].split(";")[0].strip()
        return raw.decode(charset, errors="replace")


def post_webhook(webhook_url: str, embeds: list = None, content: str = ""):
    if not webhook_url:
        print("WARN: kein Webhook konfiguriert, gebe Nachricht nur aus:")
        if content:
            print(content)
        if embeds:
            print(json.dumps(embeds, indent=2, ensure_ascii=False))
        return
    payload = {}
    if content:
        payload["content"] = content[:2000]
    if embeds:
        payload["embeds"] = embeds[:10]
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "clicktt-watcher (github.com/actions) Python-urllib/3.11",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status >= 300:
                body = resp.read().decode("utf-8", errors="replace")[:500]
                print(f"WARN: Webhook returned {resp.status}: {body}")
            else:
                print(f"INFO: Webhook OK ({resp.status})")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        print(f"WARN: Webhook fehlgeschlagen: HTTP {e.code}: {e.reason} — Body: {body}")
    except Exception as e:
        print(f"WARN: Webhook fehlgeschlagen: {type(e).__name__}: {e}")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"tournaments": {}, "watched": {}}


def save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"FEHLER: {CONFIG_FILE} nicht gefunden")
        sys.exit(1)
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}


def get_target_months(advance: int = 3) -> list:
    """Aktueller Monat plus N weitere. Rückgabe: [(YYYY, MM), ...]"""
    now = datetime.now()
    year = now.year
    months = []
    m = now.month
    for _ in range(1 + advance):
        months.append((year, m))
        m += 1
        if m > 12:
            m = 1
            year += 1
    return months


def strip_tags(html: str) -> str:
    """Entfernt HTML-Tags aus einem String, behält Text."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    # HTML-Entities dekodieren
    text = text.replace("&auml;", "ä").replace("&ouml;", "ö").replace("&uuml;", "ü")
    text = text.replace("&Auml;", "Ä").replace("&Ouml;", "Ö").replace("&Uuml;", "Ü")
    text = text.replace("&szlig;", "ß").replace("&amp;", "&").replace("&nbsp;", " ")
    return text.strip()


# ===== Übersichtsseite parsen =====

def parse_calendar(html: str, base_url: str) -> list:
    """
    Findet alle Turniere in einer click-tt Turnier-Übersicht.
    Rückgabe: Liste von {id, url, verein, date_str, region, ort, kapazitaet, warteliste, altersklasse}
    """
    tournaments = []
    seen = set()

    # Jede Turnier-Zeile enthält einen Link auf tournamentCalendarDetail mit tournament=<id>
    # Wir suchen alle solchen Links und parsen um jeden herum die Metadaten
    detail_link_pattern = re.compile(
        r'href="([^"]*tournamentCalendarDetail[^"]*tournament=(\d+)[^"]*)"',
        re.IGNORECASE
    )

    # Alle <tr>-Zeilen finden, die einen Turnier-Link enthalten
    # (die click-tt Tabelle hat pro Turnier eine <tr>)
    tr_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.IGNORECASE | re.DOTALL)

    for tr_match in tr_pattern.finditer(html):
        row_html = tr_match.group(1)
        link_match = detail_link_pattern.search(row_html)
        if not link_match:
            continue

        rel_url = link_match.group(1)
        tid = link_match.group(2)
        if tid in seen:
            continue
        seen.add(tid)

        # Absolute URL bauen
        if rel_url.startswith("http"):
            full_url = rel_url
        else:
            # Der href ist relativ zur click-tt Domain
            full_url = "https://ttvn.click-tt.de" + rel_url

        # HTML-Entities in URL fixen
        full_url = full_url.replace("&amp;", "&")

        # Einzelne <td> Zellen extrahieren
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.IGNORECASE | re.DOTALL)
        cell_texts = [strip_tags(c) for c in cells]

        # Struktur der Übersicht:
        # [0] Termin: "Sa. 01.08.2026 12:09 Uhr"
        # [1] Turnier: "TTVN-Race 2026 TTC Grün-Gelb Braunschweig" (Titel + Verein)
        # [2] frei: "3/13" oder "16/16"
        # [3] Warteliste: "2" oder "-"
        # [4] Ort (Region)
        # [5] offen für: "ITTF"
        # [6] Altersklasse: "Damen/Herren"
        # [7] Info: PDF-Link
        date_str = cell_texts[0] if len(cell_texts) > 0 else ""
        titel_full = cell_texts[1] if len(cell_texts) > 1 else ""
        kapazitaet = cell_texts[2] if len(cell_texts) > 2 else ""
        warteliste = cell_texts[3] if len(cell_texts) > 3 else ""
        region = cell_texts[4] if len(cell_texts) > 4 else ""
        altersklasse = cell_texts[6] if len(cell_texts) > 6 else ""

        # Titel splitten: "TTVN-Race 2026 TTC Grün-Gelb Braunschweig"
        # → Serien-Name und Verein trennen. Verein ist alles nach dem Serien-Namen.
        # Da wir den Serien-Namen kennen, ist das einfach: alles nach dem ersten Match rauswerfen.
        verein = titel_full
        for series_name in ["TTVN-Race 2026", "TTVN-Race 2025", "TTVN-Race 2027"]:
            if verein.startswith(series_name):
                verein = verein[len(series_name):].strip()
                break

        tournaments.append({
            "id": tid,
            "url": full_url,
            "verein": verein,
            "date": date_str,
            "region": region,
            "kapazitaet": kapazitaet,
            "warteliste": warteliste,
            "altersklasse": altersklasse,
        })

    return tournaments


# ===== Detailseite parsen: findet competition-URLs für Teilnehmerlisten =====

def parse_detail_competition_urls(html: str) -> list:
    """
    Aus der tournamentCalendarDetail-Seite die Links zu Teilnehmerlisten extrahieren.
    Ein Turnier kann mehrere Konkurrenzen haben (z.B. Damen/Herren + Jugend).
    """
    urls = []
    seen = set()
    # Links: /wa/tournamentPlayerList?...
    for m in re.finditer(
        r'href="([^"]*tournamentPlayerList[^"]*competition=(\d+)[^"]*)"',
        html,
        re.IGNORECASE
    ):
        rel_url = m.group(1)
        cid = m.group(2)
        if cid in seen:
            continue
        seen.add(cid)
        if rel_url.startswith("http"):
            full_url = rel_url
        else:
            full_url = "https://ttvn.click-tt.de" + rel_url
        full_url = full_url.replace("&amp;", "&")
        urls.append(full_url)
    return urls


# ===== Teilnehmerliste parsen =====

def parse_player_list(html: str) -> list:
    """
    Findet Teilnehmer-Namen auf einer tournamentPlayerList-Seite.
    Format in der Tabelle: "Klein, Thomas" (Nachname, Vorname)
    """
    names = []
    seen = set()

    # Wir suchen die Teilnehmer-Tabelle. Struktur:
    # <table><tr><th>Platzierung</th><th>Bilanz</th><th>Name</th><th>Verein</th>...</tr>
    #        <tr><td>1</td><td>6:0</td><td>Klein, Thomas</td>...</tr>
    #
    # Vereinfacht: alle <tr>-Zeilen mit mindestens 3 <td> holen,
    # die dritte Zelle ist der Name.

    tr_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.IGNORECASE | re.DOTALL)
    for tr_match in tr_pattern.finditer(html):
        row_html = tr_match.group(1)
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.IGNORECASE | re.DOTALL)
        if len(cells) < 3:
            continue
        name = strip_tags(cells[2])
        # Heuristik: Namen haben Komma und Leerzeichen, "Klein, Thomas"
        if "," not in name or len(name) < 4:
            continue
        # Ausschließen: Header oder komische Werte
        if name.lower() in ("name", "spieler"):
            continue
        if name in seen:
            continue
        seen.add(name)
        names.append(name)

    return names


def name_matches_watchlist(player_name: str, watch_name: str) -> bool:
    """
    Prüft, ob player_name (Format 'Nachname, Vorname') zum watch_name passt.
    Match sowohl bei "Timo Rischer" als auch bei "Rischer, Timo" als watch_name.
    Substring-basiert und case-insensitive.
    """
    player_lower = player_name.lower()
    watch_lower = watch_name.lower().strip()

    # Einfacher Substring-Match auf dem Original
    if watch_lower in player_lower:
        return True

    # Der User hat evtl. "Timo Rischer" (Vorname Nachname) eingegeben.
    # Die Seite gibt "Rischer, Timo". Also müssen wir umdrehen.
    # Einfache Regel: Wenn watch_name mehrere Wörter hat und KEIN Komma,
    # umdrehen und mit ", " verbinden.
    if " " in watch_lower and "," not in watch_lower:
        parts = watch_lower.split()
        # Alle Teile bis auf den letzten sind Vornamen, letzter ist Nachname.
        # Aber sicherheitshalber beide Richtungen ausprobieren.
        if len(parts) >= 2:
            reversed_1 = parts[-1] + ", " + " ".join(parts[:-1])  # "rischer, timo"
            if reversed_1 in player_lower:
                return True

    return False


# ===== Hauptlogik =====

def run():
    print(f"INFO: Watcher startet um {datetime.now().isoformat()}")
    config = load_config()
    state = load_state()
    print(f"INFO: State geladen: {len(state.get('tournaments', {}))} bekannte Turniere")

    webhook = os.environ.get("DISCORD_WEBHOOK") or config.get("webhook", "")
    if not webhook:
        print("WARN: Kein DISCORD_WEBHOOK (env) und kein webhook in config.yaml")

    # Watchlist
    watchlist = [n.strip() for n in (config.get("watchlist") or []) if n.strip()]
    print(f"INFO: Watchlist: {watchlist}")

    advance = int(config.get("months_advance", 3))
    federation = config.get("federation", "TTVN")
    circuit = config.get("circuit", "TTVN-Race 26")

    notifications = []
    all_current_tournaments = {}

    # 1. Übersichtsseiten pro Monat
    months = get_target_months(advance)
    print(f"INFO: Prüfe {len(months)} Monat(e): {months}")

    for (y, m) in months:
        # Erster Tag des Monats als "date"-Parameter
        date_param = f"{y:04d}-{m:02d}-01"
        url = (
            f"https://ttvn.click-tt.de/cgi-bin/WebObjects/nuLigaTTDE.woa/wa/tournamentCalendar"
            f"?circuit={quote_plus(circuit)}&federation={federation}&date={date_param}"
        )
        print(f"INFO: Lade {url}")
        try:
            html = http_get(url)
        except Exception as e:
            import traceback
            print(f"WARN: konnte {url} nicht laden: {type(e).__name__}: {e}")
            traceback.print_exc()
            continue

        tournaments = parse_calendar(html, url)
        print(f"INFO: → {len(tournaments)} Turniere gefunden (HTML {len(html)} Bytes)")
        if len(tournaments) == 0 and len(html) > 1000:
            print(f"DEBUG: erste 800 Zeichen: {html[:800]!r}")
        for t in tournaments:
            all_current_tournaments[t["id"]] = t

    print(f"INFO: Insgesamt {len(all_current_tournaments)} eindeutige Turniere")

    # 2. Vergleich mit State: neue Turniere?
    known_ids = set(state.get("tournaments", {}).keys())
    current_ids = set(all_current_tournaments.keys())
    new_ids = current_ids - known_ids
    removed_ids = known_ids - current_ids
    is_first_run = len(known_ids) == 0

    print(f"INFO: bekannt={len(known_ids)} aktuell={len(current_ids)} neu={len(new_ids)} entfernt={len(removed_ids)} first_run={is_first_run}")
    if new_ids:
        print(f"INFO: Neue Turnier-IDs: {sorted(new_ids)}")
    if removed_ids:
        print(f"INFO: Entfernte Turnier-IDs: {sorted(removed_ids)}")

    if new_ids and not is_first_run:
        for tid in sorted(new_ids):
            t = all_current_tournaments[tid]
            notifications.append({
                "type": "new_tournament",
                "embed": {
                    "title": f"🆕 Neues Turnier: {t.get('verein') or 'Unbekannt'}",
                    "description": (
                        f"**{t.get('date', '')}**\n"
                        f"📍 {t.get('region', '')}\n"
                        f"Kapazität: {t.get('kapazitaet', '')} · Warteliste: {t.get('warteliste', '-')}"
                    ),
                    "url": t["url"],
                    "color": 0x5a8a3a,
                }
            })

    # 3. Detailseiten & Teilnehmerlisten für Watchlist checken
    if watchlist:
        watched_state = state.get("watched", {})
        checked = 0
        errors = 0
        all_hits = 0

        for tid, t in sorted(all_current_tournaments.items()):
            # Detailseite laden, um competition-URLs zu bekommen
            try:
                detail_html = http_get(t["url"])
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f"WARN: detail {tid}: {type(e).__name__}: {e}")
                continue

            competition_urls = parse_detail_competition_urls(detail_html)
            if not competition_urls:
                # Kein Teilnehmerlisten-Link → überspringen (evtl. schon lange her, geschlossen etc.)
                continue

            all_participants = []
            for c_url in competition_urls:
                try:
                    plist_html = http_get(c_url)
                except Exception as e:
                    print(f"WARN: player list {c_url}: {e}")
                    continue
                all_participants.extend(parse_player_list(plist_html))

            # Watchlist-Match
            previous = set(watched_state.get(tid, {}).get("watched_present", []))
            currently_present = []
            for w_name in watchlist:
                for p_name in all_participants:
                    if name_matches_watchlist(p_name, w_name):
                        currently_present.append(p_name)
                        break

            currently_present_set = set(currently_present)
            new_present = currently_present_set - previous

            if currently_present_set:
                all_hits += len(currently_present_set)
                print(f"INFO: Turnier {tid} ({t.get('verein', '')}): {len(all_participants)} TN, Watchlist-Treffer: {sorted(currently_present_set)}")

            if new_present and not is_first_run:
                names_str = ", ".join(sorted(new_present))
                notifications.append({
                    "type": "watchlist_hit",
                    "embed": {
                        "title": f"👤 Anmeldung: {names_str}",
                        "description": (
                            f"**{t.get('date', '')}** · {t.get('verein', '')}\n"
                            f"📍 {t.get('region', '')}\n"
                            f"Aktuell {len(all_participants)} Teilnehmer"
                        ),
                        "url": t["url"],
                        "color": 0xf0a030,
                    }
                })

            watched_state[tid] = {
                "watched_present": sorted(currently_present_set),
                "all_participants_count": len(all_participants),
            }
            checked += 1

        print(f"INFO: Detailcheck: {checked} ok, {errors} fehlgeschlagen, {all_hits} Watchlist-Treffer")
        state["watched"] = {tid: w for tid, w in watched_state.items() if tid in all_current_tournaments}
    else:
        print("INFO: Watchlist leer – Detailseiten werden nicht geprüft.")

    # 4. State aktualisieren (nach dem Watchlist-Check, damit Metadaten frisch sind)
    state["tournaments"] = {
        tid: {
            "verein": t.get("verein", ""),
            "region": t.get("region", ""),
            "date": t.get("date", ""),
            "url": t.get("url", ""),
            "kapazitaet": t.get("kapazitaet", ""),
            "warteliste": t.get("warteliste", ""),
            "altersklasse": t.get("altersklasse", ""),
        }
        for tid, t in all_current_tournaments.items()
    }

    # 5. Benachrichtigungen abschicken
    if notifications:
        print(f"=> {len(notifications)} Benachrichtigungen")
        buffer = []
        for n in notifications:
            buffer.append(n["embed"])
            if len(buffer) >= 10:
                post_webhook(webhook, embeds=buffer)
                buffer = []
        if buffer:
            post_webhook(webhook, embeds=buffer)
    else:
        if is_first_run:
            print("=> Erster Lauf: State initialisiert, keine Benachrichtigungen")
        else:
            print("=> Keine Änderungen")

    save_state(state)


if __name__ == "__main__":
    run()
