# Cutting Room

A personal tool that takes the ads out of podcasts.

Every morning it downloads new episodes of your shows, transcribes them on your own
computer, finds the ads, cuts them out, and sends clean episodes to your phone. You open
your podcast app and the new episodes are there, with no ads.

**For your own listening only.** Clean episodes go to your own devices and nowhere else.
Never share them, post them, or put them anywhere public. That would be re-publishing
someone else's show without the ads that pay for it.

This runs on Windows, with an NVIDIA graphics card, Python, ffmpeg, Claude Code, and
Syncthing. Commands below are for PowerShell, typed from inside the project folder.

---

## How it works: six stations

Think of it as a line in an edit bay. Each station is its own small script. If one
breaks, the others carry on with whatever is there.

| # | Station | Script | What it does | What it leaves behind |
|---|---|---|---|---|
| 1 | **Dailies** | `fetch.py` | Checks each show's feed and downloads the newest episode(s). | `episodes/raw/<show>/` |
| 2 | **Logging** | `transcribe.py` | Transcribes the episode on the graphics card, with a time on every word. | `episodes/transcripts/<show>/<name>.json` and `.txt` |
| 3 | **Spotting** | `spot.py` | Hands the transcript to Claude, which marks the ads. Claude is locked out of all tools: it can only read and answer. | `<name>.cuts.json` (the cut list) and `.cuts.txt` |
| 4 | **The Edit** | `edit.py` | Cuts the ads out with ffmpeg, with a short crossfade at each seam. Checks the length adds up. | `episodes/clean/<show>/<name>.mp3` and `<name>.kept.txt` |
| 5 | **Delivery** | `deliver.py` | Asks Syncthing whether the phone has every clean episode. | `episodes/delivered.json` |
| 6 | **Cleanup** | `cleanup.py` | Deletes files the line has finished with, so the disk never fills. | `episodes/retired.json` |

`run.py` runs all six in order and writes the morning sheet. `run.ps1` is the small file
Windows Task Scheduler starts; it finds the tools and runs `run.py`.

Things worth knowing:
- **Only sure things get cut.** The Spotter marks each ad high, medium or low
  confidence. Only high gets cut. Medium and low stay in, and are listed in
  `<name>.kept.txt` with their time in the clean episode. When in doubt, it leaves it in.
- **The original download is never changed.** The Edit only reads it. A bad cut can
  always be re-done from the original, until Cleanup deletes the original.
- **Every station skips work already done.** Running the line twice in a row is safe;
  the second run does nothing new.
- **Getting to the phone:** Syncthing copies `episodes/clean` to the phone (Send Only on
  the computer, Receive Only on the phone). AntennaPod has each show's folder added as a
  local folder, so each show appears as a podcast. The publish date comes along with
  the files, and so does the show art (`folder.jpg`) if the episode has art built in.

## The daily schedule

Task Scheduler runs the task named **Cutting Room**:
- every day at **7:30 AM**, and
- **when you log on** (catches up if the computer was off at 7:30).

A run with nothing new takes under a minute. A new hour-long episode takes several
minutes, mostly transcribing. Only one run happens at a time; a second one that finds the
line busy leaves quietly. No new work starts after 2.5 hours, and a run is stopped at
2 hours 50, before Task Scheduler's 3-hour limit.

Run it yourself any time:

```powershell
.venv\Scripts\python run.py
```

Or start the scheduled task: Task Scheduler > Cutting Room > Run.

## Where the settings live

| File | In git? | What's in it |
|---|---|---|
| `shows.local.yaml` | **No, private** | Your shows and their feeds, delivery and cleanup settings. |
| `shows.example.yaml` | Yes | A copy with fake shows, showing every option. Start here. |
| `edit-settings.yaml` | Yes | How cuts sound (the house style). |
| `spotter-rules.md` | Yes | The general rules the Spotter follows for every show. |
| `show-notes/<show-folder>.md` | Yes | Extra notes for one show's Spotter. Optional. |

### shows.local.yaml

```yaml
delivery: home        # home, cloud, both or off

cleanup:
  keep_clean: 3       # newest clean episodes kept per show
  clean_min_days: 7   # never delete a clean episode younger than this
  raw_days: 7         # delete an original this long after the phone got the clean one
  text_days: 30       # delete transcripts this long after publish (once finished)
  test_days: 7        # delete files in episodes/test older than this

defaults:
  enabled: true       # used by every show unless the show sets its own
  keep: 1

shows:
  - name: Some Show
    feed: <the show's feed link>
    enabled: true     # false = paused: nothing new is downloaded
    keep: 1           # how many of the newest episodes to process (1 to 10)
    skip_titles:      # pass over episodes whose title contains any of these
      - Live Q&A
```

- **delivery:** `home` is Syncthing. `cloud` isn't built in this project; leave it on
  `home`. `off` means don't check the phone.
- **cleanup:** see Cleanup below. A missing or bad value falls back to the default
  shown, and the morning sheet warns you.
- A typo in this file (a misspelled setting, a missing feed) shows up on the morning
  sheet. The line uses what it can and carries on.

**Feed links are passwords.** Some have a private key in them. They live only in
`shows.local.yaml`, which git ignores. Never paste one anywhere else.

### edit-settings.yaml

| Setting | Default | Meaning |
|---|---|---|
| `cut_confidence` | `[high]` | Which marks get cut. Leave it at high. |
| `crossfade` | `0.25` | Seconds of crossfade at each seam. Speech sounds natural between 0.1 and 0.3. |
| `padding` | `0.15` | Seconds cut wider than the ad on each side, never into a show word. |
| `merge_gap` | `5.0` | Cuts closer than this, with no words between, become one cut. |
| `format`, `bitrate`, `sample_rate`, `channels` | `mp3`, `match`... | The output file. `match` copies the original. |

Change a number and the next edit uses it. To hear the change on an existing episode:
`.venv\Scripts\python edit.py <transcript.json> --redo`, then
`.venv\Scripts\python edit.py <transcript.json> --seams` for a short reel of every seam.

### spotter-rules.md and show-notes

`spotter-rules.md` is the Spotter's rulebook: what counts as an ad, what to keep, and
when to mark something high. If the Spotter keeps making the same mistake, fix the
rule here, not one episode.

A show with its own habits (how its breaks start, sponsor reads done as jokes) gets a
notes file in `show-notes/`. Name it exactly like the show's folder in
`episodes\clean\`: "Some Show Name" becomes `show-notes/some-show-name.md`. Punctuation
and `&` are dropped, so copy the folder name rather than guessing. Only that show's notes are
sent when spotting that show. `show-notes/example-show.md` shows the layout. No feed
links or anything private in these files: they're committed to git.

## Add or remove a show

**Add a show:**
1. Find the show's **RSS feed link** (the show's website, or ask Claude to find it). It
   must be the feed itself, not an Apple Podcasts or Spotify page. Those look right but
   don't work.
2. Add it at the end of the `shows:` list in `shows.local.yaml`. Copy this shape exactly:
   two spaces, a dash, a space; and the `feed:` line lined up under `name:`.
   ```yaml
   shows:
     - name: Some Show
       feed: <the show's feed link>
   ```
3. Check the link: run `.venv\Scripts\python fetch.py`. Under the show's name, `newest:`
   then `saved:` means it worked (it downloads the episode). `ERROR: the feed has no
   episodes in it` on a new show means **the link is wrong**, not a bad morning. Go back
   to step 1.
4. Run `.venv\Scripts\python run.py` to finish the episode, or wait for the morning.
5. On the computer, look in `episodes\clean\`. The new folder there is the show's folder
   name (lowercase, dashes, punctuation dropped). It appears only once the first clean
   episode is made. Optional: if the show has odd ad habits, write
   `show-notes/<that exact folder name>.md`.
6. On the phone, the same folder arrives inside the folder Syncthing-Fork syncs into
   (the one you picked when you accepted Clean Episodes on the phone). In AntennaPod:
   Add podcast > Add local folder, and pick **the show's own folder inside it**, not
   the Clean Episodes folder itself. Then in that podcast's settings, set
   **New episodes action** to **Add to inbox** so new ones show up in your Inbox.
7. Listen to the first clean episode all the way through. Check the seams.

**Never change a show's `name:`** once it's running. The folder name comes from it, so a
new name is a new show: new folder, episodes downloaded again, a new podcast on the
phone, and its notes file no longer matches.

**Pause a show:** set `enabled: false`. Nothing new downloads; what's already
downloaded still finishes the line.

**Remove a show:** delete its entry from `shows.local.yaml`. Its old files stay until
you delete them. Remove the podcast in AntennaPod too.

## Reading the morning sheet

Open `logs\morning.txt`. It takes ten seconds:

```
MORNING SHEET  Fri Nov 20, last run finished 07:41 (all good)

NEW CLEAN EPISODES (2, 12.7 min of ads gone)
  <Show>: <Episode title>
     5.6 min saved, now 13:08 long

DELIVERY home: <phone>: has all 11 clean episodes (connected now)

CLEANUP 3 file(s) deleted, 412.0 MB freed

PROBLEMS: none
```

- **The first line** says when the last run finished and whether it was all good.
  If it says **a run STARTED at ... and hasn't finished**, a run is going right now, or
  one died.
- **NEW CLEAN EPISODES** lists the last 24 hours, with minutes saved.
- **DELIVERY** says whether the phone has everything. "On the way" is normal for an
  episode made in the last day.
- **CLEANUP** says what was deleted.
- **PROBLEMS (nothing to do yet)** means the line hit something and expects it to sort
  itself out, like a feed that was down. Each problem has a `->` line with advice.
- **NEEDS YOU** means do something. It happens when a problem needs a person, or when
  the same problem has come back 3 days running.

The full log of every run is in `logs\` (the newest file, named by date and time). The
sheet's last line names it. Logs older than 30 days are dropped on their own.

## The five most likely things to break

### 1. No new episodes: the run didn't happen
The sheet's date is old. Usually the computer was asleep or off at 7:30. The logon run
catches up the next time you log in. Check Task Scheduler > Cutting Room > Last Run Result:
`0x0` is good, `0x1` means look at the sheet. If it says the line is **stuck**, restart
the computer; the next run starts normally.

### 2. A show stopped getting episodes: the feed moved or broke
The sheet names the show and says its server turned the line away, or the feed came
back empty. One bad morning is normal; the next run tries again. Act when:
- it moves to **NEEDS YOU** (the same problem 3 days running), or
- it says the feed has **no audio** in it. Act on that one straight away, or
- a show has had **nothing new for longer than usual**. The sheet can't catch this
  one: a feed that stops updating looks the same as a show on a break. Once a month,
  look at the newest dates in `episodes\clean\` and compare with the show's website.

The fix: open the show's website and find its current feed link (shows move hosts).
In `shows.local.yaml`, **change only the `feed:` line**, never `name:`. Then run
`.venv\Scripts\python fetch.py`: `newest:` then `saved:` or `already have it` means it
works; `ERROR:` means the link is still wrong. Run `.venv\Scripts\python run.py` after,
so the morning sheet clears. One broken feed never stops the other shows.

### 3. Episodes downloaded but nothing got cut: Claude is logged out
The sheet says Claude **isn't logged in**. Spotting is skipped until you fix it; the next
run catches up. Open PowerShell and run:
```powershell
claude auth login
```

### 4. The phone isn't getting episodes
**How you'll know:** the DELIVERY line on the morning sheet. "On the way" for less than
a day is normal. Over a day, it becomes a problem: the phone **hasn't received** an
episode, or Syncthing **isn't running on this computer**.

There is one Syncthing folder, **Clean Episodes**, holding every show. Work down this list:

**On the computer**, open `http://127.0.0.1:8384` in a browser.
- [ ] The page opens. (If not, Syncthing isn't running: Start menu > Start Syncthing.)
- [ ] Clean Episodes says **Up to Date**, not Paused. (Paused: press Resume.)
- [ ] Clean Episodes > Edit > Advanced: Folder Type is **Send Only**.
- [ ] Clean Episodes > Edit > Sharing: the phone is ticked.
- [ ] Under Remote Devices, the phone says **Connected** (or connected recently).

**On the phone**, open Syncthing-Fork.
- [ ] It's running (not stopped in its own menu).
- [ ] Settings > Run conditions: it's allowed on the networks you use (Wi-Fi and mobile data).
- [ ] Android battery settings: battery saver isn't putting Syncthing-Fork to sleep.
- [ ] The Clean Episodes folder is there and Receive Only.

Once both sides connect, it catches up on its own. No need to re-run the line.

### 5. An ad got through, or a cut sounds wrong
Look in the episode's `.kept.txt` (in `episodes/clean/<show>/` on the computer): the ad
may have been marked medium and left in on purpose. If the Spotter got it wrong, add a
line to `spotter-rules.md` (or the show's notes) about the habit behind it, then re-do
that episode while its original is still on the computer (7 days after delivery):
```powershell
.venv\Scripts\python spot.py episodes\transcripts\<show>\<name>.json --redo
.venv\Scripts\python edit.py episodes\transcripts\<show>\<name>.json --redo
```
If the seam itself sounds rough (clipped breath, jump), change `crossfade` or `padding` in
`edit-settings.yaml` instead, and re-do the edit.

### Smaller things
- **Transcribing is very slow** (the log shows 1x speed instead of about 5x). The graphics
  card is full. Close games, launchers and animated-wallpaper apps; Whisper needs about
  5 GB of video memory.
- **ffmpeg or claude wasn't found** after an update. Reinstall it
  (`winget install ffmpeg`), then restart the computer.
- **A file couldn't be deleted.** A player or Syncthing had it open. The next run tries
  again.

## Cleanup and deleting

Deleting can't be undone, so Cleanup has guards:
- An original is deleted only after the phone has had its clean version for `raw_days`.
  An episode that failed somewhere keeps its original.
- Older clean episodes beyond `keep_clean` per show are deleted. **Syncthing then
  removes them from the phone too.**
- It only ever touches `episodes/raw`, `clean`, `transcripts` and `test`, and never
  `folder.jpg`, `.stignore` or the record files.
- Finished episodes are written to `episodes/retired.json`, so they're never downloaded
  or edited again.

See what it would delete, without deleting anything:
```powershell
.venv\Scripts\python cleanup.py                       # as of now
.venv\Scripts\python cleanup.py --as-of 2026-12-31    # as of a future date
```
Always do this after changing a cleanup setting. The morning run deletes for real.

## The cloud link

This project uses the home road (Syncthing) only. There is no cloud feed and no cloud
link to rotate. If you ever build one: keep its link secret, keep only recent episodes
on it, and if the link ever leaks, make a new random address, move the files, delete
the old ones, and re-subscribe on the phone.

## Keys and secrets

- Feed links live only in `shows.local.yaml` (git ignores it).
- Syncthing's key stays in Syncthing's own settings file. `deliver.py` reads it each run,
  sends it only to this computer (127.0.0.1), and never prints it.
- Audio, logs and `episodes/` never go in git. Check `git status` before every commit.
- If a key is ever pasted into a chat or a file, treat it as burned: make a new one.

## Turning it off

- **Pause one show:** `enabled: false` in `shows.local.yaml`.
- **Pause the whole line:** Task Scheduler > Cutting Room > right-click > Disable.
  Enable it again to resume. Nothing is lost.
- **Stop sending to the phone:** in Syncthing (`http://127.0.0.1:8384`), pause the Clean
  Episodes folder.
- **Turn it off for good:**
  1. Delete the Cutting Room task in Task Scheduler.
  2. In Syncthing, remove the Clean Episodes folder (this does not delete the files).
     Uninstall Syncthing if nothing else uses it.
  3. On the phone, remove the podcasts from AntennaPod and the folder from Syncthing-Fork.
  4. Delete the `episodes` folder to free the space. That's the only folder that's big.

## Setting it up on a new computer

1. Install Python, ffmpeg (`winget install ffmpeg`), Claude Code (then
   `claude auth login`), and an NVIDIA driver.
2. Copy this project. Make the Python environment:
   ```powershell
   py -3.10 -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   .venv\Scripts\python check_gpu.py
   ```
   The last line must say it's using CUDA (the graphics card). If it doesn't, Whisper
   would run on the processor and take hours.
3. Copy `shows.example.yaml` to `shows.local.yaml` and put your shows in it.
4. Run `.venv\Scripts\python run.py` once by hand. The first run downloads the Whisper
   model (a few GB, into `models\`).
5. Install Syncthing and set up the folder (Send Only here, Receive Only on the phone).
6. In Task Scheduler, make a task that runs
   `powershell -NoProfile -ExecutionPolicy Bypass -File "<project>\run.ps1"` daily at
   7:30 and at logon, with a 3-hour limit.

## License

MIT. See LICENSE. The license covers this code only, not anyone's podcasts: clean episodes are for your own listening and must never be shared or re-published.
