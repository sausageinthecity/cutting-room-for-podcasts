"""
Station 2: Logging.

Transcribes audio on the GPU with WhisperX, with a start and end time on every word.

For episodes/raw/<show>/<name>.mp3 it writes:
  episodes/transcripts/<show>/<name>.json   every sentence and every word, with times (seconds)
  episodes/transcripts/<show>/<name>.txt    one sentence per line: [HH:MM:SS] text

If both files already exist, the episode is skipped.

Run it with:  .venv\\Scripts\\python transcribe.py episodes\\raw\\<show>\\<file>.mp3
"""

import json
import logging
import os
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
TRANSCRIPTS_DIR = PROJECT / "episodes" / "transcripts"
MODELS_DIR = PROJECT / "models"
MODEL_NAME = "large-v3"   # the biggest Whisper; uses ~5 GB of the 3080's 10 GB
LANGUAGE = "en"
SAMPLE_RATE = 16000       # whisperx works at 16,000 samples/sec
MIN_GAP = 10.0            # seconds with no words that get a second listen (same as spot.py)
GAP_PAD = 2.0             # extra seconds heard on each side of a gap

# Keep the AI models inside this project, in models/ (ignored by git).
os.environ.setdefault("HF_HOME", str(MODELS_DIR))
os.environ.setdefault("TORCH_HOME", str(MODELS_DIR / "torch"))
# If the Whisper model is already here, don't touch the internet at all.
# (Windows has a download bug that can hang forever.)
if (MODELS_DIR / "hub" / f"models--Systran--faster-whisper-{MODEL_NAME}").exists():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

# Hide harmless startup noise (torchcodec, symlinks, TF32).
warnings.filterwarnings("ignore")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import torch     # noqa: E402  (must come after the settings above)
import whisperx  # noqa: E402

logging.getLogger("whisperx").setLevel(logging.WARNING)

_models = {}  # loaded once, reused for every episode in the same run


def load_models():
    if not _models:
        if not torch.cuda.is_available():
            sys.exit("No CUDA GPU found. Run check_gpu.py to see what's wrong.")
        print(f"Loading Whisper {MODEL_NAME} on {torch.cuda.get_device_name(0)}...")
        start = time.time()
        _models["whisper"] = whisperx.load_model(
            MODEL_NAME, device="cuda", compute_type="float16", language=LANGUAGE)
        _models["align"], _models["align_meta"] = whisperx.load_align_model(
            language_code=LANGUAGE, device="cuda")
        print(f"  models ready in {time.time() - start:.1f}s")
    return _models


def timecode(seconds):
    s = int(seconds)
    return f"[{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}]"


def outputs_for(audio_path):
    """episodes/raw/<show>/<name>.mp3 -> episodes/transcripts/<show>/<name>.json and .txt"""
    folder = TRANSCRIPTS_DIR / audio_path.parent.name
    return folder / f"{audio_path.stem}.json", folder / f"{audio_path.stem}.txt"


def nice_path(path):
    """Show a path relative to the project if it's inside it, otherwise in full.
    (Files from elsewhere, like Downloads, used to crash here after all the GPU work.)"""
    try:
        return str(Path(path).relative_to(PROJECT))
    except ValueError:
        return str(path)


def write_safely(path, text):
    """Write to a .part file, then rename, so a crash never leaves a half-written transcript."""
    part = path.with_name(path.name + ".part")
    part.write_text(text, encoding="utf-8")
    os.replace(part, path)


def find_gaps(words, duration):
    """Stretches of MIN_GAP seconds or more with no words, including the very start and end."""
    gaps, previous_end = [], 0.0
    for w in words:
        if w["start"] is None:
            continue
        if w["start"] - previous_end >= MIN_GAP:
            gaps.append((previous_end, w["start"]))
        previous_end = max(previous_end, w["end"])
    if duration - previous_end >= MIN_GAP:
        gaps.append((previous_end, duration))
    return gaps


def listen_again(audio, gap_start, gap_end, models):
    """Transcribe just one gap (plus a little padding) and return the words found inside it,
    on the episode's clock.
    WhisperX's speech detector skips some stretches entirely, often stitched-in ads with
    music under them. So this second listen goes straight to Whisper with the detector off."""
    s0 = max(0.0, gap_start - GAP_PAD)
    s1 = min(len(audio) / SAMPLE_RATE, gap_end + GAP_PAD)
    piece = audio[int(s0 * SAMPLE_RATE):int(s1 * SAMPLE_RATE)]

    segments, _ = models["whisper"].model.transcribe(
        piece, language=LANGUAGE, vad_filter=False, beam_size=5,
        condition_on_previous_text=False)
    segments = [{"start": s.start, "end": s.end, "text": s.text.strip()}
                for s in segments if s.text.strip()]
    if not segments:
        return []

    aligned = whisperx.align(segments, models["align"], models["align_meta"], piece, device="cuda")
    found = []
    for seg in aligned["segments"]:
        for w in seg.get("words", []):
            if "start" not in w or "end" not in w:
                continue
            start, end = w["start"] + s0, w["end"] + s0
            # Only words that sit inside the gap. The padding re-hears words we already
            # have on either side; those are dropped here, so nothing is doubled.
            if gap_start < (start + end) / 2 < gap_end:
                found.append({"word": w["word"], "start": round(start, 3), "end": round(end, 3)})
    return found


def as_sentences(words):
    """Group words back into sentence lines, breaking after . ? !"""
    sentences, current = [], []
    for w in words:
        current.append(w)
        if w["word"].rstrip().endswith((".", "?", "!")):
            sentences.append(current)
            current = []
    if current:
        sentences.append(current)
    return [{"start": s[0]["start"], "end": s[-1]["end"],
             "text": " ".join(w["word"].strip() for w in s), "repaired": True}
            for s in sentences]


def repair_gaps(data, audio, models):
    """Give every long wordless gap a second listen and fill in what was missed.
    Changes data in place and returns a list of (start, end, words added, first few words)."""
    report = []
    for gap_start, gap_end in find_gaps(data["words"], data["duration"]):
        found = listen_again(audio, gap_start, gap_end, models)
        report.append((gap_start, gap_end, len(found), " ".join(w["word"] for w in found[:12])))
        if not found:
            continue
        # Words: slot the new ones in just before the first timed word after the gap.
        words = data["words"]
        at = next((i for i, w in enumerate(words) if w["start"] is not None and w["start"] >= gap_end),
                  len(words))
        data["words"] = words[:at] + found + words[at:]
        data["segments"] = sorted(data["segments"] + as_sentences(found), key=lambda s: s["start"])
    data["gap_repair"] = {
        "done": datetime.now().isoformat(timespec="seconds"),
        "min_gap": MIN_GAP,
        "gaps": [{"start": round(a, 3), "end": round(b, 3), "words_added": n} for a, b, n, _ in report],
    }
    return report


def print_repair(report):
    filled = [r for r in report if r[2]]
    print(f"  gap repair: {len(report)} gaps of {MIN_GAP:.0f}s+, {len(filled)} had missed words")
    for a, b, n, preview in report:
        what = f"{n} words: {preview}..." if n else "nothing (music or silence)"
        print(f"    {timecode(a)[:-1]}-{timecode(b)[1:]}  {what}")


def save(json_path, txt_path, data):
    write_safely(json_path, json.dumps(data, indent=2, ensure_ascii=False))
    write_safely(txt_path, "".join(f"{timecode(s['start'])} {s['text']}\n" for s in data["segments"]))


def repair_file(json_path, force=False):
    """Run gap repair on a transcript that already exists, without redoing the episode.
    Returns the report, or None if it was skipped."""
    json_path = Path(json_path).resolve()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if "gap_repair" in data and not force:
        print(f"already gap-repaired: {json_path.stem}")
        return None
    audio_path = Path(data["audio"])
    if not audio_path.is_absolute():
        audio_path = PROJECT / audio_path
    if not audio_path.exists():
        print(f"can't repair {json_path.stem}: audio not found ({nice_path(audio_path)})")
        return None

    models = load_models()
    print(f"\nGap repair: {json_path.stem}")
    audio = whisperx.load_audio(str(audio_path))
    report = repair_gaps(data, audio, models)
    save(json_path, json_path.with_suffix(".txt"), data)
    print_repair(report)
    return report


def all_transcripts():
    return sorted(p for p in TRANSCRIPTS_DIR.rglob("*.json") if not p.name.endswith(".cuts.json"))


def transcribe_file(audio_path):
    """Transcribe one file. Returns seconds taken, or None if it was skipped."""
    audio_path = Path(audio_path).resolve()
    json_path, txt_path = outputs_for(audio_path)
    if json_path.exists() and txt_path.exists():
        print(f"already transcribed: {audio_path.name}")
        return None

    models = load_models()
    print(f"\nTranscribing: {audio_path.name}")
    start = time.time()

    audio = whisperx.load_audio(str(audio_path))  # decoded by the ffmpeg program
    duration = len(audio) / 16000                 # whisperx works at 16,000 samples/sec

    # 1. Speech to text (rough sentence timing).
    result = models["whisper"].transcribe(audio, batch_size=16, language=LANGUAGE)
    t_text = time.time() - start

    # 2. Line each word up with the audio (precise word timing).
    aligned = whisperx.align(
        result["segments"], models["align"], models["align_meta"], audio, device="cuda")
    elapsed = time.time() - start

    segments, words = [], []
    for seg in aligned["segments"]:
        segments.append({
            "start": round(seg["start"], 3),
            "end": round(seg["end"], 3),
            "text": seg["text"].strip(),
        })
        for w in seg.get("words", []):
            # A few words (often numbers) can come back without a time. Keep them, marked None.
            words.append({
                "word": w["word"],
                "start": round(w["start"], 3) if "start" in w else None,
                "end": round(w["end"], 3) if "end" in w else None,
            })

    data = {
        "audio": nice_path(audio_path),
        "duration": round(duration, 3),
        "model": MODEL_NAME,
        "created": datetime.now().isoformat(timespec="seconds"),
        "segments": segments,
        "words": words,
    }
    # 3. Second listen on any long gap, to catch speech the first pass skipped.
    report = repair_gaps(data, audio, models)
    elapsed = time.time() - start

    json_path.parent.mkdir(parents=True, exist_ok=True)
    save(json_path, txt_path, data)
    segments, words = data["segments"], data["words"]

    missing = sum(1 for w in words if w["start"] is None)
    print(f"  {duration / 60:.1f} min of audio in {elapsed:.0f}s "
          f"({duration / elapsed:.0f}x real time; text {t_text:.0f}s, word timing {elapsed - t_text:.0f}s)")
    print(f"  {len(segments)} sentences, {len(words)} words, {missing} without a time")
    print_repair(report)
    print(f"  saved: {nice_path(txt_path)}")
    print(f"  saved: {nice_path(json_path)}")
    return elapsed


def main():
    args = sys.argv[1:]
    if args and args[0] == "--repair":
        # Gap repair on transcripts that already exist. No files named = all of them.
        # Add --force to repair again one that's already been done.
        force = "--force" in args
        paths = [Path(a) for a in args[1:] if a != "--force"] or all_transcripts()
        for path in paths:
            repair_file(path, force=force)
        return
    if not args:
        sys.exit("Usage: .venv\\Scripts\\python transcribe.py <audio file> [more files...]\n"
                 "       .venv\\Scripts\\python transcribe.py --repair [transcript.json ...] [--force]")
    for arg in args:
        path = Path(arg)
        if not path.exists():
            print(f"not found: {arg}")
            continue
        transcribe_file(path)


if __name__ == "__main__":
    main()
