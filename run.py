"""
Dailies + Logging + Spotting + The Edit + Delivery + Cleanup in one command.

1. fetch.py: check every show's feed and download anything new.
2. transcribe.py: transcribe every episode in episodes/raw/ that has no transcript yet.
3. spot.py: make a cut list (and Audacity labels) for every transcript that has none yet.
4. edit.py: render a clean episode for every cut list that has no clean version yet.
5. deliver.py: check the clean episodes reached the phone (the delivery: setting).
6. cleanup.py: delete what the line has finished with (the cleanup: setting).
7. Print a summary and write the morning sheet.

Built to run with nobody watching (Module 4.4 and 4.8):
- Only one run at a time. A second run that finds the line busy leaves quietly. If the
  run holding the line started over 3 hours ago, it's stuck, and the sheet says so.
- The morning sheet is replaced the moment a run starts ("started, not finished"), so it
  can never show an old "all good" after a run that died.
- Time budget: no new work is started after 2.5 hours. At 2 hours 50 a hard stop writes
  the sheet and ends the run (and anything it started), before Task Scheduler's 3-hour limit.
- One bad show or episode is logged and the rest carry on. If a whole station breaks,
  it's logged and the next station still runs on whatever is there.
- Disk space is checked before every download and every edit.
- If Claude isn't logged in, spotting is skipped with one clear line. Next run catches up.
- Exit code 0 means everything worked, 1 means something is listed under Problems.
- Every run is logged in logs/, and logs/morning.txt is the 10-second summary (logbook.py).

Run it with:  .venv\\Scripts\\python run.py
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

# Only light modules here. The heavy ones (the GPU libraries behind transcribe.py) are
# loaded inside main(), after the "started" sheet is written, so if they fail to load the
# sheet says so instead of showing yesterday's.
import cleanup
import deliver
import fetch
import logbook
import records
import shows

edit = spot = transcribe = None   # loaded by load_stations()
NOT_RUN = "(didn't run)"

PROJECT = Path(__file__).resolve().parent
LOCK_FILE = PROJECT / "episodes" / "run.lock"
LOCK_BYTE = 4096   # the lock sits on this byte, so the human-readable note at the top stays readable

BUDGET = 2.5 * 3600            # no new work is started after this many seconds
HARD_STOP = (2 * 60 + 50) * 60  # the run ends itself here, before Task Scheduler's 3 hours
STUCK_AFTER = 3 * 3600         # a lock held longer than this belongs to a stuck run


# ---------- one run at a time ----------

class RunLock:
    """A lock held by Windows (or macOS/Linux) itself, not just a file that exists.
    While this run is alive, the lock is held. The moment the process ends, however it ends
    (finished, crashed, killed, power cut), the system lets go of it. The file stays behind,
    but nobody holds it, so the next run takes it straight away."""

    def __init__(self, path=None):
        self.path = Path(path or LOCK_FILE)
        self.handle = None

    def acquire(self):
        """True if this run now holds the lock. False if another run holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(LOCK_BYTE)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        # Ours. Leave a note for a human who opens the file.
        handle.seek(0)
        handle.truncate()
        handle.write(f"started {datetime.now():%Y-%m-%d %H:%M:%S} by process {os.getpid()}\n")
        handle.flush()
        self.handle = handle   # kept open on purpose: closing it would let go of the lock
        return True

    def note(self):
        """What the run holding the lock wrote, e.g. 'started 2026-11-20 07:30:02 by process 812'."""
        try:
            return self.path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""


def lock_started(note):
    """When the run holding the lock started, from its note, or None."""
    try:
        return datetime.strptime(note.split(" by ")[0].replace("started ", "").strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def busy_message(note):
    """The one line a second run prints."""
    started = lock_started(note)
    when = f"{started:%H:%M}" if started else "earlier"
    return f"Another run started at {when} is still going. Leaving it to finish."


def stuck_message(note):
    started = lock_started(note)
    process = note.split(" by process ")[-1] if " by process " in note else "?"
    return (f"A run that started {started:%a %H:%M} (process {process}) is stuck: it has held the line "
            f"for over {STUCK_AFTER // 3600} hours, so every run since has had to leave.")


# ---------- small helpers ----------

def needs_transcript(audio):
    json_path, txt_path = transcribe.outputs_for(audio)
    return not (json_path.exists() and txt_path.exists())


def minutes(seconds):
    return f"{seconds / 60:.1f} min" if seconds >= 60 else f"{seconds:.0f}s"


def run_station(name, errors, work):
    """Run one station. If the whole station breaks (not just one episode), log it in one
    line and return None, so the next station still runs. Also catches a station that tries
    to shut the whole program down (SystemExit), e.g. 'No CUDA GPU found'."""
    try:
        return work()
    except (Exception, SystemExit) as err:
        message = fetch.hide_urls(err) or type(err).__name__
        print(f"  STATION STOPPED: {message}")
        errors.append((f"{name} station", message))
        return None


# What the run is doing and has done, for the hard stop (it runs on another thread).
state = {"step": "starting up", "started": time.time(), "errors": [], "edited": [], "log": None}


def doing(step):
    state["step"] = step


def out_of_time():
    return time.time() - state["started"] > BUDGET


def ran_out(errors, station, left, verb):
    """Log, once per station, that work was left for the next run."""
    if left:
        print(f"  Out of time: {left} episode(s) left to {verb} next run.")
        errors.append(("Time budget", f"out of time after {BUDGET / 3600:g} hours: "
                                      f"{left} episode(s) left to {verb} next run."))


# ---------- the four stations ----------

def dailies(errors):
    doing("checking feeds")
    downloads, show_errors = fetch.fetch_all(out_of_time)
    errors += show_errors
    return downloads


def logging_station(errors):
    raw_dir = fetch.RAW_DIR
    audio_files = sorted(
        p for p in raw_dir.rglob("*") if p.suffix.lower() in fetch.AUDIO_EXTENSIONS
    ) if raw_dir.exists() else []
    to_do = [p for p in audio_files if needs_transcript(p)]
    transcribed = []
    if not to_do:
        print("nothing new to transcribe")
    for n, audio in enumerate(to_do):
        if out_of_time():
            ran_out(errors, "Logging", len(to_do) - n, "transcribe")
            break
        doing(f"transcribing {audio.name}")
        try:
            seconds = transcribe.transcribe_file(audio)
            if seconds is not None:
                transcribed.append((audio, seconds))
        except Exception as err:
            message = fetch.hide_urls(err)
            print(f"  ERROR transcribing {audio.name}: {message}")
            errors.append((audio.name, message))
    return {"transcribed": transcribed, "done_before": len(audio_files) - len(to_do)}


def spotting(errors):
    transcripts = spot.all_transcripts()
    to_spot = [t for t in transcripts if not spot.cut_paths(t)[0].exists()]
    spotted = []
    result = {"spotted": spotted, "done_before": len(transcripts) - len(to_spot)}
    if not to_spot:
        print("nothing new to spot")
        return result
    doing("checking Claude's login")
    ok, message = spot.claude_ready()
    if message:
        print(f"  {message}")
    if not ok:
        errors.append(("Spotting", message))
        return result
    for n, t in enumerate(to_spot):
        if out_of_time():
            ran_out(errors, "Spotting", len(to_spot) - n, "spot")
            break
        doing(f"spotting {t.stem}")
        try:
            r = spot.spot_file(t, labels=True)
            if r:
                spotted.append(r)
        except spot.ClaudeLoginError as err:
            # Every other episode would fail the same way. One line, then stop.
            print(f"  STOPPED: {err}")
            errors.append(("Spotting", str(err)))
            break
        except Exception as err:
            message = fetch.hide_urls(err)
            print(f"  ERROR spotting {t.stem}: {message}")
            errors.append((t.stem, message))
    return result


def the_edit(errors):
    with_cuts = [t for t in spot.all_transcripts() if spot.cut_paths(t)[0].exists()]
    edited, done_before = state["edited"], 0
    started_any = False
    retired = records.read_retired()
    for n, t in enumerate(with_cuts):
        try:
            # Finished and cleaned up (cleanup.py): never re-made, and its missing original
            # isn't an error.
            if records.key_for(t) in retired:
                done_before += 1
                continue
            # Inside the try: one damaged transcript is one problem, not the whole station.
            if edit.is_edited(t):
                done_before += 1
                continue
            if out_of_time():
                left = sum(1 for x in with_cuts[n:]
                           if records.key_for(x) not in retired and not safe_is_edited(x))
                ran_out(errors, "Edit", left, "edit")
                break
            started_any = True
            doing(f"editing {t.stem}")
            original = edit.find_original(json.loads(t.read_text(encoding="utf-8")))
            # Room for the clean copy and its temporary file.
            fetch.has_room(Path(original).stat().st_size * 2, PROJECT)
            r = edit.edit_episode(t)
            if r:
                r["show_name"] = shows.show_name(r["show"])
                edited.append(r)
        except Exception as err:   # includes edit.OriginalProtected and fetch.NotEnoughRoom
            started_any = True
            message = fetch.hide_urls(err)
            print(f"  ERROR editing {t.stem}: {message}")
            errors.append((t.stem, message))
    if not started_any:
        print("nothing new to edit")
    return {"edited": edited, "done_before": done_before}


def delivery_station(errors):
    """Station 5 (deliver.py). Returns the lines for the summary and the morning sheet."""
    warnings = []
    road = shows.delivery_setting(warnings)
    errors += [("shows.local.yaml", w) for w in warnings]
    lines, problems = deliver.deliver(road)
    for line in lines:
        print(f"  {line}")
    errors += problems
    return lines


def cleanup_station(errors):
    """Station 6 (cleanup.py), deleting for real. Runs after Delivery, so it uses the
    phone's latest record. Returns one line for the summary and the morning sheet."""
    warnings = []
    result = cleanup.run_cleanup(delete=True, warnings=warnings)
    errors += [("shows.local.yaml", w) for w in warnings
               if ("shows.local.yaml", w) not in errors]   # Delivery may have reported it already
    errors += [(name, f"couldn't delete it ({why}). The next run tries again.")
               for name, why in result["failed"]]
    if not result["items"]:
        return "nothing to delete"
    line = f"{result['deleted']} file(s) deleted, {cleanup.size_text(result['freed'])} freed"
    if result["failed"]:
        line += f", {len(result['failed'])} couldn't be deleted"
    return line


def safe_is_edited(t):
    try:
        return edit.is_edited(t)
    except Exception:
        return False


# ---------- the end of a run ----------

def finish_sheet(log_path, started, errors, warning=None):
    """Record the run and write the morning sheet. If that fails, write the emergency
    sheet, so the "started, not finished" sheet never outlives a run that did finish."""
    try:
        logbook.record_run(log_path, started, state["edited"], errors, warning,
                           delivery=state.get("delivery"), cleanup=state.get("cleanup"))
        sheet = logbook.write_morning_sheet()
        print(f"Morning sheet: logs\\{sheet.name}")
        return True
    except Exception as err:
        message = f"The run finished, but its morning sheet couldn't be written: {logbook.scrub(err)}"
        print(message)
        errors.append(("Morning sheet", message))
        try:
            logbook.write_emergency_sheet(message, log_path.name if log_path else "")
        except Exception:
            pass   # run.ps1 notices an un-updated sheet and writes one itself
        return False


def end_everything():
    """End this run and anything it started (Claude, ffmpeg), the hard way."""
    sys.stdout.flush()
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(os.getpid())], capture_output=True)
    os._exit(1)


def hard_stop():
    """Runs on a timer thread at HARD_STOP: write the sheet while there's still time."""
    message = (f"hard stop after {HARD_STOP // 3600}h{HARD_STOP % 3600 // 60:02d} while "
               f"{state['step']}. Anything unfinished waits for the next run.")
    print(f"\nHARD STOP: {message}")
    errors = state["errors"] + [("Time limit", message)]
    finish_sheet(state["log"], state["started"], errors)
    end_everything()


def load_stations():
    global edit, spot, transcribe
    import edit as _edit
    import spot as _spot
    import transcribe as _transcribe
    edit, spot, transcribe = _edit, _spot, _transcribe


# ---------- the run ----------

def main():
    started = time.time()
    state.update(started=started, step="starting up", errors=[], edited=[], log=None, delivery=None, cleanup=None)

    lock = RunLock()
    if not lock.acquire():
        note = lock.note()
        began = lock_started(note)
        if began and datetime.now() - began > timedelta(seconds=STUCK_AFTER):
            message = stuck_message(note)
            print(message)
            logbook.record_run(None, started, [], [("Stuck run", message)], None)
            logbook.write_morning_sheet()
            sys.exit(1)
        print(busy_message(note))
        sys.exit(0)   # not an error: the line is already running (and the morning sheet is its job)

    # From here on the old sheet is gone: if this run dies, the sheet says it never finished.
    log_path = logbook.start_log()
    state["log"] = log_path
    give_up_by = datetime.fromtimestamp(started) + timedelta(seconds=HARD_STOP + 10 * 60)
    logbook.write_started_sheet(log_path, datetime.fromtimestamp(started), give_up_by)
    print(f"Cutting Room line, {datetime.now():%a %b %d %H:%M}. Full log: logs\\{log_path.name}\n")

    timer = threading.Timer(HARD_STOP, hard_stop)
    timer.daemon = True
    timer.start()

    errors = state["errors"]
    try:
        load_stations()
    except (Exception, SystemExit) as err:
        message = f"The line didn't start: {logbook.scrub(err)}"
        print(message)
        errors.append(("Start-up", message))
        finish_sheet(log_path, started, errors)
        sys.exit(1)

    # The tools the line needs from outside Python. Checked here too (not just in run.ps1)
    # so a missing one shows up in the log and on the morning sheet.
    for tool, needed_by in (("ffmpeg", "transcribing and the Edit"), ("ffprobe", "the Edit"),
                            ("claude", "spotting")):
        if not shutil.which(tool):
            message = f"{tool} wasn't found, so {needed_by} will fail."
            print(f"PROBLEM: {message}")
            errors.append(("Tools", message))

    print("=== 1. Dailies: checking feeds ===")
    downloads = run_station("Dailies", errors, lambda: dailies(errors)) or []

    print("\n=== 2. Logging: transcribing anything new ===")
    logged = run_station("Logging", errors, lambda: logging_station(errors)) or {}

    print("\n=== 3. Spotting: cut lists for anything new ===")
    spotted = run_station("Spotting", errors, lambda: spotting(errors)) or {}

    print("\n=== 4. The Edit: clean episodes for anything new ===")
    edits = run_station("Edit", errors, lambda: the_edit(errors)) or {}

    print("\n=== 5. Delivery: did the clean episodes reach the phone? ===")
    doing("checking delivery")
    delivery = run_station("Delivery", errors, lambda: delivery_station(errors)) or []

    print("\n=== 6. Cleanup: delete what the line has finished with ===")
    doing("cleaning up")
    tidied = run_station("Cleanup", errors, lambda: cleanup_station(errors))
    print(f"  {tidied or NOT_RUN}")
    doing("writing the summary")

    print("\n=== Summary ===")
    print(f"Downloaded: {len(downloads)}")
    for path, seconds in downloads:
        print(f"  {path.parent.name}/{path.name}  ({minutes(seconds)})")
    transcribed = logged.get("transcribed", [])
    print(f"Transcribed: {len(transcribed)}")
    for path, seconds in transcribed:
        print(f"  {path.parent.name}/{path.name}  ({minutes(seconds)})")
    print(f"Already done: {logged.get('done_before', '?')} episode(s) with transcripts")
    print(f"Spotted: {len(spotted.get('spotted', []))}")
    for s in spotted.get("spotted", []):
        c = s["counts"]
        print(f"  {s['show']}/{s['episode']}  ({minutes(s['seconds'])})")
        print(f"    {s['cuts']} cuts: {c['high']} high, {c['medium']} medium, {c['low']} low")
        print(f"    high-confidence cuts: {s['high_seconds'] / 60:.1f} min of {s['duration'] / 60:.1f} min")
    print(f"Already spotted: {spotted.get('done_before', '?')} episode(s) with cut lists")
    print(f"Edited: {len(state['edited'])}")
    for e in state["edited"]:
        saved = e["original_length"] - e["clean_length"]
        print(f"  {e['show']}/{e['episode']}  ({minutes(e['seconds'])})")
        print(f"    original {e['original_length'] / 60:.1f} min -> clean {e['clean_length'] / 60:.1f} min, "
              f"{saved / 60:.1f} min saved")
        print(f"    left in: {len(e['kept'])} medium/low segment(s), see {e['output'].stem}.kept.txt")
    print(f"Already edited: {edits.get('done_before', '?')} episode(s) with clean versions")
    print("Delivery:")
    for line in delivery or ["(not checked)"]:
        print(f"  {line}")
    print(f"Cleanup: {tidied or NOT_RUN}")
    if errors:
        print(f"Problems: {len(errors)}")
        for what, message in errors:
            print(f"  {what}: {message}")
    print(f"Total time: {minutes(time.time() - started)}")

    # The morning sheet, with Claude's login warning if there is one.
    warning = None
    try:
        ready, login_note = spot.claude_ready()
        if login_note and not any(login_note == m for _, m in errors):
            warning = login_note
    except Exception:
        pass
    timer.cancel()
    state["delivery"] = delivery
    state["cleanup"] = tidied
    finish_sheet(log_path, started, errors, warning)

    # A non-zero exit lets the scheduler (4.7) notice that something went wrong.
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
