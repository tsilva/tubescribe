---
name: tubescribe_verify-pipeline
description: Verify TubeScribe's discovery, manually selected processing, word timestamps, speaker continuity, saved-result recovery, and live transcript and summary delivery. Use for pipeline smoke tests and end-to-end release checks; distinguish inspection from authorized processing or deployment.
---

# TubeScribe Pipeline Verification

Verify the requested story from selection through the published reader. Use fresh evidence from the configured archive, worker and live site. A saved response or successful build alone does not establish end-to-end completion.

## Establish scope

- An inspection request is read-only. Do not queue videos, retry jobs, call paid models, restart services or publish a release merely to inspect them. For an authorized processing or release check, reuse the user's selected video and current authorization; clarify only missing choices that materially affect scope or cost.
- Creation or invocation of this skill is not blanket authorization for future paid calls, fault injection, releases or outbound messages. In particular, testing recovery against production requires a separately authorized, scoped experiment; existing offline recovery tests are usually sufficient.
- Read the current [operations guide](../../../docs/guide.md#owner-selected-processing-on-the-mac) and [production delivery contract](../../../docs/production-delivery.md) as relevant. Consult [the source map](references/source-map.md) when tracing behavior. Resolve current model, profile, chunk/reference budgets, launcher and price from maintained source/configuration; do not reuse historical video IDs, counts, charges or deployment IDs.
- Preserve automatic discovery with manual-only admission, the Mac worker and the project's reviewed credential flow. Use its dedicated Agent Vault launcher and placeholder checks; never retrieve keys, broaden proxy permissions or introduce a Beast-3 fallback to make a check pass.

## Check the necessary boundaries

1. **Discovery and selection.** Confirm the intended signed-in library has its followed channels and discovered uploads. Reading the library must not generate work. For an authorized smoke test, admit only the selected video through the ordinary manual queue and record its video, profile, job and submission IDs. Do not enqueue the backlog or overwrite older transcript versions.
2. **Worker and saved progress.** Confirm the selected job is claimed by the Mac worker using the expected model/profile. Inspect only relevant stage events and counters; withhold credentials and raw provider error output. Record native generation IDs, confirmed charges and unknown outcomes separately. Reuse valid saved results. Never clear `.pending` markers or resubmit an ambiguous paid request without billing/activity review and authorization covering the repeat.
3. **Timings and speakers.** Validate the saved authoritative transcript against the archive checksum and current ASR contract. Check word timing, source offsets and duration bounds, speaker references, and removal of reference speech from published content. The bundled helper performs local structural checks without network access or processing:

   ```sh
   .venv/bin/python .agents/skills/tubescribe_verify-pipeline/scripts/check_transcript.py \
     /absolute/path/to/saved/transcript.json --expect-model microsoft/mai-transcribe-2
   ```

   Substitute the currently expected model. Add `--require-continuity` only when the selected sample spans multiple chunks and includes speaker labels. JSON structure and matching counters do **not** prove recognition accuracy, timestamp precision or voice identity. To exercise continuity, use authorized known-voice audio with a returning speaker absent for a whole content chunk; verify the expected A–B–A identities, not equality of provider-local speaker numbers. Conflicting references must not merge distinct known voices. A single-speaker video is useful coverage but cannot establish multi-speaker quality.
4. **Transcript and summary publication.** Wait for transcript enrichment and summary publication, not merely ASR completion. Verify the requested profile's processed transcript, summary and subtitles are readable on the live site. Inspect meaningful text, source-time links, expected speaker turns, and the absence of a coarse-timing warning when claiming word timing. Open the main library as its owner and verify version selection and Pipeline state/model. Check preserved older versions when the change affects profile migration.
5. **Release, when requested.** Run checks appropriate to the patch; use `scripts/verify` for a substantive pipeline change. Follow the applicable publication skills, including `private-skills_push` for any authorized push. Preserve the maintained delivery workflow and its exact-commit CI requirement. Verify the intended Production deployment is READY, matches the release SHA and has the live alias assigned. Check `/auth/config` for actual processing defaults and reference-aware price estimates; rendered documents can update through shared storage while the reader still runs older code.

Use Brave with the maintained browser runtime for signed-in UI verification. Use the project's normal clients for archive reads. Do not dump environments, read credential caches or replay private one-off scripts from previous sessions.

## Report evidence and stop

Keep private reports and captures under ignored `.internal/qa/` and `.internal/evidence/`, following repository exclusion rules. Read [the source map](references/source-map.md#delayed-workflow-status) for contradictory workflow status, rather than dispatching repeated runs or changing CI gates.

Report what passed, what remains incomplete, the selected live transcript/summary links, the model/profile and any material limits. Label transcription charges separately from summary costs and include billed reference audio. Distinguish native request time from full pipeline elapsed time. For a release, include the verified commit and deployment result. Preserve an unverified or blocked boundary as such; fix within the authorized scope and recheck it before declaring success. Once the requested flow passes, stop adding paid samples, retries or broader checks.
