# Speaker diarization trial

This experiment tests speaker tagging on beast-3’s RTX 4090. NVIDIA Nemotron 3 completed the interview and monologue control; pyannote Community-1 remains an optional gated comparison. It does not enable diarization in the archive worker. The isolated project and committed lock preserve the seven-day age cutoff and bad-package constraints. Both runners verify CUDA/RTX 4090 and at least 2 GiB free VRAM, reuse their loaded model across sequential runs, record provenance and timings, and exit afterward.

The prepared remote environment is `~/.local/share/tubescribe/diarization-trial`. Transfer this directory’s source files and the repository’s `uv.toml` there. Supply mono 16 kHz PCM16 WAV files, including an optional single-speaker control.

## Nemotron 3: tested without authentication

[NVIDIA Nemotron 3 Diarization](https://huggingface.co/nvidia/Nemotron-3-Diarization) runs through the released Transformers 5.18.0 package in the optional `nemotron` extra. The runner pins revision `f667ed73aee57d40cc39428eb768b4fd87a0a29e` and explicitly downloads without a token. No Git dependency or package-age exception is needed.

```bash
cd ~/.local/share/tubescribe/diarization-trial
~/.local/bin/uv --config-file uv.toml sync --locked --extra nemotron --python 3.12 --no-dev
CUDA_VISIBLE_DEVICES=0 .venv/bin/python run_nemotron.py interview.wav prediction.json --control monologue.wav
```

The runner records overlapping speaker activity and a derived exclusive timeline, choosing the highest activity probability during overlap for word assignment. Raw overlapping turns remain available. It does not require a supplied speaker count.

On 2026-10-09, the ten-minute interview produced two speakers in 1.025 seconds after model loading, with 461 MiB peak PyTorch allocation. The 30-second Jordi monologue produced one speaker in 0.043 seconds. Later reference intervals showed 97.9% label agreement over 412.34 seconds of predicted speech, covering 84.7% of the 487 seconds of reference intervals. Reference intervals include pauses. These are sample results, not general accuracy or a comparison against pyannote. The local evidence is under ignored `data/diarization/`, including `prediction.json`, `evaluation.json`, `report.md`, and `nemotron_automatic.srt`.

## Pyannote Community-1: optional comparison

Accept the model’s [access conditions](https://huggingface.co/pyannote/speaker-diarization-community-1) and configure a read token using `hf auth login` on beast-3. Tokens remain in the normal Hugging Face credential store, outside this repository. Do not put a token in commands, transcripts, or logs. This comparison has not run because model access was unavailable.

```bash
cd ~/.local/share/tubescribe/diarization-trial
~/.local/bin/uv --config-file uv.toml sync --locked --python 3.12 --no-dev
CUDA_VISIBLE_DEVICES=0 .venv/bin/python run.py interview.wav prediction.json
```

PyTorch/torchaudio 2.8 and TorchCodec 0.7 are paired deliberately. Beast-3 has FFmpeg 8; this runner supplies the predecoded WAV in memory, avoiding TorchCodec’s FFmpeg 4–7 decoder limitation. A decoder warning is expected at import. The runner pins the model revision and records automatic speaker count and a known-two-speakers comparison, including regular/exclusive diarization.

## Review and subtitle export

Copy the selected runner’s `prediction.json` back into local `data/diarization/`, which also contains `asr.json` from the existing Whisper service and `reference-turns.json` with approximate publisher turn labels. Then run from the repository root:

```bash
uv --config-file uv.toml run --locked python experiments/diarization/review.py data/diarization
```

The test source is the first ten minutes of [Jordi Visser’s TFTC interview with Marty Bent](https://www.youtube.com/watch?v=VkDpGZ_KsrY), outside the indexed Jordi channel. The [publisher’s transcript](https://www.tftc.io/jordi-visser-900-trillion-tokenization-ai-agents-bitcoin-transcript) supplies speaker names and approximate turn times. It is machine transcription with light cleanup, not a human ground truth annotation. Model names are mapped using only the first long turn for each voice. Evaluation uses later turns, excludes one second at each boundary and excludes same-second/very short interjections. Agreement is measured only where predicted speech covers reference intervals; coverage is reported separately. This is not diarization error rate or a claim of general accuracy. Short interjections/overlap need separate review.

Review writes `evaluation.json` plus `<run-label>.json`, `.txt`, and `.srt` under ignored `data/diarization/`. Nemotron’s label is `nemotron_automatic`; pyannote’s labels are `automatic` and `known_two_speakers`. Each word is assigned by temporal overlap, then cues split on speaker changes. Unmatched words remain `UNKNOWN`. Names are reference-guided for this recording; no person identification or cross-video voice recognition is inferred. Original archive transcripts remain unchanged.
