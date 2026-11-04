"""Paired WER, concatenated minimum-permutation WER, exact-interval DER, and timing."""

import argparse
import functools
import json
import re
import statistics
import unicodedata
from collections import defaultdict
from pathlib import Path


def tokens(text):
    text = re.sub(r"<[^>]+>", " ", text)
    text = unicodedata.normalize("NFKC", text).lower().replace("’", "'")
    return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)*", text)


def distance(reference, hypothesis):
    row = list(range(len(hypothesis) + 1))
    for i, a in enumerate(reference, 1):
        new = [i]
        for j, b in enumerate(hypothesis, 1):
            new.append(min(row[j] + 1, new[-1] + 1, row[j - 1] + (a != b)))
        row = new
    return row[-1]


def assignment(costs):
    """Square minimum-weight assignment via subset DP; includes unmatched dummy speakers."""
    n = len(costs)

    @functools.lru_cache(None)
    def solve(i, mask):
        if i == n:
            return 0, ()
        choices = []
        for j in range(n):
            if not (mask >> j) & 1:
                value, tail = solve(i + 1, mask | (1 << j))
                choices.append((costs[i][j] + value, (j,) + tail))
        return min(choices)

    return solve(0, 0) if n else (0, ())


def cpwer(reference_words, hypothesis_words):
    ref, hyp = defaultdict(list), defaultdict(list)
    for w in reference_words:
        ref[str(w["speaker"])].extend(tokens(w["text"]))
    for w in hypothesis_words:
        hyp[str(w["speaker"])].extend(tokens(w["text"]))
    n = max(len(ref), len(hyp))
    if n > 10:
        raise ValueError("More than ten speakers; refuse exponential assignment")
    refs = list(ref.values()) + [[]] * (n - len(ref))
    hyps = list(hyp.values()) + [[]] * (n - len(hyp))
    error, permutation = assignment([[distance(a, b) for b in hyps] for a in refs])
    count = sum(map(len, refs))
    return {
        "errors": error,
        "reference_words": count,
        "rate": error / count if count else None,
        "reference_speakers": len(ref),
        "hypothesis_speakers": len(hyp),
        "permutation": permutation,
    }


def der(reference, hypothesis, uem, collar=0.25, exclude_overlap=False):
    reference = [{**t, "speaker": str(t["speaker"])} for t in reference]
    hypothesis = [{**t, "speaker": str(t["speaker"])} for t in hypothesis]
    ref_ids = sorted({t["speaker"] for t in reference})
    hyp_ids = sorted({t["speaker"] for t in hypothesis})
    n = max(len(ref_ids), len(hyp_ids))
    boundaries = {v for t in reference for v in [t["start"], t["end"]]}
    points = {x for region in uem for x in region}
    for t in reference + hypothesis:
        points.update([t["start"], t["end"]])
    for b in boundaries:
        points.update([b - collar, b + collar])
    points = sorted(points)
    intervals = []
    overlap = [[0.0] * n for _ in range(n)]
    ri, hi = {v: i for i, v in enumerate(ref_ids)}, {v: i for i, v in enumerate(hyp_ids)}
    for a, b in zip(points, points[1:]):
        mid = (a + b) / 2
        if not any(x <= mid < y for x, y in uem):
            continue
        if collar and any(abs(mid - v) < collar for v in boundaries):
            continue
        r = {t["speaker"] for t in reference if t["start"] <= mid < t["end"]}
        h = {t["speaker"] for t in hypothesis if t["start"] <= mid < t["end"]}
        if exclude_overlap and len(r) > 1:
            continue
        intervals.append((b - a, r, h))
        for x in r:
            for y in h:
                overlap[ri[x]][hi[y]] += b - a
    _, permutation = assignment([[-v for v in row] for row in overlap])
    mapping = {
        hyp_ids[j]: ref_ids[i]
        for i, j in enumerate(permutation)
        if i < len(ref_ids) and j < len(hyp_ids)
    }
    missed = false = confusion = total = 0.0
    for length, r, h in intervals:
        correct = len(r & {mapping.get(s) for s in h})
        missed += length * max(0, len(r) - len(h))
        false += length * max(0, len(h) - len(r))
        confusion += length * (min(len(r), len(h)) - correct)
        total += length * len(r)
    return {
        "rate": (missed + false + confusion) / total if total else None,
        "missed_seconds": missed,
        "false_alarm_seconds": false,
        "confusion_seconds": confusion,
        "reference_speaker_seconds": total,
        "collar": collar,
        "exclude_overlap": exclude_overlap,
        "mapping": mapping,
    }


def score_sample(sample, prediction):
    reference = sample["reference_words"]
    ref_tokens = [t for w in reference for t in tokens(w["text"])]
    hyp_tokens = tokens(prediction["text"])
    errors = distance(ref_tokens, hyp_tokens)
    turns = prediction["turns"]
    return {
        "wer": {
            "rate": errors / len(ref_tokens),
            "errors": errors,
            "reference_words": len(ref_tokens),
        },
        "cpwer": cpwer(reference, prediction["words"]),
        "der_overlap": der(sample["reference_turns"], turns, sample["uem"]),
        "der_no_overlap": der(
            sample["reference_turns"], turns, sample["uem"], exclude_overlap=True
        ),
        "der_overlap_no_collar": der(sample["reference_turns"], turns, sample["uem"], collar=0),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("corpus", type=Path)
    p.add_argument("runs", type=Path, nargs="+")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    corpus = json.loads(args.corpus.read_text())
    paired_keys = None
    for run in args.runs:
        keys = set()
        for sample in corpus["samples"]:
            for file in run.glob(sample["id"] + "-r*.json"):
                prediction = json.loads(file.read_text())
                keys.add((prediction["sample_id"], prediction["repeat"]))
        if paired_keys is None:
            paired_keys = keys
        elif paired_keys != keys:
            raise ValueError(
                "Backends have different completed sample/repeat sets; refuse unpaired timing"
            )
    report = {
        "normalization": "NFKC, lowercase, ASCII words/numbers, punctuation removed; apostrophes retained. No model-derived references or number expansion.",
        "accuracy_scope": "Six fixed 3-minute AMI test excerpts from three meetings; human word and speaker annotations. Channel samples are timing-only.",
        "results": {},
    }
    for run in args.runs:
        scored, timings = [], []
        for sample in corpus["samples"]:
            predictions = sorted(run.glob(sample["id"] + "-r*.json"))
            if not predictions:
                raise ValueError(f"Missing sample {sample['id']} in {run}")
            for file in predictions:
                pred = json.loads(file.read_text())
                if pred["audio_sha256"] != sample["sha256"]:
                    raise ValueError("Scored audio differs from frozen corpus")
                timing = {
                    k: pred.get(k)
                    for k in [
                        "sample_id",
                        "repeat",
                        "wall_seconds",
                        "duration",
                        "server_compute_seconds",
                        "asr_wall_seconds",
                        "diarization_wall_seconds",
                    ]
                }
                gpu = pred["gpu_samples"]
                timing["peak_total_gpu_mib"] = max((x["used_mib"] for x in gpu), default=None)
                timing["mean_gpu_utilization"] = (
                    statistics.mean(x["utilization"] for x in gpu) if gpu else None
                )
                timings.append(timing)
                if sample["reference_words"] is not None and pred["repeat"] == 0:
                    print("Scoring", run.name, sample["id"], flush=True)
                    scored.append({"sample_id": sample["id"], **score_sample(sample, pred)})
        aggregate = {}
        for metric in ["wer", "cpwer"]:
            e = sum(r[metric]["errors"] for r in scored)
            count = sum(r[metric]["reference_words"] for r in scored)
            aggregate[metric] = {"rate": e / count, "errors": e, "reference_words": count}
        for metric in ["der_overlap", "der_no_overlap", "der_overlap_no_collar"]:
            components = {
                k: sum(r[metric][k] for r in scored)
                for k in [
                    "missed_seconds",
                    "false_alarm_seconds",
                    "confusion_seconds",
                    "reference_speaker_seconds",
                ]
            }
            components["rate"] = (
                sum(
                    components[k]
                    for k in ["missed_seconds", "false_alarm_seconds", "confusion_seconds"]
                )
                / components["reference_speaker_seconds"]
            )
            aggregate[metric] = components
        aggregate["throughput_x_realtime"] = sum(t["duration"] for t in timings) / sum(
            t["wall_seconds"] for t in timings
        )
        aggregate["wall_seconds"] = sum(t["wall_seconds"] for t in timings)
        aggregate["audio_seconds_including_repeats"] = sum(t["duration"] for t in timings)
        report["results"][run.name] = {
            "aggregate": aggregate,
            "samples": scored,
            "timings": timings,
        }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v["aggregate"] for k, v in report["results"].items()}, indent=2))


if __name__ == "__main__":
    main()
