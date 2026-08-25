#!/usr/bin/env python3
"""
Copilot Agent Monitor
=====================
A local, read-only dashboard that watches every running GitHub Copilot CLI
session ("agent") on this machine and tells you which ones are actively
working and which ones are WAITING FOR YOU (permission prompt, an ask_user
question, or idle awaiting your next message).

It renders:
  * a live, auto-refreshing TERMINAL dashboard, and
  * a WEB dashboard (http://localhost:<port>) with the same data + browser
    notifications when an agent starts waiting on you.

It only READS local Copilot state files under ~/.copilot. It never modifies
any session, never talks to the network, and contains no third-party code.
"""

import glob
import html
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOME = os.path.expanduser("~")
COPILOT_DIR = os.path.join(HOME, ".copilot")
SESSION_STATE_DIR = os.path.join(COPILOT_DIR, "session-state")
SESSION_STORE_DB = os.path.join(COPILOT_DIR, "session-store.db")

TAIL_BYTES = 512 * 1024          # how much of events.jsonl to scan from the end
REFRESH_SECONDS = 1.0            # dashboard refresh cadence
WEB_PORT = int(os.environ.get("MONITOR_PORT", "8787"))

# ---- state constants -------------------------------------------------------
WAIT_PERMISSION = "WAITING_PERMISSION"   # blocked on an approval prompt
WAIT_QUESTION = "WAITING_QUESTION"       # blocked on an ask_user question
WAIT_INPUT = "WAITING_INPUT"             # finished its turn, awaiting your msg
WORKING = "WORKING"                      # actively running tools / thinking

WAITING_STATES = {WAIT_PERMISSION, WAIT_QUESTION, WAIT_INPUT}

# ---------------------------------------------------------------------------
# Session summary lookup (best-effort, cached)
# ---------------------------------------------------------------------------
_summary_cache = {}
_summary_cache_ts = 0.0


def load_summaries():
    """Map session_id -> (summary, cwd) from the session store (read-only)."""
    global _summary_cache, _summary_cache_ts
    now = time.time()
    if now - _summary_cache_ts < 15 and _summary_cache:
        return _summary_cache
    result = {}
    try:
        import sqlite3
        # read-only, immutable-ish connection so we never lock the writer
        uri = f"file:{SESSION_STORE_DB}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            for sid, summary, cwd in con.execute(
                "SELECT id, summary, cwd FROM sessions"
            ):
                result[sid] = (summary or "", cwd or "")
        finally:
            con.close()
    except Exception:
        pass
    _summary_cache = result
    _summary_cache_ts = now
    return result


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------
def live_pid(session_dir):
    """Return the PID holding the session lock if that process is alive."""
    for lock in glob.glob(os.path.join(session_dir, "inuse.*.lock")):
        base = os.path.basename(lock)
        try:
            pid = int(base.split(".")[1])
        except (IndexError, ValueError):
            continue
        try:
            os.kill(pid, 0)          # signal 0 == existence check
            return pid
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Terminal / TTY discovery (so we can focus the right window)
# ---------------------------------------------------------------------------
_APP_RE = re.compile(r"/([^/]+)\.app/")
_term_cache = {}  # pid -> (tty_path, app_name, kind); terminals don't move


def terminal_info(pid):
    """Return (tty_path, app_name, kind) for the terminal hosting `pid`.

    kind is one of: iterm, terminal, vscode, generic. Cached per pid.
    """
    if pid in _term_cache:
        return _term_cache[pid]
    tty_path, app_name, kind = None, None, "generic"
    try:
        tty = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2).stdout.strip()
        if tty and tty not in ("??", "?"):
            tty_path = "/dev/" + tty
    except Exception:
        pass

    # walk the parent chain looking for a GUI *.app owner
    cur = pid
    for _ in range(12):
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(cur)],
                capture_output=True, text=True, timeout=2).stdout.strip()
        except Exception:
            break
        if not out:
            break
        parts = out.split(None, 1)
        ppid = parts[0]
        comm = parts[1] if len(parts) > 1 else ""
        m = _APP_RE.search(comm)
        if m:
            app_name = m.group(1)
        if ppid in ("", "0", "1"):
            break
        cur = ppid

    if app_name:
        low = app_name.lower()
        if "iterm" in low:
            kind = "iterm"
        elif low == "terminal":
            kind = "terminal"
        elif low in ("cursor", "code", "vscodium", "electron") or "code" in low:
            kind = "vscode"
    _term_cache[pid] = (tty_path, app_name, kind)
    return tty_path, app_name, kind


def focus_terminal(tty_path, app_name, kind):
    """Bring the terminal tab/window that owns `tty_path` to the front."""
    if kind == "iterm":
        script = f'''
        tell application "iTerm2"
          repeat with w in windows
            repeat with t in tabs of w
              repeat with s in sessions of t
                if tty of s is "{tty_path}" then
                  select s
                  set index of w to 1
                  activate
                  return "ok"
                end if
              end repeat
            end repeat
          end repeat
        end tell
        return "notfound"'''
    elif kind == "terminal":
        script = f'''
        tell application "Terminal"
          repeat with w in windows
            repeat with t in tabs of w
              if tty of t is "{tty_path}" then
                set selected of t to true
                set frontmost of w to true
                activate
                return "ok"
              end if
            end repeat
          end repeat
        end tell
        return "notfound"'''
    elif app_name:
        script = f'tell application "{app_name}" to activate\nreturn "activated"'
    else:
        return False, "Unknown terminal — could not focus."
    try:
        res = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=6)
        out = (res.stdout or "").strip()
        if out == "ok":
            return True, f"Focused {app_name} tab ({tty_path})."
        if out == "activated":
            return True, (f"Activated {app_name}. Look for the tab on "
                          f"{tty_path} (VS Code/Cursor can't auto-select tabs).")
        if out == "notfound":
            return False, (f"Activated {app_name} but couldn't find the tab for "
                           f"{tty_path}; it may be in another window.")
        return False, (res.stderr or "osascript failed").strip()[:200]
    except Exception as e:
        return False, f"focus error: {e}"


# The command sent to auto-approve a blocked agent in FAST & FURIOUS mode.
# /allow-all is Copilot CLI's built-in "enable all permissions" command.
APPROVE_CMD = "/allow-all"


def autopilot_terminal(tty_path, app_name, kind, cmd=APPROVE_CMD):
    """Type `cmd` + Enter into the exact terminal session that owns tty_path.

    iTerm2 supports `write text` which targets one session precisely (no risk of
    hitting the wrong window). Terminal.app falls back to focus + System Events
    keystroke. VS Code/Cursor integrated terminals can't be targeted safely, so
    they are refused.
    """
    safe = cmd.replace('\\', '\\\\').replace('"', '\\"')
    if kind == "iterm":
        script = f'''
        tell application "iTerm2"
          repeat with w in windows
            repeat with t in tabs of w
              repeat with s in sessions of t
                if tty of s is "{tty_path}" then
                  tell s to write text "{safe}"
                  return "ok"
                end if
              end repeat
            end repeat
          end repeat
        end tell
        return "notfound"'''
    elif kind == "terminal":
        script = f'''
        tell application "Terminal"
          repeat with w in windows
            repeat with t in tabs of w
              if tty of t is "{tty_path}" then
                set selected of t to true
                set frontmost of w to true
                activate
                delay 0.15
                tell application "System Events"
                  keystroke "{safe}"
                  key code 36
                end tell
                return "ok"
              end if
            end repeat
          end repeat
        end tell
        return "notfound"'''
    else:
        return False, (f"Auto-approve only supports iTerm/Terminal; "
                       f"{app_name or 'this terminal'} must be approved manually.")
    try:
        res = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=6)
        out = (res.stdout or "").strip()
        if out == "ok":
            return True, f"Sent '{cmd}' to {app_name} ({tty_path})."
        if out == "notfound":
            return False, f"Couldn't find the {app_name} tab for {tty_path}."
        return False, (res.stderr or "osascript failed").strip()[:200]
    except Exception as e:
        return False, f"autopilot error: {e}"


def kill_agent(pid, force=False):
    """Send SIGTERM (or SIGKILL) to an agent process."""
    try:
        os.kill(int(pid), signal.SIGKILL if force else signal.SIGTERM)
        return True, f"Sent {'SIGKILL' if force else 'SIGTERM'} to pid {pid}."
    except ProcessLookupError:
        return False, f"pid {pid} not found (already exited)."
    except PermissionError:
        return False, f"Not permitted to kill pid {pid}."
    except Exception as e:
        return False, f"kill error: {e}"


# ---------------------------------------------------------------------------
# Event tail parsing
# ---------------------------------------------------------------------------
def tail_events(path, nbytes=TAIL_BYTES):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
                f.readline()          # discard partial first line
            data = f.read()
    except OSError:
        return []
    events = []
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            events.append(json.loads(raw))
        except Exception:
            continue
    return events


def _iso_to_epoch(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def analyze(events):
    """Derive the current agent state from a tail of its event stream."""
    pending_perm = {}     # requestId -> permission info
    pending_ask = {}      # toolCallId -> question text
    last_state_evt = None
    last_tool = None
    last_assistant_msg = None
    last_ts = None

    STATE_EVENTS = {
        "user.message", "assistant.turn_start", "assistant.turn_end",
        "assistant.message", "tool.execution_start", "tool.execution_complete",
    }

    for e in events:
        t = e.get("type")
        d = e.get("data") or {}
        ts = _iso_to_epoch(e.get("timestamp"))
        if ts:
            last_ts = ts

        if t == "permission.requested":
            pr = d.get("permissionRequest") or {}
            pending_perm[d.get("requestId")] = {
                "intention": pr.get("intention") or "",
                "kind": pr.get("kind") or "",
                "command": (pr.get("fullCommandText") or "")[:400],
            }
        elif t == "permission.completed":
            pending_perm.pop(d.get("requestId"), None)
        elif t == "tool.execution_start":
            name = d.get("toolName")
            last_tool = name
            if name == "ask_user":
                args = d.get("arguments") or {}
                pending_ask[d.get("toolCallId")] = args.get("question") or "(question)"
        elif t == "tool.execution_complete":
            pending_ask.pop(d.get("toolCallId"), None)
        elif t == "assistant.message":
            content = d.get("content") or d.get("text") or ""
            if isinstance(content, str) and content.strip():
                last_assistant_msg = content.strip()

        if t in STATE_EVENTS:
            last_state_evt = t

    # ---- decide, most-urgent first ----
    if pending_perm:
        info = list(pending_perm.values())[-1]
        detail = info["intention"] or info["command"] or info["kind"] or "approval needed"
        return WAIT_PERMISSION, detail, last_tool, last_ts
    if pending_ask:
        return WAIT_QUESTION, list(pending_ask.values())[-1], "ask_user", last_ts
    if last_state_evt == "assistant.turn_end":
        snippet = (last_assistant_msg or "")[:200]
        return WAIT_INPUT, snippet or "turn finished — awaiting your message", last_tool, last_ts
    # otherwise actively working
    detail = f"running {last_tool}" if last_tool else "thinking…"
    return WORKING, detail, last_tool, last_ts


def _oneline(s):
    return " ".join((s or "").split())


def _tool_hint(name, args):
    args = args or {}
    if name == "bash":
        return _oneline(args.get("command", ""))[:160]
    if name in ("view", "create", "edit"):
        return args.get("path", "")
    if name in ("read_bash", "stop_bash"):
        return f"shell {args.get('shellId', '')}"
    if name == "grep":
        return f"/{args.get('pattern', '')}/"
    if name == "glob":
        return args.get("pattern", "")
    if name == "tool_search_tool":
        return args.get("pattern", "")
    try:
        return json.dumps(args)[:120]
    except Exception:
        return ""


def build_log(session_id, n=25, nbytes=256 * 1024):
    """Return the last N human-readable activity lines for a session."""
    path = os.path.join(SESSION_STATE_DIR, session_id, "events.jsonl")
    if not os.path.exists(path):
        return []
    lines = []
    idx_by_call = {}
    for e in tail_events(path, nbytes):
        t = e.get("type")
        d = e.get("data") or {}
        ts = (e.get("timestamp") or "")[11:19]
        if t == "user.message":
            c = _oneline(d.get("content") or "")
            if c:
                lines.append(f"{ts} 🧑 you: {c[:220]}")
        elif t == "assistant.message":
            c = _oneline(d.get("content") or d.get("text") or "")
            if c:
                lines.append(f"{ts} 🤖 {c[:220]}")
        elif t == "tool.execution_start":
            name = d.get("toolName")
            hint = _tool_hint(name, d.get("arguments"))
            lines.append(f"{ts} → {name}: {hint}" if hint else f"{ts} → {name}")
            idx_by_call[d.get("toolCallId")] = len(lines) - 1
        elif t == "tool.execution_complete":
            i = idx_by_call.get(d.get("toolCallId"))
            ok = d.get("success")
            if i is not None:
                lines[i] += "   ✓" if ok else "   ✗"
                if not ok:
                    err = _oneline((d.get("error") or {}).get("message") or "")
                    if err:
                        lines[i] += f" — {err[:140]}"
        elif t == "permission.requested":
            pr = d.get("permissionRequest") or {}
            lines.append(f"{ts} ⛔ needs approval: "
                         f"{_oneline(pr.get('intention') or pr.get('kind') or '')[:140]}")
        elif t == "permission.completed":
            res = (d.get("result") or {}).get("kind")
            lines.append(f"{ts} 🔓 permission {res}")
        elif t == "assistant.turn_end":
            lines.append(f"{ts} ⏹ turn complete")
    return lines[-n:]


def scan():
    """Return a list of agent status dicts for all live sessions."""
    summaries = load_summaries()
    agents = []
    if not os.path.isdir(SESSION_STATE_DIR):
        return agents
    for sid in os.listdir(SESSION_STATE_DIR):
        sdir = os.path.join(SESSION_STATE_DIR, sid)
        if not os.path.isdir(sdir):
            continue
        pid = live_pid(sdir)
        if pid is None:
            continue  # only report live/working agents
        evpath = os.path.join(sdir, "events.jsonl")
        if not os.path.exists(evpath):
            continue
        state, detail, tool, last_ts = analyze(tail_events(evpath))
        summary, cwd = summaries.get(sid, ("", ""))
        idle_for = (time.time() - last_ts) if last_ts else None
        tty_path, term_app, term_kind = terminal_info(pid)
        if state == WORKING:
            category, waiting_for = "processing", None
        elif state == WAIT_INPUT:
            category, waiting_for = "done", None
        else:  # WAIT_PERMISSION / WAIT_QUESTION -> blocked mid-task on a human
            category, waiting_for = "blocked", detail
        agents.append({
            "id": sid,
            "short": sid[:8],
            "pid": pid,
            "summary": summary or "(untitled session)",
            "cwd": cwd,
            "state": state,
            "category": category,
            "waiting": state in WAITING_STATES,
            "waiting_for": waiting_for,
            "detail": detail,
            "tool": tool,
            "idle_seconds": round(idle_for) if idle_for is not None else None,
            "last_ts": last_ts,
            "tty": tty_path,
            "term_app": term_app,
            "term_kind": term_kind,
        })
    # waiting agents first, then by most recently active
    agents.sort(key=lambda a: (not a["waiting"], -(a["last_ts"] or 0)))
    return agents


# ---------------------------------------------------------------------------
# Terminal rendering
# ---------------------------------------------------------------------------
class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    RED = "\033[91m"; YEL = "\033[93m"; GRN = "\033[92m"; CYN = "\033[96m"
    MAG = "\033[95m"; BG_RED = "\033[41m\033[97m"; BG_GRN = "\033[42m\033[30m"


# animated frames advanced once per render
GEAR_FRAMES = ["◴", "◷", "◶", "◵"]           # rotating "wheel" for processing
SPIN_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_frame = {"i": 0}


def _fmt_age(sec):
    if sec is None:
        return "?"
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def render_terminal(agents, prev_waiting_ids):
    lines = []
    now = datetime.now().strftime("%H:%M:%S")
    waiting = [a for a in agents if a["waiting"]]
    header = f"{C.BOLD}{C.CYN}Copilot Agent Monitor{C.RESET}  {C.DIM}{now}{C.RESET}"
    stats = (f"  agents: {len(agents)}   "
             f"{C.RED}waiting: {len(waiting)}{C.RESET}   "
             f"web: http://localhost:{WEB_PORT}")
    lines.append(header + stats)
    lines.append(C.DIM + "─" * 78 + C.RESET)

    if not agents:
        lines.append(C.DIM + "  No live Copilot CLI sessions found." + C.RESET)

    _frame["i"] = (_frame["i"] + 1) % 10000
    gear = GEAR_FRAMES[_frame["i"] % len(GEAR_FRAMES)]
    spin = SPIN_FRAMES[_frame["i"] % len(SPIN_FRAMES)]

    for a in agents:
        cat = a.get("category")
        title = a["summary"][:44]
        if cat == "processing":
            head = (f"{C.GRN}{gear} {spin}  PROCESSING     {C.RESET} "
                    f"{C.BOLD}{title}{C.RESET}")
        elif cat == "blocked":
            head = (f"{C.BG_RED} ✋ BLOCKED — NEEDS YOU {C.RESET} "
                    f"{C.BOLD}{title}{C.RESET}")
        else:  # done / idle
            head = (f"{C.BG_GRN} ✅ DONE — NEXT? {C.RESET} "
                    f"{C.BOLD}{title}{C.RESET}")
        lines.append(head + f" {C.DIM}[{a['short']} pid {a['pid']}]{C.RESET}")

        if cat == "blocked":
            wf = (a.get("waiting_for") or "").replace("\n", " ⏎ ")[:70]
            kind = "approve command" if a["state"] == WAIT_PERMISSION else "answer question"
            lines.append(f"    {C.RED}{C.BOLD}⏳ waiting for you to {kind}:{C.RESET} "
                         f"{C.YEL}{wf}{C.RESET}")
        elif cat == "processing":
            detail = (a["detail"] or "").replace("\n", " ⏎ ")[:66]
            lines.append(f"    {C.DIM}{gear} {detail}   ·   running {_fmt_age(a['idle_seconds'])}{C.RESET}")
        else:
            snip = (a["detail"] or "").replace("\n", " ⏎ ")[:66]
            lines.append(f"    {C.GRN}✓ finished{C.RESET} {C.DIM}· idle {_fmt_age(a['idle_seconds'])} · last: {snip}{C.RESET}")
        loc = f"{a.get('term_app') or '?'} · {a.get('tty') or '?'}"
        lines.append(f"    {C.DIM}  ↳ {loc}{C.RESET}")
        lines.append("")

    # clear screen + home, then paint
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.write("\n".join(lines) + "\n")

    # ring the bell if a NEW agent started waiting
    new_waiting = {a["id"] for a in waiting} - prev_waiting_ids
    if new_waiting:
        sys.stdout.write("\a")  # terminal bell
        for a in waiting:
            if a["id"] in new_waiting:
                sys.stdout.write(
                    f"{C.BG_RED} ⚠  {a['summary'][:40]} needs you: "
                    f"{(a['detail'] or '')[:50]} {C.RESET}\n")
    sys.stdout.flush()
    return {a["id"] for a in waiting}


# ---------------------------------------------------------------------------
# Web dashboard
# ---------------------------------------------------------------------------
_latest = {"agents": [], "ts": 0}
_latest_lock = threading.Lock()

INDEX_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>J.A.R.V.I.S. · Copilot Agent Monitor</title>
<style>
 :root{--cy:#22d3ee;--cy2:#7df9ff;--gold:#ffb020;--red:#ff3b47;--grn:#39ffa8;--muted:#7fa7b3;--panel:rgba(8,20,30,.55)}
 *{box-sizing:border-box}
 html,body{height:100%}
 body{font-family:ui-monospace,Menlo,Consolas,monospace;background:#02060c;color:#cdeaf2;margin:0;padding:22px;min-height:100vh;overflow-x:hidden}
 /* layered HUD background */
 #fx{position:fixed;inset:0;z-index:-3}
 body::before{content:'';position:fixed;inset:0;z-index:-2;background:
   linear-gradient(rgba(34,211,238,.05) 1px,transparent 1px) 0 0/100% 34px,
   linear-gradient(90deg,rgba(34,211,238,.05) 1px,transparent 1px) 0 0/34px 100%,
   radial-gradient(900px 500px at 50% -10%,rgba(34,211,238,.10),transparent 60%);pointer-events:none}
 .sweep{position:fixed;top:50%;left:50%;width:180vmax;height:180vmax;transform:translate(-50%,-50%);z-index:-2;
   background:conic-gradient(from 0deg,rgba(34,211,238,.12),transparent 25%,transparent 75%,rgba(34,211,238,.06));
   animation:sweep 14s linear infinite;pointer-events:none;opacity:.5}
 @keyframes sweep{to{transform:translate(-50%,-50%) rotate(360deg)}}
 .scan{position:fixed;left:0;right:0;height:120px;z-index:-1;pointer-events:none;
   background:linear-gradient(180deg,transparent,rgba(34,211,238,.06),transparent);animation:scan 6s linear infinite}
 @keyframes scan{0%{top:-120px}100%{top:100%}}
 /* header */
 header{display:flex;align-items:center;gap:16px;margin-bottom:16px}
 .reactor-lg{position:relative;width:58px;height:58px;flex:0 0 58px}
 .reactor-lg .r,.reactor .r{position:absolute;inset:0;border-radius:50%;border:2px solid transparent}
 .reactor-lg .r1{border-top-color:var(--cy);border-right-color:var(--cy);animation:spin 2.4s linear infinite;box-shadow:0 0 14px rgba(34,211,238,.6)}
 .reactor-lg .r2{inset:8px;border-bottom-color:var(--cy2);border-left-color:var(--cy2);animation:spin 3.4s linear infinite reverse}
 .reactor-lg .r3{inset:16px;border-top-color:#eafeff;animation:spin 1.4s linear infinite}
 .reactor-lg .core,.reactor .core{position:absolute;border-radius:50%;background:radial-gradient(circle,#eafeff,#22d3ee 55%,transparent 74%);box-shadow:0 0 22px #22d3ee,0 0 48px rgba(34,211,238,.55);animation:coreP 1.5s ease-in-out infinite}
 .reactor-lg .core{inset:22px}
 @keyframes spin{to{transform:rotate(360deg)}}
 @keyframes coreP{0%,100%{opacity:.8;transform:scale(.9)}50%{opacity:1;transform:scale(1.1)}}
 h1{font-size:20px;margin:0;letter-spacing:4px;color:#e8fbff;text-shadow:0 0 12px rgba(34,211,238,.8)}
 h1 b{color:var(--cy)}
 .tag{font-size:10px;letter-spacing:3px;color:var(--muted);margin-top:3px}
 .clock{margin-left:auto;text-align:right;font-size:12px;letter-spacing:2px;color:var(--cy);text-shadow:0 0 8px rgba(34,211,238,.6)}
 .clock small{display:block;color:var(--muted);letter-spacing:3px;font-size:9px}
 /* status chips */
 .chips{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
 /* top control bar */
 .controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 16px;padding:10px 12px;
   background:rgba(6,16,24,.5);border:1px solid rgba(34,211,238,.2);border-radius:8px;backdrop-filter:blur(4px)}
 .ctl{font-family:inherit;font-size:11px;font-weight:700;letter-spacing:1.5px;color:var(--cy2);background:rgba(2,8,14,.8);
   border:1px solid rgba(34,211,238,.4);border-radius:6px;padding:7px 13px;cursor:pointer;transition:.12s}
 .ctl:hover{background:rgba(34,211,238,.16)}
 #liveBtn.live{color:#02060c;background:var(--grn);border-color:var(--grn);box-shadow:0 0 12px rgba(57,255,168,.5)}
 #liveBtn.paused{color:var(--gold);border-color:var(--gold);background:rgba(255,176,32,.1)}
 #fxBtn.off{color:var(--muted);border-color:rgba(127,167,179,.4);background:rgba(127,167,179,.06)}
 /* fast&furious sliding switch */
 .ffswitch{display:inline-flex;align-items:center;gap:9px;cursor:pointer;user-select:none;
   padding:5px 11px;border:1px solid rgba(34,211,238,.3);border-radius:8px;background:rgba(2,8,14,.7)}
 .ffside{font-size:10.5px;font-weight:700;letter-spacing:1px;color:var(--muted);transition:.15s;white-space:nowrap}
 .fftrack{position:relative;width:46px;height:22px;border-radius:22px;background:rgba(34,211,238,.18);
   border:1px solid rgba(34,211,238,.5);transition:.2s}
 .ffknob{position:absolute;top:1px;left:1px;width:18px;height:18px;border-radius:50%;
   background:var(--cy2);box-shadow:0 0 8px rgba(34,211,238,.7);transition:.2s}
 /* MANUAL (off) state */
 .ffswitch.manual .ffside.l{color:var(--cy2)}
 /* FAST & FURIOUS (on) state */
 .ffswitch.ff .fftrack{background:linear-gradient(90deg,#ff3b30,#ff8a00);border-color:#ff8a00;box-shadow:0 0 12px rgba(255,90,0,.6)}
 .ffswitch.ff .ffknob{left:25px;background:#fff;box-shadow:0 0 10px rgba(255,140,0,.9)}
 .ffswitch.ff .ffside.r{color:#ff8a00;text-shadow:0 0 8px rgba(255,90,0,.5)}
 .ffswitch.ff{border-color:#ff8a00;animation:ffpulse 1.4s ease-in-out infinite}
 @keyframes ffpulse{0%,100%{box-shadow:0 0 6px rgba(255,80,0,.25)}50%{box-shadow:0 0 16px rgba(255,120,0,.6)}}
 .ctlgrp{display:flex;align-items:center;gap:5px}
 .ctllbl{font-size:10px;letter-spacing:2px;color:var(--muted)}
 .ctlgrp.dim{opacity:.4;pointer-events:none}
 .segb2{font-family:inherit;font-size:10px;font-weight:700;letter-spacing:1px;color:var(--muted);background:rgba(2,8,14,.8);
   border:1px solid rgba(34,211,238,.3);border-radius:5px;padding:5px 10px;cursor:pointer;transition:.12s}
 .segb2:hover{color:var(--cy2);border-color:var(--cy)}
 .segb2.on{color:#02060c;background:var(--cy);border-color:var(--cy);font-weight:800}
 .ctlhint{margin-left:auto;font-size:10.5px;letter-spacing:1px;color:var(--muted)}
 body.nofx #fx,body.nofx .sweep,body.nofx .scan{display:none}
 body.nofx .reactor-lg .r,body.nofx .reactor .r,body.nofx .reactor-lg .core,body.nofx .reactor .core,
 body.nofx .panel .rule,body.nofx .warn,body.nofx .doneicon,body.nofx .bar i{animation:none!important}
 .chip{display:inline-flex;align-items:center;gap:7px;font-size:11px;font-weight:700;letter-spacing:1.5px;padding:6px 12px;border-radius:4px;background:var(--panel);backdrop-filter:blur(4px);position:relative}
 .chip::before,.chip::after{content:'';position:absolute;width:7px;height:7px;border:1px solid currentColor;opacity:.7}
 .chip::before{top:2px;left:2px;border-right:0;border-bottom:0}
 .chip::after{bottom:2px;right:2px;border-left:0;border-top:0}
 .chip.p{color:var(--cy);box-shadow:0 0 10px rgba(34,211,238,.25) inset}
 .chip.b{color:var(--red);box-shadow:0 0 10px rgba(255,59,71,.3) inset}
 .chip.d{color:var(--gold);box-shadow:0 0 10px rgba(255,176,32,.25) inset}
 .chip .n{font-size:15px}
 /* grid + panels */
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:18px}
 /* three-lane bifurcation */
 #grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;align-items:start}
 @media(max-width:1150px){#grid{grid-template-columns:1fr}}
 .lane{background:rgba(6,16,24,.4);border:1px solid rgba(34,211,238,.15);border-radius:8px;padding:12px;backdrop-filter:blur(3px)}
 .lane-head{display:flex;align-items:center;gap:9px;font-size:12px;font-weight:800;letter-spacing:2px;padding:6px 6px 12px;border-bottom:1px solid rgba(127,167,179,.2);margin-bottom:14px}
 .lane-ic{font-size:16px}
 .lane-ct{margin-left:auto;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:800}
 .lane-p .lane-head{color:var(--cy)}.lane-p .lane-ct{background:rgba(34,211,238,.16)}
 .lane-b .lane-head{color:var(--red)}.lane-b{border-color:rgba(255,59,71,.3)}.lane-b .lane-ct{background:rgba(255,59,71,.2)}
 .lane-d .lane-head{color:var(--gold)}.lane-d{border-color:rgba(255,176,32,.25)}.lane-d .lane-ct{background:rgba(255,176,32,.2)}
 .lane-body{display:flex;flex-direction:column;gap:14px}
 .lane-empty{color:var(--muted);font-size:11px;letter-spacing:1.5px;padding:14px;text-align:center;opacity:.55;border:1px dashed rgba(127,167,179,.2);border-radius:6px}
 .panel{position:relative;background:var(--panel);backdrop-filter:blur(6px);border:1px solid rgba(34,211,238,.35);
   border-radius:6px;padding:16px 18px;--edge:var(--cy);box-shadow:0 0 18px rgba(34,211,238,.12),0 0 0 1px rgba(34,211,238,.08) inset;
   transition:transform .15s,box-shadow .25s;overflow:hidden}
 .panel:hover{transform:translateY(-3px);box-shadow:0 0 28px rgba(34,211,238,.28)}
 .panel::before,.panel::after{content:'';position:absolute;width:16px;height:16px;border:2px solid var(--edge);opacity:.9}
 .panel::before{top:6px;left:6px;border-right:0;border-bottom:0}
 .panel::after{bottom:6px;right:6px;border-left:0;border-top:0}
 .panel .rule{position:absolute;top:0;left:0;width:100%;height:2px;background:linear-gradient(90deg,transparent,var(--edge),transparent);opacity:.7;animation:rule 3.5s linear infinite}
 @keyframes rule{0%{opacity:.2}50%{opacity:.8}100%{opacity:.2}}
 .panel.processing{--edge:var(--cy)}
 .panel.blocked{--edge:var(--red);border-color:rgba(255,59,71,.6);box-shadow:0 0 22px rgba(255,59,71,.35);animation:alert 1.3s infinite}
 @keyframes alert{0%{box-shadow:0 0 0 0 rgba(255,59,71,.5),0 0 22px rgba(255,59,71,.35)}70%{box-shadow:0 0 0 12px rgba(255,59,71,0),0 0 22px rgba(255,59,71,.15)}100%{box-shadow:0 0 0 0 rgba(255,59,71,0),0 0 22px rgba(255,59,71,.35)}}
 .panel.done{--edge:var(--gold);border-color:rgba(255,176,32,.5);box-shadow:0 0 18px rgba(255,176,32,.16)}
 .head{display:flex;align-items:center;gap:14px}
 /* small reactor for processing cards */
 .reactor{position:relative;width:44px;height:44px;flex:0 0 44px}
 .reactor .r1{border-top-color:var(--cy);border-right-color:var(--cy);animation:spin 1.6s linear infinite;box-shadow:0 0 10px rgba(34,211,238,.6)}
 .reactor .r2{inset:6px;border-bottom-color:var(--cy2);border-left-color:var(--cy2);animation:spin 2.2s linear infinite reverse}
 .reactor .r3{inset:12px;border-top-color:#eafeff;animation:spin 1s linear infinite}
 .reactor .core{inset:16px}
 .warn{width:44px;height:44px;flex:0 0 44px;display:grid;place-items:center;font-size:28px;color:var(--red);filter:drop-shadow(0 0 8px rgba(255,59,71,.9));animation:shake .9s ease-in-out infinite}
 @keyframes shake{0%,100%{transform:translateX(0) rotate(0)}25%{transform:translateX(-2px) rotate(-5deg)}75%{transform:translateX(2px) rotate(5deg)}}
 .doneicon{width:44px;height:44px;flex:0 0 44px;display:grid;place-items:center;font-size:26px;color:var(--gold);filter:drop-shadow(0 0 8px rgba(255,176,32,.7));animation:bob 2.4s ease-in-out infinite}
 @keyframes bob{0%,100%{transform:translateY(0)}50%{transform:translateY(-3px)}}
 .title{font-size:14px;font-weight:700;letter-spacing:1px;color:#eafbff}
 .stat{font-size:10.5px;font-weight:700;letter-spacing:2px;margin-top:4px}
 .stat.processing{color:var(--cy);text-shadow:0 0 8px rgba(34,211,238,.6)}
 .stat.blocked{color:var(--red);text-shadow:0 0 8px rgba(255,59,71,.7)}
 .stat.done{color:var(--gold);text-shadow:0 0 8px rgba(255,176,32,.6)}
 .dots{display:inline-block;width:20px;text-align:left}
 .detail{margin-top:12px;color:#a9cdd8;font-size:12.5px;white-space:pre-wrap;word-break:break-word;max-height:92px;overflow:auto;line-height:1.5}
 .bar{margin-top:10px;height:4px;border-radius:3px;background:rgba(34,211,238,.12);overflow:hidden}
 .bar i{display:block;height:100%;width:38%;background:linear-gradient(90deg,transparent,var(--cy),transparent);animation:load 1.4s linear infinite}
 @keyframes load{0%{transform:translateX(-120%)}100%{transform:translateX(320%)}}
 .waitbox{margin-top:12px;border:1px solid rgba(255,59,71,.5);border-radius:5px;padding:10px 12px;position:relative;
   background:repeating-linear-gradient(45deg,rgba(255,59,71,.06),rgba(255,59,71,.06) 10px,rgba(255,59,71,.12) 10px,rgba(255,59,71,.12) 20px)}
 .waitbox .lbl{font-size:10.5px;font-weight:800;color:var(--red);letter-spacing:1.5px;margin-bottom:5px;text-shadow:0 0 6px rgba(255,59,71,.6)}
 .waitbox .q{font-size:12.5px;color:#ffe0e1;white-space:pre-wrap;word-break:break-word;max-height:120px;overflow:auto;line-height:1.5}
 .meta{display:flex;flex-wrap:wrap;gap:10px;color:var(--muted);font-size:10.5px;letter-spacing:1px;margin-top:12px}
 .meta span{padding:2px 6px;border:1px solid rgba(127,167,179,.25);border-radius:3px}
 .path{color:#5f8794;font-size:10.5px;margin-top:6px;word-break:break-all}
 .actions{margin-top:14px;display:flex;gap:10px}
 .btn{font-family:inherit;font-size:11px;font-weight:700;letter-spacing:1.5px;border:1px solid var(--cy);background:rgba(34,211,238,.08);
   color:var(--cy2);padding:7px 12px;border-radius:4px;cursor:pointer;transition:.14s;text-shadow:0 0 6px rgba(34,211,238,.5)}
 .btn:hover{background:rgba(34,211,238,.2);box-shadow:0 0 12px rgba(34,211,238,.5)}
 .btn.kill{border-color:var(--red);color:#ff9aa0;background:rgba(255,59,71,.08);text-shadow:0 0 6px rgba(255,59,71,.5)}
 .btn.kill:hover{background:rgba(255,59,71,.2);box-shadow:0 0 12px rgba(255,59,71,.5)}
 .btn.log{border-color:rgba(127,167,179,.55);color:#bfe9f2;background:rgba(127,167,179,.08);text-shadow:none}
 .btn.log:hover{background:rgba(127,167,179,.2);box-shadow:0 0 10px rgba(127,167,179,.3)}
 .logwrap{margin-top:12px;max-height:280px;overflow:auto;border:1px solid rgba(34,211,238,.28);border-radius:5px;
   background:rgba(1,6,11,.75);box-shadow:0 0 12px rgba(34,211,238,.12) inset}
 .logwrap::-webkit-scrollbar{width:8px}.logwrap::-webkit-scrollbar-thumb{background:rgba(34,211,238,.3);border-radius:4px}
 .logbox{margin:0;padding:10px 12px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;line-height:1.6;
   color:#bfe9f2;white-space:pre-wrap;word-break:break-word}
 .logbar{display:flex;align-items:center;gap:8px;padding:7px 10px;border-bottom:1px solid rgba(34,211,238,.2);
   font-size:10px;letter-spacing:1.5px;color:var(--cy);background:rgba(34,211,238,.06);position:sticky;top:0;backdrop-filter:blur(4px)}
 .logsel{margin-left:auto;font-family:inherit;font-size:10px;letter-spacing:1px;color:var(--cy2);background:rgba(2,8,14,.9);
   border:1px solid rgba(34,211,238,.4);border-radius:4px;padding:3px 6px;cursor:pointer}
 .logseg{margin-left:auto;display:flex;gap:4px}
 .segb{font-family:inherit;font-size:10px;letter-spacing:1px;color:var(--muted);background:rgba(2,8,14,.8);
   border:1px solid rgba(34,211,238,.3);border-radius:4px;padding:3px 9px;cursor:pointer;transition:.12s}
 .segb:hover{color:var(--cy2);border-color:var(--cy)}
 .segb.on{color:#02060c;background:var(--cy);border-color:var(--cy);font-weight:800;text-shadow:none}
 .toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:rgba(8,20,30,.9);border:1px solid var(--cy);
   color:#eafbff;padding:11px 18px;border-radius:5px;font-size:12.5px;letter-spacing:1px;opacity:0;transition:opacity .2s;pointer-events:none;
   box-shadow:0 0 24px rgba(34,211,238,.4)}
 .toast.show{opacity:1}
 .empty{color:var(--muted);letter-spacing:2px}
 #banner{display:none;background:linear-gradient(90deg,rgba(255,59,71,.25),rgba(255,59,71,.5),rgba(255,59,71,.25));
   border:1px solid var(--red);color:#ffe3e4;font-weight:800;letter-spacing:2px;padding:12px 16px;border-radius:5px;margin-bottom:16px;
   text-shadow:0 0 8px rgba(255,59,71,.7);animation:alert 1.3s infinite}
</style></head><body>
<canvas id=fx></canvas>
<div class=sweep></div><div class=scan></div>
<header>
 <div class=reactor-lg><span class="r r1"></span><span class="r r2"></span><span class="r r3"></span><span class=core></span></div>
 <div><h1>J.A.R.V.I.S. <b>//</b> AGENT MONITOR</h1><div class=tag>COPILOT CLI · REAL-TIME AGENT TELEMETRY</div></div>
 <div class=clock id=clock>--:--:--<small>SYSTEMS ONLINE</small></div>
</header>
<div class=controls>
 <button id=liveBtn class=ctl></button>
 <span class=ctlgrp id=intgrp>
  <span class=ctllbl>EVERY</span>
  <button class=segb2 data-int="1000">1s</button>
  <button class=segb2 data-int="3000">3s</button>
  <button class=segb2 data-int="10000">10s</button>
 </span>
 <button id=refreshBtn class=ctl>\u27F3 REFRESH NOW</button>
 <button id=fxBtn class=ctl>\u2728 FX</button>
 <span class=ffswitch id=ffSwitch>
  <span class="ffside l">\U0001F6E1 MANUAL</span>
  <span class=fftrack><span class=ffknob></span></span>
  <span class="ffside r">\U0001F3CE FAST &amp; FURIOUS</span>
 </span>
 <span class=ctlhint id=ctlhint></span>
</div>
<div class=chips id=chips></div>
<div id=banner></div>
<div class=grid id=grid></div>
<div class=toast id=toast></div>
<script>
const DOTS=['','.','..','...'];let frame=0;
let prevDone=new Set();let started=false;let chimeAt=new Map();const REPEAT_MS=20000;
const expanded=new Set();const logN=new Map();
function logOpts(id){const v=logN.get(id)||'25';
 return ['25','50','full'].map(o=>'<button class="segb'+(o===v?' on':'')+'" data-id="'+id+'" data-n="'+o+'">'+(o==='full'?'FULL':o)+'</button>').join('');}
function fmtAge(s){if(s==null)return'?';if(s<60)return s+'s';if(s<3600)return((s/60)|0)+'m';return((s/3600)|0)+'h';}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),3500);}
async function focusAgent(id){try{const r=await(await fetch('/api/focus?id='+encodeURIComponent(id))).json();toast(r.message||(r.ok?'focused':'could not focus'));}catch(e){toast('focus failed');}}
async function killAgent(id,name){if(!confirm('TERMINATE AGENT?\\n\\n'+name+'\\n\\nSends SIGTERM to its process.'))return;
 try{const r=await(await fetch('/api/kill?id='+encodeURIComponent(id))).json();toast(r.message||(r.ok?'terminated':'could not terminate'));setTimeout(tick,400);}catch(e){toast('kill failed');}}
function card(x){
 const loc=(x.term_app||'?')+' · '+(x.tty||'?');
 const dots=DOTS[frame%4];
 let icon,cls,statetxt,body;
 if(x.category==='processing'){cls='processing';
  icon='<div class=reactor><span class="r r1"></span><span class="r r2"></span><span class="r r3"></span><span class=core></span></div>';
  statetxt='PROCESSING<span class=dots>'+dots+'</span>';
  body='<div class=detail>'+esc(x.detail||'')+'</div><div class=bar><i></i></div>';
 }else if(x.category==='blocked'){cls='blocked';
  icon='<div class=warn>\u26A0</div>';
  statetxt='BLOCKED \u00B7 AWAITING YOUR RESPONSE';
  const lbl=x.state==='WAITING_PERMISSION'?'AUTHORIZATION REQUIRED \u2014 approve command':'INPUT REQUIRED \u2014 answer question';
  body='<div class=waitbox><div class=lbl>\u23F3 '+lbl+'</div><div class=q>'+esc(x.waiting_for||x.detail||'')+'</div></div>';
 }else{cls='done';
  icon='<div class=doneicon>\u2714</div>';
  statetxt='STANDBY \u00B7 AWAITING NEXT DIRECTIVE';
  body='<div class=detail>\u2714 turn complete \u2014 last transmission: '+esc((x.detail||'').slice(0,240))+'</div>';
 }
 return '<div class="panel '+cls+'"><div class=rule></div>'+
  '<div class=head>'+icon+'<div><div class=title>'+esc(x.summary)+'</div><div class="stat '+cls+'">'+statetxt+'</div></div></div>'+
  body+
  '<div class=meta><span>ID '+x.short+'</span><span>PID '+x.pid+'</span><span>'+(x.category==='processing'?'RUN ':'IDLE ')+fmtAge(x.idle_seconds)+'</span><span>'+esc(loc)+'</span></div>'+
  '<div class=path>'+esc(x.cwd||'')+'</div>'+
  '<div class=actions><button class="btn focus" data-id="'+x.id+'">\u25A3 FOCUS</button>'+
  '<button class="btn log" data-id="'+x.id+'">'+(expanded.has(x.id)?'\u25BE HIDE LOG':'\u25B8 SHOW LOG')+'</button>'+
  '<button class="btn kill" data-id="'+x.id+'" data-name="'+esc(x.summary)+'">\u2715 KILL</button></div>'+
  '<div class=logwrap id="log-'+x.id+'"'+(expanded.has(x.id)?'':' style=display:none')+'>'+
  '<div class=logbar>RECENT ACTIVITY<span class=logseg>'+logOpts(x.id)+'</span></div>'+
  '<pre class=logbox id="logbox-'+x.id+'">loading recent activity\u2026</pre></div>'+
  '</div>';
}
function lane(title,cls,icon,items){
 const inner=items.length?items.map(card).join(''):'<div class=lane-empty>\u2014 NONE \u2014</div>';
 return '<section class="lane '+cls+'"><div class=lane-head><span class=lane-ic>'+icon+'</span>'+title+
   '<span class=lane-ct>'+items.length+'</span></div><div class=lane-body>'+inner+'</div></section>';
}
async function tick(){
 frame++;
 const now=new Date();
 document.getElementById('clock').innerHTML=now.toLocaleTimeString()+'<small>SYSTEMS ONLINE</small>';
 let a;try{a=await (await fetch('/api/agents')).json();}catch(e){document.getElementById('clock').innerHTML='OFFLINE<small>MONITOR DOWN</small>';return;}
 const procArr=a.filter(x=>x.category==='processing');
 const blk=a.filter(x=>x.category==='blocked');
 const doneArr=a.filter(x=>x.category==='done');
 document.getElementById('chips').innerHTML=
  '<span class="chip p"><span class=n>\u2699</span> '+procArr.length+' PROCESSING</span>'+
  '<span class="chip b"><span class=n>\u270B</span> '+blk.length+' BLOCKED ON YOU</span>'+
  '<span class="chip d"><span class=n>\u2714</span> '+doneArr.length+' AWAITING NEXT</span>';
 const g=document.getElementById('grid');
 if(!a.length){g.innerHTML='<div class=empty>NO LIVE COPILOT CLI SESSIONS DETECTED</div>';}
 else{g.innerHTML=
   lane('RUNNING \u00B7 IN PROGRESS','lane-p','\u2699',procArr)+
   lane('BLOCKED \u00B7 NEEDS YOUR INPUT','lane-b','\u270B',blk)+
   lane('COMPLETED \u00B7 AWAITING YOUR NEXT ASK','lane-d','\u2714',doneArr);}
 const banner=document.getElementById('banner');
 if(blk.length){banner.style.display='block';banner.textContent='\u26A0 '+blk.length+' AGENT(S) BLOCKED \u2014 AWAITING YOUR RESPONSE: '+blk.map(w=>w.summary).join('  \u00B7  ');}
 else banner.style.display='none';
 const nowBlocked=new Set(blk.map(w=>w.id));
 const doneIds=new Set(a.filter(x=>x.category==='done').map(x=>x.id));
 const nm=Date.now();
 if(started){
  for(const w of blk){const due=chimeAt.get(w.id);
   if(due==null){notify(w);blockedSound();chimeAt.set(w.id,nm+REPEAT_MS);}
   else if(nm>=due){blockedSound();chimeAt.set(w.id,nm+REPEAT_MS);}}
  for(const id of doneIds){if(!prevDone.has(id)){doneSound();doneNotify(a.find(x=>x.id===id));}}
 }else{for(const w of blk)chimeAt.set(w.id,nm+REPEAT_MS);}
 for(const id of [...chimeAt.keys()])if(!nowBlocked.has(id))chimeAt.delete(id);
 prevDone=doneIds;started=true;
 // FAST & FURIOUS: auto-send /allow-all to newly permission-blocked agents
 if(ff){
  const perm=blk.filter(x=>x.state==='WAITING_PERMISSION'&&(x.term_kind==='iterm'||x.term_kind==='terminal'));
  for(const w of perm){if(!autopSent.has(w.id)){autopSent.add(w.id);autoApprove(w);}}
 }
 const permIds=new Set(blk.filter(x=>x.state==='WAITING_PERMISSION').map(x=>x.id));
 for(const id of [...autopSent])if(!permIds.has(id))autopSent.delete(id);
 refreshOpenLogs();
}
async function autoApprove(w){
 try{const r=await(await fetch('/api/autopilot?id='+encodeURIComponent(w.id))).json();
  toast((r.ok?'\U0001F3CE FAST & FURIOUS \u2192 auto-approved ':'auto-approve failed: ')+w.summary+(r.ok?'':' \u2014 '+(r.message||'')));
 }catch(e){toast('auto-approve failed');}
}
async function refreshOpenLogs(){
 for(const id of expanded){
  const box=document.getElementById('logbox-'+id);if(!box)continue;
  const n=logN.get(id)||'25';
  try{const r=await(await fetch('/api/log?id='+encodeURIComponent(id)+'&n='+n)).json();
   const txt=(r.lines||[]).join('\\n')||'(no recent activity)';
   const wrap=box.parentElement;const atBottom=wrap.scrollTop+wrap.clientHeight>=wrap.scrollHeight-20;
   if(box.textContent!==txt){box.textContent=txt;if(atBottom)wrap.scrollTop=wrap.scrollHeight;}
  }catch(e){}
 }
}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
let _ac;function audio(){try{_ac=_ac||new (window.AudioContext||window.webkitAudioContext)();if(_ac.state==='suspended')_ac.resume();return _ac;}catch(e){return null;}}
function beep(freq,start,dur,vol,type){const o=audio();if(!o)return;const t=o.currentTime+start;const osc=o.createOscillator();const g=o.createGain();osc.type=type||'sine';osc.frequency.value=freq;osc.connect(g);g.connect(o.destination);g.gain.setValueAtTime(0,t);g.gain.linearRampToValueAtTime(vol,t+0.02);g.gain.exponentialRampToValueAtTime(0.0001,t+dur);osc.start(t);osc.stop(t+dur+0.02);}
function blockedSound(){beep(660,0,0.18,0.06,'sine');beep(988,0.14,0.28,0.06,'sine');}
function doneSound(){beep(784,0,0.22,0.05,'sine');beep(523,0.18,0.4,0.05,'sine');}
function notify(w){try{if(Notification.permission==='granted')new Notification('\u270B Agent blocked \u2014 needs your response',{body:w.summary+': '+(w.waiting_for||w.detail||'')});}catch(e){}}
function doneNotify(w){if(!w)return;try{if(Notification.permission==='granted')new Notification('\u2714 Agent finished a turn',{body:w.summary+' \u2014 awaiting your next question'});}catch(e){}}
if('Notification'in window&&Notification.permission==='default')Notification.requestPermission();
/* single stable delegated listener; use pointerdown so it fires instantly on press
   (a single tap always registers even though the grid re-renders every 1.2s) */
document.getElementById('grid').addEventListener('pointerdown',e=>{
 const seg=e.target.closest('.segb');if(seg){e.preventDefault();logN.set(seg.dataset.id,seg.dataset.n);
  seg.parentElement.querySelectorAll('.segb').forEach(b=>b.classList.toggle('on',b===seg));
  const box=document.getElementById('logbox-'+seg.dataset.id);if(box)box.textContent='loading\u2026';refreshOpenLogs();return;}
 const l=e.target.closest('.btn.log');if(l){e.preventDefault();const id=l.dataset.id;
  if(expanded.has(id))expanded.delete(id);else expanded.add(id);
  const on=expanded.has(id);const wrap=document.getElementById('log-'+id);
  if(wrap){wrap.style.display=on?'':'none';}l.textContent=on?'\u25BE HIDE LOG':'\u25B8 SHOW LOG';
  if(on)refreshOpenLogs();return;}
 const f=e.target.closest('.btn.focus');if(f){e.preventDefault();focusAgent(f.dataset.id);return;}
 const k=e.target.closest('.btn.kill');if(k){e.preventDefault();killAgent(k.dataset.id,k.dataset.name);return;}
});
window.addEventListener('pointerdown',()=>audio(),{once:true});
window.addEventListener('keydown',()=>audio(),{once:true});
/* ---- constellation background (pausable + throttled to save CPU) ---- */
let fxRAF=null,fxLast=0;
const cvs=document.getElementById('fx'),cx=cvs.getContext('2d');let cw,ch,PTS=[];
function fxResize(){cw=cvs.width=innerWidth;ch=cvs.height=innerHeight;PTS=[];const n=Math.min(42,(cw*ch)/44000);
 for(let i=0;i<n;i++)PTS.push({x:Math.random()*cw,y:Math.random()*ch,vx:(Math.random()-.5)*.3,vy:(Math.random()-.5)*.3});}
addEventListener('resize',fxResize);fxResize();
function fxLoop(ts){fxRAF=requestAnimationFrame(fxLoop);
 if(ts-fxLast<50)return;fxLast=ts;   /* ~20fps */
 cx.clearRect(0,0,cw,ch);
 for(const p of PTS){p.x+=p.vx;p.y+=p.vy;if(p.x<0||p.x>cw)p.vx*=-1;if(p.y<0||p.y>ch)p.vy*=-1;}
 for(let i=0;i<PTS.length;i++){for(let j=i+1;j<PTS.length;j++){const a=PTS[i],b=PTS[j],dx=a.x-b.x,dy=a.y-b.y,d=Math.hypot(dx,dy);
   if(d<120){cx.strokeStyle='rgba(34,211,238,'+(0.12*(1-d/120))+')';cx.lineWidth=1;cx.beginPath();cx.moveTo(a.x,a.y);cx.lineTo(b.x,b.y);cx.stroke();}}}
 for(const p of PTS){cx.fillStyle='rgba(125,249,255,.5)';cx.beginPath();cx.arc(p.x,p.y,1.5,0,7);cx.fill();}}
function startFX(){if(!fxRAF&&fxOn)fxRAF=requestAnimationFrame(fxLoop);}
function stopFX(){if(fxRAF){cancelAnimationFrame(fxRAF);fxRAF=null;}cx.clearRect(0,0,cw,ch);}
/* ---- live / offline scheduler ---- */
let live=(localStorage.getItem('mon_live')||'1')==='1';
let intervalMs=parseInt(localStorage.getItem('mon_int')||'3000',10);
let fxOn=(localStorage.getItem('mon_fx')||'1')==='1';
let ff=(localStorage.getItem('mon_ff')||'0')==='1';
const autopSent=new Set();
let timer=null;
function schedule(){if(timer)clearInterval(timer);timer=null;if(live)timer=setInterval(tick,intervalMs);}
function updateControls(){
 const lb=document.getElementById('liveBtn');
 lb.textContent=live?'\u25CF LIVE':'\u23F8 PAUSED';lb.className='ctl '+(live?'live':'paused');
 document.getElementById('intgrp').classList.toggle('dim',!live);
 document.querySelectorAll('.segb2').forEach(b=>b.classList.toggle('on',parseInt(b.dataset.int,10)===intervalMs));
 const fb=document.getElementById('fxBtn');fb.className='ctl '+(fxOn?'':'off');fb.textContent=(fxOn?'\u2728 FX ON':'\u2728 FX OFF');
 document.getElementById('ffSwitch').className='ffswitch '+(ff?'ff':'manual');
 document.getElementById('ctlhint').textContent=live?('auto-refresh every '+(intervalMs/1000)+'s'):'offline \u2014 press REFRESH NOW to update';
}
function setLive(v){live=v;localStorage.setItem('mon_live',v?'1':'0');updateControls();
 if(live){tick();schedule();if(fxOn)startFX();}else{if(timer)clearInterval(timer);timer=null;stopFX();
  document.getElementById('clock').innerHTML='PAUSED<small>OFFLINE MODE</small>';}}
function setInt(ms){intervalMs=ms;localStorage.setItem('mon_int',''+ms);updateControls();if(live)schedule();}
function setFX(v){fxOn=v;localStorage.setItem('mon_fx',v?'1':'0');document.body.classList.toggle('nofx',!v);updateControls();
 if(fxOn&&live)startFX();else stopFX();}
function setFF(v){ff=v;localStorage.setItem('mon_ff',v?'1':'0');autopSent.clear();updateControls();
 audio();toast(v?'\U0001F3CE FAST & FURIOUS \u2014 blocked agents auto-approved via /allow-all (iTerm/Terminal only)':'\U0001F6E1 MANUAL \u2014 you approve every prompt yourself');
 if(v)tick();}
document.getElementById('liveBtn').addEventListener('click',()=>setLive(!live));
document.getElementById('refreshBtn').addEventListener('click',()=>tick());
document.getElementById('fxBtn').addEventListener('click',()=>setFX(!fxOn));
document.getElementById('ffSwitch').addEventListener('click',()=>setFF(!ff));
document.getElementById('intgrp').addEventListener('click',e=>{const b=e.target.closest('.segb2');if(b)setInt(parseInt(b.dataset.int,10));});
document.body.classList.toggle('nofx',!fxOn);
updateControls();
tick();
if(live){schedule();if(fxOn)startFX();}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # silence

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _agent_by_id(self, sid):
        with _latest_lock:
            for a in _latest["agents"]:
                if a["id"] == sid:
                    return dict(a)
        return None

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)

        if path == "/api/agents":
            with _latest_lock:
                self._json(_latest["agents"])
            return

        if path == "/api/log":
            sid = (qs.get("id") or [""])[0]
            nraw = (qs.get("n") or ["25"])[0]
            if nraw == "full":
                n, nbytes = 5000, 4 * 1024 * 1024
            else:
                try:
                    n = int(nraw)
                except ValueError:
                    n = 25
                n = max(1, min(n, 500))
                nbytes = 256 * 1024
            if not sid or not os.path.isdir(os.path.join(SESSION_STATE_DIR, sid)):
                self._json({"ok": False, "lines": [], "message": "unknown session"}, 404)
                return
            self._json({"ok": True, "lines": build_log(sid, n, nbytes)})
            return

        if path == "/api/focus":
            sid = (qs.get("id") or [""])[0]
            a = self._agent_by_id(sid)
            if not a:
                self._json({"ok": False, "message": "agent not found"}, 404)
                return
            ok, msg = focus_terminal(a.get("tty"), a.get("term_app"),
                                     a.get("term_kind"))
            self._json({"ok": ok, "message": msg})
            return

        if path == "/api/kill":
            sid = (qs.get("id") or [""])[0]
            force = (qs.get("force") or ["0"])[0] in ("1", "true", "yes")
            a = self._agent_by_id(sid)
            if not a:
                self._json({"ok": False, "message": "agent not found"}, 404)
                return
            ok, msg = kill_agent(a.get("pid"), force=force)
            self._json({"ok": ok, "message": msg})
            return

        if path == "/api/autopilot":
            sid = (qs.get("id") or [""])[0]
            a = self._agent_by_id(sid)
            if not a:
                self._json({"ok": False, "message": "agent not found"}, 404)
                return
            ok, msg = autopilot_terminal(a.get("tty"), a.get("term_app"),
                                         a.get("term_kind"))
            self._json({"ok": ok, "message": msg})
            return

        body = INDEX_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_web():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", WEB_PORT), Handler)
    except OSError as e:
        print(f"[web] could not bind port {WEB_PORT}: {e}", file=sys.stderr)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()


# ---------------------------------------------------------------------------
def main():
    web = "--no-web" not in sys.argv
    quiet = "--no-terminal" in sys.argv
    if web:
        start_web()
        print(f"Web dashboard: http://localhost:{WEB_PORT}")
        time.sleep(0.6)

    prev_waiting = set()
    stop = {"v": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("v", True))
    try:
        while not stop["v"]:
            agents = scan()
            with _latest_lock:
                _latest["agents"] = agents
                _latest["ts"] = time.time()
            if not quiet:
                prev_waiting = render_terminal(agents, prev_waiting)
            time.sleep(REFRESH_SECONDS)
    finally:
        sys.stdout.write("\033[0m\nMonitor stopped.\n")


if __name__ == "__main__":
    main()
