#!/usr/bin/env python3
"""Re-audit completed pilots and publish an offline comparison; never calls a model."""

import argparse
import hashlib
import html
import json
import os
import re
import zipfile
from pathlib import Path

from tubescribe import postprocessing as pilot


def read_json(path):
    return json.loads(path.read_text())


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resource_path(run, value):
    path = (run / value).resolve()
    if not path.is_relative_to(run.resolve()) or Path(value).is_absolute():
        raise ValueError("Resource path escapes the run directory")
    return path


def metrics(run):
    report = read_json(run / "report.json")
    response = read_json(run / "response.json")
    usage = report["usage"]["total"]
    agent_seconds = report.get("agent_elapsed_seconds", response["elapsed_seconds"])
    return {
        "run": str(run.resolve()),
        "minutes": round(report["duration"] / 60, 2),
        "agent_seconds": agent_seconds,
        "caller_seconds": report["elapsed_seconds"],
        "queue_and_transport_seconds": round(report["elapsed_seconds"] - agent_seconds, 2),
        "total_tokens": usage["totalTokens"],
        "uncached_input_tokens": usage["inputTokens"] - usage.get("cachedInputTokens", 0),
        "cached_input_tokens": usage.get("cachedInputTokens", 0),
        "output_tokens": usage["outputTokens"],
        "tool_calls": report["tool_calls"],
        "figures": report["figures"],
    }


def audit(run):
    """Check actual source, response, Markdown, resource bytes and downloadable package."""
    report = read_json(run / "report.json")
    source_path = Path(report["source"])
    request_path = run / "request.json"
    title_match = (
        re.search(r"^Title: (.*)$", read_json(request_path).get("input", ""), re.M)
        if request_path.exists()
        else None
    )
    source = pilot.load_source(
        source_path,
        title=title_match[1] if title_match else None,
        speaker_labels=report.get("declared_speaker_labels", ()),
    )
    if sha256(source_path) != report["source_sha256"]:
        raise ValueError("Source hash changed since processing")
    if sha256(source["srt_path"]) != report["srt_sha256"]:
        raise ValueError("SRT hash changed since processing")
    response = read_json(run / "response.json")
    assets_path = run / "assets.json"
    assets = read_json(assets_path) if assets_path.exists() else {}
    calls_path = run / "tool-calls.json"
    calls = read_json(calls_path) if calls_path.exists() else []
    issued = {image_id for call in calls for image_id in call["image_ids"]}
    resources = read_json(run / "resources.json")
    expected_resources = set(response["output"]["used_image_ids"])
    expected_resources.update(
        entry["image_id"]
        for speaker in response["output"]["speaker_names"]
        for entry in speaker["evidence"]
        if entry["source"] == "video_frame"
    )
    if set(resources) != expected_resources or not expected_resources.issubset(issued):
        raise ValueError("Packaged resources differ from returned figure/identity evidence IDs")
    for image_id, resource in resources.items():
        if resource != assets.get(image_id):
            raise ValueError("Resource metadata differs from captured asset")
        path = resource_path(run, resource["path"])
        if sha256(path) != resource["sha256"] or not path.read_bytes().startswith(
            b"\x89PNG\r\n\x1a\n"
        ):
            raise ValueError("Resource PNG missing, modified or invalid")
    annotation_labels = [
        (note, cue["text"].split(":", 1)[0])
        for cue in pilot.srt_cues(source["srt"])
        for note in pilot.annotation_texts(cue["text"])
    ]
    markdown, checks = pilot.stitch(
        response,
        {key: value for key, value in assets.items() if key in issued},
        source["cue_count"],
        source["speaker_ids"],
        source["annotations"],
        source["provided_speaker_labels"],
        annotation_labels,
    )
    checks.update(pilot.audit_transcript_fidelity(source["srt"], markdown))
    if (run / "processed.md").read_text() != markdown.rstrip() + "\n":
        raise ValueError("Processed Markdown differs from validated model output")
    if read_json(run / "speaker_names.json") != response["output"]["speaker_names"]:
        raise ValueError("Speaker identity file differs from model output")
    for key, value in checks.items():
        if key in report and report[key] != value:
            raise ValueError(f"Reported {key} differs from re-audit")
    names = ["processed.md", "preview.html", "report.json", "resources.json", "speaker_names.json"]
    names += [value["path"] for value in resources.values()]
    with zipfile.ZipFile(run / "processed-with-figures.zip") as archive:
        if archive.testzip() is not None or sorted(archive.namelist()) != sorted(names):
            raise ValueError("Download package is corrupt or incomplete")
        for name in names:
            if archive.read(name) != resource_path(run, name).read_bytes():
                raise ValueError("Download package differs from run files")
    full_ids = {
        image_id
        for call in calls
        if call["arguments"]["resolution"] == "full"
        for image_id in call["image_ids"]
    }
    return {
        **metrics(run),
        **checks,
        "title": source["title"],
        "source_sha256": report["source_sha256"],
        "srt_sha256": report["srt_sha256"],
        "source_url": source["source_url"],
        "speaker_ids": source["speaker_ids"],
        "confirmed_names": [
            sp["name"]
            for sp in response["output"]["speaker_names"]
            if sp["confidence"] == "confirmed"
        ],
        "figures_without_full_view": sorted(set(response["output"]["used_image_ids"]) - full_ids),
        "status": "pass",
    }


def compare(baseline, candidate):
    before, after = metrics(baseline), metrics(candidate)
    before_source, after_source = (
        read_json(baseline / "report.json"),
        read_json(candidate / "report.json"),
    )
    if before_source["srt_sha256"] != after_source["srt_sha256"]:
        raise ValueError("Comparison runs must use the same original SRT")
    fields = ("total_tokens", "uncached_input_tokens", "agent_seconds")
    return {
        "baseline": before,
        "candidate": after,
        "reduction_percent": {
            key: round(100 * (1 - after[key] / before[key]), 1) for key in fields if before[key]
        },
    }


def publish(runs, comparisons, output, notes=None):
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for run in runs:
        try:
            results.append(audit(run))
        except (ValueError, KeyError, OSError, TypeError, zipfile.BadZipFile) as exc:
            results.append({"run": str(run.resolve()), "status": "fail", "error": str(exc)})
    pairs = [compare(Path(before), Path(after)) for before, after in comparisons]
    unique = {item["srt_sha256"]: item for item in results if item["status"] == "pass"}
    summary = {
        "passed": sum(item["status"] == "pass" for item in results),
        "failed": sum(item["status"] == "fail" for item in results),
        "unique_input_transcripts": len(unique),
        "unique_input_minutes": round(sum(item["minutes"] for item in unique.values()), 2),
        "limitations": "Structural/resource checks and gross omission detection, not semantic accuracy. "
        "Cue declarations do not prove every sentence is preserved. Review captions, identities and speech. "
        "Agent time excludes bridge queueing; caller time includes queue/transport. "
        "Total tokens include cached inputs; reductions are not equivalent to billing savings. "
        "Single paired runs contain model variance and multiple changes, not controlled causal estimates.",
        "runs": results,
        "comparisons": pairs,
        "review_notes": notes,
    }
    (output / "evaluation.json").write_text(json.dumps(summary, indent=2) + "\n")
    rows = []
    for item in results:
        href = Path(os.path.relpath(item["run"], output)).as_posix()
        label = html.escape(item.get("title", Path(item["run"]).name))
        if item["status"] == "fail":
            rows.append(
                f"<tr><td>{label}</td><td colspan='7'>FAIL: {html.escape(item['error'])}</td></tr>"
            )
            continue
        rows.append(
            f"<tr><td><a href='{html.escape(href, quote=True)}/preview.html'>{label}</a>"
            f"<br><small>{html.escape(Path(item['run']).name)}</small>"
            f" · <a href='{html.escape(href, quote=True)}/processed-with-figures.zip'>Download</a></td>"
            f"<td>{item['minutes']}</td><td>{item['figures']}</td>"
            f"<td>{item['agent_seconds']:.0f}s</td><td>{item['caller_seconds']:.0f}s</td>"
            f"<td>{item['total_tokens']:,}</td><td>{item['uncached_input_tokens']:,}</td>"
            f"<td>PASS<br>{len(item['unresolved_references'])} unresolved</td></tr>"
        )
    comparison_rows = [
        "<tr><td>"
        + html.escape(Path(pair["baseline"]["run"]).name)
        + " → "
        + html.escape(Path(pair["candidate"]["run"]).name)
        + "</td>"
        + "".join(
            f"<td>{pair['reduction_percent'][key]}%</td>"
            for key in ("total_tokens", "uncached_input_tokens", "agent_seconds")
        )
        + "</tr>"
        for pair in pairs
    ]
    (output / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>Post-processing evaluation</title>"
        "<style>body{font:16px system-ui;margin:32px;color:#18252c;background:#f5f7f8}"
        "table{border-collapse:collapse;background:white;width:100%}td,th{text-align:left;"
        "padding:12px;border-bottom:1px solid #ddd}small{color:#64737a}a{color:#075d91}"
        "p{max-width:1000px;line-height:1.5}</style><h1>Post-processing evaluation</h1>"
        f"<p>{summary['passed']} passed · {summary['failed']} failed · "
        f"{len(unique)} unique inputs · {summary['unique_input_minutes']} minutes. "
        "Inputs may be excerpts; the displayed duration is the supplied scope.</p>"
        "<p>" + html.escape(summary["limitations"]) + "</p>"
        "<table><tr><th>Transcript / result</th><th>Minutes</th><th>Figures</th>"
        "<th>Agent</th><th>Caller</th><th>Total tokens</th><th>Uncached input</th><th>Audit</th></tr>"
        + "".join(rows)
        + "</table><h2>Paired run reductions</h2>"
        "<p>Negative reductions indicate a regression. These comparisons include all supplied pairs.</p>"
        "<table><tr><th>Pair</th><th>Total tokens</th><th>Uncached input</th><th>Agent time</th></tr>"
        + "".join(comparison_rows)
        + "</table>"
        "<p><a href='evaluation.json'>Machine-readable audit and metrics</a></p>"
        + (
            "<details><summary>Review notes and scope</summary><pre style='white-space:pre-wrap'>"
            + html.escape(notes)
            + "</pre></details>"
            if notes
            else ""
        )
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs", type=Path, nargs="+", required=True, help="Completed run directories"
    )
    parser.add_argument(
        "--compare", nargs=2, action="append", default=[], metavar=("BASELINE", "CANDIDATE")
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="Evaluation directory, alongside runs"
    )
    parser.add_argument(
        "--notes", type=Path, help="Optional human review notes to include verbatim"
    )
    args = parser.parse_args()
    if any(args.output.resolve() == run.resolve() for run in args.runs):
        parser.error("Evaluation output must differ from every input run directory")
    summary = publish(
        args.runs, args.compare, args.output, args.notes.read_text() if args.notes else None
    )
    print(
        f"{summary['passed']} passed, {summary['failed']} failed; report: {args.output / 'index.html'}"
    )
    raise SystemExit(bool(summary["failed"]))


if __name__ == "__main__":
    main()
