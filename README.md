# TubeScribe

A local personal YouTube transcript archive. This Mac owns channel discovery, the SQLite queue, downloaded audio, and durable transcripts. Beast-3 runs Whisper ASR and Nemotron speaker diarization on its RTX 4090 over loopback SSH tunnels. A local dashboard lists channels and videos and previews saved transcripts; the CLI manages ingestion. No paid transcript provider is needed.

## Install

Requires `uv`, Node.js **22 or newer**, `ffmpeg`/`ffprobe`, and OpenSSH locally. The locked `yt-dlp[default]` dependency includes its matching EJS challenge solver. [yt-dlp's runtime setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS) explains these requirements.

```bash
cd /Users/tsilva/repos/tsilva/tubescribe
uv --config-file uv.toml sync --locked
scripts/tubescribe --help
```

The wrapper runs `uv --config-file uv.toml run --locked tubescribe`, ensuring machine-specific uv configuration cannot change resolution. Both `pyproject.toml` and the explicit `uv.toml` preserve a **seven-day package age cutoff** and the existing bad-package exclusions. `uv.lock` records exact dependency versions and hashes. Refresh deliberately with `uv --config-file uv.toml lock --upgrade`; do not bypass the cutoff to fix download errors.

## SSH and remote ASR

The working host is `beast3-ssh.tsilva.eu`, reached through the existing authenticated Cloudflare SSH proxy. `beast-3.local` was unreachable from this Mac during setup. The local alias is `tubescribe-beast`.

`~/.ssh/config` includes `~/.ssh/tubescribe.conf` before the existing configuration. The addition preserves the global **Bitwarden SSH agent**, the existing public identity, strict host-key checks, and the proxy. It adds `ControlMaster auto`, `ControlPersist 8h`, a private control directory, a 15-second connection timeout, and 30-second keepalives with three missed replies allowed. A timestamped backup of the original configuration remains in `~/.ssh/`. No private key is read or copied. Keychain fallback remains disabled as required by the existing SSH policy.

For a fresh machine, create this host block (with the same configured Cloudflare/Bitwarden prerequisites):

```sshconfig
Host tubescribe-beast beast3-ssh.tsilva.eu
  HostName beast3-ssh.tsilva.eu
  User tsilva
  ProxyCommand /opt/homebrew/bin/cloudflared access ssh --hostname %h
  HostKeyAlias beast3-ssh.tsilva.eu
  StrictHostKeyChecking yes
  ControlMaster auto
  ControlPath ~/.ssh/control/%C
  ControlPersist 8h
  ServerAliveInterval 30
  ServerAliveCountMax 3
  ConnectTimeout 15
```

Keep `~/.ssh/control` private (`chmod 700`). Unlock Bitwarden and approve its SSH signing request when opening a new connection. Cloudflare Access may also require browser login when its cached authorization expires. Subsequent SSH channels reuse the authorized connection; a disconnected connection requires fresh authorization according to Bitwarden's settings.

```bash
scripts/remote-service.sh install
scripts/remote-service.sh start
scripts/tubescribe tunnel start
scripts/tubescribe health
```

Installation transfers only source and lock/config files into an isolated uv environment at `/home/tsilva/.local/share/tubescribe/service`. It installs a user systemd unit, `tubescribe-asr`, with one process and one loaded model. PyAV is constrained below 17 (locked at 16.1.0), since PyAV 19 removed a decoder argument used by faster-whisper 1.2.1. `start` checks that the remote port is available and leaves an already running service alone. Beast-3 has user lingering enabled, so the service survives SSH disconnects and starts at login/boot. Installation updates files but does not restart a running service; stop it first when deploying changed service code.

Remote ASR binds **127.0.0.1:18765**, forwarded to **http://127.0.0.1:18766** locally. Both ports were checked before use. The tunnel owns a dedicated SSH master at `data/main-ssh.sock`, detached from the starting terminal; stopping it does not stop other SSH masters. `ExitOnForwardFailure` rejects bind failures. Its state lives in `data/tunnel.json` and errors in `data/tunnel.log`. Starting an existing healthy tunnel reuses it. Health failures are reported, including a service still loading its model.

```bash
scripts/tubescribe tunnel status
scripts/tubescribe tunnel stop
scripts/tubescribe tunnel start                    # reconnect after a lost connection
scripts/remote-service.sh status
scripts/remote-service.sh logs
scripts/remote-service.sh stop
scripts/remote-service.sh start
```

`remote-service.sh` reuses the dedicated tunnel connection when present. Remote logs are available through `journalctl --user -u tubescribe-asr`. To free GPU memory while idle, stop the ASR service. Other models and services are unaffected. To prevent the service starting at boot, use `ssh tubescribe-beast 'systemctl --user disable tubescribe-asr'`.

If a signing prompt cannot be approved before a tunnel command times out, authorize a persistent master interactively in a separate terminal, then start the forward:

```bash
ssh -M -S "$PWD/data/main-ssh.sock" -o ControlPersist=no -NT tubescribe-beast
# Leave this terminal connected; in another terminal:
scripts/tubescribe tunnel start
```

Alternative ports are explicit, never silently substituted over an occupied port:

```bash
TUBESCRIBE_REMOTE_PORT=18769 scripts/remote-service.sh install
TUBESCRIBE_REMOTE_PORT=18769 scripts/remote-service.sh start
scripts/tubescribe tunnel start --remote-port 18769 --local-port 0
```

`--local-port 0` selects an available local port and prints/persists the resulting endpoint. Stop the existing service/tunnel before changing ports. The CLI reads `tunnel.json` automatically, or accepts `--endpoint http://127.0.0.1:PORT` / `TUBESCRIBE_ENDPOINT`.

### Model and API

Verified hardware: **NVIDIA RTX 4090, 24 GB VRAM**, approximately 16 GB initially free, and 60 GiB system RAM with approximately 50 GiB available. The existing Bonsai 27B service and desktop remained running.

The initial model is **Whisper large-v3-turbo**, CTranslate2 weights from `mobiuslabsgmbh/faster-whisper-large-v3-turbo`, pinned to revision `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`. Backend: **faster-whisper 1.2.1 / CTranslate2 4.8.2**, CUDA on device 0, float16. Startup verifies device 0 is an RTX 4090 and the CLI rejects CPU/non-4090 endpoints; CPU inference fallback is disabled. Isolated NVIDIA CUDA 12/cuDNN 9 wheels supply their own libraries through the unit's `LD_LIBRARY_PATH`; system CUDA remains intact. Model weights are cached at `/home/tsilva/.cache/tubescribe/models`.

`GET /health` returns readiness, busy state, model identity, revision, backend versions, precision, GPU name/device/memory, remote hostname, and concurrency. `POST /transcribe` accepts a multipart field named `audio`. It returns timestamped segments and words, English/other detected language, language probability, segment confidence, model provenance, audio/speech duration, UTC start/end times, and measured processing seconds. Uploads are bounded at 1 GiB. Temporary remote audio is removed after inference, including on failure.

One inference request runs at a time; busy requests receive HTTP 503. Beam size 5, voice activity detection, and `condition_on_previous_text=False` reduce repetition while preserving timestamped speech. Speech recognition still warrants review for names, tickers, numbers, and quoted material.

To upgrade explicitly, change the unit's `TUBESCRIBE_MODEL` and `TUBESCRIBE_MODEL_REVISION`, stop/start the service, and index/run using a **new profile** such as `turbo-v2`. A profile is bound to the observed model revision/backend/decoding identity; mismatches fail before downloads. Existing transcripts are preserved:

```bash
scripts/tubescribe sync --profile turbo-v2
scripts/tubescribe work --profile turbo-v2 --wait
```

## Remote speaker diarization

The production diarizer is **NVIDIA Nemotron-3-Diarization**, pinned to revision `f667ed73aee57d40cc39428eb768b4fd87a0a29e`, using Transformers 5.18.0 / PyTorch 2.8.0+cu128, float32 on **RTX 4090 device 0**. It is ungated and needs no Hugging Face token. The isolated environment is `/home/tsilva/.local/share/tubescribe/diarization-service`; it shares cached model weights but does not change the ASR environment or other running models.

```bash
scripts/tubescribe tunnel start                    # authorize/reuse the main SSH connection
scripts/remote-diarization.sh install              # source + locked isolated Linux environment
scripts/remote-diarization.sh start
scripts/tubescribe diarization-tunnel start
scripts/tubescribe diarization-tunnel status
scripts/remote-diarization.sh status
scripts/remote-diarization.sh logs
scripts/tubescribe diarization-tunnel stop         # cancel only this forward, preserving ASR
scripts/remote-diarization.sh stop                 # free this model's GPU memory
```

The user systemd unit is `tubescribe-diarization`, with lingering enabled. Installation does not restart a running service; stop it before deploying changed service code. The service binds **127.0.0.1:18767** remotely, forwarded to **http://127.0.0.1:18768** locally on the main authorized SSH master. State lives in `data/diarization-tunnel.json`. `start` reuses a healthy forward or restores it after reconnection. Ports can be set with `TUBESCRIBE_DIARIZATION_REMOTE_PORT` on installation/start and `diarization-tunnel start --remote-port PORT --local-port PORT`; use `--local-port 0` for an available local port. Stop an existing forward before changing ports. An occupied unhealthy port fails clearly. Stopping the main tunnel also disconnects this forward; restart both forwards afterward.

`GET /health` returns model/GPU identity, readiness and busy state. `POST /diarize` takes a binary audio body with `Content-Length`; requests are limited to 1 GiB and decoded audio to four hours. ffmpeg decodes mono 16 kHz audio. One resident model handles one request at a time (HTTP 503 when busy), using the model's 340-second chunks and 40-second right context. Startup requires at least 2 GiB free VRAM; other models are left running. Unused PyTorch request buffers are released after requests while keeping model weights loaded, leaving room for ASR and other workloads. Responses retain overlapping speaker activity plus exclusive turns, UTC times and processing seconds. Archive words are assigned by temporal overlap; unmatched words remain `UNKNOWN` and are never silently dropped.

Speaker IDs are anonymous and local to each video: **Speaker 1 is not a verified person name** and can refer to a different person in another video. A separately recorded `speaker_name` takes precedence in SRT/Markdown. Voice changes, background speech and overlap can produce extra labels. The model does not identify Jordi or guests. Full original ASR segments/word confidence remain in JSON alongside speaker-labelled cues.

For already saved transcripts, stop the ASR backfill, wait for its worker to exit, then run `scripts/tubescribe diarize --wait`, or use the detached enrichment worker below. Deleted source audio is downloaded again for diarization, but ASR is reused. Diarization/download/save attempts have independent bounded counters (three by default) and checkpoints; interrupted connections pause the batch without consuming other videos' retry budgets. After enrichment, resume with `scripts/backfill.py start`; future videos include diarization automatically. An intentional model/settings upgrade requires a new profile, rather than silently relabelling completed work.

```bash
scripts/backfill.py stop
scripts/backfill.py status                         # wait until archive_worker_active is false
scripts/backfill.py start --diarize
scripts/backfill.py status --diarize
tail -f data/diarization-backfill.log
scripts/backfill.py stop --diarize                 # safe stop; source/checkpoints retained
```

Both modes use the same exclusive archive lock and separately verified process-group state. Stop the matching mode before switching. Like the normal import, detached enrichment uses `caffeinate` on macOS and survives chat completion, but requires restarting after a reboot. The normal import remains stopped during enrichment.

## Archive commands

```bash
scripts/tubescribe channels add https://www.youtube.com/@JordiVisserLabs
scripts/tubescribe channels list
scripts/tubescribe latest --count 3                # resolve dates across all channel tabs; queue the latest three
scripts/tubescribe sync                            # index accessible public history, without transcribing
scripts/tubescribe sync UCSLOw8JrFTBb3qF-p4v0v_w
scripts/tubescribe status
scripts/tubescribe status --failures
scripts/tubescribe work --video hKBjZBiw62U --video LhuSxhCNa5g --video BkNzS8Ia-7k
scripts/tubescribe work --limit 3
scripts/tubescribe work --video VIDEO_ID --keep-audio # retain successful source for quality review
scripts/tubescribe retry --video VIDEO_ID           # reset failed job attempt counters/backoff
scripts/tubescribe retry                           # reset all failed jobs
scripts/tubescribe work --wait                     # drain pending work and wait for bounded automatic retries
scripts/tubescribe diarize --wait                  # enrich already saved ASR; no repeat ASR
scripts/tubescribe diarize --video VIDEO_ID         # enrich one saved transcript
scripts/tubescribe subtitles                       # add/repair transcript.srt and transcript.md
scripts/tubescribe subtitles --video hKBjZBiw62U   # one saved video
scripts/tubescribe subtitles /absolute/path/to/subtitles --profile turbo-v1 # export VIDEO_ID.PROFILE.srt/.md files
```

Jordi Visser's stable channel ID is **UCSLOw8JrFTBb3qF-p4v0v_w**. Discovery checks `/videos`, `/shorts`, and `/streams` independently. Absent tabs count as empty; failed tab requests remain visible. Active/upcoming livestreams and recordings still being prepared are excluded. Flat indexing does not always expose exact upload dates; `latest` resolves metadata before choosing and a download checks availability/channel identity again. `sync --limit N` limits each tab rather than the whole channel.

Downloads prefer audio-only HLS (`bestaudio[protocol^=m3u8]/bestaudio`), because direct DASH audio returned HTTP 403 for two smoke-test videos. Normal ingestion uses one fragment at a time; redownloading for saved-ASR enrichment uses two concurrent fragments. Both process only one video at a time, retain request/download sleeps, and use two bounded yt-dlp network retries. Unavailable HLS fragments cause failure rather than a silently truncated transcript. Decoded duration is checked against video metadata. Stage attempts default to three total attempts, with exponential cooldowns starting at one minute. Rate limits/bot checks pause the entire worker for 30 minutes with `--wait`, or stop the current run without it. No cookies or authenticated YouTube account are required by default; inaccessible/private/member-only videos are recorded as failures when encountered. Discovery reflects currently accessible public listings and cannot enumerate hidden/private/deleted content.

Each video/profile pair has a unique job. Download, ASR, and diarization have independent states; discovery is tracked per channel/tab. `work` runs download → ASR → diarization → saved speaker-labelled formats. ASR commits first as a durable checkpoint; failed diarization resumes from that checkpoint without repeating ASR, even if ASR succeeded on its last allowed attempt. Only one worker can hold the archive lock. After an interrupted worker releases the lock, the next worker reopens active stages and reuses any committed download. Operator interruptions do not consume stage retry budgets. Jobs with intact transcripts and completed diarization are skipped without an inference request. Missing/corrupt saved JSON/text is repaired from valid ASR and diarization checkpoints when available. JSON, readable text, SubRip subtitles, and Markdown turns are atomically written and fsynced before SQLite completion commits; audio is removed **only after diarization completion commits** (unless `--keep-audio` is requested for a review). Failed inference or storage retains audio for retry. `status` reports ASR and diarization separately; `status --failures` and `retry` include both stages.

**SubRip (`.srt`)** is the default subtitle format: plain UTF-8, numbered cues and `HH:MM:SS,mmm` timestamps. Diarized cues are split at speaker changes and kept short for subtitles, with a speaker prefix; older ASR-only cues retain their original segment boundaries. No video title or metadata is inserted into subtitle cues; JSON retains full provenance. Existing archives gain SRT files through `subtitles` or on the next worker run, using saved JSON without downloading or transcribing again. Missing/corrupt SRT and Markdown files are regenerated from that authoritative JSON. Stop an active backfill before `subtitles` or backup/export to obtain the archive lock, then restart it. Subtitle exports can reuse a destination directory; matching video/profile filenames are updated, while unrelated files remain untouched. CLI output lists both formats and counts all generated files.

The default archive directory is `data/` in this repository (the wrapper always changes to the repository root):

- `data/archive.sqlite3`: channels, discovery state, videos, jobs, ASR/diarization profiles, independent diarization state, transcript index and hashes.
- `data/transcripts/CHANNEL_ID/VIDEO_ID/PROFILE/transcript.json`: timestamped segments/words and complete ASR/video provenance.
- `data/transcripts/CHANNEL_ID/VIDEO_ID/PROFILE/transcript.txt`: readable timestamped text.
- `data/transcripts/CHANNEL_ID/VIDEO_ID/PROFILE/transcript.srt`: SubRip subtitles for video players/editors.
- `data/transcripts/CHANNEL_ID/VIDEO_ID/PROFILE/transcript.md`: consecutive cues grouped into speaker turns, each prefixed with `**Speaker name:**`. Recorded names take precedence over anonymous speaker IDs; unlabelled ASR uses `Speaker`. The formatter does not infer names or run diarization. A monologue becomes one turn, with subtitle timestamps retained in SRT rather than repeated in Markdown.
- `data/transcripts/CHANNEL_ID/VIDEO_ID/PROFILE/transcript.asr.json`: immutable original ASR checkpoint and hash; word timestamps/confidence are retained.
- `data/transcripts/CHANNEL_ID/VIDEO_ID/PROFILE/diarization.json`: immutable model prediction checkpoint, raw overlapping turns, exclusive turns, GPU/model provenance and processing time. Repairs reuse it without GPU inference.
- `data/audio/VIDEO_ID.EXT`: temporary or retained retry audio; complete downloads use the original audio codec.
- `data/smoke.log`, `data/backfill.log`, `data/diarization-backfill.log`: stage events with UTC timestamps, errors, duration, and inference speed.

Override with `--data /absolute/path` (before the command) or `TUBESCRIBE_DATA`. Data, audio, transcripts, logs, secrets, environments, and model weights are ignored by Git.

## Background import and backup

After a successful smoke test, start the already indexed history:

```bash
scripts/backfill.py start
scripts/backfill.py status
scripts/tubescribe status --failures
tail -f data/backfill.log
scripts/backfill.py stop
```

The worker starts in a separate process session and survives this chat/terminal finishing. On macOS, `caffeinate -i` prevents idle sleep for the worker's lifetime. It does not survive a reboot; run `start` again afterward. The worker checks both inference services before downloading and reconnects recorded forwards after a lost connection. If reconnection requires fresh SSH authorization or a service is unavailable, it stops the queue and retains audio and checkpoints. Authorize/reconnect, inspect failures, retry, and restart. A PID alone is not trusted for stopping: the script checks the live archive lock and process group before signaling its supervisor group. Download subprocesses and their ffmpeg children are stopped before the worker releases its lock. Stop requests retain recoverable stage state; wait until `status` reports stopped before taking a backup.

```bash
scripts/tubescribe export /absolute/path/to/a/new/tubescribe-backup
```

Export holds the worker lock, uses SQLite's backup API, and copies JSON, text, SRT, and Markdown transcripts, original ASR/diarization checkpoints and retained retry audio into a **new** directory outside the live archive. It generates SRT and Markdown from saved JSON for older completed transcripts before copying. Stop an active worker first. Restore by selecting the exported directory with `--data`, creating/reconnecting both forwards, and resuming `work`. The export deliberately omits SSH control sockets and host-specific tunnel state. Copy the export to your normal backup storage; a local copy alone does not protect against disk loss.

## Admin dashboard

```bash
scripts/tubescribe dashboard                       # start in the background; prints the URL
scripts/tubescribe dashboard status                # current URL, PID, and health
scripts/tubescribe dashboard stop                  # stop only the verified dashboard process
scripts/tubescribe dashboard start --port 8877     # optional fixed port
scripts/tubescribe dashboard serve --port auto     # run in this terminal; Ctrl-C stops it
```

The dashboard binds only to `127.0.0.1`. The default `--port auto` asks the OS for an available port; use the printed URL, or `dashboard status` to find it again. Starting an already running dashboard reuses it, even if a different port was requested. To change its port, stop it first. An unavailable fixed port reports a startup failure rather than replacing another server. The background process survives chat/terminal completion, but does not automatically restart after a reboot. State and logs live in `data/dashboard.json`, `data/dashboard.lock`, and `data/dashboard.log` (or the selected `--data` directory).

The dashboard is a single channel → videos page. Each channel lists its videos newest first, with title, source link, publish date, duration, status, download/ASR/speaker progress, model/profile, processing times, and any failure or retry details directly in the row. A video appears once; multiple model profiles are shown together within that video’s row. Search matches channel names/IDs and video titles/IDs. Empty channels and discovery errors remain visible. There are no separate overview, files, or video-details views.

Each saved transcript has a **Transcript** button in its row, including ASR transcripts that are still awaiting speaker diarization. It opens a preview with Markdown, text, SRT, or JSON format selection and download. Markdown previews display speaker turns. Previews are limited to 2 MiB; downloads contain the complete file.

The dashboard reads SQLite in short transactions and does not acquire the worker's archive lock or run inference. It refreshes every five seconds while visible, preserves search and open previews, and only rebuilds the video list when its data changes. `Awaiting diarization` means ASR is saved but speakers are pending. `Complete` requires both saved ASR and completed diarization. Status reflects recorded state; a stopped worker may leave a stage recorded as active until the next worker recovers it. Worker liveness is shown in the header and beside any interrupted active stage.

This is a read-only local monitor. Use the CLI for adding channels, discovery, retries, and worker control. File access is restricted to recognized transcript/review outputs and audio inside the archive; it does not expose SQLite, SSH configuration, sockets, or arbitrary local files. No external frontend assets or JavaScript packages are required.

## Verification

```bash
uv --config-file uv.toml run --locked python -m unittest discover -s tests -v
```

Tests cover duplicate indexing, completed-job skips, independent ASR/diarization state, speaker-word assignment and unmatched speech, inference failure with audio reuse, checkpoint integrity, failed durable writes, repair without repeated inference, exclusive workers, interrupted-stage recovery, bounded attempts/backoff, absent tabs vs bot checks, explicit upgrade profiles, RTX 4090 enforcement, tunnel reconnection, offline batch suspension, downloader process cleanup, dashboard diarization state, and timestamp/schema validation. The real smoke run processes the latest three videos end to end, then reruns those IDs to check that no download or ASR runs again. Runtime quality/speed results are recorded separately in ignored `data/quality/`; channel history is complete only when its recorded jobs establish completion.

### Verified first run (9 October 2026)

The latest three Jordi Visser videos (`hKBjZBiw62U`, `LhuSxhCNa5g`, `BkNzS8Ia-7k`) completed with **167.8 minutes of audio in 112.7 seconds of ASR**, approximately **89.3× real time** on beast-3's RTX 4090. A rerun reported `completed: 0, failed: 0, skipped: 3`. Seven source audio clips independently checked with Whisper large-v3 on the RTX 4090 had **97.7% normalized word agreement**, which is cross-model agreement rather than human-reference accuracy. Timestamp/content inspection passed. Full results and source excerpts are in `data/quality/report.md` and `report.json`. Downloading and tunnel upload add wall-clock time.

Initial full discovery indexed **113** accessible uploads. Use `status` for the live completion count; the smoke test alone does not establish a complete channel archive.

### Verified archive diarization (9 October 2026)

All **26** previously saved videos were enriched on beast-3's RTX 4090: **22.5 hours of audio in 98.9 seconds of remote processing**, approximately **820× real time**, including decoding but excluding downloads, tunnel uploads and local formatting. Original ASR segments and all **248,108** nonempty words/timestamps/confidence values were preserved; **26** unmatched words remain `UNKNOWN`. Output/checkpoint hashes and subtitle timestamps passed checks. A saved-ASR rerun returned `completed: 0, failed: 0, skipped: 26`. This verifies preservation and processing, rather than human-reference diarization accuracy. Runtime details and per-video results are in `data/diarization/archive-report.md` and `.json`.

The integrated pipeline then recovered `Sopf31BOP4U` from its retained failed-ASR audio: **60.4 minutes** transcribed in **38.9 seconds**, followed automatically by **4.7 seconds** of diarization, all formats committed, and audio removed afterward. All 10,999 ASR words were preserved; its rerun skipped the completed job. Evidence is in `data/quality/pipeline-diarization.json` and its logs. The existing Bonsai and ASR services stayed running; the diarizer retained its model and returned unused GPU buffers after requests (868 MiB process VRAM observed after this request). Service restart and independent forward stop/start were also verified. Use the dashboard/CLI for current backfill progress.

### Speaker diarization experiment

An isolated [diarization trial](experiments/diarization/README.md) tested NVIDIA Nemotron 3 on beast-3's RTX 4090 against a Jordi/Marty interview and a Jordi monologue. It downloaded without authentication and detected two speakers and one speaker respectively. The ten-minute interview took 1.03 seconds for diarization after loading, with 97.9% agreement on speech covered by approximate publisher labels; this is not general accuracy. Speaker-labelled SRT and detailed evidence are under ignored `data/diarization/`. Pyannote Community-1 is an optional gated comparison that has not run. The production pipeline now uses Nemotron for speaker labels; person names in the original trial were reference-guided.
