#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Game of the Day sender  v1  (13 Nov 2026, Claude conversation
"PlayWithEngine_2026-09-28 deep analysis / Arasan integration", KT section 291)

A small web form that runs ONLY on this PC (http://127.0.0.1, never reachable from outside).
Choose a .pgn (chessgames.com / Lichess / Chess.com), type the title + story (+ optional key move),
press Send. The tool then:
  1. checks every move (python-chess), the key move, duplicates, the push size;
  2. sends the FCM push (topic "gotd"; today's game + the previous 3 for phones that missed days);
  3. publishes the signed "latest 30 games" file to GitHub Pages (the mailbox the app reads);
  4. keeps everything in ../../data (outside every version folder, so updates never lose it):
       gotd_history.db  (SQLite: games, every send, tags)   sent_log.csv   send_record.txt
       pgn/  (readable copies)   backups/  (db copy before each send, last 30)
       config.json / firebase_key.json / feed_signing_key.pem   (PRIVATE - never share)

Wire format = what the app's GotdValidator accepts (all strings): key, sid, kind, title, w, b, event,
year, res, eco, opening, tags, tagn, gn, story, moves, km, kmu, rev.

14 Nov 2026 (KT section 299, audit fixes, same v1 folder):
  - a correction ("fix") keeps the game's ORIGINAL send id + a higher "rev" (it no longer jumps to today / the top);
    "Fix that game" is also offered when the SAME game was sent before (story typo, key move);
  - a built-in game cannot be "resent" (phones already have it); "Feature again" / "Fix" only;
  - the PC clock is checked against Google's clock before every send (a wrong date would stick on top);
  - the push is signed with the feed key ("sig"); the app drops unsigned / wrongly signed pushes;
  - a spare signing key is kept + one backup file of both keys (data/KEY_BACKUP) - the app trusts both, so a
    lost PC key never needs an app update; a backup file found at start is restored by itself;
  - a game too long for the 4 KB push: the form says how much to shorten the story; if even that cannot help,
    it goes through the mailbox only;
  - test sends no longer use up the day's number (row id "<sid>-t001"); fixes are stored as "<sid>-r001".

14 Nov 2026 (v2, STABLE 15, KT section 303, owner-approved demo puzzle-of-the-day v6 "7 · On your PC"):
  - "What are you sending?": Game of the Day (as before) or Puzzle of the Day (find the move, then the full game).
    A puzzle needs the key move; the line to find is worked out (to the mate when the game ends in mate within
    15 half-moves, else just the key move) and the Question is filled in ("Mate in 2" / "Best move", editable).
    Wire fields added: t ("puzzle"), pl (line length in half-moves), q (question). Puzzle key = first 16 hex of
    sha256(moves + "|p|" + key ply) - so a game and its puzzle are separate entries on phones.
  - "Add to today's list": several games / puzzles are kept as "queued" and go out together with
    "Send all N (one alert)" - ONE push (the phones show one notification: the first item + "N more") and ONE
    mailbox update. Queued items never reach the mailbox before they are sent.
  - The mailbox file holds the latest 40 items (games + puzzles); history shows puzzles.
  - Built-in items that are new in the app's seed (the 25 bundled puzzles) are added to the history by themselves.

14 Nov 2026 (v3, KT section 305, owner-approved demo potd-fen-demo-versions/v2):
  - "What are you sending?" has a third choice, "Puzzle from a position": paste a FEN + the solution (your moves and
    the replies). The tool checks everything before Send is possible (same rules as the app): a legal position,
    every move legal, odd 1..15 half-moves; for mates up to mate in 4 (a plain search, no engine): no faster mate,
    forced, and only one move mates as fast at every solver move before the last; a non-mate line is refused when a
    mate in 1-2 exists (players would be told the mate is wrong). "Find it for me" fills the shortest forced mate.
  - Wire: t=puzzle, fen, moves (UCI from the FEN), km=0, kmu, pl=all moves, q, title, event=credit, tags; no players
    (older apps reject it cleanly). Key = sha256("pos|" + FEN parts 1-4 + "|" + moves)[:16]. History stores the FEN
    (games.fen, added by itself); PGN copies get [SetUp]/[FEN]; today's list / history show POSITION.

14 Nov 2026 (v4, KT section 306, owner: every item stored in git, GitHub used as the server / database for now):
  - The complete history is also kept in the GitHub repo, one file per year: gotd/history/<year>.json (every send:
    games, puzzles from games, position puzzles, again / fix, the built-in items; not tests). Rewritten after every
    real send; a failed upload is retried on the next send / start and never blocks a send. The first start of v4
    uploads every year once. Setup tab: "Upload the full history now". Phones still read only latest.json (40).

15 Nov 2026 (v5, KT section 318, owner-approved demo gotd-admin-app-demo-versions/v2 - the GOTD Admin app):
  - ONE history for the PC and the Admin app: before every send (and at start / before a history upload) the tool
    reads gotd/history/*.json from GitHub and adds the rows it does not have (the phone's sends) - so "sent before",
    corrections and the day number (#1, #2) see both. A row the phone retried successfully updates the PC's row.
  - Never two senders at once: before a send the tool waits while an Admin-app send (GitHub Action "gotd-admin")
    is running, then holds a short lock file (lock/pc.json on the gotd-outbox branch) that the Action waits for.
  - GOTD_DATA (environment) = another data folder: the GitHub Action uses this same file for every check and send.
  - SETUP_ADMIN_APP (gotd_sender.py --setup-admin, admin_setup.py): sets up the Action and the phone (QR).
  - v7 (KT 353): SEND_GAME_OF_THE_DAY is the only launcher - one_click.py runs whatever is still missing first.
"""
import base64
import csv
import datetime as dt
import email.utils
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import chess
import chess.pgn
import chess.svg
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding

TOOL_VERSION = "v9_2026-11-19"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_DIR = os.path.dirname(APP_DIR)
TOOL_ROOT = os.path.dirname(os.path.dirname(VERSION_DIR))          # 42_game-of-the-day
DATA = os.environ.get("GOTD_DATA") or os.path.join(TOOL_ROOT, "data")   # v5: the Action uses its own folder
DB_PATH = os.path.join(DATA, "gotd_history.db")
CONFIG = os.path.join(DATA, "config.json")
FIREBASE_KEY = os.path.join(DATA, "firebase_key.json")
SIGNING_KEY = os.path.join(DATA, "feed_signing_key.pem")
SPARE_KEY = os.path.join(DATA, "feed_signing_key_spare.pem")
KEY_BACKUP_DIR = os.path.join(DATA, "KEY_BACKUP")
KEY_BACKUP_NAME = "GOTD_FEED_KEYS_BACKUP.pem"
KEY_BACKUP = os.path.join(KEY_BACKUP_DIR, KEY_BACKUP_NAME)
CLOCK_TOLERANCE_S = 15 * 60
RECORD = os.path.join(DATA, "send_record.txt")
CSV_LOG = os.path.join(DATA, "sent_log.csv")
PGN_DIR = os.path.join(DATA, "pgn")
BACKUP_DIR = os.path.join(DATA, "backups")
EXPORT_DIR = os.path.join(DATA, "exports")

TOPIC = "gotd"
TEST_TOPIC = "gotd-test"
FEED_PATH = "gotd/v1/latest.json"
FEED_GAMES = 40                # games + puzzles (v2)
PUSH_EXTRA = 3
PUSH_LIMIT = 3900            # FCM allows 4096 bytes of data; keep a margin
MAX_STORY = 300
MAX_TITLE = 80
MAX_PLIES = 600
PORT = 8765

KEY_RE = re.compile(r"^[0-9a-f]{8,64}$")
TAG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
UNSAFE_RE = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁠-⁤﻿]")
WS_RE = re.compile(r"\s+")

_lock = threading.Lock()
_token_cache = {"token": None, "exp": 0}


# ──────────────────────────────────────────────────────────── records

def record(line):
    os.makedirs(DATA, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(RECORD, "a", encoding="utf-8") as f:
        f.write(f"{stamp}  {line}\n")


def clean(s, max_len):
    if not s:
        return ""
    t = WS_RE.sub(" ", UNSAFE_RE.sub(" ", str(s))).strip()
    return t[:max_len].rstrip()


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_config(cfg):
    os.makedirs(DATA, exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG)


# ──────────────────────────────────────────────────────────── database

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
  key TEXT PRIMARY KEY, title TEXT, white TEXT, black TEXT, event TEXT, year TEXT, result TEXT,
  eco TEXT, opening TEXT, round TEXT, tags TEXT, story TEXT, moves TEXT, plies INTEGER,
  key_ply INTEGER, key_uci TEXT, pgn_original TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS sends (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT UNIQUE, key TEXT, kind TEXT, sent_at TEXT, test INTEGER,
  item TEXT, push_status TEXT, push_detail TEXT, feed_status TEXT, feed_detail TEXT, push_bytes INTEGER);
CREATE INDEX IF NOT EXISTS sends_key ON sends(key);
CREATE TABLE IF NOT EXISTS tags (id TEXT PRIMARY KEY, name TEXT, aliases TEXT);
"""

DEFAULT_TAGS = [
    ("famous-games", "Famous games", "famous,classic,classics,famous game,immortal"),
]


def db():
    os.makedirs(DATA, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db(skip_builtins=None):
    """v6 (KT 331): [skip_builtins] = built-in keys the owner deleted (gotd/history/meta.json) - not added back."""
    skip_builtins = set(skip_builtins if skip_builtins is not None else _META.get("hidden_builtins", []))
    with db() as con:
        con.executescript(SCHEMA)
        # v3 (KT section 305): start position of a puzzle sent as a FEN ("" = a game / a puzzle from a game).
        if "fen" not in [r[1] for r in con.execute("PRAGMA table_info(games)").fetchall()]:
            con.execute("ALTER TABLE games ADD COLUMN fen TEXT DEFAULT ''")
        for t in DEFAULT_TAGS:
            con.execute("INSERT OR IGNORE INTO tags(id,name,aliases) VALUES(?,?,?)", t)
    seed = os.path.join(APP_DIR, "seed", "gotd_seed.json")
    with db() as con:
        known = {r[0] for r in con.execute("SELECT key FROM sends WHERE kind='bundled'").fetchall()}
    if os.path.exists(seed):
        with open(seed, encoding="utf-8") as f:
            games = [it for it in json.load(f).get("games", []) if it.get("key") not in known and it.get("key") not in skip_builtins]
    else:
        games = []
    # v2: every built-in item the history does not know yet (the first run: all; after an update: the new ones).
    if games:
        with db() as con:
            for it in games:
                con.execute(
                    "INSERT OR IGNORE INTO games(key,title,white,black,event,year,result,eco,opening,round,tags,story,"
                    "moves,plies,key_ply,key_uci,pgn_original,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (it["key"], it["title"], it.get("w", ""), it.get("b", ""), it.get("event", ""), it.get("year", ""), it.get("res", "*"),
                     it.get("eco", ""), it.get("opening", ""), it.get("gn", ""), it.get("tags", ""), it.get("story", ""),
                     it["moves"], len(it["moves"].split()), -1, "", "", now_iso()))
                con.execute(
                    "INSERT OR IGNORE INTO sends(sid,key,kind,sent_at,test,item,push_status,push_detail,feed_status,"
                    "feed_detail,push_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (it["sid"], it["key"], "bundled", now_iso(), 0, json.dumps(it, ensure_ascii=False),
                     "inside app", "", "inside app", "", 0))
        record(f"history: imported {len(games)} built-in items that ship inside the app (they count as already sent)")


def now_iso():
    return dt.datetime.now().replace(microsecond=0).isoformat()


def backup_db():
    if not os.path.exists(DB_PATH):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = "gotd_history_" + dt.datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".db"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(os.path.join(BACKUP_DIR, name))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db"))
    for old in files[:-30]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass


# ──────────────────────────────────────────────────────────── openings + PGN

_OPENINGS = None


def openings():
    global _OPENINGS
    if _OPENINGS is None:
        table = {}
        for letter in "abcde":
            path = os.path.join(APP_DIR, "openings", f"{letter}.tsv")
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                for row in csv.DictReader(f, delimiter="\t"):
                    b = chess.Board()
                    try:
                        for tok in row["pgn"].split():
                            if re.match(r"^\d+\.", tok):
                                continue
                            b.push_san(tok)
                    except ValueError:
                        continue
                    table[b.epd()] = (row["eco"], row["name"])
        _OPENINGS = table
    return _OPENINGS


def person(name):
    """'Carlsen, Magnus' -> 'Magnus Carlsen'; '?' -> ''."""
    n = clean(name, 60)
    if n in ("?", "-", "NN", "N.N."):
        return ""
    if n.count(",") == 1 and " / " not in n:
        last, first = [p.strip() for p in n.split(",")]
        if first and last:
            n = f"{first} {last}"
    return n


def slug(s):
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return re.sub(r"-{2,}", "-", s)[:40].strip("-")


def game_key(uci_moves):
    return hashlib.sha256(" ".join(uci_moves).encode()).hexdigest()[:16]


def puzzle_key(uci_moves, ply):
    """v2: a Puzzle of the Day on this game, starting at [ply] - its own entry on phones (the app's seed uses the same)."""
    return hashlib.sha256((" ".join(uci_moves) + "|p|" + str(ply)).encode()).hexdigest()[:16]


MAX_LINE = 15


def puzzle_line(a, ply):
    """v2: the line to find from the key move: to the mate when the game ends in mate within MAX_LINE half-moves
    (an odd number - it ends on the solver's move), else the key move alone. Returns (plies, question, label)."""
    uci, sans = a["uci"], a["sans"]
    rest = len(uci) - ply
    b = chess.Board()
    for u in uci:
        b.push_uci(u)
    if b.is_checkmate() and 1 <= rest <= MAX_LINE and rest % 2 == 1:
        pl, q = rest, f"Mate in {(rest + 1) // 2}"
    else:
        pl, q = 1, "Best move"
    parts = []
    for i in range(ply, ply + pl):
        n = i // 2 + 1
        if i % 2 == 0:
            parts.append(f"{n}.{sans[i]}")
        else:
            parts.append(f"{n}...{sans[i]}" if i == ply else sans[i])
    return pl, q, " ".join(parts)


# ── KT 372 (17 Nov 2026, conversation "PlayWithEngine_2026-09-28 deep analysis / Arasan integration"): every site's PGN
# (Chess.com live / daily / analysis, Lichess game / study / broadcast, chessgames.com, ChessBase, TWIC, chess24,
# 365chess, text copied from web pages) is first turned into plain PGN: headers + main-line moves + result. Comments
# {..} / ; .., variations (..), NAGs $n, % lines, clocks / evals / arrows and move glyphs (!, ?, ±, N ...) are dropped,
# so nobody's annotations are ever stored or sent. Copy-paste quirks are fixed: smart quotes in headers, several
# headers on one line, figurines (♘f3), "…", ½-½, en dashes, 0-0, e.p., ++, invisible / non-breaking spaces.
# The Admin app (GotdLogic.kt Pgn.read) does exactly the same, step by step.
_PGN_KEY_FIRST = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
_PGN_KEY_REST = _PGN_KEY_FIRST | set("_+#=:-")
_PGN_SMART_QUOTES = str.maketrans({c: '"' for c in "\u201c\u201d\u201e\u201f\u2033\xab\xbb"})
_PGN_SPACES = re.compile("[\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\u0085\t\x0b\x0c]")
_PGN_INVISIBLE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\xad]")
_PGN_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"})
_PGN_FIGURINES = str.maketrans({"♔": "K", "♚": "K", "♕": "Q", "♛": "Q", "♖": "R", "♜": "R",
                                "♗": "B", "♝": "B", "♘": "N", "♞": "N", "♙": "", "♟": ""})
_PGN_GLYPHS = set("!?+-=/±∓⩲⩱∞□⨀○●→←↑↓⇆⇄⇔Δ∇⌓⊥⟳‼⁇⁉⁈")
_PGN_TRAIL = "".join(sorted(_PGN_GLYPHS - set("+-=/")))    # glyphs stripped off the end of a move (Nf3±, Qxf7!!)
_PGN_RESULTS = ("1-0", "0-1", "1/2-1/2", "*")
_PGN_SCAN = re.compile(r"\{[^}]*\}?|;[^\n]*|[()}]|[ ]+|[^ \n{};()]+")
PGN_MAX_GAMES = 200


def _pgn_header_line(line):
    """'[Event "x"] [Site "y"]' -> [("Event", "x"), ("Site", "y")]; None when the line is not only header pairs.
    Read by hand (no regex), the same way as the Admin app - safe on any line, however long."""
    out, i, n = [], 0, len(line)
    while True:
        while i < n and line[i] == " ":
            i += 1
        if i == n:
            return out or None
        if line[i] != "[":
            return None
        i += 1
        while i < n and line[i] == " ":
            i += 1
        k0 = i
        if i < n and line[i] in _PGN_KEY_FIRST:
            i += 1
            while i < n and line[i] in _PGN_KEY_REST:
                i += 1
        key = line[k0:i]
        if not key or i >= n or line[i] != " ":
            return None
        while i < n and line[i] == " ":
            i += 1
        if i >= n or line[i] != '"':
            return None
        i += 1
        val = []
        while i < n and line[i] != '"':
            if line[i] == "\\":
                if i + 1 >= n:
                    return None
                val.append(line[i + 1])
                i += 2
            else:
                val.append(line[i])
                i += 1
        if i >= n:
            return None
        i += 1
        while i < n and line[i] == " ":
            i += 1
        if i >= n or line[i] != "]":
            return None
        i += 1
        out.append((key, "".join(val)))


def _pgn_token(tok):
    """One movetext token -> ("result", r) | ("move", san) | None (dropped)."""
    if tok.startswith("$"):                            # NAG ($1 good move, $146 novelty ...)
        return None
    t = tok.translate(_PGN_DASHES).replace("\xbd", "1/2").replace("\u2026", "...")   # 1/2 sign, ellipsis
    if t in _PGN_RESULTS:
        return ("result", t)
    t = re.sub(r"^[0-9]+\.+", "", t)                  # 12. / 12... / 12.e4 / 12...Qd5
    if t in _PGN_RESULTS:
        return ("result", t)
    if not t or set(t) <= {"."}:
        return None
    if t.lower() in ("e.p.", "e.p", "ep"):
        return None
    t = t.translate(_PGN_FIGURINES)
    if t in ("N", "TN", "") or (t != "--" and set(t) <= _PGN_GLYPHS):
        return None
    t = re.sub(r"(?<=[1-8])e\.?p\.?$", "", t)          # exd6e.p.
    t = t.rstrip(_PGN_TRAIL)
    t = re.sub(r"(\+/?[-=]|[-=]/?\+)$", "", t)          # an evaluation glued on: Nf3+- / Nf3=+ / Nf3+/=
    t = re.sub(r"\+{2,}$", "+", t)                     # old double check ++
    m = re.match(r"^[0Oo]-[0Oo](-[0Oo])?(?=[+#]?$)", t)
    if m:
        t = ("O-O-O" if m.group(1) else "O-O") + t[m.end():]
    return ("move", t) if t else None


def clean_pgn(text, max_games=PGN_MAX_GAMES):
    """Any site's PGN -> [{"headers": [(k, v)], "moves": [san tokens], "result": r or None}] (comments etc. dropped)."""
    text = _PGN_INVISIBLE.sub("", (text or "").replace("\r\n", "\n").replace("\r", "\n"))
    text = _PGN_SPACES.sub(" ", text)
    games, cur = [], None
    depth, in_moves, pos, n = 0, False, 0, len(text)

    def flush():
        nonlocal cur, depth, in_moves
        if cur and (cur["headers"] or cur["moves"]):
            games.append(cur)
        cur, depth, in_moves = None, 0, False

    line_start = True
    while pos < n and len(games) < max_games:
        if line_start:
            line_start = False
            eol = text.find("\n", pos)
            eol = n if eol < 0 else eol
            line = text[pos:eol].strip()
            if line.startswith("%"):
                pos = eol
                continue
            if line.startswith("["):
                pairs = _pgn_header_line(line.translate(_PGN_SMART_QUOTES))
                if pairs:
                    if in_moves:
                        flush()
                    if cur is None:
                        cur = {"headers": [], "moves": [], "result": None}
                    for k, v in pairs:
                        cur["headers"] = [(a, b) for a, b in cur["headers"] if a != k] + [(k, v)]
                    pos = eol
                    continue
        if text[pos] == "\n":
            pos += 1
            line_start = True
            continue
        m = _PGN_SCAN.match(text, pos)
        s = m.group(0)
        pos = m.end()
        c = s[0]
        if c in "{;} ":
            continue
        if c == "(":
            depth += 1
            continue
        if c == ")":
            depth = max(0, depth - 1)
            continue
        if depth:
            continue
        r = _pgn_token(s)
        if r is None:
            continue
        if cur is None:
            cur = {"headers": [], "moves": [], "result": None}
        in_moves = True
        if r[0] == "result":
            cur["result"] = r[1]
        else:
            cur["moves"].append(r[1])
    flush()
    for g in games:      # a result only in the moves fills a missing / unknown Result header
        hd = dict(g["headers"])
        if g["result"] in ("1-0", "0-1", "1/2-1/2") and hd.get("Result") not in ("1-0", "0-1", "1/2-1/2"):
            g["headers"] = [(a, b) for a, b in g["headers"] if a != "Result"] + [("Result", g["result"])]
    return games[:max_games]


def _pgn_escape(v):
    return v.replace("\\", "\\\\").replace('"', '\\"')


def clean_pgn_text(text, max_games=PGN_MAX_GAMES):
    """The same games as plain PGN text (headers + moves + result) - what python-chess reads and what is stored / sent."""
    out = []
    for g in clean_pgn(text, max_games):
        head = "".join(f'[{k} "{_pgn_escape(v)}"]\n' for k, v in g["headers"])
        res = g["result"] or dict(g["headers"]).get("Result") or "*"
        if res not in _PGN_RESULTS:
            res = "*"
        out.append(head + "\n" + " ".join(g["moves"] + [res]) + "\n")
    return "\n".join(out)


def export_clean(g):
    """One parsed game as plain PGN: headers + numbered main line, no comments / variations / NAGs."""
    return g.accept(chess.pgn.StringExporter(headers=True, variations=False, comments=False)).strip() + "\n"


def read_games(pgn_text):
    games = []
    stream = io.StringIO(clean_pgn_text(pgn_text))
    while len(games) < 200:
        g = chess.pgn.read_game(stream)
        if g is None:
            break
        games.append(g)
    return games


def summary(g):
    h = g.headers
    return {"white": person(h.get("White", "")), "black": person(h.get("Black", "")),
            "event": clean(h.get("Event", ""), 80), "round": h.get("Round", ""),
            "date": h.get("Date", ""), "result": h.get("Result", "*")}


def game_problem(g):
    """Why one game cannot be sent ("" = it can) - the quick checks only, no database (KT 340: also the paste box)."""
    h = g.headers
    variant = h.get("Variant", "Standard")
    if variant.lower() not in ("standard", "chess", "from position", ""):
        return f"This is a '{variant}' game. Only normal chess games can be sent."
    if h.get("SetUp") == "1" or "FEN" in h:
        return "This game starts from a set-up position (FEN header). Only games from the normal start can be sent."
    if g.errors:
        return f"The PGN has a problem near: {g.errors[0]}. Fix the file (or download it again) and choose it once more."
    return ""


def analyse(g):
    """Everything the form needs from one game, or {'error': ...}."""
    h = g.headers
    problem = game_problem(g)
    if problem:
        return {"error": problem}
    board = g.board()
    uci, sans = [], []
    epd_hits = (None, None)
    for mv in g.mainline_moves():
        if not board.is_legal(mv):
            return {"error": f"Move {len(uci) + 1} ({mv.uci()}) is not legal. Fix the file and choose it again."}
        sans.append(board.san(mv))
        board.push(mv)
        uci.append(mv.uci())
        hit = openings().get(board.epd())
        if hit:
            epd_hits = hit
    if not uci:
        return {"error": "The game has no moves."}
    if len(uci) > MAX_PLIES:
        return {"error": f"The game has {len(uci)} half-moves; the app accepts at most {MAX_PLIES}."}

    white, black = person(h.get("White", "")), person(h.get("Black", ""))
    date = h.get("Date", "") or h.get("UTCDate", "")
    year = date[:4] if re.match(r"^\d{4}", date) else ""
    result = h.get("Result", "*")
    if result not in ("1-0", "0-1", "1/2-1/2"):
        result = "*"
        if board.is_checkmate():
            result = "0-1" if board.turn == chess.WHITE else "1-0"
    eco = h.get("ECO", "").strip().upper()
    if not re.match(r"^[A-E]\d\d$", eco):
        eco = epd_hits[0] or ""
    opening = clean(h.get("Opening", ""), 80)
    if not opening and h.get("ECOUrl"):
        tail = h["ECOUrl"].rstrip("/").split("/")[-1]
        opening = clean(urllib.parse.unquote(tail).replace("-", " "), 80)
    if not opening and epd_hits[1]:
        opening = clean(epd_hits[1], 80)
    event = clean(h.get("Event", ""), 80)
    if event in ("?", "-"):
        event = ""
    rnd = ""
    m = re.match(r"^(\d{1,3})(?:\.\d+)?$", h.get("Round", "").strip())
    if m and h.get("Round", "").strip() not in ("-", "?"):
        rnd = str(int(m.group(1)))
    key = game_key(uci)
    return {
        "white": white or "White", "black": black or "Black", "event": event, "year": year, "result": result,
        "eco": eco, "opening": opening, "round": rnd, "uci": uci, "sans": sans, "plies": len(uci), "key": key,
        "final_svg": chess.svg.board(board, size=280, lastmove=board.peek(), coordinates=False),
        "tag_suggest": suggest_tag(event, year, key),
        "title_suggest": suggest_title(key, white, black, rnd),
    }


def suggest_title(key, white, black, rnd):
    with db() as con:
        r = con.execute("SELECT title FROM games WHERE key=?", (key,)).fetchone()
    if r and r["title"]:
        return r["title"]
    if rnd:
        return f"Game {rnd}"
    def sur(n):
        return n.split()[-1] if n else ""
    if white or black:
        return clean(f"{sur(white) or 'White'} vs {sur(black) or 'Black'}", MAX_TITLE)
    return "Game of the Day"


GENERIC_EVENT = re.compile(r"^(rated|casual|unrated|live|online|let'?s play|friendly|training|\?)\b.*|.*\b(blitz|bullet|rapid|correspondence|daily) (game|chess)$", re.I)


def suggest_tag(event, year, key=""):
    """Tags this game had before; else an existing tag matching the event; else a new 'event-year' id.
    Online games ("Rated Blitz game", "Live Chess", ...) get no automatic tag."""
    with db() as con:
        known = con.execute("SELECT tags FROM games WHERE key=?", (key,)).fetchone() if key else None
        rows = con.execute("SELECT id,name,aliases FROM tags").fetchall()
    if known and known["tags"]:
        first = known["tags"].split(",")[0]
        name = next((r["name"] for r in rows if r["id"] == first), first)
        return {"id": ",".join(known["tags"].split(",")), "name": name, "known": True}
    if not event or GENERIC_EVENT.match(event.strip()):
        return {"id": "", "name": "", "known": False}
    ev = event.lower()
    for r in rows:
        names = [r["name"].lower(), r["id"].replace("-", " ")] + [a.strip().lower() for a in (r["aliases"] or "").split(",") if a.strip()]
        if any(n and (n == ev or n in ev) for n in names) and (not year or year in r["id"] or year in ev):
            return {"id": r["id"], "name": r["name"], "known": True}
    base = slug(re.sub(r"\b(19|20)\d\d\b", "", event))
    tid = f"{base}-{year}" if year and base else (base or "")
    return {"id": tid[:40].strip("-"), "name": clean(f"{event}", 40), "known": False}


def match_tags(raw):
    """User-typed tag -> existing tag (by id, name or alias) or a clean new id."""
    out = []
    with db() as con:
        rows = con.execute("SELECT id,name,aliases FROM tags").fetchall()
    for t in [x.strip() for x in raw.split(",") if x.strip()]:
        low = t.lower()
        hit = None
        for r in rows:
            names = [r["id"], r["name"].lower()] + [a.strip().lower() for a in (r["aliases"] or "").split(",") if a.strip()]
            if low in names or slug(low) == r["id"]:
                hit = {"id": r["id"], "name": r["name"], "known": True}
                break
        if not hit:
            sid = slug(low)
            if not sid:
                continue
            close = [r for r in rows if same_tag(r["id"], sid)]
            hit = {"id": sid, "name": clean(t, 40), "known": False,
                   "did_you_mean": ({"id": close[0]["id"], "name": close[0]["name"]} if close else None)}
        if all(o["id"] != hit["id"] for o in out):
            out.append(hit)
    return out


def same_tag(a, b):
    """'wcc26' ~ 'wcc-2026' ~ 'wcc2026': same letters, same year (2-digit years read as 20xx)."""
    def parts(s):
        letters = re.sub(r"[^a-z]", "", s)
        nums = re.findall(r"\d+", s)
        year = nums[-1] if nums else ""
        if len(year) == 2:
            year = "20" + year
        return letters, year
    return a != b and parts(a) == parts(b)


def key_move(raw, sans, uci):
    """'16.Qb8+', '16 Qb8+', '16...Nxb8', 'Qb8+' -> ply index, or a message."""
    s = (raw or "").strip()
    if not s:
        return {"ply": -1}
    m = re.match(r"^(\d+)\s*(\.\.\.|…|\.)?\s*(.+)$", s)
    def norm(x):
        return x.replace("+", "").replace("#", "").replace("!", "").replace("?", "").replace("0-0-0", "O-O-O").replace("0-0", "O-O").strip()
    if m:
        num = int(m.group(1))
        black = m.group(2) in ("...", "…")
        want = norm(m.group(3))
        ply = (num - 1) * 2 + (1 if black else 0)
        if 0 <= ply < len(sans) and norm(sans[ply]) == want:
            return {"ply": ply, "uci": uci[ply], "label": label(ply, sans)}
        if not black and 0 <= ply + 1 < len(sans) and norm(sans[ply + 1]) == want:
            return {"ply": ply + 1, "uci": uci[ply + 1], "label": label(ply + 1, sans)}
        actual = []
        for p in (ply, ply + 1):
            if 0 <= p < len(sans):
                actual.append(label(p, sans))
        return {"error": f"'{s}' is not in this game." + (f" Move {num} is {' / '.join(actual)}." if actual else "")}
    want = norm(s)
    hits = [i for i, x in enumerate(sans) if norm(x) == want]
    if len(hits) == 1:
        return {"ply": hits[0], "uci": uci[hits[0]], "label": label(hits[0], sans)}
    if not hits:
        return {"error": f"'{s}' is not in this game."}
    return {"error": f"'{s}' was played {len(hits)} times: " + ", ".join(label(i, sans) for i in hits) + ". Type it with the move number."}


def label(ply, sans):
    n = ply // 2 + 1
    return f"{n}.{sans[ply]}" if ply % 2 == 0 else f"{n}...{sans[ply]}"


# ──────────────────────────────────────────────────────────── items + sizes

def item_for(a, form, key, sid, kind, kp, rev=0, puzzle=None):
    tags = [t["id"] for t in form["tags"] if TAG_RE.match(t["id"])][:6]
    it = {"key": key, "sid": sid, "kind": kind, "title": clean(form["title"], MAX_TITLE),
          "w": clean(a["white"], 60), "b": clean(a["black"], 60), "event": clean(a["event"], 80),
          "year": a["year"], "res": a["result"], "eco": a["eco"], "opening": clean(a["opening"], 80),
          "tags": ",".join(tags), "story": clean(form["story"], 400), "moves": " ".join(a["uci"])}
    names = [f"{t['id']}={clean(t['name'], 40)}" for t in form["tags"] if t["id"] in tags and t.get("name")]
    if names:
        it["tagn"] = "|".join(names)
    rnd = clean(form.get("round", ""), 3)
    if re.match(r"^\d{1,3}$", rnd):
        it["gn"] = rnd
    if kp.get("ply", -1) >= 0:
        it["km"] = str(kp["ply"])
        it["kmu"] = kp["uci"]
    if rev:
        it["rev"] = str(rev)
    if puzzle:                       # v2: Puzzle of the Day {"pl": int, "q": str}
        it["t"] = "puzzle"
        it["pl"] = str(puzzle["pl"])
        if puzzle.get("q"):
            it["q"] = clean(puzzle["q"], 40)
    return {k: v for k, v in it.items() if v != ""}


def push_data(items):
    """Push data; "sig" = ES256 over the UTF-8 bytes of "items" with the current feed key (KT 299, audit G7)."""
    text = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    sig = signing_key().sign(text.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    return {"type": "gotd", "v": "1", "items": text, "sig": base64.b64encode(sig).decode()}


def push_bytes(data):
    return sum(len(k.encode()) + len(v.encode()) for k, v in data.items())


def build_push(item, test):
    """Today's item + up to 3 earlier games (newest first) that still fit."""
    data, size, extras, _ = build_push_many([item], test)
    return data, size, extras


def build_push_many(today, test):
    """v2: today's items (send order) + up to 3 earlier ones that still fit. Earlier ones are dropped first; if
    today's list alone is too big, its last items are left to the mailbox file. Returns (data, bytes, extras, today_sent)."""
    extras = []
    keys = {it["key"] for it in today}
    if not test:
        with db() as con:
            rows = con.execute(
                "SELECT item, key FROM sends WHERE test=0 AND kind!='bundled' AND push_status IN ('sent','') "
                "ORDER BY sid DESC LIMIT 30").fetchall()
        seen = set(keys)
        for r in rows:
            it = json.loads(r["item"])
            if it["key"] in seen:
                continue
            seen.add(it["key"])
            extras.append(it)
            if len(extras) >= PUSH_EXTRA:
                break
    first = list(today)
    while True:
        items = first + extras
        data = push_data(items)
        size = push_bytes(data)
        if size <= PUSH_LIMIT or len(items) == 1:
            return data, size, len(extras), len(first)
        if extras:
            extras = extras[:-1]    # drop the oldest extra game first
        else:
            first = first[:-1]      # then the last of today's list (it still reaches phones through the mailbox)


def too_long(item):
    """KT 299, audit G10: None when today's game alone fits in a push; else {'over': bytes, 'cut': chars or 0}.
    cut = about how many story characters to remove (0 = even no story would fit: mailbox only)."""
    size = push_bytes(push_data([item]))
    if size <= PUSH_LIMIT:
        return None
    over = size - PUSH_LIMIT
    story = item.get("story", "")
    if len(story.encode("utf-8")) <= over:
        return {"over": over, "cut": 0}
    cut, acc = 0, 0
    for ch in reversed(story):
        acc += len(ch.encode("utf-8"))
        cut += 1
        if acc >= over:
            break
    return {"over": over, "cut": cut}


SID_ROW_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(\d{2})$")


def next_sid(con, day=None):
    """Next free number of the day. Only real sends count (test rows '<sid>-t001' and fix rows '<sid>-r001' don't)."""
    day = day or dt.date.today().isoformat()
    rows = con.execute("SELECT sid FROM sends WHERE sid LIKE ?", (day + "-%",)).fetchall()
    used = {int(m.group(1)) for m in (SID_ROW_RE.match(r["sid"]) for r in rows) if m}
    # v6 (KT 331): numbers of deleted sends are never given again (phones may still hold the deleted item).
    used |= {int(m.group(1)) for m in (SID_ROW_RE.match(x) for x in _META.get("deleted_sids", []) if x.startswith(day + "-")) if m}
    # v5 (KT 318): while GitHub's shared history cannot be read, this PC numbers from 51 - the Admin app (GitHub
    # Action) keeps counting from 1, so the two can never give the same number to different items.
    n = 51 if _merge_state.get("offline") else 1
    while n in used:
        n += 1
    if n > 99:
        raise RuntimeError("99 sends today already")
    return f"{day}-{n:02d}"


def clock_problem():
    """Compares this PC's clock with Google's (KT 299, audit G6). A wrong date would give a send id in the future,
    which then stays on top on every phone. Returns a plain message, or "" when fine / Google cannot be reached."""
    try:
        req = urllib.request.Request("https://www.google.com/generate_204", method="HEAD")
        with urllib.request.urlopen(req, timeout=8) as r:
            date = r.headers.get("Date")
    except urllib.error.HTTPError as e:
        date = e.headers.get("Date") if e.headers else None
    except Exception:
        return ""
    if not date:
        return ""
    try:
        server = email.utils.parsedate_to_datetime(date).timestamp()
    except Exception:
        return ""
    off = time.time() - server
    if abs(off) <= CLOCK_TOLERANCE_S:
        return ""
    mins = abs(off) / 60
    how = f"{mins / 1440:.1f} days" if mins >= 1440 else (f"{mins / 60:.1f} hours" if mins >= 60 else f"{mins:.0f} minutes")
    return (f"This PC's clock is {how} {'ahead' if off > 0 else 'behind'}. A wrong date would put this game on the wrong "
            "day on every phone. Fix it: Windows Settings > Time & language > Date & time > 'Sync now' "
            "(Mac: System Settings > General > Date & Time > set automatically). Then press Send again.")


# ──────────────────────────────────────────────────────────── FCM (HTTP v1)

def http_json(url, body=None, headers=None, method=None, timeout=25):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None and "Content-Type" not in (headers or {}):
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300].decode("utf-8", "replace")}


def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def firebase_creds():
    with open(FIREBASE_KEY, encoding="utf-8") as f:
        k = json.load(f)
    for need in ("client_email", "private_key", "project_id"):
        if not k.get(need):
            raise RuntimeError(f"firebase_key.json has no '{need}' - download the key again (Project settings > Service accounts).")
    return k


def access_token():
    if _token_cache["token"] and time.time() < _token_cache["exp"] - 120:
        return _token_cache["token"]
    k = firebase_creds()
    now = int(time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = b64url(json.dumps({
        "iss": k["client_email"], "scope": "https://www.googleapis.com/auth/firebase.messaging",
        "aud": k.get("token_uri", "https://oauth2.googleapis.com/token"), "iat": now, "exp": now + 3600}).encode())
    try:
        pk = serialization.load_pem_private_key(k["private_key"].encode(), password=None)
    except (ValueError, TypeError):
        raise RuntimeError("The Firebase key file is damaged (its private key cannot be read). Download a new key: "
                           "Firebase console > Project settings > Service accounts > Generate new private key, then Setup step 1.")
    sig = pk.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    jwt = f"{header}.{claims}.{b64url(sig)}"
    body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt}).encode()
    code, resp = http_json(k.get("token_uri", "https://oauth2.googleapis.com/token"), body,
                           {"Content-Type": "application/x-www-form-urlencoded"})
    if code != 200 or "access_token" not in resp:
        raise RuntimeError(fcm_reason(code, resp, signin=True))
    _token_cache.update(token=resp["access_token"], exp=time.time() + int(resp.get("expires_in", 3600)))
    return _token_cache["token"]


def send_push(data, test):
    k = firebase_creds()
    url = f"https://fcm.googleapis.com/v1/projects/{k['project_id']}/messages:send"
    msg = {"message": {"topic": TEST_TOPIC if test else TOPIC, "data": data,
                       "android": {"priority": "high", "ttl": "2419200s"}}}
    last = None
    for attempt in range(1, 4):
        try:
            code, resp = http_json(url, msg, {"Authorization": "Bearer " + access_token()})
        except RuntimeError as e:                   # sign-in refused: a plain message already
            return False, str(e)
        except Exception as e:                      # network / DNS / TLS
            code, resp = 0, {"error": str(e)}
        if code == 200:
            return True, resp.get("name", "sent")
        last = fcm_reason(code, resp)
        if code == 401:
            _token_cache["token"] = None
        if code in (0, 401, 429, 500, 502, 503, 504):
            time.sleep(2 * attempt * attempt)
            continue
        break
    return False, last


def _short(resp):
    return json.dumps(resp, ensure_ascii=False)[:200]


def fcm_reason(code, resp, signin=False):
    """Plain-words reason for a Firebase failure (13 Nov 2026, owner-approved point 5); the raw reply follows."""
    text = _short(resp)
    if code == 0:
        why = "No internet on this PC (or Google cannot be reached). Check the connection, then press 'Finish it'."
    elif signin or code == 401:
        why = ("Firebase refused the key (it was deleted, disabled or belongs to another project). Make a new one: "
               "Firebase console > Project settings > Service accounts > Generate new private key, then Setup step 1.")
    elif code == 403:
        why = ("Firebase said 'not allowed'. Turn on 'Firebase Cloud Messaging API (V1)' for this project "
               "(Google Cloud console > APIs & Services), and check the key belongs to this app's project.")
    elif code == 404:
        why = "Firebase project not found. The key is for a different or deleted project - Setup step 1 with the right key."
    elif code == 400:
        why = "Firebase rejected the message format. Send the record file (data/send_record.txt) to Claude."
    elif code == 429:
        why = "Firebase says too many messages right now. Wait a few minutes, then press 'Finish it'."
    elif code >= 500:
        why = "Firebase is having a problem right now. Wait a few minutes, then press 'Finish it'."
    else:
        why = "Firebase did not accept the message."
    return f"{why} [HTTP {code}: {text}]"


def github_reason(code, resp, cfg):
    """Plain-words reason for a GitHub failure (owner-approved point 5); the raw reply follows."""
    repo = f"{cfg.get('github_owner', '')}/{cfg.get('github_repo', '')}"
    text = _short(resp)
    if code == 0:
        why = "No internet on this PC (or GitHub cannot be reached). Check the connection, then press 'Finish it'."
    elif code == 401 and REMOTE.token:
        # KT 395: this PC's device access token (the one SEND_GAME_OF_THE_DAY checks at every start) - the old advice
        # ("Contents: Read and write, then Setup step 2") was wrong for it.
        why = ("GitHub does not accept this PC's device access token any more (it was deleted, regenerated or has "
               "expired on GitHub). Close this window and double-click SEND_GAME_OF_THE_DAY - it asks for your token, "
               "checks it and goes on. Then send again.")
    elif code == 401:
        why = ("The GitHub token has expired or is wrong. Make a new one: GitHub > Settings > Developer settings > "
               f"Fine-grained tokens (only {repo}, Contents: Read and write), then Setup step 2.")
    elif code == 403 and "dispatch" in text:     # KT 374: starting the Action was refused - an Actions permission
        why = (f"The GitHub token may not start the Action on {repo}. Edit the token (GitHub > Settings > Developer "
               f"settings > Fine-grained tokens): Repository access = {repo}, Permissions > Actions = Read and write, "
               "Contents = Read-only. (Or GitHub limits were hit - wait a few minutes.)")
    elif code == 403:
        why = (f"The GitHub token may not write to {repo}. Edit the token: Repository access = {repo}, "
               "Permissions > Contents = Read and write. (Or GitHub limits were hit - wait a few minutes.)")
    elif code == 404:
        why = (f"GitHub cannot find {repo} for this token. Check the user name and repository in Setup, and that the "
               "token includes this repository.")
    elif code == 422:
        why = f"GitHub refused the upload - usually the branch name. Check 'Branch' in Setup (most repositories use main)."
    elif code == 409:
        why = "The file changed on GitHub at the same moment. Press 'Finish it' to try again."
    elif code >= 500 or code == 429:
        why = "GitHub is having a problem right now. Phones still got the push. Press 'Finish it' later."
    else:
        why = "GitHub did not accept the upload."
    return f"{why} [HTTP {code}: {text}]"


# ──────────────────────────────────────────────────────────── feed (signed) + GitHub Pages

def _pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def _load_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _write_key(path, key):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(_pem(key))
    os.replace(tmp, path)


def write_key_backup():
    """One file with BOTH keys (current first, then spare). Owner copies it once to Google Drive / a pendrive."""
    if not (os.path.exists(SIGNING_KEY) and os.path.exists(SPARE_KEY)):
        return ""
    body = (b"# Game of the Day feed keys - PRIVATE. Keep a copy OFF this PC (Google Drive / pendrive), never share it.\n"
            b"# A new PC: put this file in Downloads (or 42_game-of-the-day/data) and start the tool - it restores itself.\n"
            + _pem(_load_key(SIGNING_KEY)) + _pem(_load_key(SPARE_KEY)))
    old = open(KEY_BACKUP, "rb").read() if os.path.exists(KEY_BACKUP) else None
    if old != body:
        os.makedirs(KEY_BACKUP_DIR, exist_ok=True)
        with open(KEY_BACKUP, "wb") as f:
            f.write(body)
        record(f"wrote the key backup file {KEY_BACKUP} - copy it once to Google Drive or a pendrive")
    return KEY_BACKUP


def restore_keys_if_missing():
    """Start of every run: no key on this PC but a backup file around -> restore both keys (a new / repaired PC)."""
    if os.path.exists(SIGNING_KEY):
        return False
    cands = [KEY_BACKUP, os.path.join(DATA, KEY_BACKUP_NAME), os.path.join(downloads_dir(), KEY_BACKUP_NAME)]
    for c in cands:
        if not os.path.isfile(c):
            continue
        try:
            blocks = re.findall(rb"-----BEGIN PRIVATE KEY-----.+?-----END PRIVATE KEY-----\s*", open(c, "rb").read(), re.S)
            keys = [serialization.load_pem_private_key(b, password=None) for b in blocks[:2]]
        except Exception as e:
            record(f"key backup {c} could not be read: {e}")
            continue
        if not keys:
            continue
        _write_key(SIGNING_KEY, keys[0])
        if len(keys) > 1:
            _write_key(SPARE_KEY, keys[1])
        record(f"restored the feed keys from {c}")
        print(f"  Restored the feed keys from {c}", flush=True)
        return True
    if os.path.exists(SPARE_KEY):
        # The current key is gone but the spare is here: the app already trusts it - use it from now on.
        os.replace(SPARE_KEY, SIGNING_KEY)
        record("the current feed key was missing - the spare key (already trusted by the app) is now the current one")
        return True
    return False


def signing_key(create=True):
    if os.path.exists(SIGNING_KEY):
        key = _load_key(SIGNING_KEY)
    elif not create:
        return None
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        _write_key(SIGNING_KEY, key)
        record("created the feed signing key (data/feed_signing_key.pem) - keep it private, never share it")
    if create and not os.path.exists(SPARE_KEY):
        _write_key(SPARE_KEY, ec.generate_private_key(ec.SECP256R1()))
        record("created the spare feed key (data/feed_signing_key_spare.pem) - the app trusts it too")
    if create:
        write_key_backup()
    return key


def _pub_b64(key):
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(der).decode()


def public_key_b64():
    """Current key first, then the spare - written into the app as one comma-separated line (KT 299, audit G5)."""
    k = signing_key(create=True)
    keys = [_pub_b64(k)]
    if os.path.exists(SPARE_KEY):
        keys.append(_pub_b64(_load_key(SPARE_KEY)))
    return ",".join(keys)


def latest_items(limit=FEED_GAMES):
    """Latest send of each game / puzzle (not tests, not still queued), newest first - bundled items included."""
    with db() as con:
        rows = con.execute("SELECT item, key, sid FROM sends WHERE test=0 AND push_status!='queued' ORDER BY sid DESC").fetchall()
    out, seen = [], set()
    for r in rows:
        if r["key"] in seen:
            continue
        seen.add(r["key"])
        out.append(json.loads(r["item"]))
        if len(out) >= limit:
            break
    return out


def build_feed():
    payload = json.dumps({"v": 1, "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "games": latest_items()}, ensure_ascii=False, separators=(",", ":"))
    sig = signing_key().sign(payload.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    env = {"v": 1, "alg": "ES256", "payload": payload, "sig": base64.b64encode(sig).decode()}
    return json.dumps(env, ensure_ascii=False, separators=(",", ":"))


def feed_url(cfg):
    owner, repo = cfg.get("github_owner", ""), cfg.get("github_repo", "")
    if not owner or not repo:
        return ""
    if repo.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner}.github.io/{FEED_PATH}"
    return f"https://{owner}.github.io/{repo}/{FEED_PATH}"


def mirror_url(cfg):
    """Second address for the same file (13 Nov 2026, owner-approved): jsDelivr's free CDN copy of the GitHub repo.
    Used by the app only when GitHub Pages fails. jsDelivr caches it; purge_mirror() refreshes it after each send."""
    owner, repo = cfg.get("github_owner", ""), cfg.get("github_repo", "")
    if not owner or not repo:
        return ""
    return f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{cfg.get('github_branch') or 'main'}/{FEED_PATH}"


def feed_urls(cfg):
    return [u for u in (feed_url(cfg), mirror_url(cfg)) if u]


def purge_mirror(cfg):
    """Ask jsDelivr to drop its cached copy (best effort; a stale mirror only matters while GitHub Pages is down)."""
    url = mirror_url(cfg)
    if not url:
        return
    purge = url.replace("https://cdn.jsdelivr.net/", "https://purge.jsdelivr.net/", 1)
    try:
        code, resp = http_json(purge, timeout=15)
        record(f"  mirror refresh: HTTP {code}")
    except Exception as e:
        record(f"  mirror refresh skipped: {e}")


def github_token(cfg):
    """The token to use: one typed in Setup, else the GitHub CLI sign-in (never saved by this tool)."""
    if REMOTE.token:                    # v6 (KT 331): a paired PC uses its access token (kept by Windows, never in config)
        return REMOTE.token
    if cfg.get("github_token"):
        return cfg["github_token"]
    if cfg.get("github_auth") == "gh":
        exe = gh_cli()
        if exe:
            r = run([exe, "auth", "token", "--hostname", "github.com"])
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
    return ""


def gh(cfg, method, path, body=None):
    url = f"https://api.github.com/repos/{cfg['github_owner']}/{cfg['github_repo']}{path}"
    return http_json(url, body, {"Authorization": "Bearer " + github_token(cfg), "Accept": "application/vnd.github+json",
                                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "gotd-sender"}, method=method)


def github_ready(cfg):
    return bool(cfg.get("github_owner") and cfg.get("github_repo") and
                (cfg.get("github_token") or cfg.get("github_auth") == "gh" or REMOTE.token))


def publish_feed(text):
    cfg = load_config()
    if not github_ready(cfg):
        return False, "GitHub is not set up (Setup tab > 'Set up GitHub for me')."
    if not github_token(cfg):
        return False, "GitHub sign-in is missing or expired. Setup tab > 'Set up GitHub for me' (one Authorize click)."
    branch = cfg.get("github_branch") or "main"
    last = None
    for attempt in range(1, 4):
        try:
            code, cur = gh(cfg, "GET", f"/contents/{FEED_PATH}?ref={urllib.parse.quote(branch)}")
            sha = cur.get("sha") if code == 200 else None
            body = {"message": f"Game of the Day feed {now_iso()}", "branch": branch,
                    "content": base64.b64encode(text.encode("utf-8")).decode()}
            if sha:
                body["sha"] = sha
            code, resp = gh(cfg, "PUT", f"/contents/{FEED_PATH}", body)
        except Exception as e:
            code, resp = 0, {"error": str(e)}
        if code in (200, 201):
            purge_mirror(cfg)
            return True, (resp.get("commit") or {}).get("sha", "ok")[:12]
        last = github_reason(code, resp, cfg)
        if code in (0, 409, 429, 500, 502, 503, 504):     # 409 = file changed meanwhile: read the sha again
            time.sleep(2 * attempt * attempt)
            continue
        break
    return False, last



# ──────────────────────────────────────────────────────────── v4: the complete history on GitHub
# 14 Nov 2026 (KT section 306, owner: "I want every item to be stored in git ... we will use that as our server - db").
# Besides the mailbox (latest.json, the newest 40 - the only file phones read), the repo keeps EVERY item ever sent,
# one file per year: gotd/history/2026.json = {"v":1,"year":"2026","generated":..., "count":N, "sends":[{row, kind,
# sent_at, push, feed, item}, ...]} in send order - games, puzzles from games, position puzzles, "again" / "fix"
# sends and the items built into the app; never test sends, never items still waiting in today's list.
# The PC database stays the working copy; GitHub is the shared / backup copy. Rewritten after every real send (only
# the years that changed); an upload that fails is remembered (config "history_pending") and retried on the next
# send / start - it never blocks a send. "Upload the full history now" (Setup tab) rewrites every year.

HISTORY_DIR = "gotd/history"


def history_years():
    with db() as con:
        return sorted({r[0][:4] for r in con.execute(
            "SELECT sid FROM sends WHERE test=0 AND push_status!='queued'").fetchall() if re.match(r"^\d{4}", r[0])})


def history_doc(year):
    with db() as con:
        rows = con.execute(
            "SELECT sid, kind, sent_at, item, push_status, feed_status FROM sends WHERE test=0 AND push_status!='queued' "
            "AND sid LIKE ? ORDER BY sid", (f"{year}-%",)).fetchall()
    sends = [{"row": r["sid"], "kind": r["kind"], "sent_at": r["sent_at"], "push": r["push_status"],
              "feed": r["feed_status"], "item": json.loads(r["item"])} for r in rows]
    return json.dumps({"v": 1, "year": year, "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "count": len(sends), "sends": sends}, ensure_ascii=False, indent=1)


def publish_file(path, text, message, branch=None):
    """Create / replace one file in the repo (same retries as the mailbox). Works for files over 1 MB too.
    v5: [branch] = another branch (the Admin app's gotd-outbox)."""
    cfg = load_config()
    if not github_ready(cfg):
        return False, "GitHub is not set up (Setup tab > 'Set up GitHub for me')."
    if not github_token(cfg):
        return False, "GitHub sign-in is missing or expired. Setup tab > 'Set up GitHub for me' (one Authorize click)."
    branch = branch or cfg.get("github_branch") or "main"
    last = None
    for attempt in range(1, 4):
        try:
            url = f"https://api.github.com/repos/{cfg['github_owner']}/{cfg['github_repo']}/contents/{path}?ref={urllib.parse.quote(branch)}"
            # "object" = the file's details (sha) without its content: allowed for files up to 100 MB.
            code, cur = http_json(url, None, {"Authorization": "Bearer " + github_token(cfg), "Accept": "application/vnd.github.object",
                                              "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "gotd-sender"})
            sha = cur.get("sha") if code == 200 and isinstance(cur, dict) else None
            body = {"message": message, "branch": branch, "content": base64.b64encode(text.encode("utf-8")).decode()}
            if sha:
                body["sha"] = sha
            code, resp = gh(cfg, "PUT", f"/contents/{path}", body)
        except Exception as e:
            code, resp = 0, {"error": str(e)}
        if code in (200, 201):
            return True, (resp.get("commit") or {}).get("sha", "ok")[:12]
        last = github_reason(code, resp, cfg)
        if code in (0, 409, 429, 500, 502, 503, 504):
            time.sleep(2 * attempt * attempt)
            continue
        break
    return False, last


def publish_history(years):
    """Uploads gotd/history/<year>.json for [years] + any year still pending. -> (all ok, detail)."""
    cfg = load_config()
    todo = sorted(set(years) | set(cfg.get("history_pending", [])))
    if not todo:
        return True, "nothing to upload"
    done, failed, why = [], [], ""
    for y in todo:
        try:
            ok, detail = publish_file(f"{HISTORY_DIR}/{y}.json", history_doc(y), f"Game of the Day history {y} {now_iso()}")
        except Exception as e:
            ok, detail = False, str(e)
        (done if ok else failed).append(y)
        if not ok:
            why = detail
    cfg = load_config()
    cfg["history_pending"] = failed
    if not failed and not cfg.get("history_full_upload"):
        # every year was uploaded at least once after this one (first start of v4 uploads them all)
        cfg["history_full_upload"] = now_iso() if set(done) >= set(history_years()) else ""
    save_config(cfg)
    record(f"  history: uploaded {', '.join(done) or '-'}" + (f"; failed {', '.join(failed)} ({why}) - retried next send" if failed else ""))
    if failed:
        return False, f"history {', '.join(failed)} not uploaded ({why}) - it is retried on the next send"
    return True, f"history {', '.join(done)} saved on GitHub"


def history_after(items_or_sids):
    """The years touched by a real send."""
    ys = set()
    for x in items_or_sids:
        sid = x if isinstance(x, str) else x.get("sid", "")
        if re.match(r"^\d{4}", sid):
            ys.add(sid[:4])
    return ys


def history_catch_up():
    """At start: the first run of v4 uploads every year once; later runs only retry what failed. Never raises."""
    try:
        cfg = load_config()
        if not github_ready(cfg):
            return
        years = [] if cfg.get("history_full_upload") else history_years()
        if years or cfg.get("history_pending"):
            record("history: catching up on GitHub")
            with _lock, shared_send():      # never at the same moment as a send (v5: nor an Admin-app send)
                publish_history(years)
        else:
            with _lock:
                merge_remote_history()      # v5: the Admin app's sends show up in History / duplicate checks
    except Exception as e:
        record(f"history: catch-up skipped ({e})")


def api_history_upload(_):
    with _lock, shared_send():
        ok, detail = publish_history(history_years())
    return {"ok": ok, "messages": [detail]} if ok else {"error": detail}


# ──────────────────────────────────────────────────────────── v5: one history for the PC + the Admin app
# 15 Nov 2026 (KT section 318). GitHub's gotd/history/<year>.json is the shared record. merge_remote_history() adds
# the rows this database does not have (the phone's sends, made by the GitHub Action with this same file) - and the
# Action itself starts from an EMPTY database filled the same way. shared_send() = wait for a running Admin-app send,
# hold the PC lock, merge, then send: the two senders never pick the same day number or overwrite each other.

ADMIN_WORKFLOW = "gotd-admin.yml"
OUTBOX_BRANCH = "gotd-outbox"
PC_LOCK_PATH = "lock/pc.json"
_merge_state = {"at": 0.0}


def gh_raw(cfg, path, ref=None):
    """(code, text) of one repo file, or (code, None)."""
    ref = ref or cfg.get("github_branch") or "main"
    url = f"https://api.github.com/repos/{cfg['github_owner']}/{cfg['github_repo']}/contents/{path}?ref={urllib.parse.quote(ref)}"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + github_token(cfg), "Accept": "application/vnd.github.raw",
                                               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "gotd-sender"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def remote_history_rows(cfg):
    """Every row of gotd/history/*.json on GitHub, or None when GitHub cannot be read ([] = no history yet)."""
    code, listing = gh(cfg, "GET", f"/contents/{HISTORY_DIR}?ref={urllib.parse.quote(cfg.get('github_branch') or 'main')}")
    if code == 404:
        return []
    if code != 200 or not isinstance(listing, list):
        return None
    rows = []
    for f in sorted(listing, key=lambda x: x.get("name", "")):
        if not re.match(r"^\d{4}\.json$", f.get("name", "")):
            continue
        c, text = gh_raw(cfg, f["path"])
        if c != 200 or text is None:
            return None
        try:
            rows += json.loads(text).get("sends", [])
        except ValueError:
            return None
    return rows


def merge_history_rows(rows):
    """Adds rows (history-file format) this database lacks; a row sent elsewhere after failing here is updated.
    Returns the number of rows added / updated."""
    n = 0
    with db() as con:
        for r in rows:
            sid, it = r.get("row", ""), r.get("item") or {}
            if not sid or not it.get("key") or not re.match(r"^\d{4}-\d{2}-\d{2}-\d{2}(-r\d{3})?$", sid):
                continue
            have = con.execute("SELECT push_status, feed_status FROM sends WHERE sid=?", (sid,)).fetchone()
            push, feed = r.get("push", "") or "", r.get("feed", "") or ""
            if have:
                if have["push_status"] in ("failed", "sending") and push == "sent":
                    con.execute("UPDATE sends SET push_status=?, feed_status=? WHERE sid=?", (push, feed or have["feed_status"], sid))
                    n += 1
                continue
            con.execute("INSERT INTO sends(sid,key,kind,sent_at,test,item,push_status,push_detail,feed_status,feed_detail,push_bytes) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, it["key"], r.get("kind", "new"), r.get("sent_at", ""), 0, json.dumps(it, ensure_ascii=False),
                         push, "", feed, "", 0))
            km = it.get("km", "")
            con.execute(
                "INSERT OR IGNORE INTO games(key,title,white,black,event,year,result,eco,opening,round,tags,story,moves,plies,"
                "key_ply,key_uci,pgn_original,created_at,fen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (it["key"], it.get("title", ""), it.get("w", ""), it.get("b", ""), it.get("event", ""), it.get("year", ""),
                 it.get("res", "*"), it.get("eco", ""), it.get("opening", ""), it.get("gn", ""), it.get("tags", ""),
                 it.get("story", ""), it.get("moves", ""), len(it.get("moves", "").split()),
                 int(km) if str(km).isdigit() else -1, it.get("kmu", ""), "", r.get("sent_at", "") or now_iso(), it.get("fen", "")))
            for pair in (it.get("tagn", "") or "").split("|"):
                if "=" in pair:
                    tid, name = pair.split("=", 1)
                    if TAG_RE.match(tid):
                        con.execute("INSERT OR IGNORE INTO tags(id,name,aliases) VALUES(?,?,?)", (tid, name, name.lower()))
            n += 1
    return n


def merge_remote_history(force=True):
    """Reads GitHub's history and merges it. Best effort for the PC (False = GitHub not reachable / not set up)."""
    cfg = load_config()
    if not github_ready(cfg) or not github_token(cfg):
        return False
    if not force and time.time() - _merge_state["at"] < 10:      # several PGNs chosen in a row: one read
        return True
    rows = remote_history_rows(cfg)
    if rows is None:
        record("history: GitHub's copy could not be read - using this PC's history only")
        return False
    n = merge_history_rows(rows)
    _merge_state["at"] = time.time()
    if n:
        record(f"history: {n} row(s) from GitHub added (Admin app sends)")
    return True


def admin_enabled(cfg):
    return bool(cfg.get("admin_enabled"))


def wait_admin_runs(cfg, max_s=300):
    """Waits while an Admin-app send (the GitHub Action) is queued or running. -> plain note or ""."""
    t0, said = time.time(), False
    while time.time() - t0 < max_s:
        busy = 0
        for st in ("in_progress", "queued"):
            code, resp = gh(cfg, "GET", f"/actions/workflows/{ADMIN_WORKFLOW}/runs?status={st}&per_page=5")
            if code == 200 and isinstance(resp, dict):
                busy += int(resp.get("total_count", 0) or 0)
        if not busy:
            return ""
        if not said:
            record("  waiting: an Admin-app send is running on GitHub")
            print("  Waiting for the Admin app's send to finish on GitHub...", flush=True)
            said = True
        time.sleep(5)
    return "An Admin-app send was still running after 5 minutes - sent anyway."


def pc_lock(cfg, on):
    """The PC's short lock (lock/pc.json on gotd-outbox): the Action waits while it is fresh. Best effort."""
    try:
        if on:
            body = json.dumps({"by": "pc", "at": now_iso(), "until": int(time.time()) + 300})
            publish_file(PC_LOCK_PATH, body, "PC send lock", branch=OUTBOX_BRANCH)
        else:
            code, cur = gh(cfg, "GET", f"/contents/{PC_LOCK_PATH}?ref={OUTBOX_BRANCH}")
            if code == 200 and isinstance(cur, dict) and cur.get("sha"):
                gh(cfg, "DELETE", f"/contents/{PC_LOCK_PATH}",
                   {"message": "PC send done", "sha": cur["sha"], "branch": OUTBOX_BRANCH})
    except Exception as e:
        record(f"  PC lock {'set' if on else 'release'} skipped: {e}")


class shared_send:
    """with _lock, shared_send(): ... - one sender at a time (PC / Admin app) and the merged history."""
    def __enter__(self):
        self.cfg = load_config()
        self.locked = False
        merged = False
        try:
            if github_ready(self.cfg) and github_token(self.cfg):
                if admin_enabled(self.cfg) and os.environ.get("GOTD_ACTION") != "1":
                    wait_admin_runs(self.cfg)
                    pc_lock(self.cfg, True)
                    self.locked = True
                merged = merge_remote_history()
        except Exception as e:
            record(f"  shared history check skipped: {e}")
        _merge_state["offline"] = admin_enabled(self.cfg) and not merged and os.environ.get("GOTD_ACTION") != "1"
        if _merge_state["offline"]:
            record("  GitHub's history could not be read: today's numbers start at 51 (no clash with the Admin app)")
        return self

    def __exit__(self, *exc):
        _merge_state["offline"] = False
        if self.locked:
            pc_lock(self.cfg, False)
        return False


# ──────────────────────────────────────────────────────────── readable copies

def write_copies(item, sent_at, push_s, feed_s):
    os.makedirs(PGN_DIR, exist_ok=True)
    rev = f"-r{int(item['rev']):03d}" if item.get("rev") else ""
    name = f"{item['sid']}{rev}_{slug(item['title']) or 'game'}.pgn"
    with open(os.path.join(PGN_DIR, name), "w", encoding="utf-8") as f:
        f.write(pgn_of(item))
    with db() as con:
        rows = con.execute("SELECT item FROM sends WHERE test=0 AND kind!='bundled' ORDER BY sid").fetchall()
    with open(os.path.join(PGN_DIR, "all_games.pgn"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(pgn_of(json.loads(r["item"])) + "\n")
    new = not os.path.exists(CSV_LOG)
    with open(CSV_LOG, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["send_id", "sent_at", "kind", "title", "white", "black", "event", "year", "result", "tags",
                        "game_key", "push", "feed"])
        w.writerow([item["sid"], sent_at, item["kind"] + (" (position)" if item.get("fen") else " (puzzle)" if item.get("t") == "puzzle" else ""), item["title"], item.get("w", ""), item.get("b", ""), item.get("event", ""),
                    item.get("year", ""), item.get("res", ""), item.get("tags", ""), item["key"], push_s, feed_s])


def pgn_of(it):
    b = chess.Board(it["fen"]) if it.get("fen") else chess.Board()   # v3: a puzzle from a position starts there
    sans = []
    for u in it["moves"].split():
        mv = chess.Move.from_uci(u)
        sans.append(b.san(mv))
        b.push(mv)
    body = []
    for i, s in enumerate(sans):
        body.append(f"{i // 2 + 1}. {s}" if i % 2 == 0 else s)
    head = [("Event", it.get("event", "?")), ("Date", (it.get("year") or "????") + ".??.??"), ("Round", it.get("gn", "-")),
            ("White", it.get("w", "?")), ("Black", it.get("b", "?")), ("Result", it.get("res", "*")), ("ECO", it.get("eco", "?")),
            ("Opening", it.get("opening", "?")), ("Title", it["title"]), ("SendId", it["sid"]), ("GameKey", it["key"])]
    if it.get("fen"):             # v3
        head += [("SetUp", "1"), ("FEN", it["fen"]), ("PuzzleOfTheDay", f"from a position · {it.get('q', '')}")]
    elif it.get("t") == "puzzle":   # v2
        head.append(("PuzzleOfTheDay", f"from half-move {int(it.get('km', '0')) + 1}, {it.get('pl', '1')} half-moves · {it.get('q', '')}"))
    hdr = "".join(f'[{k} "{(v or "?").replace(chr(34), chr(39))}"]\n' for k, v in head)
    return hdr + "\n" + " ".join(body) + " " + it.get("res", "*") + "\n"


# ──────────────────────────────────────────────────────────── API

class Session:
    analysed = {}       # token -> analysis of the chosen game


def api_status(_):
    cfg = load_config()
    try:
        project = firebase_creds().get("project_id", "") if os.path.exists(FIREBASE_KEY) else ""
    except Exception:
        project = ""
    return {
        "version": TOOL_VERSION, "data": DATA,
        "firebase": bool(project), "firebase_project": project,
        "github": github_ready(cfg),
        "github_owner": cfg.get("github_owner", ""), "github_repo": cfg.get("github_repo", ""),
        "github_branch": cfg.get("github_branch", "main"), "project_path": cfg.get("project_path", "") or guess_project(),
        "job": JOB.snapshot(),
        "signing_key": os.path.exists(SIGNING_KEY), "public_key": public_key_b64() if os.path.exists(SIGNING_KEY) else "",
        "key_backup": KEY_BACKUP if os.path.exists(KEY_BACKUP) else "",
        "feed_url": "  +  ".join(feed_urls(cfg)),
        "unfinished": unfinished(),
    }


def unfinished():
    with db() as con:
        rows = con.execute("SELECT sid,key,item,push_status,feed_status,test FROM sends WHERE kind!='bundled' AND "
                           "(push_status IN ('sending','failed') OR feed_status IN ('sending','failed')) ORDER BY sid DESC LIMIT 20").fetchall()
    return [{"sid": r["sid"], "title": json.loads(r["item"])["title"], "push": r["push_status"], "feed": r["feed_status"],
             "test": bool(r["test"])} for r in rows]


def api_parse(body):
    text = body.get("pgn", "")
    if len(text) > 5_000_000:
        return {"error": "That file is too big for one game."}
    games = read_games(text)
    if not games:
        return {"error": "No game found in that file. Choose a .pgn file."}
    idx = body.get("index")
    if len(games) > 1 and idx is None:
        return {"choose": [dict(summary(g), index=i) for i, g in enumerate(games)]}
    g = games[int(idx or 0)]
    try:
        with _lock:
            if REMOTE.active():                   # v6: a fresh copy of GitHub's history (deletes included)
                if time.time() - _merge_state["at"] > 10:
                    remote_refresh()
            else:
                merge_remote_history(force=False)     # v5: the Admin app's sends count as "sent before" too
    except Exception as e:
        record(f"  history merge skipped: {e}")
    a = analyse(g)
    if "error" in a:
        return a
    a["pgn_original"] = export_clean(g)      # KT 372: the chosen game only, plain (no comments / variations / NAGs)
    tok = secrets.token_hex(8)
    Session.analysed[tok] = a
    while len(Session.analysed) > 5:          # the last few chosen games only
        Session.analysed.pop(next(iter(Session.analysed)))
    with db() as con:
        prev = con.execute("SELECT sid, kind, sent_at FROM sends WHERE key=? ORDER BY sid DESC", (a["key"],)).fetchall()
        corr = []
        if a["round"]:
            corr = con.execute("SELECT key,title FROM games WHERE white=? AND black=? AND event=? AND round=? AND key!=?",
                               (a["white"], a["black"], a["event"], a["round"], a["key"])).fetchall()
        tags = [dict(r) for r in con.execute("SELECT id,name FROM tags ORDER BY name").fetchall()]
        # v8 (KT 401, as the Admin app): a game sent before comes back with the story it was last sent with
        last = con.execute("SELECT item FROM sends WHERE key=? AND test=0 AND push_status!='queued' ORDER BY sid DESC LIMIT 1",
                           (a["key"],)).fetchone()
    try:
        prev_story = json.loads(last["item"]).get("story", "") if last else ""
    except ValueError:
        prev_story = ""
    return {
        "token": tok, "white": a["white"], "black": a["black"], "event": a["event"], "year": a["year"],
        "result": a["result"], "eco": a["eco"], "opening": a["opening"], "round": a["round"], "plies": a["plies"],
        "moves": (a["plies"] + 1) // 2, "key": a["key"], "svg": a["final_svg"], "sans": a["sans"],
        "title": a["title_suggest"], "tag": a["tag_suggest"], "tags": tags,
        "sent_before": [dict(r) for r in prev], "correction_of": [dict(r) for r in corr], "story": prev_story,
    }


def api_keymove(body):
    a = Session.analysed.get(body.get("token", ""))
    if not a:
        return {"error": "Choose the PGN again."}
    k = key_move(body.get("text", ""), a["sans"], a["uci"])
    if "ply" in k and k["ply"] >= 0:
        b = chess.Board()
        for u in a["uci"][:k["ply"] + 1]:
            b.push_uci(u)
        k["svg"] = chess.svg.board(b, size=280, lastmove=b.peek(), coordinates=False)
        # v2: Puzzle of the Day - the line to find + the suggested question, and the puzzle position (before the move,
        # from the side to move).
        pl, q, line = puzzle_line(a, k["ply"])
        k.update({"pl": pl, "question": q, "line": line})
        pb = chess.Board()
        for u in a["uci"][:k["ply"]]:
            pb.push_uci(u)
        k["puzzle_svg"] = chess.svg.board(pb, size=280, lastmove=pb.peek() if pb.move_stack else None,
                                          coordinates=False, orientation=pb.turn)
    return k


def api_tags(body):
    return {"tags": match_tags(body.get("text", ""))}


def api_preview(body):
    a = Session.analysed.get(body.get("token", ""))
    if not a:
        return {"error": "Choose the PGN again."}
    form = body.get("form", {})
    kp = key_move(form.get("keymove", ""), a["sans"], a["uci"])
    if "error" in kp:
        kp = {"ply": -1}
    with db() as con:
        sid = next_sid(con)
    pz = puzzle_of(a, form, kp)
    key = puzzle_key(a["uci"], kp["ply"]) if pz else a["key"]
    item = item_for(a, form_norm(form), key, sid, "new", kp, puzzle=pz)
    data, size, extras = build_push(item, bool(form.get("test")))
    return {"bytes": size, "limit": PUSH_LIMIT, "extras": extras, "too_long": too_long(item)}


def is_puzzle_form(form):
    return (form.get("type") or "game") == "puzzle"


def puzzle_of(a, form, kp):
    """v2: {"pl", "q"} for a Puzzle of the Day form with a good key move, else None."""
    if not is_puzzle_form(form) or kp.get("ply", -1) < 0:
        return None
    pl, q, _ = puzzle_line(a, kp["ply"])
    return {"pl": pl, "q": clean(form.get("question", ""), 40) or q}


def form_norm(form):
    tags = match_tags(form.get("tags", ""))
    return {"title": form.get("title", ""), "story": form.get("story", ""), "round": form.get("round", ""), "tags": tags}


def api_send(body):
    with _lock, shared_send():
        return do_send(body)


def do_send(body):
    a = Session.analysed.get(body.get("token", ""))
    if not a:
        return {"error": "Choose the PGN again (the tool was restarted)."}
    form = body.get("form", {})
    test = bool(form.get("test"))
    title = clean(form.get("title", ""), MAX_TITLE)
    story = clean(form.get("story", ""), MAX_STORY + 100)
    if not title:
        return {"error": "Type a title."}
    if not story and not is_puzzle_form(form):
        # KT 390 (18 Nov 2026): a game needs its story; a puzzle's story is optional (it shows after solving), like
        # the phone admin app ("shown after the puzzle") - before, the phone said optional and this refused it.
        return {"error": "Type the story (2-3 lines in your own words)."}
    if len(story) > MAX_STORY:
        return {"error": f"The story is {len(story)} characters; keep it under {MAX_STORY}."}
    kp = key_move(form.get("keymove", ""), a["sans"], a["uci"])
    if "error" in kp:
        return {"error": "Key move: " + kp["error"]}
    # v2: Puzzle of the Day
    pz = None
    if is_puzzle_form(form):
        if kp.get("ply", -1) < 0:
            return {"error": "A puzzle needs its key move (the move the player has to find), e.g. 16.Qb8+."}
        pz = puzzle_of(a, form, kp)
    queue = bool(body.get("queue"))
    if queue and test:
        return {"error": "Test sends go out one by one - untick Test send to add to today's list."}
    if not os.path.exists(FIREBASE_KEY):
        return {"error": "Firebase key missing - Setup tab, step 1."}
    clock = clock_problem()
    if clock:
        return {"error": clock}
    choice = body.get("choice", "")
    key, kind = (puzzle_key(a["uci"], kp["ply"]) if pz else a["key"]), "new"
    with db() as con:
        prev = con.execute("SELECT sid, sent_at, kind FROM sends WHERE key=? AND test=0 ORDER BY sid DESC LIMIT 1", (key,)).fetchone()
        corr = []
        if a["round"] and not pz:
            corr = con.execute("SELECT key,title FROM games WHERE white=? AND black=? AND event=? AND round=? AND key!=?",
                               (a["white"], a["black"], a["event"], a["round"], key)).fetchall()
        # v2: the same item already waiting in today's list
        if con.execute("SELECT 1 FROM sends WHERE key=? AND push_status='queued'", (key,)).fetchone():
            return {"error": "This one is already in today's list."}
    if not test:
        if prev and choice not in ("again", "resend", "fix"):
            return {"needs_choice": "repeat", "last_sid": prev["sid"], "last_at": prev["sent_at"], "last_kind": prev["kind"]}
        if corr and not prev and choice not in ("fix", "different"):
            return {"needs_choice": "correction", "of": [dict(r) for r in corr]}
        if choice == "resend" and prev and prev["kind"] == "bundled":
            # KT 299, audit G8: every phone already has the built-in games - a resend would show nothing.
            return {"error": "This game is built into the app, so every phone already has it. Use 'Feature again today' "
                             "(it becomes today's game) or 'Fix that game' (corrects it quietly)."}
        if choice == "again":
            kind = "again"
        elif choice == "fix" and prev:
            kind = "fix"                                   # same game: story / key move / tags corrected
        elif choice == "fix" and corr:
            key, kind = corr[0]["key"], "fix"              # same players + event + game number, different moves
    # v6 (KT 331): History > Fix with a NEW PGN / key move: the correction keeps the game's own key and day.
    fix_key = body.get("fix_key", "")
    if fix_key and not test:
        with db() as con:
            tgt = con.execute("SELECT item FROM sends WHERE key=? AND test=0 ORDER BY sid DESC LIMIT 1", (fix_key,)).fetchone()
        if not tgt:
            return {"error": "The game to fix is not in the history."}
        if bool(json.loads(tgt["item"]).get("fen")) or (json.loads(tgt["item"]).get("t") == "puzzle") != bool(pz):
            return {"error": "A fix must stay the same type (game / puzzle from a game)."}
        key, kind = fix_key, "fix"
    fnorm = {"title": title, "story": story, "round": form.get("round", "") or a["round"], "tags": match_tags(form.get("tags", ""))}

    # KT 299, audit G10: a game that is too long for the 4 KB push.
    probe = item_for(a, fnorm, key, "2000-01-01-01", kind, kp, puzzle=pz)
    tl = too_long(probe)
    if tl and tl["cut"]:
        return {"error": f"Too long for the push by {tl['over']} bytes. Shorten the story by about {tl['cut']} characters "
                         "(letters that are not English count up to 3 each), then press Send again."}

    backup_db()
    if choice == "resend" and prev:
        # Same message again (a delivery that failed): the stored send is pushed once more, nothing new is stored.
        with db() as con:
            con.execute("UPDATE sends SET push_status='sending', feed_status='sending' WHERE sid=?", (prev["sid"],))
        record(f"RESEND {prev['sid']} key={key}")
        return finish(prev["sid"])
    with db() as con:
        rev = 0
        if kind == "fix":
            # KT 299, audit G2: a correction keeps the game's own send id (its day) + the next correction number.
            target = con.execute("SELECT item FROM sends WHERE key=? AND test=0 ORDER BY sid DESC LIMIT 1", (key,)).fetchone()
            sid = json.loads(target["item"])["sid"]
            revs = [int(json.loads(r["item"]).get("rev", "0") or 0) for r in
                    con.execute("SELECT item FROM sends WHERE key=? AND test=0", (key,)).fetchall()
                    if json.loads(r["item"]).get("sid") == sid]
            rev = max(revs + [0]) + 1
            row_sid = f"{sid}-r{rev:03d}"
        elif test:
            # KT 299, audit G12: a test does not use up the day's number. Test phones take each newer test as a correction.
            sid = next_sid(con)
            n = con.execute("SELECT COUNT(*) FROM sends WHERE sid LIKE ?", (sid + "-t%",)).fetchone()[0] + 1
            rev, row_sid = n, f"{sid}-t{n:03d}"
        else:
            sid = next_sid(con)
            # After tests of this number, the real send is a correction for test phones (they show the final text).
            rev = con.execute("SELECT COUNT(*) FROM sends WHERE sid LIKE ?", (sid + "-t%",)).fetchone()[0]
            rev = rev + 1 if rev else 0
            row_sid = sid
        item = item_for(a, fnorm, key, sid, kind, kp, rev, puzzle=pz)
        con.execute(
            "INSERT INTO games(key,title,white,black,event,year,result,eco,opening,round,tags,story,moves,plies,key_ply,"
            "key_uci,pgn_original,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "title=excluded.title, story=excluded.story, tags=excluded.tags, round=excluded.round, moves=excluded.moves, "
            "plies=excluded.plies, key_ply=excluded.key_ply, key_uci=excluded.key_uci, pgn_original=excluded.pgn_original",
            (key, item["title"], item["w"], item["b"], item.get("event", ""), item.get("year", ""), item.get("res", "*"),
             item.get("eco", ""), item.get("opening", ""), item.get("gn", ""), item.get("tags", ""), item["story"],
             item["moves"], len(a["uci"]), kp.get("ply", -1), kp.get("uci", ""), a.get("pgn_original", ""), now_iso()))
        for t in fnorm["tags"]:
            if not t.get("known") and TAG_RE.match(t["id"]):
                con.execute("INSERT OR IGNORE INTO tags(id,name,aliases) VALUES(?,?,?)", (t["id"], t["name"], t["name"].lower()))
        con.execute("INSERT INTO sends(sid,key,kind,sent_at,test,item,push_status,push_detail,feed_status,feed_detail,push_bytes) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (row_sid, key, kind, now_iso(), 1 if test else 0, json.dumps(item, ensure_ascii=False),
                     "queued" if queue else ("skipped (too long - mailbox only)" if tl else "sending"), "",
                     "skipped (test)" if test else ("queued" if queue else "sending"), "", 0))
    what = "PUZZLE" if pz else "GAME"
    if queue:
        record(f"QUEUED {row_sid} [{what} {kind}] '{item['title']}' key={key}")
        return {"queued": row_sid, "title": item["title"], "list": queue_list()}
    record(f"SEND {row_sid} [{what} {kind}{' TEST' if test else ''}{' rev ' + str(rev) if rev else ''}] '{item['title']}' key={key}")
    return finish(row_sid)



# ──────────────────────────────────────────────────────────── v3: Puzzle of the Day from a position (FEN)
# 14 Nov 2026 (KT section 305, owner-approved demo potd-fen-demo-versions/v2): the owner pastes a FEN and the solution
# (every move, the replies included). Everything is checked here BEFORE anything is sent, with the same rules the app
# uses (app: GotdFen + FenValidator + GotdValidator), so the app never has to refuse an item:
#   position: 2-6 FEN parts, 8 ranks of 8, one king each, <= 16 pieces / 8 pawns a side, no pawn on rank 1/8,
#             promoted pieces <= missing pawns, the side not to move not in check, possible checks only, not mate /
#             stalemate already; castling rights the king / rooks do not allow and an impossible en-passant square
#             are dropped quietly; the 50-move counter is set to 0.
#   solution: SAN or UCI (move numbers allowed), every move legal, odd count 1..15 (it ends on the solver's move).
#   mates (up to mate in 4, a plain search - no engine): no faster mate exists, the line is forced (every defence is
#             mated), and at every solver move before the last no OTHER move mates as fast (the app checks the player's
#             moves against exactly the moves sent; on the last move it accepts any mate).
#   duplicates: the same position + solution sent before -> the usual "again / resend / fix" choice; the same position
#             with another solution -> a warning.
# Wire item: t=puzzle, fen, moves (UCI), km=0, kmu=first move, pl=all moves, q, title, event=credit, tags.
# No players: older app versions reject the item cleanly ("no_players").

MATE_CHECK_MAX = 4
SEARCH_NODES = 600_000
POS_MAX_TITLE = 80        # KT 390 (18 Nov 2026): 80 like a game's title (was 60)
POS_MAX_CREDIT = 60
POS_MAX_Q = 40


class _Budget(Exception):
    pass


class _Live:
    """The form re-checks on every key press: a newer check makes an older one still searching stop at once
    (no pile of searches eating the PC's processor)."""
    gen = 0
    lock = threading.Lock()


class _Stale(Exception):
    pass


class _Nodes:
    def __init__(self, limit=SEARCH_NODES, gen=None):
        self.left = limit
        self.gen = gen          # None = never stale (Send, Find it for me)

    def tick(self):
        self.left -= 1
        if self.left < 0:
            raise _Budget()
        if self.gen is not None and (self.left & 1023) == 0 and self.gen != _Live.gen:
            raise _Stale()


def _ordered(b):
    """Checks first, then captures, then the rest: mates are found far sooner."""
    ms = list(b.legal_moves)
    ms.sort(key=lambda m: (not b.gives_check(m), not b.is_capture(m)))
    return ms


def _defences_lose(b, n, nodes):
    """Defender to move: True when EVERY reply still allows mate within n attacker moves."""
    if b.is_checkmate():
        return True
    if b.is_game_over() or n <= 0:
        return False
    for d in list(b.legal_moves):
        nodes.tick()
        b.push(d)
        try:
            if not _can_mate(b, n, nodes):
                return False
        finally:
            b.pop()
    return True


def _can_mate(b, n, nodes):
    """Attacker to move: True when a mate within n moves is forced."""
    if n <= 0 or b.is_game_over():
        return False
    for m in _ordered(b):
        nodes.tick()
        b.push(m)
        try:
            if b.is_checkmate() or (n > 1 and _defences_lose(b, n - 1, nodes)):
                return True
        finally:
            b.pop()
    return False


def _mates_after(b, m, n, nodes):
    """True when attacker move m forces mate within n moves (m included)."""
    b.push(m)
    try:
        return b.is_checkmate() or (n > 1 and _defences_lose(b, n - 1, nodes))
    finally:
        b.pop()


def fastest_mate(b, upto=MATE_CHECK_MAX, nodes=None):
    """Smallest n <= upto with a forced mate in n for the side to move, else None. Raises _Budget when too slow."""
    nodes = nodes or _Nodes()
    for n in range(1, upto + 1):
        if _can_mate(b, n, nodes):
            return n
    return None


PIECE_NAMES = {chess.KING: "K", chess.QUEEN: "Q", chess.ROOK: "R", chess.BISHOP: "B", chess.KNIGHT: "N", chess.PAWN: "pawn"}


def pieces_text(b):
    def side(color):
        ps = [b.pieces(t, color) for t in (chess.KING, chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT)]
        names = []
        for t, sq in zip((chess.KING, chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT), ps):
            names += [PIECE_NAMES[t]] * len(sq)
        p = len(b.pieces(chess.PAWN, color))
        s = " ".join(names)
        return s + (f" + {p} pawn{'s' if p != 1 else ''}" if p else "")
    return f"White {side(chess.WHITE)} · Black {side(chess.BLACK)}"


def promoted_problem(b):
    for color, name in ((chess.WHITE, "White"), (chess.BLACK, "Black")):
        pawns = len(b.pieces(chess.PAWN, color))
        q = len(b.pieces(chess.QUEEN, color)); r = len(b.pieces(chess.ROOK, color))
        bs = len(b.pieces(chess.BISHOP, color)); n = len(b.pieces(chess.KNIGHT, color))
        promoted = max(0, q - 1) + max(0, r - 2) + max(0, bs - 2) + max(0, n - 2)
        if promoted > 8 - pawns:
            return f"{name} has more promoted pieces ({promoted}) than missing pawns ({8 - pawns}) - impossible in a real game."
    return None


def app_dead_position(b):
    """The app's own dead-position rule, copied exactly (PuzzleEngine.isInsufficientMaterial, KT section 305 audit):
    only kings; king + knight vs king; king + bishop vs king; king + bishop vs king + bishop on the same colour;
    only bishops left, all on squares of one colour. The app skips a puzzle whose position it judges dead, so the
    sender refuses it too (as well as anything python-chess calls over)."""
    wn = wb = wbc = bn = bb = bbc = 0
    for sq, pc in b.piece_map().items():
        r, c = 7 - chess.square_rank(sq), chess.square_file(sq)      # app rows: 0 = rank 8
        colour = (r + c) % 2                                         # app: 0 = light, 1 = dark
        if pc.piece_type == chess.KING:
            continue
        if pc.piece_type == chess.KNIGHT:
            if pc.color: wn += 1
            else: bn += 1
        elif pc.piece_type == chess.BISHOP:
            if pc.color: wb += 1; wbc += colour
            else: bb += 1; bbc += colour
        else:
            return False                                             # pawn, rook or queen = enough to mate
    wp, bp = wn + wb, bn + bb
    if wp == 0 and bp == 0:
        return True
    if (wp == 0 and bp == 1) or (bp == 0 and wp == 1):
        return True                                                  # K+N vs K, K+B vs K
    if wp == 1 and wb == 1 and bp == 1 and bb == 1 and wbc == bbc:
        return True
    bishops = wb + bb
    if wn == 0 and bn == 0 and bishops > 0 and (wbc + bbc == 0 or wbc + bbc == bishops):
        return True
    return False


def check_fen(raw):
    """-> (board or None, normalised FEN or "", [errors], [notes]). Same rules as the app (see the block comment)."""
    errors, notes = [], []
    s = WS_RE.sub(" ", (raw or "").strip())
    if not s:
        return None, "", ["Paste a FEN (Lichess / Chess.com: Share › FEN)."], notes
    if len(s) > 100:
        return None, "", ["That is too long for a FEN."], notes
    parts = s.split(" ")
    if not 2 <= len(parts) <= 6:
        return None, "", ["A FEN needs at least the board and the side to move (e.g. '... w')." if len(parts) < 2
                          else "A FEN has at most 6 parts."], notes
    ranks = parts[0].split("/")
    if len(ranks) != 8:
        return None, "", [f"The board part needs 8 ranks separated by '/', this one has {len(ranks)}."], notes
    for i, rk in enumerate(ranks):
        n = 0
        for ch in rk:
            if ch in "12345678":
                n += int(ch)
            elif ch in "pnbrqkPNBRQK":
                n += 1
            else:
                return None, "", [f"Rank {8 - i}: '{ch}' is not a piece letter or a number 1-8."], notes
        if n != 8:
            return None, "", [f"Rank {8 - i} has {n} squares - every rank needs exactly 8."], notes
    if parts[1] not in ("w", "b"):
        return None, "", ["Side to move must be 'w' (White) or 'b' (Black)."], notes
    cr = parts[2] if len(parts) > 2 else "-"
    if cr != "-" and (any(c not in "KQkq" for c in cr) or len(set(cr)) != len(cr)):
        return None, "", ["Castling must be '-' or letters from KQkq (each once)."], notes
    ep = parts[3] if len(parts) > 3 else "-"
    full = parts[5] if len(parts) > 5 else "1"
    full = full if full.isdigit() and 1 <= int(full) <= 9999 else "1"
    try:
        b = chess.Board(f"{parts[0]} {parts[1]} {cr} {ep if re.match(r'^[a-h][36]$', ep) else '-'} 0 {full}")
    except ValueError as e:
        return None, "", [f"Not a valid FEN: {e}"], notes
    st = b.status()
    msgs = [
        (chess.STATUS_NO_WHITE_KING, "White king is missing."), (chess.STATUS_NO_BLACK_KING, "Black king is missing."),
        (chess.STATUS_TOO_MANY_KINGS, "Only one king per side."),
        (chess.STATUS_TOO_MANY_WHITE_PAWNS, "White has more than 8 pawns."), (chess.STATUS_TOO_MANY_BLACK_PAWNS, "Black has more than 8 pawns."),
        (chess.STATUS_PAWNS_ON_BACKRANK, "A pawn stands on the first or last rank."),
        (chess.STATUS_TOO_MANY_WHITE_PIECES, "White has more than 16 pieces."), (chess.STATUS_TOO_MANY_BLACK_PIECES, "Black has more than 16 pieces."),
        (chess.STATUS_OPPOSITE_CHECK, "The side NOT to move is in check - check the side to move ('w' / 'b')."),
        (chess.STATUS_TOO_MANY_CHECKERS, "Impossible check: three or more pieces give check."),
        (chess.STATUS_IMPOSSIBLE_CHECK, "Impossible check: these two pieces cannot give check together."),
    ]
    for flag, text in msgs:
        if st & flag:
            errors.append(text)
    if st & chess.STATUS_BAD_CASTLING_RIGHTS:
        b.castling_rights = b.clean_castling_rights()
        notes.append("Castling rights that the king / rooks do not allow were removed.")
    if st & chess.STATUS_INVALID_EP_SQUARE or (ep != "-" and b.ep_square is None):
        b.ep_square = None
        if ep != "-":
            notes.append("The en-passant square was not possible here and was removed.")
    if not errors:
        p = promoted_problem(b)
        if p:
            errors.append(p)
    if not errors and b.is_game_over():
        errors.append("The game is already over in this position (mate, stalemate or no mating material) - nothing to solve.")
    elif not errors and app_dead_position(b):
        errors.append("Dead position: neither side has enough pieces to mate (the app would skip this puzzle).")
    fen = b.fen() if not errors else ""
    return (b if not errors else None), fen, errors, notes


def parse_solution(b, text):
    """SAN or UCI, move numbers / '...' allowed. -> (uci list, san list, error or None)."""
    if len(text or "") > 400:
        return [], [], "The solution is too long (a puzzle line has at most 15 half-moves)."
    text = (text or "").replace("\u2026", "...").replace("\u2013", "-")   # "1…Re1+" / dashes pasted from web pages
    toks = [t for t in re.split(r"\s+", text.strip()) if t]
    toks = [re.sub(r"^\d+\.(\.\.)?", "", t) for t in toks]
    toks = [re.sub(r"[!?]+$", "", t) for t in toks]                      # annotations: Re8+! Rxe8?
    toks = [t for t in toks if t and not re.match(r"^\d+\.+$", t) and t not in ("...", "1-0", "0-1", "1/2-1/2", "*")]
    if not toks:
        return [], [], "Type the solution: your moves and the replies, e.g. Re8+ Rxe8 Rxe8#."
    bb = b.copy()
    uci, san = [], []
    for i, t in enumerate(toks):
        mv = None
        try:
            mv = bb.parse_san(t)
        except ValueError:
            try:
                m2 = chess.Move.from_uci(t.lower())
                mv = m2 if m2 in bb.legal_moves else None
            except ValueError:
                mv = None
        if mv is None:
            who = "White" if bb.turn else "Black"
            return [], [], f"Move {i + 1} \"{t}\" is not a legal {who} move here."
        san.append(bb.san(mv))
        uci.append(mv.uci())
        bb.push(mv)
    return uci, san, None


def numbered(b, sans):
    """'1.Re8+ Rxe8 2.Rxe8#' from the position's own move number / side."""
    n, white = b.fullmove_number, b.turn
    out = []
    for i, s in enumerate(sans):
        if white:
            out.append(f"{n}.{s}")
        else:
            out.append(f"{n}...{s}" if i == 0 else s)
            n += 1
        white = not white
    return " ".join(out)


def pos_key(fen, uci):
    """Puzzle key: the position (first 4 FEN parts) + the solution."""
    return hashlib.sha256(("pos|" + " ".join(fen.split(" ")[:4]) + "|" + " ".join(uci)).encode()).hexdigest()[:16]


def analyse_position(fen_raw, solution, deep=True, gen=None):
    """Everything the form shows: checks (ok / warn / bad), board, side, pieces, line, suggested question."""
    out = {"checks": [], "ok": False}
    b, fen, errors, notes = check_fen(fen_raw)
    def add(kind, text):
        out["checks"].append({"kind": kind, "text": text})
    if errors:
        for e in errors:
            add("bad", e)
        return out
    out.update({"fen": fen, "side": "White" if b.turn else "Black", "pieces": pieces_text(b),
                "svg": chess.svg.board(b, size=300, orientation=b.turn, coordinates=True)})
    add("ok", "Legal position: one king each, no pawn on the first or last rank, the side not to move is not in check.")
    add("ok", f"{out['side']} to move - the app shows {out['side']} at the bottom.")
    for n in notes:
        add("warn", n)
    uci, sans, err = parse_solution(b, solution)
    if err:
        add("bad", err)
        return out
    if len(uci) > MAX_LINE:
        add("bad", f"The solution has {len(uci)} half-moves; the app plays at most {MAX_LINE}.")
        return out
    if len(uci) % 2 == 0:
        add("bad", f"The solution must end with {out['side']}'s move (an odd number of half-moves); this one ends with the reply.")
        return out
    line = numbered(b, sans)
    out.update({"uci": uci, "sans": sans, "line": line, "key": pos_key(fen, uci)})
    add("ok", f"Solution is legal: {line} ({len(uci)} half-move{'s' if len(uci) > 1 else ''}"
              + (f"; the repl{'ies' if len(uci) > 3 else 'y'} play{'' if len(uci) > 3 else 's'} by itself in the app)" if len(uci) > 1 else ")"))
    end = b.copy()
    for u in uci:
        end.push_uci(u)
    mate = end.is_checkmate()
    n = (len(uci) + 1) // 2
    out["question"] = f"Mate in {n}" if mate else "Best move"
    if mate and deep and n <= MATE_CHECK_MAX:
        try:
            nodes = _Nodes(gen=gen)
            fm = fastest_mate(b, n, nodes)
            if fm is not None and fm < n:
                faster = [b.san(m) for m in b.legal_moves if _mates_after(b, m, fm, nodes)]
                add("bad", f"There is a faster mate: mate in {fm} ({', '.join(faster[:3])}). The app would refuse it, because it "
                           "checks the player's moves against yours - send the faster mate.")
                return out
            bb = b.copy()
            for i in range(0, len(uci), 2):
                left = n - i // 2
                mv = chess.Move.from_uci(uci[i])
                if not _mates_after(bb, mv, left, nodes):
                    # find the defence that escapes
                    bb.push(mv)
                    esc = None
                    for d in list(bb.legal_moves):
                        bb.push(d)
                        ok2 = _can_mate(bb, left - 1, nodes) if left > 1 else False
                        bb.pop()
                        if not ok2:
                            esc = bb.san(d)
                            break
                    bb.pop()
                    add("bad", f"Not a forced mate: after {sans[i]} {'the defence ' + esc + ' escapes' if esc else 'there is a defence'} "
                               f"(no mate in {left - 1 if left > 1 else 0} after it).")
                    return out
                if i < len(uci) - 1:
                    others = [m for m in bb.legal_moves if m != mv and _mates_after(bb, m, left, nodes)]
                    if others:
                        add("bad", f"At {sans[i]} the move{'s' if len(others) > 1 else ''} {', '.join(bb.san(m) for m in others[:3])} "
                                   f"also mate{'' if len(others) > 1 else 's'} in {left}. The app accepts only the moves you send "
                                   "(any mate only on the last move) - choose a position with one solution.")
                        return out
                bb.push(mv)
                if i + 1 < len(uci):
                    bb.push_uci(uci[i + 1])
            add("ok", f"Checked here: forced mate in {n} - every defence is mated.")
            add("ok", "One solution: at every move before the last, only your move mates this fast (on the last move any mate counts).")
        except _Budget:
            add("warn", f"Mate in {n} is too big to check here in time - make sure it is forced and has one solution.")
    elif mate:
        add("warn", f"Mate in {n} is longer than the tool checks (up to mate in {MATE_CHECK_MAX}) - make sure it is forced and has one solution.")
    else:
        # Not a mate: a quick look for a forced mate the player could find instead (the app would call it wrong).
        try:
            nodes = _Nodes(SEARCH_NODES // 3, gen=gen)
            fm = fastest_mate(b, 2, nodes)
            if fm is not None:
                mates = [m for m in b.legal_moves if _mates_after(b, m, fm, nodes)]
                if chess.Move.from_uci(uci[0]) not in mates or len(uci) > 1:
                    add("bad", f"There is a forced mate in {fm} here ({', '.join(b.san(m) for m in mates[:3])}). A player who finds it "
                               "would be told it is wrong - send the mate instead.")
                    return out
        except _Budget:
            pass
        add("warn", "Not a mate: the tool cannot check (no engine) that your moves are the only good ones - make sure they are. "
                    "The app accepts exactly the moves you send.")
    with db() as con:
        same = con.execute("SELECT sid FROM sends WHERE key=? AND test=0 ORDER BY sid DESC LIMIT 1", (out["key"],)).fetchone()
        other = con.execute("SELECT g.key, s.sid FROM games g JOIN sends s ON s.key=g.key WHERE g.fen LIKE ? AND g.key!=? AND s.test=0 "
                            "ORDER BY s.sid DESC LIMIT 1", (" ".join(fen.split(" ")[:4]) + "%", out["key"])).fetchone()
    if same:
        add("warn", f"This position and solution were sent before ({same['sid']}). Sending asks: feature again / resend / fix.")
    elif other:
        add("warn", f"This position was sent before ({other['sid']}) with another solution.")
    else:
        add("ok", "Not sent before (this position is new).")
    out["ok"] = not any(c["kind"] == "bad" for c in out["checks"])
    return out


def find_solution(fen_raw):
    """'Find it for me': the shortest forced mate (up to mate in 4) with the most stubborn defence, or a message."""
    b, fen, errors, _ = check_fen(fen_raw)
    if errors:
        return {"error": errors[0]}
    try:
        nodes = _Nodes(SEARCH_NODES * 2)
        n = fastest_mate(b, MATE_CHECK_MAX, nodes)
        if n is None:
            return {"error": f"No forced mate in {MATE_CHECK_MAX} or fewer here. Type the solution yourself."}
        bb, sans, left = b.copy(), [], n
        while True:
            first = [m for m in _ordered(bb) if _mates_after(bb, m, left, nodes)]
            m = first[0]
            sans.append(bb.san(m))
            bb.push(m)
            if bb.is_checkmate():
                break
            # most stubborn reply: the one after which the mate is longest
            best, best_len = None, -1
            for d in list(bb.legal_moves):
                bb.push(d)
                ln = fastest_mate(bb, left - 1, nodes) or 99
                bb.pop()
                if ln > best_len:
                    best, best_len = d, ln
            sans.append(bb.san(best))
            bb.push(best)
            left -= 1
        return {"solution": " ".join(sans), "mate": n, "question": f"Mate in {n}"}
    except _Budget:
        return {"error": "Too big to work out here in time. Type the solution yourself."}


def pos_item(fen, uci, form, key, sid, kind, rev=0):
    tags = [t["id"] for t in form["tags"] if TAG_RE.match(t["id"])][:6]
    it = {"key": key, "sid": sid, "kind": kind, "t": "puzzle", "title": clean(form["title"], POS_MAX_TITLE),
          "fen": fen, "moves": " ".join(uci), "km": "0", "kmu": uci[0], "pl": str(len(uci)),
          "q": clean(form.get("question", ""), POS_MAX_Q), "event": clean(form.get("credit", ""), POS_MAX_CREDIT),
          "tags": ",".join(tags), "story": clean(form.get("story", ""), 400)}     # KT 390: the story (optional)
    names = [f"{t['id']}={clean(t['name'], 40)}" for t in form["tags"] if t["id"] in tags and t.get("name")]
    if names:
        it["tagn"] = "|".join(names)
    if rev:
        it["rev"] = str(rev)
    return {k: v for k, v in it.items() if v != ""}


def api_fen_check(body):
    with _Live.lock:
        _Live.gen += 1
        gen = _Live.gen
    try:
        a = analyse_position(body.get("fen", ""), body.get("solution", ""), deep=bool(body.get("deep", True)), gen=gen)
    except _Stale:
        return {"stale": True}
    a.pop("uci", None)
    return a


def api_fen_find(body):
    return find_solution(body.get("fen", ""))


def api_send_position(body):
    with _lock, shared_send():
        return do_send_position(body)


def do_send_position(body):
    """Same flow as do_send (checks again here - the form is never trusted), for a puzzle from a position."""
    form = body.get("form", {})
    a = analyse_position(form.get("fen", ""), form.get("solution", ""))
    if not a.get("ok"):
        bad = [c["text"] for c in a.get("checks", []) if c["kind"] == "bad"]
        return {"error": bad[0] if bad else "Check the position and the solution."}
    title = clean(form.get("title", ""), POS_MAX_TITLE + 40)
    if not title:
        return {"error": "Type a title (players see it on the card, e.g. Back-rank mate)."}
    if len(title) > POS_MAX_TITLE:
        return {"error": f"The title is {len(title)} characters; keep it under {POS_MAX_TITLE}."}
    if len(clean(form.get("credit", ""), 200)) > POS_MAX_CREDIT:
        return {"error": f"Credit / source: keep it under {POS_MAX_CREDIT} characters."}
    q = clean(form.get("question", ""), 200) or a["question"]
    if len(q) > POS_MAX_Q:
        return {"error": f"The question is {len(q)} characters; keep it under {POS_MAX_Q}."}
    story = clean(form.get("story", ""), MAX_STORY + 100)          # KT 390: optional, shown after the puzzle is solved
    if len(story) > MAX_STORY:
        return {"error": f"The story is {len(story)} characters; keep it under {MAX_STORY}."}
    test = bool(form.get("test"))
    queue = bool(body.get("queue"))
    if queue and test:
        return {"error": "Test sends go out one by one - untick Test send to add to today's list."}
    if not os.path.exists(FIREBASE_KEY):
        return {"error": "Firebase key missing - Setup tab, step 1."}
    clock = clock_problem()
    if clock:
        return {"error": clock}
    key, kind, choice = a["key"], "new", body.get("choice", "")
    with db() as con:
        prev = con.execute("SELECT sid, sent_at, kind FROM sends WHERE key=? AND test=0 ORDER BY sid DESC LIMIT 1", (key,)).fetchone()
        if con.execute("SELECT 1 FROM sends WHERE key=? AND push_status='queued'", (key,)).fetchone():
            return {"error": "This one is already in today's list."}
    if not test and prev:
        if choice not in ("again", "resend", "fix"):
            return {"needs_choice": "repeat", "last_sid": prev["sid"], "last_at": prev["sent_at"], "last_kind": prev["kind"]}
        if choice == "resend" and prev["kind"] == "bundled":
            return {"error": "This puzzle is built into the app, so every phone already has it. Use 'Feature again today' or 'Fix'."}
        kind = {"again": "again", "fix": "fix"}.get(choice, kind)
    fix_key = body.get("fix_key", "")          # v6 (KT 331): History > Fix with a new position / solution
    if fix_key and not test:
        with db() as con:
            tgt = con.execute("SELECT item FROM sends WHERE key=? AND test=0 ORDER BY sid DESC LIMIT 1", (fix_key,)).fetchone()
        if not tgt or not json.loads(tgt["item"]).get("fen"):
            return {"error": "The puzzle to fix is not in the history (or is not a puzzle from a position)."}
        key, kind = fix_key, "fix"
    fnorm = {"title": title, "question": q, "credit": form.get("credit", ""), "tags": match_tags(form.get("tags", "")),
             "story": story}
    # KT 390: the same 4 KB push check as a game (the story can now make a position puzzle long).
    tl = too_long(pos_item(a["fen"], a["uci"] if "uci" in a else [], fnorm, key, "2000-01-01-01", kind))
    if tl and tl["cut"]:
        return {"error": f"Too long for the push by {tl['over']} bytes. Shorten the story by about {tl['cut']} characters "
                         "(letters that are not English count up to 3 each), then press Send again."}
    backup_db()
    if choice == "resend" and prev:
        with db() as con:
            con.execute("UPDATE sends SET push_status='sending', feed_status='sending' WHERE sid=?", (prev["sid"],))
        record(f"RESEND {prev['sid']} key={key}")
        return finish(prev["sid"])
    with db() as con:
        rev = 0
        if kind == "fix":
            target = con.execute("SELECT item FROM sends WHERE key=? AND test=0 ORDER BY sid DESC LIMIT 1", (key,)).fetchone()
            sid = json.loads(target["item"])["sid"]
            revs = [int(json.loads(r["item"]).get("rev", "0") or 0) for r in
                    con.execute("SELECT item FROM sends WHERE key=? AND test=0", (key,)).fetchall()
                    if json.loads(r["item"]).get("sid") == sid]
            rev = max(revs + [0]) + 1
            row_sid = f"{sid}-r{rev:03d}"
        elif test:
            sid = next_sid(con)
            n = con.execute("SELECT COUNT(*) FROM sends WHERE sid LIKE ?", (sid + "-t%",)).fetchone()[0] + 1
            rev, row_sid = n, f"{sid}-t{n:03d}"
        else:
            sid = next_sid(con)
            rev = con.execute("SELECT COUNT(*) FROM sends WHERE sid LIKE ?", (sid + "-t%",)).fetchone()[0]
            rev = rev + 1 if rev else 0
            row_sid = sid
        item = pos_item(a["fen"], a["uci"] if "uci" in a else [], fnorm, key, sid, kind, rev)
        con.execute(
            "INSERT INTO games(key,title,white,black,event,year,result,eco,opening,round,tags,story,moves,plies,key_ply,"
            "key_uci,pgn_original,created_at,fen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "title=excluded.title, event=excluded.event, tags=excluded.tags, story=excluded.story, fen=excluded.fen",
            (key, item["title"], "", "", item.get("event", ""), "", "*", "", "", "", item.get("tags", ""), item.get("story", ""),
             item["moves"], len(item["moves"].split()), 0, item["kmu"], "", now_iso(), item["fen"]))
        for t in fnorm["tags"]:
            if not t.get("known") and TAG_RE.match(t["id"]):
                con.execute("INSERT OR IGNORE INTO tags(id,name,aliases) VALUES(?,?,?)", (t["id"], t["name"], t["name"].lower()))
        con.execute("INSERT INTO sends(sid,key,kind,sent_at,test,item,push_status,push_detail,feed_status,feed_detail,push_bytes) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (row_sid, key, kind, now_iso(), 1 if test else 0, json.dumps(item, ensure_ascii=False),
                     "queued" if queue else "sending", "",
                     "skipped (test)" if test else ("queued" if queue else "sending"), "", 0))
    if queue:
        record(f"QUEUED {row_sid} [POSITION {kind}] '{item['title']}' key={key}")
        return {"queued": row_sid, "title": item["title"], "list": queue_list()}
    record(f"SEND {row_sid} [POSITION {kind}{' TEST' if test else ''}{' rev ' + str(rev) if rev else ''}] '{item['title']}' key={key}")
    return finish(row_sid)


# ──────────────────────────────────────────────────────────── v2: today's list (several items, one alert)

def queue_list():
    with db() as con:
        rows = con.execute("SELECT sid, kind, item FROM sends WHERE push_status='queued' ORDER BY sid").fetchall()
    out = []
    for r in rows:
        it = json.loads(r["item"])
        line = ""
        if it.get("fen"):   # v3: a puzzle from a position
            try:
                b = chess.Board(it["fen"])
                sans = []
                for u in it["moves"].split():
                    mv = chess.Move.from_uci(u); sans.append(b.san(mv)); b.push(mv)
                line = "solution " + numbered(chess.Board(it["fen"]), sans)
            except Exception:
                line = "position"
        elif it.get("t") == "puzzle":
            km = int(it.get("km", "0"))
            try:
                b = chess.Board()
                for u in it["moves"].split()[:km]:
                    b.push_uci(u)
                san = b.san(chess.Move.from_uci(it["kmu"]))
            except Exception:
                san = ""
            line = f"key move {km // 2 + 1}{'.' if km % 2 == 0 else '...'}{san}"
        out.append({"sid": r["sid"], "kind": r["kind"], "puzzle": it.get("t") == "puzzle", "title": it["title"],
                    "question": it.get("q", ""), "players": "puzzle position" if it.get("fen") else f"{it.get('w', '')} vs {it.get('b', '')}",
                    "line": line, "position": bool(it.get("fen"))})
    return out


def api_queue(_):
    data = None
    items = [json.loads(r["item"]) for r in _queued_rows()]
    if items:
        data, size, extras, sent = build_push_many(items, False)
        return {"list": queue_list(), "bytes": size, "limit": PUSH_LIMIT, "in_push": sent, "extras": extras}
    return {"list": []}


def _queued_rows():
    with db() as con:
        return con.execute("SELECT * FROM sends WHERE push_status='queued' ORDER BY sid").fetchall()


def api_queue_remove(body):
    sid = body.get("sid", "")
    with _lock:
        with db() as con:
            n = con.execute("DELETE FROM sends WHERE sid=? AND push_status='queued'", (sid,)).rowcount
    if n:
        record(f"QUEUE removed {sid}")
    return {"list": queue_list()}


def api_send_queue(_):
    with _lock, shared_send():
        return send_queue()


def send_queue():
    """v2: every queued item in ONE push (phones show one notification) + ONE mailbox update."""
    rows = _queued_rows()
    if not rows:
        return {"error": "Today's list is empty."}
    if not os.path.exists(FIREBASE_KEY):
        return {"error": "Firebase key missing - Setup tab, step 1."}
    clock = clock_problem()
    if clock:
        return {"error": clock}
    backup_db()
    sids = [r["sid"] for r in rows]
    items = [json.loads(r["item"]) for r in rows]
    with db() as con:
        for s in sids:
            con.execute("UPDATE sends SET push_status='sending', feed_status='sending' WHERE sid=?", (s,))
    record(f"SEND LIST {', '.join(sids)} ({len(items)} items, one alert)")
    data, size, extras, sent = build_push_many(items, False)
    ok, detail = send_push(data, False)
    st = "sent" if ok else "failed"
    with db() as con:
        for i, s in enumerate(sids):
            if i < sent:
                con.execute("UPDATE sends SET push_status=?, push_detail=?, push_bytes=? WHERE sid=?", (st, detail, size, s))
            else:
                con.execute("UPDATE sends SET push_status=?, push_detail=? WHERE sid=?",
                            ("skipped (too long - mailbox only)", "did not fit in the push with the others", s))
    record(f"  push {st}: {detail} ({size} bytes, {sent} of today's items + {extras} earlier)")
    try:
        fok, fdetail = publish_feed(build_feed())
    except Exception as e:
        fok, fdetail = False, str(e)
    fst = "published" if fok else "failed"
    with db() as con:
        for s in sids:
            con.execute("UPDATE sends SET feed_status=?, feed_detail=? WHERE sid=?", (fst, fdetail, s))
    record(f"  feed {fst}: {fdetail}")
    for it in items:
        try:
            write_copies(it, now_iso(), st, fst)
        except Exception as e:
            record(f"  copies failed: {e}")
    note = f"{sent} of {len(items)} in the push" + (f" (the rest through the mailbox file)" if sent < len(items) else "")
    hok, hdetail = publish_history(history_after(sids))   # v4: the complete history on GitHub
    return {"sid": sids[0] + (f" … {sids[-1]}" if len(sids) > 1 else ""), "title": f"{len(items)} items, one alert",
            "steps": {"push": {"ok": ok, "detail": f"{detail} · {note}", "bytes": size, "extras": extras},
                      "feed": {"ok": fok, "detail": fdetail}, "history": {"ok": hok, "detail": hdetail}}, "list": queue_list()}


def finish(sid):
    """Push + feed for one stored send; safe to call again (only the missing parts run)."""
    with db() as con:
        row = con.execute("SELECT * FROM sends WHERE sid=?", (sid,)).fetchone()
    if not row:
        return {"error": f"send {sid} not found"}
    item, test = json.loads(row["item"]), bool(row["test"])
    steps = {}
    if (row["push_status"] or "").startswith("skipped"):
        steps["push"] = {"ok": True, "detail": "not sent: the game is too long for a push - phones get it from the mailbox file"}
    elif row["push_status"] != "sent":
        data, size, extras = build_push(item, test)
        ok, detail = send_push(data, test)
        st = "sent" if ok else "failed"
        with db() as con:
            con.execute("UPDATE sends SET push_status=?, push_detail=?, push_bytes=? WHERE sid=?", (st, detail, size, sid))
        record(f"  push {st}: {detail} ({size} bytes, +{extras} earlier games, topic {TEST_TOPIC if test else TOPIC})")
        steps["push"] = {"ok": ok, "detail": detail, "bytes": size, "extras": extras}
    else:
        steps["push"] = {"ok": True, "detail": "already sent"}
    if not test and row["feed_status"] != "published":
        try:
            ok, detail = publish_feed(build_feed())
        except Exception as e:
            ok, detail = False, str(e)
        st = "published" if ok else "failed"
        with db() as con:
            con.execute("UPDATE sends SET feed_status=?, feed_detail=? WHERE sid=?", (st, detail, sid))
        record(f"  feed {st}: {detail}")
        steps["feed"] = {"ok": ok, "detail": detail}
    elif test:
        steps["feed"] = {"ok": True, "detail": "not published for a test send"}
    else:
        steps["feed"] = {"ok": True, "detail": "already published"}
    with db() as con:
        row = con.execute("SELECT push_status, feed_status FROM sends WHERE sid=?", (sid,)).fetchone()
    try:
        if not test:
            write_copies(item, now_iso(), row["push_status"], row["feed_status"])
    except Exception as e:
        record(f"  copies failed: {e}")
    if not test:   # v4: the complete history on GitHub (never blocks: a failure is retried on the next send)
        hok, hdetail = publish_history(history_after([sid]))
        steps["history"] = {"ok": hok, "detail": hdetail}
    return {"sid": sid, "title": item["title"], "steps": steps}


def api_retry(body):
    with _lock, shared_send():
        return finish(body.get("sid", ""))


def api_history(body):
    q = f"%{body.get('q', '').strip().lower()}%"
    with db() as con:
        games = con.execute(
            "SELECT * FROM games WHERE lower(title) LIKE ? OR lower(white) LIKE ? OR lower(black) LIKE ? OR lower(event) LIKE ? "
            "OR lower(tags) LIKE ? OR lower(opening) LIKE ? "
            # v8 (KT 401, as the Admin app): the day number too ("2026-11-18-02", "11-18")
            "OR key IN (SELECT key FROM sends WHERE lower(sid) LIKE ?)", (q, q, q, q, q, q, q)).fetchall()
        out = []
        for g in games:
            sends = con.execute("SELECT sid,kind,sent_at,test,push_status,push_detail,feed_status,feed_detail,push_bytes "
                                "FROM sends WHERE key=? ORDER BY sid DESC", (g["key"],)).fetchall()
            puzzle = False
            if sends:
                one = con.execute("SELECT item FROM sends WHERE key=? ORDER BY sid DESC LIMIT 1", (g["key"],)).fetchone()
                one_it = json.loads(one["item"])
                puzzle = "position" if one_it.get("fen") else (one_it.get("t") == "puzzle")
            out.append({"key": g["key"], "title": g["title"], "white": g["white"], "black": g["black"], "event": g["event"],
                        "year": g["year"], "tags": g["tags"], "sends": [dict(s) for s in sends],
                        "last": sends[0]["sid"] if sends else "", "puzzle": puzzle})
    out.sort(key=lambda x: x["last"], reverse=True)
    return {"games": out[:300]}


def api_setup(body):
    cfg = load_config()
    msgs = []
    if body.get("firebase_key"):
        try:
            k = json.loads(body["firebase_key"])
            assert k.get("type") == "service_account" and k.get("private_key") and k.get("project_id")
        except Exception:
            return {"error": "That is not a Firebase service-account key file (.json from Project settings > Service accounts)."}
        os.makedirs(DATA, exist_ok=True)
        with open(FIREBASE_KEY, "w", encoding="utf-8") as f:
            json.dump(k, f)
        _token_cache["token"] = None
        msgs.append(f"Firebase key saved (project {k['project_id']}).")
        record(f"setup: firebase key saved for project {k['project_id']}")
    for f in ("github_owner", "github_repo", "github_branch", "project_path"):
        if f in body and body[f] is not None:
            cfg[f] = str(body[f]).strip().strip('"')
    if body.get("github_token"):
        cfg["github_token"] = body["github_token"].strip()
    save_config(cfg)
    signing_key(create=True)
    if body.get("test_github"):
        if not github_ready(cfg):
            return {"error": "Fill GitHub name, repository and token first."}
        code, resp = gh(cfg, "GET", "")
        if code != 200:
            return {"error": github_reason(code, resp, cfg)}
        msgs.append("GitHub connection works.")
    if body.get("test_firebase"):
        try:
            access_token()
            msgs.append("Firebase sign-in works.")
        except Exception as e:
            return {"error": str(e)}
    return {"ok": True, "messages": msgs, "status": api_status(None)}


def api_apply_to_project(_):
    """Writes the feed URL + public key into the app project's gotd_config.xml."""
    cfg = load_config()
    proj = cfg.get("project_path", "")
    path = os.path.join(proj, "app", "src", "main", "res", "values", "gotd_config.xml")
    if not proj or not os.path.exists(path):
        return {"error": f"Not found: {path}. Put the chess-project folder (the one with gradlew.bat) in Setup."}
    urls, pub = feed_urls(cfg), public_key_b64()
    if not urls:
        return {"error": "Fill the GitHub name and repository first."}
    with open(path, encoding="utf-8") as f:
        xml = f.read()
    new = re.sub(r'(<string-array name="gotd_feed_urls"[^>]*>)(.*?)(</string-array>)',
                 lambda m: m.group(1) + "".join(f"\n        <item>{u}</item>" for u in urls) + f"\n    {m.group(3)}",
                 xml, flags=re.S)
    new = re.sub(r'(<string name="gotd_feed_public_key"[^>]*>)(.*?)(</string>)',
                 lambda m: f"{m.group(1)}{pub}{m.group(3)}", new, flags=re.S)
    if new == xml:
        return {"ok": True, "messages": ["gotd_config.xml already has these values."]}
    shutil.copy2(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    record(f"setup: wrote feed URL + public key into {path} (old file kept as .bak)")
    return {"ok": True, "messages": [f"Written into {path}. Build the app as usual."]}


def api_export_seed(_):
    """Latest 30 games as the app's bundled file (assets/gotd/gotd_seed.json)."""
    doc = json.dumps({"v": 1, "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "games": latest_items()}, ensure_ascii=False, separators=(",", ":"))
    cfg = load_config()
    proj = cfg.get("project_path", "")
    target = os.path.join(proj, "app", "src", "main", "assets", "gotd", "gotd_seed.json") if proj else ""
    if target and os.path.isdir(os.path.dirname(target)):
        if os.path.exists(target):
            shutil.copy2(target, target + ".bak")
        with open(target, "w", encoding="utf-8") as f:
            f.write(doc)
        record(f"export: {len(latest_items())} games -> {target}")
        return {"ok": True, "messages": [f"Written {target}. The next app release ships these games."]}
    os.makedirs(EXPORT_DIR, exist_ok=True)
    out = os.path.join(EXPORT_DIR, "gotd_seed.json")
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    return {"ok": True, "messages": [f"Saved {out}. Copy it to chess-project/app/src/main/assets/gotd/ before a release."]}


# ──────────────────────────────────────────────────────────── quick setup (13 Nov 2026, owner: "reduce my job")
# "Set up GitHub for me": GitHub CLI (installed with winget / brew if missing) -> one browser sign-in ("Authorize") ->
# repository chess-gotd -> GitHub Pages on -> first feed uploaded -> waits until the address answers -> writes the
# addresses + public key into the app project. "Get the Firebase key": opens the exact Firebase page for this app's
# project (read from app/google-services.json) -> the player clicks "Generate new private key" -> the downloaded file
# is picked up from Downloads by itself. Both run in the background; the form shows each step.

REPO_NAME = "chess-gotd"


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.name, self.lines, self.state = "", [], "idle"      # idle | running | done | failed
        self.code, self.code_url = "", ""                        # GitHub one-time code shown big in the form
        self.echo = False                                        # SETUP_GAME_OF_THE_DAY.bat: print every step

    def start(self, name, fn):
        with self.lock:
            if self.state == "running":
                return False
            self.name, self.lines, self.state = name, [], "running"
            self.code, self.code_url = "", ""
        def go():
            try:
                fn(self)
                self.finish("done")
            except Exception as e:
                self.say(f"STOPPED: {e}")
                record(f"{self.name} failed: {e}\n{traceback.format_exc()}")
                self.finish("failed")
        threading.Thread(target=go, daemon=True).start()
        return True

    def say(self, line):
        with self.lock:
            self.lines.append(line)
        if self.echo:
            print(line, flush=True)
        record(f"{self.name}: {line}")

    def finish(self, state):
        with self.lock:
            self.state = state

    def snapshot(self):
        with self.lock:
            return {"name": self.name, "state": self.state, "lines": list(self.lines), "code": self.code, "code_url": self.code_url}


JOB = Job()


def run(cmd, timeout=120):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def gh_cli():
    cfg = load_config()
    cands = [cfg.get("gh_path", ""), shutil.which("gh") or ""]
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles", ""), os.environ.get("LOCALAPPDATA", "") + "\\Programs"):
            cands.append(os.path.join(base, "GitHub CLI", "gh.exe"))
    else:
        cands += ["/opt/homebrew/bin/gh", "/usr/local/bin/gh"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b")


def gh_login_quiet(job, exe):
    """GitHub CLI sign-in without any window: the one-time code is shown big in the form (with a Copy button) and
    github.com/login/device opens by itself - the player only pastes the code and clicks Authorize.
    False = this GitHub CLI version does not work this way; the caller then opens the normal sign-in window."""
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    try:
        # KT 404: with 'workflow' right away - updating GitHub's copy of the code then needs no second Authorize
        proc = subprocess.Popen([exe, "auth", "login", "--web", "--hostname", "github.com", "--git-protocol", "https",
                                 "--skip-ssh-key", "--scopes", "workflow"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        return False
    lines = []
    reader = threading.Thread(target=lambda: [lines.append(l) for l in proc.stdout], daemon=True)
    reader.start()
    code = ""
    t0 = time.time()
    while time.time() - t0 < 20 and not code and proc.poll() is None:
        m = CODE_RE.search("".join(lines))
        if m:
            code = m.group(1)
        time.sleep(0.3)
    if not code:
        try:
            proc.kill()
        except OSError:
            pass
        record("quiet GitHub sign-in not available: " + "".join(lines)[-300:])
        return False
    try:                                   # some versions wait for Enter before polling; harmless otherwise
        proc.stdin.write("\n")
        proc.stdin.flush()
    except OSError:
        pass
    with job.lock:
        job.code, job.code_url = code, "https://github.com/login/device"
    copied = copy_to_clipboard(code)
    if job.echo:
        print("\n" + "=" * 46 + f"\n   YOUR GITHUB CODE:   {code}\n" + "=" * 46, flush=True)
    job.say(f"  Your code: {code}" + (" (already copied - just press Ctrl+V)" if copied else "") +
            " - paste it on the GitHub page that just opened, Continue, then click Authorize.")
    webbrowser.open("https://github.com/login/device")
    try:
        proc.wait(timeout=900)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("No GitHub sign-in within 15 minutes. Close the tool and open it again when ready.")
    with job.lock:
        job.code = ""
    if run([exe, "auth", "status", "--hostname", "github.com"]).returncode != 0:
        raise RuntimeError("GitHub sign-in did not finish: " + "".join(lines)[-200:].strip())
    return True


def copy_to_clipboard(text):
    """Puts the GitHub code on the clipboard so the player only presses Ctrl+V. Best effort."""
    try:
        if os.name == "nt":
            return subprocess.run(["clip"], input=text, text=True, timeout=5,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode == 0
        if shutil.which("pbcopy"):
            return subprocess.run(["pbcopy"], input=text, text=True, timeout=5).returncode == 0
    except Exception:
        pass
    return False


def gh_login_window(job, exe):
    """Fallback: GitHub CLI's own sign-in window."""
    job.say("  A black window opens: press Enter for each question there; your browser opens GitHub - paste the "
            "8-letter code shown in that window and click Authorize.")
    login = [exe, "auth", "login", "--web", "--hostname", "github.com", "--git-protocol", "https", "--scopes", "workflow"]
    if os.name == "nt":
        subprocess.Popen(["cmd", "/c", "start", "GitHub sign-in", "cmd", "/k"] + login)
    else:
        script = " ".join(f"'{a}'" for a in login)
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{script}"'])
    deadline = time.time() + 600
    while run([exe, "auth", "status", "--hostname", "github.com"]).returncode != 0:
        if time.time() > deadline:
            raise RuntimeError("No GitHub sign-in within 10 minutes. Close the tool and open it again when ready.")
        time.sleep(3)


def guess_project():
    """chess-project next to SCRIPTS_LIBRARY (the usual layout), if it has gradlew.bat."""
    lib = os.path.dirname(TOOL_ROOT)
    for cand in (os.path.join(os.path.dirname(lib), "chess-project"), os.path.join(lib, "chess-project")):
        if os.path.isfile(os.path.join(cand, "gradlew.bat")) or os.path.isfile(os.path.join(cand, "gradlew")):
            return cand
    return ""


def github_setup(job):
    cfg = load_config()
    job.say("Step 1/6 - GitHub's own tool (GitHub CLI)...")
    exe = gh_cli()
    if not exe:
        if os.name == "nt":
            if not shutil.which("winget"):
                raise RuntimeError("winget is missing. Install 'GitHub CLI' from https://cli.github.com and press the button again.")
            job.say("  installing GitHub CLI with winget (one time, about a minute)...")
            r = run(["winget", "install", "-e", "--id", "GitHub.cli", "--silent",
                     "--accept-source-agreements", "--accept-package-agreements"], timeout=900)
            record(f"winget gh: rc={r.returncode} {r.stdout[-400:]} {r.stderr[-400:]}")
        elif shutil.which("brew"):
            job.say("  installing GitHub CLI with Homebrew...")
            run(["brew", "install", "gh"], timeout=900)
        exe = gh_cli()
        if not exe:
            raise RuntimeError("GitHub CLI could not be installed. Install it from https://cli.github.com, then press the button again.")
    cfg["gh_path"] = exe
    save_config(cfg)
    job.say(f"  ok: {exe}")

    job.say("Step 2/6 - GitHub sign-in...")
    if run([exe, "auth", "status", "--hostname", "github.com"]).returncode != 0:
        if not gh_login_quiet(job, exe):
            gh_login_window(job, exe)
    cfg["github_auth"] = "gh"
    cfg.pop("github_token", None)
    r = run([exe, "api", "user", "--jq", ".login"])
    owner = r.stdout.strip()
    if r.returncode != 0 or not owner:
        raise RuntimeError(f"Signed in, but GitHub did not say who you are: {r.stderr.strip()[:200]}")
    cfg.update(github_owner=owner, github_repo=cfg.get("github_repo") or REPO_NAME, github_branch=cfg.get("github_branch") or "main")
    save_config(cfg)
    job.say(f"  ok: signed in as {owner}")

    repo = cfg["github_repo"]
    job.say(f"Step 3/6 - repository {owner}/{repo}...")
    code, info = gh(cfg, "GET", "")
    if code == 404:
        r = run([exe, "repo", "create", repo, "--public", "--add-readme",
                 "--description", "Game of the Day feed for the Chess Puzzles app (public famous games)"])
        if r.returncode != 0:
            raise RuntimeError(f"Could not create the repository: {r.stderr.strip()[:300]}")
        for _ in range(10):
            code, info = gh(cfg, "GET", "")
            if code == 200:
                break
            time.sleep(2)
    if code != 200:
        raise RuntimeError(github_reason(code, info, cfg))
    branch = info.get("default_branch") or "main"
    cfg["github_branch"] = branch
    save_config(cfg)
    job.say(f"  ok: https://github.com/{owner}/{repo} (branch {branch})")

    job.say("Step 4/6 - uploading the first games file...")
    ok, detail = publish_feed(build_feed())
    if not ok:
        raise RuntimeError(detail)
    job.say("  ok")

    job.say("Step 5/6 - turning on GitHub Pages...")
    code, resp = gh(cfg, "POST", "/pages", {"source": {"branch": branch, "path": "/"}})
    if code not in (201, 409):           # 409 = already on
        code2, _ = gh(cfg, "GET", "/pages")
        if code2 != 200:
            raise RuntimeError("GitHub Pages could not be turned on: " + github_reason(code, resp, cfg))
    publish_feed(build_feed())           # a fresh commit after Pages is on starts the first Pages build
    url = feed_url(cfg)
    job.say(f"  waiting until {url} answers (GitHub needs 1-3 minutes the first time)...")
    deadline = time.time() + 600
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"Cache-Control": "no-cache"}), timeout=20) as r:
                if r.status == 200 and b'"payload"' in r.read(4096):
                    break
        except Exception:
            pass
        if time.time() > deadline:
            raise RuntimeError("GitHub Pages is still not answering after 10 minutes. Wait a little and press the button again (it continues from here).")
        time.sleep(10)
    job.say("  ok: the games file is live")

    job.say("Step 6/6 - writing the addresses + public key into the app project...")
    if not cfg.get("project_path"):
        guess = guess_project()
        if guess:
            cfg["project_path"] = guess
            save_config(cfg)
    res = api_apply_to_project(None)
    if res.get("error"):
        job.say("  " + res["error"] + " Fill the chess-project folder and press 'Write both into the app project'.")
    else:
        for m in res.get("messages", []):
            job.say("  " + m)
    job.say("DONE - GitHub is ready. Build the app as usual.")


def firebase_project_id():
    proj = load_config().get("project_path") or guess_project()
    for sub in (("app", "google-services.json"), ("app", "src", "prod", "google-services.json")):
        p = os.path.join(proj, *sub) if proj else ""
        if p and os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)["project_info"]["project_id"]
            except Exception:
                pass
    return ""


def downloads_dir():
    home = os.path.expanduser("~")
    return os.path.join(home, "Downloads")


def firebase_setup(job):
    pid = firebase_project_id()
    started = time.time()
    if pid:
        url = f"https://console.firebase.google.com/project/{pid}/settings/serviceaccounts/adminsdk"
        job.say(f"Step 1/3 - opening the Firebase page of project {pid}...")
    else:
        url = "https://console.firebase.google.com/"
        job.say("Step 1/3 - opening Firebase (app/google-services.json not found: pick the app's project, then "
                "Project settings > Service accounts).")
    webbrowser.open(url)
    job.say("Step 2/3 - on that page click 'Generate new private key' > 'Generate key'. The file goes to Downloads.")
    dl = downloads_dir()
    deadline = time.time() + 900
    found = None
    while not found:
        if time.time() > deadline:
            raise RuntimeError(f"No new key file appeared in {dl} within 15 minutes. Press the button again, or "
                               "choose the file yourself under 'Firebase key'.")
        try:
            for name in os.listdir(dl):
                if not name.lower().endswith(".json"):
                    continue
                path = os.path.join(dl, name)
                if os.path.getmtime(path) < started - 5:
                    continue
                try:
                    with open(path, encoding="utf-8") as f:
                        k = json.load(f)
                except Exception:
                    continue
                if k.get("type") == "service_account" and k.get("private_key") and (not pid or k.get("project_id") == pid):
                    found = (path, k)
                    break
        except OSError:
            pass
        time.sleep(3)
    path, k = found
    job.say(f"Step 3/3 - found {os.path.basename(path)}; saving it into the tool's private folder...")
    res = api_setup({"firebase_key": json.dumps(k), "test_firebase": True})
    if res.get("error"):
        raise RuntimeError(res["error"])
    job.say(f"  The tool keeps its own private copy. You can delete {os.path.basename(path)} from Downloads.")
    job.say(f"DONE - Firebase is ready (project {k.get('project_id')}).")


def setup_all(job):
    """Everything that is still missing, in order. Started by itself when the form opens (owner: reduce my job)."""
    if not github_ready(load_config()) or not github_token(load_config()):
        github_setup(job)
    else:
        job.say("GitHub: already set up.")
    try:
        ok = bool(firebase_creds().get("project_id"))
    except Exception:
        ok = False
    if not ok:
        firebase_setup(job)
    else:
        job.say("Firebase: already set up.")
    # KT 299: the app must trust the current AND the spare key - refresh gotd_config.xml every time (no change = no write).
    if feed_urls(load_config()):
        res = api_apply_to_project(None)
        for m in ([res["error"]] if res.get("error") else res.get("messages", [])):
            job.say("App project: " + m)
    path = write_key_backup()
    if path:
        job.say(f"ONE THING TO KEEP: copy {path} to Google Drive or a pendrive (once). If this PC is ever lost, put it "
                "in Downloads on the new PC and start the tool - games keep working without an app update.")
    job.say("ALL SET - choose a .pgn on the Send tab whenever you like.")


_AUTO = {"started": False}


def api_autosetup(_):
    """Called once by the form when it opens: starts setup_all if anything is missing (once per tool run)."""
    cfg = load_config()
    try:
        fb = bool(firebase_creds().get("project_id"))
    except Exception:
        fb = False
    if github_ready(cfg) and fb:
        return {"needed": False}
    if _AUTO["started"]:
        return {"needed": True, "job": JOB.snapshot()}
    _AUTO["started"] = True
    JOB.start("Setup", setup_all)
    return {"needed": True, "job": JOB.snapshot()}


def api_quick(body):
    what = body.get("what")
    if what == "all":
        ok = JOB.start("Setup", setup_all)
    elif what == "github":
        ok = JOB.start("GitHub setup", github_setup)
    elif what == "firebase":
        ok = JOB.start("Firebase key", firebase_setup)
    else:
        return {"error": "unknown setup"}
    return {"ok": ok, "job": JOB.snapshot()} if ok else {"error": "Another setup is still running - wait for it to finish."}


def api_job(_):
    return {"job": JOB.snapshot()}


# ════════════════════════════════════════════════════════════ v6 (16 Nov 2026, KT section 331)
# Claude conversation "PlayWithEngine_2026-09-28 deep analysis / Arasan integration"; owner-approved demos
# gotd-admin-app-demo-versions/v3..v5 + gotd-sender-demo-versions/v3.
#   * history admin: delete selected / delete all (backup first, undo), built-ins back, full fix (fix_key)
#   * auto-fill: title, story draft, key move, tag suggestions (same rules as the Admin app's AutoFill.kt)
#   * a paired PC: no Firebase / feed / inbox key here - every send goes, signed by this PC's own key and sealed, through
#     the same GitHub Action as the Admin app (gotd_secure.py); pairing by a code your phone approves.

import gotd_secure as sec

HISTORY_META = "gotd/history/meta.json"
BACKUP_DIR_REMOTE = "backup"
_META = {"hidden_builtins": [], "deleted_sids": []}
REPO_HINT = os.path.join(APP_DIR, "gotd_repo.json")          # owner / repo / branch only (not secret), for a copied folder


def load_meta(cfg=None):
    """gotd/history/meta.json -> _META. False when GitHub cannot be read (keeps the last copy)."""
    cfg = cfg or load_config()
    code, text = gh_raw(cfg, HISTORY_META)
    if code == 404:
        _META.update({"hidden_builtins": [], "deleted_sids": []})
        return True
    if code != 200 or text is None:
        return False
    try:
        m = json.loads(text)
        _META["hidden_builtins"] = [k for k in m.get("hidden_builtins", []) if KEY_RE.match(str(k))]
        _META["deleted_sids"] = [s for s in m.get("deleted_sids", []) if re.match(r"^\d{4}-\d{2}-\d{2}-\d{2}$", str(s))]
        return True
    except ValueError:
        return False


def save_meta():
    doc = json.dumps({"v": 1, "generated": now_iso(), "hidden_builtins": sorted(set(_META["hidden_builtins"])),
                      "deleted_sids": sorted(set(_META["deleted_sids"]))}, indent=1)
    return publish_file(HISTORY_META, doc, f"Game of the Day history meta {now_iso()}")


def rebuild_fresh(rows):
    """The database = exactly [rows] (GitHub's history) + the built-ins not deleted; today's list (queued) is kept."""
    with db() as con:
        con.executescript(SCHEMA)
        if "fen" not in [r[1] for r in con.execute("PRAGMA table_info(games)").fetchall()]:
            con.execute("ALTER TABLE games ADD COLUMN fen TEXT DEFAULT ''")
        con.execute("DELETE FROM sends WHERE push_status!='queued'")
        con.execute("DELETE FROM games WHERE key NOT IN (SELECT key FROM sends)")
        for t in DEFAULT_TAGS:
            con.execute("INSERT OR IGNORE INTO tags(id,name,aliases) VALUES(?,?,?)", t)
    merge_history_rows(rows)
    init_db()


def _rows_for(sids):
    """History-format rows for these send ids and their corrections (-r001 ...)."""
    out = []
    with db() as con:
        for s in sids:
            for r in con.execute("SELECT sid, kind, sent_at, item, push_status, feed_status FROM sends WHERE test=0 AND "
                                 "push_status!='queued' AND (sid=? OR sid LIKE ?) ORDER BY sid", (s, s + "-r%")).fetchall():
                out.append({"row": r["sid"], "kind": r["kind"], "sent_at": r["sent_at"], "push": r["push_status"],
                            "feed": r["feed_status"], "item": json.loads(r["item"])})
    return out


def _republish(years, why):
    steps = {}
    hok, hdetail = publish_history(sorted(years))
    steps["history"] = {"ok": hok, "detail": hdetail}
    try:
        fok, fdetail = publish_feed(build_feed())
    except Exception as e:
        fok, fdetail = False, str(e)
    steps["feed"] = {"ok": fok, "detail": fdetail}
    mok, mdetail = save_meta()
    steps["meta"] = {"ok": mok, "detail": mdetail}
    record(f"  {why}: history {hok}, feed {fok}, meta {mok}")
    return steps


def admin_delete(sids, reason):
    """Deletes these sends (and their corrections) from the history and the feed. A backup file comes first."""
    sids = sorted({s for s in sids if isinstance(s, str) and re.match(r"^\d{4}-\d{2}-\d{2}-\d{2}$", s)})
    rows = _rows_for(sids)
    if not rows:
        return {"error": "Nothing to delete (those sends are not in the history)."}
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    path = f"{BACKUP_DIR_REMOTE}/{stamp}.json"
    backup = {"v": 1, "made": now_iso(), "reason": reason, "rows": rows}
    ok, detail = publish_file(path, json.dumps(backup, ensure_ascii=False, indent=1), f"backup before delete {stamp}",
                              branch=OUTBOX_BRANCH)
    if not ok:
        return {"error": "The backup could not be saved, so nothing was deleted: " + detail}
    years, n = set(), 0
    with db() as con:
        for r in rows:
            con.execute("DELETE FROM sends WHERE sid=?", (r["row"],))
            years.add(r["row"][:4])
            n += 1
            if r["kind"] == "bundled":
                _META["hidden_builtins"].append(r["item"]["key"])
            elif SID_ROW_RE.match(r["row"]):
                _META["deleted_sids"].append(r["row"])
        con.execute("DELETE FROM games WHERE key NOT IN (SELECT key FROM sends)")
    record(f"DELETE {n} row(s) ({reason}) - backup {path}")
    steps = _republish(years, "delete")
    return {"deleted": n, "backup": path, "title": f"{n} deleted", "steps": steps}


def admin_delete_all(keep_builtins):
    with db() as con:
        q = "SELECT sid, kind FROM sends WHERE test=0 AND push_status!='queued'"
        sids = [r["sid"] for r in con.execute(q).fetchall()
                if SID_ROW_RE.match(r["sid"]) and not (keep_builtins and r["kind"] == "bundled")]
    if not sids:
        return {"error": "The history is already empty."}
    return admin_delete(sids, "delete all" + (" (built-ins kept)" if keep_builtins else ""))


def admin_restore_builtins():
    hidden = list(_META["hidden_builtins"])
    _META["hidden_builtins"] = []
    init_db(skip_builtins=set())
    with db() as con:
        years = {r["sid"][:4] for r in con.execute("SELECT sid FROM sends WHERE kind='bundled'").fetchall()}
    record(f"BUILT-INS back ({len(hidden)} had been deleted)")
    steps = _republish(years, "built-ins back")
    return {"restored": len(hidden), "title": "Built-in games and puzzles are back", "steps": steps}


def admin_backups():
    cfg = load_config()
    code, listing = gh(cfg, "GET", f"/contents/{BACKUP_DIR_REMOTE}?ref={OUTBOX_BRANCH}")
    if code == 404:
        return []
    if code != 200 or not isinstance(listing, list):
        return None
    return sorted([f["path"] for f in listing if re.match(r"^\d{4}-\d{2}-\d{2}_\d{6}\.json$", f.get("name", ""))], reverse=True)[:30]


def admin_undo(path):
    if not re.match(r"^backup/\d{4}-\d{2}-\d{2}_\d{6}\.json$", path or ""):
        return {"error": "Unknown backup."}
    cfg = load_config()
    code, text = gh_raw(cfg, path, ref=OUTBOX_BRANCH)
    if code != 200 or not text:
        return {"error": "That backup could not be read from GitHub."}
    rows = json.loads(text).get("rows", [])
    keys = {r["item"]["key"] for r in rows if r.get("kind") == "bundled"}
    sids = {r["row"] for r in rows}
    _META["hidden_builtins"] = [k for k in _META["hidden_builtins"] if k not in keys]
    _META["deleted_sids"] = [s for s in _META["deleted_sids"] if s not in sids]
    n = merge_history_rows(rows)
    record(f"UNDO {path}: {n} row(s) back")
    steps = _republish({r["row"][:4] for r in rows}, "undo")
    return {"restored": n, "title": f"{n} brought back", "steps": steps}


# ──────────────────────────────────────────── auto-fill (same rules as the Admin app's AutoFill.kt)

PIECE_VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
PIECE_WORD = {chess.PAWN: "a pawn", chess.KNIGHT: "a knight", chess.BISHOP: "a bishop", chess.ROOK: "a rook", chess.QUEEN: "the queen"}


def _material(b, color):
    return sum(PIECE_VALUE[p.piece_type] for p in b.piece_map().values() if p.color == color)


def find_key_ply(uci, result):
    """The winner's biggest sacrifice near the end (material given up after the reply, >= 3, earliest on a tie);
    else the first check of the final mating run; else -1. Same in Kotlin (AutoFill.findKeyPly)."""
    if result not in ("1-0", "0-1"):
        return -1, None
    win = chess.WHITE if result == "1-0" else chess.BLACK
    b = chess.Board()
    bal = []                                       # winner minus loser after each ply (index = plies played)
    bal.append(_material(b, win) - _material(b, not win))
    for u in uci:
        b.push_uci(u)
        bal.append(_material(b, win) - _material(b, not win))
    mate = b.is_checkmate()
    n = len(uci)
    lo = max(0, n - 30)
    best, best_drop, piece = -1, 2, None
    boards = [chess.Board()]
    for u in uci:
        nb = boards[-1].copy(stack=False)
        nb.push_uci(u)
        boards.append(nb)
    for i in range(lo, n):
        if boards[i].turn != win or i + 2 > n:
            continue
        j = min(n, i + 3)                          # after the reply AND the winner's next move (a recapture undoes it)
        drop = bal[i] - bal[j]
        if drop > best_drop:
            lost = [t for t in (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.PAWN)
                    if len(boards[j].pieces(t, win)) < len(boards[i].pieces(t, win))]
            best, best_drop, piece = i, drop, (lost[0] if lost else None)
    if best >= 0:
        return best, piece
    if mate:
        b = chess.Board()
        checks = []
        for u in uci:
            b.push_uci(u)
            checks.append(b.is_check())
        i = n - 1                                   # the mating move (winner's)
        while i - 2 >= 0 and checks[i - 2]:
            i -= 2
        return i, None
    return -1, None


def story_draft(a, ply, piece):
    res = a.get("result", "*")
    moves = (len(a["uci"]) + 1) // 2
    sur = lambda n: (n or "").split()[-1] if n else ""
    if res == "1/2-1/2":
        return f"A hard-fought draw in {moves} moves."
    if res not in ("1-0", "0-1"):
        return ""
    who = sur(a["white"] if res == "1-0" else a["black"]) or ("White" if res == "1-0" else "Black")
    b = chess.Board()
    for u in a["uci"]:
        b.push_uci(u)
    mate = b.is_checkmate()
    if ply < 0:
        return f"{who} wins in {moves} moves." if not mate else f"{who} mates in {moves} moves."
    n = ply // 2 + 1
    left = (len(a["uci"]) - 1 - ply) // 2
    end = ("mates on the next move" if left == 1 else "mates at once" if left == 0 else f"mates {left} moves later") if mate else "wins"
    if piece and piece in PIECE_WORD:
        give = PIECE_WORD[piece].replace("the queen", "his queen")
        return f"{who} gives up {give} on move {n} and {end}."
    return f"{who} starts the final attack on move {n} and {end}."


def famous_title(key):
    seed = os.path.join(APP_DIR, "seed", "gotd_seed.json")
    try:
        with open(seed, encoding="utf-8") as f:
            for it in json.load(f).get("games", []):
                if it.get("key") == key and it.get("title"):
                    return it["title"]
    except (OSError, ValueError):
        pass
    return ""


def tag_suggestions(a, ply, piece, puzzle_q=""):
    with db() as con:
        known = {r["id"]: r["name"] for r in con.execute("SELECT id,name FROM tags").fetchall()}
        before = con.execute("SELECT tags FROM games WHERE key=?", (a["key"],)).fetchone()
    out, seen = [], set()

    def add(tid, name, pick):
        if tid and TAG_RE.match(tid) and tid not in seen:
            seen.add(tid)
            out.append({"id": tid, "name": known.get(tid, name), "known": tid in known, "picked": bool(pick)})
    for t in ((before["tags"] if before else "") or "").split(","):
        add(t.strip(), t.strip(), True)
    if famous_title(a["key"]):
        add("famous-games", "Famous games", True)
    for p in (a.get("white", ""), a.get("black", "")):
        s = slug(p.split()[-1]) if p and p not in ("White", "Black") else ""
        add(s, p.split()[-1] if p else s, s in known)
    ev = a.get("tag_suggest") or {}
    for t in (ev.get("id") or "").split(","):
        add(t.strip(), ev.get("name") or t.strip(), ev.get("known"))
    if piece == chess.QUEEN:
        add("queen-sacrifice", "Queen sacrifice", "queen-sacrifice" in known)
    m = re.match(r"^Mate in (\d)$", puzzle_q or "")
    if m:
        add(f"mate-in-{m.group(1)}", f"Mate in {m.group(1)}", True)
    if a.get("opening"):
        fam = slug(re.split(r"[:,]", a["opening"])[0])
        add(fam, re.split(r"[:,]", a["opening"])[0], fam in known)
    return out[:10]


def autofill(a):
    """{title, story, keymove, keyply, tags:[...]} for a freshly read game."""
    ply, piece = find_key_ply(a["uci"], a.get("result", "*"))
    title = a.get("title_suggest") or ""
    with db() as con:
        sent = con.execute("SELECT title FROM games WHERE key=?", (a["key"],)).fetchone()
    title = (sent["title"] if sent and sent["title"] else "") or famous_title(a["key"]) or title
    km = label(ply, a["sans"]) if ply >= 0 else ""
    return {"title": title, "story": story_draft(a, ply, piece), "keymove": km, "keyply": ply,
            "tags": tag_suggestions(a, ply, piece)}


# ──────────────────────────────────────────── a paired PC (talks to the GitHub Action like the Admin app)

class _Remote:
    def __init__(self):
        self.token = ""
        self.team = ""
        self.key = None
        self.dev = ""
        self.inbox = ""
        self.workflow = ADMIN_WORKFLOW

    def load(self):
        try:
            st = sec.PrivateStore(DATA).load()
        except Exception as e:
            record(f"paired keys could not be opened: {e}")
            st = None
        if not st or not st.get("key"):
            return False
        # KT 387: a store without a token (or team / inbox) is read as "not paired" - never a crash at start
        self.key = sec.key_from_pem(st["key"])
        self.token, self.team, self.inbox = st.get("token") or "", st.get("team") or "", st.get("inbox") or ""
        self.dev = sec.device_id(sec.pub_der_b64(self.key))
        self.workflow = st.get("workflow") or ADMIN_WORKFLOW
        return True

    def active(self):
        return bool(self.key and self.token and self.team and self.inbox)


REMOTE = _Remote()


def utc_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def remote_request(op, fields=None, wait_s=420, key=None, token=None, pub=None, inbox=None):
    """Signs + seals one request, starts the Action and waits for its answer (outbox/<id>.json)."""
    cfg = load_config()
    key = key or REMOTE.key
    rid = f"pc-{dt.datetime.now():%Y%m%d%H%M%S}-{secrets.token_hex(3)}"
    req = dict(fields or {}, v=2, id=rid, op=op, created=utc_iso())
    _, env = sec.sign_request(key, req)
    if pub:
        env["pub"] = pub
    box = sec.seal_box(env, rid, inbox or REMOTE.inbox)
    url = f"https://api.github.com/repos/{cfg['github_owner']}/{cfg['github_repo']}/actions/workflows/{REMOTE.workflow}/dispatches"
    hdr = {"Authorization": "Bearer " + (token or REMOTE.token), "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "gotd-sender"}
    code, resp = http_json(url, {"ref": cfg.get("github_branch") or "main", "inputs": {"id": rid, "box": box}}, hdr, method="POST")
    if code != 204:
        return {"stage": "error", "error": github_reason(code, resp, cfg)}
    record(f"REQUEST {op} {rid}")
    t0, polls, ended = time.time(), 0, None
    while time.time() - t0 < wait_s:
        time.sleep(4)
        polls += 1
        c, text = gh_raw(cfg, f"outbox/{rid}.json", ref=OUTBOX_BRANCH)
        if c == 200 and text:
            try:
                st = json.loads(text)
            except ValueError:
                continue
            if st.get("stage") in ("done", "error", "needs_choice"):
                return st
            continue
        # KT 332: a run that ended without an answer = refused / cancelled / failed - say so at once, not after 7 minutes
        if polls % 3 == 0 or ended:
            run = _run_of(cfg, rid, token or REMOTE.token)
            if run and run.get("status") == "completed":
                if ended and time.time() - ended > 10:
                    return {"stage": "error", "error": _run_end_reason(run.get("conclusion", ""))}
                ended = ended or time.time()
    return {"stage": "error", "error": "No answer from GitHub within 7 minutes. Open History later - the send may still finish."}


def _run_of(cfg, rid, token):
    url = (f"https://api.github.com/repos/{cfg['github_owner']}/{cfg['github_repo']}/actions/workflows/{REMOTE.workflow}"
           "/runs?event=workflow_dispatch&per_page=30")
    try:
        code, resp = http_json(url, None, {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                                           "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "gotd-sender"})
    except Exception:
        return None
    if code != 200 or not isinstance(resp, dict):
        return None
    return next((r for r in resp.get("workflow_runs", []) if r.get("display_title") == f"gotd {rid}"), None)


def _run_end_reason(conclusion):
    if conclusion == "cancelled":
        return "GitHub cancelled this request before it ran. Send it again."
    if conclusion == "success":
        return ("GitHub refused this request: this PC is not (or no longer) in your device list, or its clock is more than "
                "15 minutes off. Check the PC's date and time, then close this window and double-click SEND_GAME_OF_THE_DAY "
                "again (it adds this PC again by itself).")
    return "The GitHub Action stopped with an error. Try again; if it repeats, open GitHub › Actions › gotd-admin to see why."


def _as_ui(st):
    """Action answer -> the shapes the form already knows (needs_choice / error / sid + steps)."""
    if st.get("stage") == "needs_choice":
        return dict(st.get("choice") or {}, item=st.get("item"))
    if st.get("stage") == "error":
        return {"error": st.get("error", "GitHub stopped the request.")}
    r = st.get("result") or {}
    return dict(r, steps=st.get("steps") or {}, sid=r.get("sid", ""), title=r.get("title", ""))


_refresh_lock = threading.Lock()


def remote_refresh():
    """A fresh copy of GitHub's history. KT 332: one at a time (two at once could clash and show an error after a send
    that went out)."""
    with _refresh_lock:
        cfg = load_config()
        if not load_meta(cfg):
            return False
        rows = remote_history_rows(cfg)
        if rows is None:
            return False
        rebuild_fresh(rows)
        _merge_state["at"] = time.time()
        return True


RQ = os.path.join(DATA, "remote_queue.json")


def _rq_load():
    try:
        with open(RQ, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _rq_save(items):
    with open(RQ, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)


def _rq_view():
    return [{"sid": q["qid"], "kind": q.get("kind", "new"), "puzzle": q.get("puzzle", False), "title": q["title"],
             "question": q.get("question", ""), "players": q.get("players", ""), "line": q.get("line", ""),
             "position": q.get("position", False)} for q in _rq_load()]


def _precheck(key, choice, a=None):
    """The same 'sent before?' question as a local send, on the copy of GitHub's history."""
    with db() as con:
        prev = con.execute("SELECT sid, sent_at, kind FROM sends WHERE key=? AND test=0 ORDER BY sid DESC LIMIT 1", (key,)).fetchone()
        corr = []
        if a and a.get("round"):
            corr = con.execute("SELECT key,title FROM games WHERE white=? AND black=? AND event=? AND round=? AND key!=?",
                               (a["white"], a["black"], a["event"], a["round"], key)).fetchall()
    if prev and choice not in ("again", "resend", "fix"):
        return {"needs_choice": "repeat", "last_sid": prev["sid"], "last_at": prev["sent_at"], "last_kind": prev["kind"]}
    if a and corr and not prev and choice not in ("fix", "different"):
        return {"needs_choice": "correction", "of": [dict(r) for r in corr]}
    return None


def r_send(body):
    a = Session.analysed.get(body.get("token", ""))
    if not a:
        return {"error": "Choose the PGN again (the tool was restarted)."}
    form = body.get("form", {})
    test, queue, choice = bool(form.get("test")), bool(body.get("queue")), body.get("choice", "")
    if not clean(form.get("title", ""), MAX_TITLE):
        return {"error": "Type a title."}
    if not clean(form.get("story", ""), MAX_STORY + 100) and not is_puzzle_form(form):   # KT 390: optional for a puzzle
        return {"error": "Type the story (2-3 lines in your own words)."}
    kp = key_move(form.get("keymove", ""), a["sans"], a["uci"])
    if "error" in kp:
        return {"error": "Key move: " + kp["error"]}
    pz = puzzle_of(a, form, kp)
    if is_puzzle_form(form) and not pz:
        return {"error": "A puzzle needs its key move (the move the player has to find), e.g. 16.Qb8+."}
    key = puzzle_key(a["uci"], kp["ply"]) if pz else a["key"]
    if queue and not test and any(x.get("key") == key for x in _rq_load()):     # v8 (KT 401, as the Admin app)
        return {"error": "This one is already in today's list."}
    if not test and not body.get("fix_key"):
        need = _precheck(key, choice, a)
        if need:
            return need
    item = {"type": "game", "pgn": a["pgn_original"], "index": 0, "choice": choice,
            "form": {"title": form.get("title", ""), "story": form.get("story", ""), "keymove": form.get("keymove", ""),
                     "tags": form.get("tags", ""), "round": form.get("round", ""), "question": form.get("question", ""),
                     "kind": "puzzle" if pz else "game"}}
    if body.get("fix_key"):
        item["fix_key"] = body["fix_key"]
    if queue:
        if test:
            return {"error": "Test sends go out one by one - untick Test send to add to today's list."}
        q = _rq_load()
        q.append({"qid": f"q{len(q) + 1}-{secrets.token_hex(2)}", "item": item, "key": key, "title": clean(form.get("title", ""), MAX_TITLE),
                  "kind": choice or "new", "puzzle": bool(pz), "question": (pz or {}).get("q", ""),
                  "players": f"{a['white']} vs {a['black']}", "line": f"key move {form.get('keymove', '')}" if pz else ""})
        _rq_save(q)
        return {"queued": q[-1]["qid"], "title": q[-1]["title"], "list": _rq_view()}
    st = remote_request("send", {"test": test, "items": [item]})
    remote_refresh()
    return _as_ui(st)


def r_send_position(body):
    form = body.get("form", {})
    a = analyse_position(form.get("fen", ""), form.get("solution", ""))
    if not a.get("ok"):
        bad = [c["text"] for c in a.get("checks", []) if c["kind"] == "bad"]
        return {"error": bad[0] if bad else "Check the position and the solution."}
    test, queue, choice = bool(form.get("test")), bool(body.get("queue")), body.get("choice", "")
    if queue and not test and any(x.get("key") == a["key"] for x in _rq_load()):   # v8 (KT 401, as the Admin app)
        return {"error": "This one is already in today's list."}
    if not test and not body.get("fix_key"):
        need = _precheck(a["key"], choice)
        if need:
            return need
    item = {"type": "position", "choice": choice,
            "form": {k: form.get(k, "") for k in ("fen", "solution", "title", "question", "credit", "tags", "story")}}   # KT 390
    if body.get("fix_key"):
        item["fix_key"] = body["fix_key"]
    if queue:
        if test:
            return {"error": "Test sends go out one by one - untick Test send to add to today's list."}
        q = _rq_load()
        q.append({"qid": f"q{len(q) + 1}-{secrets.token_hex(2)}", "item": item, "key": a["key"], "title": clean(form.get("title", ""), POS_MAX_TITLE),
                  "kind": choice or "new", "puzzle": True, "question": form.get("question", "") or a.get("question", ""),
                  "players": "puzzle position", "line": "solution " + form.get("solution", ""), "position": True})
        _rq_save(q)
        return {"queued": q[-1]["qid"], "title": q[-1]["title"], "list": _rq_view()}
    st = remote_request("send", {"test": test, "items": [item]})
    remote_refresh()
    return _as_ui(st)


def r_queue(_):
    return {"list": _rq_view()}


def r_queue_remove(body):
    _rq_save([q for q in _rq_load() if q["qid"] != body.get("sid", "")])
    return {"list": _rq_view()}


def r_send_queue(_):
    q = _rq_load()
    if not q:
        return {"error": "Today's list is empty."}
    st = remote_request("send", {"test": False, "items": [x["item"] for x in q]})
    if st.get("stage") == "done":
        _rq_save([])
    remote_refresh()
    r = _as_ui(st)
    r["list"] = _rq_view()
    return r


def r_ping(_):
    """v8 (KT 401, as the Admin app's Setup › Check GitHub now): a sealed test through the whole path - nothing is sent
    to players. The last answer is kept (Setup shows it)."""
    r = _as_ui(remote_request("ping", {}))
    if r.get("ping"):
        cfg = load_config()
        cfg["last_ping"] = f"{dt.datetime.now():%d %b %Y %H:%M} - GitHub answered ({r.get('history_rows', 0)} history rows)"
        save_config(cfg)
        r["last_ping"] = cfg["last_ping"]
        record(f"CHECK GITHUB: {cfg['last_ping']}")
    return r


def r_retry(body):
    st = remote_request("retry", {"sid": body.get("sid", "")})
    remote_refresh()
    return _as_ui(st)


def api_repeat(body):
    """History: feature again / resend / fix (title, story, tags, key move, question - or a whole new PGN / position)."""
    fields = {"sid": body.get("sid", ""), "choice": body.get("choice", ""), "form": body.get("form") or {}}
    if isinstance(fields["form"].get("pgn"), str) and fields["form"]["pgn"].strip():   # KT 372: a corrected PGN goes plain
        plain = clean_pgn_text(fields["form"]["pgn"])
        if plain.strip():           # nothing readable: sent as it is, so the Action still says what is wrong
            fields["form"]["pgn"] = plain
    if REMOTE.active():
        st = remote_request("repeat", fields)
        remote_refresh()
        return _as_ui(st)
    return {"error": "This PC is not set up yet - double-click SEND_GAME_OF_THE_DAY again."}


def _admin_op(op, fields=None):
    if not REMOTE.active():
        return {"error": "This PC is not set up yet - double-click SEND_GAME_OF_THE_DAY again."}
    st = remote_request(op, fields or {})
    remote_refresh()
    return _as_ui(st)


def api_delete(body):
    return _admin_op("delete", {"sids": list(body.get("sids") or [])[:500]})


def api_delete_all(body):
    return _admin_op("delete_all", {"keep_builtins": bool(body.get("keep_builtins"))})


def api_restore_builtins(_):
    return _admin_op("restore_builtins")


def api_undo(body):
    return _admin_op("undo_delete", {"backup": body.get("backup", "")})


def api_backups(_):
    b = admin_backups()
    return {"backups": b or []}


def api_history_upload_v6(_):
    return _admin_op("history_upload")


def api_refresh(_):
    ok = remote_refresh() if REMOTE.active() else merge_remote_history()
    return {"ok": bool(ok)}


def api_autofill(body):
    a = Session.analysed.get(body.get("token", ""))
    if not a:
        return {"error": "Choose the PGN again."}
    return autofill(a)


def api_tags_all(_):
    """Every tag ever used (PC + phone), most used first, with when it was last used."""
    with db() as con:
        names = {r["id"]: r["name"] for r in con.execute("SELECT id,name FROM tags").fetchall()}
        use = {}
        for r in con.execute("SELECT sid, item FROM sends WHERE test=0 AND push_status!='queued'").fetchall():
            for t in (json.loads(r["item"]).get("tags", "") or "").split(","):
                t = t.strip()
                if t:
                    n, last = use.get(t, (0, ""))
                    use[t] = (n + 1, max(last, r["sid"][:10]))
    rows = [{"id": t, "name": names.get(t, t), "count": n, "last": last} for t, (n, last) in use.items()]
    rows += [{"id": t, "name": nm, "count": 0, "last": ""} for t, nm in names.items() if t not in use]
    rows.sort(key=lambda x: (-x["count"], x["id"]))
    return {"tags": rows}


# ── week plan (stored sealed with your team key on GitHub; the Action sends each item on its day)

def _plan_op(op, fields=None):
    if not REMOTE.active():
        return {"error": "This PC is not set up yet - double-click SEND_GAME_OF_THE_DAY again."}
    st = remote_request(op, fields or {})
    if st.get("stage") != "done":
        return _as_ui(st)
    r = st.get("result") or {}
    if r.get("sealed"):
        try:
            r = sec.team_open(r["sealed"], REMOTE.team, "gotd-result")
        except Exception:
            return {"error": "The plan could not be opened on this PC (pair it again)."}
    return r


def _plan_view(plan):
    """Same shape as the Action's plan_view (gotd_action.py)."""
    return {"time": plan.get("time", "08:30"), "tz": "Asia/Kolkata",
            "items": [{"pid": p["pid"], "day": p["day"], "state": p.get("state", "planned"), "sid": p.get("sid", ""),
                       "error": p.get("error", ""), "titles": [i.get("title", "") for i in p.get("items", [])],
                       "kinds": [("position" if i.get("type") == "position" else (i.get("form") or {}).get("kind", "game"))
                                 for i in p.get("items", [])]} for p in sorted(plan.get("items", []), key=lambda x: (x["day"], x["pid"]))]}


def api_plan(_):
    """KT 332: read straight from GitHub (sealed with your team key) - seconds, not a GitHub run."""
    if not REMOTE.active():
        return {"error": "This PC is not set up yet - double-click SEND_GAME_OF_THE_DAY again."}
    code, text = gh_raw(load_config(), "plan/plan.json", ref=OUTBOX_BRANCH)
    if code == 404:
        return _plan_view({})
    if code != 200 or not text:
        return {"error": "The week plan could not be read from GitHub. Check the internet and try again."}
    try:
        return _plan_view(sec.team_open(text, REMOTE.team, "gotd-plan"))
    except Exception:
        return {"error": "The plan could not be opened on this PC (pair it again)."}


def api_plan_add(body):
    day = body.get("day", "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        return {"error": "Choose a day."}
    items = []
    if body.get("token"):                      # the game / puzzle now in the form
        a = Session.analysed.get(body["token"])
        if not a:
            return {"error": "Choose the PGN again."}
        form = body.get("form") or {}
        kp = key_move(form.get("keymove", ""), a["sans"], a["uci"])
        pz = puzzle_of(a, form, kp) if "error" not in kp else None
        items.append({"type": "game", "pgn": a["pgn_original"], "index": 0, "choice": body.get("choice", ""),
                      "form": {k: form.get(k, "") for k in ("title", "story", "keymove", "tags", "round", "question")} | {"kind": "puzzle" if pz else "game"},
                      "title": clean(form.get("title", ""), MAX_TITLE)})
    elif body.get("position"):
        form = body["position"]
        items.append({"type": "position", "choice": body.get("choice", ""), "form": {k: form.get(k, "") for k in ("fen", "solution", "title", "question", "credit", "tags", "story")},   # KT 390: + story
                      "title": clean(form.get("title", ""), POS_MAX_TITLE)})
    if not items:
        return {"error": "Nothing to plan."}
    return _plan_op("plan_add", {"day": day, "items": items})


def api_plan_remove(body):
    return _plan_op("plan_remove", {"pid": body.get("pid", "")})


def api_plan_move(body):
    return _plan_op("plan_move", {"pid": body.get("pid", ""), "day": body.get("day", "")})


def api_plan_time(body):
    return _plan_op("plan_time", {"time": body.get("time", "")})


# ── devices + pairing

def api_devices(_):
    """KT 332: read straight from GitHub (names sealed with your team key) - seconds, not a GitHub run."""
    if not REMOTE.active():
        return {"error": "This PC is not set up yet - double-click SEND_GAME_OF_THE_DAY again."}
    code, text = gh_raw(load_config(), "devices.json", ref=OUTBOX_BRANCH)
    if code != 200 or not text:
        return {"error": "Your devices could not be read from GitHub. Check the internet and try again."}
    d = json.loads(text)
    week = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"me": REMOTE.dev, "last_ping": load_config().get("last_ping", ""),       # v8 (KT 401)
            "devices": [{"id": x["id"], "label": sec.device_label(x, REMOTE.team), "kind": x.get("kind", ""),
                         "added": x.get("added", ""), "new": x.get("added", "") > (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
                        for x in d.get("devices", [])],
            "removed": [{"label": sec.device_label(x, REMOTE.team), "removed": x.get("removed", "")}
                        for x in d.get("removed", []) if x.get("removed", "") > week]}


def api_unpair(body):
    return _plan_op("unpair", {"dev": body.get("dev", "")})


def _b64u(obj):
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")


def _repo_hint():
    cfg = load_config()
    if cfg.get("github_owner"):
        return cfg
    try:
        with open(REPO_HINT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_pairing(got, key, label):
    """KT 404: keep the device access GitHub sealed to this PC (its own key + the access + team + inbox keys) - the
    PC is one of your devices from now on. A store this Windows user cannot open any more is kept aside, not lost."""
    store = sec.PrivateStore(DATA)
    old = {}
    try:
        old = store.load() or {}
    except Exception:
        if os.path.exists(store.path):
            aside = store.path + ".old-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            os.replace(store.path, aside)
            record(f"the old protected keys could not be opened here - kept aside as {aside}")
    store.save({"key": sec.key_pem(key), "token": got["token"], "team": got["team"], "inbox": got["inbox"],
                "workflow": got.get("workflow") or ADMIN_WORKFLOW, "recovery": old.get("recovery") or {}})
    cfg = load_config()
    cfg.update({"github_owner": got["owner"], "github_repo": got["repo"], "github_branch": got.get("branch") or "main",
                "mode": "remote", "admin_enabled": True, "paired_at": now_iso(), "device_label": label})
    cfg.pop("github_token", None)
    save_config(cfg)
    try:
        with open(REPO_HINT, "w", encoding="utf-8") as f:
            json.dump({"github_owner": got["owner"], "github_repo": got["repo"], "github_branch": got.get("branch") or "main"}, f)
    except OSError:
        pass
    REMOTE.load()
    record(f"SET UP this PC ({label}) with the owner's GitHub sign-in - device {REMOTE.dev}")


def phone_join_code(hours=22):
    """KT 404: the one-time setup code a phone's app build carries (BUILD_ADMIN_APK): the phone joins by itself when it
    is opened (no scan, no approval). Single use; the ticket ends after [hours] (the Action allows up to a day)."""
    if not REMOTE.active():
        return {"error": "This PC is not set up yet - double-click SEND_GAME_OF_THE_DAY once first."}
    ticket = secrets.token_urlsafe(24)
    # KT 404 audit 8: at most 22 h by THIS PC's clock - the Action allows 24 h by GitHub's, so a PC clock up to 2 h
    # fast still works (it was 24 h - 60 s: a PC one minute fast got "bad ticket")
    exp = int(time.time()) + int(min(hours, 22) * 3600)
    st = remote_request("pair_ticket", {"hash": sec.ticket_hash(ticket), "expires": exp})
    if st.get("stage") != "done":
        return {"error": st.get("error") or "GitHub did not answer. Check the internet and try again."}
    cfg = load_config()
    text = "GOTDJOIN2:" + _b64u({"v": 2, "o": cfg["github_owner"], "r": cfg["github_repo"], "b": cfg.get("github_branch") or "main",
                                 "w": REMOTE.workflow, "k": REMOTE.inbox, "t": REMOTE.token, "m": REMOTE.team,
                                 "tk": ticket, "x": exp})
    return {"code": text, "expires": exp}


def api_positions(body):
    """The board after every half-move of the chosen game (for the playable board)."""
    a = Session.analysed.get(body.get("token", ""))
    if not a:
        return {"error": "Choose the PGN again."}
    b = chess.Board()
    out = [{"fen": b.board_fen(), "from": "", "to": ""}]
    for u in a["uci"]:
        mv = chess.Move.from_uci(u)
        b.push(mv)
        out.append({"fen": b.board_fen(), "from": chess.square_name(mv.from_square), "to": chess.square_name(mv.to_square)})
    return {"positions": out, "sans": a["sans"]}


def api_legal(body):
    """Puzzle from a position: the board after the moves played so far + the legal moves now (click-to-move)."""
    board, fen, errors, _ = check_fen(body.get("fen", ""))
    if errors or not fen:
        return {"error": errors[0] if errors else "Paste a valid FEN first."}
    b = chess.Board(fen)
    sans, last = [], None
    for u in (body.get("moves") or [])[:15]:
        try:
            mv = chess.Move.from_uci(u)
        except ValueError:
            return {"error": "bad move"}
        if mv not in b.legal_moves:
            return {"error": "That move is not legal here."}
        sans.append(b.san(mv)); b.push(mv); last = mv
    return {"fen": b.board_fen(), "white": b.turn == chess.WHITE, "sans": sans,
            "legal": [m.uci() for m in b.legal_moves], "from": chess.square_name(last.from_square) if last else "",
            "to": chess.square_name(last.to_square) if last else "", "over": b.is_game_over()}


def api_open_set(body):
    PENDING_OPEN.update({"path": body.get("path", ""), "puzzle": bool(body.get("puzzle"))})
    return {"ok": True}


def api_mode(_):
    cfg = load_config()
    return {"remote": REMOTE.active(), "device": REMOTE.dev, "label": cfg.get("device_label", ""),
            "owner": _repo_hint().get("github_owner", ""), "repo": _repo_hint().get("github_repo", "")}


PENDING_OPEN = {"path": ""}


def api_open_file(body):
    """Right-click › Send as Game of the Day: the form asks for the file waiting here once."""
    p, PENDING_OPEN["path"] = PENDING_OPEN["path"], ""
    if not p or not p.lower().endswith((".pgn", ".txt")) or not os.path.isfile(p) or os.path.getsize(p) > 5_000_000:
        return {"none": True}
    with open(p, encoding="utf-8", errors="replace") as f:
        return {"pgn": f.read(), "name": os.path.basename(p), "puzzle": bool(body.get("puzzle")) or PENDING_OPEN.get("puzzle", False)}


# ──────────────────────────────────────────── paste (KT 339, owner-approved demo gotd-paste-demo-versions/v1)
# A PGN, a Lichess game link or a FEN pasted on the Send tab (button or Ctrl+V). Chess.com links cannot be read
# (Chess.com has no public single-game download): a clear message says to use Share › PGN there.

LICHESS_RE = re.compile(r"lichess\.org/(?:game/export/)?([A-Za-z0-9]{8})(?:[A-Za-z0-9]{4})?(?![A-Za-z0-9])")
FEN_RE = re.compile(r"^\s*([pnbrqkPNBRQK1-8]{1,8}(?:/[pnbrqkPNBRQK1-8]{1,8}){7})(\s+[wb](\s+\S+){0,4})?\s*$")
# KT 340: Lichess pages whose name looks like a game id (8 letters) - not games; and analysis / editor links carry a FEN
LICHESS_NOT_GAMES = {"analysis", "training", "practice", "streamer", "calendar", "download", "features", "settings",
                     "insights", "password", "notifica", "tutorial", "playlist", "starters", "contacts", "followed", "accounts"}
LICHESS_FEN_RE = re.compile(r"lichess\.org/(?:analysis|editor)/(?:standard/|fromPosition/)?([pnbrqkPNBRQK1-8]{1,8}(?:/[pnbrqkPNBRQK1-8]{1,8}){7}[^\s#?]*)")


def paste_detect(text):
    """-> {what: pgn | lichess | fen | chesscom | empty | unknown, text: plain words, ok: bool, ...}."""
    t = (text or "").strip()
    if not t:
        return {"what": "empty", "ok": False, "text": "Nothing pasted yet - press Ctrl+V in the box."}
    if len(t) > 5_000_000:
        return {"what": "unknown", "ok": False, "text": "That is too big (over 5 MB)."}
    head = t[:300]
    link = LICHESS_FEN_RE.search(head) if "[" not in head and len(t) < 300 else None
    fen_line = urllib.parse.unquote(link.group(1).replace("_", " ")).strip() if link else ""
    if not fen_line and "[" not in head and FEN_RE.match(t.splitlines()[0] if t.count("\n") < 2 else ""):
        fen_line = t.splitlines()[0].strip()
    if fen_line:
        _, fen, errors, _ = check_fen(fen_line)
        if errors:
            return {"what": "fen", "ok": False, "text": "A position (FEN), but: " + errors[0]}
        side = "White" if fen.split(" ")[1] == "w" else "Black"
        return {"what": "fen", "ok": True, "fen": fen, "text": f"A position (FEN) · {side} to move · becomes a Puzzle from a position"}
    if "[" not in head and re.search(r"(^|//|\.)chess\.com/", head):
        return {"what": "chesscom", "ok": False,
                "text": "Chess.com links cannot be read. On Chess.com: Share › PGN › copy, then paste that."}
    m = LICHESS_RE.search(head)
    if m and "[" not in head and len(t) < 300 and m.group(1).lower() in LICHESS_NOT_GAMES:
        return {"what": "unknown", "ok": False, "text": "That Lichess link is not a game. Open the game on Lichess and copy its link (or its PGN)."}
    if m and "[" not in head and len(t) < 300:
        return {"what": "lichess", "ok": True, "id": m.group(1), "text": "A Lichess game - the sender fetches its PGN"}
    games = [g for g in read_games(t) if g.variations or g.errors]       # a game with at least one move (or a bad one)
    games = [g for g in games if g.variations or _illegal_san(g)]       # "hello there" is not a PGN
    if not games:
        return {"what": "unknown", "ok": False, "text": "Not a PGN, a Lichess game link or a FEN."}
    good = [g for g in games[:20] if not game_problem(g)]
    if not good:                                # KT 340: say why now - not a green tick, then an error after Use it
        g0 = games[0]
        san = _illegal_san(g0)
        why = f"Move {len(list(g0.mainline_moves())) + 1} ({san}) is not legal. Fix it and paste again." if san else game_problem(g0)
        return {"what": "pgn", "ok": False, "text": f"A PGN, but: {why}"}
    s0 = summary(good[0])
    moves = (len(list(good[0].mainline_moves())) + 1) // 2
    many = f"{len(games)} games - you pick one" if len(games) > 1 else "1 game"
    return {"what": "pgn", "ok": True, "text": f"A PGN · {many} · {s0['white'] or 'White'} vs {s0['black'] or 'Black'} · {moves} move{'' if moves == 1 else 's'}"}


def _illegal_san(game):
    """The move python-chess stopped at ("" when the text was not moves at all)."""
    for e in getattr(game, "errors", []) or []:
        m = re.search(r"(?:illegal|ambiguous) san: '([^']+)'", str(e))
        if m and re.match(r"^(?:[NBKRQ]?[a-h]?[1-8]?[-x]?[a-h][1-8](?:=?[NBRQnbrq])?|O-O(?:-O)?|0-0(?:-0)?)[+#]?[!?]*$", m.group(1)):
            return m.group(1)
    return ""


def lichess_pgn(game_id):
    """The game's PGN from Lichess (public export, no sign-in)."""
    req = urllib.request.Request(f"https://lichess.org/game/export/{game_id}?clocks=false&evals=false&literate=false",
                                 headers={"Accept": "application/x-chess-pgn", "User-Agent": "gotd-sender"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read(5_000_000).decode("utf-8", errors="replace"), ""
    except urllib.error.HTTPError as e:
        return "", ("Lichess does not know that game." if e.code == 404 else f"Lichess answered {e.code}.") + " Paste the PGN text instead."
    except Exception as e:
        return "", f"Lichess could not be reached ({type(e).__name__}). Paste the PGN text instead."


def api_paste(body):
    """{text, use}: what it is (for the paste box) - or, with use, the PGN (Lichess fetched) / the FEN to load."""
    d = paste_detect(body.get("text", ""))
    if not body.get("use") or not d.get("ok"):
        return d
    if d["what"] == "fen":
        return {"what": "fen", "fen": d["fen"]}
    if d["what"] == "lichess":
        pgn, err = lichess_pgn(d["id"])
        return {"error": err} if err else {"what": "pgn", "pgn": pgn}
    return {"what": "pgn", "pgn": (body.get("text") or "").strip()}


def _paired(fn_remote):
    """v6: on a PC every send / change goes through the GitHub Action, signed by this PC (no keys here)."""
    def w(body):
        if not REMOTE.active():
            return {"error": "This PC is not set up yet. Close this window and double-click SEND_GAME_OF_THE_DAY again - it sets the PC up by itself."}
        return fn_remote(body)
    return w


ROUTES = {"/api/history_upload": api_history_upload, "/api/fen_check": api_fen_check, "/api/fen_find": api_fen_find, "/api/send_position": api_send_position,
          "/api/autosetup": api_autosetup, "/api/quick": api_quick, "/api/job": api_job, "/api/status": api_status, "/api/parse": api_parse, "/api/keymove": api_keymove, "/api/tags": api_tags,
          "/api/preview": api_preview, "/api/send": api_send, "/api/retry": api_retry, "/api/history": api_history,
          "/api/setup": api_setup, "/api/apply": api_apply_to_project, "/api/export_seed": api_export_seed,
          "/api/queue": api_queue, "/api/queue_remove": api_queue_remove, "/api/send_queue": api_send_queue}
# v6 (KT 331): sends + history changes go through the Action; new pages: board / tags / delete / week / devices / pairing.
ROUTES.update({
    "/api/send": _paired(r_send), "/api/send_position": _paired(r_send_position), "/api/queue": r_queue,
    "/api/queue_remove": r_queue_remove, "/api/send_queue": _paired(r_send_queue), "/api/retry": _paired(r_retry),
    "/api/history_upload": api_history_upload_v6, "/api/repeat": api_repeat, "/api/delete": api_delete,
    "/api/delete_all": api_delete_all, "/api/restore_builtins": api_restore_builtins, "/api/undo": api_undo,
    "/api/backups": api_backups, "/api/refresh": api_refresh, "/api/autofill": api_autofill, "/api/tags_all": api_tags_all,
    "/api/plan": api_plan, "/api/plan_add": api_plan_add, "/api/plan_remove": api_plan_remove, "/api/plan_move": api_plan_move,
    "/api/plan_time": api_plan_time, "/api/devices": api_devices, "/api/unpair": api_unpair,
    "/api/mode": api_mode,
    "/api/open_file": api_open_file, "/api/open_set": api_open_set, "/api/positions": api_positions, "/api/legal": api_legal,
    "/api/paste": api_paste,
    "/api/ping": _paired(r_ping),        # v8 (KT 401)
})
for _k in ("/api/setup", "/api/apply", "/api/export_seed", "/api/autosetup", "/api/quick"):
    ROUTES[_k] = (lambda f: (lambda body: f(body) if not REMOTE.active() or _k_allowed(f) else {"error": "Not needed on this PC (it sends through GitHub)."}))(ROUTES[_k])


def _k_allowed(f):
    return f in (api_apply_to_project, api_export_seed)


class Handler(BaseHTTPRequestHandler):
    token = ""

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        # Only this PC's own address: blocks other web pages that point a made-up name at 127.0.0.1.
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(APP_DIR, "web", "index.html"), "rb") as f:
                html = f.read().replace(b"__TOKEN__", Handler.token.encode()).replace(b"__VERSION__", TOOL_VERSION.encode())
            return self._send(200, html, "text/html")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        u = urllib.parse.urlparse(self.path)
        if self.headers.get("X-Token") != Handler.token:
            return self._send(403, {"error": "Open the tool from its own window (token mismatch)."})
        fn = ROUTES.get(u.path)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            self._send(200, fn(body))
        except Exception as e:
            record(f"ERROR {u.path}: {e}\n{traceback.format_exc()}")
            self._send(200, {"error": f"{type(e).__name__}: {e}"})


def setup_only():
    """SETUP_GAME_OF_THE_DAY.bat: the whole one-time setup in this window, no form needed."""
    print("  One-time setup: GitHub (paste a code + Authorize) and Firebase (click Generate key).\n", flush=True)
    JOB.echo = True
    JOB.name, JOB.state = "Setup", "running"
    try:
        setup_all(JOB)
        JOB.finish("done")
    except Exception as e:
        JOB.say(f"STOPPED: {e}")
        record(f"setup failed: {e}\n{traceback.format_exc()}")
        JOB.finish("failed")
        print("\n[ERROR] The setup stopped - the reason is the line above. Fix it and double-click SEND_GAME_OF_THE_DAY again;"
              " finished steps are skipped.", flush=True)
        sys.exit(3)
    print("\nDONE - now build the app. To send a game: SEND_GAME_OF_THE_DAY.bat", flush=True)


SERVER_INFO = os.path.join(DATA, "server.json")


def _forward_open(path, puzzle):
    """Right-click while the sender already runs: hand the file to it and open its page."""
    try:
        with open(SERVER_INFO, encoding="utf-8") as f:
            info = json.load(f)
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/open_set", method="POST",
                                     data=json.dumps({"path": path, "puzzle": puzzle}).encode(),
                                     headers={"X-Token": info["token"], "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            if r.status == 200:
                webbrowser.open(f"http://127.0.0.1:{info['port']}/#send")
                return True
    except Exception:
        pass
    return False


def main():
    # KT 404 audit 13: run as a script this file is "__main__"; one_click / admin_setup "import gotd_sender" would load
    # a SECOND copy, so a setup they finish (REMOTE) never reached the page ("This PC is not set up yet"). One copy.
    sys.modules.setdefault("gotd_sender", sys.modules[__name__])
    os.makedirs(DATA, exist_ok=True)
    init_db()
    REMOTE.load()                 # v6 (KT 331): a paired PC - no Firebase / feed / inbox key on this PC
    open_path, puzzle = "", False
    for flag in ("--open", "--open-puzzle"):
        if flag in sys.argv and sys.argv.index(flag) + 1 < len(sys.argv):
            open_path, puzzle = sys.argv[sys.argv.index(flag) + 1], flag == "--open-puzzle"
    if open_path and _forward_open(open_path, puzzle):
        return
    PENDING_OPEN.update({"path": open_path, "puzzle": puzzle})
    if "--setup" in sys.argv:                # v1-v6 only (first setup of a brand-new GitHub repository)
        setup_only()
        return
    if "--setup-admin" in sys.argv:          # v5/v6 menu (1 repair, 2 new keys for every device, 3 test) - rare
        import admin_setup
        admin_setup.main()
        return
    # v7 (KT 353): ONE click - everything that is still missing, then the form. A stop pauses with what to do; the
    # next click continues (one_click.py).
    import one_click
    if not one_click.run_or_pause():
        sys.exit(4)
    REMOTE.load()                            # KT 404 audit 13: what the one-click steps just set up
    Handler.token = secrets.token_hex(16)
    server = None
    for port in range(PORT, PORT + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print("[ERROR] No free port between 8765 and 8774. Close other copies of this tool and run again.")
        sys.exit(2)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        with open(SERVER_INFO, "w", encoding="utf-8") as f:
            json.dump({"port": server.server_address[1], "token": Handler.token}, f)
    except OSError:
        pass
    record(f"started {TOOL_VERSION} at {url} (data: {DATA}, {'paired' if REMOTE.active() else 'not paired'})")
    print(f"  Form: {url}")
    print(f"  Data: {DATA}")
    print("  Keep this window open while you use the form. Close it to stop the tool.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    threading.Thread(target=remote_refresh if REMOTE.active() else (lambda: None), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
