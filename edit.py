"""
Station 4: The Edit.

Takes an original episode and its cut list, and renders a clean episode with the ads
removed, following the house style in edit-settings.yaml.

For a transcript at episodes/transcripts/<show>/<name>.json with a <name>.cuts.json
next to it, the original is read from episodes/raw/<show>/ and the clean episode is
written to:
  episodes/clean/<show>/<name>.mp3

The original download is NEVER changed. It is only ever read.

Run it with:
  .venv\\Scripts\\python edit.py                    every episode with a cut list but no clean version
  .venv\\Scripts\\python edit.py <transcript.json>  just that one
  add --redo to render again even if a clean version exists
  add --one-cut to make only the first cut, as a test file in episodes/test/
  add --seams to make a review reel of every seam in an already-clean episode,
      saved in episodes/test/<name>.seams.mp3, with a numbered list to read along
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

import spot

PROJECT = Path(__file__).resolve().parent
RAW_DIR = PROJECT / "episodes" / "raw"
CLEAN_DIR = PROJECT / "episodes" / "clean"
SETTINGS_FILE = PROJECT / "edit-settings.yaml"

# The length check: the clean episode must come out within this many seconds of
# (original length - everything cut - the overlap at each crossfade), or the edit is refused.
LENGTH_TOLERANCE = 0.5
FFMPEG_LIMIT = 15 * 60   # seconds any one ffmpeg job may take (one episode takes under a minute)
# Tags copied from the original into the clean episode. The title gets " (clean)" added.
COPY_TAGS = ["title", "album", "artist", "album_artist", "date", "genre"]

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ---------- protecting the originals ----------

class OriginalProtected(RuntimeError):
    """Raised instead of ever writing over, or next to, an original download."""


def _same_place(path):
    """A path in a form that can be compared safely on Windows
    (full path, no '..', no shortcuts, upper/lower case ignored)."""
    return os.path.normcase(str(Path(path).resolve()))


def check_output_is_safe(output, original):
    """Stop before writing if the output would land on the original or anywhere in episodes/raw/."""
    out = _same_place(output)
    if out == _same_place(original):
        raise OriginalProtected(
            f"REFUSED: the output would overwrite the original download ({Path(original).name}). "
            f"Nothing was written.")
    raw = _same_place(RAW_DIR)
    if out == raw or out.startswith(raw + os.sep):
        raise OriginalProtected(
            f"REFUSED: the output would be written inside episodes/raw/, where the originals live "
            f"({output}). Nothing was written.")


# ---------- reading ----------

def load_settings():
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        s = yaml.safe_load(f) or {}
    return {
        "cut_confidence": [str(c).lower() for c in s.get("cut_confidence", ["high"])],
        "crossfade": float(s.get("crossfade", 0.25)),
        "padding": float(s.get("padding", 0.15)),
        "merge_gap": float(s.get("merge_gap", 5.0)),
        "bitrate": str(s.get("bitrate", "match")),
        "sample_rate": str(s.get("sample_rate", "match")),
        "channels": str(s.get("channels", "match")),
    }


def probe(path):
    """Length and audio format of a file, from ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=bit_rate,sample_rate,channels:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=FFMPEG_LIMIT).stdout
    info = json.loads(out)
    stream = (info.get("streams") or [{}])[0]
    return {
        "duration": float(info["format"]["duration"]),
        "bit_rate": int(stream.get("bit_rate") or 128000),
        "sample_rate": int(stream.get("sample_rate") or 44100),
        "channels": int(stream.get("channels") or 2),
    }


def true_length(path):
    """The real length, found by decoding every bit of audio (a few seconds per episode).
    ffprobe's quick answer is an estimate, and some MP3s report it wrong."""
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0",
         "-f", "null", "-", "-progress", "pipe:1", "-nostats"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, timeout=FFMPEG_LIMIT).stdout
    times = [line.split("=", 1)[1] for line in out.splitlines() if line.startswith("out_time_us=")]
    values = [int(t) for t in times if t.strip().lstrip("-").isdigit()]
    if not values:
        raise RuntimeError(f"couldn't measure the length of {Path(path).name}")
    return max(values) / 1_000_000


def clear_leftover(part):
    """Remove a leftover temporary file from a crashed run before writing a new one.
    (Removing it only deletes that name. If it were ever a second name for another file,
    writing into it could change that file; deleting it can't.)"""
    if part.exists() or part.is_symlink():
        part.unlink()


def read_tags(path):
    """The original's tags (title, show name...) and which stream holds its cover art, if any."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format_tags:stream=index,codec_type:stream_disposition=attached_pic",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, timeout=FFMPEG_LIMIT).stdout
    info = json.loads(out)
    tags = {k.lower(): v for k, v in (info.get("format", {}).get("tags") or {}).items()}
    cover = next((s["index"] for s in info.get("streams", [])
                  if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic")),
                 None)
    return tags, cover


def find_original(transcript):
    audio = Path(transcript["audio"])
    return audio if audio.is_absolute() else PROJECT / audio


# ---------- planning the cuts ----------

def mmss(seconds):
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def plan_cuts(cuts, words, duration, settings):
    """Pick, pad and merge the cuts. Returns (list of (start, end) to remove, notes)."""
    notes = []
    chosen = sorted((c for c in cuts if c["confidence"] in settings["cut_confidence"]),
                    key=lambda c: c["start"])
    timed = [w for w in words if w.get("start") is not None and w.get("end") is not None]

    # "Show words" are words outside every cut being made. Padding never reaches into one.
    def in_a_cut(w):
        mid = (w["start"] + w["end"]) / 2
        return any(c["start"] <= mid <= c["end"] for c in chosen)
    show_words = [w for w in timed if not in_a_cut(w)]

    pad = settings["padding"]
    padded = []
    for c in chosen:
        start, end = c["start"], c["end"]
        before = [w["end"] for w in show_words if w["end"] <= start]
        after = [w["start"] for w in show_words if w["start"] >= end]
        # Wider by `padding`, but stop halfway to the nearest show word.
        new_start = max(start - pad, (max(before) + start) / 2) if before else start - pad
        new_end = min(end + pad, (end + min(after)) / 2) if after else end + pad
        padded.append([max(0.0, new_start), min(duration, new_end), [c["id"]]])

    # Merge cuts that overlap, touch, or sit close together with nobody talking between them.
    merged = []
    for cut in padded:
        if merged:
            prev = merged[-1]
            gap = cut[0] - prev[1]
            words_between = any(w["end"] > prev[1] and w["start"] < cut[0] for w in timed)
            if gap <= 0 or (gap < settings["merge_gap"] and not words_between):
                if gap > 0:
                    notes.append(f"merged cuts {prev[2][-1]} and {cut[2][0]}: "
                                 f"{gap:.1f}s apart at {mmss(prev[1])} with no words between")
                prev[1] = max(prev[1], cut[1])
                prev[2] += cut[2]
                continue
        merged.append(cut)

    # Same rule at the very start and end: a short wordless sliver left before the first cut
    # or after the last one is part of the break, not the show.
    if merged:
        first, last = merged[0], merged[-1]
        if 0 < first[0] < settings["merge_gap"] and not any(w["start"] < first[0] for w in timed):
            notes.append(f"cut {first[2][0]} extended to the very start ({first[0]:.1f}s with no words)")
            first[0] = 0.0
        tail = duration - last[1]
        if 0 < tail < settings["merge_gap"] and not any(w["end"] > last[1] for w in timed):
            notes.append(f"cut {last[2][-1]} extended to the very end ({tail:.1f}s with no words)")
            last[1] = duration
    return [(a, b, ids) for a, b, ids in merged], notes


def keep_pieces(removals, duration):
    """Flip the cuts into the stretches of show to keep, in order."""
    pieces, cursor = [], 0.0
    for start, end, _ in removals:
        if start - cursor > 0.01:
            pieces.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor > 0.01:
        pieces.append((cursor, duration))
    return pieces


def crossfades_for(pieces, crossfade):
    """Crossfade length at each join. A very short piece gets a shorter fade so it isn't swallowed."""
    return [min(crossfade, (a1 - a0) / 3, (b1 - b0) / 3)
            for (a0, a1), (b0, b1) in zip(pieces, pieces[1:])]


def filter_graph(pieces, fades, fade_in, fade_out, crossfade):
    """One ffmpeg filter graph: trim every piece from the single input, crossfade them together."""
    n = len(pieces)
    lines = [f"[0:a]asplit={n}" + "".join(f"[s{i}]" for i in range(n)) if n > 1 else "[0:a]anull[s0]"]
    for i, (a, b) in enumerate(pieces):
        chain = f"[s{i}]atrim=start={a:.6f}:end={b:.6f},asetpts=PTS-STARTPTS"
        if i == 0 and fade_in:
            chain += f",afade=t=in:d={min(crossfade, (b - a) / 3):.3f}"
        if i == n - 1 and fade_out:
            d = min(crossfade, (b - a) / 3)
            chain += f",afade=t=out:st={(b - a) - d:.6f}:d={d:.3f}"
        lines.append(chain + f"[p{i}]")
    last = "p0"
    for i, d in enumerate(fades, start=1):
        lines.append(f"[{last}][p{i}]acrossfade=d={d:.3f}:c1=tri:c2=tri[x{i}]")
        last = f"x{i}"
    lines.append(f"[{last}]anull[out]")
    return ";\n".join(lines)


def layout(removals, duration, settings):
    """The keep pieces, the crossfade at each join, and whether the ends need a fade."""
    pieces = keep_pieces(removals, duration)
    if not pieces:
        raise RuntimeError("the cut list would remove the whole episode. Nothing was written.")
    fades = crossfades_for(pieces, settings["crossfade"])
    fade_in = pieces[0][0] > 0.01               # a cut at the very start: fade in
    fade_out = pieces[-1][1] < duration - 0.01  # a cut at the very end: fade out
    return pieces, fades, fade_in, fade_out


# ---------- rendering ----------

def load_episode(transcript_path):
    """The transcript, its cut list file, and the original audio it came from."""
    transcript_path = Path(transcript_path).resolve()
    cuts_json = spot.cut_paths(transcript_path)[0]
    if not cuts_json.exists():
        raise RuntimeError(f"no cut list for {transcript_path.stem}. Run spot.py first.")
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    original = find_original(transcript)
    if not original.exists():
        raise RuntimeError(f"original audio not found: {original}")
    return transcript_path, cuts_json, transcript, original


def clean_path(transcript_path, original):
    return CLEAN_DIR / Path(transcript_path).parent.name / f"{original.stem}.mp3"


COVER_NAME = "folder.jpg"   # AntennaPod uses this picture as a local folder's show art
COVER_MAX = 1400            # pixels; big enough for any phone, much smaller than some originals


def publish_time(path):
    """The publish date at the start of the file name (2026-11-17_...), as noon that day.
    None if the name has no date (fetch.py writes 'undated' when a feed gives none)."""
    try:
        day = time.strptime(Path(path).name[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return time.mktime(day[:3] + (12, 0, 0, 0, 0, -1))


def date_like_published(path):
    """Set the clean file's 'modified' date to its publish date, so the phone sorts by when
    the episode came out, not when it was edited. Syncthing carries this date to the phone.
    Only ever used on clean files."""
    raw = _same_place(RAW_DIR)
    if _same_place(path).startswith(raw + os.sep):
        raise OriginalProtected(f"REFUSED: won't change the date of an original ({Path(path).name}).")
    when = publish_time(path)
    if when is not None:
        os.utime(path, (time.time(), when))
    return when


def save_show_cover(clean_file):
    """Save the cover art inside a clean episode as folder.jpg in its show folder.
    Only for files in episodes/clean/<show>/. Returns a short note, never raises:
    a missing picture is cosmetic and must never cost an episode."""
    clean_file = Path(clean_file)
    if not _same_place(clean_file.parent.parent) == _same_place(CLEAN_DIR):
        return None
    cover = clean_file.parent / COVER_NAME
    part = cover.with_name("folder.part.jpg")
    try:
        check_output_is_safe(cover, clean_file)
        clear_leftover(part)
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(clean_file), "-map", "0:v:0", "-frames:v", "1",
             "-vf", f"scale='min({COVER_MAX},iw)':-2", "-q:v", "3", str(part)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=FFMPEG_LIMIT)
        if result.returncode != 0 or not part.exists():
            clear_leftover(part)
            return "no show art saved (no cover art in this episode)"
        os.replace(part, cover)
        return f"show art saved as {COVER_NAME}"
    except Exception as err:
        clear_leftover(part)
        return f"show art not saved ({type(err).__name__})"


def is_edited(transcript_path):
    """True if this transcript's episode already has a clean version."""
    transcript = json.loads(Path(transcript_path).read_text(encoding="utf-8"))
    return clean_path(transcript_path, find_original(transcript)).exists()


def edit_episode(transcript_path, redo=False, output=None, only_first=None):
    """Render one clean episode. Returns a summary dict, or None if skipped.
    output: write somewhere other than episodes/clean/ (used for tests).
    only_first: make only the first N cuts, counted after back-to-back ads are merged
                (used for the one-cut test)."""
    settings = load_settings()
    transcript_path, cuts_json, transcript, original = load_episode(transcript_path)

    if output is None:
        output = clean_path(transcript_path, original)
    output = Path(output)
    part = output.with_name(output.name + ".part")
    # Check BOTH the finished file and the temporary .part file before anything is written.
    check_output_is_safe(output, original)
    check_output_is_safe(part, original)

    if output.exists() and not redo:
        print(f"already edited: {original.stem}")
        return None

    print(f"\nEditing: {original.stem}")
    start_time = time.time()
    info = probe(original)
    duration = true_length(original)
    if abs(duration - info["duration"]) > LENGTH_TOLERANCE:
        print(f"  note: the file says it's {info['duration']:.2f}s long, but it really is "
              f"{duration:.2f}s. Using the real length.")

    cuts = json.loads(cuts_json.read_text(encoding="utf-8"))["cuts"]
    words = transcript.get("words", [])
    removals, notes = plan_cuts(cuts, words, duration, settings)
    if only_first:
        # After merging, so back-to-back ads in one break count as one cut (one seam).
        removals = removals[:only_first]
    check_plan(cuts, words, removals, duration, settings, partial=bool(only_first))
    pieces, fades, fade_in, fade_out = layout(removals, duration, settings)

    bitrate = (f"{round(info['bit_rate'] / 1000)}k" if settings["bitrate"] == "match"
               else settings["bitrate"])
    rate = str(info["sample_rate"]) if settings["sample_rate"] == "match" else settings["sample_rate"]
    channels = str(info["channels"]) if settings["channels"] == "match" else settings["channels"]

    # Tags: copy the title, show name etc. from the original, and mark the title "(clean)".
    # Everything else is dropped (e.g. the original's stored length, which would now be wrong).
    tags, cover = read_tags(original)
    tag_args = ["-map_metadata", "-1"]
    for key in COPY_TAGS:
        value = tags.get(key)
        if key == "title":
            value = f"{value or original.stem} (clean)"
        if value:
            tag_args += ["-metadata", f"{key}={value}"]
    # Cover art: copied as-is (no re-encode) and marked as the episode's picture.
    cover_args = (["-map", f"0:{cover}", "-c:v", "copy", "-disposition:v:0", "attached_pic",
                   "-metadata:s:v:0", "comment=Cover (front)"] if cover is not None else [])

    output.parent.mkdir(parents=True, exist_ok=True)
    clear_leftover(part)
    with tempfile.TemporaryDirectory(prefix="edit-") as tmp:
        # The filter graph goes in a file: Windows limits how long a command line can be.
        graph_file = Path(tmp) / "graph.txt"
        graph_file.write_text(filter_graph(pieces, fades, fade_in, fade_out, settings["crossfade"]),
                              encoding="utf-8")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(original),                      # the original is only ever an INPUT
               "-/filter_complex", str(graph_file),
               "-map", "[out]", *cover_args,
               "-c:a", "libmp3lame", "-b:a", bitrate, "-ar", rate, "-ac", channels,
               *tag_args, "-id3v2_version", "3",
               "-f", "mp3", str(part)]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=FFMPEG_LIMIT)
    if result.returncode != 0:
        if part.exists():
            part.unlink()
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr.strip()[-1500:]}")

    # The length check. This proves ffmpeg rendered what was planned: nothing dropped,
    # doubled or cut short. (check_plan, above, is what proves the plan itself was right.)
    clean_length = true_length(part)
    removed = sum(min(b, duration) - max(a, 0.0) for a, b, _ in removals)
    expected = duration - removed - sum(fades)
    off = clean_length - expected
    if abs(off) > LENGTH_TOLERANCE:
        part.unlink()
        raise RuntimeError(
            f"LENGTH CHECK FAILED for {original.stem}: the clean file is {clean_length:.2f}s but "
            f"should be {expected:.2f}s (original {duration:.2f}s - cut {removed:.2f}s - "
            f"crossfades {sum(fades):.2f}s). Off by {off:+.2f}s, more than {LENGTH_TOLERANCE}s "
            f"allowed. Nothing was saved.")

    # The kept list goes first. If it fails, the clean episode isn't saved either, so the
    # next run tries the whole episode again instead of thinking it's done.
    try:
        kept_file, kept, gone = write_kept(output, original, cuts, settings, removals,
                                           pieces, fades, clean_length)
    except Exception:
        part.unlink()
        raise
    check_output_is_safe(output, original)   # one last check before the rename
    os.replace(part, output)
    dated = date_like_published(output)
    art_note = save_show_cover(output) if cover is not None else None
    seconds = time.time() - start_time

    print(f"  {len(removals)} cuts made ({sum(len(r[2]) for r in removals)} from the cut list), "
          f"{len(fades)} seams" + (", fade-in at start" if fade_in else "") +
          (", fade-out at end" if fade_out else ""))
    for note in notes:
        print(f"  {note}")
    print(f"  original {mmss(duration)}, removed {mmss(removed)}, "
          f"clean {mmss(clean_length)} (expected {mmss(expected)}, off by {off:+.2f}s: "
          f"length check passed)")
    print(f"  title: {tags.get('title', original.stem)} (clean)"
          + (", cover art copied" if cover is not None else ", no cover art in the original"))
    print("  file date: " + (time.strftime("%Y-%m-%d", time.localtime(dated)) + " (publish date)"
                             if dated else "left as today (no date in the file name)")
          + (f"; {art_note}" if art_note else ""))
    print(f"  left in: {len(kept)} medium/low mark(s), listed in {kept_file.name}"
          + (f" ({gone} more fell inside a high cut and went with it)" if gone else ""))
    print(f"  saved: {output.relative_to(PROJECT) if output.is_relative_to(PROJECT) else output}  "
          f"({seconds:.0f}s)")
    return {"episode": original.stem, "show": transcript_path.parent.name, "output": output,
            "removals": removals, "pieces": pieces, "fades": fades, "original_length": duration,
            "clean_length": clean_length, "expected_length": expected, "seconds": seconds,
            "kept": kept, "title": str(tags.get("title") or original.stem).strip()}


# ---------- what got left in ----------

def to_clean_time(t, pieces, fades):
    """Convert a time in the original to the same moment in the clean episode.

    Walk through the kept pieces in order, keeping track of where each one starts in the
    clean episode. The first piece starts at 0. Each next piece starts where the previous
    one ended, minus its crossfade (the two pieces overlap for that long). Once we find
    the piece that holds t, the answer is that piece's clean start plus how far t is into it.
    Returns (clean time, True if t was inside a cut and got moved to the seam)."""
    clean_start = 0.0
    for i, (a, b) in enumerate(pieces):
        if i > 0:
            clean_start -= fades[i - 1]
        if t < a:                       # t was cut out: it lands where the next piece starts
            return clean_start, True
        if t <= b:
            return clean_start + (t - a), False
        clean_start += b - a
    return clean_start, True


def check_plan(cuts, words, removals, duration, settings, partial=False):
    """A check of the plan that doesn't trust plan_cuts: straight from the Spotter's cut list
    and the transcript words. Every high mark must be removed in full, and no word of the
    show may land inside a cut. Stops before anything is rendered if either fails.
    partial: only some cuts are being made (the one-cut test), so only check those."""
    chosen = [c for c in cuts if c["confidence"] in settings["cut_confidence"]]
    planned = {i for _, _, ids in removals for i in ids}
    must_remove = [c for c in chosen if c["id"] in planned] if partial else chosen
    problems = []
    for c in must_remove:
        s, e = max(0.0, c["start"]), min(duration, c["end"])
        if not any(a - 0.01 <= s and e <= b + 0.01 for a, b, _ in removals):
            problems.append(f"cut list #{c['id']} ({tc(s)} to {tc(e)}) would not be fully removed")
    for w in words:
        if w.get("start") is None or w.get("end") is None:
            continue
        mid = (w["start"] + w["end"]) / 2
        if any(c["start"] <= mid <= c["end"] for c in chosen):
            continue                                    # an ad word: it's meant to go
        if any(a < mid < b for a, b, _ in removals):
            problems.append(f"show word \"{w['word']}\" at {tc(mid)} would be cut")
    if problems:
        shown = "\n  ".join(problems[:10]) + (f"\n  ...and {len(problems) - 10} more"
                                               if len(problems) > 10 else "")
        raise RuntimeError(f"PLAN CHECK FAILED. Nothing was written.\n  {shown}")


def write_kept(output, original, cuts, settings, removals, pieces, fades, clean_length):
    """Write <episode>.kept.txt next to the clean episode: every mark that stayed in,
    with its time in the clean episode. A medium/low mark that sits inside a high cut went
    with it, so it's listed separately, and one that's partly inside is flagged.
    Returns (file, marks left in, how many went with a high cut)."""
    kept_file = output.with_suffix(".kept.txt")
    check_output_is_safe(kept_file, original)
    kept, gone = [], []
    for c in sorted(cuts, key=lambda c: c["start"]):
        if c["confidence"] in settings["cut_confidence"]:
            continue
        length = max(0.0, c["end"] - c["start"])
        inside = sum(max(0.0, min(b, c["end"]) - max(a, c["start"])) for a, b, _ in removals)
        start, moved = to_clean_time(c["start"], pieces, fades)
        end, _ = to_clean_time(c["end"], pieces, fades)
        mark = {**c, "clean_start": start, "clean_end": end, "moved": moved, "inside": inside}
        (gone if inside >= length - 0.05 else kept).append(mark)

    def describe(k):
        return [f"  {k['id']:>2}  {tc(k['clean_start']):<10} ({tc(k['start'])})"
                f"{'':<{max(0, 10 - len(tc(k['start'])))}} "
                f"{tc(k['clean_end'] - k['clean_start']):>6}  {k.get('type', '?'):<8} "
                f"{str(k.get('confidence', '?')).upper():<7} {k.get('reason', '')}",
                f"      \"{k.get('first_words', '')}\" ... \"{k.get('last_words', '')}\""]

    lines = [f"Left in: {original.stem}",
             f"Clean episode {tc(clean_length)}. The Spotter flagged these, but only "
             f"{'/'.join(settings['cut_confidence'])} gets cut, so they're still in.",
             "Times are in the CLEAN episode. The original's time is in brackets.", ""]
    if not kept:
        lines.append("  Nothing was left in.")
    else:
        lines.append(f"  {'#':>2}  {'CLEAN AT':<10} {'(ORIGINAL)':<12} {'LEN':>6}  "
                     f"{'TYPE':<8} {'CONF':<7} REASON")
        for k in kept:
            lines += describe(k)
            if k["inside"] > 0.05:
                lines.append(f"      PARTLY CUT: {k['inside']:.1f}s of it was inside a high cut, "
                             f"so only part of it is still in.")
            elif k["moved"]:
                lines.append("      (its start was inside a cut, so it's listed at the seam)")
    if gone:
        lines += ["", "Went anyway: these were medium/low, but sat inside a high cut, so they're gone.",
                  "(The clean time shown is the seam where they were.)"]
        for k in gone:
            lines += describe(k)
    clear_leftover(kept_file.with_name(kept_file.name + ".part"))
    spot.write_safely(kept_file, "\n".join(lines) + "\n")
    return kept_file, kept, len(gone)


# ---------- the seams reel ----------

REEL_CONTEXT = 5.0   # seconds of clean episode before and after each seam
REEL_GAP = 1.0       # seconds of silence between seams in the reel


def tc(seconds):
    """A timecode with tenths, like 1:37.9 or 1:25:15.2."""
    seconds = max(0.0, seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{int(h)}:{int(m):02d}:{s:04.1f}" if h else f"{int(m)}:{s:04.1f}"


def seam_times(pieces, fades):
    """Where each seam lands in the clean episode: the middle of its crossfade.
    Each crossfade overlaps the two pieces, so every seam pulls the timeline in by its length."""
    times, cursor = [], pieces[0][1] - pieces[0][0]
    for (a, b), d in zip(pieces[1:], fades):
        times.append(cursor - d / 2)
        cursor += (b - a) - d
    return times


def seams_reel(transcript_path):
    """Make a short review file of every seam in an already-edited episode, and print a list."""
    settings = load_settings()
    transcript_path, cuts_json, transcript, original = load_episode(transcript_path)
    clean = clean_path(transcript_path, original)
    if not clean.exists():
        raise RuntimeError(f"no clean episode yet for {original.stem}. Run edit.py first.")

    # Re-plan the same cuts the edit made, so we know where every seam is.
    info = probe(original)
    duration = true_length(original)
    cuts = json.loads(cuts_json.read_text(encoding="utf-8"))["cuts"]
    by_id = {c["id"]: c for c in cuts}
    removals, _ = plan_cuts(cuts, transcript.get("words", []), duration, settings)
    pieces, fades, fade_in, fade_out = layout(removals, duration, settings)

    # If the cut list or settings changed since the edit, these seam times would be wrong.
    clean_length = true_length(clean)
    expected = sum(b - a for a, b in pieces) - sum(fades)
    if abs(clean_length - expected) > LENGTH_TOLERANCE:
        raise RuntimeError(
            f"the clean episode doesn't match the current cut list and settings "
            f"({clean_length:.1f}s, expected {expected:.1f}s). Run edit.py --redo on it first.")

    # Every edit to review: the seams, plus a fade-in/fade-out where a cut reached an end.
    middle = [r for r in removals if r[0] > 0.01 and r[1] < duration - 0.01]
    seams = seam_times(pieces, fades)
    if len(middle) != len(seams):
        raise RuntimeError("couldn't line the seams up with the cuts. Nothing was written.")
    points = [("seam", t, r) for t, r in zip(seams, middle)]
    if fade_in:
        points.insert(0, ("start", 0.0, removals[0]))
    if fade_out:
        points.append(("end", clean_length, removals[-1]))
    if not points:
        print(f"\n{original.stem}: no cuts, so no seams to review.")
        return None
    clips = [(max(0.0, t - REEL_CONTEXT), min(clean_length, t + REEL_CONTEXT)) for _, t, _ in points]

    output = PROJECT / "episodes" / "test" / f"{original.stem}.seams.mp3"
    part = output.with_name(output.name + ".part")
    check_output_is_safe(output, original)
    check_output_is_safe(part, original)

    # One filter graph: trim each clip from the clean episode, put silence between, join.
    rate, channels = info["sample_rate"], info["channels"]
    fmt = (f"aformat=sample_fmts=fltp:sample_rates={rate}:"
           f"channel_layouts={'mono' if channels == 1 else 'stereo'}")
    n = len(clips)
    lines = [f"[0:a]asplit={n}" + "".join(f"[s{i}]" for i in range(n)) if n > 1 else "[0:a]anull[s0]"]
    order = []
    for i, (a, b) in enumerate(clips):
        lines.append(f"[s{i}]atrim=start={a:.6f}:end={b:.6f},asetpts=PTS-STARTPTS,{fmt}[c{i}]")
        order.append(f"[c{i}]")
        if i < n - 1:
            lines.append(f"anullsrc=r={rate}:cl={'mono' if channels == 1 else 'stereo'},"
                         f"atrim=duration={REEL_GAP},{fmt}[g{i}]")
            order.append(f"[g{i}]")
    lines.append("".join(order) + f"concat=n={len(order)}:v=0:a=1[out]")

    output.parent.mkdir(parents=True, exist_ok=True)
    clear_leftover(part)
    with tempfile.TemporaryDirectory(prefix="seams-") as tmp:
        graph_file = Path(tmp) / "graph.txt"
        graph_file.write_text(";\n".join(lines), encoding="utf-8")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(clean), "-/filter_complex", str(graph_file), "-map", "[out]",
               "-c:a", "libmp3lame", "-b:a", f"{round(info['bit_rate'] / 1000)}k",
               "-f", "mp3", str(part)]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=FFMPEG_LIMIT)
    if result.returncode != 0:
        if part.exists():
            part.unlink()
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr.strip()[-1500:]}")
    os.replace(part, output)

    # The list to read along with.
    print(f"\nSeams reel: {original.stem}")
    reel_pos = 0.0
    for i, ((kind, t, (a, b, ids)), (c0, c1)) in enumerate(zip(points, clips), start=1):
        at_in_reel = reel_pos + (t - c0)
        label = {"seam": "SEAM", "start": "FADE-IN (cut at the very start)",
                 "end": "FADE-OUT (cut at the very end)"}[kind]
        print(f"\n {i}. {label}: {tc(t)} in the clean episode, {tc(at_in_reel)} in the reel")
        print(f"    cut {tc(a)} to {tc(b)} of the original ({tc(b - a)} removed), "
              f"cut list #{', #'.join(str(x) for x in ids)}:")
        for x in ids:
            c = by_id.get(x, {})
            reason = c.get("reason", "")
            reason = reason if len(reason) <= 70 else reason[:67] + "..."
            print(f"      #{x} {c.get('type', '?')}: {reason}")
        reel_pos += (c1 - c0) + REEL_GAP
    reel_length = reel_pos - REEL_GAP
    print(f"\n  saved: {output.relative_to(PROJECT)}  ({tc(reel_length)} long, {len(points)} to check)")
    return {"output": output, "points": points, "reel_length": reel_length}


def main():
    args = sys.argv[1:]
    redo = "--redo" in args
    one_cut = "--one-cut" in args   # test: only the first cut, saved in episodes/test/
    seams = "--seams" in args       # review reel of every seam in a clean episode
    paths = [Path(a) for a in args if a not in ("--redo", "--one-cut", "--seams")]
    if not paths:
        paths = [t for t in spot.all_transcripts() if spot.cut_paths(t)[0].exists()]
        if seams:
            paths = [t for t in paths
                     if clean_path(t, find_original(json.loads(t.read_text(encoding="utf-8")))).exists()]
    if not paths:
        print("No episodes to work on. Run spot.py (and edit.py for --seams) first.")
    failed = 0
    for path in paths:
        try:
            if seams:
                seams_reel(path)
                continue
            if one_cut:
                out = PROJECT / "episodes" / "test" / f"{Path(path).stem}.one-cut.mp3"
                result = edit_episode(path, redo=True, output=out, only_first=1)
                a, b, ids = result["removals"][0]
                print(f"  cut {a:.2f}s-{b:.2f}s of the original (cut list ids {ids})")
                if not result["fades"]:
                    # A cut at the very start or end leaves nothing on one side: no seam to hear.
                    where = "start" if a <= 0.01 else "end"
                    print(f"  NO SEAM: this cut reaches the very {where} of the episode, so the test "
                          f"file just {'fades in' if where == 'start' else 'fades out'} there.")
                    continue
                d = result["fades"][0]
                seam = seam_times(result["pieces"], result["fades"])[0]
                print(f"  SEAM in the test file at {seam:.2f}s ({int(seam // 60)}:{seam % 60:05.2f}), "
                      f"crossfade from {seam - d / 2:.2f}s to {seam + d / 2:.2f}s")
                continue
            edit_episode(path, redo=redo)
        except OriginalProtected as err:
            print(f"  {err}")
            failed += 1
        except Exception as err:
            print(f"  ERROR: {err}")
            failed += 1
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
