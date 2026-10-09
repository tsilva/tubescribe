# TubeScribe

A local personal YouTube transcript archive. This Mac owns channel discovery, the SQLite queue, downloaded audio, and durable transcripts. Beast-3 runs Whisper ASR over a loopback SSH tunnel. No paid transcript provider or web UI is needed.

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
TUBESCRIBE_REMOTE_PORT=18767 scripts/remote-service.sh install
TUBESCRIBE_REMOTE_PORT=18767 scripts/remote-service.sh start
scripts/tubescribe tunnel start --remote-port 18767 --local-port 0
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
scripts/tubescribe subtitles                       # add/repair transcript.srt beside all completed transcripts
scripts/tubescribe subtitles --video hKBjZBiw62U   # one saved video
scripts/tubescribe subtitles /absolute/path/to/subtitles --profile turbo-v1 # export VIDEO_ID.PROFILE.srt files
```

Jordi Visser's stable channel ID is **UCSLOw8JrFTBb3qF-p4v0v_w**. Discovery checks `/videos`, `/shorts`, and `/streams` independently. Absent tabs count as empty; failed tab requests remain visible. Active/upcoming livestreams and recordings still being prepared are excluded. Flat indexing does not always expose exact upload dates; `latest` resolves metadata before choosing and a download checks availability/channel identity again. `sync --limit N` limits each tab rather than the whole channel.

Downloads prefer audio-only HLS (`bestaudio[protocol^=m3u8]/bestaudio`), because direct DASH audio returned HTTP 403 for two smoke-test videos. They use one fragment at a time, request/download sleeps, and two bounded yt-dlp network retries. Unavailable HLS fragments cause failure rather than a silently truncated transcript. Decoded duration is checked against video metadata. Stage attempts default to three total attempts, with exponential cooldowns starting at one minute. Rate limits/bot checks pause the entire worker for 30 minutes with `--wait`, or stop the current run without it. No cookies or authenticated YouTube account are required by default; inaccessible/private/member-only videos are recorded as failures when encountered. Discovery reflects currently accessible public listings and cannot enumerate hidden/private/deleted content.

Each video/profile pair has a unique job. Download, ASR, and overall completion have independent states; discovery is tracked per channel/tab. Only one worker can hold the archive lock. After an interrupted worker releases the lock, the next worker reopens active stages and reuses any committed download. Complete jobs with intact transcript hashes are skipped without an inference request. Missing/corrupt saved JSON/text is queued for repair. JSON, readable text, and SubRip subtitles are atomically written and fsynced before SQLite completion commits; audio is removed **only after that commit** (unless `--keep-audio` is requested for a review). Failed inference or storage retains audio for retry.

**SubRip (`.srt`)** is the default subtitle format: plain UTF-8, numbered cues, `HH:MM:SS,mmm` timestamps, and one cue per ASR segment. No video title or metadata is inserted into subtitle cues; JSON retains full provenance. Existing archives gain SRT files through `subtitles` or on the next worker run, using saved JSON without downloading or transcribing again. Missing/corrupt SRT files are regenerated from that authoritative JSON. Stop an active backfill before `subtitles` or backup/export to obtain the archive lock, then restart it. Subtitle exports can reuse a destination directory; matching video/profile filenames are updated, while unrelated files remain untouched.

The default archive directory is `data/` in this repository (the wrapper always changes to the repository root):

- `data/archive.sqlite3`: channels, discovery state, videos, jobs, ASR profiles, transcript index and hashes.
- `data/transcripts/CHANNEL_ID/VIDEO_ID/PROFILE/transcript.json`: timestamped segments/words and complete ASR/video provenance.
- `data/transcripts/CHANNEL_ID/VIDEO_ID/PROFILE/transcript.txt`: readable timestamped text.
- `data/transcripts/CHANNEL_ID/VIDEO_ID/PROFILE/transcript.srt`: SubRip subtitles for video players/editors.
- `data/audio/VIDEO_ID.EXT`: temporary or retained retry audio; complete downloads use the original audio codec.
- `data/smoke.log`, `data/backfill.log`: stage events with UTC timestamps, errors, duration, and inference speed.

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

The worker starts in a separate process session and survives this chat/terminal finishing. On macOS, `caffeinate -i` prevents idle sleep for the worker's lifetime. It does not survive a reboot; run `start` again afterward. The worker checks ASR before each download and again before inference, reconnecting its recorded tunnel after a lost connection. If reconnection requires fresh SSH authorization or the service is unavailable, it stops the queue and retains audio. Authorize/reconnect, inspect failures, retry, and restart. A PID alone is not trusted for stopping: the script checks the live archive lock and process group before signaling its supervisor group. Download subprocesses and their ffmpeg children are stopped before the worker releases its lock. Stop requests retain recoverable stage state; wait until `status` reports stopped before taking a backup.

```bash
scripts/tubescribe export /absolute/path/to/a/new/tubescribe-backup
```

Export holds the worker lock, uses SQLite's backup API, and copies JSON, text, SRT transcripts plus retained retry audio into a **new** directory outside the live archive. It generates SRT from saved JSON for older completed transcripts before copying. Stop an active worker first. Restore by selecting the exported directory with `--data`, creating/reconnecting a tunnel, and resuming `work`. The export deliberately omits SSH control sockets and host-specific tunnel state. Copy the export to your normal backup storage; a local copy alone does not protect against disk loss.

## Verification

```bash
uv --config-file uv.toml run --locked python -m unittest discover -s tests -v
```

Tests cover duplicate indexing, completed-job skips, independent stage state, inference failure with audio reuse, failed durable writes, exclusive workers, interrupted-stage recovery, bounded attempts/backoff, absent tabs vs bot checks, explicit upgrade profiles, RTX 4090 enforcement, tunnel reconnection, offline queue suspension, downloader process cleanup, and timestamp/schema validation. The real smoke run processes the latest three videos end to end, then reruns those IDs to check that no download or ASR runs again. Runtime quality/speed results are recorded separately in ignored `data/quality/`; channel history is complete only when its recorded jobs establish completion.

### Verified first run (9 October 2026)

The latest three Jordi Visser videos (`hKBjZBiw62U`, `LhuSxhCNa5g`, `BkNzS8Ia-7k`) completed with **167.8 minutes of audio in 112.7 seconds of ASR**, approximately **89.3× real time** on beast-3's RTX 4090. A rerun reported `completed: 0, failed: 0, skipped: 3`. Seven source audio clips independently checked with Whisper large-v3 on the RTX 4090 had **97.7% normalized word agreement**, which is cross-model agreement rather than human-reference accuracy. Timestamp/content inspection passed. Full results and source excerpts are in `data/quality/report.md` and `report.json`. Downloading and tunnel upload add wall-clock time.

Initial full discovery indexed **113** accessible uploads. Use `status` for the live completion count; the smoke test alone does not establish a complete channel archive.

### Speaker diarization experiment

An isolated [diarization trial](experiments/diarization/README.md) tested NVIDIA Nemotron 3 on beast-3's RTX 4090 against a Jordi/Marty interview and a Jordi monologue. It downloaded without authentication and detected two speakers and one speaker respectively. The ten-minute interview took 1.03 seconds for diarization after loading, with 97.9% agreement on speech covered by approximate publisher labels; this is not general accuracy. Speaker-labelled SRT and detailed evidence are under ignored `data/diarization/`. Pyannote Community-1 is an optional gated comparison that has not run. Archive workers currently save ASR without speaker labels; names in this trial are reference-guided.
