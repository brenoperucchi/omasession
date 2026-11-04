"""Keep a log of every restore that really ran, with what explains it.

`last-restore.log` used to be the only trace, and every restore overwrote it:
the login restore of 2026-10-31 came back with empty Chrome windows and bare
terminals, and a manual restore sixteen minutes later had already erased what
the login one did. The lab cannot stand in for this -- it runs on clean
profiles and shuts down its own way -- so the evidence has to come from the
machine where it went wrong.

Each restore now writes `restores/<stamp>-<trigger>.log` next to a copy of the
session it replayed, and the newest KEEP of them stay. `last-restore.log` is
still written, as the copy of the latest one. Nothing here may fail a restore:
a log that cannot be written is reported once on stderr and dropped.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote, urlencode

from safe_fs import Refused, TooLarge, open_dir_chain, read_bytes, read_capped_path, write_bytes

KEEP = 20
DIR = "restores"
LAST = "last-restore.log"
TRIGGERS = ("login", "manual", "app")
STEM = r"\d{8}-\d{6}-[a-z]+(?:-\d+)?"
ENTRY = re.compile(rf"^({STEM})\.(log|session\.toml|titles\.json|result\.json)$")
RESULT = "result.json"
JOURNAL_TIMEOUT = 5
# How far back from the previous boot's last entry to look for the shutdown.
# Bounding the search keeps it to milliseconds: an unbounded pattern search
# that finds nothing scans the whole boot (8.5 s measured on an 11-day boot),
# and the login restore still holds the session lock while this runs.
SHUTDOWN_WINDOW = 600
TIMED_OUT = object()
ROOT = Path(__file__).resolve().parents[1]


def trigger_from_env() -> str:
    trigger = os.environ.get("OMASESSION_RESTORE_TRIGGER", "manual")
    return trigger if trigger in TRIGGERS else "manual"


def free_stem(dir_fd: int, now: float, trigger: str) -> str:
    """A name no earlier restore holds; two restores in one second both stay.

    The name only has to be unique; what came first is decided by when each
    log was written (see newest_first), since a `manual` and an `app` restore
    in the same second would sort by their trigger, not by their order.
    """
    base = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{trigger}"
    # After the highest stem already taken, never in a gap pruning left.
    taken = 0
    for name in os.listdir(dir_fd):
        m = ENTRY.match(name)
        if not m or not m.group(1).startswith(base):
            continue
        rest = m.group(1)[len(base):]
        if rest == "":
            taken = max(taken, 1)
        elif rest[1:].isdigit() and rest[0] == "-":
            taken = max(taken, int(rest[1:]))
    return base if taken == 0 else f"{base}-{taken + 1:02d}"


def by_age(dir_fd: int) -> list[tuple[str, list[str]]]:
    """Restores oldest first, each with its files.

    Ordered by when the log was last written, not by name. Restores never
    overlap (they hold the session lock), so the later one is always written
    last; a stem without its log sorts as the oldest.
    """
    groups: dict[str, list[str]] = {}
    for name in os.listdir(dir_fd):
        m = ENTRY.match(name)
        if m:
            groups.setdefault(m.group(1), []).append(name)

    def written(stem: str) -> tuple[int, str]:
        try:
            return os.stat(f"{stem}.log", dir_fd=dir_fd, follow_symlinks=False).st_mtime_ns, stem
        except OSError:
            return 0, stem

    return [(stem, groups[stem]) for stem in sorted(groups, key=written)]


def prune(dir_fd: int, keep: int = KEEP) -> None:
    """Drop all but the newest `keep` restores, each with its session copy."""
    aged = by_age(dir_fd)
    for _stem, names in aged[:-keep] if keep > 0 else aged:
        for name in names:
            try:
                os.unlink(name, dir_fd=dir_fd)
            except OSError:
                # A cleanup that cannot finish costs nothing but disk.
                pass


class RestoreLog:
    """Publishes the restore's output to its own file and to last-restore.log."""

    def __init__(self, state_dir: Path, trigger: str, now: float | None = None,
                 keep: int = KEEP) -> None:
        self.trigger = trigger
        self.state_fd: int | None = None
        self.dir_fd: int | None = None
        self.stem: str | None = None
        self.failed = False
        self.keep = keep
        self.started = time.time() if now is None else now
        self.pruned = False
        try:
            self.state_fd = open_dir_chain(str(state_dir), create=True)
            self.dir_fd = open_dir_chain(str(state_dir / DIR), create=True)
            self.stem = free_stem(self.dir_fd, self.started, trigger)
        except (OSError, Refused, TooLarge) as exc:
            self._disable(f"restore log unavailable: {exc}")

    @property
    def name(self) -> str | None:
        return f"{DIR}/{self.stem}.log" if self.stem else None

    def _disable(self, why: str) -> None:
        if not self.failed:
            sys.__stderr__.write(f"[omasession] {why}\n")
            sys.__stderr__.flush()
        self.failed = True
        self.close()

    def publish(self, text: str) -> None:
        if self.failed or self.state_fd is None:
            return
        data = text.encode()
        try:
            if self.dir_fd is not None and self.stem:
                write_bytes(self.dir_fd, f"{self.stem}.log", data, mode=0o600)
                if not self.pruned:
                    # After the first write, so the new log counts as one kept.
                    # Outside what disables the log: a failed cleanup must not
                    # cost the restore its log.
                    self.pruned = True
                    try:
                        prune(self.dir_fd, self.keep)
                    except OSError as exc:
                        sys.__stderr__.write(f"[omasession] restore log cleanup skipped: {exc}\n")
            write_bytes(self.state_fd, LAST, data, mode=0o600)
        except (OSError, Refused, TooLarge) as exc:
            self._disable(f"restore log disabled: {exc}")

    def keep_session(self, toml: Path, sidecar: Path | None) -> None:
        """Copy the session this restore replayed, so the log can be read
        against what it was asked to do even after `last` moves on."""
        if self.failed or self.dir_fd is None or not self.stem:
            return
        for src, suffix in ((toml, "session.toml"), (sidecar, "titles.json")):
            if src is None:
                continue
            try:
                write_bytes(self.dir_fd, f"{self.stem}.{suffix}",
                            read_capped_path(src), mode=0o600)
            except (OSError, Refused, TooLarge) as exc:
                print(f"  ! could not keep a copy of {src.name}: {exc}")

    def finish(self, rc: int | None, text: str) -> None:
        """Write what the panel's Log tab lists: the outcome and the lines
        worth reading first. A restore without one never finished."""
        if self.failed or self.dir_fd is None or not self.stem:
            return
        summary = summarize(text, rc, self.trigger, self.started, time.time())
        try:
            write_bytes(self.dir_fd, f"{self.stem}.{RESULT}",
                        (json.dumps(summary, indent=1) + "\n").encode(), mode=0o600)
        except (OSError, Refused, TooLarge) as exc:
            self._disable(f"restore result not written: {exc}")

    def close(self) -> None:
        for attr in ("dir_fd", "state_fd"):
            fd = getattr(self, attr)
            if fd is not None:
                os.close(fd)
                setattr(self, attr, None)


def _first_journal_line(args: list[str]):
    """The newest matching journal line: "" if nothing matches, None if the
    journal cannot be read, TIMED_OUT if it did not answer in time."""
    try:
        out = subprocess.run(["journalctl", "--no-pager", "-q", "-r", "-n", "1", *args],
                             capture_output=True, text=True, timeout=JOURNAL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return TIMED_OUT
    except (OSError, subprocess.SubprocessError):
        return None
    lines = out.stdout.strip().splitlines()
    if out.returncode == 0:
        return lines[0] if lines else ""
    # `journalctl -g` exits 1 when nothing matches; anything else, or 1 with
    # an error and no output, is a journal we could not read.
    if out.returncode == 1 and not lines and not out.stderr.strip():
        return ""
    return None


def _show(found, missing: str) -> str:
    if found is TIMED_OUT:
        return f"gave up after {JOURNAL_TIMEOUT}s"
    if found is None:
        return "journal unreadable"
    return found or missing


def previous_boot_lines(journal=_first_journal_line) -> list[str]:
    """How the previous boot ended, from the journal.

    Omarchy's menu shutdown closes windows one by one first, and that is what
    leaves Chrome with a single window (docs/plans/008); `systemctl poweroff`
    does not, and a crash leaves neither record.
    """
    last = journal(["-b", "-1", "-o", "short-unix"])
    if last is TIMED_OUT or last is None or not last:
        return [f"previous boot: {_show(last, 'no entries')}"]
    try:
        end = float(last.split(" ", 1)[0])
    except ValueError:
        return [f"previous boot: unexpected journal line {last[:60]!r}"]
    since = ["--since", f"@{int(end) - SHUTDOWN_WINDOW}", "-o", "short-iso"]
    menu = journal(["--user", "-b", "-1", *since, "-g",
                    "omarchy-system-(shutdown|reboot|logout)"])
    logind = journal(["-b", "-1", *since, "-u", "systemd-logind", "-g",
                      "(poweroff|reboot|halt) requested|Power key"])
    if logind == "" and journal(["-b", "-1", "-u", "systemd-logind"]) == "":
        # Every boot logs logind starting. `-q` hides the warning about a
        # system journal this user cannot read, so seeing none at all means
        # the search above was blind, not that nobody asked for a shutdown.
        logind = None
    return [f"previous boot: last entry {_iso(end)}",
            f"  omarchy menu: {_show(menu, 'no record')}",
            f"  logind: {_show(logind, 'no shutdown request (crash or power loss?)')}"]


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return "?"


def _version() -> str:
    try:
        return json.loads((ROOT / "manifest.json").read_text()).get("version", "?")
    except (OSError, ValueError):
        return "?"


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(ts))


def context_lines(trigger: str, session: Path, windows: list[dict], state_dir: Path,
                  log_name: str | None = None, asked: Path | None = None) -> list[str]:
    """The header that lets a log be read without the machine it came from.

    Window classes, workspaces and counts only: no titles, no URLs.
    """
    lines = [f"trigger: {trigger}", f"omasession: {_version()}"]
    if log_name:
        lines.append(f"log: {log_name}")
    uptime = _read("/proc/uptime").split(" ", 1)[0]
    try:
        up = f"{float(uptime):.0f}s"
    except ValueError:
        up = "?"
    lines.append(f"boot: {_read('/proc/sys/kernel/random/boot_id')}, up {up}")
    lines.append(f"hyprland: {os.environ.get('HYPRLAND_INSTANCE_SIGNATURE', '?')}")
    try:
        saved = _iso(session.stat().st_mtime)
    except OSError:
        saved = "?"
    classes = Counter(str(w.get("app_id", "?")) for w in windows)
    summary = ", ".join(f"{n} {app}" for app, n in classes.most_common())
    lines.append(f"session: {session} (saved {saved}; {len(windows)} window(s): {summary})")
    if asked is not None and asked != session:
        lines.append(f"  fallback: {asked.name} was not a consistent pair")
    try:
        last = json.loads((state_dir / "last-save.json").read_text())
        lines.append(f"last save: {last.get('when', '?')} rc={last.get('rc', '?')} "
                     f"{last.get('message', '')}".rstrip())
    except (OSError, ValueError, AttributeError):
        lines.append("last save: unknown")
    env = sorted((k, v) for k, v in os.environ.items() if k.startswith("OMASESSION_"))
    if env:
        lines.append("settings: " + " ".join(f"{k[len('OMASESSION_'):].lower()}={v}"
                                             for k, v in env))
    return lines


def profile_lines(prefs: Path, sessions: Path, window_count) -> list[str]:
    """What a browser profile holds before the restore touches it.

    exit_type is the field arm_browser_profile rewrites, and the session files
    are what repair_sessions edits; seeing both as they were is what tells a
    browser that never got its session apart from one we failed to repair.
    """
    lines = []
    try:
        profile = json.loads(prefs.read_text()).get("profile", {})
        lines.append(f"exit_type={profile.get('exit_type', '?')} "
                     f"exited_cleanly={profile.get('exited_cleanly', '?')}")
    except (OSError, ValueError, AttributeError) as exc:
        lines.append(f"Preferences unreadable: {type(exc).__name__}")
    try:
        files = sorted(p for p in sessions.iterdir() if p.is_file())
    except OSError:
        files = []
    described = []
    for p in files:
        # The browser may rotate a file between the listing and this stat.
        try:
            st = p.stat()
        except OSError:
            described.append(f"{p.name} (gone)")
            continue
        described.append(f"{p.name} ({st.st_size} B, "
                         f"{time.strftime('%m-%d %H:%M:%S', time.localtime(st.st_mtime))})")
    lines.append("Sessions: " + (", ".join(described) if described else "none"))
    try:
        lines.append(f"windows in the newest session: {window_count(sessions)}")
    except Exception as exc:  # a parse error is a finding, not a failure
        lines.append(f"windows in the newest session: unreadable ({type(exc).__name__})")
    return lines


# ── what the panel lists, and what a bug report carries ─────────────────────
MAX_LINES = 12
ISSUES_URL = "https://github.com/brenoperucchi/omasession/issues/new"
# GitHub refuses a new-issue link much past 8 KB; leave room for the title.
MAX_URL = 7000
PLACED = re.compile(r"^(\d+)/(\d+) placed in ")
NO_WINDOW = re.compile(r"^\[\d+/\d+\] \S+: no window after ")
TERMINAL = re.compile(r"^\[\d+/\d+\] \S+: .*  \((cwd |no cwd saved)")
# A profile folder name can hold a space: Chrome's second one is "Profile 1".
PROFILE = re.compile(r"^  [^:]+: (exit_type=|windows in the newest session:)")
EPILOGUE = ("previous boot:", "  omarchy menu:", "  logind:", "  fallback:")


def _capped(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_LINES:
        return lines
    return lines[:MAX_LINES - 1] + [f"… and {len(lines) - MAX_LINES + 1} more in the log"]


def summarize(text: str, rc: int | None, trigger: str, started: float,
              finished: float) -> dict:
    """The outcome of one restore, and the log lines worth reading first.

    `placed` counts windows the restore put somewhere; it cannot see what is
    inside them, so a Chrome window that came back without its tabs still
    counts. That is why every restore can be reported, not only failed ones.
    """
    lines = text.splitlines()
    problems: list[str] = []
    notes: list[str] = []
    placed = total = None
    nothing = False
    terminals = bare = gone = no_cwd = 0
    for i, line in enumerate(lines):
        m = PLACED.match(line)
        if m:
            placed, total = int(m.group(1)), int(m.group(2))
        elif NO_WINDOW.match(line) or line.lstrip().startswith("! "):
            problems.append(line.strip())
        elif line.startswith("Traceback ("):
            # The exception itself is the first unindented line after it.
            raised = next((x for x in lines[i + 1:] if x and not x.startswith(" ")), "?")
            problems.append(f"crashed: {raised.strip()}")
        elif line.startswith("nothing to restore") or line.startswith("no saved windows for"):
            nothing = True
            notes.append(line.strip())
        elif PROFILE.match(line) or line.startswith(EPILOGUE):
            notes.append(line.strip())
        if TERMINAL.match(line):
            terminals += 1
            bare += "no program saved" in line
            gone += "(gone)" in line
            no_cwd += "no cwd saved" in line
    if bare or gone or no_cwd:
        notes.append(f"terminals: {terminals}, {bare} with no program saved, "
                     f"{gone} whose folder is gone, {no_cwd} with no folder saved")
    if rc is None:
        result = "interrupted"
    elif nothing and not problems:
        # A session that could not be read also ends in "nothing to restore",
        # after a `! <file>: ...` line; that one failed.
        result = "nothing"
    elif rc == 0:
        result = "ok"
    elif rc == 2:
        result = "partial"
    else:
        result = "failed"
    return {"trigger": trigger, "started": int(started), "finished": int(finished),
            "rc": rc, "result": result, "placed": placed, "total": total,
            "problems": _capped(problems), "notes": _capped(notes)}


def _entry(dir_fd: int, stem: str, path: Path) -> dict:
    entry: dict = {}
    try:
        data = read_bytes(dir_fd, f"{stem}.{RESULT}")
        loaded = json.loads(data) if data else None
        if isinstance(loaded, dict):
            entry = loaded
    except (OSError, Refused, TooLarge, ValueError):
        pass
    if not entry:
        # Still running, or killed before it could finish (SIGKILL, power).
        try:
            started = time.mktime(time.strptime(stem[:15], "%Y%m%d-%H%M%S"))
        except ValueError:
            started = 0
        entry = {"result": "unfinished", "started": int(started), "problems": [], "notes": []}
    entry["trigger"] = stem.split("-")[2]
    entry["name"] = stem
    entry["log"] = str(path / f"{stem}.log")
    return entry


def entries(state_dir: Path) -> list[dict]:
    """Every kept restore, newest first."""
    path = state_dir / DIR
    try:
        dir_fd = open_dir_chain(str(path), create=False)
    except (OSError, Refused):
        return []
    try:
        return [_entry(dir_fd, stem, path) for stem, names in reversed(by_age(dir_fd))
                if f"{stem}.log" in names]
    finally:
        os.close(dir_fd)


def _redact(line: str) -> str:
    home = str(Path.home())
    return line.replace(home, "~") if home not in ("", "/") else line


# What a bug report may carry. The panel shows every flagged line, but some of
# them end with a browser window's title ("! extra window the browser
# reopened: <title>") or a folder ("(cwd ~/Devs/<project>; runs btop)"), so the
# issue takes only the formats known to hold neither, rewrites the few whose
# diagnosis is worth keeping, and counts the rest. Every pattern is anchored at
# both ends: a new `! ...` line in the replay, or a longer form of a known one,
# stays out until it is added here on purpose.
SHAREABLE_PROBLEMS = (
    re.compile(r"^! \S+ reopened nothing$"),
    re.compile(r"^! no profile for \S+, falling back to per-window launch$"),
    re.compile(r"^! \S+ is running; not touching its Preferences\. "
               r"Close it first or its tabs will not come back\.$"),
    re.compile(r"^… and \d+ more in the log$"),
)
SHAREABLE_NOTES = (
    # A profile folder name ("Default", "Profile 1"), then a fixed format.
    re.compile(r"^[^:]+: (exit_type=\S+ exited_cleanly=\S+"
               r"|windows in the newest session: (\d+|unreadable \(\w+\)))$"),
    re.compile(r"^terminals: \d+, \d+ with no program saved, \d+ whose folder is gone, "
               r"\d+ with no folder saved$"),
    re.compile(r"^previous boot: (last entry .*|no entries|journal unreadable|gave up after \d+s)$"),
    re.compile(r"^(nothing to restore(: all saved windows excluded by appRestoreGhostty=false)?"
               r"|no saved windows for \S+)$"),
    re.compile(r"^… and \d+ more in the log$"),
)
# Replay lines that end with a browser window's title: the diagnosis is kept,
# the title is not.
TITLED_PROBLEMS = re.compile(r"^(! (?:leaving unmatched (?:blank|live) browser window"
                             r"|extra blank browser window:|extra window the browser reopened:)) .*$")
# Replay lines that end with an error message, which can name any file: the
# step that failed is kept, the message is not.
ARM_ERROR = re.compile(r"^(! could not arm \S+ profile): .*$")
COPY_ERROR = re.compile(r"^! could not keep a copy of ([^:]+): .*$")
FALLBACK_NOTE = re.compile(r"^fallback: (\S+) was not a consistent pair$")
# A session file keeps its name in the issue only when it is the default one:
# `omasession restore <name>` replays a session the person named, and that
# name can be a project's or a customer's.
DEFAULT_SESSION_FILE = re.compile(r"^last(\.prev)?\.(toml|titles\.json)$")
# A terminal that never opened: "[i/n] app: no window after Ns", then what the
# save recorded for it, "(cwd <folder>[ (gone)]; runs <program>)" or "(no cwd
# saved; no program saved)". The folder and the program are left out, since
# either can name a project or a customer; whether each was saved stays.
TERMINAL_PROBLEM = re.compile(r"^(\[\d+/\d+\] \S+: no window after \d+s)(?:  \((.*)\))?$")
# A journal line names the machine and then carries free text: "<time> <host>
# <unit>[pid]: <message>". The issue keeps the time and what the search
# matched, never the rest.
JOURNAL_NOTE = re.compile(r"^(omarchy menu|logind): (\d{4}-\d\d-\d\dT\S+) \S+ (.*)$")
JOURNAL_MATCH = {
    "omarchy menu": re.compile(r"omarchy-system-(shutdown|reboot|logout)"),
    "logind": re.compile(r"(poweroff|reboot|halt) requested|Power key"),
}
# What the same two notes say when the journal had nothing to match.
JOURNAL_EMPTY = re.compile(r"^(omarchy menu|logind): (no record|no entries|journal unreadable"
                           r"|gave up after \d+s|no shutdown request \(crash or power loss\?\))$")


def _session_file(name: str) -> str:
    return name if DEFAULT_SESSION_FILE.match(name) else "<session file>"


def _shareable_terminal(head: str, detail: str | None) -> str:
    """A failed terminal's line without the folder it was saved in."""
    if detail is None:
        return head
    where, sep, program = detail.rpartition("; ")
    if not sep:
        return head
    if where == "no cwd saved":
        shown = where
    elif where.startswith("cwd "):
        shown = "folder left out" + (", gone" if where.endswith(" (gone)") else "")
    else:
        return head
    if program.startswith("runs ") and program[len("runs "):]:
        program = "program left out"
    elif program != "no program saved":
        return head
    return f"{head}  ({shown}; {program})"


def _shareable(problems: list, notes: list) -> tuple[list[str], list[str], int]:
    """The lines a bug report may carry, and how many were left out."""
    kept_problems, kept_notes, left_out = [], [], 0
    for line in map(str, problems or []):
        terminal = TERMINAL_PROBLEM.match(line)
        if line.startswith("crashed: "):
            # The exception's message can hold anything; its type cannot.
            kept_problems.append("crashed: " + line[len("crashed: "):].split(":", 1)[0])
        elif terminal:
            kept_problems.append(_shareable_terminal(terminal.group(1), terminal.group(2)))
        elif ARM_ERROR.match(line):
            kept_problems.append(ARM_ERROR.sub(r"\1: <details left out>", line))
        elif COPY_ERROR.match(line):
            name = _session_file(COPY_ERROR.match(line).group(1))
            kept_problems.append(f"! could not keep a copy of {name}: <details left out>")
        elif any(rx.match(line) for rx in SHAREABLE_PROBLEMS):
            kept_problems.append(line)
        elif TITLED_PROBLEMS.match(line):
            kept_problems.append(TITLED_PROBLEMS.sub(r"\1 <title left out>", line))
        else:
            left_out += 1
    for line in map(str, notes or []):
        journal = JOURNAL_NOTE.match(line)
        fallback = FALLBACK_NOTE.match(line)
        if journal:
            found = JOURNAL_MATCH[journal.group(1)].search(journal.group(3))
            kept_notes.append(f"{journal.group(1)}: {journal.group(2)} "
                              + (found.group(0) if found else "<details left out>"))
        elif JOURNAL_EMPTY.match(line):
            kept_notes.append(line)
        elif fallback:
            kept_notes.append(f"fallback: {_session_file(fallback.group(1))} was not a consistent pair")
        elif any(rx.match(line) for rx in SHAREABLE_NOTES):
            kept_notes.append(line)
        else:
            left_out += 1
    return kept_problems, kept_notes, left_out


def issue(entry: dict, version: str, report_dir: str) -> tuple[str, str]:
    """A new-issue link with this restore filled in, and the body it carries.

    Counts, outcomes and the flagged lines whose format holds no window
    title, with the home folder written as ~; the log and the session copy
    stay in the report folder, because they hold window titles and folder
    names the person has to look at first.
    """
    problems, notes, left_out = _shareable(entry.get("problems"), entry.get("notes"))
    trigger = str(entry.get("trigger", "?"))
    result = str(entry.get("result", "?"))
    score = (f"{entry['placed']}/{entry['total']} placed"
             if entry.get("placed") is not None and entry.get("total") is not None else "")
    title = f"{trigger.capitalize()} restore: {result}" + (f", {score}" if score else "")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.get("started") or 0))

    def block(label: str, lines: list) -> list[str]:
        lines = [_redact(str(x)) for x in lines or []]
        return [f"{label}:", "```text", *lines, "```", ""] if lines else []

    def body(with_notes: bool, problems: list) -> str:
        return "\n".join([
            "### What came back wrong",
            "",
            "<!-- Which windows, on which workspace, and what was missing in them. -->",
            "",
            "### The restore",
            "",
            f"- omasession {version}, {trigger} restore at {when}",
            f"- result: {result}" + (f" ({score})" if score else ""),
            "",
            *block("Problems the restore saw", problems),
            *(block("Notes", notes) if with_notes else []),
            *([f"{left_out} other flagged line(s) are only in the log: they can hold window titles "
                "or folder names.", ""]
              if left_out else []),
            f"The full log and a copy of the session are in `{_redact(report_dir)}`. "
            "They hold window titles and folder names, so attach only what you are fine sharing.",
            "",
        ])

    text = body(True, problems)
    url = f"{ISSUES_URL}?{urlencode({'title': title, 'body': text}, quote_via=quote)}"
    if len(url) > MAX_URL:
        text = body(False, problems)
        url = f"{ISSUES_URL}?{urlencode({'title': title, 'body': text}, quote_via=quote)}"
    while len(url) > MAX_URL and problems:
        problems = problems[:-1]
        text = body(False, problems + ["… more in the log"])
        url = f"{ISSUES_URL}?{urlencode({'title': title, 'body': text}, quote_via=quote)}"
    return url, text


def _text_row(entry: dict) -> str:
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.get("started") or 0))
    score = (f" {entry['placed']}/{entry['total']} placed"
             if entry.get("placed") is not None and entry.get("total") is not None else "")
    flagged = f", {len(entry.get('problems') or [])} problem line(s)" if entry.get("problems") else ""
    return f"{when}  {entry.get('trigger', '?'):6}  {entry.get('result', '?')}{score}{flagged}  {entry['name']}"


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "list":
        found = entries(Path(argv[1]))
        if "--json" in argv[2:]:
            print(json.dumps({"restores": found}))
        elif not found:
            print("no restore logged yet")
        else:
            for entry in found:
                print(_text_row(entry))
        return 0
    if len(argv) == 4 and argv[0] == "issue":
        state_dir, name, out = Path(argv[1]), argv[2], Path(argv[3])
        match = next((e for e in entries(state_dir) if e["name"] == name), None)
        if match is None:
            print(f"no restore named {name}", file=sys.stderr)
            return 1
        url, text = issue(match, _version(), str(out))
        (out / "issue.md").write_text(text)
        os.chmod(out / "issue.md", 0o600)
        print(url)
        return 0
    print("usage: restore_log.py list <state_dir> [--json] | issue <state_dir> <name> <dir>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
