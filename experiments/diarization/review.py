"""Compare a trial to approximate publisher labels; export speaker-labelled ASR."""

import argparse
import itertools
import json
from pathlib import Path

from tubescribe.subtitles import render_markdown, render_srt, timestamp


def overlap(a, b):
    return max(0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def identify(turns, start, end):
    scores = {}
    for turn in turns:
        amount = overlap(turn, {"start": start, "end": end})
        if end == start and turn["start"] <= start < turn["end"]:
            amount = 1
        if amount:
            scores[turn["speaker"]] = scores.get(turn["speaker"], 0) + amount
    return max(scores, key=scores.get) if scores else "UNKNOWN"


def reference_intervals(reference):
    # Same-second interjections have no measurable duration in this reference.
    # Keep the last label at that timestamp; boundary padding excludes the ambiguity.
    starts = {turn["start"]: turn["speaker"] for turn in reference["turns"]}
    ordered = sorted(starts)
    return [
        {"start": start + 1, "end": end - 1, "speaker": starts[start]}
        for start, end in zip(ordered, [*ordered[1:], reference["duration"]])
        if end - start > 2
    ]


def evaluate(run, reference):
    turns = run["exclusive_turns"]
    labels = sorted({t["speaker"] for t in turns})
    intervals = reference_intervals(reference)
    anchors = {}
    for interval in intervals:
        if interval["end"] - interval["start"] >= 8:
            anchors.setdefault(interval["speaker"], interval)
    names = sorted(anchors)
    if len(labels) < len(names):
        return {"speaker_count": len(labels), "error": "Fewer detected speakers than reference"}
    # Name mapping uses just the first long turn of each voice, not the evaluation turns.
    scores = {
        (label, name): sum(overlap(t, anchors[name]) for t in turns if t["speaker"] == label)
        for label in labels
        for name in names
    }
    best = max(
        itertools.permutations(labels, len(names)),
        key=lambda permutation: sum(scores[label, name] for label, name in zip(permutation, names)),
    )
    mapping = dict(zip(best, names))
    anchor_end = max(a["end"] for a in anchors.values())
    total = covered = correct = 0.0
    checks = []
    for interval in intervals:
        if interval["start"] <= anchor_end:
            continue
        duration = interval["end"] - interval["start"]
        matched = known = 0.0
        for turn in turns:
            amount = overlap(turn, interval)
            known += amount
            if mapping.get(turn["speaker"]) == interval["speaker"]:
                matched += amount
        total += duration
        covered += known
        correct += matched
        checks.append({**interval, "labelled_seconds": known, "correct_seconds": matched})
    return {
        "speaker_count": len(labels),
        "mapping": mapping,
        "anchors": anchors,
        "evaluated_seconds": total,
        "speech_covered_seconds": covered,
        "label_agreement_on_covered_speech": correct / covered if covered else None,
        "reference_interval_coverage": covered / total if total else None,
        "intervals": checks,
        "caveat": "Approximate publisher-label agreement after one-second boundary exclusions; not human DER or measured accuracy",
    }


def label_words(asr, turns, mapping):
    cues = []
    for segment in asr["segments"]:
        words = segment.get("words") or [
            {
                "start": segment["start"],
                "end": segment["end"],
                "word": segment["text"],
            }
        ]
        for word in words:
            speaker = identify(turns, word["start"], word["end"])
            text = word["word"].strip()
            if not text:
                continue
            if (
                not cues
                or speaker != cues[-1]["speaker_id"]
                or word["start"] - cues[-1]["end"] > 1.2
                or word["end"] - cues[-1]["start"] > 7
                or len(cues[-1]["text"]) + len(text) > 90
            ):
                cues.append(
                    {
                        "start": word["start"],
                        "end": max(word["end"], word["start"] + 0.001),
                        "text": text,
                        "speaker_id": speaker,
                        "speaker_name": mapping.get(speaker),
                    }
                )
            else:
                cues[-1]["end"] = max(word["end"], cues[-1]["end"])
                cues[-1]["text"] += " " + text
    return cues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory
    asr = json.loads((root / "asr.json").read_text())
    prediction = json.loads((root / "prediction.json").read_text())
    reference = json.loads((root / "reference-turns.json").read_text())
    evaluations = [
        {"label": run["label"], **evaluate(run, reference)} for run in prediction["runs"]
    ]
    (root / "evaluation.json").write_text(json.dumps(evaluations, indent=2))
    for run, evaluation in zip(prediction["runs"], evaluations):
        if "error" in evaluation:
            continue
        cues = label_words(asr, run["exclusive_turns"], evaluation["mapping"])
        result = {
            **asr,
            "source_url": "https://www.youtube.com/watch?v=VkDpGZ_KsrY",
            "sample_start_seconds": 0,
            "speaker_name_provenance": "Reference-guided mapping from the first long turn of each speaker; not automatic person recognition",
            "diarization": {k: v for k, v in prediction.items() if k != "runs"},
            "diarization_run": run,
            "speaker_mapping": evaluation["mapping"],
            "segments": cues,
        }
        (root / f"{run['label']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        labelled = [
            {**cue, "text": f"{cue['speaker_name'] or cue['speaker_id']}: {cue['text']}"}
            for cue in cues
        ]
        (root / f"{run['label']}.srt").write_bytes(render_srt(labelled))
        (root / f"{run['label']}.md").write_bytes(render_markdown(cues))
        (root / f"{run['label']}.txt").write_text(
            "\n".join(f"[{timestamp(cue['start'])}] {cue['text']}" for cue in labelled) + "\n"
        )
    print(json.dumps(evaluations, indent=2))


if __name__ == "__main__":
    main()
