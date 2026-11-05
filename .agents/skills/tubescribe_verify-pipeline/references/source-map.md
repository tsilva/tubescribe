# Pipeline verification source map

Resolve these paths relative to the TubeScribe repository root. Read the relevant files instead of freezing their current values into the skill.

| Concern | Maintained source |
| --- | --- |
| Supported setup, credentials and manual admission | `docs/guide.md`, section “Owner-selected processing on the Mac” |
| Current and legacy hosted profiles | `src/tubescribe/config.py` |
| Model, upload transport, chunk size, pricing and paid-request checkpoints | `src/tubescribe/openrouter.py` |
| Acoustic references, matching thresholds and unresolved voices | `src/tubescribe/speaker_continuity.py` |
| Manual admission, legacy retries and per-profile queue status | `src/tubescribe/manual_queue.py` |
| Worker execution and stage dependencies | `src/tubescribe/worker.py`, `src/tubescribe/pipeline.py` |
| ASR validation and rendered subtitles | `src/tubescribe/asr.py`, `src/tubescribe/subtitles.py` |
| Saved enrichment, summaries and recovery | `src/tubescribe/postprocessing_worker.py`, `src/tubescribe/artifacts.py`, `src/tubescribe/submission.py` |
| Reader details and current processing settings | `src/tubescribe/pipeline_details.py`, `src/tubescribe/dashboard.py`, `src/tubescribe/dashboard_assets/app.js` |
| Release procedure and CI | `docs/production-delivery.md`, `.github/workflows/production-delivery.yml`, `.github/workflows/ci.yml`, `scripts/verify` |

## Proportional local checks

For focused changes, the manual-processing, speaker-continuity and automatic-recovery Python tests cover useful invariants; the reader tests cover controls, profile selection and cost estimates. The complete maintained local gate is `scripts/verify`. Use existing tests before inventing live faults or additional paid samples. Do not repeatedly run the entire suite after it passes unless a subsequent change or failure justifies it.

The transcript helper reads a **materialized TubeScribe ASR transcript**, not a raw provider response or processed Markdown. Obtain it with the configured archive client or an authorized download. It delegates base validation to the project's `asr.validate` and additionally checks word-level structure and selected continuity metadata. It prints metadata only, never transcript text. It does not verify publication, billing completeness, audio-to-word alignment, or speaker identity; those require their own evidence. Run it with the repository's configured Python environment.

## Live resources

Use the selected video ID and actual profile, rather than an old example:

- `/videos/VIDEO_ID/transcripts/PROFILE/` — processed transcript, falling back to raw ASR before enrichment finishes. Check which one is being served.
- `/videos/VIDEO_ID/transcripts/PROFILE/artifacts/summary/` — the saved summary.
- `/videos/VIDEO_ID/transcripts/PROFILE/subtitles/` — granular source-time links.
- `/api/videos/VIDEO_ID/pipeline?profile=PROFILE` — stage and recorded-usage details under the library's normal access controls.
- `/auth/config` — public client configuration, including current processing defaults when enabled. Inspect only relevant non-secret fields.

Do not invent credentials or use an unrelated archive if a resource is inaccessible. When evidence includes private request IDs, worker state, screenshots or operator paths, keep it in ignored local reports, not public contributor documentation.

## Delayed workflow status

Inspect the exact SHA's run, job/step conclusions and current deployment when summaries disagree. GitHub can report delayed or inconsistent states; a queued display does not by itself prove lack of runner capacity. Check pending deployment protections before treating `waiting` as a runner problem.

Use bounded polling and preserve the current run while diagnosing it. Do not automatically cancel a waiting production job, force-cancel runs, dispatch duplicate CI, skip required checks or change concurrency/workflow definitions. Cancellation or re-dispatch may be appropriate inside an authorized release, but first verify its state and side effects and whether a replacement would actually satisfy the gate. Read the **currently pinned** trusted delivery implementation to learn which CI event and SHA it accepts; a manual diagnostic CI run may not qualify as the required push run.

If cancellation is accepted but the run stays active, or force cancellation returns 409 while GET still says active, reconcile state before another mutation. Persistent stuck runs or concurrency holds may require GitHub Support. Prepare the complete draft and obtain the user's required approval before contacting anyone; do not send a ticket as part of diagnosis automatically. Stop owned local observers when done, and report any unresolved external state accurately.
