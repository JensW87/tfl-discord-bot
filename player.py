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
