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
import shlex
import shutil
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
    keystroke. VS Code/Cursor integrated terminals can't be targeted by tty via
    AppleScript, so we activate the editor and send the keystroke to whichever
    integrated terminal currently has focus (best-effort).
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
    elif kind == "vscode":
        # VS Code/Cursor: AppleScript can't enumerate integrated-terminal tabs by
        # tty, so bring the editor to the front and type into the focused terminal.
        # When an agent is blocked on a prompt its terminal panel is normally the
        # active element, so this reliably lands on the waiting session.
        app = app_name or "Cursor"
        script = f'''
        tell application "{app}" to activate
        delay 0.25
        tell application "System Events"
          keystroke "{safe}"
          key code 36
        end tell
        return "ok"'''
    else:
        return False, (f"Auto-approve only supports iTerm/Terminal/Cursor/VS Code; "
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


_PR_RE = re.compile(r"https?://github\.com/[^\s\"')]+/pull/\d+")
_COMMIT_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def build_summary(session_id, max_bytes=8 * 1024 * 1024):
    """Build a heuristic summary of what a past session accomplished."""
    path = os.path.join(SESSION_STATE_DIR, session_id, "events.jsonl")
    if not os.path.exists(path):
        return None
    first_user = ""
    last_assistant = ""
    user_turns = 0
    assistant_turns = 0
    files_created = set()
    files_edited = set()
    bash_cmds = []
    pr_links = set()
    start_ts = end_ts = None

    try:
        size = os.path.getsize(path)
        with open(path, "r", errors="ignore") as f:
            if size > max_bytes:
                # read the head (for the first ask) then tail (for outcome)
                head = f.read(max_bytes // 4)
                f.seek(size - (max_bytes - max_bytes // 4))
                f.readline()  # drop partial line
                chunks = head.splitlines() + f.read().splitlines()
            else:
                chunks = f.read().splitlines()
    except Exception:
        return None

    for line in chunks:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get("type")
        d = e.get("data") or {}
        ts = _iso_to_epoch(e.get("timestamp"))
        if ts:
            start_ts = ts if start_ts is None else min(start_ts, ts)
            end_ts = ts if end_ts is None else max(end_ts, ts)
        if t == "user.message":
            c = _oneline(d.get("content") or "")
            if c:
                user_turns += 1
                if not first_user:
                    first_user = c
                for m in _PR_RE.findall(c):
                    pr_links.add(m)
        elif t == "assistant.message":
            c = _oneline(d.get("content") or d.get("text") or "")
            if c:
                assistant_turns += 1
                last_assistant = c
                for m in _PR_RE.findall(c):
                    pr_links.add(m)
        elif t == "tool.execution_start":
            name = d.get("toolName")
            args = d.get("arguments") or {}
            if name == "create" and args.get("path"):
                files_created.add(args["path"])
            elif name == "edit" and args.get("path"):
                files_edited.add(args["path"])
            elif name == "bash" and args.get("command"):
                bash_cmds.append(_oneline(args["command"]))
        elif t == "tool.execution_complete":
            res = d.get("result") or {}
            blob = ""
            if isinstance(res, dict):
                blob = json.dumps(res)[:2000]
            for m in _PR_RE.findall(blob):
                pr_links.add(m)

    dur = round(end_ts - start_ts) if (start_ts and end_ts) else None
    return {
        "first_user": first_user[:600],
        "last_assistant": last_assistant[:800],
        "user_turns": user_turns,
        "assistant_turns": assistant_turns,
        "files_created": sorted(files_created)[:40],
        "files_edited": sorted(files_edited)[:40],
        "files_created_count": len(files_created),
        "files_edited_count": len(files_edited),
        "bash_count": len(bash_cmds),
        "bash_sample": bash_cmds[:8],
        "pr_links": sorted(pr_links)[:20],
        "duration_seconds": dur,
    }


# ---------------------------------------------------------------------------
# Lightweight session briefs + suggestions
# ---------------------------------------------------------------------------
_brief_cache = {}  # sid -> (mtime, brief)
_STOP = {"the", "and", "for", "with", "that", "this", "you", "can", "how",
         "get", "was", "are", "from", "into", "just", "want", "need", "please",
         "help", "some", "like", "give", "make", "have", "then", "what", "why"}


def _tokens(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) >= 3 and w not in _STOP}


def session_brief(sid):
    """Cheap, cached brief of a session for ranking (size + first ask)."""
    path = os.path.join(SESSION_STATE_DIR, sid, "events.jsonl")
    try:
        st = os.stat(path)
    except OSError:
        return None
    cached = _brief_cache.get(sid)
    if cached and cached[0] == st.st_mtime:
        return cached[1]
    first_user = ""
    try:
        with open(path, "r", errors="ignore") as f:
            head = f.read(256 * 1024)
        for line in head.splitlines():
            if '"user.message"' not in line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") == "user.message":
                c = _oneline((e.get("data") or {}).get("content") or "")
                if c:
                    first_user = c
                    break
    except Exception:
        pass
    brief = {"size": st.st_size, "mtime": st.st_mtime, "first_user": first_user}
    _brief_cache[sid] = (st.st_mtime, brief)
    return brief


def suggest_sessions(query="", limit=5, scan_limit=60):
    """Suggest past sessions: ranked by relevance to `query`, else by richness.

    Richness is proxied by event-log size (more activity = richer). Relevance
    is token overlap between the query and title/first-ask/repo/path.
    """
    summaries = load_summaries()
    cand = history(limit=scan_limit)  # recent, non-live
    q_tokens = _tokens(query)
    rows = []
    max_size = 1
    briefs = {}
    for c in cand:
        b = session_brief(c["id"])
        if not b:
            continue
        briefs[c["id"]] = b
        max_size = max(max_size, b["size"])
    for c in cand:
        b = briefs.get(c["id"])
        if not b:
            continue
        title = c["summary"]
        hay = " ".join([title, b["first_user"], c.get("repository", ""),
                        c.get("cwd", "")]).lower()
        hay_tokens = _tokens(hay)
        matched = sorted(q_tokens & hay_tokens)
        # substring boost for multi-word phrases in the query
        phrase_hit = bool(query.strip()) and query.strip().lower() in hay
        richness = b["size"] / max_size  # 0..1
        if q_tokens:
            rel = len(matched) / len(q_tokens)
            score = rel * 3 + (1.0 if phrase_hit else 0) + richness
        else:
            score = richness
        rows.append({
            "id": c["id"],
            "short": c["short"],
            "summary": title,
            "cwd": c.get("cwd", ""),
            "repository": c.get("repository", ""),
            "age_seconds": c.get("age_seconds"),
            "size_kb": round(b["size"] / 1024),
            "first_user": b["first_user"][:240],
            "matched": matched,
            "score": round(score, 3),
        })
    if q_tokens:
        # keep only sessions that matched at least one term when querying
        matched_rows = [r for r in rows if r["matched"]]
        pool = matched_rows if matched_rows else rows
        pool.sort(key=lambda r: (-r["score"], -(r["size_kb"] or 0)))
        return pool[:limit], bool(matched_rows)
    rows.sort(key=lambda r: -(r["size_kb"] or 0))
    return rows[:limit], True


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
# Session history (previous / non-live sessions)
# ---------------------------------------------------------------------------
def _live_session_ids():
    """Set of session ids that currently have a live process holding the lock."""
    live = set()
    if not os.path.isdir(SESSION_STATE_DIR):
        return live
    for sid in os.listdir(SESSION_STATE_DIR):
        sdir = os.path.join(SESSION_STATE_DIR, sid)
        if os.path.isdir(sdir) and live_pid(sdir) is not None:
            live.add(sid)
    return live


def load_sessions_full():
    """Return list of dicts for every session in the store (read-only)."""
    rows = []
    try:
        import sqlite3
        uri = f"file:{SESSION_STORE_DB}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            for sid, summary, cwd, repo, branch, created, updated in con.execute(
                "SELECT id, summary, cwd, repository, branch, created_at, "
                "updated_at FROM sessions"
            ):
                rows.append({
                    "id": sid,
                    "summary": summary or "",
                    "cwd": cwd or "",
                    "repository": repo or "",
                    "branch": branch or "",
                    "created_at": created or "",
                    "updated_at": updated or "",
                })
        finally:
            con.close()
    except Exception:
        pass
    return rows


def history(limit=80):
    """Return past (non-live) sessions, most-recently-updated first."""
    live = _live_session_ids()
    rows = load_sessions_full()
    out = []
    for r in rows:
        if r["id"] in live:
            continue
        updated_epoch = _iso_to_epoch(r["updated_at"])
        has_log = os.path.exists(
            os.path.join(SESSION_STATE_DIR, r["id"], "events.jsonl"))
        age = round(time.time() - updated_epoch) if updated_epoch else None
        out.append({
            "id": r["id"],
            "short": r["id"][:8],
            "summary": r["summary"] or "(untitled session)",
            "cwd": r["cwd"],
            "repository": r["repository"],
            "branch": r["branch"],
            "updated_at": r["updated_at"],
            "updated_epoch": updated_epoch,
            "age_seconds": age,
            "has_log": has_log,
        })
    out.sort(key=lambda x: -(x["updated_epoch"] or 0))
    return out[:limit]


# The CLI binary used to resume a session in a fresh terminal window.
RESUME_BIN = os.environ.get("MONITOR_COPILOT_BIN", "copilot")
PRIMER_DIR = os.path.join(COPILOT_DIR, "session-primers")


def _open_terminal(inner):
    """Open a new terminal window running the shell command `inner`.

    Prefers iTerm2 if installed, otherwise falls back to Terminal.app.
    Returns (ok, app_name, error).
    """
    use_iterm = os.path.isdir("/Applications/iTerm.app")
    safe = inner.replace('\\', '\\\\').replace('"', '\\"')
    if use_iterm:
        script = f'''
        tell application "iTerm2"
          activate
          set w to (create window with default profile)
          tell current session of w to write text "{safe}"
        end tell
        return "ok"'''
    else:
        script = f'''
        tell application "Terminal"
          activate
          do script "{safe}"
        end tell
        return "ok"'''
    app = "iTerm2" if use_iterm else "Terminal"
    try:
        res = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=8)
        if (res.stdout or "").strip() == "ok":
            return True, app, ""
        return False, app, (res.stderr or "could not open terminal").strip()[:200]
    except Exception as e:
        return False, app, str(e)


def resume_session(session_id, cwd="", allow_all=False):
    """Open a new terminal window running `copilot --resume=<id>`.

    When allow_all is set, `--allow-all-tools` is added so the resumed session
    auto-approves prompts.
    """
    flags = "--allow-all-tools " if allow_all else ""
    inner = f"{RESUME_BIN} {flags}--resume={shlex.quote(session_id)}"
    if cwd:
        inner = f"cd {shlex.quote(cwd)} && {inner}"
    ok, app, err = _open_terminal(inner)
    if ok:
        return True, f"Resuming in a new {app} window{' (allow-all)' if allow_all else ''}."
    return False, err or "could not open terminal"


def context_items(session_id):
    """Return granular, selectable context suggestions for a past session."""
    summ = build_summary(session_id)
    if not summ:
        return []
    items = []
    if summ.get("first_user"):
        items.append({"key": "ask", "label": "Original ask", "text": summ["first_user"]})
    if summ.get("last_assistant"):
        items.append({"key": "outcome", "label": "Last outcome / result",
                      "text": summ["last_assistant"]})
    files = (summ.get("files_created") or []) + (summ.get("files_edited") or [])
    if files:
        n = summ.get("files_created_count", 0) + summ.get("files_edited_count", 0)
        items.append({"key": "files", "label": f"Files touched ({n})",
                      "text": ", ".join(files)})
    if summ.get("pr_links"):
        items.append({"key": "prs", "label": "Pull requests",
                      "text": ", ".join(summ["pr_links"])})
    if summ.get("bash_sample"):
        items.append({"key": "commands", "label": "Key commands",
                      "text": "; ".join(summ["bash_sample"])})
    return items


def _write_primer(blocks):
    """Write assembled markdown blocks to a primer file; return its path."""
    if not blocks:
        return None
    os.makedirs(PRIMER_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(PRIMER_DIR, f"primer-{stamp}.md")
    header = (
        "# Memory from previous Copilot sessions\n\n"
        "The following is context the user hand-picked from earlier sessions "
        "to carry forward into this new session.\n\n")
    with open(path, "w") as f:
        f.write(header + "\n\n---\n\n".join(blocks) + "\n")
    return path


def build_primer_from_items(items):
    """Assemble a primer from explicitly chosen context items.

    Each item is a dict with `title` (session/group heading), `label`, `text`.
    Items are grouped by title in first-seen order.
    """
    groups = {}
    order = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        text = (it.get("text") or "").strip()
        if not text:
            continue
        title = (it.get("title") or "Context").strip()
        if title not in groups:
            groups[title] = []
            order.append(title)
        groups[title].append((it.get("label") or "note", text))
    blocks = []
    for title in order:
        lines = [f"## {title}"]
        for label, text in groups[title]:
            lines.append(f"- {label}: {text}")
        blocks.append("\n".join(lines))
    return _write_primer(blocks)


def build_primer(session_ids):
    """Write a markdown 'memory' file summarizing the given past sessions.

    Returns (primer_path, session_summaries) or (None, []) if nothing usable.
    """
    summaries = load_summaries()
    blocks = []
    for sid in session_ids:
        summ = build_summary(sid)
        title = (summaries.get(sid, ("", ""))[0]) or "(untitled session)"
        if not summ:
            continue
        lines = [f"## {title}", f"- Session id: `{sid}`"]
        if summ.get("first_user"):
            lines.append(f"- Original ask: {summ['first_user']}")
        if summ.get("last_assistant"):
            lines.append(f"- Last outcome: {summ['last_assistant']}")
        if summ.get("files_edited") or summ.get("files_created"):
            files = (summ.get("files_created") or []) + (summ.get("files_edited") or [])
            lines.append(f"- Files touched: {', '.join(files[:30])}")
        if summ.get("pr_links"):
            lines.append(f"- Pull requests: {', '.join(summ['pr_links'])}")
        if summ.get("bash_sample"):
            lines.append(f"- Sample commands: {'; '.join(summ['bash_sample'][:6])}")
        blocks.append("\n".join(lines))
    if not blocks:
        return None, []
    path = _write_primer(blocks)
    return path, blocks


def new_session(session_ids=None, cwd="", task="", allow_all=False, items=None):
    """Open a new copilot session, optionally seeded with memory from past ones.

    Memory can come from hand-picked context `items` (preferred) or, failing
    that, whole `session_ids`. A primer file is built and the new interactive
    session starts with an initial prompt telling copilot to read it.
    """
    session_ids = session_ids or []
    primer_path = None
    if items:
        primer_path = build_primer_from_items(items)
        if not primer_path:
            return False, "no usable context was selected."
    elif session_ids:
        primer_path, _ = build_primer(session_ids)
        if not primer_path:
            return False, "could not build a memory primer from those sessions."
    parts = [RESUME_BIN]
    if allow_all:
        parts.append("--allow-all")
    if primer_path:
        parts += ["--add-dir", shlex.quote(PRIMER_DIR)]
        ask = task.strip() or "Continue where these left off — ask me what I want to do next."
        prompt = (f"Read the memory file {shlex.quote(primer_path)} which "
                  f"summarizes my previous related Copilot sessions, treat it as "
                  f"prior context, then help me: {ask}")
        parts += ["-i", shlex.quote(prompt)]
    elif task.strip():
        parts += ["-i", shlex.quote(task.strip())]
    inner = " ".join(parts)
    if cwd:
        inner = f"cd {shlex.quote(cwd)} && {inner}"
    ok, app, err = _open_terminal(inner)
    if ok:
        if items:
            note = f" seeded with {len(items)} hand-picked context item(s)"
        elif session_ids:
            note = f" seeded with memory from {len(session_ids)} session(s)"
        else:
            note = ""
        return True, f"Opened a new Copilot session in {app}{note}."
    return False, err or "could not open terminal"


def delete_session(session_id):
    """Remove a non-live session: its state dir and its store row (best-effort)."""
    sdir = os.path.join(SESSION_STATE_DIR, session_id)
    if os.path.isdir(sdir) and live_pid(sdir) is not None:
        return False, "session is live — cannot delete a running session."
    removed = []
    if os.path.isdir(sdir):
        try:
            shutil.rmtree(sdir)
            removed.append("state")
        except Exception as e:
            return False, f"could not remove session state: {e}"
    try:
        import sqlite3
        con = sqlite3.connect(SESSION_STORE_DB, timeout=2.0)
        try:
            con.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            con.commit()
            removed.append("record")
        finally:
            con.close()
    except Exception:
        pass  # store row deletion is best-effort
    if not removed:
        return False, "nothing to delete (session not found)."
    return True, f"Deleted session ({' + '.join(removed)})."


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

STYLE = """<style>
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
 a.ctl{text-decoration:none;display:inline-flex;align-items:center}
 #histLink{border-color:rgba(127,167,179,.5);color:#cfeef6}
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
 .btn.send{border-color:var(--gold);color:#ffdf9a;background:rgba(255,176,32,.1);text-shadow:0 0 6px rgba(255,176,32,.5)}
 .btn.send:hover{background:rgba(255,176,32,.22);box-shadow:0 0 12px rgba(255,176,32,.5)}
 .replybox{margin-top:12px;display:flex;gap:8px;align-items:stretch}
 .replyta{flex:1;font-family:inherit;font-size:12.5px;color:#eafbff;background:rgba(1,6,11,.7);
   border:1px solid rgba(34,211,238,.35);border-radius:4px;padding:8px 10px;outline:none;transition:.14s}
 .replyta:focus{border-color:var(--cy);box-shadow:0 0 12px rgba(34,211,238,.35)}
 .replyta::placeholder{color:var(--muted);letter-spacing:.5px}
 #histsec{margin-top:26px}
 .histbar{display:flex;align-items:center;gap:14px;margin-bottom:14px;flex-wrap:wrap}
 .histsearch{flex:1;min-width:220px;font-family:inherit;font-size:12.5px;color:#eafbff;background:rgba(1,6,11,.7);
   border:1px solid rgba(34,211,238,.3);border-radius:4px;padding:8px 12px;outline:none;transition:.14s}
 .histsearch:focus{border-color:var(--cy);box-shadow:0 0 12px rgba(34,211,238,.3)}
 .histsearch::placeholder{color:var(--muted);letter-spacing:.5px}
 .histcount{font-size:11px;letter-spacing:1.5px;color:var(--muted)}
 .panel.hist{border-color:rgba(127,167,179,.3);opacity:.94}
 .panel.hist .title{color:#dff2f7}
 .hmeta{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px 14px;font-size:11px;color:var(--muted);letter-spacing:.5px}
 .hmeta b{color:var(--cy2);font-weight:600}
 .btn.resume{border-color:var(--grn,#39d98a);color:#9af5c6;background:rgba(57,217,138,.1);text-shadow:0 0 6px rgba(57,217,138,.4)}
 .btn.resume:hover{background:rgba(57,217,138,.22);box-shadow:0 0 12px rgba(57,217,138,.4)}
 .btn.resumeff{border-color:var(--gold);color:#ffdf9a;background:rgba(255,176,32,.1);text-shadow:0 0 6px rgba(255,176,32,.5)}
 .btn.resumeff:hover{background:rgba(255,176,32,.22);box-shadow:0 0 12px rgba(255,176,32,.5)}
 .summbox{padding:14px 16px;font-size:12.5px;line-height:1.55;color:#dbeef4}
 .sstats{display:flex;flex-wrap:wrap;gap:8px 16px;margin-bottom:12px;font-size:11.5px;color:var(--cy2);letter-spacing:.5px}
 .sstats span{background:rgba(34,211,238,.08);border:1px solid rgba(34,211,238,.2);border-radius:4px;padding:4px 9px}
 .srow{margin-top:12px}
 .slbl{font-size:10px;font-weight:800;letter-spacing:1.5px;color:var(--muted);margin-bottom:5px}
 .sask{white-space:pre-wrap;word-break:break-word;color:#eafbff;background:rgba(1,6,11,.5);border-left:2px solid rgba(34,211,238,.4);padding:8px 11px;border-radius:0 4px 4px 0;max-height:180px;overflow:auto}
 .sfiles{display:flex;flex-direction:column;gap:4px}
 .sfiles code{font-family:inherit;font-size:11.5px;color:#bfe9f2;background:rgba(127,167,179,.08);padding:3px 8px;border-radius:3px;word-break:break-all}
 .sfiles a{color:var(--gold);word-break:break-all;text-decoration:none}
 .sfiles a:hover{text-decoration:underline}
 .smore{color:var(--muted);font-size:11px;padding:3px 4px}
 .sempty{color:var(--muted);letter-spacing:1px;padding:6px 2px}
 .newbtn{border-color:var(--grn);color:#9af5c6;background:rgba(57,217,138,.1)}
 .newbtn:hover{background:rgba(57,217,138,.2)}
 .panel.hist{position:relative}
 .panel.picked{border-color:var(--grn);box-shadow:0 0 16px rgba(57,217,138,.25)}
 .hpick{position:absolute;top:12px;right:14px;display:inline-flex;align-items:center;gap:6px;font-size:10px;
   letter-spacing:1.5px;font-weight:700;color:var(--muted);cursor:pointer;user-select:none}
 .hpick input{accent-color:var(--grn);width:15px;height:15px;cursor:pointer}
 #histgrid{display:flex;flex-direction:column;gap:6px}
 .hrow{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:12px;
   padding:8px 12px;border:1px solid rgba(127,167,179,.25);border-radius:7px;
   background:rgba(4,12,18,.6);transition:border-color .15s,background .15s}
 .hrow:hover{border-color:rgba(34,211,238,.4);background:rgba(6,16,24,.8)}
 .hrow.picked{border-color:var(--grn);box-shadow:0 0 12px rgba(57,217,138,.2)}
 .hrow.open{background:rgba(6,16,24,.85)}
 .hpick2{display:inline-flex;align-items:center;cursor:pointer}
 .hpick2 input{accent-color:var(--grn);width:16px;height:16px;cursor:pointer}
 .hmain{min-width:0}
 .hmain[data-toggle]{cursor:pointer}
 .hmain[data-toggle]:hover .htitle{color:var(--cy)}
 .htitle{font-size:13px;font-weight:600;color:#dff2f7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .hmeta2{display:flex;gap:10px;align-items:center;font-size:10.5px;color:var(--muted);letter-spacing:.4px;margin-top:2px;overflow:hidden}
 .hmeta2 .hr{white-space:nowrap;flex:none}
 .hmeta2 .hpath{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;opacity:.7;min-width:0}
 .hacts{display:flex;gap:5px;flex:none}
 .hacts .btn{padding:5px 9px;font-size:10.5px;min-width:0;letter-spacing:.5px}
 .hrow .logwrap{grid-column:1/-1;animation:slidein .18s ease}
 @keyframes slidein{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
 .selbar{position:sticky;bottom:14px;margin-top:16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
   background:rgba(6,18,14,.92);border:1px solid var(--grn);border-radius:8px;padding:12px 16px;
   box-shadow:0 0 24px rgba(57,217,138,.3);backdrop-filter:blur(6px);z-index:20}
 .selcount{font-size:11px;font-weight:800;letter-spacing:1.5px;color:#9af5c6}
 .selbar .histsearch{flex:1;min-width:220px}
 .selchk{display:inline-flex;align-items:center;gap:6px;font-size:11px;letter-spacing:1px;color:#ffdf9a;cursor:pointer}
 .selchk input{accent-color:var(--gold);width:15px;height:15px;cursor:pointer}
 .suggbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 12px}
 .suggbox{display:none;margin:0 0 18px;border:1px solid rgba(34,211,238,.3);border-radius:8px;
   background:rgba(3,12,18,.85);box-shadow:0 0 18px rgba(34,211,238,.12) inset;padding:10px 12px}
 .suggtitle{display:flex;align-items:center;justify-content:space-between;font-size:11px;font-weight:800;
   letter-spacing:1.4px;color:#8fe9ff;margin:2px 2px 10px}
 .suggrow{display:flex;align-items:center;gap:12px;padding:9px 4px;border-top:1px solid rgba(34,211,238,.12)}
 .suggrow:first-of-type{border-top:none}
 .suggmain{flex:1;min-width:0}
 .suggname{font-size:13px;font-weight:700;color:#eafbff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .suggwhy{font-size:10.5px;letter-spacing:.4px;color:var(--muted);margin-top:2px}
 .suggask{font-size:11px;color:#bfe9ff;margin-top:4px;opacity:.85;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .suggadd{flex:none;white-space:nowrap}
 .modal{position:fixed;inset:0;background:rgba(1,4,8,.78);backdrop-filter:blur(4px);z-index:60;
   display:flex;align-items:center;justify-content:center;padding:24px}
 .modalcard{width:min(880px,96vw);max-height:88vh;display:flex;flex-direction:column;
   background:rgba(5,14,20,.98);border:1px solid var(--cy);border-radius:12px;
   box-shadow:0 0 40px rgba(34,211,238,.35)}
 .modalhead{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;
   font-size:13px;font-weight:800;letter-spacing:1.4px;color:#8fe9ff;border-bottom:1px solid rgba(34,211,238,.2)}
 .xbtn{background:none;border:none;color:var(--muted);font-size:16px;cursor:pointer;padding:2px 6px;line-height:1}
 .xbtn:hover{color:#ff8a8a}
 .modalhint{padding:10px 20px 4px;font-size:11px;color:var(--muted);letter-spacing:.4px}
 .composebody{flex:1;overflow:auto;padding:8px 20px 12px}
 .composebody::-webkit-scrollbar{width:8px}.composebody::-webkit-scrollbar-thumb{background:rgba(34,211,238,.3);border-radius:4px}
 .cgroup{margin:12px 0;border:1px solid rgba(34,211,238,.2);border-radius:8px;padding:10px 12px;background:rgba(1,6,11,.6)}
 .cgtitle{font-size:12px;font-weight:800;letter-spacing:1px;color:#9af5c6;margin-bottom:8px}
 .cgid{font-weight:500;color:var(--muted);letter-spacing:.5px}
 .citem{display:flex;gap:9px;align-items:flex-start;padding:6px 4px;font-size:12px;color:#dff4ff;cursor:pointer;border-top:1px solid rgba(34,211,238,.08)}
 .citem:first-of-type{border-top:none}
 .citem input{accent-color:var(--cy);width:15px;height:15px;margin-top:2px;cursor:pointer;flex:none}
 .citxt{line-height:1.5}.citxt b{color:#8fe9ff}
 .modalfoot{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:14px 20px;border-top:1px solid rgba(34,211,238,.2)}
 .modalfoot .histsearch{flex:1;min-width:220px}
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
</style>"""

INDEX_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>J.A.R.V.I.S. · Copilot Agent Monitor</title>
""" + STYLE + """</head><body>
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
 <button id=newBtn class="ctl newbtn">\uFF0B NEW COPILOT</button>
 <a href="/history" class=ctl id=histLink>\U0001F5C2 PREVIOUS SESSIONS</a>
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
const replyDraft=new Map();let replyFocus=null;
function replyBox(id){
 return '<div class=replybox><input class=replyta id="reply-'+id+'" data-id="'+id+'" type=text '+
  'placeholder="Type a reply and press Enter to send\u2026" autocomplete=off spellcheck=false>'+
  '<button class="btn send" data-id="'+id+'">\u21B5 SEND</button></div>';
}
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
 const canType=(x.term_kind==='iterm'||x.term_kind==='terminal'||x.term_kind==='vscode');
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
  if(x.state==='WAITING_QUESTION'&&canType)body+=replyBox(x.id);
 }else{cls='done';
  icon='<div class=doneicon>\u2714</div>';
  statetxt='STANDBY \u00B7 AWAITING NEXT DIRECTIVE';
  body='<div class=detail>\u2714 turn complete \u2014 last transmission: '+esc((x.detail||'').slice(0,240))+'</div>';
  if(canType)body+=replyBox(x.id);
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
 restoreReplies();
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
  const perm=blk.filter(x=>x.state==='WAITING_PERMISSION'&&(x.term_kind==='iterm'||x.term_kind==='terminal'||x.term_kind==='vscode'));
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
/* keep reply drafts + focus/caret across the periodic grid re-render */
function restoreReplies(){
 document.querySelectorAll('.replyta').forEach(ta=>{
  const id=ta.dataset.id;const d=replyDraft.get(id);if(d!=null)ta.value=d;
  if(replyFocus===id){ta.focus();const n=ta.value.length;try{ta.setSelectionRange(n,n);}catch(e){}}
 });
}
async function sendReply(id){
 const ta=document.getElementById('reply-'+id);
 const msg=((replyDraft.get(id)!=null?replyDraft.get(id):(ta?ta.value:''))||'').trim();
 if(!msg){toast('reply is empty');return;}
 try{const r=await(await fetch('/api/reply?id='+encodeURIComponent(id),
   {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})})).json();
  if(r.ok){toast('\u21B5 reply sent');replyDraft.delete(id);if(replyFocus===id)replyFocus=null;
   if(ta)ta.value='';setTimeout(tick,450);}
  else toast('reply failed'+(r.message?' \u2014 '+r.message:''));
 }catch(e){toast('reply failed');}
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
 const s=e.target.closest('.btn.send');if(s){e.preventDefault();sendReply(s.dataset.id);return;}
 const k=e.target.closest('.btn.kill');if(k){e.preventDefault();killAgent(k.dataset.id,k.dataset.name);return;}
});
/* reply input: track draft text, focus, and Enter-to-send across re-renders */
document.getElementById('grid').addEventListener('input',e=>{
 const ta=e.target.closest('.replyta');if(ta)replyDraft.set(ta.dataset.id,ta.value);
});
document.getElementById('grid').addEventListener('focusin',e=>{
 const ta=e.target.closest('.replyta');if(ta)replyFocus=ta.dataset.id;
});
document.getElementById('grid').addEventListener('focusout',e=>{
 const ta=e.target.closest('.replyta');if(ta&&replyFocus===ta.dataset.id)replyFocus=null;
});
document.getElementById('grid').addEventListener('keydown',e=>{
 const ta=e.target.closest('.replyta');if(!ta)return;
 if(e.key==='Enter'){e.preventDefault();sendReply(ta.dataset.id);}
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
 audio();toast(v?'\U0001F3CE FAST & FURIOUS \u2014 blocked agents auto-approved via /allow-all (iTerm/Terminal/Cursor/VS Code)':'\U0001F6E1 MANUAL \u2014 you approve every prompt yourself');
 if(v)tick();}
document.getElementById('liveBtn').addEventListener('click',()=>setLive(!live));
document.getElementById('refreshBtn').addEventListener('click',()=>tick());
document.getElementById('fxBtn').addEventListener('click',()=>setFX(!fxOn));
document.getElementById('ffSwitch').addEventListener('click',()=>setFF(!ff));
document.getElementById('newBtn').addEventListener('click',async()=>{
 const task=prompt('Start a new Copilot session.\\n\\nOptional first instruction (leave blank for a plain session):','')||'';
 try{const r=await(await fetch('/api/new-session',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({task:task})})).json();toast(r.message||(r.ok?'opening\u2026':'failed'));
 }catch(e){toast('could not start session');}
});
document.getElementById('intgrp').addEventListener('click',e=>{const b=e.target.closest('.segb2');if(b)setInt(parseInt(b.dataset.int,10));});
document.body.classList.toggle('nofx',!fxOn);
updateControls();
tick();
if(live){schedule();if(fxOn)startFX();}
</script></body></html>"""


HISTORY_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>Previous Sessions · Copilot Agent Monitor</title>
""" + STYLE + """</head><body class=nofx>
<header>
 <div class=reactor-lg><span class="r r1"></span><span class="r r2"></span><span class="r r3"></span><span class=core></span></div>
 <div><h1>J.A.R.V.I.S. <b>//</b> SESSION ARCHIVE</h1><div class=tag>COPILOT CLI \u00B7 PREVIOUS SESSIONS \u2014 RESUME / REVIEW / CLEAN UP</div></div>
 <div class=clock id=clock>--<small>ARCHIVE</small></div>
</header>
<div class=controls>
 <a href="/" class=ctl>\u2190 BACK TO LIVE DASHBOARD</a>
 <button id=refreshBtn class=ctl>\u27F3 REFRESH</button>
 <input id=histSearch class=histsearch type=text placeholder="Filter by title, repo, or path\u2026" autocomplete=off spellcheck=false>
 <span class=ctlhint id=histCount></span>
</div>
<div class=suggbar>
 <input id=suggQ class=histsearch type=text placeholder="Describe what you\u2019re about to work on \u2192 get suggested sessions to reuse\u2026" autocomplete=off spellcheck=false>
 <button id=suggBtn class="ctl newbtn">\U0001F50E SUGGEST</button>
 <button id=richBtn class=ctl>\u2B50 TOP RICH</button>
</div>
<div id=suggbox class=suggbox></div>
<div class=grid id=histgrid><div class=empty>LOADING PREVIOUS SESSIONS\u2026</div></div>
<div id=selbar class=selbar style=display:none>
 <span class=selcount id=selCount></span>
 <button id=selStart class="btn resume">\U0001F9E0 COMPOSE MEMORY \u2192</button>
 <button id=selClear class="btn log">CLEAR</button>
</div>
<div id=composeModal class=modal style=display:none>
 <div class=modalcard>
  <div class=modalhead>\U0001F9E0 COMPOSE MEMORY FOR NEW SESSION<button id=composeClose class=xbtn>\u2715</button></div>
  <div class=modalhint>Pick the exact context to copy from each selected session. Only checked items are carried into the new session.</div>
  <div id=composeBody class=composebody>loading\u2026</div>
  <div class=modalfoot>
   <input id=composeTask class=histsearch type=text placeholder="What should the new session do? (optional)" autocomplete=off spellcheck=false>
   <label class=selchk><input type=checkbox id=composeAllow> \U0001F3CE allow-all</label>
   <button id=composeStart class="btn resume">\u25B6 START NEW SESSION</button>
  </div>
 </div>
</div>
<div class=toast id=toast></div>
<script>
const expanded=new Set();const logN=new Map();const selected=new Set();
let histData=[];let histFilter='';
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),3500);}
function fmtAgeLong(s){if(s==null)return'?';if(s<60)return s+'s ago';if(s<3600)return((s/60)|0)+'m ago';
 if(s<86400)return((s/3600)|0)+'h ago';return((s/86400)|0)+'d ago';}
function logOpts(id){const v=logN.get(id)||'25';
 return ['25','50','full'].map(o=>'<button class="segb'+(o===v?' on':'')+'" data-id="'+id+'" data-n="'+o+'">'+(o==='full'?'FULL':o)+'</button>').join('');}
function fmtDur(s){if(s==null)return'?';if(s<60)return s+'s';if(s<3600)return((s/60)|0)+'m';return((s/3600)|0)+'h'+(((s%3600)/60)|0)+'m';}
function histCard(x){
 const sel=selected.has(x.id);
 const exp=expanded.has(x.id);
 const summbtn=x.has_log?'<button class="btn log" data-id="'+x.id+'">'+(exp?'\u25BE SUMMARY':'\u25B8 SUMMARY')+'</button>':'';
 const meta=(x.repository?'<span class=hr>\u2325 '+esc(x.repository)+(x.branch?'@'+esc(x.branch):'')+'</span>':'')+
  '<span class=hr>'+x.short+'</span><span class=hr>'+fmtAgeLong(x.age_seconds)+'</span>';
 return '<div class="hrow'+(sel?' picked':'')+(exp?' open':'')+'" data-id="'+x.id+'">'+
  '<label class=hpick2 title="add to memory"><input type=checkbox class=hsel data-id="'+x.id+'"'+(sel?' checked':'')+'></label>'+
  '<div class=hmain'+(x.has_log?' data-toggle="'+x.id+'"':'')+'>'+
   '<div class=htitle>'+esc(x.summary)+'</div>'+
   '<div class=hmeta2>'+meta+'<span class=hpath>'+esc(x.cwd||'')+'</span></div>'+
  '</div>'+
  '<div class=hacts>'+
   summbtn+
   '<button class="btn resume" data-id="'+x.id+'">\u25B6 RESUME</button>'+
   '<button class="btn resumeff" data-id="'+x.id+'">\U0001F3CE ALLOW-ALL</button>'+
   '<button class="btn kill" data-id="'+x.id+'" data-name="'+esc(x.summary)+'">\u2715 DELETE</button>'+
  '</div>'+
  '<div class=logwrap id="summ-'+x.id+'"'+(exp?'':' style=display:none')+'>'+
   '<div class=summbox id="summbox-'+x.id+'">loading summary\u2026</div></div>'+
  '</div>';
}
function fileList(title,arr,total){
 if(!arr||!arr.length)return'';
 const more=total>arr.length?' <span class=smore>+'+(total-arr.length)+' more</span>':'';
 return '<div class=srow><div class=slbl>'+title+' ('+total+')</div><div class=sfiles>'+
  arr.map(p=>'<code>'+esc(p)+'</code>').join('')+more+'</div></div>';
}
function renderSummary(s){
 if(!s)return'<div class=sempty>No summary available.</div>';
 const stats='<div class=sstats>'+
  '<span>\U0001F5E3 '+s.user_turns+' asks</span>'+
  '<span>\U0001F916 '+s.assistant_turns+' replies</span>'+
  '<span>\U0001F4C4 +'+s.files_created_count+' new</span>'+
  '<span>\u270F '+s.files_edited_count+' edited</span>'+
  '<span>\u2699 '+s.bash_count+' commands</span>'+
  '<span>\u23F1 '+fmtDur(s.duration_seconds)+'</span></div>';
 let html=stats;
 if(s.first_user)html+='<div class=srow><div class=slbl>ORIGINAL ASK</div><div class=sask>'+esc(s.first_user)+'</div></div>';
 if(s.last_assistant)html+='<div class=srow><div class=slbl>OUTCOME \u00B7 LAST MESSAGE</div><div class=sask>'+esc(s.last_assistant)+'</div></div>';
 html+=fileList('CREATED',s.files_created,s.files_created_count);
 html+=fileList('EDITED',s.files_edited,s.files_edited_count);
 if(s.pr_links&&s.pr_links.length)html+='<div class=srow><div class=slbl>PULL REQUESTS</div><div class=sfiles>'+
  s.pr_links.map(u=>'<a href="'+esc(u)+'" target=_blank rel=noopener>'+esc(u)+'</a>').join('')+'</div></div>';
 if(s.bash_sample&&s.bash_sample.length)html+='<div class=srow><div class=slbl>SAMPLE COMMANDS</div><div class=sfiles>'+
  s.bash_sample.map(c=>'<code>'+esc(c)+'</code>').join('')+'</div></div>';
 return html;
}
function renderHistory(){
 const grid=document.getElementById('histgrid');
 const q=histFilter.trim().toLowerCase();
 const items=q?histData.filter(x=>((x.summary||'')+' '+(x.repository||'')+' '+(x.branch||'')+' '+(x.cwd||'')).toLowerCase().includes(q)):histData;
 document.getElementById('histCount').textContent=items.length+' / '+histData.length+' SESSION(S)';
 grid.innerHTML=items.length?items.map(histCard).join(''):'<div class=empty>'+(histData.length?'NO SESSIONS MATCH FILTER':'NO PREVIOUS SESSIONS')+'</div>';
 for(const id of expanded)fetchSummary(id);
 updateSelBar();
}
function updateSelBar(){
 const bar=document.getElementById('selbar');const n=selected.size;
 bar.style.display=n?'flex':'none';
 document.getElementById('selCount').textContent=n+' SELECTED';
 if(document.getElementById('suggbox').style.display!=='none')renderSuggState();
}
let composeData=[];
async function fetchSuggest(q){
 const box=document.getElementById('suggbox');
 box.style.display='block';box.innerHTML='<div class=sempty>finding relevant sessions\u2026</div>';
 try{const r=await(await fetch('/api/suggest?limit=5&q='+encodeURIComponent(q||''))).json();
  renderSugg(r);
 }catch(e){box.innerHTML='<div class=sempty>could not load suggestions</div>';}
}
function renderSugg(r){
 const box=document.getElementById('suggbox');
 const rows=r.sessions||[];
 const head='<div class=suggtitle>'+(r.query?('SUGGESTED FOR: \u201C'+esc(r.query)+'\u201D'+(r.matched?'':' \u2014 no keyword match, showing richest')):'TOP RICH SESSIONS')+
  ' <button id=suggHide class=xbtn>\u2715</button></div>';
 if(!rows.length){box.innerHTML=head+'<div class=sempty>no sessions found</div>';return;}
 box.innerHTML=head+rows.map(x=>{
  const on=selected.has(x.id);
  const why=(x.matched&&x.matched.length)?('matches: '+x.matched.map(esc).join(', ')):(x.size_kb+' KB of activity');
  return '<div class=suggrow>'+
   '<div class=suggmain><div class=suggname>'+esc(x.summary)+'</div>'+
    '<div class=suggwhy>'+why+' \u00B7 '+esc(x.repository||x.cwd||'')+' \u00B7 '+fmtAgeLong(x.age_seconds)+'</div>'+
    (x.first_user?'<div class=suggask>'+esc(x.first_user)+'</div>':'')+'</div>'+
   '<button class="btn '+(on?'log':'resume')+' suggadd" data-id="'+x.id+'">'+(on?'\u2713 ADDED':'\uFF0B ADD')+'</button>'+
  '</div>';
 }).join('');
}
async function openCompose(){
 if(!selected.size){toast('select or add sessions first');return;}
 const modal=document.getElementById('composeModal');modal.style.display='flex';
 const body=document.getElementById('composeBody');body.innerHTML='<div class=sempty>loading context\u2026</div>';
 try{const r=await(await fetch('/api/context?ids='+[...selected].map(encodeURIComponent).join(','))).json();
  composeData=r.sessions||[];renderCompose();
 }catch(e){body.innerHTML='<div class=sempty>could not load context</div>';}
}
function renderCompose(){
 const body=document.getElementById('composeBody');
 if(!composeData.length){body.innerHTML='<div class=sempty>no context found</div>';return;}
 body.innerHTML=composeData.map((s,si)=>
  '<div class=cgroup><div class=cgtitle>'+esc(s.title)+' <span class=cgid>'+s.short+'</span></div>'+
   (s.items.length?s.items.map((it,ii)=>
    '<label class=citem><input type=checkbox class=cchk data-si="'+si+'" data-ii="'+ii+'" checked>'+
     '<span class=citxt><b>'+esc(it.label)+':</b> '+esc(it.text.slice(0,320))+(it.text.length>320?'\u2026':'')+'</span></label>'
   ).join(''):'<div class=sempty>no extractable context</div>')+
  '</div>').join('');
}
async function composeStart(){
 const items=[];
 document.querySelectorAll('#composeBody .cchk').forEach(cb=>{
  if(!cb.checked)return;const s=composeData[cb.dataset.si];const it=s.items[cb.dataset.ii];
  items.push({title:s.title,label:it.label,text:it.text});
 });
 if(!items.length){toast('check at least one context item');return;}
 const first=histData.find(x=>x.id===composeData[0].id);
 const body={items:items,task:document.getElementById('composeTask').value||'',
  allow_all:document.getElementById('composeAllow').checked,cwd:(first&&first.cwd)||''};
 toast('building memory & opening new session\u2026');
 try{const r=await(await fetch('/api/new-session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  toast(r.message||(r.ok?'opening\u2026':'failed'));
  if(r.ok){document.getElementById('composeModal').style.display='none';selected.clear();renderHistory();}
 }catch(e){toast('could not start session');}
}
async function fetchHistory(){
 try{const r=await(await fetch('/api/history')).json();histData=r.sessions||[];renderHistory();}
 catch(e){document.getElementById('histgrid').innerHTML='<div class=empty>MONITOR OFFLINE</div>';}
}
async function fetchSummary(id){
 const box=document.getElementById('summbox-'+id);if(!box)return;
 try{const r=await(await fetch('/api/summary?id='+encodeURIComponent(id))).json();
  box.innerHTML=r.ok?renderSummary(r.summary):'<div class=sempty>'+esc(r.message||'no summary')+'</div>';
 }catch(e){box.innerHTML='<div class=sempty>could not load summary</div>';}
}
async function resumeSession(id,allow){
 try{const r=await(await fetch('/api/resume?id='+encodeURIComponent(id)+(allow?'&allow=1':''))).json();
  toast(r.message||(r.ok?'resuming\u2026':'resume failed'));
 }catch(e){toast('resume failed');}
}
async function deleteSession(id,name){
 if(!confirm('DELETE SESSION?\\n\\n'+name+'\\n\\nRemoves its local state and store record. This cannot be undone.'))return;
 try{const r=await(await fetch('/api/delete?id='+encodeURIComponent(id),{method:'POST'})).json();
  toast(r.message||(r.ok?'deleted':'delete failed'));
  if(r.ok){histData=histData.filter(x=>x.id!==id);expanded.delete(id);renderHistory();}
 }catch(e){toast('delete failed');}
}
function toggleSummary(id){
 if(expanded.has(id))expanded.delete(id);else expanded.add(id);
 const on=expanded.has(id);const wrap=document.getElementById('summ-'+id);
 if(wrap)wrap.style.display=on?'':'none';
 const btn=document.querySelector('.btn.log[data-id="'+id+'"]');
 if(btn)btn.textContent=on?'\u25BE SUMMARY':'\u25B8 SUMMARY';
 const row=document.querySelector('.hrow[data-id="'+id+'"]');if(row)row.classList.toggle('open',on);
 if(on)fetchSummary(id);
}
document.getElementById('histgrid').addEventListener('pointerdown',e=>{
 const l=e.target.closest('.btn.log');if(l){e.preventDefault();toggleSummary(l.dataset.id);return;}
 const hm=e.target.closest('.hmain[data-toggle]');if(hm){e.preventDefault();toggleSummary(hm.dataset.toggle);return;}
 const rf=e.target.closest('.btn.resumeff');if(rf){e.preventDefault();resumeSession(rf.dataset.id,true);return;}
 const r=e.target.closest('.btn.resume');if(r){e.preventDefault();resumeSession(r.dataset.id,false);return;}
 const k=e.target.closest('.btn.kill');if(k){e.preventDefault();deleteSession(k.dataset.id,k.dataset.name);return;}
});
document.getElementById('histSearch').addEventListener('input',e=>{histFilter=e.target.value;renderHistory();});
document.getElementById('histgrid').addEventListener('change',e=>{
 const cb=e.target.closest('.hsel');if(!cb)return;const id=cb.dataset.id;
 if(cb.checked)selected.add(id);else selected.delete(id);
 const panel=cb.closest('.hrow');if(panel)panel.classList.toggle('picked',cb.checked);
 updateSelBar();
});
document.getElementById('selStart').addEventListener('click',openCompose);
document.getElementById('selClear').addEventListener('click',()=>{selected.clear();renderHistory();});
document.getElementById('refreshBtn').addEventListener('click',fetchHistory);
document.getElementById('suggBtn').addEventListener('click',()=>fetchSuggest(document.getElementById('suggQ').value));
document.getElementById('richBtn').addEventListener('click',()=>{document.getElementById('suggQ').value='';fetchSuggest('');});
let suggTimer=null;
document.getElementById('suggQ').addEventListener('input',e=>{
 const v=e.target.value;clearTimeout(suggTimer);
 suggTimer=setTimeout(()=>fetchSuggest(v),250);
});
document.getElementById('suggQ').addEventListener('keydown',e=>{if(e.key==='Enter'){clearTimeout(suggTimer);fetchSuggest(e.target.value);}});
document.getElementById('suggbox').addEventListener('click',e=>{
 if(e.target.closest('#suggHide')){document.getElementById('suggbox').style.display='none';return;}
 const a=e.target.closest('.suggadd');if(!a)return;const id=a.dataset.id;
 if(selected.has(id))selected.delete(id);else selected.add(id);
 renderHistory();renderSuggState();
});
function renderSuggState(){
 document.querySelectorAll('#suggbox .suggadd').forEach(a=>{
  const on=selected.has(a.dataset.id);
  a.textContent=on?'\u2713 ADDED':'\uFF0B ADD';
  a.classList.toggle('resume',!on);a.classList.toggle('log',on);
 });
}
document.getElementById('composeClose').addEventListener('click',()=>{document.getElementById('composeModal').style.display='none';});
document.getElementById('composeModal').addEventListener('click',e=>{if(e.target.id==='composeModal')e.target.style.display='none';});
document.getElementById('composeStart').addEventListener('click',composeStart);
fetchHistory();
fetchSuggest('');
setInterval(fetchHistory,15000);
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

        if path == "/api/summary":
            sid = (qs.get("id") or [""])[0]
            if not sid or not os.path.isdir(os.path.join(SESSION_STATE_DIR, sid)):
                self._json({"ok": False, "message": "unknown session"}, 404)
                return
            summ = build_summary(sid)
            if summ is None:
                self._json({"ok": False, "message": "no activity recorded"}, 404)
                return
            self._json({"ok": True, "summary": summ})
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

        if path == "/api/history":
            self._json({"ok": True, "sessions": history()})
            return

        if path == "/api/suggest":
            q = (qs.get("q") or [""])[0]
            try:
                lim = int((qs.get("limit") or ["5"])[0])
            except ValueError:
                lim = 5
            lim = max(1, min(lim, 15))
            rows, matched = suggest_sessions(q, lim)
            self._json({"ok": True, "query": q, "matched": matched,
                        "sessions": rows})
            return

        if path == "/api/context":
            ids = [x for x in (qs.get("ids") or [""])[0].split(",") if x]
            summaries = load_summaries()
            out = []
            for sid in ids[:20]:
                if not os.path.isdir(os.path.join(SESSION_STATE_DIR, sid)):
                    continue
                out.append({
                    "id": sid,
                    "short": sid[:8],
                    "title": (summaries.get(sid, ("", ""))[0]) or "(untitled session)",
                    "items": context_items(sid),
                })
            self._json({"ok": True, "sessions": out})
            return

        if path == "/history":
            body = HISTORY_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/resume":
            sid = (qs.get("id") or [""])[0]
            allow = (qs.get("allow") or ["0"])[0] in ("1", "true", "yes")
            row = None
            for r in load_sessions_full():
                if r["id"] == sid:
                    row = r
                    break
            if not row and not os.path.isdir(os.path.join(SESSION_STATE_DIR, sid)):
                self._json({"ok": False, "message": "unknown session"}, 404)
                return
            ok, msg = resume_session(sid, (row or {}).get("cwd", ""), allow)
            self._json({"ok": ok, "message": msg})
            return

        body = INDEX_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)

        if path == "/api/reply":
            sid = (qs.get("id") or [""])[0]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                payload = json.loads(raw or b"{}")
            except Exception:
                payload = {}
            msg = ""
            if isinstance(payload, dict):
                msg = (payload.get("message") or "").strip()
            # Reply is typed into the agent's terminal + Enter, which submits it
            # to Copilot CLI as one message, so collapse newlines/tabs to spaces
            # (they would otherwise break AppleScript or submit early).
            msg = msg.replace("\r", " ").replace("\n", " ").replace("\t", " ")
            a = self._agent_by_id(sid)
            if not a:
                self._json({"ok": False, "message": "agent not found"}, 404)
                return
            if not msg:
                self._json({"ok": False, "message": "empty reply"}, 400)
                return
            ok, m = autopilot_terminal(a.get("tty"), a.get("term_app"),
                                       a.get("term_kind"), cmd=msg)
            self._json({"ok": ok, "message": m})
            return

        if path == "/api/delete":
            sid = (qs.get("id") or [""])[0]
            if not sid:
                self._json({"ok": False, "message": "missing id"}, 400)
                return
            ok, m = delete_session(sid)
            self._json({"ok": ok, "message": m})
            return

        if path == "/api/new-session":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                payload = json.loads(raw or b"{}")
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            ids = payload.get("session_ids") or []
            if not isinstance(ids, list):
                ids = []
            ids = [str(x) for x in ids][:20]
            items = payload.get("items") or []
            if not isinstance(items, list):
                items = []
            items = items[:100]
            cwd = (payload.get("cwd") or "").strip()
            task = (payload.get("task") or "").strip()
            allow = bool(payload.get("allow_all"))
            ok, m = new_session(ids, cwd, task, allow, items=items)
            self._json({"ok": ok, "message": m})
            return

        self._json({"ok": False, "message": "not found"}, 404)


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
