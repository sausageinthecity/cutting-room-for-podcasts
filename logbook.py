"""
The line's paperwork (Module 4.5).

- logs/<date>_<time>.log: everything one run printed, minus the download progress bars.
  The last 30 are kept.
- logs/runs.jsonl: one line per run with what it made and what went wrong, so the
  morning sheet can cover every run in the last day, not just the latest one.
- logs/morning.txt: the 10-second sheet. New clean episodes from the last 24 hours,
  the latest run's problems with what to do about each, and any Claude login warning.

Nothing written here may contain a web link or an email address. Every line passes
through scrub() on its way into a file, as a last guard behind the stations' own care.
logs/ is git-ignored.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import fetch

PROJECT = Path(__file__).resolve().parent
LOG_DIR = PROJECT / "logs"
RUNS_FILE = LOG_DIR / "runs.jsonl"
MORNING_FILE = LOG_DIR / "morning.txt"
KEEP_LOGS = 30
SHEET_HOURS = 24          # the morning sheet covers new episodes from this many hours back
LOG_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}(-\d+)?\.log$")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


def scrub(text):
    """No links, no email addresses. Ever."""
    return EMAIL.sub("[email hidden]", fetch.hide_urls(text))


def scrub_all(value):
    """scrub() every piece of text inside a record, before it's turned into JSON.
    (Scrubbing the finished JSON could eat the quotes and brackets around a link.)"""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [scrub_all(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub_all(v) for k, v in value.items()}
    return value


# ---------- the full log ----------

class Tee:
    """Sends everything to the screen as usual, and a clean copy to the log file.
    Progress bars redraw themselves with a carriage return (\\r); in the file only the
    bar's final state is kept, so a download is one line, not thousands."""

    def __init__(self, screen, file):
        self.screen, self.file, self.line = screen, file, ""

    def write(self, text):
        try:
            self.screen.write(text)
        except (OSError, ValueError):
            pass   # no screen (e.g. run by the scheduler): the file still gets it
        for ch in text:
            if ch == "\n":
                self.file.write(scrub(self.line.rstrip()) + "\n")
                self.line = ""
            elif ch == "\r":
                self.line = ""   # a progress bar redrawing: drop what was there
            else:
                self.line += ch
        self.file.flush()
        return len(text)

    def flush(self):
        try:
            self.screen.flush()
        except (OSError, ValueError):
            pass
        self.file.flush()

    def isatty(self):
        return False

    def reconfigure(self, **settings):
        """Modules ask for UTF-8 output when they load. Pass that on if the screen can do
        it; never let an unusual screen (redirected, captured, missing) stop the run."""
        try:
            self.screen.reconfigure(**settings)
        except (AttributeError, ValueError, OSError):
            pass

    def __getattr__(self, name):          # encoding, reconfigure, fileno, etc.
        return getattr(self.screen, name)


def start_log():
    """Start copying everything printed into a new log file. Returns its path."""
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = LOG_DIR / f"{stamp}.log"
    n = 2
    while path.exists():                  # two runs in the same minute
        path = LOG_DIR / f"{stamp}-{n}.log"
        n += 1
    file = open(path, "w", encoding="utf-8", errors="replace")
    old_out, old_err = sys.stdout, sys.stderr
    # With no screen at all (some scheduled runs), write the screen copy nowhere.
    nowhere = open(os.devnull, "w", encoding="utf-8")
    sys.stdout, sys.stderr = Tee(old_out or nowhere, file), Tee(old_err or nowhere, file)
    # Libraries that set up their own logging before this point still point at the old
    # screen streams. Point them at the log too.
    loggers = [logging.getLogger()] + [l for l in logging.root.manager.loggerDict.values()
                                       if isinstance(l, logging.Logger)]
    for lg in loggers:
        for h in lg.handlers:
            if isinstance(h, logging.StreamHandler) and h.stream in (old_out, old_err):
                h.setStream(sys.stderr if h.stream is old_err else sys.stdout)
    prune_logs()
    return path


def prune_logs():
    """Keep only the newest KEEP_LOGS run logs. Only touches files named like a run log."""
    logs = sorted(p for p in LOG_DIR.glob("*.log") if LOG_NAME.match(p.name))
    for old in logs[:-KEEP_LOGS]:
        old.unlink(missing_ok=True)


# ---------- what to do about each problem ----------

# (words to look for, what to do, what to do if it keeps happening).
# The third part is None when the problem needs you straight away. Otherwise it's a
# "wait and see" problem: the sheet says "nothing to do yet" until it has happened on
# ESCALATE_DAYS different days in a row, then it moves to NEEDS YOU with the stronger advice.
# Checked in order, first match wins, so specific entries come before general ones.
ADVICE = [
    # Delivery (deliver.py)
    ("hasn't received", "Check the phone: open Syncthing-Fork and make sure it's running and allowed "
                        "to sync (Run conditions, battery saver). It catches up on its own once it connects.", None),
    ("isn't running on this computer", "Start it: Start menu > Start Syncthing, or log off and on again.", None),
    ("isn't send only", "Open Syncthing (127.0.0.1:8384) > Clean Episodes > Edit > Advanced: set "
                        "Folder Type to Send Only.", None),
    ("paused in syncthing", "Open Syncthing (127.0.0.1:8384) and press Resume on the Clean Episodes folder.", None),
    ("isn't shar", "Open Syncthing (127.0.0.1:8384) and share episodes\\clean with the phone again, "
                   "Send Only.", None),
    ("syncthing's settings file", "Syncthing may have been removed. Reinstall it: "
                                  "winget install BillStewart.SyncthingWindowsSetup", None),
    ("turned the key away", "Nothing to do yet: the next run reads Syncthing's key again.",
     "Syncthing has refused the key {days} days running. Look at the full log."),
    ("cloud road isn't built", "Set delivery: home in shows.local.yaml.", None),
    ("delivery should be", "Fix the delivery: line at the top of shows.local.yaml (home, cloud, both or off).", None),
    ("syncthing", "Syncthing had trouble. Nothing to do yet: the next run checks again.",
     "Syncthing has had trouble {days} days running. Open its page (127.0.0.1:8384) and look."),
    # Cleanup (cleanup.py)
    ("couldn't delete", "Nothing to do yet: a player or Syncthing probably had the file open. "
                        "The next run tries again.",
     "The same file has refused to delete {days} days running. Restart the computer, then "
     "run: .venv\\Scripts\\python cleanup.py"),
    ("stuck", "Restart the computer. That ends the stuck run; the next run starts normally.", None),
    ("isn't logged in", "Open a terminal and run: claude auth login", None),
    ("ffmpeg wasn't found", "Reinstall it (winget install ffmpeg), then restart the computer.", None),
    ("ffprobe wasn't found", "Reinstall ffmpeg (winget install ffmpeg), then restart the computer.", None),
    ("claude wasn't found", "Claude Code may have been uninstalled or moved. Reinstall it, then restart.", None),
    ("claude command wasn't found", "Claude Code may have been uninstalled or moved. Reinstall it.", None),
    ("disk space", "Free up space: empty the Recycle Bin, or move old episodes off this disk. Don't "
                   "delete clean episodes to make room: the line would just make them again.", None),
    ("shows.local.yaml", "Open shows.local.yaml and fix what it names.", None),
    ("cuda", "Restart the computer. If it happens again, run check_gpu.py.", None),
    ("didn't start", "The line couldn't start at all. Look at the full log, or run run.ps1 by hand to see why.", None),
    ("over the", "The server sent something far too big. The show's feed may be broken: look at the full log.", None),
    ("no audio file", "The show's feed no longer has audio in it. Look at the show's page, or ask Claude to check the feed.", None),
    ("skip_titles", "Every episode is being skipped. Check this show's skip_titles in shows.local.yaml.", None),
    ("length check failed", "The Edit caught its own mistake and saved nothing. Look at the full log.", None),
    ("share a file name", "Look at the full log.", None),
    ("refused", "The Edit refused to touch an original download, as it should. Look at the full log.", None),
    ("out of time", "The line ran out of time and left some work for the next run. Nothing to do.",
     "It has run out of time {days} days running: there's more to do than fits. Lower `keep` for "
     "some shows, or ask Claude to look at what's slow."),
    ("hard stop", "The run was stopped before Task Scheduler's 3-hour limit. Unfinished work waits for the next run.",
     "It has hit the hard stop {days} days running. Something is hanging: look at the full log."),
    ("broken twice", "Claude couldn't make a usable cut list for this episode. The next run tries again.",
     "Claude has failed on this episode {days} days running. Look at its .spot-error.txt, or move "
     "the episode out of the way."),
    ("the server said", "The show's server turned the line away. Nothing to do yet: the next run tries again.",
     "It has failed {days} days running. Check the show still exists, and ask Claude to find its feed again."),
    ("no episodes in it", "The feed came back empty. Nothing to do yet: the next run tries again.",
     "It has failed {days} days running. Ask Claude to find the show's feed again."),
    ("server", "The show's server had a bad morning. Nothing to do: the next run tries again.",
     "The server has failed {days} days running. Check the show still exists, and ask Claude to find its feed again."),
    ("took longer than", "The show's server was too slow. Nothing to do: the next run tries again.",
     "It has been too slow {days} days running. Look at the full log."),
    ("connection broke", "The download was cut off. Nothing to do: the next run tries again.",
     "Downloads have failed {days} days running. Look at the full log."),
    ("claude", "The Spotter had trouble. Nothing to do yet: the next run tries again.",
     "The Spotter has failed {days} days running. Look at the full log."),
]
ESCALATE_DAYS = 3
FALLBACK = ("", "Look at the full log for details.", None)
NAMES = re.compile(r"\S*[\\/]\S*|\S+\.(?:mp3|m4a|aac|ogg|opus|wav|flac|json|txt|log|yaml|part)\b", re.I)


def advice_entry(message):
    """The ADVICE entry for a message. File names and paths are taken out first, so an
    episode called 'claude-radcliffe...' or '...-original-...' can't pick the advice."""
    m = NAMES.sub(" ", str(message)).lower()
    return next((a for a in ADVICE if a[0] in m), FALLBACK)


def advice_for(message):
    return advice_entry(message)[1]


# ---------- the run record and the morning sheet ----------

def minutes(seconds):
    return f"{seconds / 60:.1f} min"


def record_run(log_path, started, edited, problems, warning, delivery=None, cleanup=None):
    """Add this run to logs/runs.jsonl, dropping runs older than 30 days.
    delivery: the Delivery station's lines (None if it didn't get that far).
    cleanup: the Cleanup station's one line (None if it didn't get that far)."""
    entry = {
        "finished": datetime.now().isoformat(timespec="seconds"),
        "took": round(time.time() - started),
        "log": log_path.name if log_path else "",
        "episodes": [{
            "show": e["show_name"], "title": e["title"],
            "saved": round(e["original_length"] - e["clean_length"]),
            "clean": round(e["clean_length"]),
            "left_in": [{"at": round(k["clean_start"]), "type": k["type"],
                         "length": round(k["end"] - k["start"])} for k in e["kept"]],
        } for e in edited],
        "problems": [{"what": w, "message": m} for w, m in problems],
        "warning": warning,
        "delivery": delivery,
        "cleanup": cleanup,
    }
    entry = scrub_all(entry)
    cutoff = datetime.now() - timedelta(days=30)
    runs = [r for r in read_runs() if when(r) > cutoff] + [entry]
    LOG_DIR.mkdir(exist_ok=True)
    part = RUNS_FILE.with_name(RUNS_FILE.name + ".part")
    part.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in runs),
                    encoding="utf-8")
    part.replace(RUNS_FILE)


def when(run):
    return datetime.fromisoformat(run["finished"])


def read_runs():
    """Every run in the record that can be read. A damaged line is skipped, never fatal."""
    if not RUNS_FILE.exists():
        return []
    runs = []
    for line in RUNS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            run = json.loads(line)
            when(run)
            run.setdefault("episodes", [])
            run.setdefault("problems", [])
            runs.append(run)
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
    return runs


def clock(seconds):
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def days_running(runs, problem):
    """On how many different days in a row (counting back from the latest run) this same
    problem, for the same show or episode, has come up."""
    key = (problem["what"], advice_entry(problem["message"])[0])
    days = set()
    for run in reversed(runs):
        if not any((p["what"], advice_entry(p["message"])[0]) == key for p in run["problems"]):
            break
        days.add(when(run).date())
    return len(days)


def episodes_since(runs):
    since = datetime.now() - timedelta(hours=SHEET_HOURS)
    return [e for r in runs if when(r) > since for e in r["episodes"]]


def episode_lines(episodes):
    if not episodes:
        return [f"NEW CLEAN EPISODES: none in the last {SHEET_HOURS} hours"]
    total = sum(e["saved"] for e in episodes)
    out = [f"NEW CLEAN EPISODES ({len(episodes)}, {minutes(total)} of ads gone)"]
    for e in episodes:
        out.append(f"  {e['show']}: {e['title']}")
        line = f"     {minutes(e['saved'])} saved, now {clock(e['clean'])} long"
        if e["left_in"]:
            bits = ", ".join(f"{k['type']} at {clock(k['at'])} ({k['length']}s)" for k in e["left_in"])
            line += f". Left in: {bits}"
        out.append(line)
    return out


def save_sheet(lines):
    LOG_DIR.mkdir(exist_ok=True)
    part = MORNING_FILE.with_name(MORNING_FILE.name + ".part")
    part.write_text(scrub("\n".join(lines)) + "\n", encoding="utf-8")
    part.replace(MORNING_FILE)
    return MORNING_FILE


def write_morning_sheet():
    """logs/morning.txt from the run record: every episode made in the last day (so a
    second run that finds nothing new doesn't blank the sheet), plus the latest run's
    problems and warning. A problem that keeps coming back moves to NEEDS YOU."""
    runs = read_runs()
    if not runs:
        return None
    latest = runs[-1]
    problems = latest["problems"]
    done = when(latest)

    out = [f"MORNING SHEET  {done:%a %b %d}, last run finished {done:%H:%M} "
           f"({'all good' if not problems else f'{len(problems)} problem' + ('s' if len(problems) > 1 else '')})",
           ""]
    if latest.get("warning"):
        out += [f"!! {latest['warning']}", ""]
    out += episode_lines(episodes_since(runs))
    out.append("")
    if latest.get("delivery"):
        out.append("DELIVERY " + latest["delivery"][0])
        out += [f"  {line}" for line in latest["delivery"][1:]]
        out.append("")
    if latest.get("cleanup"):
        out += [f"CLEANUP {latest['cleanup']}", ""]

    if problems:
        needs_you, lines = False, []
        for p in problems:
            key, tip, if_it_keeps = advice_entry(p["message"])
            days = days_running(runs, p)
            if if_it_keeps is None:
                needs_you = True
            elif days >= ESCALATE_DAYS:
                needs_you, tip = True, if_it_keeps.format(days=days)
            lines += [f"  {p['what']}: {p['message']}", f"     -> {tip}"]
        out.append("NEEDS YOU" if needs_you else "PROBLEMS (nothing to do yet)")
        out += lines
    else:
        out.append("PROBLEMS: none")

    if latest.get("log"):
        out += ["", f"Full log: logs\\{latest['log']}"]
    return save_sheet(out)


def write_started_sheet(log_path, started_at, give_up_by):
    """Written the moment a run starts, so the sheet can never show an old 'all good' if
    this run dies. The run's own sheet replaces it when it finishes."""
    out = [f"MORNING SHEET  {started_at:%a %b %d}: a run STARTED at {started_at:%H:%M} and hasn't finished.",
           "",
           f"If you're reading this after {give_up_by:%H:%M}, it died before it could finish.",
           f"  -> Look at the end of the full log to see where it stopped: logs\\{log_path.name}",
           "     If the computer was simply turned off, nothing to do: the next run carries on.",
           ""]
    out += episode_lines(episodes_since(read_runs()))
    return save_sheet(out)


def write_emergency_sheet(message, log_name=""):
    """The last resort, used when the normal sheet can't be built. Needs nothing but a message."""
    out = [f"MORNING SHEET  {datetime.now():%a %b %d}, {datetime.now():%H:%M}: something went wrong",
           "", "NEEDS YOU", f"  {message}", f"     -> {advice_for(message)}"]
    if log_name:
        out += ["", f"Full log: logs\\{log_name}"]
    return save_sheet(out)
