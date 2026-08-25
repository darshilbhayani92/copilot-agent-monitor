# Copilot Agent Monitor

A **local, read-only** dashboard that watches every running GitHub Copilot CLI
session ("agent") on this machine and highlights the ones that are **waiting for
you**.

It shows the same live view in two places at once:

- **Terminal** — an auto-refreshing table (rings the bell when an agent starts
  waiting on you).
- **Web** — `http://localhost:8787`, with pulsing red cards, a top banner, a
  sound, and a browser notification the moment an agent needs you.

## States detected

| State | Meaning |
|-------|---------|
| ⛔ `WAITING_PERMISSION` | Blocked on an approval prompt (e.g. a shell command). |
| ❓ `WAITING_QUESTION`   | Blocked on an `ask_user` question. |
| ⏸ `WAITING_INPUT`      | Finished its turn — awaiting your next message. |
| ▶ `WORKING`            | Actively running tools / thinking. |

The first three all count as **"waiting on you"** and are pushed to the top,
highlighted, and alerted.

## Actions (web dashboard)

Each agent card has two buttons:

- **↪ Focus terminal** — jumps you straight to the terminal tab/window running
  that agent so you can type your answer.
  - **iTerm2 / Terminal.app:** selects the *exact* tab by matching its TTY and
    brings it to the front.
  - **VS Code / Cursor:** their integrated terminal has no per-tab AppleScript
    API, so the app is activated and the card shows the TTY (e.g. `/dev/ttys011`)
    — run `tty` in a tab to find the match.
- **■ Kill** — sends `SIGTERM` to that agent's process (with a confirm prompt).

The card meta line shows the hosting app + TTY and the working directory.

> **Can I answer the prompt directly in the dashboard?** No. Each Copilot CLI
> agent reads input from its own interactive terminal; there is no supported API
> to inject a message from outside. The **Focus terminal** button is the safe,
> supported way to get to the right tab instantly.

## Run it

```bash
cd ~/copilot-agent-monitor
./watch.sh                # terminal + web (recommended)
./watch.sh --no-web       # terminal only
./watch.sh --no-terminal  # web only (good for background)
MONITOR_PORT=9000 ./watch.sh
```

Then open <http://localhost:8787> in a browser. Press `Ctrl+C` to stop.


## Start automatically at login (macOS)

Install as a LaunchAgent so the web dashboard runs in the background from login:

```bash
./launchd/install-login-startup.sh            # install & start
MONITOR_PORT=9000 ./launchd/install-login-startup.sh   # custom port
./launchd/install-login-startup.sh uninstall  # stop & remove
```

The script generates `~/Library/LaunchAgents/com.<you>.copilot-agent-monitor.plist`
pointing at this repo's `monitor.py`, loads it with `launchctl`, and logs to
`/tmp/copilot-monitor.log`. A sample plist is in `launchd/`.

## How it works

It only **reads** local Copilot state under `~/.copilot`:

- discovers live sessions via `~/.copilot/session-state/<id>/inuse.<pid>.lock`
  (and verifies the PID is alive),
- tails each session's `events.jsonl` to derive the current state from
  `permission.requested/completed`, `ask_user` tool calls, and turn events,
- reads session titles from `~/.copilot/session-store.db` (read-only).

It never modifies any session, makes no network calls, and contains no
third-party code — consistent with LinkedIn's Copilot CLI usage policy
(`go/ai-dev-safely`).
