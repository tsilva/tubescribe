# TubeScribe

A local personal YouTube transcript archive. This Mac owns channel discovery, the SQLite queue, cached media, and durable transcripts. Beast-3 runs Whisper ASR and Nemotron speaker diarization on its RTX 4090 over loopback SSH tunnels. AgentBridge/Codex produces cleaned Markdown with inspected video figures and evidence-backed speaker names, then generates a one-page summary as the final artifact. A local dashboard lists channels and videos and opens the summary when available, with the full transcript linked from it; the CLI manages ingestion.

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

The production diarizer is **NVIDIA Nemotron-3-Diarization**, pinned to revision `f667ed73aee57d40cc39428eb768b4fd87a0a29e`, with the configured new-install baseline Transformers 5.18.0 / PyTorch 2.13.0+cu130, float32 on **RTX 4090 device 0**. It is ungated and needs no Hugging Face token. The locked Linux Torch wheel requires a CUDA 13-compatible NVIDIA driver. On 2026-10-10 the idle service was redeployed and verified healthy on Torch 2.13.0+cu130, with the previous environment retained for rollback. Updating the repository alone does not restart or upgrade a service. Existing diarization profiles retain their original backend identity; use a new explicit profile such as `turbo-security-20261010` for future ingestion after this upgrade. The isolated environment is `/home/tsilva/.local/share/tubescribe/diarization-service`; it shares cached model weights but does not change the ASR environment or other running models.

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

A detected voice can be absent from Whisper's words, such as a tablet speaking during a presentation. Enrichment preserves activity for such a voice as an explicit **Untranscribed speech** turn with timestamps in every published format. These annotations have `annotation: "untranscribed_speech"` and an empty `words` list in JSON; they are observations from diarization, not recognized dialogue. They occupy gaps outside existing subtitle cues, merge fragments separated by at most 0.1 seconds, and require at least 0.5 seconds of activity. `diarization_coverage` records voices with ASR words, voices without ASR words, and the annotated intervals. Fully overlapping activity or shorter detections remain in the model prediction and coverage metadata without replacing existing words. A completed enrichment means the prediction and formats were saved; untranscribed annotations still need speech recognition or human review. Most Jordi presentations are monologues, so one speaker label is expected.

For already saved transcripts, stop the ASR backfill, wait for its worker to exit, then run `scripts/tubescribe diarize --wait`, or use the detached enrichment worker below. Deleted source audio is downloaded again for diarization, but ASR is reused. Diarization/download/save attempts have independent bounded counters (three by default) and checkpoints; interrupted connections pause the batch without consuming other videos' retry budgets. After enrichment, resume with `scripts/backfill.py start`; future videos include diarization automatically. An intentional model/settings upgrade requires a new profile, rather than silently relabelling completed work.

```bash
scripts/backfill.py stop
scripts/backfill.py status                         # wait until archive_worker_active is false
scripts/backfill.py start --diarize
scripts/backfill.py status --diarize
tail -f data/diarization-backfill.log
scripts/backfill.py stop --diarize                 # safe stop; source/checkpoints retained
```

Both modes use the same exclusive archive capacity lease and separately verified process-group state. Stop the matching mode before switching. Like the normal import, detached enrichment uses `caffeinate` on macOS and survives chat completion, but requires restarting after a reboot. The normal import remains stopped during enrichment.

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
scripts/tubescribe work --postprocessing text        # full speech cleanup without video inspection
scripts/tubescribe work --no-summary                 # finish Markdown without the final summary
scripts/tubescribe work --postprocessing off         # explicitly stop after diarization
scripts/tubescribe work --video VIDEO_ID --keep-audio # retain successful source for quality review
scripts/tubescribe retry --video VIDEO_ID           # reset failed job attempt counters/backoff
scripts/tubescribe retry                           # reset all failed jobs
scripts/tubescribe work --wait                     # drain pending work and wait for bounded automatic retries
scripts/tubescribe diarize --wait                  # enrich already saved ASR; no repeat ASR
scripts/tubescribe diarize --video VIDEO_ID         # enrich one saved transcript
scripts/tubescribe postprocess --video VIDEO_ID      # finish Markdown and summary; no ASR/diarization
scripts/tubescribe postprocess --limit 3 --wait      # bounded final-stage batch/retries
scripts/tubescribe postprocess --video VIDEO_ID --force # new model run; retain previous bundles
scripts/backfill.py start --postprocess             # detached final-stage batch; no earlier service/tunnel startup
scripts/tubescribe subtitles                       # add/repair transcript.srt and transcript.md
scripts/tubescribe subtitles --video hKBjZBiw62U   # one saved video
scripts/tubescribe subtitles /absolute/path/to/subtitles --profile turbo-v1 # export VIDEO_ID.PROFILE.srt/.md files
```

Jordi Visser's stable channel ID is **UCSLOw8JrFTBb3qF-p4v0v_w**. Discovery checks `/videos`, `/shorts`, and `/streams` independently. Absent tabs count as empty; failed tab requests remain visible. Active/upcoming livestreams and recordings still being prepared are excluded. Flat indexing does not always expose exact upload dates; `latest` resolves metadata before choosing and a download checks availability/channel identity again. `sync --limit N` limits each tab rather than the whole channel.

Downloads prefer audio-only HLS (`bestaudio[protocol^=m3u8]/bestaudio`), because direct DASH audio returned HTTP 403 for two smoke-test videos. Normal ingestion uses one fragment at a time; redownloading for saved-ASR enrichment uses two concurrent fragments. Both process only one video at a time, retain request/download sleeps, and use two bounded yt-dlp network retries. Unavailable HLS fragments cause failure rather than a silently truncated transcript. Decoded duration is checked against video metadata. Stage attempts default to three total attempts, with exponential cooldowns starting at one minute. Rate limits/bot checks pause the entire worker for 30 minutes with `--wait`, or stop the current run without it. No cookies or authenticated YouTube account are required by default; inaccessible/private/member-only videos are recorded as failures when encountered. Discovery reflects currently accessible public listings and cannot enumerate hidden/private/deleted content.

Each video/profile pair has a unique job. Download, ASR, diarization, postprocessing, and summary have independent states; discovery is tracked per channel/tab. `work` defaults to download → ASR → diarization → AgentBridge postprocessing → one-page summary. Raw speaker-labelled formats commit before the final step. Failed diarization resumes from ASR, and failed postprocessing resumes from the diarized source without either earlier inference call, even if an earlier stage succeeded on its last allowed attempt. Only one worker can hold the archive capacity lease. Operator interruptions do not consume stage retry budgets. Jobs with intact transcripts, completed diarization, a valid processed bundle, and a valid summary are skipped. Summary failures resume from the finished Markdown and saved figures without earlier inference or a video download; model/save attempts and retry backoff are tracked separately. `retry` resets failed artifact budgets too. Missing/corrupt saved JSON/text is repaired from ASR and diarization checkpoints when available. Raw formats are atomically written and fsynced before SQLite commits; audio is removed **only after diarization completion commits** (unless `--keep-audio` is requested). A later postprocessing failure preserves those raw formats and the separate video cache. `status` reports all four inference stages; `status --failures` and `retry` include summaries. Overall completion requires both the processed bundle and the summary; completed older transcripts await a summary until the next scoped worker run. `--no-summary` explicitly stops after processed Markdown. Old diarized videos appear as awaiting postprocessing until finished. `--postprocessing off` explicitly preserves the earlier ASR/diarization-only worker behavior.

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

The worker starts in a separate process session and survives this chat/terminal finishing. On macOS, `caffeinate -i` prevents idle sleep for the worker's lifetime. It does not survive a reboot; run `start` again afterward. The worker checks both inference services before downloading and reconnects recorded forwards after a lost connection. If reconnection requires fresh SSH authorization or a service is unavailable, it stops the queue and retains audio and checkpoints. Authorize/reconnect, inspect failures, retry, and restart. A PID alone is not trusted for stopping: the script checks the live archive lease and process group before signaling its supervisor group. Download subprocesses and their ffmpeg children are stopped before the worker releases its lease. Stop requests retain recoverable stage state; wait until `status` reports stopped before taking a backup.

```bash
scripts/tubescribe export /absolute/path/to/a/new/tubescribe-backup
```

Export holds the archive lease, uses SQLite's backup API, and copies JSON, text, SRT, and Markdown transcripts, original ASR/diarization checkpoints and retained retry audio into a **new** directory outside the live archive. It generates SRT and Markdown from saved JSON for older completed transcripts before copying. Stop an active worker first. Restore by selecting the exported directory with `--data`, creating/reconnecting both forwards, and resuming `work`. The export deliberately omits SSH control sockets and host-specific tunnel state. Copy the export to your normal backup storage; a local copy alone does not protect against disk loss.

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

A completed pipeline has a **Summary** button in its row. The summary links to the illustrated full transcript. Until a summary is available, each saved transcript has a **Transcript** button, including ASR transcripts that are still awaiting speaker diarization. It opens a preview with Markdown, text, SRT, or JSON format selection and download. Markdown previews display speaker turns. Previews are limited to 2 MiB; downloads contain the complete file.

The dashboard reads SQLite in short transactions. Its monitoring endpoints do not acquire the worker's archive lock or run inference. It refreshes every five seconds while visible, preserves search and open previews, and only rebuilds the video list when its data changes. `Awaiting diarization` means ASR is saved but speakers are pending. Final completion requires saved ASR, diarization, a processed bundle, and its summary. `Awaiting summary` and `Summarizing` distinguish the final step; summary failures appear directly in the row. Status reflects recorded state; a stopped worker may leave a stage recorded as active until the next worker recovers it. Worker liveness is shown in the header and beside any interrupted active stage.

The processed transcript header links to available artifact workflows. Open **One-page summary**, then **Build artifact** to launch a background worker; its status page refreshes until completion. This same-origin form is the dashboard's only write action, and repeated clicks share one worker. Use the CLI for adding channels, discovery, retries, and other worker control. File access is restricted to recognized transcript/review/artifact outputs and audio inside the archive; it does not expose SQLite, SSH configuration, sockets, or arbitrary local files. No external frontend assets or JavaScript packages are required.

## Artifact workflows

Artifact workflows consume the completed, illustrated Markdown through AgentBridge with
`codex/gpt-6.1-sol` at **high** reasoning. The built-in summary runs automatically as
the final step of both `work` and `postprocess`. Other workflows run on demand after
postprocessing; `--no-summary` skips the default final step. The built-in `summary` targets 450–600 words
with a hard 650-word limit: central thesis, major insights, mechanisms, consequential
numbers, and caveats. Delivering roughly 95% of useful insight is an editorial objective,
not a measured accuracy claim. Summaries link to at most three saved screenshots instead
of embedding them, and their preview includes a compact A4 print layout.

```bash
scripts/tubescribe artifacts --video VIDEO_ID                    # build/retry summary directly
scripts/tubescribe artifacts --workflow summary --limit 3 --wait # bounded batch; wait for archive lock
scripts/tubescribe artifacts --list-workflows
scripts/tubescribe artifacts --video VIDEO_ID --force            # regenerate; preserve previous bundle
```

`--video` is repeatable; `--profile`, `--bridge`, and `--max-attempts` select the source
profile, saved/explicit bridge URL, and retry budget. Omitting `--video` selects completed
processed transcripts in the profile. No source-video download or earlier inference is
needed. The agent receives final Markdown, unresolved source issues, and a saved figure
index. An optional tool supplies real saved pixels in batches of up to four images,
with at most three calls and six distinct images per run. Text-only transcripts work too.
The caller validates image IDs, copies referenced figures, and stitches their links into
Markdown. Evidence quotes for main claims must occur in the source; this validates quote
provenance, not semantic correctness of every claim.

Additional Markdown workflows are JSON definitions in `data/workflows/NAME.json`
(or the selected archive root). Names use lowercase letters, numbers, underscores, and
hyphens and start with a letter; `summary` is reserved for the built-in workflow. For
example, `data/workflows/study-guide.json`:

```json
{
  "title": "Study guide",
  "instructions": "Explain the central concepts, their relationships, and five questions for checking understanding. Link saved figures when useful. Preserve source uncertainty.",
  "max_words": 1200
}
```

Run it with `scripts/tubescribe artifacts --workflow study-guide --video VIDEO_ID`, or
select its link in the processed transcript header. Definitions require exactly these
three fields; `max_words` ranges from 50 to 10,000. This extension point currently
produces Markdown and HTML previews, rather than arbitrary binary document formats.

The `artifacts` table tracks each job/workflow's state, source and definition fingerprints,
attempts, retry backoff, active PID, errors, and latest completed output. Bundles live separately in
`artifacts/CHANNEL/VIDEO/PROFILE/WORKFLOW/RUN_ID/` with canonical `artifact.md`, portable `read.md`, `preview.html`, logical `references.json`,
referenced `figures/`, request/response checkpoints, evidence/usage report, and a hashed
manifest. Intact completed outputs skip inference; changed source or workflow definitions
generate a new run on the next invocation. Save failures resume from the validated model
response without another inference call. Artifact failures preserve completed transcript stages while keeping overall pipeline completion
pending until the summary succeeds. All runs use the shared archive capacity lease. Export includes artifact bundles and
custom workflow definitions alongside the transcript archive.

## Local runtime and future cloud adapters

The runtime remains local. No cloud infrastructure, credentials, service migration, or
new dependency is required. Existing completed bundles and checkpoints stay readable.

| Module | Responsibility |
| --- | --- |
| `src/tubescribe/pipeline.py` | Stage dependencies, completion, retry eligibility, dashboard status, and shared final-stage execution with typed inputs/results. |
| `src/tubescribe/repository.py` | SQLite schema/migrations, indexed jobs, stage records, scoped selection, and fenced state updates. Inference workers use these methods rather than executing SQL. `Store` coordinates archive content and recovery. |
| `src/tubescribe/blobs.py` | Storage keys and SHA-256 references, durable writes, verified bundle publication. Local filesystem and in-memory adapters exercise the same interface. |
| `src/tubescribe/submission.py` | Durable, deduplicated requests shared by CLI and dashboard. CLI executes inline; dashboard dispatches a local process with the saved submission ID. |
| `src/tubescribe/execution.py` | Atomic archive/job claims, ownership expiry, heartbeat renewal, and conditional state commits. |
| `src/tubescribe/inference.py` | AgentBridge transport, callback lifecycle/authentication, timeouts, endpoint policy, and hardware policy. Domain screenshot/transcript tools supply functions rather than HTTP handling. |
| `src/tubescribe/publication.py` | Logical transcript/figure references resolved into dashboard URLs or portable export links. |

SQLite gains `claims` and `submissions` tables automatically. Claims last **90 seconds**
and renew every **30 seconds** during long inference calls and queue waits. A crashed
worker's claims expire; recovery resets interrupted stage state only when no live job
claim owns it. Stale owners cannot renew/release replacement claims or commit stage
state. The archive claim preserves concurrency of **one worker**; per-video claims
cover all stages and separate stage invocations. `worker.lock` is now a PID display
file, while the dashboard and backfill script read the archive claim for liveness.
Submissions survive dashboard restarts and duplicate clicks share one queued/active
request. Failed process launches are recorded; abandoned requests can be resubmitted
once their launch grace period and any ownership lease have expired.
All four worker commands handle `SIGTERM` as an orderly interruption, returning the
active stage's attempt budget, retaining checkpoints, and releasing their leases.

ASR and diarization clients accept an HTTP transport, headers, endpoint policy, and
hardware policy; the worker accepts an ASR client and diarization factory. The ASR app
factory and Nemotron engine accept the same hardware policy while retaining CUDA execution. Model stages
accept an AgentBridge runner. Defaults still require loopback HTTP and RTX 4090 CUDA.
An explicit HTTPS endpoint policy and hardware policy can support a separately configured
runtime later. `RemoteTools` is a transport seam for a separately hosted tool executor;
the current AgentBridge server still requires local callbacks, so cloud use also needs
corresponding server support. File/media extraction remains local scratch work.
A future cloud migration must supply shared database/claims, object storage with staging,
a durable worker dispatcher, and compatible remote inference/tool adapters.

New canonical artifact Markdown keeps `transcript:JOB_ID` and `figure:IMAGE_ID` links
with a `references.json` map. The dashboard resolves these at delivery time; downloaded
Markdown has usable links. Standalone previews and `read.md` use relative archive paths.
Export resolves both new and legacy bundles, regenerates manifests only in the exported
copy, and clears copied live claims/PIDs. Completed live bundles are not rewritten.
Changing the serving hostname therefore does not require another model call.

## Verification

```bash
uv --config-file uv.toml run --locked python -m unittest discover -s tests -v
```

Tests cover duplicate indexing, completed-job skips, independent ASR/diarization/postprocessing state, speaker-word assignment and unmatched speech, inference failure with media reuse, native image tool delivery, checkpoint integrity, failed durable writes, repair without repeated inference, exclusive workers, interrupted-stage recovery, bounded attempts/backoff, force limits, absent tabs vs bot checks, explicit upgrade profiles, RTX 4090 enforcement, tunnel reconnection, offline batch suspension, downloader process cleanup, dashboard state and figure downloads, and timestamp/schema validation. The initial ASR smoke run processed three videos and reran those IDs to check completed-job skips. Final-stage integration was separately verified on an existing diarized source. Runtime quality/speed results are recorded in ignored `data/quality/`; channel history is complete only when its recorded jobs establish completion.

### Verified first run (9 October 2026)

The latest three Jordi Visser videos (`hKBjZBiw62U`, `LhuSxhCNa5g`, `BkNzS8Ia-7k`) completed with **167.8 minutes of audio in 112.7 seconds of ASR**, approximately **89.3× real time** on beast-3's RTX 4090. A rerun reported `completed: 0, failed: 0, skipped: 3`. Seven source audio clips independently checked with Whisper large-v3 on the RTX 4090 had **97.7% normalized word agreement**, which is cross-model agreement rather than human-reference accuracy. Timestamp/content inspection passed. Full results and source excerpts are in `data/quality/report.md` and `report.json`. Downloading and tunnel upload add wall-clock time.

Initial full discovery indexed **113** accessible uploads. Use `status` for the live completion count; the smoke test alone does not establish a complete channel archive.

### Verified archive diarization (9 October 2026)

All **26** previously saved videos were enriched on beast-3's RTX 4090: **22.5 hours of audio in 98.9 seconds of remote processing**, approximately **820× real time**, including decoding but excluding downloads, tunnel uploads and local formatting. Original ASR segments and all **248,108** nonempty words/timestamps/confidence values were preserved; **26** unmatched words remain `UNKNOWN`. Output/checkpoint hashes and subtitle timestamps passed checks. A saved-ASR rerun returned `completed: 0, failed: 0, skipped: 26`. This verifies preservation and processing, rather than human-reference diarization accuracy. Runtime details and per-video results are in `data/diarization/archive-report.md` and `.json`.

The integrated pipeline then recovered `Sopf31BOP4U` from its retained failed-ASR audio: **60.4 minutes** transcribed in **38.9 seconds**, followed automatically by **4.7 seconds** of diarization, all formats committed, and audio removed afterward. All 10,999 ASR words were preserved; its rerun skipped the completed job. Evidence is in `data/quality/pipeline-diarization.json` and its logs. The existing Bonsai and ASR services stayed running; the diarizer retained its model and returned unused GPU buffers after requests (868 MiB process VRAM observed after this request). Service restart and independent forward stop/start were also verified. Use the dashboard/CLI for current backfill progress.

### Agent-assisted Markdown processing

The shared [postprocessor](src/tubescribe/postprocessing.py) sends a completed,
diarized SRT through caller-owned transcript and video tools to AgentBridge in one request,
using `codex/gpt-6.1-sol` with high reasoning. It requires AgentBridge's experimental
`POST /api/v1/agent/runs` endpoint, Codex CLI 0.159.2's native dynamic-tool protocol,
FFmpeg/FFprobe when inspecting a cached video. The normal archive worker invokes it after
diarization. The dedicated `postprocess` command never starts ASR or diarization and
never rewrites raw transcripts. The [standalone pilot entrypoint](scripts/postprocess-pilot.py)
uses the same implementation for comparisons outside the archive queue.

AgentBridge selection is `--bridge`, then `TUBESCRIBE_AGENTBRIDGE`, then the optional
`bridge` field in `data/postprocessing.json`, then `http://127.0.0.1:8082`. Use a loopback
HTTP base URL for a build exposing `/api/v1/agent/runs`; an older installation without
that endpoint pauses this stage. Connection errors and server failures pause the batch
without exhausting every video's attempts. Neither worker automatically starts AgentBridge.
The dedicated `postprocess` command stops on YouTube rate limits even with `--wait`;
the retry remains queued with a 30-minute cooldown.

Video mode reuses `data/videos/VIDEO_ID.mp4` (also `.webm`/`.mkv`) or downloads a stream
at up to 720p with one concurrent fragment. The archive's cached video is deleted only
after the processed bundle and its manifest are durably committed. Failed/interrupted runs
keep the video for retries; saved figures, transcripts, checkpoints, and the YouTube URL
remain. Cleanup failures leave completion intact and retry during worker recovery after
validating the bundle. Forced video-mode reprocessing downloads the source again when needed.
`--video-cache /path/to/cache` or
`--video-cache ssh://HOST/absolute/cache/directory` selects a read-only existing cache;
remote entries are `VIDEO_ID.mp4`, and only frames are transferred. Caller-owned caches
outside the archive are never deleted. Missing custom-cache
entries fail rather than silently downloading elsewhere. Use `work --postprocessing text`
or `postprocess --mode text` when video inspection is unnecessary; no figures are invented.

The `postprocessings` SQLite table tracks download/model/save attempts independently,
backoff, source hashes, output location and a hashed artifact manifest. Each model run
has a fresh `transcripts/CHANNEL/VIDEO/PROFILE/postprocessing/RUN_ID/` directory; previous
outputs are preserved. Files and directories are fsynced before the final SQLite commit.
A persisted response is bound to original source hashes and instructions in
`run-context.json`; packaging/storage retries validate and reuse it without video access,
service readiness, or another model call. Corrupt response checkpoints are rejected.
Invalid new model outputs are retried with a fresh response within the model attempt bound.
An interruption before a response checkpoint exists requires a new agent turn; native
agent turns are not resumed. Changed source content queues a new run. Missing/corrupt
derived bundles can be rebuilt from valid response/frame checkpoints. `--force` explicitly
regenerates completed results, including when changing between text and video modes.
With `--force --limit N`, only the N attempted videos are reset; other completed results
keep their completion pointers. A successful final model attempt can still retry packaging
within its separate save budget.

The dashboard keeps Channel → Videos and one artifact action per row. Completed
pipelines open the summary, which links to the illustrated version, with downloads for Markdown, figures ZIP,
and the original SRT. View SRT opens a simple cue-by-cue reader; clicking a start timestamp
opens the original YouTube video at that second. The reader also links to YouTube and the
SRT download. Earlier raw transcripts remain readable while this stage is pending, with
the same View SRT action. The original YouTube URL stays in the video record and transcript
JSON even after local media cleanup.
The generated preview escapes source text and executes no scripts. Archive export includes
processed outputs and their checkpoints under `transcripts/`; it does not copy the video cache.

Archive integration was verified on the full 22-minute `Domk77rYJhU` transcript: one native
agent run took 188 seconds and produced six inspected figures plus an evidence-backed
Jordi Visser speaker label. The normal worker rerun skipped it; all 145 existing raw
transcript files retained their hashes. Evidence is in `data/quality/pipeline-integration.log`,
`pipeline-integration-skip.log`, and `pipeline-evaluation/`.

```bash
uv run --frozen python scripts/postprocess-pilot.py \
  --bridge http://127.0.0.1:PORT \
  --transcript data/transcripts/CHANNEL_ID/VIDEO_ID/PROFILE/transcript.json \
  --video /absolute/path/to/video.mp4 \
  --output data/quality/postprocessing-pilot/VIDEO_ID/run-01
```

Input may be a transcript JSON with an adjacent SRT, or a standalone SRT.
`--title` and `--source-url` override optional source metadata. Omit `--video` for
transcript-only processing: no figures are invented, and visual references remain
unresolved. Cached media supplied as video must contain a video stream, cover the transcript
duration, and have a decodable end; finish downloads/copies before processing.
`--video ssh://HOST/absolute/cache/path.mp4` inspects a caller-selected remote cache
using batch-mode SSH and remote FFmpeg/FFprobe; only captured frames are transferred.
It does not download or copy the full video. The host must already be accessible without
interactive authentication, and both executables must be installed there.
The script starts a transient authenticated loopback callback bound to that source.
Codex receives the entire transcript by default as lossless compact cue records:
speaker changes, cue IDs, start/end seconds, and JSON-escaped original text. This removes
repeated SRT formatting without summarizing speech. `--input-format srt` supplies verbose
normalized SRT instead for comparisons. Numeric body lines, multiline text, BOM/CRLF
inputs, and metadata-provided names are supported; invalid cue times are rejected.
Experimental
`--defer-transcript` instead starts with a small timestamped visual-reference index
built from excerpts, including introduction and other-voice cues. The index is
incomplete; `get_full_transcript` delivers the complete selected format once, preferably after
visual inspection and always before final writing. The caller rejects a deferred
result that never loaded it. No extra model request builds the index. A full-video
repeat on the 22-minute tutorial reduced uncached input 9.5%, but increased total
tokens 19.4% and agent time 6.4% versus immediate compact input; its cleanup also
removed more wording. Delayed loading remains opt-in.

`video_screenshots` accepts up to eight timestamps per call. Preview returns one
numbered contact sheet with 320×180 tiles; full requests return individual detail images. Identical
views and numeric-equivalent crops are delivered once per run. Broad preview crops
covering at least 65% of a frame reuse the whole-frame preview. Conservative
low-resolution similarity suppresses redundant preview tiles across timestamps,
comparing the entire frame without assuming where presenter overlays are placed.
Similarity is compared within equivalent crop regions. This does not
verify small chart-value changes: a requested full view still inspects that
specific original timestamp. Crops affect inspection only; IDs export whole PNGs.
An encoding failure does not mark previews as seen, so a retry still returns the image;
captured frames from failed calls cannot be used as figures or identity evidence until issued.
The agent batches selected detail views and starts speech-only videos with a representative
preview batch, stopping when additional talking-head frames cannot supply identity evidence.
`--legacy-visual-planning` retains the earlier batch guidance for comparisons. The smaller
preview size showed mixed results; total usage also depends on how many follow-up views
the model requests, so it is not a guaranteed token reduction.

`video_search` scans one or two short windows (5–20 seconds on either side), samples
locally every two seconds, and returns up to four visually distinct candidates per
window. It does not identify charts semantically; Codex must inspect the candidates.
Returned candidates share the remaining capture budget, including when it is nearly exhausted.
Budgets scale with source duration, capped at 30 tool calls, 120 captured timestamps,
24 full detail views, and 252 local search samples. Charts, diagrams, and software
demonstration screens can all be selected. The figure target is roughly one per
four minutes (at most 20), a ceiling rather than a quota: zero figures is valid
for interviews or other speech-only material. Transcript-only mode allows two tool
calls and no images. Each request permits 30 minutes. The output is the complete
cleaned transcript with relevant figures, source links when available (plain
timestamps otherwise), and every existing missing-speech annotation.

Numeric speaker IDs are preserved; named labels declared in source metadata are normalized
in memory and supplied as existing identity provenance, even when appearing only once.
For a standalone named SRT without metadata, repeat `--speaker-label 'Alice'` for each
known speaker prefix. Capitalization and repetition alone cannot distinguish a name
from a heading such as `Key Point:`; ambiguous prefixes remain intact as speech under
`UNKNOWN`. Unlabelled speech remains `UNKNOWN`,
which may include different voices and is never named as one person.
Speaker identification uses self-introductions, other speakers' introductions,
and inspected presenter/name cards. It records confirmed, probable, or unknown
identities with cue/frame evidence. Only confirmed names replace Markdown labels,
retaining the original ID, for example `Jordi Visser (Speaker 1)`. Probable names
remain suggestions in `speaker_names.json`; unidentified or device voices retain
their numbered labels. Names of people mentioned in discussion, channel ownership,
or face resemblance alone do not establish a speaker's identity. Original SRTs
and diarization labels are unchanged.

The script stores `request.json` without the callback token, `response.json`,
`assets.json`, `tool-calls.json`, `searches.json` when used, and captured `figures/`.
Successful runs produce `processed.md`, escaped `preview.html`, `report.json`
(tokens, caller elapsed time, agent time excluding queueing, image deduplication,
search samples, and the full-SRT tool
position), `speaker_names.json`, `resources.json`, and `processed-with-figures.zip`.
The package includes selected figures and any extra frames cited as identity
evidence, so name assignments remain reviewable. Before publishing, it validates issued image IDs,
declared contiguous source-cue coverage, ordered Markdown headings, and all supplied
missing-speech/inaudibility annotations with their original speaker attribution;
invented missing-speech annotations and reassigned existing names are rejected.
It also rejects external image embeds, changed speaker-turn order, and gross summaries
below 70% of source word count for sources of at least 150 words. This deliberately loose
threshold detects obvious omissions; it does not establish semantic fidelity. Competing
proper-name spellings and source contradictions remain unresolved unless explicit source
evidence establishes a correction. Declared cue coverage is a structural check; human
review is still needed to establish semantic fidelity and chart relevance. The script supports
different transcript lengths, speaker counts, and
video layouts. The standalone entrypoint uses a fresh output directory for every request;
`--resume` can rebuild a saved response from its run context. Interrupted runs retain
captured frames but do not resume the agent turn.

[The offline evaluator](scripts/postprocess-evaluate.py) rechecks completed runs against
the original source hashes, actual response and Markdown, speaker turns, annotations,
issued image IDs, resource PNG hashes, and every packaged file. It produces an HTML
index and `evaluation.json` without calling a model or restarting services. Optional
`--notes /path/to/review-notes.md` includes human review notes verbatim. Failed runs
remain in the report and cause a nonzero exit. Repeated inputs are deduplicated in the
duration total. Comparisons require reports containing original SRT hashes and durations;
use identical source scopes and fresh run directories for each model trial.

```bash
uv run --frozen python scripts/postprocess-evaluate.py \
  --runs data/quality/postprocessing-pilot/VIDEO_ID/run-01 \
         data/quality/postprocessing-pilot/VIDEO_ID/run-02 \
  --compare data/quality/postprocessing-pilot/VIDEO_ID/run-01 \
            data/quality/postprocessing-pilot/VIDEO_ID/run-02 \
  --output data/quality/postprocessing-pilot/evaluation
```

The expanded local evaluation ran 15 new model trials across nine videos: five complete
sources and four excerpts, totaling 301 minutes of distinct supplied content. All nine
selected results passed the structural/resource audit; all 29 archived SRTs also passed
lossless compact round trips. The set covers long monologues, an interview, a software
tutorial, chart presentations, text-only input, remote video frames, unknown speakers,
and missing-ASR annotations. Review inspected 44 selected figures and spot-checked
numeric details and transcript passages. Most videos share one English-speaking presenter;
this does not establish performance for other languages or complex multi-party speech.

Two same-source baseline/final pairs (tutorial and interview) used 31% fewer combined
total tokens, 7% fewer uncached input tokens, and 18% less combined agent time. A repeat
of the physical-AI excerpt regressed 18% in total tokens and 2% in agent time. These
single paired runs include multiple changes and model variance. Agent time excludes
AgentBridge queueing, while caller time includes queueing and transport; cached tokens
are included in total usage, so token reductions are not equivalent to billing savings.
Artifacts, comparisons including regressions, and review notes are under ignored
`data/quality/postprocessing-expanded/`; rerun the evaluator to refresh its index.

### Speaker diarization experiment

An isolated [diarization trial](experiments/diarization/README.md) tested NVIDIA Nemotron 3 on beast-3's RTX 4090 against a Jordi/Marty interview and a Jordi monologue. It downloaded without authentication and detected two speakers and one speaker respectively. The ten-minute interview took 1.03 seconds for diarization after loading, with 97.9% agreement on speech covered by approximate publisher labels; this is not general accuracy. Speaker-labelled SRT and detailed evidence are under ignored `data/diarization/`. The unrun Pyannote Community-1 comparison was retired on 2026-10-10 because no compatible patched Torch/audio stack was available; its source and dependency snapshot remain recoverable from Git history. Nemotron trials now use the patched root lock. The production pipeline now uses Nemotron for speaker labels; person names in the original trial were reference-guided.
