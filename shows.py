"""
The show list. Every station reads the same ticket.

Reads shows.local.yaml (git-ignored, because it holds the feed URLs), fills in a default
for anything a show leaves out, and checks the values. Each show's notes for the Spotter
live in show-notes/<show-folder>.md (committed: they hold no secrets).

A show entry is never printed, because it would include the feed URL.
"""

import re
import unicodedata
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parent
SHOWS_FILE = PROJECT / "shows.local.yaml"
NOTES_DIR = PROJECT / "show-notes"

# Used when shows.local.yaml has no defaults: section, or leaves an option out of it.
BUILT_IN_DEFAULTS = {"enabled": True, "keep": 1}
SHOW_OPTIONS = {"name", "feed", "enabled", "keep", "skip_titles"}
DEFAULT_OPTIONS = {"enabled", "keep"}
MAX_KEEP = 10



class ShowsFileError(Exception):
    """shows.local.yaml is missing or can't be read. The message never quotes the file."""


YES = {"true", "yes", "on"}
NO = {"false", "no", "off"}


class SettingsProblem(ValueError):
    """A value in shows.local.yaml that can't be used."""


def slugify(text, max_length=60):
    """Turn any title into a safe filename piece: lowercase, a-z, 0-9 and dashes only.
    'Part Two: Don Simpson!' -> 'part-two-don-simpson'"""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(text) > max_length:
        # Cut at a dash so we don't end mid-word.
        text = text[:max_length].rsplit("-", 1)[0]
    return text or "untitled"


def as_list(value):
    """skip_titles can be written on one line or as a list. Always give back a list of text,
    so 'Bonus' is matched as a word, not letter by letter.
      skip_titles: Bonus        ->  ["Bonus"]
      skip_titles:
        - Bonus
        - 1883                  ->  ["Bonus", "1883"]"""
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    return [str(v).strip() for v in value if str(v).strip()]


def yes_no(value, where):
    if isinstance(value, bool):
        return value
    word = str(value).strip().lower()
    if word in YES:
        return True
    if word in NO:
        return False
    raise SettingsProblem(f"{where}: enabled should be true or false, not '{value}'")


def keep_count(value, where):
    try:
        n = int(str(value).strip())
    except ValueError:
        raise SettingsProblem(f"{where}: keep should be a number from 1 to {MAX_KEEP}, not '{value}'")
    if not 1 <= n <= MAX_KEEP:
        raise SettingsProblem(f"{where}: keep should be from 1 to {MAX_KEEP}, not {n}")
    return n


def load_shows(quiet=False, warnings=None):
    """Every show, with defaults filled in. Each is a dict:
      name, folder (e.g. 'hollywood-handbook'), feed, enabled, keep, skip_titles,
      problem (None, or why this show can't run. The line logs it and carries on).
    quiet: don't print warnings (for stations that only look a show up).
    warnings: a list to add each warning to, so the line can count them as problems
      (a typo like 'enabeld' would otherwise only scroll past on a screen nobody reads)."""
    def warn(message):
        if warnings is not None:
            warnings.append(message)
        if not quiet:
            print(f"  shows.local.yaml: {message}")

    if not SHOWS_FILE.exists():
        raise ShowsFileError("No shows.local.yaml found. Copy shows.example.yaml to "
                             "shows.local.yaml and put your shows in it.")
    try:
        with open(SHOWS_FILE, encoding="utf-8") as f:
            # BaseLoader reads every value as plain text, so a show called 1883 or Yes
            # stays "1883" or "Yes" instead of turning into a number or true/false.
            data = yaml.load(f, Loader=yaml.BaseLoader) or {}
    except yaml.YAMLError as err:
        # Built by hand: YAML's own message quotes the broken line, which could be a feed URL.
        mark = getattr(err, "problem_mark", None)
        where = f" on line {mark.line + 1}" if mark else ""
        problem = getattr(err, "problem", None) or "it isn't valid YAML"
        raise ShowsFileError(f"Can't read shows.local.yaml: there's a mistake{where} ({problem}). "
                             f"Check the spacing and colons there.") from None
    if not isinstance(data, dict):
        data = {}

    # The defaults: section, checked. A bad value there falls back to the built-in default.
    defaults = dict(BUILT_IN_DEFAULTS)
    raw_defaults = data.get("defaults") or {}
    if not isinstance(raw_defaults, dict):
        warn("defaults: should be a list of options. Using the built-in defaults.")
        raw_defaults = {}
    for key in raw_defaults:
        if key not in DEFAULT_OPTIONS:
            warn(f"defaults: unknown option '{key}' (ignored). A typo?")
    try:
        if "enabled" in raw_defaults:
            defaults["enabled"] = yes_no(raw_defaults["enabled"], "defaults")
        if "keep" in raw_defaults:
            defaults["keep"] = keep_count(raw_defaults["keep"], "defaults")
    except SettingsProblem as err:
        warn(f"{err}. Using the built-in default.")

    shows, seen_folders = [], {}
    for number, raw in enumerate(data.get("shows") or [], start=1):
        if not isinstance(raw, dict):
            warn(f"show #{number} isn't written as a show (name:, feed: ...). Skipped.")
            continue
        name = str(raw.get("name") or "").strip()
        label = name or f"show #{number}"
        for key in raw:
            if key not in SHOW_OPTIONS:
                warn(f"{label}: unknown option '{key}' (ignored). A typo?")
        show = {"name": name, "folder": slugify(name) if name else "",
                "feed": str(raw.get("feed") or "").strip(), "enabled": defaults["enabled"],
                "keep": defaults["keep"], "skip_titles": as_list(raw.get("skip_titles")),
                "problem": None}
        try:
            if not name or not show["feed"]:
                raise SettingsProblem(f"{label}: needs both a name and a feed")
            show["enabled"] = yes_no(raw.get("enabled", defaults["enabled"]), label)
            show["keep"] = keep_count(raw.get("keep", defaults["keep"]), label)
            if show["folder"] in seen_folders:
                raise SettingsProblem(f"{label}: has the same folder name ('{show['folder']}') "
                                      f"as {seen_folders[show['folder']]}. Rename one of them")
            seen_folders[show["folder"]] = label
        except SettingsProblem as err:
            show["problem"] = str(err)
        shows.append(show)
    return shows


DELIVERY_ROADS = ("home", "cloud", "both", "off")


def delivery_setting(warnings=None):
    """The delivery: line at the top of shows.local.yaml: home, cloud, both or off.
    Missing means off. A value that isn't one of those is a warning, and means off."""
    try:
        with open(SHOWS_FILE, encoding="utf-8") as f:
            data = yaml.load(f, Loader=yaml.BaseLoader) or {}
    except (OSError, yaml.YAMLError):
        return "off"   # the Dailies station already reports a missing or broken file
    value = str(data.get("delivery", "off") if isinstance(data, dict) else "off").strip().lower()
    if value not in DELIVERY_ROADS:
        message = f"delivery should be home, cloud, both or off, not '{value}'. Delivery is off."
        if warnings is not None:
            warnings.append(message)
        return "off"
    return value


CLEANUP_DEFAULTS = {"keep_clean": 3, "clean_min_days": 7, "raw_days": 7, "text_days": 30, "test_days": 7}
CLEANUP_LIMITS = {"keep_clean": (1, 50), "clean_min_days": (0, 3650), "raw_days": (1, 3650),
                  "text_days": (1, 3650), "test_days": (1, 3650)}


def cleanup_settings(warnings=None):
    """The cleanup: section of shows.local.yaml, with the defaults filled in. A missing or
    bad value uses the default, with a warning for a bad one."""
    settings = dict(CLEANUP_DEFAULTS)
    def warn(message):
        if warnings is not None:
            warnings.append(message)
    try:
        with open(SHOWS_FILE, encoding="utf-8") as f:
            data = yaml.load(f, Loader=yaml.BaseLoader) or {}
    except (OSError, yaml.YAMLError):
        return settings
    raw = data.get("cleanup") if isinstance(data, dict) else None
    if raw is None:
        return settings
    if not isinstance(raw, dict):
        warn("cleanup: should be a list of settings. Using the defaults.")
        return settings
    for key, value in raw.items():
        if key not in CLEANUP_DEFAULTS:
            warn(f"cleanup: unknown setting '{key}' (ignored). A typo?")
            continue
        low, high = CLEANUP_LIMITS[key]
        try:
            n = int(str(value).strip())
            if not low <= n <= high:
                raise ValueError
            settings[key] = n
        except ValueError:
            warn(f"cleanup: {key} should be a number from {low} to {high}, not '{value}'. "
                 f"Using {CLEANUP_DEFAULTS[key]}.")
    return settings


def find_show(folder):
    """The show whose episodes live in this folder name, or None.
    Works without shows.local.yaml too (returns None), e.g. on someone else's computer."""
    if not SHOWS_FILE.exists():
        return None
    try:
        return next((s for s in load_shows(quiet=True) if s["folder"] == folder), None)
    except ShowsFileError:
        return None   # a broken show list shouldn't stop spotting: fall back to the folder name


def show_name(folder):
    """The show's real name, e.g. 'Hollywood Handbook'. Falls back to the folder name."""
    show = find_show(folder)
    return show["name"] if show else folder.replace("-", " ").title()


def notes_path(folder):
    return NOTES_DIR / f"{folder}.md"


def notes_for(folder):
    """This show's notes for the Spotter, or None if it has none yet."""
    path = notes_path(folder)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None
