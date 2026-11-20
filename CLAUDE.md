# Cutting Room

A personal tool that cleans the ads out of podcasts.

It downloads new podcast episodes, transcribes them on this computer, finds the ads, cuts them out, and delivers clean episodes to my phone. It runs locally on my own machine.

**For my own listening only.** Nothing this tool makes is shared, uploaded, or re-published.

## How it's built

Five stations, each a separate small script. If one breaks, the others don't care.

1. **Dailies.** Download the newest episode from each show's feed.
2. **Logging.** Transcribe it on the GPU, with a timestamp on every word.
3. **Spotting.** Read the transcript and mark the ads to cut.
4. **The edit.** Cut them with ffmpeg.
5. **Delivery.** Get the clean file to my phone.

## Cut list format (decided Module 2.1)

`<episode>.cuts.json` = `{"episode": "<base name>", "cuts": [ ... ]}`. Each cut:

```json
{"id": 1, "start": 98.31, "end": 161.02, "type": "sponsor", "confidence": "high",
 "reason": "Host-read GhostBed ad with promo code BTB",
 "first_words": "This is an iHeart Podcast.", "last_words": "Promo code BTB."}
```

- `start`/`end` are seconds, on the same clock as the word-level transcript JSON.
- `type` is one of `sponsor`, `promo` (another show), `plug` (the hosts' own Patreon/merch), `theme`.
- `confidence` is `high`, `medium` or `low`. **Only `high` ever gets cut. When in doubt, leave it in.**
- `id` runs 1, 2, 3… in time order. Back-to-back ads are separate entries.

## Shows and their settings (Module 4.2)

- `shows.local.yaml` (git-ignored): each show's name, feed, and options (`enabled`, `keep`, `skip_titles`), with a `defaults:` section. `shows.example.yaml` shows every option. `shows.py` reads it for every station.
- Spotter notes for one show live in `show-notes/<show-folder>.md` (committed, no secrets). `spotter-rules.md` holds only the general rules. The Spotter is sent the general rules plus that one show's notes, never another show's.

## House rules

- **Audio files and feed URLs must never be committed to git.** Audio lives in `episodes/`, which git ignores. Feed URLs live in `shows.local.yaml`, which git ignores. Some feed URLs contain a private key, so treat them like passwords.
- Check `git status` before every commit. If an audio file or a `.local` file shows up, stop and fix `.gitignore` first.
- Nothing counts as working until it's been heard. A transcript isn't proven until a timestamp in it matches the audio.
- The owner doesn't write code. Explain changes in plain language: what it does and why.
- This runs on Windows. Use PowerShell for terminal commands.
