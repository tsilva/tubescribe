# Speaker diarization trial

This experiment tests `pyannote/speaker-diarization-community-1` on beast-3’s RTX 4090. It does not enable diarization in the archive worker. The isolated project and committed lock preserve the seven-day age cutoff and bad-package constraints. PyTorch/torchaudio 2.8 and TorchCodec 0.7 are paired deliberately. Beast-3 has FFmpeg 8; this experiment supplies predecoded mono 16 kHz PCM16 WAV in memory, avoiding TorchCodec’s FFmpeg 4–7 decoder limitation. A decoder warning is expected at import; no model runs on CPU.

Accept the model’s [access conditions](https://huggingface.co/pyannote/speaker-diarization-community-1) and configure a read token using `hf auth login` on beast-3. Tokens remain in the normal Hugging Face credential store, outside this repository. Do not put a token in commands, transcripts, or logs.

The prepared remote environment is `~/.local/share/tubescribe/diarization-trial`. Install/reproduce with source/config files transferred there:

```bash
cd ~/.local/share/tubescribe/diarization-trial
~/.local/bin/uv --config-file uv.toml sync --locked --python 3.12 --no-dev
CUDA_VISIBLE_DEVICES=0 .venv/bin/python run.py interview.wav prediction.json
```

Copy `prediction.json` back into local `data/diarization/`, which also contains the source WAV, existing Whisper ASR, and approximate publisher turn labels. Then run from the repository root:

```bash
uv --config-file uv.toml run --locked python experiments/diarization/review.py data/diarization
```

The runner pins the model revision, verifies CUDA/RTX 4090 and at least 2 GiB free VRAM, runs automatic speaker count and a known-two-speakers comparison sequentially, and records model/version/device, elapsed time, memory allocation and both regular/exclusive diarization. The process exits afterward; other models remain running.

The test source is the first ten minutes of [Jordi Visser’s TFTC interview with Marty Bent](https://www.youtube.com/watch?v=VkDpGZ_KsrY), outside the indexed Jordi channel. The [publisher’s transcript](https://www.tftc.io/jordi-visser-900-trillion-tokenization-ai-agents-bitcoin-transcript) supplies speaker names and approximate turn times. It is machine transcription with light cleanup, not a human ground truth annotation. Model names are mapped using only the first long turn for each voice. Evaluation uses later turns, excludes one second at each boundary and excludes same-second/very short interjections. Agreement is measured only where predicted speech covers reference intervals; coverage is reported separately. This is not diarization error rate or a claim of general accuracy. Short interjections/overlap need separate review.

Review writes `evaluation.json` plus `automatic.*` and `known_two_speakers.*` JSON, text and SRT files under ignored `data/diarization/`. Each word is assigned by temporal overlap, then cues split on speaker changes. Unmatched words remain `UNKNOWN`. Names are reference-guided for this recording; no person identification or cross-video voice recognition is inferred. Original archive transcripts remain unchanged.
