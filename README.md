<p align="center">
  <img src="logo.png" alt="TubeScribe — a video reel becoming saved transcript pages" width="240">
  <br>
  <!-- repo-tagline:start -->
  <strong>📽️ Turn YouTube videos into a reading library 📚</strong>
  <!-- repo-tagline:end -->
</p>

<p align="center"><a href="https://tubescribe.tsilva.eu/">Open the library</a></p>

TubeScribe is for people who follow YouTube channels and want to revisit their ideas in writing. It collects videos into a chronological library with one-page summaries, full transcripts, and timestamped subtitles. Sign in to the [hosted library](https://tubescribe.tsilva.eu/), or run your own archive with the CLI and local processing services.

## Try it

1. Open [tubescribe.tsilva.eu](https://tubescribe.tsilva.eu/) and sign in if prompted. Choose a channel in the desktop sidebar, or use the channel dropdown on smaller screens. Search by channel name or video title.
2. The newest video is selected automatically. Select any video to open its saved summary in the reading pane; switch to **Transcript** for the full text, **SRT timestamps** to follow the source video, or **Pipeline** for processing progress, step descriptions, recorded costs, timings, and usage.
3. Choose **7-day summary** for a recent overview, or use **Open full page** and **Copy link** to keep a particular document.

Libraries are private to each account. The existing archive belongs to its owner; new accounts see **No videos yet**. Local processors continue publishing to the owner’s library.

With [owner-selected cloud processing](docs/guide.md#owner-selected-cloud-processing) enabled, followed channels are checked automatically. Choose **Process** on a video, or open **Discovered** to queue selected uploads. After a failure or interruption, **Process** becomes available again to retry from saved progress. Video rows show status pills. Each queued or processing video also shows a spinner in its button, while other **Process** buttons remain available. Vercel Workflows coordinate the stages; Apify retrieves selected media; temporary Sandboxes prepare audio and requested video frames. Bounded Functions call MAI-Transcribe 2 through OpenRouter, retain speaker references across chunks, and publish word-timed transcripts. OpenRouter also runs enrichment, image inspection and summaries with GPT-6.1 Sol. Discovery never selects videos for processing, and processing continues with the browser closed. Saved responses survive retries; ambiguous paid outcomes stop for review. The cloud backend has no Mac or Beast-3 dependency. The [Mac worker](docs/guide.md#owner-selected-processing-on-the-mac) remains an optional self-hosted backend.

New cloud downloads prefer 360p video to reduce acquisition costs while retaining audio and frames for visual enrichment. Fine text in charts and slides may be less legible at this resolution.

A useful first result is a readable summary with links to the original video and any saved figures. Every discovered video has a YouTube thumbnail before processing, including videos already in the library. Videos still awaiting processing show their current state; raw transcripts can be read before the summary is ready. Browsing only reads saved content and never starts processing.

The reader refreshes after sign-in, when you return to its tab, or when you choose **Refresh**. Library status and **Refresh** appear only while signed in. Pipeline shows saved OpenRouter model charges, including tool-follow-up calls and retries, and preserves the selected tab, reading position, and loaded content during updates. The hosted library receives live changes without background polling. Local PostgreSQL processors wake on database notifications, with timers only for retries and expiring work or summary windows.

## Keep the parts worth revisiting

- **Read at your own pace.** Move between concise summaries, speaker-labelled transcripts, and subtitles with consistent content margins, without leaving the reader. Summaries omit the transcript footer link and generated-content disclaimer; use the **Transcript** tab to read the source.
- **Return to the source.** Timestamp links open the original YouTube moment. Inspected screenshots preserve relevant charts, slides, and demonstrations when available. Inline images stay within the reading pane and preserve their aspect ratio.
- **Catch up across channels.** Rolling seven-day overviews cover a single channel or the whole library. The project-local [newsletter skill](.agents/skills/tubescribe_newsletter/SKILL.md) turns saved summaries into a digest grouped by channel.
- **Keep portable copies.** Download transcript formats, Markdown with figures, or verified archive backups. Failed processing resumes from saved stages rather than repeating completed work.

Transcription and generated summaries can contain mistakes. Speaker numbers are local to each video; a person’s name is used only when supported by recorded evidence. Check the source before relying on a quote or figure.

## Run your own library

Requires Python **3.11–3.13**, `uv`, Node.js **22+**, `ffmpeg`/`ffprobe`, and OpenSSH. From a checkout:

```bash
uv --config-file uv.toml sync --locked
scripts/tubescribe --help
scripts/tubescribe dashboard serve --port auto
```

Open the URL printed by the last command. A fresh installation starts with an empty library. Follow the [installation guide](docs/guide.md#install), then [add channels](docs/guide.md#archive-commands). The [cloud setup](docs/guide.md#owner-selected-cloud-processing) and [Mac setup](docs/guide.md#owner-selected-processing-on-the-mac) support manual selections and OpenRouter inference. The [local GPU setup](docs/guide.md#configuration-access-and-inference-roles) uses separate Whisper and Nemotron services and retains its explicit AgentBridge/Codex configuration.

## Further reading

The [operations guide](docs/guide.md) contains the complete reference:

- [Dashboard controls](docs/guide.md#library-dashboard) and [stable source URLs](docs/guide.md#resource-urls-and-notebooklm-sources).
- [Recent-video catch-up](docs/guide.md#rolling-recent-video-catch-up), [artifact workflows](docs/guide.md#artifact-workflows), and [backup and restore](docs/guide.md#background-import-and-backup).
- [Local and network architecture](docs/guide.md#local-runtime-and-network-adapters), [Vercel hosting](docs/guide.md#vercel-hosting-with-local-processing), and [configuration](docs/guide.md#configuration-access-and-inference-roles).
- [Verification and schema upgrades](docs/guide.md#schema-upgrades-and-automated-verification), plus the [branding asset inventory](image-assets/manifest.json).
- [Production delivery](docs/production-delivery.md) — complete CI, public-origin sync and exact-commit deployment. Existing backend credential isolation is documented in the [hosting guide](docs/guide.md#vercel-hosting-with-local-processing).

For development, run `scripts/verify` for lint and local tests. The [container rehearsal](docs/guide.md#local-postgresql-and-s3-rehearsal) checks CLI readiness and metrics alongside HTTP delivery, outages, recovery, and restore.

For recurring end-to-end checks in Codex, use the project-local [pipeline verification skill](.agents/skills/tubescribe_verify-pipeline/SKILL.md) to inspect selected videos through the live transcript and summary, including timing, speaker continuity and saved-result recovery.

The isolated [transcription and speaker-attribution benchmark](experiments/transcription_benchmark/README.md) compares the deployed pipeline with alternative models using human-annotated audio, without replacing running services or archive transcripts.

For Neon, [transfer monitoring](docs/guide.md#neon-transfer-monitoring) records result bytes by operation and checks provider usage against 3 GB and 4 GB warning thresholds.

Once configured, the hosted reader receives catalogue change notifications through [Neon Realtime](docs/guide.md#neon-realtime-reader). It loads new videos and status changes automatically through the versioned feed API. An initial snapshot keeps saved channels and videos visible when the live connection is unavailable.

## License

[MIT License](LICENSE).

Production delivery uses the reviewed shared workflow in `tsilva/.github`, pinned to a full commit SHA. A standalone signed Infisical relay triggers sync and deployment; see [production delivery](docs/production-delivery.md) for the trust boundary and recovery.
