"""
Station 6: Closing duties. Deletes what the line has finished with, so the disk never fills.

  .venv\\Scripts\\python cleanup.py                       DRY RUN: lists what it would delete. Touches nothing.
  .venv\\Scripts\\python cleanup.py --as-of 2026-12-10    DRY RUN as if it were that date
  .venv\\Scripts\\python cleanup.py --delete              deletes exactly what a dry run would list right now

The rules (numbers in the cleanup: section of shows.local.yaml):
  Originals (episodes/raw)    deleted raw_days after the phone received the clean version.
                              Never if there's no clean version, or the phone never got it.
  Clean episodes              keep the newest keep_clean per show; older ones go, if they were
                              made more than clean_min_days ago. Syncthing then removes them
                              from the phone too. Its .kept.txt note goes with it.
  Transcripts, cut lists      deleted text_days after the episode was published, once its
                              original and clean episode are both gone.
  episodes/test               files older than test_days.

Never deleted: folder.jpg, .stignore, the record files (downloads, delivered, retired),
run.lock, logs/. Nothing outside those four folders is ever touched.

Before an original or clean episode is deleted, its name goes into episodes/retired.json,
so the Dailies never download it again and the Edit never re-makes it.
"""

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import fetch
import records
import shows
from deliver import made_at

PROJECT = Path(__file__).resolve().parent
EPISODES = PROJECT / "episodes"
RAW_DIR = EPISODES / "raw"
CLEAN_DIR = EPISODES / "clean"
TRANSCRIPTS_DIR = EPISODES / "transcripts"
TEST_DIR = EPISODES / "test"

TEXT_SUFFIXES = (".json", ".txt", ".cuts.json", ".cuts.txt", ".labels.txt", ".spot-error.txt")
TEST_SUFFIXES = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac", ".json", ".txt"}
NEVER = {"folder.jpg", ".stignore", "downloads.json", "delivered.json", "retired.json", "run.lock"}
DAY = 86400

GROUPS = [("raw", "Originals (episodes/raw)"),
          ("clean", "Clean episodes (episodes/clean). Syncthing removes these from the phone too"),
          ("text", "Transcripts and cut lists (episodes/transcripts)"),
          ("test", "Test files (episodes/test)")]


@dataclass
class Item:
    path: Path
    size: int
    group: str
    reason: str


def _key(p):
    return os.path.normcase(str(Path(p).resolve()))


def areas():
    return {"raw": RAW_DIR, "clean": CLEAN_DIR, "text": TRANSCRIPTS_DIR, "test": TEST_DIR}


def is_safe(path, group):
    """Only a plain file, inside its own folder, of a kind cleanup knows, never a protected one."""
    path = Path(path)
    if path.name.lower() in NEVER or path.is_symlink() or not path.is_file():
        return False
    if not _key(path).startswith(_key(areas()[group]) + os.sep):
        return False
    name = path.name.lower()
    return {"raw": path.suffix.lower() in fetch.AUDIO_EXTENSIONS,
            "clean": name.endswith(".mp3") or name.endswith(".kept.txt"),
            "text": name.endswith((".json", ".txt")),
            "test": path.suffix.lower() in TEST_SUFFIXES}[group]


def publish_time(path):
    """Noon on the date at the start of the file name, or None."""
    try:
        return datetime.strptime(Path(path).name[:10], "%Y-%m-%d").replace(hour=12).timestamp()
    except ValueError:
        return None


def day_text(seconds):
    return datetime.fromtimestamp(seconds).strftime("%b %d")


def plan(settings, now, road, delivered, retired):
    """Work out what to delete. Touches nothing. Returns (items, {key: title} to retire)."""
    items, retire = [], {}
    titles = fetch.read_downloads()

    def add(path, group, reason):
        if is_safe(path, group):
            items.append(Item(Path(path), Path(path).stat().st_size, group, reason))
            return True
        return False

    # 1. Originals
    raw_going = set()
    for audio in sorted(RAW_DIR.glob("*/*")):
        if audio.suffix.lower() not in fetch.AUDIO_EXTENSIONS:
            continue
        key = records.key_for(audio)
        clean = CLEAN_DIR / audio.parent.name / f"{audio.stem}.mp3"
        if key in retired:
            reason = "original of an episode already finished (left over)"
        elif not clean.exists():
            continue   # never made it through the line: keep the original
        else:
            if road == "off":
                since = made_at(clean)
            elif key in delivered:
                since = datetime.fromisoformat(delivered[key]).timestamp()
            else:
                continue   # the phone hasn't got it yet
            days = (now - since) / DAY
            if days < settings["raw_days"]:
                continue
            got = "made" if road == "off" else "on the phone"
            reason = f"original; clean version {got} since {day_text(since)} ({days:.0f} days)"
        if add(audio, "raw", reason):
            raw_going.add(key)
            retire[key] = titles.get(key, "")

    # 2. Clean episodes
    clean_going = set()
    keep = settings["keep_clean"]
    for show in sorted(d for d in CLEAN_DIR.iterdir() if d.is_dir()) if CLEAN_DIR.exists() else []:
        newest_first = sorted(show.glob("*.mp3"), key=lambda p: publish_time(p) or made_at(p), reverse=True)
        for p in newest_first[keep:]:
            key = records.key_for(p)
            if (now - made_at(p)) / DAY < settings["clean_min_days"]:
                continue
            original_stays = (fetch.existing_with_stem(RAW_DIR / show.name, p.stem) is not None
                              and key not in raw_going and key not in retired)
            if original_stays:
                continue   # deleting it now would just make the Edit render it again
            if add(p, "clean", f"not one of the newest {keep} of this show"):
                clean_going.add(key)
                retire.setdefault(key, titles.get(key, ""))
                kept = p.with_name(p.stem + ".kept.txt")
                if kept.exists():
                    add(kept, "clean", "its left-in note goes with it")

    # 3. Transcripts and cut lists
    for t in sorted(TRANSCRIPTS_DIR.glob("*/*.json")) if TRANSCRIPTS_DIR.exists() else []:
        if t.name.endswith(".cuts.json"):
            continue
        key, stem = records.key_for(t), t.stem
        raw_left = fetch.existing_with_stem(RAW_DIR / t.parent.name, stem) is not None and key not in raw_going
        clean_left = (CLEAN_DIR / t.parent.name / f"{stem}.mp3").exists() and key not in clean_going
        if raw_left or clean_left:
            continue
        published = publish_time(t) or t.stat().st_mtime
        days = (now - published) / DAY
        if days < settings["text_days"]:
            continue
        for suffix in TEXT_SUFFIXES:
            f = t.parent / f"{stem}{suffix}"
            if f.exists():
                add(f, "text", f"published {day_text(published)} ({days:.0f} days), episode finished")

    # 4. Test files
    for f in sorted(TEST_DIR.iterdir()) if TEST_DIR.exists() else []:
        if f.is_file():
            days = (now - f.stat().st_mtime) / DAY
            if days >= settings["test_days"]:
                add(f, "test", f"made {day_text(f.stat().st_mtime)} ({days:.0f} days ago)")
    return items, retire


def rel(path):
    """episodes\\raw\\show\\file.mp3: the path from the project folder."""
    try:
        return Path(path).relative_to(RAW_DIR.parent.parent)
    except ValueError:
        return Path(path)


def size_text(n):
    return f"{n / 1024 / 1024:,.1f} MB" if n >= 1024 * 1024 else f"{n / 1024:,.0f} KB"


def show_list(items):
    lines = []
    for group, title in GROUPS:
        mine = [i for i in items if i.group == group]
        if not mine:
            continue
        lines.append(f"{title}: {len(mine)} file(s), {size_text(sum(i.size for i in mine))}")
        for i in mine:
            lines.append(f"  {rel(i.path)}  ({size_text(i.size)})  {i.reason}")
        lines.append("")
    total = sum(i.size for i in items)
    lines.append(f"TOTAL: {len(items)} file(s), {size_text(total)} freed" if items
                 else "Nothing to delete.")
    return lines


def run_cleanup(delete=False, now=None, warnings=None):
    """The station. Returns {"items", "deleted", "freed", "failed": [(name, why)]}."""
    warnings = [] if warnings is None else warnings
    settings = shows.cleanup_settings(warnings)
    road = shows.delivery_setting(warnings)
    now = now or datetime.now().timestamp()
    items, retire = plan(settings, now, road, records.read_delivered(), records.read_retired())
    result = {"items": items, "deleted": 0, "freed": 0, "failed": [], "settings": settings}
    if not delete:
        return result
    if retire:
        records.retire(retire)   # first, so a crash mid-way never leaves the line re-doing work
    for i in items:
        try:
            if not is_safe(i.path, i.group):   # checked again at the last moment
                raise PermissionError("not a file cleanup may delete")
            i.path.unlink()
            result["deleted"] += 1
            result["freed"] += i.size
            print(f"  deleted {rel(i.path)}")
        except OSError as err:
            why = "in use by another program?" if isinstance(err, PermissionError) else type(err).__name__
            result["failed"].append((i.path.name, why))
            print(f"  COULDN'T DELETE {rel(i.path)} ({why})")
    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    delete = "--delete" in args
    now, as_of = None, None
    if "--as-of" in args:
        if delete:
            sys.exit("--as-of is for dry runs only. Nothing was deleted.")
        try:
            as_of = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d").replace(hour=12)
        except (IndexError, ValueError):
            sys.exit("Use --as-of with a date like 2026-12-10.")
        now = as_of.timestamp()
    warnings = []
    result = run_cleanup(delete=False, now=now, warnings=warnings)
    s = result["settings"]
    for w in warnings:
        print(f"shows.local.yaml: {w}")
    print(f"Settings: keep_clean {s['keep_clean']} (none younger than {s['clean_min_days']} days), "
          f"raw_days {s['raw_days']}, text_days {s['text_days']}, test_days {s['test_days']}")
    if not delete:
        print(f"DRY RUN{f' as of {as_of:%a %b %d}' if as_of else ''}: nothing is deleted. "
              f"Run with --delete to delete these.\n")
        print("\n".join(show_list(result["items"])))
        return 0
    print("DELETING:\n")
    result = run_cleanup(delete=True)
    print(f"\n{result['deleted']} file(s) deleted, {size_text(result['freed'])} freed"
          + (f", {len(result['failed'])} couldn't be deleted" if result["failed"] else ""))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
