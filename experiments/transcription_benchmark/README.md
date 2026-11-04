# Transcription and speaker-attribution benchmark

## Pilot result: 2 November 2026

On an RTX 4090 24 GB with a Ryzen 7600X, Nemotron 3.5 Transcribe improved
transcription and speaker-attributed text accuracy on every scored excerpt,
while the deployed pipeline processed audio about 2.88 times faster.

| Metric | Deployed Whisper + diarization | Nemotron 3.5 Transcribe IQ4_NL |
| --- | ---: | ---: |
| WER, lower is better | 23.86% | 13.48% |
| Speaker-attributed cpWER, lower is better | 27.60% | 12.49% |
| DER, overlap included, 250 ms collar | 27.40% | 26.02% |
| DER, overlap excluded, 250 ms collar | 26.77% | 25.05% |
| Steady-state audio seconds per wall second | 106.33 | 36.96 |
| Wall time for 48 minutes of audio across two passes | 27.09 s | 77.92 s |
| Sampled peak total GPU usage, production resident | 3.88 GiB | 20.75 GiB |

Accuracy covers 2,330 normalized human-reference words in six three-minute AMI
test excerpts from three meetings. Timing covers those six plus two three-minute
channel excerpts, each run twice after warmup. Both systems repeated their exact
transcript text across passes. The channel excerpts have no independent human
reference and are not included in accuracy figures.

The candidate used the community
[AtomicChat IQ4_NL conversion](https://huggingface.co/AtomicChat/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-GGUF/tree/dbd06c5cad7a2f33c8e06be2fb3ef814dbd9aa59),
not BF16 weights. The official NeMo-Speech.cpp runtime was built at
`536b17a41bef62d2a67a40ed3df61df26a4389fe`, with its pinned llama.cpp submodule
and NVIDIA patches. To leave the production models loaded, the candidate used
`n_cpu_moe=12`, six CPU threads, one sequence, a 4,096-token sequence pool,
8,192 maximum context, 512-token prefill batches, one encoder window per batch,
and no speculative decoding. The native chat-completions interface returned both
word attribution and raw diarization activity for matched scoring. Minimum sampled
free GPU memory during candidate requests was 2,829 MiB.

These are measurements of that memory-preserving CPU/GPU configuration, not
all-GPU maximum throughput or the model's full-precision accuracy. The main gain
was 43.5% fewer word errors and 54.7% fewer speaker-attributed word errors. Raw
diarization improved modestly overall and regressed slightly on two excerpts;
missed speech remained nearly unchanged. The small AMI pilot does not establish
accuracy on finance or AI YouTube channels.

Production unit/source hashes, service PIDs and start times matched across the
entire comparison. Both production services remained healthy. The candidate process
was stopped and independently confirmed absent after the run. No archive processing
or publication was submitted. Raw predictions, frozen corpus, checksums, model/build
provenance, scoring output and verification are retained under ignored
`data/quality/nemotron35-benchmark-20261102/`.

## Procedure

This experiment compares the deployed Whisper large-v3-turbo plus Nemotron 3
diarization pipeline against Nemotron 3.5 Transcribe. It writes only experiment
files. It does not enqueue archive work, change service configurations, restart
production, or replace saved transcripts.

The native candidate runs as an owned temporary process on a separate loopback
listener. The runner stops that process on completion or failure. Production
service PIDs, start times, unit hashes, source hashes, and health are captured
before and after each run. Candidate execution stops if production becomes busy
or less than 1 GiB of GPU memory remains. Run only during an idle processing
window; monitoring does not reserve the GPU against other applications.

## Fixed corpus and references

`prepare.py` selects two three-minute excerpts, starting at 60 and 360 seconds,
from each of AMI's test meetings ES2004a, IS1009a, and TS3003a. The same mono
16 kHz PCM16 WAV bytes go to both systems. References are independent human
annotations from the [AMI corpus](https://groups.inf.ed.ac.uk/ami/), with
word-only RTTM speech regions and evaluation regions from the pinned
[AMI diarization setup](https://github.com/pyannote/AMI-diarization-setup/tree/67c2d539286e89f68952d5dcf83912bd9f01dfae).
AMI material is subject to the corpus's attribution license.

Two optional existing channel recordings supply three-minute excerpts starting
at 120 seconds. They measure timing on project content. Their model-generated
archive transcripts are never used as accuracy references. Do not generalize
meeting accuracy to channel accuracy without human annotation of channel audio.
Selection and checksums are frozen in `corpus.json` before examining outputs.

```bash
python experiments/transcription_benchmark/prepare.py /absolute/experiment/corpus \
  --channel-audio /absolute/existing/video-id.mp4
```

## Run on the GPU host

Copy the corpus, WAV excerpts, and `run.py` into an isolated experiment directory.
Use the deployed diarization environment's Python for the baseline so speaker
assignment uses its existing implementation:

```bash
python run.py baseline /absolute/corpus/corpus.json /absolute/results/baseline \
  --service-config /absolute/private/service-config.json --repeats 2
python run.py nemotron /absolute/corpus/corpus.json /absolute/results/nemotron \
  --service-config /absolute/private/service-config.json \
  --runtime /absolute/isolated/bin/nemo-speech \
  --model /absolute/isolated/model.gguf --cpu-moe 12 --cpu-threads 6 --repeats 2
```

The required private service configuration is a JSON object with `asr_url` and
`diarization_url` loopback origins, a nonempty `service_units` list for the
read-only service identity checks, and a nonempty `source_files` list of absolute
unit and source paths to hash before and after each run. Keep this file and all
raw operational snapshots outside published source. The runner has no default
installation endpoints or private directory layout.

The candidate requires a NeMo-Speech.cpp build supporting `n_cpu_moe` when that
option is nonzero. On a GPU shared with resident production models, choose an
explicit memory-fitting configuration before running. Record exact source
revision, executable hash, model revision/checksums, quantization, CPU offload,
and settings. CPU offload affects throughput; such results are not measurements
of an all-GPU or full-precision deployment.

Each backend receives one 30-second warmup, then two fixed-order passes (the
second reverses sample order). Startup and warmup are recorded separately.
Steady-state wall time includes loopback upload, inference, JSON serialization,
speaker assignment for the baseline, and monitoring overhead. The baseline also
records server-reported compute times. Each sample has its own timing and sampled
GPU utilization and memory measurements. There is no claimed saturation or maximum
throughput from this single-request comparison.

## Accuracy and interpretation

```bash
python -m unittest discover -s experiments/transcription_benchmark -p 'test_*.py'
python experiments/transcription_benchmark/score.py /absolute/corpus/corpus.json \
  /absolute/results/baseline /absolute/results/nemotron --output /absolute/results/comparison.json
```

The dependency-free scorer reports:

- WER on time-ordered reference words. Overlapping dialogue can make serialization
  ambiguous; use cpWER as the main combined transcription/attribution metric.
- Concatenated minimum-permutation WER (cpWER): concatenate each speaker's words,
  then find the globally optimal one-to-one speaker assignment, including dummy
  speakers so extra/missing speakers incur errors.
- DER using exact interval integration, optimal overlap-based speaker mapping,
  and the reference evaluation regions. Report both overlap-included and
  overlap-excluded DER with a 250 ms boundary collar, plus overlap-included DER
  without a collar. Collar-masked reference speaker duration is reported.

Normalization applies Unicode NFKC, lowercase ASCII word/number extraction, and
punctuation removal while retaining internal apostrophes. Fillers, repetitions,
and number-form differences remain scored. Accuracy uses the first pass; the
second measures timing repeatability. Tests cover edit counts, speaker-label
permutations, extra speakers, missed speech, false alarms, overlap, and collars.

This is a small controlled pilot, not an independent leaderboard result or proof
of general accuracy. Model training contamination is not audited. Changing
quantization, prompts, batch sizes, or CPU offload creates a distinct configuration
and must be reported rather than silently pooled with previous results. Keep raw
audio, predictions, manifests, and local operational snapshots under ignored
`data/quality/`; do not publish private runtime paths or service access details.
