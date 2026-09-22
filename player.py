import os
import sys
import asyncio
import re
import time
import traceback
import threading
import unicodedata
import uuid
from datetime import datetime as dt, timedelta

import discord
import gspread
from gspread.exceptions import WorksheetNotFound
import pytz
from discord import app_commands
from discord.ext import commands
from google.oauth2.service_account import Credentials
from sheets_connection import get_season_spreadsheet, get_season_worksheet

from sheet_guard import (
    col_values_cached,
    get_all_values_cached,
    row_values_cached,
    sheet_write_call,
)

import signup
import asnyc
import restinfo
import coop
import term_offers

from plan import PlanMenuView
from asyncplan import open_async_request_from_player
import matchcenter
from matchcenter import (
    LeagueResultViewStep1,
    LeagueResultViewStep2,
    CupResultView,
    MatchCenterState,
    get_runner_modes,
    write_league_result,
    league_result_post_text,
    send_result_post,
    now_berlin_str,
    result_league_from_value,
)

GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))

CURRENT_SEASON_LABEL = os.getenv("TFL_SEASON_LABEL", "Saison #6")
TFL_SEASON_START = os.getenv("TFL_SEASON_START", "2026-10-05").strip()
TFL_SEASON_END = os.getenv("TFL_SEASON_END", "2027-01-31").strip()
TFL_COLOR = 0x1F6FEB
TFL_SUCCESS_COLOR = 0x2ECC71
TFL_DANGER_COLOR = 0xE74C3C

COOP_LEAGUE_SHEET = os.getenv("TFL_COOP_LEAGUE_SHEET", "Coop Liga").strip() or "Coop Liga"
ACHIEVEMENT_SHEET = os.getenv("TFL_ACHIEVEMENT_SHEET", "Achievements").strip() or "Achievements"

DASHBOARD_BANNER_FILENAME = "tfl_dashboard_banner.png"
TFL_DASHBOARD_BANNER_URL = os.getenv("TFL_DASHBOARD_BANNER_URL", "").strip()
TFL_DASHBOARD_BANNER_PATH = os.getenv(
    "TFL_DASHBOARD_BANNER_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), DASHBOARD_BANNER_FILENAME),
).strip()

# Discord Components V2 (discord.py >= 2.6) ermöglicht Container, Trenner,
# Textblöcke und Buttons direkt zwischen Dashboard-Abschnitten.
HAS_COMPONENTS_V2 = all(
    hasattr(discord.ui, name)
    for name in (
        "LayoutView",
        "Container",
        "TextDisplay",
        "Separator",
        "Section",
        "ActionRow",
        "MediaGallery",
    )
)
COMPONENTS_V2_FLAG = 1 << 15

# =========================================================
# STREICHMODUS CONFIG
# =========================================================

DIVISION_CHANNELS = {
    1: 1344118033920168047,
    2: 1344118383859204146,
    3: 1344118470102614036,
    4: 1344118574943572100,
    5: 1389541046874148924,
    6: 1438136009085817023,
}

# Spalte O in den Divisionstabellen 1.DIV bis 6.DIV
# leer = Änderung noch möglich
# "1" = einmalige Änderung bereits genutzt
STREICH_CHANGE_USED_COL = 15

# Config-Sheet:
# K = Division 1
# L = Division 2
# M = Division 3
# N = Division 4
# O = Division 5
# P = Division 6
STREICHMODUS_CONFIG_WORKSHEET_GID = 463142264
STREICHMODUS_MODE_COLUMNS = {
    1: 11,  # K
    2: 12,  # L
    3: 13,  # M
    4: 14,  # N
    5: 15,  # O
    6: 16,  # P
}

PLAYER_PERFORMANCE_VERSION = "player-performance-v14-dashboard-ux"
print(f"[PLAYER] geladen: {PLAYER_PERFORMANCE_VERSION}")

PLAYER_SHEET_CACHE_TTL_SECONDS = int(os.getenv("PLAYER_SHEET_CACHE_TTL_SECONDS", "120"))
PLAYER_MODE_CACHE_TTL_SECONDS = int(os.getenv("PLAYER_MODE_CACHE_TTL_SECONDS", "300"))
PLAYER_DASHBOARD_IO_TIMEOUT_SECONDS = int(os.getenv("PLAYER_DASHBOARD_IO_TIMEOUT_SECONDS", "15"))

BERLIN_TZ = pytz.timezone("Europe/Berlin")
EXIT_REQUEST_ADMIN_CHANNEL_ID = 1277927528706736162
EXIT_REQUEST_SHEET = "AustrittAnfragen"
EXIT_REQUEST_TIMEOUT_DAYS = 5
EXIT_REQUEST_CHECK_INTERVAL_SECONDS = 3600

PLAYER_DIRECT_SPREADSHEET_ID = "1pZxg1_DUtbO4dZvX95ZrIqEZnkMc1MjmE7z5SEsMHQU"
PLAYER_DIRECT_CREDS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
_PLAYER_DIRECT_GC = None
_PLAYER_DIRECT_WB = None

_PLAYER_WORKSHEET_CACHE_BY_NAME = {}
_PLAYER_WORKSHEET_CACHE_BY_GID = {}


# =========================================================
# UI HELFER
# =========================================================

def menu_embed(
    title: str,
    description: str,
    color: int = TFL_COLOR,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
    )
    embed.set_footer(text=f"Try Force League · {CURRENT_SEASON_LABEL}")
    return embed


def _clean_sheet_datetime_text(value: str) -> str:
    value = (value or "").strip()
    value = value.replace(" Uhr", "")
    value = value.replace("–", "-").replace("—", "-")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"(?<=\d)\s*-\s*(?=\d{1,2}:\d{2})", " ", value)
    value = value.replace(",", " ")
    return re.sub(r"\s+", " ", value).strip()


def parse_sheet_datetime(value: str):
    raw = _clean_sheet_datetime_text(value)
    if not raw:
        return None

    formats = (
        "%d.%m.%Y %H:%M",
        "%d.%m.%y %H:%M",
        "%d.%m.%Y",
        "%d.%m.%y",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    )

    for fmt in formats:
        try:
            parsed = dt.strptime(raw, fmt)
            if "%H" not in fmt:
                parsed = parsed.replace(hour=12, minute=0)
            return BERLIN_TZ.localize(parsed)
        except ValueError:
            continue

    return None


def _is_open_result_value(result: str) -> bool:
    result_normalized = (result or "").strip().lower()
    return result_normalized in {"", "vs", "v.s.", "-", "–", "—"}


def _parse_match_score(result: str):
    match = re.match(r"^\s*(\d+)\s*:\s*(\d+)\s*$", (result or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _safe_row_cell(row, idx0: int) -> str:
    return row[idx0].strip() if 0 <= idx0 < len(row) else ""


def _player_outcome(home: str, away: str, score: tuple[int, int], player_name: str) -> str | None:
    target = normalize_name(player_name)
    home_score, away_score = score
    if normalize_name(home) == target:
        if home_score > away_score:
            return "W"
        if home_score < away_score:
            return "L"
        return "D"
    if normalize_name(away) == target:
        if away_score > home_score:
            return "W"
        if away_score < home_score:
            return "L"
        return "D"
    return None


def _current_win_streak(rows: list[list[str]], player_name: str) -> int:
    games = []
    for row_index, row in enumerate(rows[1:], start=2):
        home = _safe_row_cell(row, 3)
        result = _safe_row_cell(row, 4)
        away = _safe_row_cell(row, 5)
        if not home or not away or _is_open_result_value(result):
            continue
        score = _parse_match_score(result)
        if score is None:
            continue
        outcome = _player_outcome(home, away, score, player_name)
        if outcome is None:
            continue
        parsed = parse_sheet_datetime(_safe_row_cell(row, 1))
        sort_value = parsed.timestamp() if parsed is not None else float(row_index)
        games.append((sort_value, row_index, outcome))
    games.sort(key=lambda item: (item[0], item[1]))
    streak = 0
    for _, _, outcome in reversed(games):
        if outcome != "W":
            break
        streak += 1
    return streak


def _achievement_sheet_invalidation(ws) -> list[str]:
    name = player_sheet_name(ws, ACHIEVEMENT_SHEET)
    return [f"records:{name}", f"values:{name}", f"row:{name}:", f"col:{name}:", f"cell:{name}:"]


_ACHIEVEMENT_LOCK = threading.Lock()
_ACHIEVEMENT_STREAK_THRESHOLDS = (3, 5, 10)


def _ensure_achievement_sheet():
    if ACHIEVEMENT_SHEET in _PLAYER_WORKSHEET_CACHE_BY_NAME:
        return _PLAYER_WORKSHEET_CACHE_BY_NAME[ACHIEVEMENT_SHEET]
    wb = get_season_spreadsheet()
    try:
        ws = wb.worksheet(ACHIEVEMENT_SHEET)
    except WorksheetNotFound:
        ws = wb.add_worksheet(title=ACHIEVEMENT_SHEET, rows=1000, cols=7)
        ws.update("A1:G1", [["Zeitpunkt", "Spieler", "Achievement", "Bezeichnung", "Division", "Quelle", "Saison"]])
    _PLAYER_WORKSHEET_CACHE_BY_NAME[ACHIEVEMENT_SHEET] = ws
    return ws


def _achievement_key_is_current(key: str) -> bool:
    if key in {
        "season_first_finisher",
        "season_first_scheduled_match",
        "season_opening_match",
        "win_streak_3",
        "win_streak_5",
        "win_streak_10",
    }:
        return True
    return bool(re.fullmatch(r"division_(?:first_finisher|opening_match)_d[1-6]", key or ""))


def _read_current_season_achievement_rows(force_refresh: bool = False):
    ws = _ensure_achievement_sheet()
    rows = get_all_values_cached(
        lambda: ws,
        sheet_name=player_sheet_name(ws, ACHIEVEMENT_SHEET),
        ttl_seconds=30,
        force_refresh=force_refresh,
    )
    current = [
        row for row in rows[1:]
        if _safe_row_cell(row, 6) == CURRENT_SEASON_LABEL
        and _achievement_key_is_current(_safe_row_cell(row, 2))
    ]
    return ws, current


def _load_awarded_achievements_for_player(player_name: str) -> list[dict]:
    try:
        _, rows = _read_current_season_achievement_rows(force_refresh=False)
        target = normalize_name(player_name)
        achievements = []
        seen = set()
        for row in rows:
            if normalize_name(_safe_row_cell(row, 1)) != target:
                continue
            key = _safe_row_cell(row, 2)
            if key in seen:
                continue
            seen.add(key)
            achievements.append({
                "key": key,
                "label": _safe_row_cell(row, 3),
                "division": _safe_row_cell(row, 4),
                "awarded_at": _safe_row_cell(row, 0),
            })
        return achievements
    except Exception as exc:
        print(f"⚠️ [ACHIEVEMENTS] Dashboard-Erfolge konnten nicht geladen werden: {exc}")
        return []


def _achievement_event_claimed(rows: list[list[str]], key: str) -> bool:
    return any(_safe_row_cell(row, 2) == key for row in rows)


def _player_achievement_claimed(rows: list[list[str]], player_name: str, key: str) -> bool:
    target = normalize_name(player_name)
    return any(
        normalize_name(_safe_row_cell(row, 1)) == target
        and _safe_row_cell(row, 2) == key
        for row in rows
    )


def _append_achievement_rows(ws, rows_to_add: list[list[str]]) -> None:
    if not rows_to_add:
        return
    sheet_write_call(
        lambda: ws.append_rows(rows_to_add, value_input_option="USER_ENTERED"),
        invalidate_prefixes=_achievement_sheet_invalidation(ws),
    )


def _player_schedule_progress(rows: list[list[str]], player_name: str) -> tuple[int, int]:
    target = normalize_name(player_name)
    total = 0
    open_games = 0
    for row in rows[1:]:
        home = _safe_row_cell(row, 3)
        away = _safe_row_cell(row, 5)
        if target not in {normalize_name(home), normalize_name(away)}:
            continue
        total += 1
        if _is_open_result_value(_safe_row_cell(row, 4)):
            open_games += 1
    return total, open_games


def _award_first_scheduled_match(
    division: int,
    row_index: int,
    entered_by: str,
) -> list[str]:
    """Vergibt genau einmal pro Saison den Preis für die erste Terminplanung."""
    try:
        with _ACHIEVEMENT_LOCK:
            achievement_ws, existing = _read_current_season_achievement_rows(force_refresh=True)
            key = "season_first_scheduled_match"
            if _achievement_event_claimed(existing, key):
                return []

            div_ws = get_player_division_worksheet(int(division))
            row = row_values_cached(
                lambda: div_ws,
                sheet_name=player_sheet_name(div_ws, f"{division}.DIV"),
                row=int(row_index),
                ttl_seconds=0,
            )
            home = _safe_row_cell(row, 3)
            away = _safe_row_cell(row, 5)
            if not home or not away:
                return []

            entered_norm = normalize_name(entered_by)
            scheduler = next(
                (p for p in (home, away) if normalize_name(p) and normalize_name(p) in entered_norm),
                "",
            )
            if not scheduler:
                # Fallback für Formate wie "Terminbörse: Spielername".
                tail = (entered_by or "").split(":")[-1].strip()
                tail_norm = normalize_name(tail)
                scheduler = next(
                    (p for p in (home, away) if normalize_name(p) == tail_norm),
                    "",
                )
            if not scheduler:
                print(
                    f"⚠️ [ACHIEVEMENTS] Erster Termin erkannt, Planer aus '{entered_by}' "
                    f"für {home} vs. {away} aber nicht eindeutig bestimmbar."
                )
                return []

            label = f"📅 First Planner – erstes Match in {CURRENT_SEASON_LABEL} geplant"
            now_text = dt.now(BERLIN_TZ).strftime("%d.%m.%Y %H:%M")
            _append_achievement_rows(achievement_ws, [[
                now_text, scheduler, key, label, str(division), "Terminplanung", CURRENT_SEASON_LABEL,
            ]])
            return [f"🏅 **{scheduler}:** {label}"]
    except Exception as exc:
        print(f"⚠️ [ACHIEVEMENTS] First-Planner-Auswertung fehlgeschlagen: {exc}")
        return []


def _award_new_achievements_after_result(division: int, player_names: list[str]) -> list[str]:
    """
    Saison-eindeutige League-Achievements.

    Event-Achievements (Opening Match) werden beiden Spielern desselben ersten
    Matches gegeben. First-Finisher ist dagegen genau ein Spieler global bzw.
    genau ein Spieler je Division. Winning Streaks sind individuell und werden
    pro Schwelle genau einmal je Saison vergeben.
    """
    try:
        division = int(division)
        with _ACHIEVEMENT_LOCK:
            div_ws = get_player_division_worksheet(division)
            rows = get_all_values_cached(
                lambda: div_ws,
                sheet_name=player_sheet_name(div_ws, f"{division}.DIV"),
                ttl_seconds=PLAYER_SHEET_CACHE_TTL_SECONDS,
                force_refresh=True,
            )
            achievement_ws, existing = _read_current_season_achievement_rows(force_refresh=True)

            players = [p for p in dict.fromkeys(player_names) if p]
            if not players:
                return []

            now_text = dt.now(BERLIN_TZ).strftime("%d.%m.%Y %H:%M")
            new_rows: list[list[str]] = []
            post_lines: list[str] = []

            def stage(player: str, key: str, label: str, source: str = "League-Ergebnis"):
                nonlocal existing
                if _player_achievement_claimed(existing + new_rows, player, key):
                    return
                row = [
                    now_text, player, key, label, str(division), source, CURRENT_SEASON_LABEL,
                ]
                new_rows.append(row)
                post_lines.append(f"🏅 **{player}:** {label}")

            # 1) Erstes tatsächlich abgeschlossenes Match der gesamten Saison.
            global_opening_key = "season_opening_match"
            if not _achievement_event_claimed(existing, global_opening_key):
                label = f"🚩 Opening Match – erstes Match in {CURRENT_SEASON_LABEL}"
                for player in players:
                    stage(player, global_opening_key, label)

            # 2) Erstes abgeschlossenes Match der jeweiligen Division.
            div_opening_key = f"division_opening_match_d{division}"
            if not _achievement_event_claimed(existing, div_opening_key):
                label = (
                    f"🚩 Division {division} Opening – erstes Match der Division {division} "
                    f"in {CURRENT_SEASON_LABEL}"
                )
                for player in players:
                    stage(player, div_opening_key, label)

            # 3) Individuelle Winning-Streak-Meilensteine.
            for player in players:
                streak = _current_win_streak(rows, player)
                for threshold in _ACHIEVEMENT_STREAK_THRESHOLDS:
                    if streak >= threshold:
                        stage(
                            player,
                            f"win_streak_{threshold}",
                            f"🔥 Winning Streak {threshold} – {threshold} Siege in Folge in {CURRENT_SEASON_LABEL}",
                        )

            # 4/5) First Finisher. Falls durch dasselbe Match beide Spieler gleichzeitig
            # ihre Saison abschließen, teilen sie sich den "ersten" Platz statt dass
            # Heim/Gast-Reihenfolge willkürlich entscheidet.
            finishers = []
            for player in players:
                total, open_games = _player_schedule_progress(rows, player)
                if total > 0 and open_games == 0:
                    finishers.append(player)

            div_finisher_key = f"division_first_finisher_d{division}"
            if finishers and not _achievement_event_claimed(existing, div_finisher_key):
                label = (
                    f"🏁 Division {division} First Finisher – als Erster der Division {division} "
                    f"alle Spiele in {CURRENT_SEASON_LABEL} bestritten"
                )
                for player in finishers:
                    stage(player, div_finisher_key, label)

            global_finisher_key = "season_first_finisher"
            if finishers and not _achievement_event_claimed(existing, global_finisher_key):
                label = (
                    f"🏆 Season First Finisher – als Erster aller Divisionen "
                    f"alle Spiele in {CURRENT_SEASON_LABEL} bestritten"
                )
                for player in finishers:
                    stage(player, global_finisher_key, label)

            _append_achievement_rows(achievement_ws, new_rows)
            return post_lines
    except Exception as exc:
        print(f"⚠️ [ACHIEVEMENTS] Auswertung fehlgeschlagen: {exc}")
        return []


def _parse_league_result_post(post_text: str):
    division_match = re.search(r"\[Division\s+(\d+)\]", post_text or "", re.IGNORECASE)
    players_match = re.search(r"^(.+?)\s+vs\s+(.+?)\s+→\s+\d+\s*:\s*\d+\s*$", post_text or "", re.MULTILINE)
    if not division_match or not players_match:
        return None
    return int(division_match.group(1)), players_match.group(1).strip(), players_match.group(2).strip()


def _install_achievement_hooks() -> None:
    """
    Hängt die Achievement-Auswertung an die zentralen Matchcenter-Funktionen.
    Dadurch zählen auch klassische /termin- und Matchcenter-Wege, nicht nur
    Klicks aus dem neuen /player-Dashboard.
    """
    original_write_schedule = getattr(
        matchcenter,
        "_tfl_original_write_league_schedule",
        matchcenter.write_league_schedule,
    )
    original_send_result_post = getattr(
        matchcenter,
        "_tfl_original_send_result_post",
        matchcenter.send_result_post,
    )
    matchcenter._tfl_original_write_league_schedule = original_write_schedule
    matchcenter._tfl_original_send_result_post = original_send_result_post

    def achievement_aware_write_league_schedule(
        row_index: int,
        mode: str,
        event_url: str,
        entered_by: str,
        timestamp: str,
        division_label: str,
    ):
        result = original_write_schedule(
            row_index,
            mode,
            event_url,
            entered_by,
            timestamp,
            division_label,
        )
        try:
            # Die Terminbörse schreibt zuerst ins Sheet und erzeugt danach das
            # Discord-Event. Dort vergeben wir das Achievement deshalb erst nach
            # erfolgreicher Event-Erstellung, damit ein Rollback keinen falschen
            # First-Planner erzeugt. Das klassische Matchcenter erzeugt das Event
            # bereits vor dem Sheet-Write und kann hier direkt gewertet werden.
            if "terminbörse" not in (entered_by or "").lower():
                match = re.search(r"(\d+)", str(division_label))
                if match:
                    _award_first_scheduled_match(int(match.group(1)), int(row_index), entered_by)
        except Exception as exc:
            print(f"⚠️ [ACHIEVEMENTS] Termin-Hook fehlgeschlagen: {exc}")
        return result

    async def achievement_aware_send_result_post(guild, post_text: str):
        augmented = post_text
        try:
            parsed = _parse_league_result_post(post_text)
            if parsed:
                division, player1, player2 = parsed
                lines = await asyncio.to_thread(
                    _award_new_achievements_after_result,
                    division,
                    [player1, player2],
                )
                if lines:
                    augmented += "\n\n**🏅 Neue Achievements**\n" + "\n".join(lines)
        except Exception as exc:
            print(f"⚠️ [ACHIEVEMENTS] Ergebnis-Hook fehlgeschlagen: {exc}")
        return await original_send_result_post(guild, augmented)

    matchcenter.write_league_schedule = achievement_aware_write_league_schedule
    matchcenter.send_result_post = achievement_aware_send_result_post
    globals()["send_result_post"] = achievement_aware_send_result_post
    matchcenter._tfl_season_achievement_hooks_installed = True


def _get_or_create_coop_league_ws():
    if COOP_LEAGUE_SHEET in _PLAYER_WORKSHEET_CACHE_BY_NAME:
        return _PLAYER_WORKSHEET_CACHE_BY_NAME[COOP_LEAGUE_SHEET]
    wb = get_season_spreadsheet()
    try:
        ws = wb.worksheet(COOP_LEAGUE_SHEET)
    except WorksheetNotFound:
        ws = wb.add_worksheet(title=COOP_LEAGUE_SHEET, rows=500, cols=10)
        ws.update("A1:H1", [["Nr", "Datum", "Modus", "Heimteam", "Ergebnis", "Gastteam", "Link", "Reporter"]])
    _PLAYER_WORKSHEET_CACHE_BY_NAME[COOP_LEAGUE_SHEET] = ws
    return ws


def _coop_league_invalidation(ws) -> list[str]:
    name = player_sheet_name(ws, COOP_LEAGUE_SHEET)
    return [f"records:{name}", f"values:{name}", f"row:{name}:", f"col:{name}:", f"cell:{name}:"]


def _build_coop_table(rows: list[list[str]], team_names: list[str], own_team: str) -> list[dict]:
    stats = {}
    for name in team_names:
        clean = (name or "").strip()
        if clean and normalize_name(clean) not in stats:
            stats[normalize_name(clean)] = {
                "name": clean, "wins": 0, "draws": 0, "losses": 0,
                "played": 0, "score_for": 0, "score_against": 0, "diff": 0, "points": 0,
            }
    for row in rows[1:]:
        home, result, away = _safe_row_cell(row, 3), _safe_row_cell(row, 4), _safe_row_cell(row, 5)
        if not home or not away or _is_open_result_value(result):
            continue
        score = _parse_match_score(result)
        if score is None:
            continue
        for team in (home, away):
            key = normalize_name(team)
            if key not in stats:
                stats[key] = {"name": team, "wins": 0, "draws": 0, "losses": 0, "played": 0, "score_for": 0, "score_against": 0, "diff": 0, "points": 0}
        hs, as_ = score
        hk, ak = normalize_name(home), normalize_name(away)
        stats[hk]["played"] += 1; stats[ak]["played"] += 1
        stats[hk]["score_for"] += hs; stats[hk]["score_against"] += as_
        stats[ak]["score_for"] += as_; stats[ak]["score_against"] += hs
        if hs > as_:
            stats[hk]["wins"] += 1; stats[ak]["losses"] += 1
        elif as_ > hs:
            stats[ak]["wins"] += 1; stats[hk]["losses"] += 1
        else:
            stats[hk]["draws"] += 1; stats[ak]["draws"] += 1
    for item in stats.values():
        item["diff"] = item["score_for"] - item["score_against"]
        item["points"] = item["wins"] * 2 + item["draws"]
    ranked = sorted(stats.values(), key=lambda x: (-x["points"], -x["diff"], -x["score_for"], -x["wins"], normalize_name(x["name"])))
    own_key = normalize_name(own_team)
    for idx, item in enumerate(ranked, start=1):
        item["rank"] = idx; item["is_self"] = normalize_name(item["name"]) == own_key
    return ranked


def load_coop_dashboard_data(member_id: int, name_candidates: list[str], force_refresh: bool = False) -> dict:
    try:
        _, signup_rows = coop.get_coop_rows()
    except Exception as exc:
        return {"found": False, "error": f"Coop-Anmeldungen konnten nicht geladen werden: {exc}", "next_matches": [], "today_matches": [], "coop_table": []}
    targets = {normalize_name(x) for x in name_candidates if x}
    team_row = None
    for row in signup_rows[1:]:
        status = _safe_row_cell(row, 5).lower()
        if status not in {"offen", "bestätigt"}:
            continue
        ids = {_safe_row_cell(row, 9), _safe_row_cell(row, 10)}
        names = {normalize_name(_safe_row_cell(row, 1)), normalize_name(_safe_row_cell(row, 2))}
        if str(member_id) in ids or (targets & names):
            team_row = row; break
    if team_row is None:
        return {"found": False, "team_name": "", "partner": "", "status": "nicht angemeldet", "next_matches": [], "today_matches": [], "coop_table": []}
    team_name = _safe_row_cell(team_row, 0)
    p1, p2 = _safe_row_cell(team_row, 1), _safe_row_cell(team_row, 2)
    status = _safe_row_cell(team_row, 5).lower() or "offen"
    p1_id = _safe_row_cell(team_row, 9)
    partner = p2 if str(member_id) == p1_id or normalize_name(p1) in targets else p1
    confirmed_teams = [_safe_row_cell(row, 0) for row in signup_rows[1:] if _safe_row_cell(row, 5).lower() == "bestätigt" and _safe_row_cell(row, 0)]
    if status != "bestätigt":
        return {"found": True, "team_name": team_name, "partner": partner, "status": status, "played": 0, "open": 0, "scheduled_open": 0, "total": 0, "next_matches": [], "today_matches": [], "coop_table": []}
    ws = _get_or_create_coop_league_ws()
    rows = get_all_values_cached(lambda: ws, sheet_name=player_sheet_name(ws, COOP_LEAGUE_SHEET), ttl_seconds=PLAYER_SHEET_CACHE_TTL_SECONDS, force_refresh=force_refresh)
    target_team = normalize_name(team_name); now = dt.now(BERLIN_TZ)
    total = played = open_games = scheduled_open = 0; upcoming = []
    for row_index, row in enumerate(rows[1:], start=2):
        home, result, away = _safe_row_cell(row, 3), _safe_row_cell(row, 4), _safe_row_cell(row, 5)
        if not home or not away: continue
        home_match, away_match = normalize_name(home) == target_team, normalize_name(away) == target_team
        if not home_match and not away_match: continue
        total += 1
        if not _is_open_result_value(result): played += 1; continue
        open_games += 1
        date_text = _safe_row_cell(row, 1)
        if date_text:
            scheduled_open += 1
            parsed = parse_sheet_datetime(date_text)
            if parsed is not None and (parsed >= now - timedelta(minutes=5) or parsed.astimezone(BERLIN_TZ).date() == now.date()):
                upcoming.append({"datetime": parsed, "date_text": date_text, "mode": _safe_row_cell(row, 2), "home": home, "away": away, "opponent": away if home_match else home, "row": row_index, "link": _safe_row_cell(row, 6)})
    upcoming.sort(key=lambda item: item["datetime"])
    today = [item for item in upcoming if item["datetime"].astimezone(BERLIN_TZ).date() == now.date()]
    return {"found": True, "team_name": team_name, "partner": partner, "status": status, "played": played, "open": open_games, "scheduled_open": scheduled_open, "total": total, "next_matches": upcoming[:25], "today_matches": today, "coop_table": _build_coop_table(rows, confirmed_teams, team_name)}


def _load_live_coop_result_match(row_index: int, expected_home: str, expected_away: str, team_name: str) -> dict:
    ws = _get_or_create_coop_league_ws()
    rows = get_all_values_cached(lambda: ws, sheet_name=player_sheet_name(ws, COOP_LEAGUE_SHEET), ttl_seconds=0, force_refresh=True)
    if row_index < 2 or row_index > len(rows): raise RuntimeError("Das Coop-Spiel wurde nicht mehr gefunden.")
    row = rows[row_index - 1]
    home, result, away = _safe_row_cell(row, 3), _safe_row_cell(row, 4), _safe_row_cell(row, 5)
    if normalize_name(home) != normalize_name(expected_home) or normalize_name(away) != normalize_name(expected_away): raise RuntimeError("Die Begegnung hat sich im Coop-Sheet geändert.")
    if normalize_name(team_name) not in {normalize_name(home), normalize_name(away)}: raise PermissionError("Dein Team ist an diesem Spiel nicht beteiligt.")
    return {"home": home, "away": away, "result": result, "open": _is_open_result_value(result), "mode": _safe_row_cell(row, 2) or "Coop", "timestamp": _safe_row_cell(row, 1), "link": _safe_row_cell(row, 6)}


def _write_coop_result(row_index: int, result: str, link: str, reporter: str) -> None:
    ws = _get_or_create_coop_league_ws(); timestamp = dt.now(BERLIN_TZ).strftime("%d.%m.%Y %H:%M")
    reqs = [{"range": f"B{row_index}:B{row_index}", "values": [[timestamp]]}, {"range": f"E{row_index}:E{row_index}", "values": [[result]]}, {"range": f"G{row_index}:G{row_index}", "values": [[link]]}, {"range": f"H{row_index}:H{row_index}", "values": [[reporter]]}]
    sheet_write_call(lambda: ws.batch_update(reqs), invalidate_prefixes=_coop_league_invalidation(ws))


def _format_simple_table(table: list[dict], name_key: str = "name") -> str:
    if not table: return "Noch keine Tabellendaten verfügbar."
    name_width = max(12, min(20, max(len(str(item.get(name_key, ""))) for item in table[:12])))
    lines = [f"{'Pl':>2}  {'Team':<{name_width}}  {'Sp':>2}  {'S':>2}  {'U':>2}  {'N':>2}  {'Pkt':>3}", f"{'--':>2}  {'-' * name_width}  {'--':>2}  {'--':>2}  {'--':>2}  {'--':>2}  {'---':>3}"]
    for item in table[:12]:
        name = f"{str(item.get(name_key, '')):<{name_width}}"
        if item.get("is_self"): name = f"\u001b[1;33m{name}\u001b[0m"
        lines.append(f"{int(item.get('rank') or 0):>2}. {name}  {int(item.get('played') or 0):>2}  {int(item.get('wins') or 0):>2}  {int(item.get('draws') or 0):>2}  {int(item.get('losses') or 0):>2}  {int(item.get('points') or 0):>3}")
    return "```ansi\n" + "\n".join(lines) + "\n```"



def _load_division_table_for_dashboard(rows: list[list[str]], player_name: str) -> list[dict]:
    """Baut die Divisionstabelle ausschließlich aus bereits geladenen Sheet-Daten."""
    players = []
    seen = set()

    # Die Roster-/Spielernamen stehen in Spalte L. Nicht auf feste Zeilenzahlen
    # verlassen: so funktioniert es auch bei leicht verschobenen Tabellen.
    for row in rows:
        raw = row[11].strip() if len(row) > 11 else ""
        if not raw:
            continue
        key = normalize_name(raw)
        if not key or key in seen or key in {"racer", "spieler", "teilnehmer"}:
            continue
        seen.add(key)
        players.append(raw)

    stats = {
        normalize_name(name): {
            "name": name,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "played": 0,
            "score_for": 0,
            "score_against": 0,
            "diff": 0,
        }
        for name in players
    }

    for row in rows[1:]:
        home = row[3].strip() if len(row) > 3 else ""
        result = row[4].strip() if len(row) > 4 else ""
        away = row[5].strip() if len(row) > 5 else ""

        if not home or not away or _is_open_result_value(result):
            continue

        parsed = _parse_match_score(result)
        if parsed is None:
            continue

        home_score, away_score = parsed
        home_key = normalize_name(home)
        away_key = normalize_name(away)

        if home_key not in stats:
            stats[home_key] = {
                "name": home,
                "wins": 0,
                "draws": 0,
                "losses": 0,
                "played": 0,
                "score_for": 0,
                "score_against": 0,
                "diff": 0,
            }
        if away_key not in stats:
            stats[away_key] = {
                "name": away,
                "wins": 0,
                "draws": 0,
                "losses": 0,
                "played": 0,
                "score_for": 0,
                "score_against": 0,
                "diff": 0,
            }

        stats[home_key]["played"] += 1
        stats[away_key]["played"] += 1
        stats[home_key]["score_for"] += home_score
        stats[home_key]["score_against"] += away_score
        stats[away_key]["score_for"] += away_score
        stats[away_key]["score_against"] += home_score

        if home_score > away_score:
            stats[home_key]["wins"] += 1
            stats[away_key]["losses"] += 1
        elif away_score > home_score:
            stats[away_key]["wins"] += 1
            stats[home_key]["losses"] += 1
        else:
            stats[home_key]["draws"] += 1
            stats[away_key]["draws"] += 1

    for item in stats.values():
        item["diff"] = item["score_for"] - item["score_against"]
        item["points"] = (item["wins"] * 2) + item["draws"]

    ranked = sorted(
        stats.values(),
        key=lambda item: (
            -item["points"],
            -item["diff"],
            -item["score_for"],
            -item["wins"],
            normalize_name(item["name"]),
        ),
    )

    target = normalize_name(player_name)
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index
        item["is_self"] = normalize_name(item["name"]) == target

    return ranked

def load_player_dashboard_data(
    name_candidates: list[str],
    preferred_division: int | None = None,
    force_refresh: bool = False,
) -> dict:
    targets = {normalize_name(x) for x in name_candidates if x}; targets.discard("")
    empty = {"found": False, "player_name": next((x for x in name_candidates if x), "Spieler"), "division": None, "mode_1": "", "mode_2": "", "played": 0, "open": 0, "scheduled_open": 0, "total": 0, "next_matches": [], "today_matches": [], "division_table": [], "achievements": []}
    if not targets: return empty
    division_order = []
    if preferred_division in {1,2,3,4,5,6}: division_order.append(int(preferred_division))
    division_order.extend(d for d in range(1,7) if d not in division_order)
    ws = rows = None; roster_row_index = div_number = None
    for candidate_div in division_order:
        candidate_ws = get_player_division_worksheet(candidate_div)
        candidate_rows = get_all_values_cached(lambda ws=candidate_ws: ws, sheet_name=player_sheet_name(candidate_ws, f"{candidate_div}.DIV"), ttl_seconds=PLAYER_SHEET_CACHE_TTL_SECONDS, force_refresh=force_refresh)
        found_row = None
        for idx, row in enumerate(candidate_rows, start=1):
            roster_name = _safe_row_cell(row, 11)
            if roster_name and normalize_name(roster_name) in targets: found_row = idx; break
        if found_row is not None:
            ws, rows, roster_row_index, div_number = candidate_ws, candidate_rows, found_row, candidate_div; break
    if ws is None or rows is None or roster_row_index is None or div_number is None: return empty
    roster_row = rows[roster_row_index - 1] if roster_row_index <= len(rows) else []
    player_name = _safe_row_cell(roster_row, 11) or next((x.strip() for x in name_candidates if x and x.strip()), "Spieler")
    mode_1, mode_2 = _safe_row_cell(roster_row, 12), _safe_row_cell(roster_row, 13)
    target = normalize_name(player_name); now = dt.now(BERLIN_TZ)
    played = open_games = scheduled_open = total = 0; upcoming = []
    for row_index, row in enumerate(rows[1:], start=2):
        home, result, away = _safe_row_cell(row, 3), _safe_row_cell(row, 4), _safe_row_cell(row, 5)
        if not home or not away: continue
        home_match, away_match = normalize_name(home) == target, normalize_name(away) == target
        if not home_match and not away_match: continue
        total += 1
        if not _is_open_result_value(result): played += 1; continue
        open_games += 1
        date_text, mode = _safe_row_cell(row, 1), _safe_row_cell(row, 2)
        if date_text:
            scheduled_open += 1
            parsed_date = parse_sheet_datetime(date_text)
            if parsed_date is not None and (
                parsed_date >= now - timedelta(minutes=5)
                or parsed_date.astimezone(BERLIN_TZ).date() == now.date()
            ):
                upcoming.append({"datetime": parsed_date, "date_text": date_text, "mode": mode, "home": home, "away": away, "opponent": away if home_match else home, "row": row_index, "link": _safe_row_cell(row, 6)})
    upcoming.sort(key=lambda item: item["datetime"])
    table = _load_division_table_for_dashboard(rows, player_name)
    today = [item for item in upcoming if item["datetime"].astimezone(BERLIN_TZ).date() == now.date()]
    achievements = _load_awarded_achievements_for_player(player_name)
    return {"found": True, "player_name": player_name, "division": int(div_number), "mode_1": mode_1, "mode_2": mode_2, "played": played, "open": open_games, "scheduled_open": scheduled_open, "total": total, "next_matches": upcoming[:25], "today_matches": today, "division_table": table, "achievements": achievements}


def _parse_season_date(value: str):
    raw = (value or "").strip()
    if not raw:
        return None

    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            parsed = dt.strptime(raw, fmt)
            return BERLIN_TZ.localize(parsed.replace(hour=0, minute=0, second=0, microsecond=0))
        except ValueError:
            continue
    return None


def get_deadline_traffic_light(played: int, total: int) -> dict:
    start = _parse_season_date(TFL_SEASON_START)
    end = _parse_season_date(TFL_SEASON_END)

    if start is None or end is None or end <= start or total <= 0:
        return {
            "emoji": "⚪",
            "label": "Zeitplan nicht konfiguriert",
            "detail": "Saisonstart/-ende fehlen",
        }

    now = dt.now(BERLIN_TZ)
    if now < start:
        return {
            "emoji": "🟢",
            "label": "im Soll",
            "detail": f"Saisonstart: {start.strftime('%d.%m.%Y')}",
        }

    if now >= end:
        if played >= total:
            return {
                "emoji": "🟢",
                "label": "im Soll",
                "detail": f"{played}/{total} gespielt",
            }
        return {
            "emoji": "🔴",
            "label": "hinter Saisonvorgabe",
            "detail": f"Soll: {total}/{total} · Stand: {played}",
        }

    season_seconds = (end - start).total_seconds()
    elapsed_seconds = max(0.0, (now - start).total_seconds())
    progress = min(1.0, elapsed_seconds / season_seconds) if season_seconds > 0 else 0.0
    expected = min(total, int(total * progress))

    if played >= expected:
        emoji = "🟢"
        label = "im Soll"
    elif played == expected - 1:
        emoji = "🟡"
        label = "bald Handlungsbedarf"
    else:
        emoji = "🔴"
        label = "hinter Saisonvorgabe"

    return {
        "emoji": emoji,
        "label": label,
        "detail": f"Soll: {expected}/{total} · Stand: {played}",
    }


def build_player_dashboard_embed(data: dict, note: str | None = None) -> discord.Embed:
    player_name = data.get("player_name") or "Spieler"
    division = data.get("division")

    if data.get("found"):
        subtitle = f"**{player_name}** · **Division {division}**"
    else:
        subtitle = f"**{player_name}** · Division noch nicht erkannt"

    description = (
        f"{subtitle}\n"
        f"**{CURRENT_SEASON_LABEL}**\n\n"
        "Deine zentrale Anlaufstelle für Spiele, Termine und Saisoninformationen."
    )

    if note:
        description += f"\n\n{note}"

    embed = discord.Embed(
        title=(f"⚔️ {player_name} · Division {division}" if data.get("found") else "⚔️ TFL SPIELERBEREICH"),
        description=(
            f"**{CURRENT_SEASON_LABEL}**\n"
            "Deine zentrale Anlaufstelle für Spiele, Termine und Saisoninformationen."
            + (f"\n\n{note}" if note else "")
        ) if data.get("found") else description,
        color=TFL_COLOR,
    )

    if data.get("found"):
        played = int(data.get("played") or 0)
        total = int(data.get("total") or 0)
        open_games = int(data.get("open") or 0)
        scheduled_open = int(data.get("scheduled_open") or 0)

        embed.add_field(
            name="🎯 Saisonstatus",
            value=(
                f"**{played}/{total}** gespielt\n"
                f"**{open_games}** offen\n"
                f"**{scheduled_open}** davon terminiert"
            ),
            inline=True,
        )

        deadline = get_deadline_traffic_light(played, total)
        embed.add_field(
            name=f"{deadline['emoji']} Deadline-Ampel",
            value=f"**{deadline['label']}**\n{deadline['detail']}",
            inline=True,
        )

        mode_1 = data.get("mode_1") or "–"
        mode_2 = data.get("mode_2") or "–"
        embed.add_field(
            name="🚫 Streichmodi",
            value=f"**{mode_1}**\n**{mode_2}**",
            inline=True,
        )

        today_matches = data.get("today_matches") or []
        if today_matches:
            matchday_lines = []
            for idx, match in enumerate(today_matches[:3], start=1):
                when = match["datetime"].strftime("%H:%M") if match.get("datetime") else "?"
                mode = match.get("mode") or "Modus noch offen"
                home = match.get("home") or "?"
                away = match.get("away") or "?"
                matchday_lines.append(f"**{idx}. {when} Uhr** · {home} vs. {away} · 🎮 {mode}")
            embed.add_field(
                name="🔥 Matchday",
                value="\n".join(matchday_lines),
                inline=False,
            )

        today_rows = {int(match.get("row") or 0) for match in today_matches}
        next_matches = [
            match for match in (data.get("next_matches") or [])
            if int(match.get("row") or 0) not in today_rows
        ]
        if next_matches:
            embed.add_field(
                name="📅 Nächste Termine",
                value="\u200b",
                inline=False,
            )

            for match in next_matches[:3]:
                when = match["datetime"].strftime("%d.%m.%Y · %H:%M")
                mode = match.get("mode") or "Modus noch offen"
                home = match.get("home") or "?"
                away = match.get("away") or "?"

                embed.add_field(
                    name=f"{when} Uhr",
                    value=(
                        f"{home} vs. {away}\n"
                        f"🎮 {mode}"
                    ),
                    inline=True,
                )
        else:
            embed.add_field(
                name="📅 Nächste Termine",
                value=(
                    "Nach den heutigen Spielen sind aktuell keine weiteren zukünftigen Termine eingetragen."
                    if today_matches
                    else "Aktuell sind keine zukünftigen Termine eingetragen."
                ),
                inline=False,
            )

        division_table = data.get("division_table") or []
        if division_table:
            # Feste Spaltenbreiten sorgen in Discord für eine echte Tabellenansicht.
            # ANSI färbt nur den bereits aufgefüllten Namensbereich und zerstört
            # dadurch die Ausrichtung der nachfolgenden Zahlen nicht.
            name_width = max(12, min(18, max(len(item["name"]) for item in division_table[:9])))

            header = (
                f"{'Pl':>2}  "
                f"{'Spieler':<{name_width}}  "
                f"{'Sp':>2}  {'S':>2}  {'U':>2}  {'N':>2}  {'Pkt':>3}"
            )
            separator = (
                f"{'--':>2}  "
                f"{'-' * name_width}  "
                f"{'--':>2}  {'--':>2}  {'--':>2}  {'--':>2}  {'---':>3}"
            )

            lines = [header, separator]

            for item in division_table[:9]:
                short_name = _truncate_dashboard_name(item.get("name") or "", name_width)
                marker = "▶" if item.get("is_self") else " "
                padded_name = f"{marker} {short_name:<{name_width}}"
                if item.get("is_self"):
                    padded_name = f"\u001b[1;33m{padded_name}\u001b[0m"

                lines.append(
                    f"{item['rank']:>2}. "
                    f"{padded_name}  "
                    f"{item['played']:>2}  "
                    f"{item['wins']:>2}  "
                    f"{item['draws']:>2}  "
                    f"{item['losses']:>2}  "
                    f"{item['points']:>3}"
                )

            table_text = "```ansi\n" + "\n".join(lines) + "\n```"
            embed.add_field(
                name=f"🏆 Aktuelle Tabelle Division {division}",
                value=table_text[:1024],
                inline=False,
            )
    else:
        embed.add_field(
            name="ℹ️ Saisonstatus",
            value=(
                "Deine Divisionsdaten konnten aktuell nicht aus dem Sheet geladen werden. "
                "Die Menüfunktionen stehen trotzdem zur Verfügung."
            ),
            inline=False,
        )

    embed.set_footer(text=f"Try Force League · {CURRENT_SEASON_LABEL} · Together we race")
    return embed


def _dashboard_division_from_member(member: discord.Member) -> int | None:
    """Ermittelt die Division ohne Sheet-Read direkt aus der Discord-Rolle."""
    if not isinstance(member, discord.Member):
        return None

    for div_number in range(1, 7):
        for role in member.roles:
            if division_role_matches(role.name, div_number):
                return div_number
    return None


async def get_player_dashboard_data(member, force_refresh: bool = False) -> dict:
    if not isinstance(member, discord.Member):
        return {
            "found": False,
            "player_name": getattr(member, "display_name", "Spieler"),
            "division": None,
            "next_matches": [],
        }

    preferred_division = _dashboard_division_from_member(member)

    started = time.perf_counter()
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(
                load_player_dashboard_data,
                get_name_candidates(member),
                preferred_division,
                force_refresh,
            ),
            timeout=PLAYER_DASHBOARD_IO_TIMEOUT_SECONDS,
        )
        elapsed = time.perf_counter() - started
        if elapsed >= 2.0:
            print(
                f"[PLAYER DASHBOARD] Daten geladen in {elapsed:.2f}s "
                f"(Division={data.get('division')}, force={force_refresh})"
            )
        return data
    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - started
        print(
            f"⚠️ [PLAYER DASHBOARD] Sheet-Laden nach {elapsed:.2f}s abgebrochen. "
            "Dashboard wird ohne Live-Daten geöffnet."
        )
        return {
            "found": False,
            "player_name": member.display_name,
            "division": preferred_division,
            "next_matches": [],
        }
    except Exception as e:
        print(f"[PLAYER DASHBOARD] Laden fehlgeschlagen: {e}")
        return {
            "found": False,
            "player_name": member.display_name,
            "division": preferred_division,
            "next_matches": [],
        }


async def get_coop_dashboard_data(member, force_refresh: bool = False) -> dict:
    if not isinstance(member, discord.Member):
        return {"found": False, "team_name": "", "partner": "", "status": "nicht verfügbar", "next_matches": [], "today_matches": [], "coop_table": []}
    started = time.perf_counter()
    try:
        data = await asyncio.wait_for(asyncio.to_thread(load_coop_dashboard_data, member.id, get_name_candidates(member), force_refresh), timeout=PLAYER_DASHBOARD_IO_TIMEOUT_SECONDS)
        elapsed = time.perf_counter() - started
        if elapsed >= 2.0: print(f"[COOP DASHBOARD] Daten geladen in {elapsed:.2f}s (force={force_refresh})")
        return data
    except asyncio.TimeoutError:
        print("⚠️ [COOP DASHBOARD] Laden wegen Timeout abgebrochen")
        return {"found": False, "team_name": "", "partner": "", "status": "Timeout beim Laden", "next_matches": [], "today_matches": [], "coop_table": []}
    except Exception as exc:
        print(f"⚠️ [COOP DASHBOARD] Laden fehlgeschlagen: {exc}")
        return {"found": False, "team_name": "", "partner": "", "status": "Daten nicht verfügbar", "next_matches": [], "today_matches": [], "coop_table": []}

async def get_player_dashboard_embed(member, note: str | None = None, force_refresh: bool = False) -> discord.Embed:
    data = await get_player_dashboard_data(member, force_refresh=force_refresh)
    return build_player_dashboard_embed(data, note=note)


def _dashboard_banner_embed() -> discord.Embed | None:
    banner = discord.Embed(color=TFL_COLOR)

    if TFL_DASHBOARD_BANNER_URL:
        banner.set_image(url=TFL_DASHBOARD_BANNER_URL)
        return banner

    if TFL_DASHBOARD_BANNER_PATH and os.path.isfile(TFL_DASHBOARD_BANNER_PATH):
        banner.set_image(url=f"attachment://{DASHBOARD_BANNER_FILENAME}")
        return banner

    return None


def _dashboard_local_banner_file() -> discord.File | None:
    if TFL_DASHBOARD_BANNER_URL:
        return None
    if not TFL_DASHBOARD_BANNER_PATH or not os.path.isfile(TFL_DASHBOARD_BANNER_PATH):
        return None
    return discord.File(TFL_DASHBOARD_BANNER_PATH, filename=DASHBOARD_BANNER_FILENAME)


def _message_uses_components_v2(message) -> bool:
    try:
        return bool(int(getattr(getattr(message, "flags", None), "value", 0)) & COMPONENTS_V2_FLAG)
    except Exception:
        return False


async def close_player_panel(interaction: discord.Interaction):
    """Schließt ein untergeordnetes /player-Fenster; das V2-Dashboard bleibt stehen."""
    if not interaction.response.is_done():
        await interaction.response.defer()

    try:
        await interaction.delete_original_response()
    except Exception:
        try:
            await interaction.edit_original_response(
                content="↩️ Das Spieler-Dashboard bleibt geöffnet.",
                embed=None,
                view=None,
                attachments=[],
            )
        except Exception:
            pass


async def show_player_dashboard(
    interaction: discord.Interaction,
    note: str | None = None,
    already_deferred: bool = False,
    force_refresh: bool = False,
    dashboard_mode: str = "league",
):
    # Hauptaktionen des V2-Dashboards öffnen eigene ephemere Panels. Ein
    # "Zurück" aus so einem Panel schließt deshalb nur dieses Panel.
    if (
        HAS_COMPONENTS_V2
        and interaction.message is not None
        and not _message_uses_components_v2(interaction.message)
        and not already_deferred
    ):
        await close_player_panel(interaction)
        return

    if not already_deferred and not interaction.response.is_done():
        await interaction.response.defer()

    dashboard_mode = "coop" if str(dashboard_mode).lower() == "coop" else "league"
    if dashboard_mode == "coop":
        data = await get_coop_dashboard_data(interaction.user, force_refresh=force_refresh)
    else:
        data = await get_player_dashboard_data(interaction.user, force_refresh=force_refresh)

    if HAS_COMPONENTS_V2:
        try:
            view = build_dashboard_layout_view(
                data=data,
                owner_id=interaction.user.id,
                show_admin=has_admin_role(interaction.user),
                note=note,
                dashboard_mode=dashboard_mode,
            )
            component_count = getattr(view, "total_children_count", None)
            if component_count is not None:
                print(f"[PLAYER DASHBOARD] Components V2: {component_count}/40 Children")
            banner_file = getattr(view, "banner_file", None)
            await interaction.edit_original_response(
                content=None,
                embed=None,
                attachments=[banner_file] if banner_file is not None else [],
                view=view,
            )
            return
        except Exception as exc:
            # Ein V2-Layoutfehler darf /player nie wieder scheinbar endlos
            # laden lassen. Vor dem Senden der V2-Nachricht können wir sicher
            # auf das klassische Dashboard zurückfallen.
            print(f"⚠️ [PLAYER DASHBOARD] Components-V2-Fallback: {exc}")
            traceback.print_exc()

    # Fallback für ältere discord.py-Versionen.
    if dashboard_mode == "coop":
        embed = discord.Embed(
            title=f"👥 Coop League · {data.get('team_name') or 'Noch kein Team'}",
            description=f"**Partner:** {data.get('partner') or '–'}\n**Status:** {data.get('status') or '–'}",
            color=0x2ECC71,
        )
        view = CoopFallbackDashboardView(owner_id=interaction.user.id)
    else:
        embed = build_player_dashboard_embed(data, note=note)
        next_matches = data.get("next_matches") or []
        view = PlayerMenuView(
            owner_id=interaction.user.id,
            show_admin=has_admin_role(interaction.user),
            next_matches=next_matches,
            division=data.get("division"),
        )

    banner_embed = _dashboard_banner_embed()
    banner_file = _dashboard_local_banner_file()
    kwargs = {"content": None, "view": view}

    if banner_embed is not None:
        kwargs["embeds"] = [banner_embed, embed]
        kwargs["attachments"] = [banner_file] if banner_file is not None else []
    else:
        kwargs["embed"] = embed
        kwargs["attachments"] = []

    await interaction.edit_original_response(**kwargs)


def normalize_name(value: str) -> str:
    """
    Normalisiert Discord-/Sheet-Namen robust.

    Beispiele:
    GNRB, .gnrb, G-N-R-B, G_N_R_B, G N R B -> gnrb
    """
    value = unicodedata.normalize("NFKC", value or "")
    value = value.lower().strip()
    return re.sub(r"[^a-z0-9äöüß]", "", value)


ADMIN_ROLE_NAME = "Admin"


def has_admin_role(member) -> bool:
    return (
        isinstance(member, discord.Member)
        and any(role.name == ADMIN_ROLE_NAME for role in member.roles)
    )


def get_main_bot_module():
    """
    bot.py läuft auf Render als __main__. Ein normales import bot würde den
    Bot ein zweites Mal initialisieren. Daher verwenden wir das bereits
    geladene Hauptmodul.
    """
    for module_name in ("__main__", "bot"):
        module = sys.modules.get(module_name)

        if module is not None and hasattr(module, "client"):
            return module

    return None


def get_main_helper(name: str):
    module = get_main_bot_module()

    if module is None:
        raise RuntimeError("Hauptmodul des Bots wurde nicht gefunden.")

    helper = getattr(module, name, None)

    if helper is None:
        raise RuntimeError(f"Bot-Helfer '{name}' ist nicht verfügbar.")

    return helper


# =========================================================
# GOOGLE SHEETS FÜR STREICHMODI
# =========================================================

def get_name_candidates(member: discord.Member) -> list[str]:
    """
    Alle sinnvollen Discord-Namensvarianten für den Vergleich mit Spalte L.
    """
    return [
        member.display_name,
        getattr(member, "global_name", None),
        member.name,
        str(member),
    ]


def get_worksheet_by_gid(workbook, gid: int):
    gid = int(gid)

    if gid in _PLAYER_WORKSHEET_CACHE_BY_GID:
        return _PLAYER_WORKSHEET_CACHE_BY_GID[gid]

    # Einige ältere Module halten ihre eigene WB-Referenz. Nach einem
    # Reconnect kann diese None sein, obwohl die zentrale Season-Verbindung
    # längst wieder verfügbar ist. Deshalb niemals blind workbook.worksheets()
    # aufrufen, sondern auf die zentrale Verbindung zurückfallen.
    if workbook is None:
        workbook = get_season_spreadsheet()

    if workbook is None:
        raise RuntimeError("Season-Spreadsheet ist aktuell nicht verbunden.")

    for ws in workbook.worksheets():
        _PLAYER_WORKSHEET_CACHE_BY_GID[int(ws.id)] = ws
        _PLAYER_WORKSHEET_CACHE_BY_NAME[getattr(ws, "title", "")] = ws

    if gid in _PLAYER_WORKSHEET_CACHE_BY_GID:
        return _PLAYER_WORKSHEET_CACHE_BY_GID[gid]

    raise RuntimeError(f"Worksheet mit gid={gid} nicht gefunden.")


def get_player_division_worksheet(div_number: int):
    sheet_name = f"{int(div_number)}.DIV"

    if sheet_name in _PLAYER_WORKSHEET_CACHE_BY_NAME:
        return _PLAYER_WORKSHEET_CACHE_BY_NAME[sheet_name]

    ws = get_season_worksheet(sheet_name)
    _PLAYER_WORKSHEET_CACHE_BY_NAME[sheet_name] = ws
    return ws


def player_sheet_name(ws, fallback: str = "PlayerSheet") -> str:
    return getattr(ws, "title", fallback)


def player_invalidate_prefixes(ws, fallback: str = "PlayerSheet") -> list[str]:
    sheet_name = player_sheet_name(ws, fallback)
    return [
        f"records:{sheet_name}",
        f"values:{sheet_name}",
        f"row:{sheet_name}:",
        f"col:{sheet_name}:",
        f"cell:{sheet_name}:",
    ]


def get_division_worksheet_for_name_candidates(name_candidates: list[str]):
    """
    Sucht den Spieler in allen Division-Tabs 1.DIV bis 6.DIV in Spalte L.
    Gibt (worksheet, row_index, division_number) zurück.

    row_index ist die echte Google-Sheet-Zeile, also 1-basiert.
    """
    targets = {normalize_name(x) for x in name_candidates if x}
    targets.discard("")

    if not targets:
        return None, None, None

    for div_number in range(1, 7):
        ws = get_player_division_worksheet(div_number)
        values = col_values_cached(
            lambda: ws,
            sheet_name=player_sheet_name(ws, f"{div_number}.DIV"),
            col=12,  # Spalte L
            ttl_seconds=PLAYER_SHEET_CACHE_TTL_SECONDS,
        )

        for idx, cell_value in enumerate(values, start=1):
            if normalize_name(cell_value) in targets:
                return ws, idx, div_number

    return None, None, None


def load_current_streichmodi_for_name_candidates(name_candidates: list[str]) -> tuple[str, str]:
    ws, row_index, div_number = get_division_worksheet_for_name_candidates(name_candidates)

    if ws is None or row_index is None:
        return "", ""

    row = row_values_cached(
        lambda: ws,
        sheet_name=player_sheet_name(ws),
        row=row_index,
        ttl_seconds=PLAYER_SHEET_CACHE_TTL_SECONDS,
    )

    mode_1 = row[12].strip() if len(row) > 12 else ""  # M
    mode_2 = row[13].strip() if len(row) > 13 else ""  # N

    return mode_1, mode_2


def get_division_modes_for_streichmodus(div_number: int) -> list[str]:
    div_number = int(div_number)

    col_index = STREICHMODUS_MODE_COLUMNS.get(div_number)
    if not col_index:
        return []

    # Nicht über restinfo.WB gehen: diese Modul-Referenz kann nach einem
    # Reconnect None sein. Die zentrale Season-Verbindung ist die führende
    # Quelle für alle Player-/Streichmodus-Zugriffe.
    wb = get_season_spreadsheet()
    ws = get_worksheet_by_gid(
        wb,
        STREICHMODUS_CONFIG_WORKSHEET_GID,
    )

    values = col_values_cached(
        lambda: ws,
        sheet_name=player_sheet_name(ws, "StreichmodusConfig"),
        col=col_index,
        ttl_seconds=PLAYER_MODE_CACHE_TTL_SECONDS,
    )

    modes = []
    seen = set()

    ignored_headers = {
        "1. division",
        "2. division",
        "3. division",
        "4. division",
        "5. division",
        "6. division",
        "division 1",
        "division 2",
        "division 3",
        "division 4",
        "division 5",
        "division 6",
        "1.division",
        "2.division",
        "3.division",
        "4.division",
        "5.division",
        "6.division",
        "modus",
        "modis",
        "modes",
    }

    for value in values:
        mode = (value or "").strip()
        if not mode:
            continue

        lowered = mode.lower()
        if lowered in ignored_headers:
            continue

        key = lowered
        if key in seen:
            continue

        seen.add(key)
        modes.append(mode)

    return modes[:25]


def load_streichmodus_state_for_name_candidates(name_candidates: list[str]) -> dict:
    ws, row_index, div_number = get_division_worksheet_for_name_candidates(name_candidates)

    if ws is None or row_index is None or div_number is None:
        return {
            "found": False,
            "ws": None,
            "row_index": None,
            "div_number": None,
            "mode_1": "",
            "mode_2": "",
            "change_used": False,
        }

    row = row_values_cached(
        lambda: ws,
        sheet_name=player_sheet_name(ws),
        row=row_index,
        ttl_seconds=PLAYER_SHEET_CACHE_TTL_SECONDS,
    )

    mode_1 = row[12].strip() if len(row) > 12 else ""  # M
    mode_2 = row[13].strip() if len(row) > 13 else ""  # N
    change_marker = row[14].strip() if len(row) > 14 else ""  # O

    return {
        "found": True,
        "ws": ws,
        "row_index": row_index,
        "div_number": int(div_number),
        "mode_1": mode_1,
        "mode_2": mode_2,
        "change_used": change_marker == "1",
    }


def write_streichmodi_for_name_candidates(
    name_candidates: list[str],
    mode_1: str,
    mode_2: str,
) -> tuple[int, int]:
    """
    Rückwärtskompatible Funktion.
    Schreibt ohne Änderungslogik.
    Wird im neuen Flow nicht mehr direkt verwendet.
    """
    ws, row_index, div_number = get_division_worksheet_for_name_candidates(name_candidates)

    if ws is None or row_index is None or div_number is None:
        normalized = sorted({normalize_name(x) for x in name_candidates if x})
        raise RuntimeError(
            "Kein passender Name in Spalte L der Divisionen 1.DIV bis 6.DIV gefunden. "
            f"Gesucht: {', '.join(normalized) or '-'}"
        )

    reqs = [
        {"range": f"M{row_index}:M{row_index}", "values": [[mode_1]]},
        {"range": f"N{row_index}:N{row_index}", "values": [[mode_2]]},
    ]

    sheet_write_call(
        lambda: ws.batch_update(reqs),
        invalidate_prefixes=player_invalidate_prefixes(ws),
    )

    return row_index, div_number


def write_streichmodi_with_change_limit(
    name_candidates: list[str],
    mode_1: str,
    mode_2: str,
) -> dict:
    state = load_streichmodus_state_for_name_candidates(name_candidates)

    if not state["found"]:
        normalized = sorted({normalize_name(x) for x in name_candidates if x})
        raise RuntimeError(
            "Kein passender Name in Spalte L der Divisionen 1.DIV bis 6.DIV gefunden. "
            f"Gesucht: {', '.join(normalized) or '-'}"
        )

    ws = state["ws"]
    row_index = state["row_index"]
    div_number = state["div_number"]

    old_mode_1 = state["mode_1"]
    old_mode_2 = state["mode_2"]
    change_used = state["change_used"]

    old_has_both = bool(old_mode_1 and old_mode_2)
    changed = (old_mode_1 != mode_1) or (old_mode_2 != mode_2)

    if old_has_both and changed and change_used:
        raise RuntimeError(
            "Du hast deine einmalige Änderung der Streichmodi für diese Saison bereits genutzt."
        )

    reqs = [
        {"range": f"M{row_index}:M{row_index}", "values": [[mode_1]]},
        {"range": f"N{row_index}:N{row_index}", "values": [[mode_2]]},
    ]

    notify_change = False

    if old_has_both and changed:
        reqs.append(
            {
                "range": f"O{row_index}:O{row_index}",
                "values": [["1"]],
            }
        )
        notify_change = True

    sheet_write_call(
        lambda: ws.batch_update(reqs),
        invalidate_prefixes=player_invalidate_prefixes(ws),
    )

    return {
        "row_index": row_index,
        "div_number": div_number,
        "old_mode_1": old_mode_1,
        "old_mode_2": old_mode_2,
        "new_mode_1": mode_1,
        "new_mode_2": mode_2,
        "changed": changed,
        "notify_change": notify_change,
    }


# =========================================================
# QUALI INFO
# =========================================================

async def build_quali_info_text(member: discord.Member, quali_number: int) -> str:
    runner_name = member.display_name.strip()

    ws = await asyncio.to_thread(asnyc.get_quali_worksheet)

    total_played, rank = await asyncio.to_thread(
        asnyc.get_quali_stats_for_runner,
        ws,
        runner_name,
        quali_number,
    )

    if rank is None:
        return (
            f"Bereits gespielt: **{total_played}**\n"
            f"Du hast Quali {quali_number} aktuell noch nicht abgeschlossen."
        )

    return (
        f"Bereits gespielt: **{total_played}**\n"
        f"Dein aktueller Platz: **{rank}/{total_played}**"
    )


async def build_quali_overall_text(member: discord.Member) -> str:
    runner_name = member.display_name.strip()

    ws = await asyncio.to_thread(asnyc.get_quali_worksheet)

    total_completed, rank = await asyncio.to_thread(
        asnyc.get_overall_stats_for_runner,
        ws,
        runner_name,
    )

    if rank is None:
        return (
            f"Beide Qualis abgeschlossen: **{total_completed}**\n"
            f"Du bist aktuell noch nicht im Gesamtstand, weil dir mindestens eine Quali fehlt."
        )

    return (
        f"Beide Qualis abgeschlossen: **{total_completed}**\n"
        f"Dein aktueller Platz: **{rank}/{total_completed}**"
    )


# =========================================================
# BASIS
# =========================================================

class PlayerBaseView(discord.ui.View):
    def __init__(self, owner_id: int, timeout: float = 1800):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Dieses Menü gehört nicht dir.",
                ephemeral=True,
            )
            return False
        return True


class PlaceholderView(PlayerBaseView):
    def __init__(self, owner_id: int, back_view: discord.ui.View, back_embed: discord.Embed):
        super().__init__(owner_id)
        self.back_view = back_view
        self.back_embed = back_embed

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=0)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=self.back_embed,
            view=self.back_view,
            content=None,
        )


# =========================================================
# DASHBOARD: DIREKTE ERGEBNISEINGABE
# =========================================================


def _load_live_dashboard_result_match(
    division: int,
    row_index: int,
    expected_home: str,
    expected_away: str,
    name_candidates: list[str],
) -> dict:
    ws = get_player_division_worksheet(int(division))
    row = ws.row_values(int(row_index))

    home = row[3].strip() if len(row) > 3 else ""
    result = row[4].strip() if len(row) > 4 else ""
    away = row[5].strip() if len(row) > 5 else ""
    mode = row[2].strip() if len(row) > 2 else ""

    if not home or not away:
        raise ValueError("Die Begegnung wurde im Sheet nicht mehr gefunden.")

    if normalize_name(home) != normalize_name(expected_home) or normalize_name(away) != normalize_name(expected_away):
        raise ValueError("Die Begegnung hat sich im Sheet geändert. Bitte Dashboard aktualisieren.")

    targets = {normalize_name(value) for value in name_candidates if value}
    if normalize_name(home) not in targets and normalize_name(away) not in targets:
        raise PermissionError("Du bist kein Teilnehmer dieser Begegnung.")

    if not _is_open_result_value(result):
        return {
            "open": False,
            "result": result,
            "home": home,
            "away": away,
            "mode": mode,
        }

    if not mode:
        raise ValueError("Für dieses Spiel ist noch kein Modus eingetragen.")

    return {
        "open": True,
        "result": result,
        "home": home,
        "away": away,
        "mode": mode,
    }


class BackToDashboardFromDirectResultButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Schließen", style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction):
        await close_player_panel(interaction)


class EnhancedLeagueResultSubmitButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Absenden", style=discord.ButtonStyle.success, row=2)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, LeagueResultViewStep2): return
        s = view.state
        if not all([s.division, s.match_row_index, s.player1, s.player2, s.mode, s.winner_value, s.racetime_link]):
            await interaction.response.send_message("Es fehlen noch Angaben.", ephemeral=True); return
        await interaction.response.defer()
        try:
            result = result_league_from_value(s.winner_value); timestamp = now_berlin_str()
            await asyncio.to_thread(write_league_result, s.match_row_index, s.mode, result, s.racetime_link, interaction.user.display_name, timestamp, s.division)
            post_text = league_result_post_text(s.division, timestamp, s.player1, s.player2, result, s.mode, s.racetime_link)
            if interaction.guild: await send_result_post(interaction.guild, post_text)
            await interaction.edit_original_response(content=f"✅ Ergebnis gespeichert:\n{post_text}", view=None)
        except Exception as exc:
            traceback.print_exc(); await interaction.edit_original_response(content=f"❌ Fehler beim Speichern: {exc}", view=view)


class DashboardLeagueResultViewStep2(LeagueResultViewStep2):
    def __init__(self, author_id: int, state):
        super().__init__(cog=None, author_id=author_id, state=state)

        for item in list(self.children):
            if isinstance(item, discord.ui.Button) and item.label in {"Zurück", "Absenden"}:
                self.remove_item(item)

        self.add_item(EnhancedLeagueResultSubmitButton())
        self.add_item(BackToDashboardFromDirectResultButton())


async def _open_direct_dashboard_result(
    interaction: discord.Interaction,
    *,
    division: int,
    row_index: int,
    expected_home: str,
    expected_away: str,
    owner_id: int,
):
    if interaction.user.id != owner_id:
        await interaction.response.send_message(
            "Dieses Dashboard gehört nicht dir.",
            ephemeral=True,
        )
        return

    # Wir bestätigen nur den Klick am Dashboard. Die Ergebniseingabe erscheint
    # anschließend als eigenes ephemeres Panel, damit das Dashboard stehen bleibt.
    await interaction.response.defer()

    if not isinstance(interaction.user, discord.Member):
        await interaction.followup.send(
            "Diese Funktion ist nur auf dem TFL-Server verfügbar.",
            ephemeral=True,
        )
        return

    try:
        live = await asyncio.to_thread(
            _load_live_dashboard_result_match,
            division,
            row_index,
            expected_home,
            expected_away,
            get_name_candidates(interaction.user),
        )
    except PermissionError as exc:
        await interaction.followup.send(f"⛔ {exc}", ephemeral=True)
        return
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        return

    if not live.get("open"):
        await interaction.followup.send(
            f"Dieses Spiel wurde bereits gewertet: **{live.get('result') or '-'}**",
            ephemeral=True,
        )
        return

    state = MatchCenterState()
    state.kind = "Ergebnis League"
    state.division = f"Div {division}"
    state.home_player = live["home"]
    state.match_label = f"{live['home']} vs. {live['away']}"
    state.match_row_index = row_index
    state.player1 = live["home"]
    state.player2 = live["away"]
    state.mode = live["mode"]

    view = DashboardLeagueResultViewStep2(
        author_id=interaction.user.id,
        state=state,
    )

    await interaction.followup.send(
        content=(
            "## ✅ Ergebnis eintragen\n"
            f"**{live['home']} vs. {live['away']}**\n"
            f"Modus: **{live['mode']}**\n\n"
            "Wähle das Ergebnis und hinterlege anschließend den Racetime-Link."
        ),
        view=view,
        ephemeral=True,
    )


class CoopResultSelect(discord.ui.Select):
    def __init__(self, home: str, away: str):
        super().__init__(placeholder="Ergebnis auswählen …", options=[discord.SelectOption(label=f"{home} gewinnt"[:100], value="1:0"), discord.SelectOption(label="Remis", value="1:1"), discord.SelectOption(label=f"{away} gewinnt"[:100], value="0:1")], row=0)
    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if isinstance(view, CoopDirectResultView): view.result_value = self.values[0]; await interaction.response.edit_message(content=view.render_text(), view=view)


class CoopResultLinkModal(discord.ui.Modal, title="Racetime-Link"):
    link_input = discord.ui.TextInput(label="Racetime-Link", placeholder="https://racetime.gg/...", required=True, max_length=300)
    def __init__(self, parent_view): super().__init__(); self.parent_view = parent_view
    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.racetime_link = str(self.link_input.value or "").strip(); await interaction.response.edit_message(content=self.parent_view.render_text(), view=self.parent_view)


class CoopDirectResultView(discord.ui.View):
    def __init__(self, *, owner_id: int, team_name: str, match: dict):
        super().__init__(timeout=900); self.owner_id = int(owner_id); self.team_name = team_name; self.match = match; self.result_value = None; self.racetime_link = None; self.add_item(CoopResultSelect(match["home"], match["away"]))
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id: await interaction.response.send_message("Diese Ergebniseingabe gehört nicht dir.", ephemeral=True); return False
        return True
    def render_text(self) -> str:
        return f"## 👥 Coop-Ergebnis eintragen\n**{self.match['home']} vs. {self.match['away']}**\n🎮 **{self.match.get('mode') or 'Coop'}**\n\n**Ergebnis:** {self.result_value or 'noch nicht gewählt'}\n**Racetime:** {self.racetime_link or 'noch nicht hinterlegt'}"
    @discord.ui.button(label="Racetime-Link", style=discord.ButtonStyle.secondary, row=1)
    async def link_button(self, interaction: discord.Interaction, button: discord.ui.Button): await interaction.response.send_modal(CoopResultLinkModal(self))
    @discord.ui.button(label="Absenden", style=discord.ButtonStyle.success, row=1)
    async def submit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.result_value or not self.racetime_link: await interaction.response.send_message("Bitte Ergebnis und Racetime-Link vollständig angeben.", ephemeral=True); return
        await interaction.response.defer()
        try:
            live = await asyncio.to_thread(_load_live_coop_result_match, int(self.match["row"]), self.match["home"], self.match["away"], self.team_name)
            if not live.get("open"): await interaction.edit_original_response(content=f"Dieses Coop-Spiel wurde bereits gewertet: **{live.get('result') or '-'}**", view=None); return
            await asyncio.to_thread(_write_coop_result, int(self.match["row"]), self.result_value, self.racetime_link, interaction.user.display_name)
            timestamp = dt.now(BERLIN_TZ).strftime("%d.%m.%Y %H:%M")
            post_text = f"👥 **Coop League – Ergebnis**\n**{live['home']} {self.result_value} {live['away']}**\n🎮 {live.get('mode') or 'Coop'}\n🕒 {timestamp}\n🔗 {self.racetime_link}"
            if interaction.guild: await send_result_post(interaction.guild, post_text)
            await interaction.edit_original_response(content=f"✅ Coop-Ergebnis gespeichert:\n{post_text}", view=None)
        except Exception as exc:
            traceback.print_exc(); await interaction.edit_original_response(content=f"❌ Coop-Ergebnis konnte nicht gespeichert werden: {exc}", view=self)
    @discord.ui.button(label="Schließen", style=discord.ButtonStyle.secondary, row=1)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button): await close_player_panel(interaction)


async def _open_direct_coop_result(interaction: discord.Interaction, *, owner_id: int, team_name: str, match: dict):
    if interaction.user.id != owner_id: await interaction.response.send_message("Dieses Dashboard gehört nicht dir.", ephemeral=True); return
    await interaction.response.defer()
    try: live = await asyncio.to_thread(_load_live_coop_result_match, int(match.get("row") or 0), str(match.get("home") or ""), str(match.get("away") or ""), team_name)
    except PermissionError as exc: await interaction.followup.send(f"⛔ {exc}", ephemeral=True); return
    except Exception as exc: await interaction.followup.send(f"❌ {exc}", ephemeral=True); return
    if not live.get("open"): await interaction.followup.send(f"Dieses Coop-Spiel wurde bereits gewertet: **{live.get('result') or '-'}**", ephemeral=True); return
    payload = dict(match); payload.update(live); payload["row"] = int(match.get("row") or 0)
    view = CoopDirectResultView(owner_id=owner_id, team_name=team_name, match=payload)
    await interaction.followup.send(content=view.render_text(), view=view, ephemeral=True)


class CoopFallbackDashboardView(PlayerBaseView):
    def __init__(self, owner_id: int): super().__init__(owner_id)
    @discord.ui.button(label="⚔️ League", style=discord.ButtonStyle.primary, row=0)
    async def league_button(self, interaction: discord.Interaction, button: discord.ui.Button): await show_player_dashboard(interaction, dashboard_mode="league")
    @discord.ui.button(label="👥 Coop-Menü", style=discord.ButtonStyle.success, row=0)
    async def coop_button(self, interaction: discord.Interaction, button: discord.ui.Button): await coop.open_coop_menu_from_player(interaction)
    @discord.ui.button(label="🔄 Aktualisieren", style=discord.ButtonStyle.secondary, row=0)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button): await show_player_dashboard(interaction, dashboard_mode="coop", force_refresh=True)


class DashboardResultButton(discord.ui.Button):
    """Legacy/Fallback-Button für das klassische View-Dashboard."""

    def __init__(self, match: dict, division: int):
        opponent = match.get("opponent") or "Gegner"
        super().__init__(
            label=f"✅ Ergebnis vs. {opponent}"[:80],
            style=discord.ButtonStyle.success,
            row=4,
        )
        self.division = int(division)
        self.row_index = int(match.get("row") or 0)
        self.expected_home = str(match.get("home") or "")
        self.expected_away = str(match.get("away") or "")

    async def callback(self, interaction: discord.Interaction):
        await _open_direct_dashboard_result(
            interaction,
            division=self.division,
            row_index=self.row_index,
            expected_home=self.expected_home,
            expected_away=self.expected_away,
            owner_id=interaction.user.id,
        )


def _truncate_dashboard_name(value: str, max_len: int) -> str:
    value = str(value or "").strip()
    if max_len <= 1:
        return value[:max_len]
    if len(value) <= max_len:
        return value
    return value[: max_len - 1] + "…"


def _dashboard_progress_bar(played: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "░" * width
    ratio = max(0.0, min(1.0, played / total))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


def _dashboard_achievement_label(item: dict) -> str:
    key = str(item.get("key") or "")
    division = str(item.get("division") or "").strip()
    if key == "season_first_finisher":
        return "🏆 Season First Finisher"
    if key.startswith("division_first_finisher_d"):
        div = division or key.rsplit("d", 1)[-1]
        return f"🏁 Division {div} First Finisher"
    if key == "season_first_scheduled_match":
        return "📅 First Planner"
    if key == "season_opening_match":
        return "🚩 Season Opening Match"
    if key.startswith("division_opening_match_d"):
        div = division or key.rsplit("d", 1)[-1]
        return f"🚩 Division {div} Opening Match"
    if key.startswith("win_streak_"):
        return f"🔥 Winning Streak {key.rsplit('_', 1)[-1]}"
    return str(item.get("label") or key or "Achievement")


def _dashboard_table_markdown(data: dict) -> str:
    division_table = data.get("division_table") or []
    if not division_table:
        return "Noch keine Tabellendaten verfügbar."

    raw_name_width = max(12, min(18, max(len(str(item.get("name") or "")) for item in division_table[:9])))
    display_width = raw_name_width + 2  # Marker + Leerzeichen
    header = (
        f"{'Pl':>2}  {'Spieler':<{display_width}}  "
        f"{'Sp':>2}  {'S':>2}  {'U':>2}  {'N':>2}  {'Pkt':>3}"
    )
    separator = (
        f"{'--':>2}  {'-' * display_width}  "
        f"{'--':>2}  {'--':>2}  {'--':>2}  {'--':>2}  {'---':>3}"
    )
    lines = [header, separator]

    for item in division_table[:9]:
        short_name = _truncate_dashboard_name(item.get("name") or "", raw_name_width)
        marker = "▶" if item.get("is_self") else " "
        display_name = f"{marker} {short_name:<{raw_name_width}}"
        if item.get("is_self"):
            display_name = f"\u001b[1;33m{display_name}\u001b[0m"
        lines.append(
            f"{item['rank']:>2}. "
            f"{display_name}  "
            f"{item['played']:>2}  "
            f"{item['wins']:>2}  "
            f"{item['draws']:>2}  "
            f"{item['losses']:>2}  "
            f"{item['points']:>3}"
        )

    return "```ansi\n" + "\n".join(lines) + "\n```"


def _deadline_accent(deadline: dict) -> int:
    emoji = deadline.get("emoji")
    if emoji == "🟢":
        return 0x2ECC71
    if emoji == "🟡":
        return 0xF1C40F
    if emoji == "🔴":
        return 0xE74C3C
    return 0x5865F2


if HAS_COMPONENTS_V2:
    class DashboardV2ResultButton(discord.ui.Button):
        def __init__(self, match: dict, division: int, owner_id: int, *, compact: bool = False):
            opponent = str(match.get("opponent") or "Gegner")
            label = "✅ Ergebnis" if compact else f"✅ Ergebnis vs. {opponent}"
            super().__init__(
                label=label[:80],
                style=discord.ButtonStyle.success,
                custom_id=f"tfl:dashboard:result:{division}:{int(match.get('row') or 0)}:{owner_id}"[:100],
            )
            self.owner_id = int(owner_id)
            self.division = int(division)
            self.row_index = int(match.get("row") or 0)
            self.expected_home = str(match.get("home") or "")
            self.expected_away = str(match.get("away") or "")

        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("Dieses Dashboard gehört nicht dir.", ephemeral=True)
                return
            await _open_direct_dashboard_result(
                interaction,
                division=self.division,
                row_index=self.row_index,
                expected_home=self.expected_home,
                expected_away=self.expected_away,
                owner_id=self.owner_id,
            )


    class DashboardV2CoopResultButton(discord.ui.Button):
        def __init__(self, *, match: dict, team_name: str, owner_id: int, compact: bool = False):
            opponent = str(match.get("opponent") or "Gegner")
            label = "✅ Ergebnis" if compact else f"✅ Ergebnis vs. {opponent}"
            super().__init__(
                label=label[:80],
                style=discord.ButtonStyle.success,
                custom_id=f"tfl:dashboard:coopresult:{int(match.get('row') or 0)}:{owner_id}"[:100],
            )
            self.owner_id = int(owner_id)
            self.team_name = team_name
            self.match = dict(match)

        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("Dieses Dashboard gehört nicht dir.", ephemeral=True)
                return
            await _open_direct_coop_result(
                interaction,
                owner_id=self.owner_id,
                team_name=self.team_name,
                match=self.match,
            )


    class DashboardModeSwitchButton(discord.ui.Button):
        def __init__(self, *, owner_id: int, target_mode: str, active: bool = False):
            target_mode = "coop" if target_mode == "coop" else "league"
            label = "👥 COOP" if target_mode == "coop" else "⚔️ LEAGUE"
            style = discord.ButtonStyle.success if target_mode == "coop" else discord.ButtonStyle.primary
            super().__init__(
                label=label,
                style=style,
                disabled=bool(active),
                custom_id=f"tfl:dashboard:switch:{target_mode}:{owner_id}"[:100],
            )
            self.owner_id = int(owner_id)
            self.target_mode = target_mode

        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("Dieses Dashboard gehört nicht dir.", ephemeral=True)
                return
            await show_player_dashboard(interaction, dashboard_mode=self.target_mode)


    class DashboardNextMatchSelect(discord.ui.Select):
        def __init__(self, *, matches: list[dict], owner_id: int, state: dict, division: int):
            self.owner_id = int(owner_id)
            self.state = state
            self.matches = {
                int(m.get("row") or 0): dict(m)
                for m in matches
                if int(m.get("row") or 0) > 0
            }
            selected_row = int(state.get("row") or 0)
            options = []
            for match in matches[:25]:
                row = int(match.get("row") or 0)
                when = match.get("datetime")
                when_text = when.strftime("%d.%m. · %H:%M") if when else str(match.get("date_text") or "Termin")
                opponent = str(match.get("opponent") or "Gegner")
                mode = str(match.get("mode") or "Modus noch offen")
                options.append(
                    discord.SelectOption(
                        label=f"{when_text} · {opponent}"[:100],
                        value=str(row),
                        description=mode[:100],
                        default=row == selected_row,
                    )
                )
            super().__init__(
                placeholder="Nächstes Spiel auswählen …",
                min_values=1,
                max_values=1,
                options=options,
                custom_id=f"tfl:dashboard:nextmatch:select:{division}:{owner_id}"[:100],
            )

        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("Dieses Dashboard gehört nicht dir.", ephemeral=True)
                return
            selected = int(self.values[0])
            self.state["row"] = selected
            for option in self.options:
                option.default = option.value == str(selected)
            await interaction.response.defer()


    class DashboardNextGameActionButton(discord.ui.Button):
        def __init__(
            self,
            *,
            owner_id: int,
            division: int,
            matches: list[dict],
            state: dict,
            action: str,
        ):
            labels = {
                "result": "✅ Ergebnis",
                "reschedule": "🔄 Spiel verschieben",
                "cancel": "❌ Spiel absagen",
            }
            styles = {
                "result": discord.ButtonStyle.success,
                "reschedule": discord.ButtonStyle.primary,
                "cancel": discord.ButtonStyle.danger,
            }
            super().__init__(
                label=labels[action],
                style=styles[action],
                custom_id=f"tfl:dashboard:nextmatch:{action}:{division}:{owner_id}"[:100],
            )
            self.owner_id = int(owner_id)
            self.division = int(division)
            self.matches = {
                int(m.get("row") or 0): dict(m)
                for m in matches
                if int(m.get("row") or 0) > 0
            }
            self.state = state
            self.action = action

        def _selected_match(self) -> dict | None:
            row = int(self.state.get("row") or 0)
            if row in self.matches:
                return self.matches[row]
            return next(iter(self.matches.values()), None)

        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("Dieses Dashboard gehört nicht dir.", ephemeral=True)
                return

            match = self._selected_match()
            if not match:
                await interaction.response.send_message(
                    "Das ausgewählte Spiel ist nicht mehr verfügbar.",
                    ephemeral=True,
                )
                return

            common = {
                "division": self.division,
                "row_index": int(match.get("row") or 0),
                "expected_home": str(match.get("home") or ""),
                "expected_away": str(match.get("away") or ""),
            }

            if self.action == "result":
                await _open_direct_dashboard_result(
                    interaction,
                    owner_id=self.owner_id,
                    **common,
                )
                return

            if self.action == "reschedule":
                await term_offers.open_schedule_reschedule_match(
                    interaction,
                    **common,
                )
                return

            if self.action == "cancel":
                await term_offers.request_schedule_cancel_match(
                    interaction,
                    **common,
                )
                return


    class DashboardMatchdaySelect(discord.ui.Select):
        def __init__(self, *, matches: list[dict], owner_id: int, state: dict, prefix: str):
            self.owner_id = int(owner_id)
            self.state = state
            self.matches = {int(m.get("row") or 0): dict(m) for m in matches if int(m.get("row") or 0) > 0}
            options = []
            selected_row = int(state.get("row") or 0)
            for match in matches[:25]:
                row = int(match.get("row") or 0)
                when = match.get("datetime")
                time_text = when.strftime("%H:%M") if when else "?"
                home = str(match.get("home") or "?")
                away = str(match.get("away") or "?")
                mode = str(match.get("mode") or "Modus noch offen")
                options.append(
                    discord.SelectOption(
                        label=f"{time_text} · {home} vs. {away}"[:100],
                        value=str(row),
                        description=mode[:100],
                        default=row == selected_row,
                    )
                )
            super().__init__(
                placeholder="Heutiges Match auswählen …",
                min_values=1,
                max_values=1,
                options=options,
                custom_id=f"tfl:dashboard:{prefix}:select:{owner_id}"[:100],
            )

        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("Dieses Dashboard gehört nicht dir.", ephemeral=True)
                return
            selected = int(self.values[0])
            self.state["row"] = selected
            for option in self.options:
                option.default = option.value == str(selected)
            await interaction.response.defer()


    class DashboardMatchdayActionButton(discord.ui.Button):
        def __init__(
            self,
            *,
            owner_id: int,
            division: int,
            matches: list[dict],
            state: dict,
            action: str,
        ):
            labels = {
                "result": "✅ Ergebnis",
                "stream": "📺 Multistream",
                "schedule": "🗓️ Termin",
            }
            styles = {
                "result": discord.ButtonStyle.success,
                "stream": discord.ButtonStyle.primary,
                "schedule": discord.ButtonStyle.secondary,
            }
            super().__init__(
                label=labels[action],
                style=styles[action],
                custom_id=f"tfl:dashboard:matchday:{action}:{division}:{owner_id}"[:100],
            )
            self.owner_id = int(owner_id)
            self.division = int(division)
            self.matches = {int(m.get("row") or 0): dict(m) for m in matches if int(m.get("row") or 0) > 0}
            self.state = state
            self.action = action

        def _selected_match(self) -> dict | None:
            row = int(self.state.get("row") or 0)
            if row in self.matches:
                return self.matches[row]
            return next(iter(self.matches.values()), None)

        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("Dieses Dashboard gehört nicht dir.", ephemeral=True)
                return
            match = self._selected_match()
            if not match:
                await interaction.response.send_message("Das ausgewählte Match ist nicht mehr verfügbar.", ephemeral=True)
                return

            if self.action == "result":
                await _open_direct_dashboard_result(
                    interaction,
                    division=self.division,
                    row_index=int(match.get("row") or 0),
                    expected_home=str(match.get("home") or ""),
                    expected_away=str(match.get("away") or ""),
                    owner_id=self.owner_id,
                )
                return

            if self.action == "schedule":
                await term_offers.open_schedule_manage_match(
                    interaction,
                    division=self.division,
                    row_index=int(match.get("row") or 0),
                    expected_home=str(match.get("home") or ""),
                    expected_away=str(match.get("away") or ""),
                )
                return

            if self.action == "stream":
                await interaction.response.defer(ephemeral=True)
                try:
                    url = await asyncio.to_thread(
                        matchcenter.build_multistream_url,
                        str(match.get("home") or ""),
                        str(match.get("away") or ""),
                    )
                    if str(url).lower().startswith(("http://", "https://")):
                        await interaction.edit_original_response(
                            content=(
                                f"📺 **{match.get('home') or '?'} vs. {match.get('away') or '?'}**\n"
                                f"<{url}>"
                            )
                        )
                    else:
                        await interaction.edit_original_response(
                            content="Für diese Begegnung ist aktuell kein Twitch-/Multistream-Link hinterlegt."
                        )
                except Exception as exc:
                    await interaction.edit_original_response(content=f"Streamlink konnte nicht geladen werden: {exc}")
                return


    class DashboardCoopMatchdayActionButton(discord.ui.Button):
        def __init__(self, *, owner_id: int, team_name: str, matches: list[dict], state: dict, action: str):
            labels = {"result": "✅ Ergebnis", "stream": "📺 Stream", "menu": "👥 Coop-Menü"}
            styles = {"result": discord.ButtonStyle.success, "stream": discord.ButtonStyle.primary, "menu": discord.ButtonStyle.secondary}
            super().__init__(
                label=labels[action],
                style=styles[action],
                custom_id=f"tfl:dashboard:coopmatchday:{action}:{owner_id}"[:100],
            )
            self.owner_id = int(owner_id)
            self.team_name = team_name
            self.matches = {int(m.get("row") or 0): dict(m) for m in matches if int(m.get("row") or 0) > 0}
            self.state = state
            self.action = action

        def _selected_match(self):
            row = int(self.state.get("row") or 0)
            return self.matches.get(row) or next(iter(self.matches.values()), None)

        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("Dieses Dashboard gehört nicht dir.", ephemeral=True)
                return
            match = self._selected_match()
            if self.action == "menu":
                await interaction.response.send_message(
                    embed=coop.menu_embed("🤝 Coop League", "Wähle einen Bereich."),
                    view=coop.CoopMenuView(owner_id=interaction.user.id),
                    ephemeral=True,
                )
                return
            if not match:
                await interaction.response.send_message("Das ausgewählte Match ist nicht mehr verfügbar.", ephemeral=True)
                return
            if self.action == "result":
                await _open_direct_coop_result(
                    interaction,
                    owner_id=self.owner_id,
                    team_name=self.team_name,
                    match=match,
                )
                return
            link = str(match.get("link") or "").strip()
            if link.lower().startswith(("http://", "https://")):
                await interaction.response.send_message(
                    f"📺 **{match.get('home') or '?'} vs. {match.get('away') or '?'}**\n<{link}>",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message("Für dieses Coop-Match ist aktuell kein Streamlink hinterlegt.", ephemeral=True)


    class DashboardV2ActionButton(discord.ui.Button):
        def __init__(
            self,
            *,
            owner_id: int,
            action: str,
            label: str,
            style: discord.ButtonStyle = discord.ButtonStyle.secondary,
            dashboard_mode: str = "league",
        ):
            super().__init__(
                label=label[:80],
                style=style,
                custom_id=f"tfl:dashboard:{action}:{dashboard_mode}:{owner_id}"[:100],
            )
            self.owner_id = int(owner_id)
            self.action = action
            self.dashboard_mode = dashboard_mode

        async def _send_panel(self, interaction, *, embed, view):
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("Dieses Dashboard gehört nicht dir.", ephemeral=True)
                return

            a = self.action
            if a == "plan":
                await self._send_panel(
                    interaction,
                    embed=menu_embed("🎮 Spiel planen", "Wähle den Bereich für deine Spielplanung."),
                    view=PlanMenuView(owner_id=interaction.user.id),
                )
                return
            if a == "offer":
                await term_offers.open_term_offer_modal(interaction)
                return
            if a == "result":
                await self._send_panel(
                    interaction,
                    embed=menu_embed("✅ Ergebnis melden", "Wähle League oder Cup."),
                    view=ResultMenuView(owner_id=interaction.user.id),
                )
                return
            if a == "schedule_manage":
                await term_offers.open_schedule_manage_menu(interaction)
                return
            if a == "my_offers":
                await term_offers.open_my_offers_menu(interaction)
                return
            if a == "info":
                await self._send_panel(
                    interaction,
                    embed=menu_embed("ℹ️ Info & Tabelle", "Saisoninfos, Meldestatus, Tabellen und weitere Übersichten."),
                    view=InfoMenuView(owner_id=interaction.user.id),
                )
                return
            if a == "rest":
                await self._send_panel(
                    interaction,
                    embed=menu_embed("📋 Restprogramm", "Zeige dein eigenes Restprogramm oder das eines anderen Spielers."),
                    view=RestprogrammView(owner_id=interaction.user.id),
                )
                return
            if a == "quali":
                cog = interaction.client.get_cog("QualiCog")
                if cog is None or not hasattr(cog, "start_quali_flow"):
                    await interaction.response.send_message("Qualifikation ist aktuell nicht verfügbar.", ephemeral=True)
                    return
                await cog.start_quali_flow(interaction, edit_existing=False)
                return
            if a == "season":
                await self._send_panel(
                    interaction,
                    embed=menu_embed("📝 Saisonmeldung", "Wähle den Bereich für deine Saisonmeldung."),
                    view=SeasonSignupMenuView(owner_id=interaction.user.id),
                )
                return
            if a == "async":
                await self._send_panel(
                    interaction,
                    embed=menu_embed("⚡ Async", "Beantrage oder spiele ein Async-Match."),
                    view=AsyncMenuView(owner_id=interaction.user.id),
                )
                return
            if a == "settings":
                await self._send_panel(
                    interaction,
                    embed=menu_embed("⚙️ Einstellungen", "Verwalte Twitch, Restream-Angaben und Streichmodi."),
                    view=SettingsMenuView(owner_id=interaction.user.id),
                )
                return
            if a == "achievements":
                await interaction.response.defer(ephemeral=True)
                data = await get_player_dashboard_data(interaction.user)
                achievements = data.get("achievements") or []
                if achievements:
                    lines = []
                    for item in achievements:
                        label = _dashboard_achievement_label(item)
                        awarded = str(item.get("awarded_at") or "").strip()
                        lines.append(f"• **{label}**" + (f" · {awarded}" if awarded else ""))
                    description = "\n".join(lines)
                else:
                    description = f"In {CURRENT_SEASON_LABEL} hast du noch kein Achievement erhalten."
                embed = discord.Embed(
                    title=f"🏅 Meine Achievements · {CURRENT_SEASON_LABEL}",
                    description=description,
                    color=0xF1C40F,
                )
                await interaction.edit_original_response(embed=embed, view=None, content=None)
                return
            if a == "more":
                await self._send_panel(
                    interaction,
                    embed=menu_embed("➕ Weitere Funktionen", "Seltener benötigte Spieler- und Saisonfunktionen."),
                    view=DashboardMoreActionsView(
                        owner_id=interaction.user.id,
                        show_admin=has_admin_role(interaction.user),
                    ),
                )
                return
            if a == "refresh":
                await show_player_dashboard(
                    interaction,
                    force_refresh=True,
                    dashboard_mode=self.dashboard_mode,
                )
                return
            if a == "coop_menu":
                await interaction.response.send_message(
                    embed=coop.menu_embed("🤝 Coop League", "Wähle einen Bereich."),
                    view=coop.CoopMenuView(owner_id=interaction.user.id),
                    ephemeral=True,
                )
                return
            if a == "exit":
                await self._send_panel(
                    interaction,
                    embed=discord.Embed(
                        title="⚠️ Liga verlassen?",
                        description=(
                            "Bist du dir absolut sicher, dass du die Liga verlassen möchtest? "
                            "Wenn du nun bestätigst, ist die Entscheidung final. Zudem ist eine "
                            "Teilnahme an der kommenden Saison damit ausgeschlossen."
                        ),
                        color=TFL_DANGER_COLOR,
                    ),
                    view=PlayerExitConfirmView(owner_id=interaction.user.id),
                )
                return
            if a == "admin":
                if not has_admin_role(interaction.user):
                    await interaction.response.send_message("⛔ Diese Funktion ist nur für Admins verfügbar.", ephemeral=True)
                    return
                await self._send_panel(
                    interaction,
                    embed=menu_embed("🟨 Administration", "Wähle eine Adminfunktion."),
                    view=AdminMenuView(owner_id=interaction.user.id),
                )
                return
            await interaction.response.send_message("Diese Dashboard-Aktion ist aktuell nicht verfügbar.", ephemeral=True)


    class DashboardMoreActionsView(discord.ui.View):
        def __init__(self, *, owner_id: int, show_admin: bool):
            super().__init__(timeout=300)
            self.owner_id = int(owner_id)
            self.add_item(DashboardV2ActionButton(owner_id=self.owner_id, action="my_offers", label="📌 Meine Angebote"))
            self.add_item(DashboardV2ActionButton(owner_id=self.owner_id, action="schedule_manage", label="🗓️ Termine verwalten"))
            self.add_item(DashboardV2ActionButton(owner_id=self.owner_id, action="achievements", label="🏅 Meine Achievements", style=discord.ButtonStyle.primary))
            self.add_item(DashboardV2ActionButton(owner_id=self.owner_id, action="info", label="ℹ️ Saisoninfos", style=discord.ButtonStyle.primary))
            self.add_item(DashboardV2ActionButton(owner_id=self.owner_id, action="quali", label="🏆 Qualifikation", style=discord.ButtonStyle.primary))
            self.add_item(DashboardV2ActionButton(owner_id=self.owner_id, action="season", label="📝 Saisonmeldung", style=discord.ButtonStyle.primary))
            self.add_item(DashboardV2ActionButton(owner_id=self.owner_id, action="async", label="⚡ Async", style=discord.ButtonStyle.primary))
            self.add_item(DashboardV2ActionButton(owner_id=self.owner_id, action="settings", label="⚙️ Einstellungen"))
            self.add_item(DashboardV2ActionButton(owner_id=self.owner_id, action="exit", label="🚪 Liga verlassen", style=discord.ButtonStyle.danger))
            if show_admin:
                self.add_item(DashboardV2ActionButton(owner_id=self.owner_id, action="admin", label="🟨 Administration"))

        async def interaction_check(self, interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("Dieses Menü gehört nicht dir.", ephemeral=True)
                return False
            return True


    class DashboardLayoutView(discord.ui.LayoutView):
        def __init__(
            self,
            *,
            data: dict,
            owner_id: int,
            show_admin: bool,
            note: str | None = None,
            dashboard_mode: str = "league",
        ):
            super().__init__(timeout=900)
            self.owner_id = int(owner_id)
            self.dashboard_mode = "coop" if dashboard_mode == "coop" else "league"
            self.banner_file = None
            self._build(data, show_admin=show_admin, note=note)

        async def interaction_check(self, interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("Dieses Dashboard gehört nicht dir.", ephemeral=True)
                return False
            return True

        def _add_banner(self):
            gallery = discord.ui.MediaGallery()
            try:
                if TFL_DASHBOARD_BANNER_URL:
                    gallery.add_item(media=TFL_DASHBOARD_BANNER_URL, description="TFL Spielerbereich")
                    self.add_item(gallery)
                    return
                if TFL_DASHBOARD_BANNER_PATH and os.path.isfile(TFL_DASHBOARD_BANNER_PATH):
                    self.banner_file = discord.File(TFL_DASHBOARD_BANNER_PATH, filename=DASHBOARD_BANNER_FILENAME)
                    gallery.add_item(media=f"attachment://{DASHBOARD_BANNER_FILENAME}", description="TFL Spielerbereich")
                    self.add_item(gallery)
            except Exception as exc:
                self.banner_file = None
                print(f"⚠️ [PLAYER DASHBOARD] V2-Banner konnte nicht geladen werden: {exc}")

        def _add_header(self, title: str, subtitle: str, note: str | None):
            card = discord.ui.Container(accent_colour=0x5865F2 if self.dashboard_mode == "league" else 0x2ECC71)
            body = f"## {title}\n{subtitle}"
            if note:
                body += f"\n> {note}"
            card.add_item(discord.ui.TextDisplay(body))
            card.add_item(
                discord.ui.ActionRow(
                    DashboardModeSwitchButton(
                        owner_id=self.owner_id,
                        target_mode="league",
                        active=self.dashboard_mode == "league",
                    ),
                    DashboardModeSwitchButton(
                        owner_id=self.owner_id,
                        target_mode="coop",
                        active=self.dashboard_mode == "coop",
                    ),
                )
            )
            self.add_item(card)

        def _add_matchday(self, data: dict, division: int):
            matches = (data.get("today_matches") or [])[:25]
            if not matches:
                return

            state = {"row": int(matches[0].get("row") or 0)}
            card = discord.ui.Container(accent_colour=0xE67E22)
            lines = [
                "## 🔥 MATCHDAY",
                "-# Alle heutigen Ligaspiele · Match auswählen, dann Aktion starten",
            ]
            for match in matches:
                when = match.get("datetime")
                time_text = when.strftime("%H:%M") if when else "?"
                lines.append(
                    f"**{time_text} Uhr** · **{match.get('home') or '?'} vs. {match.get('away') or '?'}** "
                    f"· 🎮 **{match.get('mode') or 'Modus noch offen'}**"
                )
            card.add_item(discord.ui.TextDisplay("\n".join(lines)))
            card.add_item(
                discord.ui.ActionRow(
                    DashboardMatchdaySelect(
                        matches=matches,
                        owner_id=self.owner_id,
                        state=state,
                        prefix=f"matchday:{division}",
                    )
                )
            )
            card.add_item(
                discord.ui.ActionRow(
                    DashboardMatchdayActionButton(
                        owner_id=self.owner_id,
                        division=division,
                        matches=matches,
                        state=state,
                        action="result",
                    ),
                    DashboardMatchdayActionButton(
                        owner_id=self.owner_id,
                        division=division,
                        matches=matches,
                        state=state,
                        action="stream",
                    ),
                    DashboardMatchdayActionButton(
                        owner_id=self.owner_id,
                        division=division,
                        matches=matches,
                        state=state,
                        action="schedule",
                    ),
                )
            )
            self.add_item(card)

        def _add_league_actions(self, show_admin: bool):
            card = discord.ui.Container(accent_colour=0x5865F2)
            card.add_item(discord.ui.TextDisplay("## ⚙️ Schnellaktionen\n-# Die häufigsten Funktionen direkt erreichbar"))
            card.add_item(
                discord.ui.ActionRow(
                    DashboardV2ActionButton(
                        owner_id=self.owner_id,
                        action="plan",
                        label="🎮 Spiel planen",
                        style=discord.ButtonStyle.primary,
                    ),
                    DashboardV2ActionButton(
                        owner_id=self.owner_id,
                        action="offer",
                        label="📅 Termine anbieten",
                        style=discord.ButtonStyle.success,
                    ),
                    DashboardV2ActionButton(
                        owner_id=self.owner_id,
                        action="result",
                        label="✅ Ergebnis",
                        style=discord.ButtonStyle.success,
                    ),
                )
            )
            card.add_item(
                discord.ui.ActionRow(
                    DashboardV2ActionButton(
                        owner_id=self.owner_id,
                        action="rest",
                        label="📋 Restprogramm",
                        style=discord.ButtonStyle.primary,
                    ),
                    DashboardV2ActionButton(
                        owner_id=self.owner_id,
                        action="more",
                        label="➕ Mehr",
                    ),
                    DashboardV2ActionButton(
                        owner_id=self.owner_id,
                        action="refresh",
                        label="🔄 Aktualisieren",
                    ),
                )
            )
            self.add_item(card)

        def _build_league(self, data: dict, show_admin: bool, note: str | None):
            found = bool(data.get("found"))
            name = str(data.get("player_name") or "Spieler")
            division = data.get("division")
            self._add_header(
                f"⚔️ {name} · Division {division}" if found else "⚔️ TFL SPIELERBEREICH",
                f"**{CURRENT_SEASON_LABEL}** · TFL Bot – macht das Racen leichter!",
                note,
            )

            if found:
                division = int(division)
                self._add_matchday(data, division)

                played = int(data.get("played") or 0)
                total = int(data.get("total") or 0)
                open_games = int(data.get("open") or 0)
                scheduled = int(data.get("scheduled_open") or 0)
                without_date = max(0, open_games - scheduled)
                deadline = get_deadline_traffic_light(played, total)
                pct = round((played / total) * 100) if total else 0
                progress_bar = _dashboard_progress_bar(played, total)

                progress_text = (
                    "## 🧭 Saisonfortschritt\n"
                    f"`{progress_bar}` **{played} / {total} Spiele** · {pct}%\n"
                    f"{deadline['emoji']} **{deadline['label']}** · {deadline['detail']}\n"
                    f"⚔️ **{open_games}** offen · 📅 **{scheduled}** terminiert · "
                    f"⏳ **{without_date}** noch ohne Termin\n"
                    f"🚫 **Streichmodi:** {data.get('mode_1') or '–'} · {data.get('mode_2') or '–'}"
                )
                self.add_item(
                    discord.ui.Container(
                        discord.ui.TextDisplay(progress_text),
                        accent_colour=_deadline_accent(deadline),
                    )
                )

                today_rows = {int(x.get("row") or 0) for x in data.get("today_matches") or []}
                future_matches = [
                    match
                    for match in (data.get("next_matches") or [])
                    if int(match.get("row") or 0) not in today_rows
                ]
                visible_matches = future_matches[:3]
                additional_count = max(0, len(future_matches) - len(visible_matches))
                terms = discord.ui.Container(accent_colour=0x3498DB)
                header = "## 📅 Nächste Spiele"
                if additional_count:
                    header += f"\n-# Die nächsten 3 Termine · +{additional_count} weitere eingetragen"
                elif visible_matches:
                    header += "\n-# Deine nächsten eingetragenen Spieltermine"
                else:
                    header += "\n-# Nach heute ist aktuell kein weiterer Termin eingetragen"
                if without_date:
                    header += f"\n-# {without_date} offene Saisonspiel{'e' if without_date != 1 else ''} noch ohne Termin"
                if visible_matches:
                    game_lines = []
                    for idx, match in enumerate(visible_matches, start=1):
                        when = match.get("datetime")
                        when_text = when.strftime("%d.%m.%Y · %H:%M") if when else str(match.get("date_text") or "Termin")
                        game_lines.append(
                            f"**{idx}. {when_text} Uhr** · "
                            f"**{match.get('home') or '?'} vs. {match.get('away') or '?'}** "
                            f"· 🎮 **{match.get('mode') or 'Modus noch offen'}**"
                        )
                    header += "\n\n" + "\n".join(game_lines)

                terms.add_item(discord.ui.TextDisplay(header))

                if visible_matches:
                    next_state = {"row": int(visible_matches[0].get("row") or 0)}
                    terms.add_item(
                        discord.ui.ActionRow(
                            DashboardNextMatchSelect(
                                matches=visible_matches,
                                owner_id=self.owner_id,
                                state=next_state,
                                division=division,
                            )
                        )
                    )
                    terms.add_item(
                        discord.ui.ActionRow(
                            DashboardNextGameActionButton(
                                owner_id=self.owner_id,
                                division=division,
                                matches=visible_matches,
                                state=next_state,
                                action="result",
                            ),
                            DashboardNextGameActionButton(
                                owner_id=self.owner_id,
                                division=division,
                                matches=visible_matches,
                                state=next_state,
                                action="reschedule",
                            ),
                            DashboardNextGameActionButton(
                                owner_id=self.owner_id,
                                division=division,
                                matches=visible_matches,
                                state=next_state,
                                action="cancel",
                            ),
                        )
                    )
                self.add_item(terms)

                achievements = data.get("achievements") or []
                achievement_lines = [
                    f"• **{_dashboard_achievement_label(item)}**"
                    for item in achievements[:3]
                ]
                if achievement_lines:
                    achievement_body = "\n".join(achievement_lines)
                    if len(achievements) > 3:
                        achievement_body += f"\n-# +{len(achievements) - 3} weitere über ➕ Mehr → Meine Achievements"
                else:
                    achievement_body = f"Noch kein Achievement in {CURRENT_SEASON_LABEL}."
                self.add_item(
                    discord.ui.Container(
                        discord.ui.TextDisplay(
                            f"## 🏅 Achievements · {CURRENT_SEASON_LABEL}\n{achievement_body}"
                        ),
                        accent_colour=0xF1C40F,
                    )
                )

                self.add_item(
                    discord.ui.Container(
                        discord.ui.TextDisplay(
                            f"## 🏆 Tabelle · Division {division}\n"
                            f"{_dashboard_table_markdown(data)}\n"
                            "-# ▶ = du · S = Siege · U = Remis · N = Niederlagen · Sieg 2 Pkt · Remis 1 Pkt"
                        ),
                        accent_colour=0xF1C40F,
                    )
                )
            else:
                self.add_item(
                    discord.ui.Container(
                        discord.ui.TextDisplay(
                            "### ℹ️ Divisionsdaten nicht verfügbar\n"
                            "Die Daten konnten gerade nicht aus dem Sheet geladen werden. "
                            "Die Spielerfunktionen stehen weiterhin zur Verfügung."
                        ),
                        accent_colour=0x95A5A6,
                    )
                )

            self._add_league_actions(show_admin)

        def _add_coop_matchday(self, data: dict, team_name: str):
            matches = (data.get("today_matches") or [])[:25]
            if not matches:
                return
            state = {"row": int(matches[0].get("row") or 0)}
            card = discord.ui.Container(accent_colour=0xE67E22)
            lines = ["## 🔥 MATCHDAY", "-# Alle heutigen Coop-Spiele · Match auswählen, dann Aktion starten"]
            for match in matches:
                when = match.get("datetime")
                time_text = when.strftime("%H:%M") if when else "?"
                lines.append(
                    f"**{time_text} Uhr** · **{match.get('home') or '?'} vs. {match.get('away') or '?'}** "
                    f"· 🎮 **{match.get('mode') or 'Coop'}**"
                )
            card.add_item(discord.ui.TextDisplay("\n".join(lines)))
            card.add_item(
                discord.ui.ActionRow(
                    DashboardMatchdaySelect(
                        matches=matches,
                        owner_id=self.owner_id,
                        state=state,
                        prefix="coopmatchday",
                    )
                )
            )
            card.add_item(
                discord.ui.ActionRow(
                    DashboardCoopMatchdayActionButton(
                        owner_id=self.owner_id,
                        team_name=team_name,
                        matches=matches,
                        state=state,
                        action="result",
                    ),
                    DashboardCoopMatchdayActionButton(
                        owner_id=self.owner_id,
                        team_name=team_name,
                        matches=matches,
                        state=state,
                        action="stream",
                    ),
                    DashboardCoopMatchdayActionButton(
                        owner_id=self.owner_id,
                        team_name=team_name,
                        matches=matches,
                        state=state,
                        action="menu",
                    ),
                )
            )
            self.add_item(card)

        def _build_coop(self, data: dict, note: str | None):
            team = str(data.get("team_name") or "Coop League")
            partner = str(data.get("partner") or "–")
            status = str(data.get("status") or "nicht angemeldet")
            self._add_header(
                f"👥 {team}",
                "**Coop League** · gemeinsames Team-Dashboard",
                note,
            )

            if not data.get("found"):
                self.add_item(
                    discord.ui.Container(
                        discord.ui.TextDisplay(
                            "## 👥 Noch kein Coop-Team\n"
                            "Du bist aktuell keinem offenen oder bestätigten Coop-Team zugeordnet."
                        ),
                        accent_colour=0x95A5A6,
                    )
                )
            else:
                played = int(data.get("played") or 0)
                total = int(data.get("total") or 0)
                open_games = int(data.get("open") or 0)
                scheduled = int(data.get("scheduled_open") or 0)
                without_date = max(0, open_games - scheduled)
                progress_bar = _dashboard_progress_bar(played, total)
                pct = round((played / total) * 100) if total else 0

                self.add_item(
                    discord.ui.Container(
                        discord.ui.TextDisplay(
                            "## 🧭 Coop-Fortschritt\n"
                            f"**Team:** {team} · **Partner:** {partner} · **Status:** {status}\n"
                            f"`{progress_bar}` **{played} / {total} Spiele** · {pct}%\n"
                            f"⚔️ **{open_games}** offen · 📅 **{scheduled}** terminiert · "
                            f"⏳ **{without_date}** noch ohne Termin"
                        ),
                        accent_colour=0x2ECC71 if status == "bestätigt" else 0xF1C40F,
                    )
                )

                if status == "bestätigt":
                    self._add_coop_matchday(data, team)
                    today_rows = {int(x.get("row") or 0) for x in data.get("today_matches") or []}
                    future_matches = [
                        match
                        for match in (data.get("next_matches") or [])
                        if int(match.get("row") or 0) not in today_rows
                    ]
                    visible = future_matches[:3]
                    additional = max(0, len(future_matches) - len(visible))
                    terms = discord.ui.Container(accent_colour=0x3498DB)
                    header = "## 📅 Nächste Coop-Spiele"
                    if additional:
                        header += f"\n-# Die nächsten 3 Termine · +{additional} weitere eingetragen"
                    elif visible:
                        header += "\n-# Eure nächsten gemeinsamen Termine"
                    else:
                        header += "\n-# Nach heute ist aktuell kein weiterer Termin eingetragen"
                    terms.add_item(discord.ui.TextDisplay(header))
                    for match in visible:
                        when = match.get("datetime")
                        when_text = when.strftime("%d.%m.%Y · %H:%M") if when else str(match.get("date_text") or "Termin")
                        terms.add_item(
                            discord.ui.Section(
                                f"### {when_text} Uhr\n"
                                f"**{match.get('home') or '?'}** vs. **{match.get('away') or '?'}**\n"
                                f"🎮 **{match.get('mode') or 'Coop'}**",
                                accessory=DashboardV2CoopResultButton(
                                    match=match,
                                    team_name=team,
                                    owner_id=self.owner_id,
                                    compact=True,
                                ),
                            )
                        )
                    self.add_item(terms)
                    self.add_item(
                        discord.ui.Container(
                            discord.ui.TextDisplay(
                                f"## 🏆 Coop-Tabelle\n"
                                f"{_format_simple_table(data.get('coop_table') or [])}\n"
                                "-# Sieg 2 Pkt · Remis 1 Pkt"
                            ),
                            accent_colour=0xF1C40F,
                        )
                    )

            actions = discord.ui.Container(accent_colour=0x2ECC71)
            actions.add_item(discord.ui.TextDisplay("## ⚙️ Coop-Aktionen"))
            actions.add_item(
                discord.ui.ActionRow(
                    DashboardV2ActionButton(
                        owner_id=self.owner_id,
                        action="coop_menu",
                        label="👥 Coop-Menü",
                        style=discord.ButtonStyle.success,
                        dashboard_mode="coop",
                    ),
                    DashboardV2ActionButton(
                        owner_id=self.owner_id,
                        action="refresh",
                        label="🔄 Aktualisieren",
                        dashboard_mode="coop",
                    ),
                )
            )
            self.add_item(actions)

        def _build(self, data: dict, show_admin: bool, note: str | None):
            self._add_banner()
            if self.dashboard_mode == "coop":
                self._build_coop(data, note)
            else:
                self._build_league(data, show_admin, note)


    def build_dashboard_layout_view(
        *,
        data: dict,
        owner_id: int,
        show_admin: bool,
        note: str | None = None,
        dashboard_mode: str = "league",
    ):
        return DashboardLayoutView(
            data=data,
            owner_id=owner_id,
            show_admin=show_admin,
            note=note,
            dashboard_mode=dashboard_mode,
        )
else:
    def build_dashboard_layout_view(
        *,
        data: dict,
        owner_id: int,
        show_admin: bool,
        note: str | None = None,
        dashboard_mode: str = "league",
    ):
        raise RuntimeError("Discord Components V2 sind mit dieser discord.py-Version nicht verfügbar.")


# =========================================================
# ERGEBNIS WRAPPER
# =========================================================

class BackToResultMenuFromLeagueStep1Button(discord.ui.Button):
    def __init__(self):
        super().__init__(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=menu_embed(" Ergebnis melden", "Wähle einen Bereich."),
            view=ResultMenuView(owner_id=interaction.user.id),
            content=None,
        )


class BackToResultMenuFromLeagueStep2Button(discord.ui.Button):
    def __init__(self):
        super().__init__(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction):
        view = PlayerLeagueResultViewStep1(author_id=interaction.user.id)
        view.state.kind = "Ergebnis League"

        await interaction.response.edit_message(
            content=view.render_summary(),
            view=view,
            embed=None,
        )


class BackToResultMenuFromCupButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=3)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=menu_embed(" Ergebnis melden", "Wähle einen Bereich."),
            view=ResultMenuView(owner_id=interaction.user.id),
            content=None,
        )


class PlayerLeagueResultContinueButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Weiter", style=discord.ButtonStyle.primary, row=4)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, PlayerLeagueResultViewStep1):
            return

        s = view.state

        if not all([s.division, s.match_row_index, s.match_label, s.player1, s.player2]):
            await interaction.response.send_message(
                "Bitte zuerst Division, Heimrecht und Spiel auswählen.",
                ephemeral=True,
            )
            return

        next_view = PlayerLeagueResultViewStep2(
            author_id=interaction.user.id,
            state=s.clone(),
        )

        await interaction.response.edit_message(
            content=next_view.render_summary(),
            view=next_view,
            embed=None,
        )


class PlayerLeagueResultViewStep1(LeagueResultViewStep1):
    def __init__(self, author_id: int):
        super().__init__(cog=None, author_id=author_id)

        old_back = None
        old_continue = None

        for item in list(self.children):
            if isinstance(item, discord.ui.Button) and item.label == "Zurück":
                old_back = item
            elif isinstance(item, discord.ui.Button) and item.label == "Weiter":
                old_continue = item

        if old_back is not None:
            self.remove_item(old_back)

        if old_continue is not None:
            self.remove_item(old_continue)

        self.add_item(PlayerLeagueResultContinueButton())
        self.add_item(BackToResultMenuFromLeagueStep1Button())


class PlayerLeagueResultViewStep2(LeagueResultViewStep2):
    def __init__(self, author_id: int, state):
        super().__init__(cog=None, author_id=author_id, state=state)

        for item in list(self.children):
            if isinstance(item, discord.ui.Button) and item.label in {"Zurück", "Absenden"}:
                self.remove_item(item)

        self.add_item(EnhancedLeagueResultSubmitButton())
        self.add_item(BackToResultMenuFromLeagueStep2Button())


class PlayerCupResultView(CupResultView):
    def __init__(self, author_id: int):
        super().__init__(cog=None, author_id=author_id)

        old_back = None

        for item in list(self.children):
            if isinstance(item, discord.ui.Button) and item.label == "Zurück":
                old_back = item
                break

        if old_back is not None:
            self.remove_item(old_back)

        self.add_item(BackToResultMenuFromCupButton())


# =========================================================
# STREICHMODI SETZEN
# =========================================================

class StreichmodusSelect(discord.ui.Select):
    EMPTY_VALUE = "__none__"

    def __init__(self, slot: int, modes: list[str], selected_value: str | None = None):
        self.slot = slot

        options = []

        if not selected_value:
            options.append(
                discord.SelectOption(
                    label="Bitte wählen",
                    value=self.EMPTY_VALUE,
                    default=True,
                )
            )

        for mode in modes[:25]:
            clean_mode = (mode or "").strip()
            if not clean_mode:
                continue

            options.append(
                discord.SelectOption(
                    label=clean_mode[:100],
                    value=clean_mode[:100],
                    default=(clean_mode == selected_value),
                )
            )

        super().__init__(
            placeholder=f"Streichmodus {slot} wählen …",
            min_values=1,
            max_values=1,
            options=options,
            row=slot - 1,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view

        if not isinstance(view, StreichmodusSettingView):
            return

        selected = self.values[0]

        if selected == self.EMPTY_VALUE:
            selected = ""

        if self.slot == 1:
            view.mode_1 = selected
        else:
            view.mode_2 = selected

        new_view = StreichmodusSettingView(
            owner_id=view.owner_id,
            modes=view.modes,
            mode_1=view.mode_1,
            mode_2=view.mode_2,
            div_number=view.div_number,
            change_used=view.change_used,
        )

        await interaction.response.edit_message(
            embed=new_view.build_embed(),
            view=new_view,
            content=None,
        )


class StreichmodusSettingView(PlayerBaseView):
    def __init__(
        self,
        owner_id: int,
        modes: list[str],
        mode_1: str = "",
        mode_2: str = "",
        div_number: int | None = None,
        change_used: bool = False,
    ):
        super().__init__(owner_id)
        self.modes = modes
        self.mode_1 = mode_1
        self.mode_2 = mode_2
        self.div_number = div_number
        self.change_used = change_used

        self.add_item(StreichmodusSelect(1, self.modes, self.mode_1))
        self.add_item(StreichmodusSelect(2, self.modes, self.mode_2))

    def build_embed(self) -> discord.Embed:
        div_text = f"Division {self.div_number}" if self.div_number else "Division nicht erkannt"

        change_text = (
            "Einmalige Änderung bereits genutzt."
            if self.change_used
            else "Nach der Erstsetzung ist noch genau eine Änderung möglich."
        )

        text = (
            f"**{div_text}**\n\n"
            "Wähle zwei Streichmodi.\n\n"
            f"**Modus 1:** {self.mode_1 or '-'}\n"
            f"**Modus 2:** {self.mode_2 or '-'}\n\n"
            f"{change_text}"
        )

        return menu_embed("⚙️ Einstellungen → Streichmodis setzen", text)

    @discord.ui.button(label="Speichern", style=discord.ButtonStyle.success, row=2)
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user

        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Nur auf dem Server verfügbar.", ephemeral=True)
            return

        if not self.mode_1 or not self.mode_2:
            await interaction.response.send_message(
                "Bitte beide Streichmodi auswählen.",
                ephemeral=True,
            )
            return

        if self.mode_1 == self.mode_2:
            await interaction.response.send_message(
                "Bitte zwei unterschiedliche Streichmodi auswählen.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        try:
            name_candidates = get_name_candidates(member)

            result = await asyncio.to_thread(
                write_streichmodi_with_change_limit,
                name_candidates,
                self.mode_1,
                self.mode_2,
            )

            div_number = result["div_number"]

            if result["notify_change"]:
                channel_id = DIVISION_CHANNELS.get(div_number)
                channel = interaction.client.get_channel(channel_id) if channel_id else None

                if channel is None and channel_id:
                    try:
                        channel = await interaction.client.fetch_channel(channel_id)
                    except Exception:
                        channel = None

                if channel:
                    await channel.send(
                        f"Spieler {member.display_name} hat seinen Streichmodus "
                        f"von {result['old_mode_1']} / {result['old_mode_2']} "
                        f"auf {result['new_mode_1']} / {result['new_mode_2']} geändert."
                    )

            await interaction.edit_original_response(
                embed=menu_embed(
                    "⚙️ Einstellungen → Streichmodis setzen",
                    (
                        "Streichmodi gespeichert.\n\n"
                        f"**Division:** {div_number}.DIV\n"
                        f"**Modus 1:** {self.mode_1}\n"
                        f"**Modus 2:** {self.mode_2}\n"
                        f"**Sheet-Zeile:** {result['row_index']}"
                    ),
                ),
                view=PlaceholderView(
                    owner_id=interaction.user.id,
                    back_view=SettingsMenuView(owner_id=interaction.user.id),
                    back_embed=menu_embed("⚙️ Einstellungen", "Wähle einen Bereich."),
                ),
                content=None,
            )

        except Exception as e:
            await interaction.edit_original_response(
                embed=menu_embed(
                    "⚙️ Einstellungen → Streichmodis setzen",
                    f"Fehler beim Speichern: {e}",
                ),
                view=self,
                content=None,
            )

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=2)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("⚙️ Einstellungen", "Wähle einen Bereich."),
            view=SettingsMenuView(owner_id=interaction.user.id),
            content=None,
        )


# =========================================================
# LIGA-AUSTRITT
# =========================================================

LEAGUE_ROLE_NORMALIZED = "tryforceleague"
CUP_ROLE_NORMALIZED = "tryforcecup"


def clear_shared_results_db_cache():
    """
    Leert den Results-DB-Cache aus bot.py ohne einen normalen Import von bot.py.
    Ein normales `import bot` würde beim Extension-Setup zu einer doppelten
    Bot-Initialisierung führen können.
    """
    for module_name in ("__main__", "bot"):
        module = sys.modules.get(module_name)
        if module is None:
            continue

        clear_func = getattr(module, "clear_results_db_cache", None)
        if callable(clear_func):
            try:
                clear_func()
            except Exception as e:
                print(f"[PLAYER EXIT] Results-DB-Cache konnte nicht geleert werden: {e}")
            return


def division_role_matches(role_name: str, div_number: int) -> bool:
    """
    Unterstützt u. a.:
    - 1. Division
    - Division 1
    - 1 DIV
    - DIV 1
    """
    normalized = normalize_name(role_name)
    number = str(int(div_number))
    return normalized in {
        f"{number}division",
        f"division{number}",
        f"{number}div",
        f"div{number}",
    }


def get_exit_roles(member: discord.Member, div_number: int) -> list[discord.Role]:
    roles = []

    for role in member.roles:
        normalized = normalize_name(role.name)

        if normalized in {LEAGUE_ROLE_NORMALIZED, CUP_ROLE_NORMALIZED}:
            roles.append(role)
            continue

        if division_role_matches(role.name, div_number):
            roles.append(role)

    # Doppelte Rollen sicher ausschließen
    unique = []
    seen = set()
    for role in roles:
        if role.id in seen:
            continue
        seen.add(role.id)
        unique.append(role)

    return unique


def apply_player_exit_for_name_candidates(name_candidates: list[str]) -> dict:
    """
    Findet die Division über Spalte L und wertet ALLE Ligaspiele des Spielers
    als Niederlage gegen ihn.

    Heimspieler steigt aus  -> 0:2
    Gastspieler steigt aus  -> 2:0

    Der ursprüngliche Modus in Spalte C bleibt bewusst erhalten.
    Spalte G wird auf FF gesetzt und Spalte H mit dem Austritt markiert.
    """
    ws, roster_row_index, div_number = get_division_worksheet_for_name_candidates(name_candidates)

    if ws is None or roster_row_index is None or div_number is None:
        raise RuntimeError("Du wurdest in keiner Division gefunden.")

    roster_row = row_values_cached(
        lambda: ws,
        sheet_name=player_sheet_name(ws),
        row=roster_row_index,
        ttl_seconds=PLAYER_SHEET_CACHE_TTL_SECONDS,
    )

    canonical_name = roster_row[11].strip() if len(roster_row) > 11 else ""
    if not canonical_name:
        canonical_name = next((x.strip() for x in name_candidates if x and x.strip()), "")

    if not canonical_name:
        raise RuntimeError("Spielername konnte nicht eindeutig bestimmt werden.")

    target = normalize_name(canonical_name)
    rows = ws.get_all_values()
    requests = []
    affected_rows = []

    for row_index, row in enumerate(rows[1:], start=2):
        home = row[3].strip() if len(row) > 3 else ""  # D
        away = row[5].strip() if len(row) > 5 else ""  # F

        home_match = bool(home) and normalize_name(home) == target
        away_match = bool(away) and normalize_name(away) == target

        if not home_match and not away_match:
            continue

        result_value = "0:2" if home_match else "2:0"

        requests.extend(
            [
                {"range": f"E{row_index}:E{row_index}", "values": [[result_value]]},
                {"range": f"G{row_index}:G{row_index}", "values": [["FF"]]},
                {
                    "range": f"H{row_index}:H{row_index}",
                    "values": [[f"Austritt: {canonical_name}"]],
                },
            ]
        )
        affected_rows.append(row_index)

    if not affected_rows:
        raise RuntimeError(
            f"Für {canonical_name} wurden in Division {div_number} keine Ligaspiele gefunden."
        )

    sheet_write_call(
        lambda: ws.batch_update(requests),
        invalidate_prefixes=player_invalidate_prefixes(ws),
    )

    # Der Joomla/API-Results-Cache sitzt in bot.py und muss nach dem Batch-Write
    # ebenfalls verworfen werden.
    clear_shared_results_db_cache()

    return {
        "player_name": canonical_name,
        "division": int(div_number),
        "affected_games": len(affected_rows),
        "affected_rows": affected_rows,
    }


async def send_exit_admin_message(client: discord.Client, text: str):
    channel = client.get_channel(EXIT_REQUEST_ADMIN_CHANNEL_ID)

    if channel is None:
        try:
            channel = await client.fetch_channel(EXIT_REQUEST_ADMIN_CHANNEL_ID)
        except Exception as e:
            print(f"[EXIT REQUEST] Adminchannel konnte nicht geladen werden: {e}")
            return False

    try:
        await channel.send(text)
        return True
    except Exception as e:
        print(f"[EXIT REQUEST] Adminnachricht fehlgeschlagen: {e}")
        return False


async def execute_full_player_exit(
    client: discord.Client,
    guild: discord.Guild | None,
    member: discord.Member | None = None,
    fallback_name: str | None = None,
) -> dict:
    """
    Gemeinsame Austrittslogik für:
    - normalen Austrittsbutton des Spielers
    - Antwort "Austreten" auf eine Admin-Anfrage
    - automatischen Austritt nach 5 Tagen ohne Reaktion
    """
    if member is not None:
        name_candidates = get_name_candidates(member)
    elif fallback_name:
        name_candidates = [fallback_name]
    else:
        raise RuntimeError("Spieler konnte nicht bestimmt werden.")

    result = await asyncio.to_thread(
        apply_player_exit_for_name_candidates,
        name_candidates,
    )

    player_name = result["player_name"]
    div_number = result["division"]
    warnings = []

    # Divisionschat informieren
    channel_id = DIVISION_CHANNELS.get(div_number)
    division_channel = guild.get_channel(channel_id) if guild and channel_id else None

    if division_channel is None and channel_id:
        try:
            division_channel = await client.fetch_channel(channel_id)
        except Exception as e:
            warnings.append(f"Divisionschat konnte nicht geladen werden: {e}")
            division_channel = None

    if division_channel is not None:
        try:
            await division_channel.send(
                "🚨 **LIGA-AUSTRITT** 🚨\n\n"
                f"**{player_name} ist ausgestiegen. "
                "Alle seine Spiele werden mit 0:2 gegen ihn gewertet.**"
            )
        except Exception as e:
            warnings.append(f"Divisionsnachricht konnte nicht gesendet werden: {e}")

    # Rollen entfernen
    removed_role_names = []

    if member is not None:
        roles_to_remove = get_exit_roles(member, div_number)
        removed_role_names = [role.name for role in roles_to_remove]

        if roles_to_remove:
            try:
                await member.remove_roles(
                    *roles_to_remove,
                    reason=f"TFL Liga-Austritt von {player_name}",
                )
            except Exception as e:
                warnings.append(f"Rollen konnten nicht vollständig entfernt werden: {e}")
        else:
            warnings.append("Keine passenden TFL-/Cup-/Divisionsrollen gefunden.")
    else:
        warnings.append("Discord-Mitglied nicht gefunden; Rollen konnten nicht entfernt werden.")

    return {
        **result,
        "removed_role_names": removed_role_names,
        "warnings": warnings,
    }




class PlayerExitConfirmView(PlayerBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id=owner_id, timeout=300)
        self.processing = False

    @discord.ui.button(
        label="Ja, Liga verlassen",
        style=discord.ButtonStyle.danger,
        row=0,
    )
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if self.processing:
            await interaction.response.send_message(
                "Der Austritt wird bereits verarbeitet.",
                ephemeral=True,
            )
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Diese Funktion ist nur auf dem TFL-Server verfügbar.",
                ephemeral=True,
            )
            return

        self.processing = True

        # Discord sofort bestätigen, bevor Google Sheets und Rollen bearbeitet werden.
        await interaction.response.defer()

        try:
            result = await execute_full_player_exit(
                client=interaction.client,
                guild=interaction.guild,
                member=member,
            )

            player_name = result["player_name"]
            div_number = result["division"]
            affected_games = result["affected_games"]
            removed_role_names = result["removed_role_names"]
            warning_lines = [f"⚠️ {line}" for line in result["warnings"]]

            details = (
                f"**Division:** {div_number}\n"
                f"**Gewertete Spiele:** {affected_games}\n"
            )

            if removed_role_names:
                details += f"**Entfernte Rollen:** {', '.join(removed_role_names)}\n"

            if warning_lines:
                details += "\n" + "\n".join(warning_lines)

            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="Liga-Austritt bestätigt",
                    description=(
                        f"**{player_name}**, dein Austritt aus der Try Force League ist endgültig.\n\n"
                        f"{details}"
                    ),
                    color=discord.Color.red(),
                ),
                view=None,
                content=None,
            )

        except Exception as e:
            self.processing = False
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="Liga-Austritt fehlgeschlagen",
                    description=(
                        "Der Austritt wurde nicht vollständig durchgeführt. "
                        "Bitte wende dich an einen Admin.\n\n"
                        f"**Fehler:** {e}"
                    ),
                    color=discord.Color.red(),
                ),
                view=self,
                content=None,
            )

    @discord.ui.button(
        label="Abbrechen",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await show_player_dashboard(
            interaction,
            note="Der Liga-Austritt wurde abgebrochen.",
        )


# =========================================================
# ADMIN-AUSTRITTSANFRAGEN
# =========================================================

EXIT_REQUEST_HEADERS = [
    "Request ID",
    "Spieler",
    "Discord ID",
    "Division",
    "Gesendet am",
    "Frist",
    "Status",
    "Erledigt am",
    "DM Channel ID",
    "DM Message ID",
    "Angefordert von",
    "Fehler",
]


def get_exit_request_ws():
    wb = get_season_spreadsheet()

    try:
        ws = wb.worksheet(EXIT_REQUEST_SHEET)
    except Exception:
        ws = wb.add_worksheet(
            title=EXIT_REQUEST_SHEET,
            rows=500,
            cols=12,
        )
        ws.update("A1:L1", [EXIT_REQUEST_HEADERS])

    try:
        first_row = ws.row_values(1)
        if not any((value or "").strip() for value in first_row[:12]):
            ws.update("A1:L1", [EXIT_REQUEST_HEADERS])
    except Exception:
        pass

    return ws


def parse_request_datetime(value: str):
    value = (value or "").strip()
    if not value:
        return None

    try:
        parsed = dt.fromisoformat(value)
        if parsed.tzinfo is None:
            return BERLIN_TZ.localize(parsed)
        return parsed.astimezone(BERLIN_TZ)
    except Exception:
        return None


def get_exit_request_rows():
    ws = get_exit_request_ws()
    return ws, ws.get_all_values()


def find_exit_request_row(request_id: str):
    ws, rows = get_exit_request_rows()

    for row_index, row in enumerate(rows[1:], start=2):
        current_id = row[0].strip() if len(row) > 0 else ""
        if current_id == request_id:
            return ws, row_index, row

    return ws, None, None


def find_pending_exit_request_for_player(discord_id: int):
    ws, rows = get_exit_request_rows()
    target = str(discord_id)

    for row_index, row in enumerate(rows[1:], start=2):
        player_id = row[2].strip() if len(row) > 2 else ""
        status = row[6].strip().lower() if len(row) > 6 else ""

        if player_id == target and status == "offen":
            return ws, row_index, row

    return ws, None, None


def write_exit_request(
    request_id: str,
    player_name: str,
    discord_id: int,
    division: int,
    sent_at,
    deadline,
    dm_channel_id: int,
    dm_message_id: int,
    requested_by: str,
):
    ws = get_exit_request_ws()

    ws.append_row(
        [
            request_id,
            player_name,
            str(discord_id),
            str(division),
            sent_at.isoformat(),
            deadline.isoformat(),
            "offen",
            "",
            str(dm_channel_id),
            str(dm_message_id),
            requested_by,
            "",
        ],
        value_input_option="USER_ENTERED",
    )


def update_exit_request_status(
    request_id: str,
    status: str,
    error_text: str = "",
):
    ws, row_index, row = find_exit_request_row(request_id)

    if row_index is None:
        raise RuntimeError("Austrittsanfrage wurde nicht gefunden.")

    ws.batch_update(
        [
            {"range": f"G{row_index}", "values": [[status]]},
            {"range": f"H{row_index}", "values": [[dt.now(BERLIN_TZ).isoformat()]]},
            {"range": f"L{row_index}", "values": [[error_text]]},
        ]
    )

    return row_index, row


def get_pending_exit_requests() -> list[dict]:
    _, rows = get_exit_request_rows()
    out = []

    for row_index, row in enumerate(rows[1:], start=2):
        status = row[6].strip().lower() if len(row) > 6 else ""
        if status != "offen":
            continue

        player_id = row[2].strip() if len(row) > 2 else ""
        division = row[3].strip() if len(row) > 3 else ""
        dm_channel_id = row[8].strip() if len(row) > 8 else ""
        dm_message_id = row[9].strip() if len(row) > 9 else ""

        if not player_id.isdigit():
            continue

        out.append(
            {
                "row": row_index,
                "request_id": row[0].strip() if len(row) > 0 else "",
                "player_name": row[1].strip() if len(row) > 1 else "",
                "discord_id": int(player_id),
                "division": int(division) if division.isdigit() else None,
                "deadline": parse_request_datetime(row[5] if len(row) > 5 else ""),
                "dm_channel_id": int(dm_channel_id) if dm_channel_id.isdigit() else None,
                "dm_message_id": int(dm_message_id) if dm_message_id.isdigit() else None,
            }
        )

    return out


def list_league_players_by_division(div_number: int) -> list[str]:
    """
    Liest die Teilnehmerliste für Austrittsanfragen direkt aus Spalte L
    des korrekten Division-Sheets.

    Wichtig: Nicht mehr über restinfo.WB, sondern über dieselbe direkte
    Spreadsheet-ID-Verbindung wie die funktionierende Admin-Spielplanfunktion.
    """
    ws = admin_spielplan_get_div_ws(str(div_number))

    values = col_values_cached(
        lambda: ws,
        sheet_name=player_sheet_name(ws, f"{div_number}.DIV"),
        col=12,  # L
        ttl_seconds=PLAYER_SHEET_CACHE_TTL_SECONDS
    )

    names = []
    seen = set()

    for raw in values[1:]:
        name = (raw or "").strip()
        if not name:
            continue

        # mögliche Überschrift ignorieren
        if normalize_name(name) in {"racer", "spieler", "teilnehmer"}:
            continue

        key = normalize_name(name)
        if not key or key in seen:
            continue

        seen.add(key)
        names.append(name)

    print(
        f"[EXIT REQUEST] Division {div_number}: "
        f"{len(names)} Spieler aus {ws.title}!L geladen: {names}"
    )

    return names[:25]


async def find_discord_member_for_league_player(
    guild: discord.Guild,
    player_name: str,
) -> discord.Member | None:
    target = normalize_name(player_name)

    for member in guild.members:
        if any(
            normalize_name(candidate or "") == target
            for candidate in get_name_candidates(member)
        ):
            return member

    return None


async def resolve_exit_request_continue(
    interaction: discord.Interaction,
    request_id: str,
    expected_player_id: int,
):
    if interaction.user.id != expected_player_id:
        await interaction.response.send_message(
            "Diese Anfrage ist nicht für dich bestimmt.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    try:
        _, row_index, row = await asyncio.to_thread(
            find_exit_request_row,
            request_id,
        )

        if row_index is None:
            raise RuntimeError("Anfrage wurde nicht gefunden.")

        status = row[6].strip().lower() if len(row) > 6 else ""
        if status != "offen":
            await interaction.edit_original_response(
                content="Diese Anfrage wurde bereits bearbeitet.",
                view=None,
            )
            return

        player_name = row[1].strip() if len(row) > 1 else str(interaction.user)

        await asyncio.to_thread(
            update_exit_request_status,
            request_id,
            "weiterspielen",
            "",
        )

        await send_exit_admin_message(
            interaction.client,
            f"{player_name} hat reagiert und wird weiter am Spielbetrieb teilnehmen",
        )

        await interaction.edit_original_response(
            content=(
                "✅ Danke für deine Rückmeldung.\n\n"
                "Du hast bestätigt, dass du **weiter am Spielbetrieb teilnimmst**."
            ),
            view=None,
        )

    except Exception as e:
        await interaction.edit_original_response(
            content=f"❌ Deine Rückmeldung konnte nicht verarbeitet werden: {e}",
            view=None,
        )


async def resolve_exit_request_leave(
    interaction: discord.Interaction,
    request_id: str,
    expected_player_id: int,
):
    if interaction.user.id != expected_player_id:
        await interaction.response.send_message(
            "Diese Anfrage ist nicht für dich bestimmt.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    try:
        _, row_index, row = await asyncio.to_thread(
            find_exit_request_row,
            request_id,
        )

        if row_index is None:
            raise RuntimeError("Anfrage wurde nicht gefunden.")

        status = row[6].strip().lower() if len(row) > 6 else ""
        if status != "offen":
            await interaction.edit_original_response(
                content="Diese Anfrage wurde bereits bearbeitet.",
                view=None,
            )
            return

        player_name = row[1].strip() if len(row) > 1 else str(interaction.user)

        guild = interaction.client.get_guild(GUILD_ID)
        member = guild.get_member(expected_player_id) if guild else None

        if member is None and guild is not None:
            try:
                member = await guild.fetch_member(expected_player_id)
            except Exception:
                member = None

        result = await execute_full_player_exit(
            client=interaction.client,
            guild=guild,
            member=member,
            fallback_name=player_name,
        )

        await asyncio.to_thread(
            update_exit_request_status,
            request_id,
            "ausgetreten",
            "\n".join(result["warnings"]),
        )

        await send_exit_admin_message(
            interaction.client,
            f"{result['player_name']} hat reagiert und wird aus dem Spielbetrieb austreten.",
        )

        await interaction.edit_original_response(
            content=(
                "✅ Deine Rückmeldung wurde verarbeitet.\n\n"
                "Du trittst aus dem Spielbetrieb aus. "
                "Deine Ligaspiele wurden entsprechend gewertet."
            ),
            view=None,
        )

    except Exception as e:
        try:
            await asyncio.to_thread(
                update_exit_request_status,
                request_id,
                "fehler",
                str(e),
            )
        except Exception:
            pass

        await interaction.edit_original_response(
            content=f"❌ Der Austritt konnte nicht vollständig verarbeitet werden: {e}",
            view=None,
        )


class ExitRequestDMView(discord.ui.View):
    def __init__(self, request_id: str, player_id: int):
        super().__init__(timeout=None)

        self.request_id = request_id
        self.player_id = player_id

        continue_button = discord.ui.Button(
            label="Weiterspielen",
            style=discord.ButtonStyle.success,
            custom_id=f"exitreq:continue:{request_id}",
        )
        leave_button = discord.ui.Button(
            label="Austreten",
            style=discord.ButtonStyle.danger,
            custom_id=f"exitreq:leave:{request_id}",
        )

        async def continue_callback(interaction: discord.Interaction):
            await resolve_exit_request_continue(
                interaction,
                self.request_id,
                self.player_id,
            )

        async def leave_callback(interaction: discord.Interaction):
            await resolve_exit_request_leave(
                interaction,
                self.request_id,
                self.player_id,
            )

        continue_button.callback = continue_callback
        leave_button.callback = leave_callback

        self.add_item(continue_button)
        self.add_item(leave_button)


class ExitRequestAdminView(PlayerBaseView):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False

        if not has_admin_role(interaction.user):
            await interaction.response.send_message(
                "⛔ Diese Funktion ist nur für Admins verfügbar.",
                ephemeral=True,
            )
            return False

        return True


class ExitRequestPlayerSelect(discord.ui.Select):
    def __init__(self, division: int, players: list[str]):
        self.division = division

        super().__init__(
            placeholder="Spieler auswählen …",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=name[:100], value=name[:100])
                for name in players[:25]
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        if not has_admin_role(interaction.user):
            await interaction.response.send_message("⛔ Keine Berechtigung.", ephemeral=True)
            return

        player_name = self.values[0]

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="📨 Austrittsanfrage senden",
                description=(
                    f"**Spieler:** {player_name}\n"
                    f"**Division:** {self.division}\n\n"
                    "Soll die 5-Tage-Anfrage jetzt per DM versendet werden?"
                ),
                color=discord.Color.orange(),
            ),
            view=ExitRequestSendConfirmView(
                owner_id=interaction.user.id,
                division=self.division,
                player_name=player_name,
            ),
            content=None,
        )


class ExitRequestPlayerSelectView(ExitRequestAdminView):
    def __init__(self, owner_id: int, division: int, players: list[str]):
        super().__init__(owner_id)
        self.add_item(ExitRequestPlayerSelect(division, players))

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed(
                "🟨 Administration → Austrittsanfrage",
                "Wähle eine Division.",
            ),
            view=ExitRequestDivisionSelectView(owner_id=interaction.user.id),
            content=None,
        )


class ExitRequestDivisionSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Division auswählen …",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=f"Division {i}", value=str(i))
                for i in range(1, 7)
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        if not has_admin_role(interaction.user):
            await interaction.response.send_message("⛔ Keine Berechtigung.", ephemeral=True)
            return

        division = int(self.values[0])
        await interaction.response.defer()

        try:
            players = await asyncio.to_thread(
                list_league_players_by_division,
                division,
            )

            if not players:
                raise RuntimeError(f"In Division {division} wurden keine Spieler gefunden.")

            await interaction.edit_original_response(
                embed=menu_embed(
                    "🟨 Administration → Austrittsanfrage",
                    f"Division {division}: Wähle den Spieler aus.",
                ),
                view=ExitRequestPlayerSelectView(
                    owner_id=interaction.user.id,
                    division=division,
                    players=players,
                ),
                content=None,
            )

        except Exception as e:
            await interaction.edit_original_response(
                embed=menu_embed(
                    "🟨 Administration → Austrittsanfrage",
                    f"Fehler beim Laden der Spieler: {e}",
                ),
                view=ExitRequestDivisionSelectView(owner_id=interaction.user.id),
                content=None,
            )


class ExitRequestDivisionSelectView(ExitRequestAdminView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)
        self.add_item(ExitRequestDivisionSelect())

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("🟨 Administration", "Wähle eine Adminfunktion."),
            view=AdminMenuView(owner_id=interaction.user.id),
            content=None,
        )


class ExitRequestSendConfirmView(ExitRequestAdminView):
    def __init__(self, owner_id: int, division: int, player_name: str):
        super().__init__(owner_id)
        self.division = division
        self.player_name = player_name
        self.processing = False

    @discord.ui.button(label="Anfrage senden", style=discord.ButtonStyle.success, row=0)
    async def send_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.processing:
            await interaction.response.send_message(
                "Die Anfrage wird bereits versendet.",
                ephemeral=True,
            )
            return

        self.processing = True
        await interaction.response.defer()

        try:
            if interaction.guild is None:
                raise RuntimeError("Server konnte nicht bestimmt werden.")

            member = await find_discord_member_for_league_player(
                interaction.guild,
                self.player_name,
            )

            if member is None:
                raise RuntimeError(
                    f"Discord-Mitglied für '{self.player_name}' wurde nicht gefunden."
                )

            _, existing_row, _ = await asyncio.to_thread(
                find_pending_exit_request_for_player,
                member.id,
            )

            if existing_row is not None:
                raise RuntimeError(
                    "Für diesen Spieler existiert bereits eine offene Austrittsanfrage."
                )

            request_id = uuid.uuid4().hex
            sent_at = dt.now(BERLIN_TZ)
            deadline = sent_at + timedelta(days=EXIT_REQUEST_TIMEOUT_DAYS)

            dm_channel = member.dm_channel or await member.create_dm()
            message = await dm_channel.send(
                (
                    "Bitte teile uns mit, ob du weiterhin am Spielbetrieb teilnimmst. "
                    "Mit Erhalt dieser Nachricht hast du **5 Tage Zeit**.\n\n"
                    "Bitte wähle eine der beiden Optionen:"
                ),
                view=ExitRequestDMView(
                    request_id=request_id,
                    player_id=member.id,
                ),
            )

            await asyncio.to_thread(
                write_exit_request,
                request_id,
                self.player_name,
                member.id,
                self.division,
                sent_at,
                deadline,
                dm_channel.id,
                message.id,
                interaction.user.display_name,
            )

            interaction.client.add_view(
                ExitRequestDMView(
                    request_id=request_id,
                    player_id=member.id,
                ),
                message_id=message.id,
            )

            await interaction.edit_original_response(
                embed=menu_embed(
                    "🟨 Administration → Austrittsanfrage",
                    (
                        f"✅ Anfrage an **{self.player_name}** wurde versendet.\n\n"
                        f"**Frist:** {deadline.strftime('%d.%m.%Y %H:%M')}\n"
                        "Ohne Reaktion wird der Austritt nach 5 Tagen automatisch durchgeführt."
                    ),
                ),
                view=AdminMenuView(owner_id=interaction.user.id),
                content=None,
            )

        except Exception as e:
            self.processing = False
            await interaction.edit_original_response(
                embed=menu_embed(
                    "🟨 Administration → Austrittsanfrage",
                    f"❌ Anfrage konnte nicht versendet werden: {e}",
                ),
                view=self,
                content=None,
            )

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary, row=0)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("🟨 Administration", "Wähle eine Adminfunktion."),
            view=AdminMenuView(owner_id=interaction.user.id),
            content=None,
        )


async def disable_exit_request_dm(bot: commands.Bot, request: dict, content: str):
    channel_id = request.get("dm_channel_id")
    message_id = request.get("dm_message_id")

    if not channel_id or not message_id:
        return

    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)

        message = await channel.fetch_message(message_id)
        await message.edit(content=content, view=None)
    except Exception as e:
        print(f"[EXIT REQUEST] DM nach Fristablauf nicht aktualisiert: {e}")


async def process_expired_exit_request(bot: commands.Bot, request: dict):
    request_id = request["request_id"]
    player_name = request["player_name"]
    player_id = request["discord_id"]

    _, row_index, row = await asyncio.to_thread(
        find_exit_request_row,
        request_id,
    )

    if row_index is None:
        return

    status = row[6].strip().lower() if len(row) > 6 else ""
    if status != "offen":
        return

    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(player_id) if guild else None

    if member is None and guild is not None:
        try:
            member = await guild.fetch_member(player_id)
        except Exception:
            member = None

    try:
        result = await execute_full_player_exit(
            client=bot,
            guild=guild,
            member=member,
            fallback_name=player_name,
        )

        await asyncio.to_thread(
            update_exit_request_status,
            request_id,
            "frist_abgelaufen_austritt",
            "\n".join(result["warnings"]),
        )

        await send_exit_admin_message(
            bot,
            (
                f"{result['player_name']} hat innerhalb von 5 Tagen nicht reagiert "
                "und wird aus dem Spielbetrieb austreten."
            ),
        )

        await disable_exit_request_dm(
            bot,
            request,
            (
                "Die 5-Tage-Frist ist abgelaufen.\n\n"
                "Da keine Rückmeldung eingegangen ist, wurde dein Austritt "
                "aus dem Spielbetrieb automatisch durchgeführt."
            ),
        )

    except Exception as e:
        try:
            await asyncio.to_thread(
                update_exit_request_status,
                request_id,
                "fehler",
                str(e),
            )
        except Exception:
            pass

        await send_exit_admin_message(
            bot,
            (
                f"⚠️ Automatischer Austritt für {player_name} nach Ablauf "
                f"der 5-Tage-Frist ist fehlgeschlagen: {e}"
            ),
        )


async def exit_request_monitor_loop(bot: commands.Bot):
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            pending = await asyncio.to_thread(get_pending_exit_requests)
            now = dt.now(BERLIN_TZ)

            for request in pending:
                deadline = request.get("deadline")
                if deadline is not None and deadline <= now:
                    await process_expired_exit_request(bot, request)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[EXIT REQUEST] Monitorfehler: {e}")

        await asyncio.sleep(EXIT_REQUEST_CHECK_INTERVAL_SECONDS)


async def restore_exit_request_views(bot: commands.Bot):
    try:
        pending = await asyncio.to_thread(get_pending_exit_requests)
        restored = 0

        for request in pending:
            if not request["request_id"] or not request["dm_message_id"]:
                continue

            bot.add_view(
                ExitRequestDMView(
                    request_id=request["request_id"],
                    player_id=request["discord_id"],
                ),
                message_id=request["dm_message_id"],
            )
            restored += 1

        print(f"[EXIT REQUEST] {restored} offene DM-Views wiederhergestellt.")

    except Exception as e:
        print(f"[EXIT REQUEST] Wiederherstellung fehlgeschlagen: {e}")




# =========================================================
# ADMINISTRATION
# =========================================================

class AdminOnlyView(PlayerBaseView):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False

        if not has_admin_role(interaction.user):
            await interaction.response.send_message(
                "⛔ Diese Funktion ist nur für Admins verfügbar.",
                ephemeral=True,
            )
            return False

        return True


class AdminSignupResetConfirmView(AdminOnlyView):
    @discord.ui.button(
        label="Ja, Anmeldungen zurücksetzen",
        style=discord.ButtonStyle.danger,
        row=0,
    )
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer()

        try:
            ws = await asyncio.to_thread(signup.get_worksheet)
            count = await asyncio.to_thread(signup.reset_signup_data, ws)

            await interaction.edit_original_response(
                embed=menu_embed(
                    "🟨 Administration → Anmeldung zurücksetzen",
                    f"**{count} Einträge wurden zurückgesetzt.**",
                ),
                view=AdminMenuView(owner_id=interaction.user.id),
                content=None,
            )
        except Exception as e:
            await interaction.edit_original_response(
                embed=menu_embed(
                    "🟨 Administration → Anmeldung zurücksetzen",
                    f"Fehler beim Zurücksetzen: {e}",
                ),
                view=AdminMenuView(owner_id=interaction.user.id),
                content=None,
            )

    @discord.ui.button(
        label="Abbrechen",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            embed=menu_embed("🟨 Administration", "Wähle eine Adminfunktion."),
            view=AdminMenuView(owner_id=interaction.user.id),
            content=None,
        )


class AdminQualiResetSelect(discord.ui.Select):
    def __init__(self, active_runs: dict):
        options = []

        for user_id, state in active_runs.items():
            runner_name = getattr(state, "runner_name", str(user_id))
            quali_number = getattr(state, "quali_number", "?")

            options.append(
                discord.SelectOption(
                    label=f"{runner_name} – Quali {quali_number}"[:100],
                    value=str(user_id),
                )
            )

        super().__init__(
            placeholder="Laufende Qualifikation auswählen …",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        if not has_admin_role(interaction.user):
            await interaction.response.send_message(
                "⛔ Keine Berechtigung.",
                ephemeral=True,
            )
            return

        cog = interaction.client.get_cog("QualiCog")

        if cog is None:
            await interaction.response.send_message(
                "Qualifikation ist aktuell nicht verfügbar.",
                ephemeral=True,
            )
            return

        target_user_id = int(self.values[0])
        active = cog.active_runs.pop(target_user_id, None)

        if active is None:
            await interaction.response.send_message(
                "Diese Qualifikation läuft nicht mehr.",
                ephemeral=True,
            )
            return

        active.cancelled = True
        cog.stop_state_tasks(active)

        await interaction.response.edit_message(
            embed=menu_embed(
                "🟨 Administration → Qualifikation zurücksetzen",
                f"Die laufende Qualifikation von **{active.runner_name}** wurde zurückgesetzt.",
            ),
            view=AdminMenuView(owner_id=interaction.user.id),
            content=None,
        )


class AdminQualiResetView(AdminOnlyView):
    def __init__(self, owner_id: int, active_runs: dict):
        super().__init__(owner_id)

        if active_runs:
            self.add_item(AdminQualiResetSelect(active_runs))

    @discord.ui.button(
        label="◀ Zurück",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def back_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            embed=menu_embed("🟨 Administration", "Wähle eine Adminfunktion."),
            view=AdminMenuView(owner_id=interaction.user.id),
            content=None,
        )



def get_player_direct_workbook():
    """Kompatibilitätswrapper: zentrale Season-Verbindung."""
    return get_season_spreadsheet()


def admin_spielplan_get_div_ws(div_number: str):
    wb = get_player_direct_workbook()

    try:
        return wb.worksheet(f"{div_number}.DIV")
    except Exception as e:
        raise RuntimeError(
            f"Tabellenblatt '{div_number}.DIV' konnte nicht geöffnet werden. "
            f"{type(e).__name__}: {e}"
        ) from e


def admin_spielplan_read_players(div_number: str) -> list[str]:
    ws = admin_spielplan_get_div_ws(div_number)

    values = col_values_cached(
        lambda: ws,
        sheet_name=player_sheet_name(ws, f"{div_number}.DIV"),
        col=12,  # L
        ttl_seconds=PLAYER_SHEET_CACHE_TTL_SECONDS,
    )

    players = []
    seen = set()

    # Regulärer Kaderbereich L2:L10
    for raw in values[1:10]:
        name = (raw or "").strip()
        if not name:
            continue

        key = normalize_name(name)
        if not key or key in seen:
            continue

        seen.add(key)
        players.append(name)

    if len(players) not in (8, 9):
        raise RuntimeError(
            f"Für Division {div_number} müssen genau 8 oder 9 Spieler "
            f"in Spalte L stehen. Gefunden: {len(players)}."
        )

    return players


def admin_spielplan_build_rounds(players: list[str]) -> list[list[tuple[str, str]]]:
    work = list(players)

    if len(work) % 2 == 1:
        work.append("BYE")

    n = len(work)
    half = n // 2
    rotation = work[:]
    rounds = []

    for _ in range(n - 1):
        left_half = rotation[:half]
        right_half = rotation[half:]
        right_rev = right_half[::-1]
        day_pairs = []

        for i in range(half):
            p1 = left_half[i]
            p2 = right_rev[i]

            if p1 == "BYE" or p2 == "BYE":
                continue

            day_pairs.append((p1, p2))

        rounds.append(day_pairs)

        fixed = rotation[0]
        tail = rotation[1:]
        tail = [tail[-1]] + tail[:-1]
        rotation = [fixed] + tail

    return rounds


def admin_spielplan_build_matches(players: list[str]) -> list[list[tuple[str, str]]]:
    hinrunde = admin_spielplan_build_rounds(players)
    rueckrunde = [
        [(away, home) for home, away in day]
        for day in hinrunde
    ]
    return hinrunde + rueckrunde


def admin_spielplan_find_next_free_row(ws) -> int:
    values = col_values_cached(
        lambda: ws,
        sheet_name=player_sheet_name(ws),
        col=4,  # D
        ttl_seconds=PLAYER_SHEET_CACHE_TTL_SECONDS,
    )

    for row_index, value in enumerate(values, start=1):
        if row_index == 1:
            continue

        if not (value or "").strip():
            return row_index

    return len(values) + 1


def admin_spielplan_write(ws, rounds: list[list[tuple[str, str]]]) -> int:
    start_row = admin_spielplan_find_next_free_row(ws)
    rows_to_write = []
    running_number = 1

    for matches_in_round in rounds:
        for home, away in matches_in_round:
            row_data = [""] * 9
            row_data[0] = str(running_number)
            row_data[3] = home
            row_data[4] = "vs"
            row_data[5] = away

            rows_to_write.append(row_data)
            running_number += 1

    if not rows_to_write:
        return 0

    end_row = start_row + len(rows_to_write) - 1
    cell_range = f"A{start_row}:I{end_row}"

    sheet_write_call(
        lambda: ws.update(cell_range, rows_to_write),
        invalidate_prefixes=player_invalidate_prefixes(ws),
    )

    return len(rows_to_write)


class AdminSpielplanDivisionSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Division wählen …",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=f"Division {i}", value=str(i))
                for i in range(1, 7)
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        if not has_admin_role(interaction.user):
            await interaction.response.send_message(
                "⛔ Keine Berechtigung.",
                ephemeral=True,
            )
            return

        div_number = self.values[0]
        await interaction.response.defer()

        try:
            players = await asyncio.to_thread(
                admin_spielplan_read_players,
                div_number,
            )

            rounds = admin_spielplan_build_matches(players)
            ws = await asyncio.to_thread(
                admin_spielplan_get_div_ws,
                div_number,
            )

            written = await asyncio.to_thread(
                admin_spielplan_write,
                ws,
                rounds,
            )

            await interaction.edit_original_response(
                embed=menu_embed(
                    "🟨 Administration → Spielplan erstellen",
                    (
                        f"**Division {div_number}**\n"
                        f"Spieler: **{len(players)}**\n"
                        f"Geschriebene Spiele: **{written}**"
                    ),
                ),
                view=AdminMenuView(owner_id=interaction.user.id),
                content=None,
            )
        except Exception as e:
            await interaction.edit_original_response(
                embed=menu_embed(
                    "🟨 Administration → Spielplan erstellen",
                    f"Fehler beim Erstellen: {e}",
                ),
                view=AdminMenuView(owner_id=interaction.user.id),
                content=None,
            )


class AdminSpielplanView(AdminOnlyView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)
        self.add_item(AdminSpielplanDivisionSelect())

    @discord.ui.button(
        label="◀ Zurück",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def back_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            embed=menu_embed("🟨 Administration", "Wähle eine Adminfunktion."),
            view=AdminMenuView(owner_id=interaction.user.id),
            content=None,
        )


class AdminTwitchModal(discord.ui.Modal, title="Twitchmapping setzen"):
    player_name = discord.ui.TextInput(
        label="Spielername",
        placeholder="Name wie im Runner-Sheet",
        required=True,
        max_length=100,
    )

    twitch = discord.ui.TextInput(
        label="Twitchkanal",
        placeholder="Username oder https://twitch.tv/...",
        required=True,
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not has_admin_role(interaction.user):
            await interaction.response.send_message(
                "⛔ Keine Berechtigung.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            set_runner_twitch = get_main_helper("set_runner_twitch")
            result = await asyncio.to_thread(
                set_runner_twitch,
                str(self.player_name.value),
                str(self.twitch.value),
            )

            action_text = (
                "aktualisiert"
                if result["action"] == "updated"
                else "neu angelegt"
            )

            await interaction.edit_original_response(
                content=(
                    f"✅ Twitchmapping {action_text}.\n"
                    f"**Spieler:** {result['player_name']}\n"
                    f"**Twitch:** {result['twitch']}\n"
                    f"**Runner-Zeile:** {result['row']}"
                )
            )
        except Exception as e:
            await interaction.edit_original_response(
                content=f"❌ Fehler beim Twitchmapping: {e}"
            )


class PlayerTwitchModal(discord.ui.Modal, title="Twitchkanal setzen"):
    twitch = discord.ui.TextInput(
        label="Twitchkanal",
        placeholder="Username oder https://twitch.tv/...",
        required=True,
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction):
        member = interaction.user

        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Nur auf dem Server verfügbar.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            set_runner_twitch = get_main_helper("set_runner_twitch")
            result = await asyncio.to_thread(
                set_runner_twitch,
                member.display_name.strip(),
                str(self.twitch.value),
            )

            await interaction.edit_original_response(
                content=(
                    "✅ Twitchkanal gespeichert.\n"
                    f"**Spieler:** {result['player_name']}\n"
                    f"**Twitch:** {result['twitch']}\n\n"
                    "Multistreamlinks verwenden ab jetzt diesen Eintrag aus **Runner!B**."
                )
            )
        except Exception as e:
            await interaction.edit_original_response(
                content=f"❌ Twitchkanal konnte nicht gespeichert werden: {e}"
            )


class AdminMenuView(AdminOnlyView):
    @discord.ui.button(
        label="Anmeldung zurücksetzen",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def signup_reset_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="⚠️ Anmeldungen zurücksetzen?",
                description=(
                    "Damit wird die bestehende **/resetsign**-Funktion ausgeführt.\n\n"
                    "Alle Saisonmeldungen werden zurückgesetzt. Fortfahren?"
                ),
                color=discord.Color.orange(),
            ),
            view=AdminSignupResetConfirmView(owner_id=interaction.user.id),
            content=None,
        )

    @discord.ui.button(
        label="Qualifikation zurücksetzen",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def quali_reset_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        cog = interaction.client.get_cog("QualiCog")
        active_runs = getattr(cog, "active_runs", {}) if cog else {}

        if not active_runs:
            await interaction.response.edit_message(
                embed=menu_embed(
                    "🟨 Administration → Qualifikation zurücksetzen",
                    "Aktuell läuft keine Qualifikation.",
                ),
                view=AdminMenuView(owner_id=interaction.user.id),
                content=None,
            )
            return

        await interaction.response.edit_message(
            embed=menu_embed(
                "🟨 Administration → Qualifikation zurücksetzen",
                "Wähle die laufende Qualifikation aus.",
            ),
            view=AdminQualiResetView(
                owner_id=interaction.user.id,
                active_runs=dict(active_runs),
            ),
            content=None,
        )

    @discord.ui.button(
        label="Spieler Austritt",
        style=discord.ButtonStyle.danger,
        row=1,
    )
    async def player_exit_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        try:
            player_exit_view = get_main_helper("PlayerExitDivisionSelectView")

            await interaction.response.edit_message(
                content="📤 Spieler-Exit starten:\nBitte Division auswählen.",
                embed=None,
                view=player_exit_view(requester=interaction.user),
            )
        except Exception as e:
            await interaction.response.send_message(
                f"Spieler-Austritt konnte nicht geöffnet werden: {e}",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Spielplan erstellen",
        style=discord.ButtonStyle.primary,
        row=1,
    )
    async def spielplan_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            embed=menu_embed(
                "🟨 Administration → Spielplan erstellen",
                "Wähle eine Division.",
            ),
            view=AdminSpielplanView(owner_id=interaction.user.id),
            content=None,
        )

    @discord.ui.button(
        label="Twitchmapping",
        style=discord.ButtonStyle.primary,
        row=2,
    )
    async def twitch_mapping_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_modal(AdminTwitchModal())

    @discord.ui.button(
        label="Coop League",
        style=discord.ButtonStyle.success,
        row=2,
    )
    async def coop_admin_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await coop.open_coop_admin_from_player(interaction)

    @discord.ui.button(
        label="Austritt Anfrage senden",
        style=discord.ButtonStyle.secondary,
        row=3,
    )
    async def exit_request_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            embed=menu_embed(
                "🟨 Administration → Austrittsanfrage",
                "Wähle zuerst die Division des Spielers.",
            ),
            view=ExitRequestDivisionSelectView(owner_id=interaction.user.id),
            content=None,
        )


    @discord.ui.button(
        label="◀ Zurück",
        style=discord.ButtonStyle.secondary,
        row=3,
    )
    async def back_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await show_player_dashboard(interaction)


class AdminMenuButton(discord.ui.Button):
    def __init__(self):
        # Discord bietet keine frei wählbare gelbe Buttonfarbe.
        # Deshalb gelbes Symbol + neutraler Button.
        super().__init__(
            label="🟨 Administration",
            style=discord.ButtonStyle.secondary,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        if not has_admin_role(interaction.user):
            await interaction.response.send_message(
                "⛔ Diese Funktion ist nur für Admins verfügbar.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            embed=menu_embed(
                "🟨 Administration",
                "Wähle eine Adminfunktion.",
            ),
            view=AdminMenuView(owner_id=interaction.user.id),
            content=None,
            attachments=[],
        )


# =========================================================
# SAISONMELDUNG
# =========================================================

class SeasonSignupMenuView(PlayerBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)

    @discord.ui.button(label="TFL Saison", style=discord.ButtonStyle.primary, row=0)
    async def tfl_signup_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if hasattr(signup, "open_signup_from_player"):
            await signup.open_signup_from_player(interaction)
            return

        await interaction.response.send_message(
            "Saisonmeldung ist aktuell nicht verfügbar.",
            ephemeral=True,
        )

    @discord.ui.button(label="Coop League", style=discord.ButtonStyle.success, row=0)
    async def coop_signup_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await coop.open_coop_menu_from_player(interaction)

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_player_dashboard(interaction)


# =========================================================
# HAUPTMENÜ
# =========================================================

class PlayerMenuView(PlayerBaseView):
    def __init__(
        self,
        owner_id: int,
        show_admin: bool = False,
        next_matches: list[dict] | None = None,
        division: int | None = None,
    ):
        super().__init__(owner_id)

        if division is not None:
            for match in (next_matches or [])[:3]:
                self.add_item(DashboardResultButton(match, int(division)))

        if show_admin:
            self.add_item(AdminMenuButton())

    # -----------------------------------------------------
    # Zeile 1: SPIELEN
    # -----------------------------------------------------

    @discord.ui.button(label="🎮 Spiel planen", style=discord.ButtonStyle.primary, row=0)
    async def plan_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("🎮 Spiel planen", "Wähle den Bereich für deine Spielplanung."),
            view=PlanMenuView(owner_id=interaction.user.id),
            content=None,
            attachments=[],
        )

    @discord.ui.button(label="📅 Termine anbieten", style=discord.ButtonStyle.success, row=0)
    async def term_offer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await term_offers.open_term_offer_modal(interaction)

    @discord.ui.button(label="✅ Ergebnis melden", style=discord.ButtonStyle.success, row=0)
    async def result_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("✅ Ergebnis melden", "Wähle League oder Cup."),
            view=ResultMenuView(owner_id=interaction.user.id),
            content=None,
            attachments=[],
        )

    # -----------------------------------------------------
    # Zeile 2: MEINE SAISON
    # -----------------------------------------------------

    @discord.ui.button(label="ℹ️ Info & Tabelle", style=discord.ButtonStyle.primary, row=1)
    async def info_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Info & Tabelle", "Saisoninfos, Meldestatus, Tabellen und weitere Übersichten."),
            view=InfoMenuView(owner_id=interaction.user.id),
            content=None,
            attachments=[],
        )

    @discord.ui.button(label="📋 Restprogramm", style=discord.ButtonStyle.primary, row=1)
    async def restprogramm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("📋 Restprogramm", "Zeige dein eigenes Restprogramm oder das eines anderen Spielers."),
            view=RestprogrammView(owner_id=interaction.user.id),
            content=None,
            attachments=[],
        )

    @discord.ui.button(label="🏆 Qualifikation", style=discord.ButtonStyle.primary, row=1)
    async def qualification_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if hasattr(asnyc, "open_quali_from_player"):
            await asnyc.open_quali_from_player(interaction)
            return

        await interaction.response.send_message(
            "Qualifikation ist aktuell nicht verfügbar.",
            ephemeral=True,
        )

    # -----------------------------------------------------
    # Zeile 3: SPIELERBEREICH
    # -----------------------------------------------------

    @discord.ui.button(label="📝 Saisonmeldung", style=discord.ButtonStyle.primary, row=2)
    async def season_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("📝 Saisonmeldung", "Wähle den Bereich für deine Saisonmeldung."),
            view=SeasonSignupMenuView(owner_id=interaction.user.id),
            content=None,
            attachments=[],
        )

    @discord.ui.button(label="⚡ Async", style=discord.ButtonStyle.primary, row=2)
    async def async_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("⚡ Async", "Beantrage oder spiele ein Async-Match."),
            view=AsyncMenuView(owner_id=interaction.user.id),
            content=None,
            attachments=[],
        )

    @discord.ui.button(label="⚙️ Einstellungen", style=discord.ButtonStyle.secondary, row=2)
    async def settings_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("⚙️ Einstellungen", "Verwalte Twitch, Restream-Angaben und Streichmodi."),
            view=SettingsMenuView(owner_id=interaction.user.id),
            content=None,
            attachments=[],
        )

    # -----------------------------------------------------
    # Zeile 4: DASHBOARD / KRITISCHE AKTION
    # -----------------------------------------------------

    @discord.ui.button(label="🗓️ Termine verwalten", style=discord.ButtonStyle.secondary, row=3)
    async def manage_schedule_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await term_offers.open_schedule_manage_menu(interaction)

    @discord.ui.button(label="📌 Meine Angebote", style=discord.ButtonStyle.secondary, row=3)
    async def my_offers_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await term_offers.open_my_offers_menu(interaction)

    @discord.ui.button(label="🔄 Dashboard aktualisieren", style=discord.ButtonStyle.secondary, row=3)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_player_dashboard(interaction)

    @discord.ui.button(label="🚪 Austritt", style=discord.ButtonStyle.danger, row=3)
    async def exit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user

        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Diese Funktion ist nur auf dem TFL-Server verfügbar.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="⚠️ Liga verlassen?",
                description=(
                    "Bist du dir absolut sicher, dass du die Liga verlassen möchtest? "
                    "Wenn du nun bestätigst, ist die Entscheidung final. "
                    "Zudem ist eine Teilnahme an der kommenden Saison damit ausgeschlossen."
                ),
                color=TFL_DANGER_COLOR,
            ),
            view=PlayerExitConfirmView(owner_id=interaction.user.id),
            content=None,
            attachments=[],
        )


# =========================================================
# ASYNC MENÜ
# =========================================================

class AsyncMenuView(PlayerBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)

    @discord.ui.button(label="Beantragen", style=discord.ButtonStyle.primary, row=0)
    async def beantragen_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_async_request_from_player(interaction)

    @discord.ui.button(label="Spielen", style=discord.ButtonStyle.success, row=0)
    async def spielen_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Discord erwartet innerhalb weniger Sekunden eine Bestätigung der
        # Component-Interaction. Deshalb wird der Klick hier sofort bestätigt,
        # bevor im Async-Modul Google-Sheets-Daten geladen werden.
        await interaction.response.defer()

        if hasattr(asnyc, "open_async_play_from_player"):
            await asnyc.open_async_play_from_player(
                interaction,
                already_deferred=True,
            )
            return

        await interaction.edit_original_response(
            content="Async spielen ist aktuell nicht verfügbar.",
            embed=None,
            view=None,
        )

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_player_dashboard(interaction)


# =========================================================
# ERGEBNIS MENÜ
# =========================================================

class ResultMenuView(PlayerBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)

    @discord.ui.button(label="League", style=discord.ButtonStyle.primary, row=0)
    async def league_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = PlayerLeagueResultViewStep1(author_id=interaction.user.id)
        view.state.kind = "Ergebnis League"

        await interaction.response.edit_message(
            content=view.render_summary(),
            view=view,
            embed=None,
        )

    @discord.ui.button(label="Cup", style=discord.ButtonStyle.primary, row=0)
    async def cup_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = PlayerCupResultView(author_id=interaction.user.id)
        view.state.kind = "Ergebnis Cup"

        await interaction.response.edit_message(
            content=view.render_summary(),
            view=view,
            embed=None,
        )

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_player_dashboard(interaction)


# =========================================================
# INFO MENÜ
# =========================================================

class InfoMenuView(PlayerBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)

    @discord.ui.button(label="Meldestatus", style=discord.ButtonStyle.primary, row=0)
    async def meldestatus_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Meldestatus", "Wähle einen Bereich."),
            view=MeldestatusView(owner_id=interaction.user.id),
            content=None,
        )

    @discord.ui.button(label="Qualifikation", style=discord.ButtonStyle.primary, row=0)
    async def qualifikation_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Qualifikation", "Wähle einen Bereich."),
            view=InfoQualifikationView(owner_id=interaction.user.id),
            content=None,
        )

    @discord.ui.button(label="Restprogramm", style=discord.ButtonStyle.primary, row=1)
    async def restprogramm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Restprogramm", "Wähle einen Bereich."),
            view=RestprogrammView(owner_id=interaction.user.id),
            content=None,
        )

    @discord.ui.button(label="Streichmodus", style=discord.ButtonStyle.primary, row=1)
    async def streichmodus_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Streichmodus", "Wähle einen Bereich."),
            view=StreichmodusView(owner_id=interaction.user.id),
            content=None,
        )

    @discord.ui.button(label="Ergebnisse/Tabelle", style=discord.ButtonStyle.primary, row=2)
    async def ergebnisse_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Ergebnisse/Tabelle", "Wähle eine Liga oder den Cup."),
            view=ErgebnisseTabelleView(owner_id=interaction.user.id),
            content=None,
        )

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=3)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_player_dashboard(interaction)


# =========================================================
# MELDESTATUS
# =========================================================

class MeldestatusView(PlayerBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)

    @discord.ui.button(label="Meiner", style=discord.ButtonStyle.primary, row=0)
    async def meiner_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user

        if not isinstance(member, discord.Member):
            text = "Nur auf dem Server verfügbar."
        else:
            try:
                text = signup.get_signup_status_text_for_member(member)
            except Exception as e:
                text = f"Fehler beim Abrufen deines Eintrags: {e}"

        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Meldestatus → Meiner", text),
            view=PlaceholderView(
                owner_id=interaction.user.id,
                back_view=MeldestatusView(owner_id=interaction.user.id),
                back_embed=menu_embed("ℹ️ Meldestatus", "Wähle einen Bereich."),
            ),
            content=None,
        )

    @discord.ui.button(label="League", style=discord.ButtonStyle.primary, row=0)
    async def league_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            text = signup.get_league_signup_text()
        except Exception as e:
            text = f"Fehler beim Abrufen der League-Anmeldungen: {e}"

        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Meldestatus → League", text),
            view=PlaceholderView(
                owner_id=interaction.user.id,
                back_view=MeldestatusView(owner_id=interaction.user.id),
                back_embed=menu_embed("ℹ️ Meldestatus", "Wähle einen Bereich."),
            ),
            content=None,
        )

    @discord.ui.button(label="Cup", style=discord.ButtonStyle.primary, row=0)
    async def cup_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            text = signup.get_cup_signup_text()
        except Exception as e:
            text = f"Fehler beim Abrufen der Cup-Anmeldungen: {e}"

        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Meldestatus → Cup", text),
            view=PlaceholderView(
                owner_id=interaction.user.id,
                back_view=MeldestatusView(owner_id=interaction.user.id),
                back_embed=menu_embed("ℹ️ Meldestatus", "Wähle einen Bereich."),
            ),
            content=None,
        )

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Info", "Wähle einen Bereich."),
            view=InfoMenuView(owner_id=interaction.user.id),
            content=None,
        )


# =========================================================
# INFO → QUALIFIKATION
# =========================================================

class InfoQualifikationView(PlayerBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)

    @discord.ui.button(label="Quali 1", style=discord.ButtonStyle.primary, row=0)
    async def quali1_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user

        await interaction.response.defer()

        if not isinstance(member, discord.Member):
            text = "Nur auf dem Server verfügbar."
        else:
            try:
                text = await build_quali_info_text(member, 1)
            except Exception as e:
                text = f"Fehler bei Quali 1: {e}"

        await interaction.edit_original_response(
            embed=menu_embed("ℹ️ Qualifikation → Quali 1", text),
            view=PlaceholderView(
                owner_id=interaction.user.id,
                back_view=InfoQualifikationView(owner_id=interaction.user.id),
                back_embed=menu_embed("ℹ️ Qualifikation", "Wähle einen Bereich."),
            ),
            content=None,
        )

    @discord.ui.button(label="Quali 2", style=discord.ButtonStyle.primary, row=0)
    async def quali2_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user

        await interaction.response.defer()

        if not isinstance(member, discord.Member):
            text = "Nur auf dem Server verfügbar."
        else:
            try:
                text = await build_quali_info_text(member, 2)
            except Exception as e:
                text = f"Fehler bei Quali 2: {e}"

        await interaction.edit_original_response(
            embed=menu_embed("ℹ️ Qualifikation → Quali 2", text),
            view=PlaceholderView(
                owner_id=interaction.user.id,
                back_view=InfoQualifikationView(owner_id=interaction.user.id),
                back_embed=menu_embed("ℹ️ Qualifikation", "Wähle einen Bereich."),
            ),
            content=None,
        )

    @discord.ui.button(label="Gesamt", style=discord.ButtonStyle.primary, row=0)
    async def gesamt_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user

        await interaction.response.defer()

        if not isinstance(member, discord.Member):
            text = "Nur auf dem Server verfügbar."
        else:
            try:
                text = await build_quali_overall_text(member)
            except Exception as e:
                text = f"Fehler beim Gesamtstand: {e}"

        await interaction.edit_original_response(
            embed=menu_embed("ℹ️ Qualifikation → Gesamt", text),
            view=PlaceholderView(
                owner_id=interaction.user.id,
                back_view=InfoQualifikationView(owner_id=interaction.user.id),
                back_embed=menu_embed("ℹ️ Qualifikation", "Wähle einen Bereich."),
            ),
            content=None,
        )

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Info", "Wähle einen Bereich."),
            view=InfoMenuView(owner_id=interaction.user.id),
            content=None,
        )


# =========================================================
# RESTPROGRAMM
# =========================================================

class RestOtherPlayerSelect(discord.ui.Select):
    def __init__(self, division: str, players: list[str], owner_id: int):
        self.division = division
        self.owner_id = owner_id

        options = [discord.SelectOption(label=p, value=p) for p in players[:25]]

        super().__init__(
            placeholder="Spieler wählen …",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        player = self.values[0]

        try:
            text = restinfo.format_restprogramm_text(self.division, player)
        except Exception as e:
            text = f"Fehler beim Ermitteln des Restprogramms: {e}"

        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Restprogramm → Andere", text),
            view=PlaceholderView(
                owner_id=interaction.user.id,
                back_view=RestOtherDivisionView(owner_id=interaction.user.id),
                back_embed=menu_embed("ℹ️ Restprogramm → Andere", "Wähle eine Division."),
            ),
            content=None,
        )


class RestOtherPlayerView(PlayerBaseView):
    def __init__(self, owner_id: int, division: str, players: list[str]):
        super().__init__(owner_id)
        self.add_item(RestOtherPlayerSelect(division, players, owner_id))

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Restprogramm → Andere", "Wähle eine Division."),
            view=RestOtherDivisionView(owner_id=interaction.user.id),
            content=None,
        )


class RestOtherDivisionSelect(discord.ui.Select):
    def __init__(self, owner_id: int):
        self.owner_id = owner_id

        options = [
            discord.SelectOption(label="Division 1", value="1"),
            discord.SelectOption(label="Division 2", value="2"),
            discord.SelectOption(label="Division 3", value="3"),
            discord.SelectOption(label="Division 4", value="4"),
            discord.SelectOption(label="Division 5", value="5"),
            discord.SelectOption(label="Division 6", value="6"),
        ]

        super().__init__(
            placeholder="Division wählen …",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        div_number = self.values[0]

        try:
            players = restinfo.list_rest_players(div_number)
        except Exception as e:
            await interaction.response.edit_message(
                embed=menu_embed(
                    "ℹ️ Restprogramm → Andere",
                    f"Fehler beim Laden der Spieler für Division {div_number}: {e}",
                ),
                view=RestOtherDivisionView(owner_id=interaction.user.id),
                content=None,
            )
            return

        if not players:
            await interaction.response.edit_message(
                embed=menu_embed(
                    "ℹ️ Restprogramm → Andere",
                    f"Keine Spieler in Division {div_number} für das Restprogramm gefunden.",
                ),
                view=RestOtherDivisionView(owner_id=interaction.user.id),
                content=None,
            )
            return

        await interaction.response.edit_message(
            embed=menu_embed(
                "ℹ️ Restprogramm → Andere",
                f"**Division {div_number}**\nWähle einen Spieler.",
            ),
            view=RestOtherPlayerView(
                owner_id=interaction.user.id,
                division=div_number,
                players=players,
            ),
            content=None,
        )


class RestOtherDivisionView(PlayerBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)
        self.add_item(RestOtherDivisionSelect(owner_id))

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Restprogramm", "Wähle einen Bereich."),
            view=RestprogrammView(owner_id=interaction.user.id),
            content=None,
        )


class RestprogrammView(PlayerBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)

    @discord.ui.button(label="Eigenes", style=discord.ButtonStyle.primary, row=0)
    async def eigenes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user

        await interaction.response.defer()

        if not isinstance(member, discord.Member):
            text = "Nur auf dem Server verfügbar."
        else:
            try:
                name_candidates = get_name_candidates(member)
                text = await asyncio.to_thread(
                    restinfo.get_open_restprogramm_text_for_name_candidates,
                    name_candidates,
                )
            except Exception as e:
                text = f"Fehler beim Abrufen deines Restprogramms: {e}"

        await interaction.edit_original_response(
            embed=menu_embed("ℹ️ Restprogramm → Eigenes", text),
            view=PlaceholderView(
                owner_id=interaction.user.id,
                back_view=RestprogrammView(owner_id=interaction.user.id),
                back_embed=menu_embed("ℹ️ Restprogramm", "Wähle einen Bereich."),
            ),
            content=None,
        )

    @discord.ui.button(label="Andere", style=discord.ButtonStyle.primary, row=0)
    async def andere_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Restprogramm → Andere", "Wähle eine Division."),
            view=RestOtherDivisionView(owner_id=interaction.user.id),
            content=None,
        )

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Info", "Wähle einen Bereich."),
            view=InfoMenuView(owner_id=interaction.user.id),
            content=None,
        )


# =========================================================
# STREICHMODUS INFO
# =========================================================

class StreichOtherDivisionSelect(discord.ui.Select):
    def __init__(self, owner_id: int):
        self.owner_id = owner_id

        options = [
            discord.SelectOption(label="Division 1", value="1"),
            discord.SelectOption(label="Division 2", value="2"),
            discord.SelectOption(label="Division 3", value="3"),
            discord.SelectOption(label="Division 4", value="4"),
            discord.SelectOption(label="Division 5", value="5"),
            discord.SelectOption(label="Division 6", value="6"),
        ]

        super().__init__(
            placeholder="Division wählen …",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        div_number = self.values[0]

        try:
            text = restinfo.get_streich_text_for_division(div_number)
        except Exception as e:
            text = f"Fehler beim Abrufen des Streichmodus: {e}"

        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Streichmodus → Andere Divisionen", text),
            view=PlaceholderView(
                owner_id=interaction.user.id,
                back_view=StreichOtherDivisionView(owner_id=interaction.user.id),
                back_embed=menu_embed("ℹ️ Streichmodus → Andere Divisionen", "Wähle eine Division."),
            ),
            content=None,
        )


class StreichOtherDivisionView(PlayerBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)
        self.add_item(StreichOtherDivisionSelect(owner_id))

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Streichmodus", "Wähle einen Bereich."),
            view=StreichmodusView(owner_id=interaction.user.id),
            content=None,
        )


class StreichmodusView(PlayerBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)

    @discord.ui.button(label="Eigene Division", style=discord.ButtonStyle.primary, row=0)
    async def eigene_division_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user

        await interaction.response.defer()

        if not isinstance(member, discord.Member):
            text = "Nur auf dem Server verfügbar."
        else:
            try:
                name_candidates = get_name_candidates(member)
                text = await asyncio.to_thread(
                    restinfo.get_own_division_streich_text,
                    name_candidates,
                )
            except Exception as e:
                text = f"Fehler beim Abrufen des Streichmodus: {e}"

        await interaction.edit_original_response(
            embed=menu_embed("ℹ️ Streichmodus → Eigene Division", text),
            view=PlaceholderView(
                owner_id=interaction.user.id,
                back_view=StreichmodusView(owner_id=interaction.user.id),
                back_embed=menu_embed("ℹ️ Streichmodus", "Wähle einen Bereich."),
            ),
            content=None,
        )

    @discord.ui.button(label="Andere Divisionen", style=discord.ButtonStyle.primary, row=0)
    async def andere_divisionen_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Streichmodus → Andere Divisionen", "Wähle eine Division."),
            view=StreichOtherDivisionView(owner_id=interaction.user.id),
            content=None,
        )

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Info", "Wähle einen Bereich."),
            view=InfoMenuView(owner_id=interaction.user.id),
            content=None,
        )


# =========================================================
# ERGEBNISSE / TABELLE
# =========================================================

class ErgebnisseTabelleView(PlayerBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)

        self.add_item(discord.ui.Button(
            label="1. Div",
            style=discord.ButtonStyle.link,
            url="https://tryforceleague.de/index.php/1-division",
            row=0,
        ))

        self.add_item(discord.ui.Button(
            label="2. Div",
            style=discord.ButtonStyle.link,
            url="https://tryforceleague.de/index.php/1-division-2",
            row=0,
        ))

        self.add_item(discord.ui.Button(
            label="3. Div",
            style=discord.ButtonStyle.link,
            url="https://tryforceleague.de/index.php/3-division",
            row=0,
        ))

        self.add_item(discord.ui.Button(
            label="4. Div",
            style=discord.ButtonStyle.link,
            url="https://tryforceleague.de/index.php/3-division-2",
            row=1,
        ))

        self.add_item(discord.ui.Button(
            label="5. Div",
            style=discord.ButtonStyle.link,
            url="https://tryforceleague.de/index.php/3-division-3",
            row=1,
        ))

        self.add_item(discord.ui.Button(
            label="6. Div",
            style=discord.ButtonStyle.link,
            url="https://tryforceleague.de/index.php/3-division-4",
            row=1,
        ))

        self.add_item(discord.ui.Button(
            label="Cup",
            style=discord.ButtonStyle.link,
            url="https://tryforceleague.de/index.php/cup",
            row=2,
        ))

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=3)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=menu_embed("ℹ️ Info", "Wähle einen Bereich."),
            view=InfoMenuView(owner_id=interaction.user.id),
            content=None,
        )


# =========================================================
# EINSTELLUNGEN
# =========================================================

class SettingsMenuView(PlayerBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)

    @discord.ui.button(label="Twitch setzen", style=discord.ButtonStyle.primary, row=0)
    async def twitch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PlayerTwitchModal())

    @discord.ui.button(label="Restream/Commentary/Tracker", style=discord.ButtonStyle.primary, row=0)
    async def restream_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if hasattr(signup, "open_signup_from_player"):
            await signup.open_signup_from_player(interaction)
            return

        await interaction.response.send_message(
            "Restream/Commentary/Tracker ist aktuell nicht verfügbar.",
            ephemeral=True,
        )

    @discord.ui.button(label="Streichmodis setzen", style=discord.ButtonStyle.success, row=1)
    async def streich_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user

        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Nur auf dem Server verfügbar.", ephemeral=True)
            return

        await interaction.response.defer()

        try:
            name_candidates = get_name_candidates(member)

            state = await asyncio.to_thread(
                load_streichmodus_state_for_name_candidates,
                name_candidates,
            )

            if not state["found"]:
                raise RuntimeError("Du wurdest in keiner Division in Spalte L gefunden.")

            div_number = state["div_number"]

            modes = await asyncio.to_thread(
                get_division_modes_for_streichmodus,
                div_number,
            )

            if not modes:
                raise RuntimeError(
                    f"Für Division {div_number} wurden keine erlaubten Streichmodi im Sheet gefunden."
                )

            view = StreichmodusSettingView(
                owner_id=interaction.user.id,
                modes=modes,
                mode_1=state["mode_1"],
                mode_2=state["mode_2"],
                div_number=div_number,
                change_used=state["change_used"],
            )

            await interaction.edit_original_response(
                embed=view.build_embed(),
                view=view,
                content=None,
            )

        except Exception as e:
            await interaction.edit_original_response(
                embed=menu_embed(
                    "⚙️ Einstellungen → Streichmodis setzen",
                    f"Fehler beim Laden: {e}",
                ),
                view=PlaceholderView(
                    owner_id=interaction.user.id,
                    back_view=SettingsMenuView(owner_id=interaction.user.id),
                    back_embed=menu_embed("⚙️ Einstellungen", "Wähle einen Bereich."),
                ),
                content=None,
            )

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary, row=2)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_player_dashboard(interaction)


# =========================================================
# COG
# =========================================================

class PlayerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="player", description="Öffnet das Spielermenü")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def player(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await show_player_dashboard(
                interaction,
                already_deferred=True,
            )
        except Exception as exc:
            print(f"❌ [PLAYER] /player konnte nicht aufgebaut werden: {exc}")
            traceback.print_exc()
            try:
                await interaction.edit_original_response(
                    content=(
                        "❌ Das Spieler-Dashboard konnte gerade nicht aufgebaut werden. "
                        "Bitte versuche /player erneut."
                    ),
                    embed=None,
                    view=None,
                    attachments=[],
                )
            except Exception:
                try:
                    await interaction.followup.send(
                        "❌ Das Spieler-Dashboard konnte gerade nicht aufgebaut werden. Bitte versuche /player erneut.",
                        ephemeral=True,
                    )
                except Exception:
                    pass


async def setup(bot: commands.Bot):
    # /player ist die Kernfunktion. Zusatzfunktionen wie Achievements dürfen
    # das Laden des Cogs niemals verhindern.
    try:
        _install_achievement_hooks()
    except Exception as exc:
        print(f"⚠️ [PLAYER] Achievement-Hooks konnten nicht installiert werden: {exc}")
        traceback.print_exc()

    await bot.add_cog(PlayerCog(bot))

    # Persistente Buttons offener Anfragen nach Neustart wieder registrieren.
    await restore_exit_request_views(bot)

    # Offene Terminangebote ebenfalls persistent registrieren. Alte Posts aus
    # Versionen ohne Persistenz werden nach on_ready automatisch migriert.
    try:
        await term_offers.restore_persistent_offer_views(bot)
    except Exception as exc:
        print(f"⚠️ [PLAYER] Terminangebote konnten nicht wiederhergestellt werden: {exc}")

    # Fristen liegen im Google Sheet und überstehen dadurch Bot-Neustarts.
    if not hasattr(bot, "_exit_request_monitor_task"):
        bot._exit_request_monitor_task = asyncio.create_task(
            exit_request_monitor_loop(bot)
        )
