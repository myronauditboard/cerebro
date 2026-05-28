"""Drive the user's terminal app (focus an existing window, or open a new one).

Used by the Agents tab's `o` keybind. Reads `$TERM_PROGRAM` and dispatches
to the matching AppleScript. iTerm2 and Apple_Terminal are fully supported
— `cmd` is auto-typed in the new window. Unknown terminals fall back to
`open -a $TERM_PROGRAM <cwd>`, which at least lands you in the right
directory.
"""

from __future__ import annotations

import os
import shlex
import subprocess


TERM_PROGRAM = os.environ.get("TERM_PROGRAM", "")


# ── AppleScript templates ──────────────────────────────────────────────────
# {tty}, {cmd_quoted}, {cwd_quoted} are filled in via .format().

_FOCUS_ITERM2 = '''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "{tty}" then
          tell w to select
          tell t to select
          tell s to select
          activate
          return "focused"
        end if
      end repeat
    end repeat
  end repeat
  return "not_found"
end tell
'''

_FOCUS_TERMINAL = '''
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is "{tty}" then
        set selected of t to true
        set frontmost of w to true
        activate
        return "focused"
      end if
    end repeat
  end repeat
  return "not_found"
end tell
'''

_OPEN_ITERM2 = '''
tell application "iTerm2"
  activate
  set newWin to (create window with default profile)
  tell current session of newWin to write text {cmd_quoted}
end tell
'''

_OPEN_TERMINAL = '''
tell application "Terminal"
  activate
  do script {cmd_quoted}
end tell
'''


def _run_osa(script: str) -> str | None:
    """Run an AppleScript via osascript. Returns stdout (stripped) or None on
    failure. 5-second hard timeout so a stuck terminal doesn't hang cerebro."""
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip()


def _osa_string_literal(s: str) -> str:
    """Quote `s` for embedding inside an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def focus_tty(tty: str) -> bool:
    """Bring the terminal window/tab owning `tty` to the front.

    Returns True if focus succeeded. Tries the script matching $TERM_PROGRAM
    first, then falls back to the other known app — useful when cerebro is
    running in one terminal and the agent's window is in another.
    """
    if not tty:
        return False
    # Normalize tty path: ps reports "ttys020", AppleScript checks
    # "/dev/ttys020".
    target = tty if tty.startswith("/dev/") else f"/dev/{tty}"

    candidates: list[str] = []
    if TERM_PROGRAM == "iTerm.app":
        candidates = [_FOCUS_ITERM2, _FOCUS_TERMINAL]
    elif TERM_PROGRAM == "Apple_Terminal":
        candidates = [_FOCUS_TERMINAL, _FOCUS_ITERM2]
    else:
        # Unknown — try both, best-effort. Either may not be running and
        # AppleScript will just refuse, which we catch as "no" below.
        candidates = [_FOCUS_ITERM2, _FOCUS_TERMINAL]

    for script in candidates:
        result = _run_osa(script.format(tty=target))
        if result == "focused":
            return True
    return False


def open_at(cwd: str, command: str | None = None) -> bool:
    """Open a new terminal window at `cwd` and optionally run `command` in it.

    For iTerm2 / Terminal.app we use AppleScript to auto-type the command.
    For unknown terminals we fall back to `open -a $TERM_PROGRAM <cwd>`,
    which opens at the right directory but doesn't pre-type the command.
    """
    if not cwd:
        return False
    if command:
        full_cmd = f"cd {shlex.quote(cwd)} && {command}"
    else:
        full_cmd = f"cd {shlex.quote(cwd)}"

    if TERM_PROGRAM == "iTerm.app":
        script = _OPEN_ITERM2.format(cmd_quoted=_osa_string_literal(full_cmd))
        return _run_osa(script) is not None
    if TERM_PROGRAM == "Apple_Terminal":
        script = _OPEN_TERMINAL.format(cmd_quoted=_osa_string_literal(full_cmd))
        return _run_osa(script) is not None

    # Unknown terminal: at least pop a window at the cwd. The user will have
    # to type the claude command themselves.
    app = TERM_PROGRAM or "Terminal"
    try:
        r = subprocess.run(
            ["open", "-a", app, cwd],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
