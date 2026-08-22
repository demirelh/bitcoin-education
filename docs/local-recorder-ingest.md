# Local recorder ingest

The `tagesschau_tr` profile has two possible sources for the same broadcast:

| Source | Ready at | Role |
| --- | --- | --- |
| Local `ard-recorder` capture | ~20:21 | preferred |
| YouTube upload (RSS feed) | ~21:30–22:30 | fallback |

Reading the local file removes one to two hours of waiting from every evening's
run. That is the entire reason this source exists — the pipeline itself is
unchanged after the `download` stage.

## The recorder's output contract

A separate service (`ard-recorder`, see `/home/pi/projects/ard-recorder`) records
the ARD live stream every evening and publishes:

```
/mnt/photo-backup/tagesschau/recordings/<YYYY-MM-DD>/
    tagesschau_<YYYY-MM-DD>_2000.mp4            the broadcast, already trimmed
    tagesschau_<YYYY-MM-DD>_2000.metadata.json  title, scheduled_start, duration
    tagesschau_<YYYY-MM-DD>_2000.DONE           commit marker, written last
    tagesschau_<YYYY-MM-DD>_2000.raw.mp4        untrimmed safety copy
```

Two rules follow from that contract and are enforced in
`services/local_recorder_service.py`:

* **Never read a recording without its `.DONE` marker.** The marker is the
  recorder's commit point. A video without it is either still being written or
  failed verification, and transcribing it would produce a truncated broadcast
  that looks complete.
* **Never read `*.raw.mp4`.** It is the untrimmed capture, kept as a safety net.
  It brackets the broadcast with the end of the previous programme and the start
  of the next, so a loose `*.mp4` glob would silently ingest foreign content.

The published `.mp4` is already cut to the programme boundaries by the recorder,
so nothing here trims it again. New recorder outputs also carry
`extra.completion_verified=true`: this means the opening announcement, closing
content and weather/sign-off checks agreed using the live capture itself. btcedu
rejects a `.DONE` marker without that verdict and continues to the feed fallback;
a technically playable but semantically truncated MP4 is not a valid local source.

## Configuration

```yaml
# btcedu/profiles/tagesschau_tr.yaml
ingest:
  local_recorder:
    enabled: true
    base_dir: /mnt/photo-backup/tagesschau/recordings
    supersedes_feed: true
```

Omitting the section — the default for every other profile — disables the local
source entirely and leaves the feed path untouched.

## Flow

`run_latest` (the ten-minute `btcedu-run.timer`) checks the recorder first, then
the feed:

1. `detect_local_recordings()` scans for finished recordings and inserts them as
   episodes with `source="local_recorder"` and a filesystem path in `url`.
2. `detect_episodes()` / `detect_all_active_channels()` run afterwards, exactly
   as before.
3. The newest pending episode is processed.

Local detection is wrapped in a `try`/`except`: an unreadable or unmounted
recorder directory logs a warning and falls through to the feed. **The fallback
must always be reachable**, which is why no failure here can abort the run.

### Download stage

For `source == "local_recorder"` the file is already on disk, so
`download_episode()` does not call yt-dlp (which would reject a filesystem path
anyway). Instead `_ingest_local_recording()`:

* extracts the audio track with ffmpeg — a stream copy for `m4a`, since the
  recorder already stores AAC, so it costs ~2 s and loses no quality;
* hard-links the video to `video.mp4` (falling back to a symlink across
  filesystems) rather than copying ~350 MB;
* writes `video_meta.json` so `frameextract` finds the video as usual.

Everything downstream is identical for both sources.

## Deduplication (both directions)

The same broadcast can arrive twice: from disk at 20:21 and from YouTube one to
two hours later. A duplicate is expensive — it repeats transcription,
translation, TTS, image generation and rendering — so both directions are
suppressed:

* **Feed → suppressed by local**: `detect_episodes()` drops feed entries whose
  broadcast day already exists as a local episode.
* **Local → suppressed by feed**: `detect_local_recordings()` drops recordings
  whose broadcast day already exists as a feed episode. This direction matters
  more than it looks: every broadcast from before the recorder existed, and
  every evening the recorder misses, is in the database as a YouTube episode.
  Without this filter the local file would arrive afterwards and re-run finished
  work.

Matching keys on the date **in the title** (`tagesschau 20:00 Uhr, 06.08.2026`),
not on `published_at`: an upload past midnight carries the next day's timestamp
and would not match the recording it duplicates.

## Retention interaction

`scan_recordings()` honours the profile's retention cutoff. This is what stops a
loop: retention deletes episodes after `ingest.retention_days` (10), while the
recorder keeps its files for 21 days. Without the cutoff, a recording between 10
and 21 days old would be deleted by retention and immediately re-ingested on the
next timer tick, forever.
