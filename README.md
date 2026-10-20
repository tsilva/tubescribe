<p align="center">
  <img src="logo.png" alt="TubeScribe — a video reel becoming saved transcript pages" width="240">
  <br>
  <!-- repo-tagline:start -->
  <strong>📽️ Turn YouTube videos into a reading library 📚</strong>
  <!-- repo-tagline:end -->
</p>

<p align="center"><a href="https://tubescribe.tsilva.eu/">Open the library</a></p>

TubeScribe is for people who follow YouTube channels and want to revisit their ideas in writing. It collects videos into a chronological library with one-page summaries, full transcripts, and timestamped subtitles. Read the [hosted library](https://tubescribe.tsilva.eu/) immediately, or run your own archive with the CLI and local processing services.

## Try it

1. Open [tubescribe.tsilva.eu](https://tubescribe.tsilva.eu/) and sign in if prompted. Browse all channels, select a channel, or search by channel name or video title.
2. The newest video is selected automatically. Select any video to open its saved summary in the reading pane; switch to **Transcript** for the full text or **SRT timestamps** to follow the source video.
3. Choose **7-day summary** for a recent overview, or use **Open full page** and **Copy link** to keep a particular document.

A useful first result is a readable summary with links to the original video and any saved figures. Videos still awaiting processing show their current state; raw transcripts can be read before the summary is ready. Browsing only reads saved content and never starts processing.

## Keep the parts worth revisiting

- **Read at your own pace.** Move between concise summaries, speaker-labelled transcripts, and subtitles without leaving the reader.
- **Return to the source.** Timestamp links open the original YouTube moment. Inspected screenshots preserve relevant charts, slides, and demonstrations when available.
- **Catch up across channels.** Rolling seven-day overviews cover a single channel or the whole library. The project-local [newsletter skill](.agents/skills/tubescribe-newsletter/SKILL.md) turns saved summaries into a digest grouped by channel.
- **Keep portable copies.** Download transcript formats, Markdown with figures, or verified archive backups. Failed processing resumes from saved stages rather than repeating completed work.

Transcription and generated summaries can contain mistakes. Speaker numbers are local to each video; a person’s name is used only when supported by recorded evidence. Check the source before relying on a quote or figure.

## Run your own library

Requires Python **3.11–3.13**, `uv`, Node.js **22+**, `ffmpeg`/`ffprobe`, and OpenSSH. From a checkout:

```bash
uv --config-file uv.toml sync --locked
scripts/tubescribe --help
scripts/tubescribe dashboard serve --port auto
```

Open the URL printed by the last command. A fresh installation starts with an empty library. Follow the [installation and inference setup](docs/guide.md#install), then [add channels and process videos](docs/guide.md#archive-commands) to populate it. Full processing uses separate Whisper and Nemotron services on an RTX 4090 plus AgentBridge/Codex; installing the reader alone does not start those services. The dependency lock and seven-day package age cutoff are retained.

## Further reading

The [operations guide](docs/guide.md) contains the complete reference:

- [Dashboard controls](docs/guide.md#library-dashboard) and [stable source URLs](docs/guide.md#resource-urls-and-notebooklm-sources).
- [Recent-video catch-up](docs/guide.md#rolling-recent-video-catch-up), [artifact workflows](docs/guide.md#artifact-workflows), and [backup and restore](docs/guide.md#background-import-and-backup).
- [Local and network architecture](docs/guide.md#local-runtime-and-network-adapters), [Vercel hosting](docs/guide.md#vercel-hosting-with-local-processing), and [configuration](docs/guide.md#configuration-access-and-inference-roles).
- [Verification and schema upgrades](docs/guide.md#schema-upgrades-and-automated-verification), plus the [branding asset inventory](image-assets/manifest.json).

For development, run `scripts/verify` for lint and local tests. The [container rehearsal](docs/guide.md#local-postgresql-and-s3-rehearsal) checks CLI readiness and metrics alongside HTTP delivery, outages, recovery, and restore.
