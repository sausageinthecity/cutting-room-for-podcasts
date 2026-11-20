"""
Station 5: Delivery.

The delivery setting (top of shows.local.yaml) picks the road:
  home   Syncthing sends episodes/clean to the phone by itself. This station checks
         that it did: Syncthing is running, it has seen every clean episode, and the
         phone has received each one. An episode the phone still hasn't received after
         a day is a problem on the morning sheet. One made today and not there yet is
         normal (the phone may be off, or out of signal).
  cloud  Not built (Lesson 5.4 was skipped). Reported, nothing happens.
  both   home + cloud.
  off    Nothing is checked. (The default when the setting is missing.)

Syncthing's key: Syncthing keeps its own key (its "API key") in its settings file,
config.xml. This station reads it from there each run, keeps it in memory only, sends it
only to Syncthing on this computer (127.0.0.1), and never prints, logs or saves it.

Run it on its own with:  .venv\\Scripts\\python deliver.py
"""

import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests

import records

PROJECT = Path(__file__).resolve().parent
CLEAN_DIR = PROJECT / "episodes" / "clean"
SYNCTHING_CONFIG = Path(os.environ.get("LOCALAPPDATA", "")) / "Syncthing" / "config.xml"
WAITING_TOO_LONG = 24 * 3600   # seconds an episode may wait for the phone before it's a problem
SCAN_WAIT = 90                 # seconds to wait for Syncthing to finish looking at the folder
ROADS = ("home", "cloud", "both", "off")


class DeliveryProblem(Exception):
    """A plain-language problem for the morning sheet. Never contains the key."""


def _same_place(path):
    return os.path.normcase(str(Path(path).resolve()))


def made_at(path):
    """When the clean file was made. (Not its 'modified' date: that is set to the publish
    date, so the phone sorts properly.)"""
    st = Path(path).stat()
    return getattr(st, "st_birthtime", st.st_ctime)


def when(seconds):
    return datetime.fromtimestamp(seconds).strftime("%a %H:%M")


# ---------- talking to Syncthing on this computer ----------

class Syncthing:
    def __init__(self, config=SYNCTHING_CONFIG):
        if not Path(config).exists():
            raise DeliveryProblem("Syncthing's settings file wasn't found, so Syncthing may not be "
                                  "installed for this user.")
        try:
            root = ET.parse(config).getroot()
        except ET.ParseError:
            raise DeliveryProblem("Syncthing's settings file couldn't be read.") from None
        gui = root.find("gui")
        key = (gui.findtext("apikey") or "").strip() if gui is not None else ""
        address = (gui.findtext("address") or "127.0.0.1:8384").strip() if gui is not None else ""
        if not key:
            raise DeliveryProblem("Syncthing has no API key in its settings. Open its page and "
                                  "set one under Actions > Settings > General.")
        host = address.rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            # The key only ever goes to this computer.
            raise DeliveryProblem("Syncthing's page isn't on 127.0.0.1, so the key won't be sent to it.")
        scheme = "https" if gui.get("tls", "false").lower() == "true" else "http"
        self.base = f"{scheme}://{address}/rest"
        self.session = requests.Session()
        self.session.headers["X-API-Key"] = key   # in memory only
        self.session.verify = False if scheme == "https" else True   # its own certificate, on this computer
        self.root = root

    def get(self, path, **params):
        return self._call("GET", path, params)

    def post(self, path, **params):
        return self._call("POST", path, params)

    def _call(self, method, path, params):
        try:
            r = self.session.request(method, f"{self.base}/{path}", params=params, timeout=(5, 120))
        except requests.ConnectionError:
            raise DeliveryProblem("Syncthing isn't running on this computer, so nothing is being "
                                  "sent to the phone.") from None
        except requests.RequestException as err:
            raise DeliveryProblem(f"Syncthing didn't answer ({type(err).__name__}).") from None
        if r.status_code == 403:
            raise DeliveryProblem("Syncthing turned the key away. It may have been changed: "
                                  "the next run reads it again.")
        if r.status_code != 200:
            raise DeliveryProblem(f"Syncthing answered with an error (code {r.status_code}).")
        return r.json() if r.content else None

    def clean_folder(self):
        """The Syncthing folder that is episodes/clean, and the devices it's shared with."""
        for f in self.root.findall("folder"):
            if _same_place(f.get("path", "")) == _same_place(CLEAN_DIR):
                return f
        raise DeliveryProblem("Syncthing isn't sharing episodes\\clean with any device. "
                              "Add it again in Syncthing (Send Only).")


# ---------- the check ----------

def check_home():
    """Returns (lines for the summary, problems as (what, message))."""
    st = Syncthing()
    folder = st.clean_folder()
    folder_id = folder.get("id")
    my_id = st.get("system/status")["myID"]
    names = {d.get("id"): d.get("name") or "a device" for d in st.root.findall("device")}
    phones = [d.get("id") for d in folder.findall("device") if d.get("id") != my_id]
    lines, problems = [], []

    if folder.get("type") != "sendonly":
        problems.append(("Delivery", "the clean folder isn't Send Only in Syncthing, so a "
                         "deletion on the phone could reach this computer."))
    if folder.get("paused", "false").lower() == "true":
        problems.append(("Delivery", "the clean folder is paused in Syncthing."))
    if not phones:
        raise DeliveryProblem("the clean folder isn't shared with any phone in Syncthing.")

    # Make Syncthing look at the folder now, so episodes made this run are counted.
    st.post("db/scan", folder=folder_id)
    deadline = time.time() + SCAN_WAIT
    while st.get("db/status", folder=folder_id).get("state") not in ("idle", "error") \
            and time.time() < deadline:
        time.sleep(2)

    episodes = sorted(CLEAN_DIR.glob("*/*.mp3"))
    stats = st.get("stats/device") or {}
    connections = (st.get("system/connections") or {}).get("connections", {})
    waiting_anywhere = set()
    for phone in phones:
        name = names.get(phone, "the phone")
        need = st.get("db/remoteneed", folder=folder_id, device=phone, page=1, perpage=100000) or {}
        needed = {f.get("name", "").replace("\\", "/")
                  for part in ("files", "progress", "queued", "rest") for f in (need.get(part) or [])}
        waiting = [p for p in episodes if f"{p.parent.name}/{p.name}" in needed]
        waiting_anywhere.update(waiting)
        connected = connections.get(phone, {}).get("connected", False)
        seen = stats.get(phone, {}).get("lastSeen", "")
        seen_text = "connected now" if connected else f"last connected {_seen(seen)}"
        if not waiting:
            lines.append(f"{name}: has all {len(episodes)} clean episodes ({seen_text})")
            continue
        late = [p for p in waiting if time.time() - made_at(p) > WAITING_TOO_LONG]
        lines.append(f"{name}: has {len(episodes) - len(waiting)} of {len(episodes)} clean episodes, "
                     f"{len(waiting)} on the way ({seen_text})")
        for p in waiting:
            lines.append(f"  waiting since {when(made_at(p))}: {p.parent.name}/{p.stem}")
        if late:
            problems.append(("Delivery", f"{name} hasn't received {len(late)} clean episode(s) "
                             f"in over a day ({seen_text}). Oldest made {when(min(map(made_at, late)))}."))
    # Remember when each episode was first on every phone. cleanup.py counts its days from this.
    records.note_delivered(records.key_for(p) for p in episodes if p not in waiting_anywhere)
    return lines, problems


def _seen(iso):
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return "never" if t.year < 2000 else f"{t:%a %H:%M}"
    except (ValueError, AttributeError):
        return "unknown"


def deliver(road):
    """The station. Returns (summary lines, problems)."""
    if road == "off":
        return ["delivery is off (delivery: off in shows.local.yaml)"], []
    lines, problems = [], []
    if road in ("home", "both"):
        try:
            home_lines, home_problems = check_home()
            lines += [f"home: {l}" if not l.startswith("  ") else l for l in home_lines]
            problems += home_problems
        except DeliveryProblem as err:
            lines.append(f"home: {err}")
            problems.append(("Delivery", str(err)))
    if road in ("cloud", "both"):
        message = "the cloud road isn't built (Lesson 5.4 was skipped), so nothing was uploaded."
        lines.append(f"cloud: {message}")
        problems.append(("Delivery", message))
    return lines, problems


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import shows
    road = shows.delivery_setting()
    print(f"Delivery: {road}")
    lines, problems = deliver(road)
    for l in lines:
        print(f"  {l}")
    for what, message in problems:
        print(f"  PROBLEM: {message}")
    sys.exit(1 if problems else 0)
