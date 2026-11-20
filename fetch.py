"""
Station 1: Dailies.

Reads the show list (shows.py), checks each switched-on show's feed, and downloads
its newest episodes (how many: the show's `keep` setting) into episodes/raw/<show-name>/.

- Episodes whose title matches a show's skip_titles are passed over.
- A show that's switched off (enabled: false) isn't checked.
- If the episode is already downloaded, it's skipped.
- Downloads go to a .part file first and are renamed only when complete.

Run it with:  .venv\\Scripts\\python fetch.py
"""

import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
import urllib3
from tqdm import tqdm

import records  # episodes cleanup.py has finished with are never downloaded again
from shows import ShowsFileError, load_shows, slugify  # the show list and its settings live in shows.py

# Everything is found relative to this script, so it works from any folder.
PROJECT = Path(__file__).resolve().parent
RAW_DIR = PROJECT / "episodes" / "raw"

# A plain, generic name for this tool when it talks to podcast servers.
HEADERS = {"User-Agent": "CuttingRoom/1.0 (personal podcast fetcher)"}

AUDIO_TYPES = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"}

# Toughness settings (Module 4.4). Limits are totals, not "has the server gone quiet".
QUIET_LIMIT = 60                       # seconds a server may send nothing at all
FEED_LIMIT = 2 * 60                    # seconds to read one whole feed
DOWNLOAD_LIMIT = 20 * 60               # seconds to download one whole episode
MAX_FEED_SIZE = 50 * 1024 ** 2         # a feed bigger than 50 MB is broken
MAX_EPISODE_SIZE = 1024 ** 3           # an "episode" bigger than 1 GB is broken
KEEP_FREE = 2 * 1024 ** 3              # always leave 2 GB free on the disk
ROOM_FACTOR = 3                        # an episode needs room for itself, its clean copy and a temp file
UNKNOWN_SIZE = 200 * 1024 ** 2         # if the server doesn't say how big a file is, assume 200 MB


class NotEnoughRoom(Exception):
    """The disk is too full to safely take this file. Nothing was written."""


def gb(n):
    return f"{n / 1024 ** 3:.1f} GB"


def mb(n):
    return f"{n / 1024 ** 2:.0f} MB" if n >= 1024 ** 2 else "under 1 MB"


def span(seconds):
    return f"{seconds // 60} minutes" if seconds >= 60 else f"{seconds} seconds"


def plain_error(err):
    """Network errors in plain words. requests' own messages name the server and port,
    and logs name shows, never addresses."""
    if isinstance(err, requests.HTTPError) and err.response is not None:
        r = err.response
        return f"the server said {r.status_code} {r.reason or ''}".strip() + "."
    if isinstance(err, requests.Timeout):
        return f"the server didn't answer within {span(QUIET_LIMIT)}."
    if isinstance(err, requests.ConnectionError):
        return "couldn't reach the server (no internet, or the server is down)."
    if isinstance(err, requests.RequestException):
        return f"the download failed ({type(err).__name__})."
    # Errors from reading the connection directly (read_limited) come from urllib3.
    if isinstance(err, urllib3.exceptions.ReadTimeoutError):
        return f"the server went quiet for {span(QUIET_LIMIT)} partway through."
    if isinstance(err, urllib3.exceptions.HTTPError):
        return f"the connection broke partway through ({type(err).__name__})."
    return str(err)


def has_room(bytes_needed, where=None):
    """Raise NotEnoughRoom unless bytes_needed fits while still leaving KEEP_FREE free."""
    free = shutil.disk_usage(where or PROJECT).free
    if free - bytes_needed < KEEP_FREE:
        raise NotEnoughRoom(f"Not enough disk space: {mb(bytes_needed)} needed beyond the "
                            f"{gb(KEEP_FREE)} kept free, {gb(free)} free.")


def read_limited(response, out, limit, max_bytes, what, progress=None):
    """Copy a streamed response into `out`, giving up if the WHOLE thing takes longer than
    `limit` seconds or grows past `max_bytes`. Uses read1, which hands back whatever has
    arrived instead of waiting for a full chunk, so a server trickling one byte at a time
    still gets its clock checked."""
    deadline = time.monotonic() + limit
    got = 0
    while True:
        chunk = response.raw.read1(1024 * 256, decode_content=True)
        if not chunk:
            return got
        out.write(chunk)
        got += len(chunk)
        if progress:
            progress(len(chunk))
        if got > max_bytes:
            raise RuntimeError(f"{what} is bigger than {mb(max_bytes)}, so it was stopped. "
                               f"The server may be broken.")
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what} took longer than {span(limit)}, so it was stopped.")


def get_feed(url):
    """Fetch a feed's bytes within FEED_LIMIT seconds in total."""
    buf = io.BytesIO()
    with requests.get(url, headers=HEADERS, stream=True, timeout=QUIET_LIMIT) as r:
        r.raise_for_status()
        read_limited(r, buf, FEED_LIMIT, MAX_FEED_SIZE, "The feed")
    return buf.getvalue()

# Make Windows terminals print show titles with odd characters instead of crashing.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def hide_urls(text, also_hide=()):
    """Replace web addresses in a message with [link hidden].
    Feed URLs can contain a private key, and error messages often include the URL,
    sometimes whole, sometimes split into pieces ("...with url: /the/private/path").
    Anything printed might end up in a log file, so links never get printed.
    also_hide: extra exact strings to hide, like this show's feed URL and its path."""
    text = str(text)
    for secret in sorted((s for s in also_hide if s and s != "/"), key=len, reverse=True):
        text = text.replace(secret, "[link hidden]")
    text = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://\S+", "[link hidden]", text)      # http://, HTTPS://, feed://...
    text = re.sub(r"(?i)(url: )(?!\[link hidden\])\S+", r"\1[link hidden]", text)
    text = re.sub(r"\S*\?\S*=\S*", "[link hidden]", text)                       # anything with ?key=value
    text = re.sub(r"(?i)\b(?:[a-z0-9-]+\.)+(?:com|net|org|fm|io|tv|app|audio|media|podcast|cloud)"
                  r"\b(?::\d+)?(?:/\S*)?", "[link hidden]", text)               # bare host names
    return text


def feed_secrets(feed_url):
    """The pieces of a feed URL that could show up on their own in an error."""
    parts = urlparse(feed_url)
    return [feed_url, parts.netloc, parts.hostname or "", parts.path, parts.query,
            parts.path + ("?" + parts.query if parts.query else "")]


def find_audio(entry):
    """Return (url, extension) for an episode's audio file, or (None, None)."""
    for enc in entry.get("enclosures", []):
        url = enc.get("href")
        if not url:
            continue
        mime = (enc.get("type") or "").split(";")[0].strip().lower()
        if mime in AUDIO_TYPES:
            return url, AUDIO_TYPES[mime]
        ext = Path(urlparse(url).path).suffix.lower()
        if ext in AUDIO_EXTENSIONS:
            return url, ext
        if mime.startswith("audio/"):
            return url, ".mp3"
    return None, None


def episode_date(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return time.strftime("%Y-%m-%d", parsed) if parsed else "undated"


def pick_episodes(entries, skip_titles, keep=1):
    """The newest `keep` episodes with audio that aren't on the skip list, newest first.
    Each is (entry, audio url, file extension)."""
    # Sort newest first, in case a feed lists episodes out of order.
    entries = sorted(
        entries,
        key=lambda e: e.get("published_parsed") or e.get("updated_parsed") or time.gmtime(0),
        reverse=True,
    )
    skips = [s.lower() for s in skip_titles]
    picked = []
    for entry in entries:
        if len(picked) >= keep:
            break
        title = entry.get("title", "")
        match = next((s for s in skips if s in title.lower()), None)
        if match:
            print(f"  skipping: {title}  (matches '{match}')")
            continue
        url, ext = find_audio(entry)
        if not url:
            print(f"  skipping: {title}  (no audio file)")
            continue
        picked.append((entry, url, ext))
    return picked


def download(url, dest):
    """Download to dest.part with a progress bar, then rename to dest."""
    part = dest.with_name(dest.name + ".part")
    with requests.get(url, headers=HEADERS, stream=True, timeout=QUIET_LIMIT) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0) or 0) or None
        if total and total > MAX_EPISODE_SIZE:
            raise RuntimeError(f"The episode is {mb(total)}, over the {mb(MAX_EPISODE_SIZE)} limit. "
                               f"The server may be broken.")
        # Check for room before writing a single byte.
        has_room((total or UNKNOWN_SIZE) * ROOM_FACTOR, dest.parent)
        try:
            with open(part, "wb") as f, tqdm(
                total=total, unit="B", unit_scale=True, unit_divisor=1024,
                desc="  downloading", ncols=80,
            ) as bar:
                read_limited(r, f, DOWNLOAD_LIMIT, MAX_EPISODE_SIZE, "The download", bar.update)
        except BaseException:
            # A half download is useless and only takes up room. The next run tries again.
            part.unlink(missing_ok=True)
            raise
    os.replace(part, dest)


DOWNLOADS_FILE = PROJECT / "episodes" / "downloads.json"   # which full title each file name holds


def read_downloads():
    try:
        return json.loads(DOWNLOADS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def remember_download(key, title):
    record = read_downloads()
    record[key] = title
    DOWNLOADS_FILE.parent.mkdir(parents=True, exist_ok=True)
    part = DOWNLOADS_FILE.with_name(DOWNLOADS_FILE.name + ".part")
    part.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(part, DOWNLOADS_FILE)


def existing_with_stem(folder, stem):
    """An audio file already in the folder with this name, whatever its extension."""
    if not folder.exists():
        return None
    return next((p for p in folder.iterdir()
                 if p.stem == stem and p.suffix.lower() in AUDIO_EXTENSIONS), None)


def choose_name(folder, entry, ext):
    """The file name for this episode, and whether we already have it.
    Names are the date plus the first 60 characters of the title, so two different
    episodes can get the same name ("...Part 1" and "...Part 2" on the same day). The full
    title of every download is remembered in episodes/downloads.json; if a name is taken
    by a different title, this episode gets a short code added to its name.
    Returns (path, already_have)."""
    title = entry.get("title", "untitled")
    stem = f"{episode_date(entry)}_{slugify(title)}"
    code = hashlib.sha1((title + str(entry.get("id", ""))).encode("utf-8")).hexdigest()[:6]
    record = read_downloads()
    retired = records.read_retired()
    for attempt in (stem, f"{stem}_{code}"):
        key = f"{folder.name}/{attempt}"
        found = existing_with_stem(folder, attempt)
        if not found and key in retired:
            # Finished and cleaned up (cleanup.py). Same episode unless the record says otherwise.
            if retired[key].get("title") in ("", None, title):
                return folder / f"{attempt}{ext}", True
            continue
        if not found:
            return folder / f"{attempt}{ext}", False
        known = record.get(key)
        if known is None:
            # Downloaded before this record existed. Assume it's this episode, and remember it.
            remember_download(key, title)
            return found, True
        if known == title:
            return found, True
        # Same name, different episode: try the name with the code added.
    raise RuntimeError("two different episodes share a file name and its backup name. "
                       "Look at the full log.")


def fetch_show(show):
    """Make sure the newest `keep` episodes of one show are downloaded.
    show: one show from shows.load_shows(). Returns a list of (saved file, seconds taken)."""
    print(f"\n{show['name']}")
    feed = feedparser.parse(get_feed(show["feed"]))
    if not feed.entries:
        # Logged as a problem: a feed that suddenly has no episodes is usually a broken link
        # or a server having a bad day, and nobody's watching the screen to notice.
        raise RuntimeError("the feed has no episodes in it. Is the link still right?")

    picked = pick_episodes(feed.entries, show["skip_titles"], show["keep"])
    if not picked:
        # Logged as a problem, not just printed: otherwise the show would quietly stop arriving.
        if not any(find_audio(e)[0] for e in feed.entries):
            raise RuntimeError("the feed's episodes have no audio file in them (maybe video now?).")
        raise RuntimeError("every episode in the feed matches this show's skip_titles.")

    folder = RAW_DIR / show["folder"]
    saved = []
    for entry, audio_url, ext in picked:
        title = entry.get("title", "untitled")
        label = "newest" if entry is picked[0][0] else "also keeping"
        print(f"  {label}: {title}  ({episode_date(entry)})")
        dest, already = choose_name(folder, entry, ext)
        if already:
            if dest.exists():
                print(f"  already have it: {dest.relative_to(PROJECT)}")
            else:
                print(f"  already done and cleaned up: {dest.stem}")
            continue
        folder.mkdir(parents=True, exist_ok=True)
        start = time.time()
        download(audio_url, dest)
        remember_download(f"{folder.name}/{dest.stem}", title)
        seconds = time.time() - start
        size_mb = dest.stat().st_size / (1024 * 1024)
        print(f"  saved: {dest.relative_to(PROJECT)}  ({size_mb:.1f} MB)")
        saved.append((dest, seconds))
    return saved


def fetch_all(out_of_time=None):
    """Check every show that's switched on. Returns (downloads, errors):
    downloads is a list of (file, seconds), errors is a list of (show name, message).
    out_of_time: optional function; once it returns True, no more shows are started."""
    warnings = []
    all_shows = load_shows(warnings=warnings)
    downloads, errors = [], []
    # A typo in shows.local.yaml is a problem for the morning sheet, not just a printed line.
    errors += [("shows.local.yaml", w) for w in warnings]
    if not all_shows:
        print("shows.local.yaml has no shows in it.")
        errors.append(("shows.local.yaml", "it has no shows in it, so nothing was downloaded."))
    for n, show in enumerate(all_shows):
        if out_of_time and out_of_time():
            left = sum(1 for s in all_shows[n:] if s["enabled"] and not s["problem"])
            print(f"\nOut of time: {left} show(s) not checked.")
            errors.append(("Time budget", f"out of time: {left} show(s) left to check next run."))
            break
        if show["problem"]:
            # A show that can't run is logged, and the rest keep going.
            print(f"\n{show['name'] or 'A show'}: skipped. {show['problem']}")
            errors.append((show["name"] or "?", show["problem"]))
            continue
        if not show["enabled"]:
            print(f"\n{show['name']}: switched off in shows.local.yaml")
            continue
        try:
            downloads += fetch_show(show)
        except Exception as err:
            # One broken show shouldn't stop the others.
            message = hide_urls(plain_error(err), feed_secrets(show["feed"]))
            print(f"  ERROR: {message}")
            errors.append((show["name"], message))
    return downloads, errors


def main():
    try:
        fetch_all()
    except ShowsFileError as err:
        print(err)
        sys.exit(1)


if __name__ == "__main__":
    main()
