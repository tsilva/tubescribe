# Speaker diarization trial

NVIDIA Nemotron 3 completed the interview and monologue control on beast-3's RTX 4090. Production uses a separate resident Nemotron service; see the [main README](../../README.md#remote-speaker-diarization). The Nemotron runner and review tools remain available and use the repository's patched dependency lock. They verify CUDA/RTX 4090 and at least 2 GiB free VRAM, record provenance and timings, and exit after the trial.

The optional Pyannote comparison is retired as of 2026-10-10. Its incompatible, vulnerable Torch/audio environment is no longer an installable project in this checkout. Historical source, manifests and the lock are recoverable from [revision 795322b2ba92](https://github.com/tsilva/tubescribe/tree/795322b2ba922dce09665a89bd4afdae906d5be9/experiments/diarization). Restoration requires a separately reviewed, compatible patched dependency stack. Do not launch a previously prepared trial environment.

## Nemotron 3: tested without authentication

[NVIDIA Nemotron 3 Diarization](https://huggingface.co/nvidia/Nemotron-3-Diarization) runs through the released Transformers 5.18.0 package in the root project’s `diarization` extra. The runner pins revision `f667ed73aee57d40cc39428eb768b4fd87a0a29e` and explicitly downloads without a token. No Git dependency or package-age exception is needed.

```bash
cd /path/to/tubescribe
uv --config-file uv.toml sync --locked --extra diarization --python 3.12 --no-dev
CUDA_VISIBLE_DEVICES=0 .venv/bin/python experiments/diarization/run_nemotron.py interview.wav prediction.json --control monologue.wav
```

The runner records overlapping speaker activity and a derived exclusive timeline, choosing the highest activity probability during overlap for word assignment. Raw overlapping turns remain available. It does not require a supplied speaker count.

On 2026-10-09, the ten-minute interview produced two speakers in 1.025 seconds after model loading, with 461 MiB peak PyTorch allocation. The 30-second Jordi monologue produced one speaker in 0.043 seconds. Later reference intervals showed 97.9% label agreement over 412.34 seconds of predicted speech, covering 84.7% of the 487 seconds of reference intervals. Reference intervals include pauses. These are sample results, not general accuracy or a comparison against pyannote. The local evidence is under ignored `data/diarization/`, including `prediction.json`, `evaluation.json`, `report.md`, and `nemotron_automatic.srt`.

## Pyannote Community-1: retired comparison

This comparison never ran because gated model access was unavailable. The available Torch/torchaudio pair retains a Torch vulnerability and constrains setuptools below its patched release; forcing mismatched native binaries is unsupported. The historical runner remains as reference source, but its entry point exits before importing dependencies or loading a model. Recovery instructions and the original paired environment are in the revision linked above.

## Review and subtitle export

Copy the selected runner’s `prediction.json` back into local `data/diarization/`, which also contains `asr.json` from the existing Whisper service and `reference-turns.json` with approximate publisher turn labels. Then run from the repository root:

```bash
uv --config-file uv.toml run --locked python experiments/diarization/review.py data/diarization
```

The test source is the first ten minutes of [Jordi Visser’s TFTC interview with Marty Bent](https://www.youtube.com/watch?v=VkDpGZ_KsrY), outside the indexed Jordi channel. The [publisher’s transcript](https://www.tftc.io/jordi-visser-900-trillion-tokenization-ai-agents-bitcoin-transcript) supplies speaker names and approximate turn times. It is machine transcription with light cleanup, not a human ground truth annotation. Model names are mapped using only the first long turn for each voice. Evaluation uses later turns, excludes one second at each boundary and excludes same-second/very short interjections. Agreement is measured only where predicted speech covers reference intervals; coverage is reported separately. This is not diarization error rate or a claim of general accuracy. Short interjections/overlap need separate review.

Review writes `evaluation.json` plus `<run-label>.json`, `.txt`, `.srt`, and `.md` under ignored `data/diarization/`. Nemotron’s label is `nemotron_automatic`; pyannote’s labels are `automatic` and `known_two_speakers`. Each word is assigned by temporal overlap, then cues split on speaker changes. Unmatched words remain `UNKNOWN`. Names are reference-guided for this recording; no person identification or cross-video voice recognition is inferred. Original archive transcripts remain unchanged.

Markdown merges consecutive cues from the same voice into turns with a speaker-name prefix. The known names come from the reference-guided mapping; unidentified voices keep their recorded IDs. It preserves the ASR text and omits cue timestamps for reading.

## Dependency security

The root lock uses patched Torch 2.13 and supplies the maintained Nemotron trial. The old Pyannote environment is retired, rather than upgraded through an incompatible binary pairing. Removing its dependency project does not remove source history, saved results, or model caches.
