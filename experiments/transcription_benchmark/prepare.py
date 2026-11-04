"""Freeze public human-annotated AMI excerpts and existing channel audio; no archive writes."""

import argparse
import hashlib
import json
import subprocess
import urllib.request
import wave
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

AMI_REVISION = "67c2d539286e89f68952d5dcf83912bd9f01dfae"
ANNOTATIONS = "https://groups.inf.ed.ac.uk/ami/AMICorpusAnnotations/ami_public_manual_1.6.2.zip"


def fetch(url, path):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        urllib.request.urlretrieve(url, temporary)
        temporary.replace(path)
    return path


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def clip(source, target, start, duration):
    if not target.exists():
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-ss",
                str(start),
                "-i",
                str(source),
                "-t",
                str(duration),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(target),
            ],
            check=True,
        )
    with wave.open(str(target)) as audio:
        actual = audio.getnframes() / audio.getframerate()
    if abs(actual - duration) > 0.05:
        raise ValueError(f"Incomplete excerpt: {target}")
    return {"audio": target.name, "duration": actual, "sha256": digest(target)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--channel-audio", type=Path, action="append", default=[])
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    archive = fetch(ANNOTATIONS, root / "ami-annotations.zip")
    records = []
    with zipfile.ZipFile(archive) as annotations:
        for meeting in ["ES2004a", "IS1009a", "TS3003a"]:
            url = (
                "https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus/"
                f"{meeting}/audio/{meeting}.Mix-Headset.wav"
            )
            source = fetch(url, root / "sources" / f"{meeting}.wav")
            rttm_url = (
                "https://raw.githubusercontent.com/pyannote/AMI-diarization-setup/"
                f"{AMI_REVISION}/only_words/rttms/test/{meeting}.rttm"
            )
            rttm = fetch(rttm_url, root / "sources" / f"{meeting}.rttm")
            uem_url = (
                "https://raw.githubusercontent.com/pyannote/AMI-diarization-setup/"
                f"{AMI_REVISION}/uems/test/{meeting}.uem"
            )
            uem = fetch(uem_url, root / "sources" / f"{meeting}.uem")
            words = []
            for speaker in "ABCD":
                name = f"words/{meeting}.{speaker}.words.xml"
                for word in ET.fromstring(annotations.read(name)):
                    if word.tag != "w" or word.get("punc") == "true":
                        continue
                    if not word.get("starttime") or not word.get("endtime"):
                        continue
                    words.append(
                        {
                            "start": float(word.get("starttime")),
                            "end": float(word.get("endtime")),
                            "speaker": speaker,
                            "text": "".join(word.itertext()).strip(),
                        }
                    )
            words.sort(key=lambda w: (w["start"], w["end"], w["speaker"]))
            for start in [60, 360]:
                duration = 180
                sample_id = f"ami-{meeting}-{start}"
                row = clip(source, root / f"{sample_id}.wav", start, duration)
                reference = [
                    {
                        **w,
                        "start": max(0, w["start"] - start),
                        "end": min(duration, w["end"] - start),
                    }
                    for w in words
                    if start <= (w["start"] + w["end"]) / 2 < start + duration
                ]
                turns = []
                for line in rttm.read_text().splitlines():
                    fields = line.split()
                    a, b = float(fields[3]), float(fields[3]) + float(fields[4])
                    if a < start + duration and b > start:
                        turns.append(
                            {
                                "start": max(0, a - start),
                                "end": min(duration, b - start),
                                "speaker": fields[7],
                            }
                        )
                regions = []
                for line in uem.read_text().splitlines():
                    fields = line.split()
                    a, b = float(fields[2]), float(fields[3])
                    if a < start + duration and b > start:
                        regions.append([max(0, a - start), min(duration, b - start)])
                row.update(
                    id=sample_id,
                    kind="human_reference",
                    source=url,
                    source_sha256=digest(source),
                    offset=start,
                    reference_words=reference,
                    reference_turns=turns,
                    uem=regions,
                    reference_sources=[ANNOTATIONS, rttm_url, uem_url],
                )
                records.append(row)
                print(
                    json.dumps({"prepared": sample_id, "reference_words": len(reference)}),
                    flush=True,
                )
    for source in args.channel_audio:
        sample_id = f"channel-{source.stem}-120"
        row = clip(source, root / f"{sample_id}.wav", 120, 180)
        row.update(
            id=sample_id,
            kind="channel_timing_only",
            offset=120,
            source=f"https://www.youtube.com/watch?v={source.stem}",
            source_sha256=digest(source),
            reference_words=None,
        )
        records.append(row)
    manifest = {
        "schema_version": 1,
        "selection": "Fixed before model outputs: 60s and 360s excerpts from three AMI test meetings; two cached channel excerpts at 120s.",
        "annotations_sha256": digest(archive),
        "ami_setup_revision": AMI_REVISION,
        "samples": records,
    }
    (root / "corpus.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
