#!/usr/bin/env python3
"""
click-tt Tournament Watcher
- Überwacht offizielle click-tt Turnier-Übersichten auf neue Turniere
- Überwacht Anmeldungen von Personen aus einer Watchlist (Haupt + Warteliste)
- Spezial-Logik für "self": checkt am Meldeschlusstag ab 12 Uhr stündlich,
  ob man von der Warteliste ins Hauptfeld gerutscht ist
- Sendet Benachrichtigungen an einen Discord-Webhook

Zwei Modi:
- Normal (default): alle 6h — neue Turniere, Watchlist-Treffer, Status-Tracking
- Waitlist (via arg oder WATCHER_MODE=waitlist): jede Stunde ab Meldeschluss-Tag
  12 Uhr bis Turniertag 12 Uhr, checkt nur Turniere mit self auf Warteliste
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
import urllib.request
import urllib.error

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


def post_webhook(webhook_url, embeds=None, content=""):
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


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"tournaments": {}, "watched": {}, "self_status": {}, "promoted_notified": []}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def load_config():
    if not CONFIG_FILE.exists():
        print(f"FEHLER: {CONFIG_FILE} nicht gefunden")
        sys.exit(1)
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}


def get_target_months(advance=3):
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


def strip_tags(html):
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    text = text.replace("&auml;", "ä").replace("&ouml;", "ö").replace("&uuml;", "ü")
    text = text.replace("&Auml;", "Ä").replace("&Ouml;", "Ö").replace("&Uuml;", "Ü")
    text = text.replace("&szlig;", "ß").replace("&amp;", "&").replace("&nbsp;", " ")
    return text.strip()


def parse_date_de(s):
    """Parsed 'DD.MM.YYYY HH:MM' oder 'DD.MM.YYYY' zu datetime, oder None."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_tournament_date(s):
    """Parsed 'Di. 20.10.2026 19:00 Uhr' zu datetime."""
    if not s:
        return None
    m = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s+(\d{1,2}):(\d{2}))?', s)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour = int(m.group(4)) if m.group(4) else 0
    minute = int(m.group(5)) if m.group(5) else 0
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


# ===== Übersichtsseite =====

def parse_calendar(html, base_url):
    tournaments = []
    seen = set()
    detail_link_pattern = re.compile(
        r'href="([^"]*tournamentCalendarDetail[^"]*tournament=(\d+)[^"]*)"',
        re.IGNORECASE
    )
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
        if rel_url.startswith("http"):
            full_url = rel_url
        else:
            full_url = "https://ttvn.click-tt.de" + rel_url
        full_url = full_url.replace("&amp;", "&")
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.IGNORECASE | re.DOTALL)
        cell_texts = [strip_tags(c) for c in cells]
        date_str = cell_texts[0] if len(cell_texts) > 0 else ""
        titel_full = cell_texts[1] if len(cell_texts) > 1 else ""
        kapazitaet = cell_texts[2] if len(cell_texts) > 2 else ""
        warteliste = cell_texts[3] if len(cell_texts) > 3 else ""
        region = cell_texts[4] if len(cell_texts) > 4 else ""
        altersklasse = cell_texts[6] if len(cell_texts) > 6 else ""
        verein = titel_full
        for series_name in ["TTVN-Race 2026", "TTVN-Race 2025", "TTVN-Race 2027"]:
            if verein.startswith(series_name):
                verein = verein[len(series_name):].strip()
                break
        tournaments.append({
            "id": tid, "url": full_url, "verein": verein, "date": date_str,
            "region": region, "kapazitaet": kapazitaet,
            "warteliste": warteliste, "altersklasse": altersklasse,
        })
    return tournaments


# ===== Detailseite =====

def parse_detail_competition_urls(html):
    urls = []
    seen = set()
    for m in re.finditer(
        r'href="([^"]*tournamentPlayerList[^"]*competition=(\d+)[^"]*)"',
        html, re.IGNORECASE
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


def parse_meldeschluss(html):
    """Findet den Meldeschluss. Format: '19.10.2026 23:59'"""
    plain = strip_tags(html)
    m = re.search(
        r'Meldeschluss:?\s*(\d{1,2}\.\d{1,2}\.\d{4}(?:\s+\d{1,2}:\d{2})?)',
        plain, re.IGNORECASE
    )
    if not m:
        return None
    return parse_date_de(m.group(1))


# ===== Teilnehmerliste mit Warteliste-Unterscheidung =====

def parse_player_list_with_status(html):
    """
    Rückgabe: {'main': [...], 'wait': [...]}
    Hauptliste vs. Warteliste wird über Überschriften VOR den Tabellen erkannt.
    """
    result = {"main": [], "wait": []}
    seen_main = set()
    seen_wait = set()
    tables = []
    for tm in re.finditer(r'<table[^>]*>(.*?)</table>', html, re.IGNORECASE | re.DOTALL):
        tables.append((tm.start(), tm.end(), tm.group(1)))
    for idx, (start, end, table_html) in enumerate(tables):
        prev_end = tables[idx - 1][1] if idx > 0 else max(0, start - 2000)
        context_before = html[prev_end:start].lower()
        if "warteliste" in context_before or "warte" in context_before:
            bucket = "wait"
        else:
            bucket = "main"
        names = _extract_names_from_table(table_html)
        if not names:
            continue
        target_set = seen_wait if bucket == "wait" else seen_main
        target_list = result["wait"] if bucket == "wait" else result["main"]
        for n in names:
            if n in target_set:
                continue
            target_set.add(n)
            target_list.append(n)
    return result


def _extract_names_from_table(table_html):
    names = []
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.IGNORECASE | re.DOTALL)
    if not rows:
        return names
    header_row = rows[0]
    header_cells_th = re.findall(r'<th[^>]*>(.*?)</th>', header_row, re.IGNORECASE | re.DOTALL)
    header_cells_td = re.findall(r'<td[^>]*>(.*?)</td>', header_row, re.IGNORECASE | re.DOTALL)
    header_cells = header_cells_th if header_cells_th else header_cells_td
    if not header_cells:
        return names
    header_texts = [strip_tags(c).lower() for c in header_cells]
    name_col_idx = None
    for i, h in enumerate(header_texts):
        if h == "name" or h == "spieler":
            name_col_idx = i
            break
    if name_col_idx is None:
        return names
    if not any("verein" in h for h in header_texts):
        return names
    data_rows = rows[1:] if header_cells_th else rows
    if header_cells_td and not header_cells_th and data_rows:
        first_cells = re.findall(r'<td[^>]*>(.*?)</td>', data_rows[0], re.IGNORECASE | re.DOTALL)
        first_texts = [strip_tags(c).lower() for c in first_cells]
        if "name" in first_texts or "spieler" in first_texts:
            data_rows = data_rows[1:]
    for row_html in data_rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.IGNORECASE | re.DOTALL)
        if len(cells) <= name_col_idx:
            continue
        name = strip_tags(cells[name_col_idx])
        if "," not in name or len(name) < 4:
            continue
        if name.lower() in ("name", "spieler"):
            continue
        names.append(name)
    return names


def name_matches_watchlist(player_name, watch_name):
    player_lower = player_name.lower()
    watch_lower = watch_name.lower().strip()
    if watch_lower in player_lower:
        return True
    if " " in watch_lower and "," not in watch_lower:
        parts = watch_lower.split()
        if len(parts) >= 2:
            reversed_1 = parts[-1] + ", " + " ".join(parts[:-1])
            if reversed_1 in player_lower:
                return True
    return False


def find_in_list(player_list, watch_name):
    for p in player_list:
        if name_matches_watchlist(p, watch_name):
            return p
    return None


# ===== Normal-Modus =====

def run_normal():
    print(f"INFO: Watcher (normal) startet um {datetime.now().isoformat()}")
    config = load_config()
    state = load_state()
    state.setdefault("tournaments", {})
    state.setdefault("watched", {})
    state.setdefault("self_status", {})
    state.setdefault("promoted_notified", [])
    print(f"INFO: State geladen: {len(state['tournaments'])} bekannte Turniere")

    webhook = os.environ.get("DISCORD_WEBHOOK") or config.get("webhook", "")
    if not webhook:
        print("WARN: Kein DISCORD_WEBHOOK konfiguriert")

    watchlist = [n.strip() for n in (config.get("watchlist") or []) if n.strip()]
    self_name = (config.get("self") or "").strip()
    print(f"INFO: Watchlist: {watchlist}")
    print(f"INFO: Self: {self_name!r}")

    advance = int(config.get("months_advance", 3))
    federation = config.get("federation", "TTVN")
    circuit = config.get("circuit", "TTVN-Race 26")

    notifications = []
    all_current_tournaments = {}
    months = get_target_months(advance)
    print(f"INFO: Prüfe {len(months)} Monat(e): {months}")

    for (y, m) in months:
        date_param = f"{y:04d}-{m:02d}-01"
        url = (
            f"https://ttvn.click-tt.de/cgi-bin/WebObjects/nuLigaTTDE.woa/wa/tournamentCalendar"
            f"?circuit={quote_plus(circuit)}&federation={federation}&date={date_param}"
        )
        print(f"INFO: Lade {url}")
        try:
            html = http_get(url)
        except Exception as e:
            print(f"WARN: konnte {url} nicht laden: {type(e).__name__}: {e}")
            continue
        tournaments = parse_calendar(html, url)
        print(f"INFO: → {len(tournaments)} Turniere gefunden")
        for t in tournaments:
            all_current_tournaments[t["id"]] = t

    print(f"INFO: Insgesamt {len(all_current_tournaments)} eindeutige Turniere")

    known_ids = set(state["tournaments"].keys())
    current_ids = set(all_current_tournaments.keys())
    new_ids = current_ids - known_ids
    is_first_run = len(known_ids) == 0
    print(f"INFO: bekannt={len(known_ids)} aktuell={len(current_ids)} neu={len(new_ids)} first_run={is_first_run}")

    if new_ids and not is_first_run:
        for tid in sorted(new_ids):
            t = all_current_tournaments[tid]
            notifications.append({
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

    if watchlist or self_name:
        watched_state = state["watched"]
        self_status = state["self_status"]
        promoted_notified = set(state["promoted_notified"])
        checked = 0
        errors = 0
        all_hits = 0

        for tid, t in sorted(all_current_tournaments.items()):
            try:
                detail_html = http_get(t["url"])
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f"WARN: detail {tid}: {type(e).__name__}: {e}")
                continue

            meldeschluss = parse_meldeschluss(detail_html)
            if meldeschluss:
                t["meldeschluss"] = meldeschluss.strftime("%Y-%m-%d %H:%M")

            competition_urls = parse_detail_competition_urls(detail_html)
            if not competition_urls:
                continue

            all_main = []
            all_wait = []
            for c_url in competition_urls:
                try:
                    plist_html = http_get(c_url)
                except Exception as e:
                    print(f"WARN: player list {c_url}: {e}")
                    continue
                plist = parse_player_list_with_status(plist_html)
                all_main.extend(plist["main"])
                all_wait.extend(plist["wait"])

            total_participants = len(all_main) + len(all_wait)
            all_angemeldete = all_main + all_wait

            previous = set(watched_state.get(tid, {}).get("watched_present", []))
            currently_present = []
            for w_name in watchlist:
                m = find_in_list(all_angemeldete, w_name)
                if m:
                    currently_present.append(m)

            currently_present_set = set(currently_present)
            new_present = currently_present_set - previous

            if currently_present_set:
                all_hits += len(currently_present_set)
                print(f"INFO: Turnier {tid} ({t.get('verein', '')}): main={len(all_main)} wait={len(all_wait)}, Treffer: {sorted(currently_present_set)}")

            if new_present and not is_first_run:
                names_str = ", ".join(sorted(new_present))
                notifications.append({
                    "embed": {
                        "title": f"👤 Anmeldung: {names_str}",
                        "description": (
                            f"**{t.get('date', '')}** · {t.get('verein', '')}\n"
                            f"📍 {t.get('region', '')}\n"
                            f"Teilnehmer: {len(all_main)} · Warteliste: {len(all_wait)}"
                        ),
                        "url": t["url"],
                        "color": 0xf0a030,
                    }
                })

            watched_state[tid] = {
                "watched_present": sorted(currently_present_set),
                "all_participants_count": total_participants,
            }

            if self_name:
                in_main = find_in_list(all_main, self_name)
                in_wait = find_in_list(all_wait, self_name)
                if in_main:
                    status = "main"
                elif in_wait:
                    status = "wait"
                else:
                    status = "none"
                prev_status = self_status.get(tid, {}).get("status", "none")

                if prev_status == "wait" and status == "main" and tid not in promoted_notified and not is_first_run:
                    notifications.append({
                        "embed": {
                            "title": f"🎉 Nachgerückt! {t.get('verein', '')}",
                            "description": (
                                f"**{t.get('date', '')}**\n"
                                f"📍 {t.get('region', '')}\n"
                                f"Du bist von der Warteliste ins Hauptfeld gerutscht."
                            ),
                            "url": t["url"],
                            "color": 0x4a90e2,
                        }
                    })
                    promoted_notified.add(tid)

                if status == "none" and tid in promoted_notified:
                    promoted_notified.discard(tid)

                self_status[tid] = {"status": status}
                if status != "none":
                    print(f"INFO: Self-Status Turnier {tid}: {status}")

            checked += 1

        print(f"INFO: Detailcheck: {checked} ok, {errors} fehlgeschlagen, {all_hits} Watchlist-Treffer")
        state["watched"] = {tid: w for tid, w in watched_state.items() if tid in all_current_tournaments}
        state["self_status"] = {tid: s for tid, s in self_status.items() if tid in all_current_tournaments}
        state["promoted_notified"] = sorted(promoted_notified & current_ids)
    else:
        print("INFO: Watchlist & Self leer – Detailseiten werden nicht geprüft.")

    new_tournaments_state = {}
    for tid, t in all_current_tournaments.items():
        entry = {
            "verein": t.get("verein", ""),
            "region": t.get("region", ""),
            "date": t.get("date", ""),
            "url": t.get("url", ""),
            "kapazitaet": t.get("kapazitaet", ""),
            "warteliste": t.get("warteliste", ""),
            "altersklasse": t.get("altersklasse", ""),
        }
        if "meldeschluss" in t:
            entry["meldeschluss"] = t["meldeschluss"]
        elif tid in state["tournaments"] and "meldeschluss" in state["tournaments"][tid]:
            entry["meldeschluss"] = state["tournaments"][tid]["meldeschluss"]
        new_tournaments_state[tid] = entry
    state["tournaments"] = new_tournaments_state

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


# ===== Warteliste-Modus =====

def run_waitlist():
    """Checkt nur Turniere, bei denen self auf Warteliste UND wir im Zeitfenster sind."""
    print(f"INFO: Watcher (waitlist) startet um {datetime.now().isoformat()}")
    config = load_config()
    state = load_state()
    state.setdefault("tournaments", {})
    state.setdefault("self_status", {})
    state.setdefault("promoted_notified", [])

    webhook = os.environ.get("DISCORD_WEBHOOK") or config.get("webhook", "")
    self_name = (config.get("self") or "").strip()
    if not self_name:
        print("INFO: Kein 'self' konfiguriert – nichts zu tun.")
        return

    now = datetime.now()
    promoted_notified = set(state["promoted_notified"])

    candidates = []
    for tid, info in state["tournaments"].items():
        self_s = state["self_status"].get(tid, {}).get("status")
        if self_s != "wait":
            continue
        meldeschluss_str = info.get("meldeschluss")
        if not meldeschluss_str:
            print(f"INFO: Turnier {tid}: self auf Warteliste, aber kein Meldeschluss im State → überspringe")
            continue
        meldeschluss = parse_date_de(meldeschluss_str)
        turnier_dt = parse_tournament_date(info.get("date", ""))
        if not meldeschluss or not turnier_dt:
            continue
        window_start = meldeschluss.replace(hour=12, minute=0, second=0, microsecond=0)
        window_end = turnier_dt.replace(hour=12, minute=0, second=0, microsecond=0)
        if window_start <= now <= window_end:
            candidates.append((tid, info))

    print(f"INFO: {len(candidates)} Turnier(e) im Warteliste-Prüfzeitfenster")

    notifications = []
    for tid, info in candidates:
        if tid in promoted_notified:
            print(f"INFO: Turnier {tid}: schon als nachgerückt gemeldet, überspringe")
            continue
        print(f"INFO: Prüfe Turnier {tid} ({info.get('verein', '')})")
        try:
            detail_html = http_get(info["url"])
        except Exception as e:
            print(f"WARN: detail {tid}: {type(e).__name__}: {e}")
            continue

        competition_urls = parse_detail_competition_urls(detail_html)
        all_main = []
        all_wait = []
        for c_url in competition_urls:
            try:
                plist_html = http_get(c_url)
            except Exception as e:
                print(f"WARN: player list {c_url}: {e}")
                continue
            plist = parse_player_list_with_status(plist_html)
            all_main.extend(plist["main"])
            all_wait.extend(plist["wait"])

        in_main = find_in_list(all_main, self_name)
        in_wait = find_in_list(all_wait, self_name)

        if in_main:
            print(f"INFO: 🎉 Turnier {tid}: nachgerückt!")
            notifications.append({
                "embed": {
                    "title": f"🎉 Nachgerückt! {info.get('verein', '')}",
                    "description": (
                        f"**{info.get('date', '')}**\n"
                        f"📍 {info.get('region', '')}\n"
                        f"Du bist von der Warteliste ins Hauptfeld gerutscht."
                    ),
                    "url": info["url"],
                    "color": 0x4a90e2,
                }
            })
            promoted_notified.add(tid)
            state["self_status"][tid] = {"status": "main"}
        elif in_wait:
            print(f"INFO: Turnier {tid}: noch auf Warteliste")
        else:
            print(f"INFO: Turnier {tid}: nicht mehr angemeldet")
            state["self_status"][tid] = {"status": "none"}

    state["promoted_notified"] = sorted(promoted_notified)

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
        print("=> Keine Änderungen")

    save_state(state)


if __name__ == "__main__":
    mode = os.environ.get("WATCHER_MODE", "normal").strip().lower()
    if len(sys.argv) > 1:
        mode = sys.argv[1].strip().lower()
    if mode == "waitlist":
        run_waitlist()
    else:
        run_normal()
