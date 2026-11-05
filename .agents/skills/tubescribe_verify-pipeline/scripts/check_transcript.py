"""Read-only structural checks of a materialized TubeScribe ASR transcript."""

import argparse
import json
import math
from pathlib import Path

from tubescribe.asr import validate


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def check(data, expected_model=None, require_continuity=False):
    if not isinstance(data, dict):
        raise ValueError("Expected a TubeScribe transcript object")
    validate(data)
    if expected_model and data["model"] != expected_model:
        raise ValueError("Transcript does not use the expected model")
    if data.get("timing_resolution") == "audio_part":
        raise ValueError("Transcript uses coarse audio-part timing")
    count, speakers = 0, set()
    for segment in data["segments"]:
        words = segment.get("words")
        if segment.get("timing_resolution") == "audio_part" or not isinstance(words, list) or not words:
            raise ValueError("A speech segment has no word-level timing")
        previous = 0.0
        for word in words:
            if not isinstance(word, dict):
                raise ValueError("A timed word is not an object")
            start, end, text = word.get("start"), word.get("end"), word.get("word")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("A timed word has no text")
            if (not number(start) or not number(end)
                    or not 0 <= start <= end <= data["duration"] + .1
                    or start < previous or start < segment["start"] - .05
                    or end > segment["end"] + .05):
                raise ValueError("Word times are non-finite, unordered or out of bounds")
            previous = start
            count += 1
        identity = segment.get("speaker_id")
        if isinstance(identity, str) and identity.strip():
            speakers.add(identity)
    cost = data.get("cost")
    if cost is not None and (not number(cost) or cost < 0):
        raise ValueError("Recorded cost must be finite and nonnegative or unreported")
    generations = data.get("generation_ids")
    parts = len(generations) if isinstance(generations, list) else None
    continuity = data.get("speaker_continuity")
    if require_continuity:
        if not speakers or parts is None or parts < 2 or not isinstance(continuity, dict):
            raise ValueError("Continuity check requires labelled speech across multiple chunks")
        matched, unresolved = continuity.get("matched_references"), continuity.get("unresolved_references")
        if type(matched) is not int or matched < 1 or type(unresolved) is not int or unresolved != 0:
            raise ValueError("Speaker reference matches are missing or unresolved")
    return {
        "structurally_valid": True,
        "model": data["model"],
        "duration_seconds": data["duration"],
        "word_count": count,
        "speaker_count": len(speakers),
        "request_count": parts,
        "recorded_transcription_cost_usd": cost,
        "continuity_metadata_checked": require_continuity,
        "audio_alignment_and_speaker_identity_verified": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--expect-model")
    parser.add_argument("--require-continuity", action="store_true")
    args = parser.parse_args()
    try:
        result = check(json.loads(args.transcript.read_text()), args.expect_model, args.require_continuity)
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Avoid copying arbitrary source text or provider errors into reports.
        result = {"structurally_valid": False, "error_type": type(error).__name__}
        if type(error) is ValueError:
            result["reason"] = str(error)
        print(json.dumps(result))
        return 1
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
