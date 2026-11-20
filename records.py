"""
Two small records the line keeps about each episode (key = "<show-folder>/<file name without extension>"):

  episodes/delivered.json  the first time deliver.py saw the episode on the phone
  episodes/retired.json    episodes cleanup.py has finished with (original deleted).
                           Dailies won't download them again and the Edit won't re-make them.

Both are written to a .part file first and then swapped in, so a crash never leaves half a file.
Neither is ever deleted by cleanup.
"""

import json
import os
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
DELIVERED_FILE = PROJECT / "episodes" / "delivered.json"
RETIRED_FILE = PROJECT / "episodes" / "retired.json"


def key_for(path):
    path = Path(path)
    return f"{path.parent.name}/{path.stem}"


def _read(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(part, path)


def read_delivered():
    """{key: when it was first seen on the phone, as an ISO date-time}"""
    return _read(DELIVERED_FILE)


def note_delivered(keys, when=None):
    """Remember the first time each of these episodes was seen on the phone. Later
    sightings don't change the date."""
    record = read_delivered()
    stamp = (when or datetime.now()).isoformat(timespec="seconds")
    new = {k: stamp for k in keys if k not in record}
    if new:
        record.update(new)
        _write(DELIVERED_FILE, record)
    return len(new)


def read_retired():
    """{key: {"title": full title or "", "retired": ISO date-time}}"""
    return _read(RETIRED_FILE)


def is_retired(key):
    return key in read_retired()


def retire(entries, when=None):
    """entries: {key: title}. Written BEFORE any file is deleted."""
    record = read_retired()
    stamp = (when or datetime.now()).isoformat(timespec="seconds")
    for key, title in entries.items():
        record.setdefault(key, {"title": title or "", "retired": stamp})
    _write(RETIRED_FILE, record)
