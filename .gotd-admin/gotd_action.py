#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GOTD - the GitHub Action side  (v6_2026-11-16, KT section 331; v5 = KT 318). Claude conversation
"PlayWithEngine_2026-09-28 deep analysis / Arasan integration".

Every phone (Admin app) and every paired PC sends its requests here. This script:
  1. opens the sealed box (secret GOTD_INBOX_KEY2) and checks the SIGNATURE of the device that made it against
     devices.json (gotd-outbox branch) - the GitHub token that started the run proves nothing by itself;
  2. refuses old requests and any request id seen before (one run per id);
  3. builds a fresh database from GitHub's history (+ the built-ins not deleted, gotd/history/meta.json);
  4. does the work with the PC sender's own code (gotd_sender.py): send / again / resend / fix (also with a new PGN or
     position) / retry, delete / delete all / built-ins back / undo, history upload, the week plan, devices + pairing;
  5. writes the progress / answer to outbox/<id>.json (gotd-outbox) - nothing secret: a plan or a device bundle is
     sealed for your devices only.
Scheduled runs (no request): send what the week plan has for now (Asia/Kolkata time, see plan "time").
v9 (19 Nov 2026, KT 404): no device approves another any more. A PC signed in to YOUR GitHub account adds itself
(owner_pairs below); a phone is set up by the PC that builds its app (a one-time join code inside the build).

Logs are public (public repo): nothing secret is printed. Secrets: GOTD_INBOX_KEY2, GOTD_FIREBASE_KEY, GOTD_FEED_KEY,
GOTD_FEED_KEY_SPARE, GOTD_TEAM_KEY (plan + results), GOTD_DOORBELL (the access a newly paired device receives).
"""
import base64
import datetime as dt
import json
import os
import re
import secrets
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_AGE_S = 6 * 3600
PAIR_MAX_AGE_S = 20 * 60
MAX_ITEMS = 20
MAX_DEVICES = 20
OWNER_PAIR_PREFIX = "ownerpair-"        # KT 404: a run started by a PC adding itself with your GitHub sign-in
OWNER_PAIR_MAX_S = 2 * 3600             # KT 404 audit 3: room for a PC clock 30 minutes off
TICKET_MAX_S = 24 * 3600                # KT 404: the phone's join code ships inside the app build (was 15 minutes)
NEW_DEVICE_WAIT_S = 24 * 3600          # KT 332: a device added less than a day ago cannot remove your other devices
KEEPALIVE_DAYS = 40                    # KT 332: GitHub stops scheduled runs after 60 days without activity
DEVICES_PATH = "devices.json"
PLAN_PATH = "plan/plan.json"
TZ = "Asia/Kolkata"


def fail(msg, code=2):
    print(f"[gotd] {msg}", flush=True)
    sys.exit(code)


def env(name, required=True):
    v = os.environ.get(name, "")
    if required and not v.strip():
        fail(f"missing {name}")
    return v


# KT 400 (18 Nov 2026): GitHub's runners work in UTC - a send between 00:00 and 05:29 India time got YESTERDAY's day
# number (players' apps listed it under yesterday, not TODAY). The day numbers are India's day, like the PC's.
os.environ["TZ"] = TZ
if hasattr(time, "tzset"):
    time.tzset()

# ── 0. a private data folder for this run, BEFORE gotd_sender is imported (it reads GOTD_DATA at import) ──
WORK = tempfile.mkdtemp(prefix="gotd_")
os.environ["GOTD_DATA"] = WORK
os.environ["GOTD_ACTION"] = "1"
sys.path.insert(0, HERE)
import gotd_secure as sec                        # noqa: E402


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def prepare_secrets():
    problems = []
    fb = os.environ.get("GOTD_FIREBASE_KEY", "")
    if fb.strip():
        write(os.path.join(WORK, "firebase_key.json"), fb)
    else:
        problems.append("the Firebase key (GOTD_FIREBASE_KEY)")
    fk = os.environ.get("GOTD_FEED_KEY", "")
    if "PRIVATE KEY" in fk:
        write(os.path.join(WORK, "feed_signing_key.pem"), fk)
    else:
        problems.append("the feed signing key (GOTD_FEED_KEY)")
    spare = os.environ.get("GOTD_FEED_KEY_SPARE", "")
    if "PRIVATE KEY" in spare:
        write(os.path.join(WORK, "feed_signing_key_spare.pem"), spare)
    owner, _, repo = env("GITHUB_REPOSITORY").partition("/")
    cfg = {"github_owner": owner, "github_repo": repo, "github_branch": os.environ.get("GOTD_BRANCH") or "main",
           "github_token": env("GITHUB_TOKEN"), "history_full_upload": "action", "admin_enabled": True}
    write(os.path.join(WORK, "config.json"), json.dumps(cfg))
    return problems


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── devices.json (gotd-outbox): written only by this Action or the owner's own GitHub sign-in ──

def load_devices(gs, cfg):
    code, text = gs.gh_raw(cfg, DEVICES_PATH, ref=gs.OUTBOX_BRANCH)
    if code == 404:
        return {"v": 1, "devices": [], "tickets": []}
    if code != 200 or text is None:
        raise RuntimeError("The list of your devices could not be read from GitHub. Try again in a few minutes.")
    d = json.loads(text)
    d.setdefault("devices", [])
    d.setdefault("tickets", [])
    return d


def save_devices(gs, d, why):
    nowt = int(time.time())
    d["tickets"] = [t for t in d.get("tickets", []) if int(t.get("expires", 0)) > nowt][-20:]
    d["removed"] = d.get("removed", [])[-20:]
    team = os.environ.get("GOTD_TEAM_KEY", "").strip()
    for x in d["devices"] + d["removed"]:          # KT 332: names are sealed - the repo is public
        if team and x.get("label") is not None:
            x["lbl"] = sec.seal_label(x.pop("label"), team)
    ok, detail = gs.publish_file(DEVICES_PATH, json.dumps(d, indent=1), f"devices: {why}", branch=gs.OUTBOX_BRANCH)
    if not ok:
        raise RuntimeError("The list of your devices could not be saved: " + detail)


def public_devices(d):
    """For the sealed answer to your devices only (names opened here)."""
    team = os.environ.get("GOTD_TEAM_KEY", "").strip()
    return [{"id": x["id"], "label": sec.device_label(x, team), "kind": x.get("kind", ""), "added": x.get("added", "")} for x in d["devices"]]


def _age_s(iso):
    try:
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return 10 ** 9


# ── the week plan (gotd-outbox plan/plan.json, sealed with the team key) ──

def team_key():
    k = os.environ.get("GOTD_TEAM_KEY", "").strip()
    if not k:
        raise RuntimeError("The team key is not in GitHub's secret store. On the main PC run SEND_GAME_OF_THE_DAY.bat --setup-admin › 1 (repair).")
    return k


def load_plan(gs, cfg):
    code, text = gs.gh_raw(cfg, PLAN_PATH, ref=gs.OUTBOX_BRANCH)
    if code == 404:
        return {"v": 1, "time": "08:30", "items": []}
    if code != 200 or not text:
        raise RuntimeError("The week plan could not be read from GitHub. Try again in a few minutes.")
    return sec.team_open(text, team_key(), "gotd-plan")


def save_plan(gs, plan, why):
    keep_from = (dt.date.today() - dt.timedelta(days=21)).isoformat()
    plan["items"] = [p for p in plan["items"] if p.get("day", "") >= keep_from or p.get("state") == "planned"][-300:]
    ok, detail = gs.publish_file(PLAN_PATH, sec.team_seal(plan, team_key(), "gotd-plan"), f"plan: {why}", branch=gs.OUTBOX_BRANCH)
    if not ok:
        raise RuntimeError("The week plan could not be saved: " + detail)


def plan_view(plan):
    return {"time": plan.get("time", "08:30"), "tz": TZ,
            "items": [{"pid": p["pid"], "day": p["day"], "state": p.get("state", "planned"), "sid": p.get("sid", ""),
                       "error": p.get("error", ""), "titles": [i.get("title", "") for i in p.get("items", [])],
                       "kinds": [("position" if i.get("type") == "position" else (i.get("form") or {}).get("kind", "game"))
                                 for i in p.get("items", [])]} for p in sorted(plan["items"], key=lambda x: (x["day"], x["pid"]))]}


def sealed(obj):
    return {"sealed": sec.team_seal(obj, team_key(), "gotd-result")}


def local_now():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(TZ))
    except Exception:
        return dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))


# ── main ──

def main():
    scheduled = os.environ.get("GITHUB_EVENT_NAME") == "schedule" or "--scheduled" in sys.argv
    problems = prepare_secrets()
    import gotd_sender as gs                      # noqa: E402  (after GOTD_DATA)
    cfg = gs.load_config()
    owner_pairs(gs, cfg)                          # KT 404: PCs adding themselves with your GitHub sign-in
    if scheduled:
        return run_plan(gs, cfg, problems)

    req_id = env("REQ_ID").strip()
    if req_id.startswith(OWNER_PAIR_PREFIX):      # that run only had to add the PC (above)
        return 0
    if not sec.safe_id(req_id):
        fail("bad request id", 0)
    out_path = f"outbox/{req_id}.json"
    code, _ = gs.gh(cfg, "GET", f"/contents/{out_path}?ref={gs.OUTBOX_BRANCH}")
    if code == 200:
        fail("this request was handled already", 0)

    # 1. open + verify BEFORE anything is written: a request that is not from your device leaves no trace but the log.
    inbox = os.environ.get("GOTD_INBOX_KEY2", "")
    if "PRIVATE KEY" not in inbox:
        fail("GOTD_INBOX_KEY2 missing - on the main PC run SEND_GAME_OF_THE_DAY.bat --setup-admin › 1 (repair)", 0)
    try:
        envl = sec.open_box(env("REQ_BOX"), req_id, inbox)
        devices = load_devices(gs, cfg)
        req, dev = sec.verify_envelope(envl, req_id, devices["devices"])
    except Exception as e:
        print(f"[gotd] refused: {type(e).__name__}: {str(e)[:160]}", flush=True)
        return 0
    try:
        made = dt.datetime.fromisoformat(str(req.get("created", "")).replace("Z", "+00:00"))
        age = (dt.datetime.now(dt.timezone.utc) - made).total_seconds()
    except ValueError:
        age = MAX_AGE_S + 1
    limit = PAIR_MAX_AGE_S if req.get("op") in ("pair_claim", "pair_approve", "pair_ticket") else MAX_AGE_S
    if age > limit or age < -15 * 60:
        print("[gotd] refused: too old / clock wrong", flush=True)
        return 0

    state = {"id": req_id, "stage": "checking", "started": now(), "updated": now(), "steps": {}}

    def outbox(**kw):
        state.update(kw)
        state["updated"] = now()
        ok, detail = gs.publish_file(out_path, json.dumps(state, ensure_ascii=False), f"request {req_id} {state['stage']}",
                                     branch=gs.OUTBOX_BRANCH)
        if not ok:
            print(f"[gotd] outbox write failed: {detail[:160]}", flush=True)

    try:
        outbox(stage="checking")
        if problems and req.get("op") not in ("ping", "devices", "pair_claim", "pair_approve", "pair_ticket", "unpair"):
            outbox(stage="error", error="Missing in GitHub's secret store: " + ", ".join(problems) + ". On the main PC run SEND_GAME_OF_THE_DAY.bat --setup-admin › 1 (repair).")
            return 1
        ensure_access(gs, cfg)
        wait_pc_lock(gs, cfg)
        err = rebuild_db(gs, cfg)
        if err:
            outbox(stage="error", error=err)
            return 1
        res = handle(gs, cfg, req, dev, devices, outbox)
        outbox(**res)
        if res.get("stage") == "done" and req.get("op") in ("send", "repeat", "retry", "delete", "delete_all",
                                                             "restore_builtins", "undo_delete") and not req.get("test"):
            pages_build(gs, cfg)
        return 0
    except SystemExit:
        raise
    except Exception as e:
        traceback.print_exc(limit=3)
        outbox(stage="error", error=f"The request stopped: {type(e).__name__}: {str(e)[:300]}")
        return 1


def ensure_access(gs, cfg):
    """KT 400: access.json (gotd-outbox) = today's device access token, sealed with the team key. Written only when it
    is missing or holds another token (best effort - never stops a request)."""
    door = os.environ.get("GOTD_DOORBELL", "").strip()
    team = os.environ.get("GOTD_TEAM_KEY", "").strip()
    if not door or not team:
        return
    try:
        code, text = gs.gh_raw(cfg, sec.ACCESS_PATH, ref=gs.OUTBOX_BRANCH)
        if code == 200 and text and sec.access_token_of(text, team) == door:
            return
        if code not in (200, 404):
            return
        gs.publish_file(sec.ACCESS_PATH, sec.access_doc(door, team), "device access (sealed)", branch=gs.OUTBOX_BRANCH)
    except Exception as e:
        print(f"[gotd] access file: {type(e).__name__}", flush=True)


def pages_build(gs, cfg):
    """Commits made with the Action's own token do not start a GitHub Pages build - ask for one (best effort)."""
    try:
        gs.gh(cfg, "POST", "/pages/builds")
    except Exception:
        pass


def wait_pc_lock(gs, cfg, max_s=300):
    """v5 PCs held a short lock while sending locally; v6 PCs send through here, but an old v5 PC may still run."""
    t0 = time.time()
    while time.time() - t0 < max_s:
        code, text = gs.gh_raw(cfg, gs.PC_LOCK_PATH, ref=gs.OUTBOX_BRANCH)
        if code != 200 or not text:
            return
        try:
            until = int(json.loads(text).get("until", 0))
        except (ValueError, TypeError):
            return
        if until <= time.time():
            return
        print("[gotd] an older PC sender is sending - waiting", flush=True)
        time.sleep(5)


def rebuild_db(gs, cfg):
    if not gs.load_meta(cfg):
        return "GitHub's history settings (gotd/history/meta.json) could not be read. Try again in a few minutes."
    rows = gs.remote_history_rows(cfg)
    if rows is None:
        return "GitHub's history (gotd/history) could not be read, so duplicates and the day number cannot be checked. Try again in a few minutes."
    gs.rebuild_fresh(rows)
    print(f"[gotd] history rows: {len(rows)}", flush=True)
    return ""


def game_form(f, test):
    return {"title": f.get("title", ""), "story": f.get("story", ""), "keymove": f.get("keymove", ""),
            "tags": f.get("tags", ""), "round": f.get("round", ""),
            "type": "puzzle" if f.get("kind") == "puzzle" else "game", "question": f.get("question", ""), "test": test}


def pos_form(f, test):
    return {"fen": f.get("fen", ""), "solution": f.get("solution", ""), "title": f.get("title", ""),
            "question": f.get("question", ""), "credit": f.get("credit", ""), "tags": f.get("tags", ""),
            "story": f.get("story", ""), "test": test}                    # KT 390: a position puzzle's story


def one_item(gs, it, test, queue):
    fix_key = it.get("fix_key", "") if isinstance(it.get("fix_key", ""), str) else ""
    if fix_key and not gs.KEY_RE.match(fix_key):
        return {"error": "bad fix key"}
    if it.get("type") == "position":
        return gs.do_send_position({"form": pos_form(it.get("form") or {}, test), "choice": it.get("choice", ""), "queue": queue,
                                    "fix_key": fix_key})
    pgn = it.get("pgn", "")
    if not pgn or len(pgn) > 200_000:
        return {"error": "The PGN is missing or too big."}
    games = gs.read_games(pgn)
    if not games:
        return {"error": "No game found in the PGN."}
    idx = int(it.get("index") or 0)
    if not 0 <= idx < len(games):
        return {"error": "The chosen game is not in the PGN."}
    g = games[idx]
    a = gs.analyse(g)
    if "error" in a:
        return a
    a["pgn_original"] = gs.export_clean(g)      # KT 372: the chosen game only, plain (no comments / variations / NAGs)
    tok = secrets.token_hex(8)
    gs.Session.analysed[tok] = a
    return gs.do_send({"token": tok, "form": game_form(it.get("form") or {}, test), "choice": it.get("choice", ""), "queue": queue,
                       "fix_key": fix_key})


def item_from_row(gs, sid, choice, over):
    """History > again / resend / fix. [over] may change the text - and (v6) the PGN / key move / position too."""
    with gs.db() as con:
        row = con.execute("SELECT item FROM sends WHERE sid=?", (sid,)).fetchone()
    if not row:
        return None
    it = json.loads(row["item"])
    over = over or {}
    tags = ", ".join(t for t in (it.get("tags", "") or "").split(",") if t)
    if it.get("fen"):
        f = {"fen": it["fen"], "solution": it.get("moves", ""), "title": it.get("title", ""), "question": it.get("q", ""),
             "credit": it.get("event", ""), "tags": tags, "story": it.get("story", "")}              # KT 390: + story
        f.update({k: v for k, v in over.items() if k in ("title", "question", "credit", "tags", "fen", "solution", "story")
                  and isinstance(v, str)})
        out = {"type": "position", "choice": choice, "form": f}
        if choice == "fix" and (over.get("fen") or over.get("solution")):
            out["fix_key"] = it["key"]
        return out
    pgn = over.get("pgn") if isinstance(over.get("pgn"), str) and over.get("pgn").strip() else gs.pgn_of(it)
    km = it.get("km", "")
    keymove = ""
    if str(km).isdigit():
        import chess
        b = chess.Board()
        sans = []
        for u in it["moves"].split():
            mv = chess.Move.from_uci(u)
            sans.append(b.san(mv))
            b.push(mv)
        keymove = gs.label(int(km), sans)
    f = {"title": it.get("title", ""), "story": it.get("story", ""), "keymove": keymove, "tags": tags,
         "round": it.get("gn", ""), "kind": "puzzle" if it.get("t") == "puzzle" else "game", "question": it.get("q", "")}
    f.update({k: v for k, v in over.items() if k in ("title", "story", "tags", "question", "keymove") and isinstance(v, str)})
    out = {"type": "game", "pgn": pgn, "index": 0, "choice": choice, "form": f}
    if choice == "fix" and (over.get("pgn") or over.get("keymove")):
        out["fix_key"] = it["key"]
    return out


def handle(gs, cfg, req, dev, devices, outbox):
    op = req.get("op", "send")
    test = bool(req.get("test"))

    real_finish, real_queue = gs.finish, gs.send_queue

    def finish_hook(sid):
        outbox(stage="sending")
        return real_finish(sid)

    def queue_hook():
        outbox(stage="sending")
        return real_queue()
    gs.finish, gs.send_queue = finish_hook, queue_hook

    if op == "ping":
        try:
            project = gs.firebase_creds().get("project_id", "")
        except Exception:
            project = ""
        with gs.db() as con:
            n = con.execute("SELECT COUNT(*) FROM sends WHERE test=0").fetchone()[0]
        return {"stage": "done", "result": {"ping": True, "version": gs.TOOL_VERSION, "history_rows": n,
                                            "firebase_project": project, "feed_key": os.path.exists(gs.SIGNING_KEY),
                                            "devices": len(devices["devices"])}}
    if op in ("pair_claim", "pair_approve", "pair_ticket", "unpair", "devices"):
        return devices_op(gs, cfg, op, req, dev, devices)
    if op.startswith("plan_"):
        return plan_op(gs, cfg, op, req, dev)
    if op == "retry":
        return result(gs.finish(str(req.get("sid", ""))))
    if op == "repeat":
        it = item_from_row(gs, str(req.get("sid", "")), req.get("choice", ""), req.get("form"))
        if not it:
            return {"stage": "error", "error": "That send is not in the history."}
        return result(one_item(gs, it, False, False), 0)
    if op == "delete":
        return result(gs.admin_delete([str(s) for s in (req.get("sids") or [])][:500], f"selected by {dev.get('label', '')}"))
    if op == "delete_all":
        return result(gs.admin_delete_all(bool(req.get("keep_builtins"))))
    if op == "restore_builtins":
        return result(gs.admin_restore_builtins())
    if op == "undo_delete":
        return result(gs.admin_undo(str(req.get("backup", ""))))
    if op == "backups":
        return {"stage": "done", "result": {"backups": gs.admin_backups() or []}}
    if op == "history_upload":
        ok, detail = gs.publish_history(gs.history_years())
        return {"stage": "done" if ok else "error", "result": {"title": "History uploaded"}, "error": "" if ok else detail}
    if op != "send":
        return {"stage": "error", "error": f"Unknown request '{op}'."}
    return send_items(gs, req.get("items") or [], test)


def send_items(gs, items, test):
    if not items:
        return {"stage": "error", "error": "Nothing to send."}
    if len(items) > MAX_ITEMS:
        return {"stage": "error", "error": f"At most {MAX_ITEMS} items in one alert."}
    if test and len(items) != 1:
        return {"stage": "error", "error": "Test sends go out one by one."}
    multi = len(items) > 1
    r = {}
    for i, it in enumerate(items):
        r = one_item(gs, it, test, multi)
        if "error" in r or "needs_choice" in r:
            return result(r, i)
    if multi:
        return result(gs.send_queue())
    return result(r, 0)


def result(r, index=None):
    if "needs_choice" in r:
        return {"stage": "needs_choice", "item": index, "choice": r}
    if "error" in r:
        return {"stage": "error", "item": index, "error": r["error"]}
    steps = r.get("steps", {})
    res = {"sid": r.get("sid", ""), "title": r.get("title", "")}
    for k in ("deleted", "backup", "restored"):
        if k in r:
            res[k] = r[k]
    return {"stage": "done", "result": res, "steps": steps}


def devices_op(gs, cfg, op, req, dev, devices):
    nowt = int(time.time())
    if op == "devices":
        return {"stage": "done", "result": sealed({"devices": public_devices(devices), "me": dev["id"]})}
    if op == "unpair":
        target = str(req.get("dev", ""))
        gone = next((d for d in devices["devices"] if d["id"] == target), None)
        if not gone:
            return {"stage": "error", "error": "That device is not in the list."}
        left = [d for d in devices["devices"] if d["id"] != target]
        if not left:
            return {"stage": "error", "error": "This is your last device - add another one first."}
        # KT 332: a device added less than a day ago may remove only itself (a stolen one-time code cannot lock you out)
        if target != dev["id"] and dev.get("by") != "owner sign-in" and _age_s(dev.get("added", "")) < NEW_DEVICE_WAIT_S:
            return {"stage": "error", "error": "This device was added less than a day ago, so it cannot remove your other devices yet. "
                                               "Remove it from an older phone or PC."}
        devices["devices"] = left
        devices.setdefault("removed", []).append({"id": target, "lbl": gone.get("lbl"), "label": gone.get("label"),
                                                  "kind": gone.get("kind", ""), "removed": now(), "by": dev["id"]})
        if devices["removed"][-1]["label"] is None:
            devices["removed"][-1].pop("label")
        if devices["removed"][-1]["lbl"] is None:
            devices["removed"][-1].pop("lbl")
        save_devices(gs, devices, f"removed {target}")
        return {"stage": "done", "result": sealed({"devices": public_devices(devices), "title": "Device removed"})}
    if op == "pair_ticket":
        h, exp = str(req.get("hash", "")), int(req.get("expires", 0) or 0)
        if not re.match(r"^[0-9a-f]{64}$", h) or not nowt < exp <= nowt + TICKET_MAX_S:
            return {"stage": "error", "error": "bad ticket"}
        devices["tickets"].append({"hash": h, "expires": exp, "by": dev["id"]})
        save_devices(gs, devices, f"ticket by {dev['id']}")
        return {"stage": "done", "result": {"title": "Code ready"}}
    if len(devices["devices"]) >= MAX_DEVICES:
        return {"stage": "error", "error": f"You have {MAX_DEVICES} devices already - remove an old one first."}
    if op == "pair_claim":
        h = sec.ticket_hash(str(req.get("ticket", "")))
        t = next((x for x in devices["tickets"] if x.get("hash") == h and int(x.get("expires", 0)) > nowt), None)
        if not t:
            return {"stage": "error", "error": "This app's setup code was used already or has expired. On your PC double-click BUILD_ADMIN_APK again (it installs the app with a new one)."}
        devices["tickets"] = [x for x in devices["tickets"] if x is not t]
        add_device(devices, req["_pub"], req.get("label", ""), req.get("kind", "phone"), t.get("by", ""))
        save_devices(gs, devices, "paired a device (ticket)")
        return {"stage": "done", "result": {"title": "Paired"}}
    # KT 404 (v9): pair_approve is gone - no device approves another one any more (a PC adds itself with your GitHub
    # sign-in, owner_pairs; a phone gets a join code inside its app build). An older app asking is refused.
    return {"stage": "error", "error": "Approving a device is not used any more - on the new PC just double-click SEND_GAME_OF_THE_DAY."}


def owner_pairs(gs, cfg):
    """KT 404 (owner: no approvals - "click the .bat and send"): a PC signed in to YOUR GitHub account writes
    {pid, pub, xfer, label, expires (<= 2 h)} into devices.json "owner_pairs" and starts a run. Only your own GitHub sign-in or
    this Action can write devices.json (the devices' shared token can only read and start runs), so such an entry is
    you. Here the PC is added to your devices and gets the device access sealed to its one-time key
    (outbox/pair/<pid>.json, as an approval did before). Runs at the start of every run; never stops a run."""
    try:
        devices = load_devices(gs, cfg)
    except Exception as e:
        print(f"[gotd] owner pairs: devices not readable ({type(e).__name__})", flush=True)
        return
    asks = devices.get("owner_pairs") or []
    if not asks:
        return
    door = os.environ.get("GOTD_DOORBELL", "").strip()
    try:
        inbox_pub = sec.pub_der_b64(sec.key_from_pem(os.environ.get("GOTD_INBOX_KEY2", "")).public_key())
        team = team_key()
    except Exception:
        inbox_pub, team = "", ""
    nowt = int(time.time())
    added = 0
    for a in asks[-5:]:
        pid, pub, xfer = str(a.get("pid", "")), str(a.get("pub", "")), str(a.get("xfer", ""))
        if not sec.safe_id(pid, 32) or not nowt < int(a.get("expires", 0) or 0) <= nowt + OWNER_PAIR_MAX_S:
            continue
        try:
            sec.load_pub(pub)
            sec.load_pub(xfer)
        except Exception:
            continue
        if not door or not inbox_pub or not team:
            err = "GitHub's secret store lacks the device access. On the main PC run SEND_GAME_OF_THE_DAY.bat --setup-admin › 1 (repair)."
        elif len(devices["devices"]) >= MAX_DEVICES and not any(d["id"] == sec.device_id(pub) for d in devices["devices"]):
            err = f"You have {MAX_DEVICES} devices already - remove an old one in Setup › Your devices first."
        else:
            err = ""
        if err:
            gs.publish_file(f"outbox/pair/{pid}.json", json.dumps({"error": err}), f"pairing {pid}", branch=gs.OUTBOX_BRANCH)
            continue
        new_id = add_device(devices, pub, a.get("label", "") or "PC", "pc", "owner sign-in")
        bundle = sec.seal_bundle({"dev": new_id, "token": door, "team": team, "inbox": inbox_pub,
                                  "owner": cfg["github_owner"], "repo": cfg["github_repo"], "branch": cfg.get("github_branch") or "main",
                                  "workflow": gs.ADMIN_WORKFLOW}, pid, xfer)
        ok, detail = gs.publish_file(f"outbox/pair/{pid}.json", json.dumps(bundle), f"pairing {pid}", branch=gs.OUTBOX_BRANCH)
        if ok:
            added += 1
        else:
            print(f"[gotd] owner pair {pid}: not saved ({detail[:120]})", flush=True)
    devices["owner_pairs"] = []
    try:
        save_devices(gs, devices, f"{added} PC(s) added by your GitHub sign-in")
    except Exception as e:
        print(f"[gotd] owner pairs: devices not saved ({type(e).__name__})", flush=True)


def add_device(devices, pub_b64, label, kind, by):
    did = sec.device_id(pub_b64)
    devices["devices"] = [d for d in devices["devices"] if d["id"] != did]
    devices["devices"].append({"id": did, "pub": pub_b64, "label": re.sub(r"[^\w .·-]", "", str(label))[:30] or "device",
                               "kind": "pc" if kind == "pc" else "phone", "added": now(), "by": by})   # label sealed on save
    return did


def _real_day(day):
    """KT 341: a real calendar day ("2026-13-45" matched the old pattern and would have gone out at a random time)."""
    try:
        return re.match(r"^\d{4}-\d{2}-\d{2}$", day) is not None and dt.date.fromisoformat(day).isoformat() == day
    except ValueError:
        return False


def plan_op(gs, cfg, op, req, dev):
    plan = load_plan(gs, cfg)
    if op == "plan_list":
        return {"stage": "done", "result": sealed(plan_view(plan))}
    if op == "plan_time":
        t = str(req.get("time", ""))
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", t):
            return {"stage": "error", "error": "Choose a time like 08:30."}
        plan["time"] = t
        save_plan(gs, plan, "time")
        return {"stage": "done", "result": sealed(plan_view(plan))}
    if op == "plan_add":
        day = str(req.get("day", ""))
        items = req.get("items") or []
        if not _real_day(day) or day < local_now().date().isoformat():
            return {"stage": "error", "error": "Choose today or a later day."}
        if not items or len(items) > MAX_ITEMS:
            return {"stage": "error", "error": "Nothing to plan."}
        # check each item now (legal moves, key move, solution) without sending: one_item on a copy of the database
        for i, it in enumerate(items):
            chk = dry_check(gs, it)
            if chk:
                return {"stage": "error", "item": i, "error": chk}
        plan["items"].append({"pid": secrets.token_hex(5), "day": day, "state": "planned", "items": items,
                              "added": now(), "by": dev["id"]})
        save_plan(gs, plan, f"add {day}")
        return {"stage": "done", "result": sealed(plan_view(plan))}
    pid = str(req.get("pid", ""))
    p = next((x for x in plan["items"] if x["pid"] == pid), None)
    if not p or p.get("state") != "planned":
        return {"stage": "error", "error": "That planned item is not waiting any more."}
    if op == "plan_remove":
        plan["items"] = [x for x in plan["items"] if x is not p]
    elif op == "plan_move":
        day = str(req.get("day", ""))
        if not _real_day(day) or day < local_now().date().isoformat():
            return {"stage": "error", "error": "Choose today or a later day."}
        p["day"] = day
    else:
        return {"stage": "error", "error": f"Unknown request '{op}'."}
    save_plan(gs, plan, op)
    return {"stage": "done", "result": sealed(plan_view(plan))}


def dry_check(gs, it):
    """'' when the item would pass the send checks, else the plain reason. Nothing is sent or stored."""
    real = gs.finish, gs.send_push, gs.publish_feed, gs.publish_history, gs.backup_db
    gs.finish = lambda sid: {"sid": sid, "title": "", "steps": {}}
    try:
        r = one_item(gs, dict(it, choice=it.get("choice") or "again"), False, False)
    finally:
        gs.finish, gs.send_push, gs.publish_feed, gs.publish_history, gs.backup_db = real
        rebuild_after_dry(gs)
    if "error" in r:
        return r["error"]
    return ""


def rebuild_after_dry(gs):
    """Forget what a dry check stored: the database is rebuilt from GitHub's history again."""
    cfg = gs.load_config()
    rows = gs.remote_history_rows(cfg)
    if rows is not None:
        gs.rebuild_fresh(rows)


def run_plan(gs, cfg, problems):
    """Scheduled: send every planned item whose day + time have come (one alert for several).
    KT 332: an item sent before is featured again ("again"; a correction-looking game goes out as a different game);
    an item that cannot go out is set aside (3 tries) and the others still go - one bad item never blocks a day."""
    if os.environ.get("GITHUB_REF", "refs/heads/main") != "refs/heads/main":
        fail("not main", 0)
    try:
        plan = load_plan(gs, cfg)
    except Exception as e:
        print(f"[gotd] plan: {e}", flush=True)
        return 0
    keepalive(gs, cfg, plan)
    ensure_access(gs, cfg)
    t = local_now()
    hh, mm = [int(x) for x in plan.get("time", "08:30").split(":")]
    due = [p for p in plan["items"] if p.get("state") == "planned" and
           (p["day"] < t.date().isoformat() or (p["day"] == t.date().isoformat() and (t.hour, t.minute) >= (hh, mm)))]
    if not due:
        print("[gotd] plan: nothing due", flush=True)
        return 0
    if problems:
        print("[gotd] plan: secrets missing", flush=True)
        return 1
    wait_pc_lock(gs, cfg)
    # (entry, item index in entry, item) - the choice a "sent before" answer needs is filled in per item
    batch = [[p, k, {kk: v for kk, v in it.items() if kk != "title"}] for p in due for k, it in enumerate(p["items"])]
    res, failed = {"stage": "error", "error": "nothing to send"}, {}
    for _ in range(len(batch) * 2 + 2):
        if not batch:
            break
        err = rebuild_db(gs, cfg)
        if err:
            res = {"stage": "error", "error": err}
            break
        with gs.db() as con:                               # a try that stopped half-way left queued rows behind
            con.execute("DELETE FROM sends WHERE push_status='queued'")
        res = send_items(gs, [b[2] for b in batch], False)
        idx = res.get("item")
        if res.get("stage") == "needs_choice" and isinstance(idx, int) and 0 <= idx < len(batch):
            need = (res.get("choice") or {}).get("needs_choice")
            pick = "different" if need == "correction" else "again"
            if batch[idx][2].get("choice") == pick:          # asked again: leave it out
                failed[id(batch[idx][0])] = "needs a choice"
                batch = [b for b in batch if b[0] is not batch[idx][0]]
            else:
                batch[idx][2] = dict(batch[idx][2], choice=pick)
            continue
        if res.get("stage") == "error" and isinstance(idx, int) and 0 <= idx < len(batch):
            failed[id(batch[idx][0])] = res.get("error", "")[:200]
            batch = [b for b in batch if b[0] is not batch[idx][0]]   # set this day's entry aside, send the rest
            continue
        break
    sent_ids = {id(b[0]) for b in batch} if res.get("stage") == "done" else set()
    for p in due:
        if id(p) in sent_ids:
            p.update({"state": "sent", "sid": (res.get("result") or {}).get("sid", ""), "sent_at": now(), "error": ""})
            continue
        p["tries"] = int(p.get("tries", 0)) + 1
        p["error"] = failed.get(id(p)) or res.get("error", "")[:200]
        if p["tries"] >= 3:
            p["state"] = "failed"
    save_plan(gs, plan, "scheduled send")
    if sent_ids:
        pages_build(gs, cfg)
    print(f"[gotd] plan: {len(due)} planned item(s), {len(sent_ids)} sent -> {res.get('stage')}", flush=True)
    return 0


def keepalive(gs, cfg, plan):
    """GitHub turns scheduled runs off after 60 days without repository activity: a tiny commit every ~40 days."""
    last = str(plan.get("keepalive", ""))
    today = dt.date.today()
    try:
        if last and (today - dt.date.fromisoformat(last)).days < KEEPALIVE_DAYS:
            return
    except ValueError:
        pass
    ok, _ = gs.publish_file("gotd/keepalive.json", json.dumps({"v": 1, "at": now()}), "keep the week plan running")
    if ok:
        plan["keepalive"] = today.isoformat()
        try:
            save_plan(gs, plan, "keepalive")
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
