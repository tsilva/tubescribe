"""Stable HTTP resource identities independent of database IDs and object keys."""

import html
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote


def transcript_url(video_id, profile):
    return f"/videos/{quote(video_id, safe='')}/transcripts/{quote(profile, safe='')}/"


def artifact_url(video_id, profile, workflow):
    return transcript_url(video_id, profile) + "artifacts/" + quote(workflow, safe="") + "/"


def segment(value):
    value = unquote(value)
    if not value or value in {".", ".."} or any(c in value for c in "/\\\x00"):
        raise FileNotFoundError("Invalid resource identity")
    return value


@dataclass(frozen=True)
class Resource:
    row: dict
    base: str
    internal: str
    leaf: str = ""
    workflow: str | None = None
    raw_key: str | None = None
    canonical_request: bool = False

    @property
    def url(self):
        return self.base + self.leaf

    def metadata(self, views, origin=""):
        row = self.row
        base = transcript_url(row["video_id"], row["profile"])
        links = {"self": origin + self.base, "transcript": origin + base, "youtube": row["url"]}
        if self.workflow:
            artifact = views.artifact(row["job_id"], self.workflow)
            state = artifact[2] if artifact else None
            if state == "complete":
                links["markdown"] = origin + self.base + "artifact.md"
            return {
                "video_id": row["video_id"],
                "profile": row["profile"],
                "workflow": self.workflow,
                "state": state or "ready",
                "links": links,
            }
        if row["json_path"]:
            links.update(
                {
                    "raw_json": origin + base + "transcript.json",
                    "raw_markdown": origin + base + "transcript.md",
                    "text": origin + base + "transcript.txt",
                    "subtitles": origin + base + "subtitles/",
                    "srt": origin + base + "source.srt",
                }
            )
        if row["postprocessing_state"] == "complete":
            links.update(
                {
                    "markdown": origin + base + "processed.md",
                    "bundle": origin + base + "processed-with-figures.zip",
                }
            )
        artifacts = [
            {
                "workflow": a["workflow"],
                "state": a["state"],
                "url": origin + artifact_url(row["video_id"], row["profile"], a["workflow"]),
            }
            for a in views.resource_artifacts(row["job_id"])
        ]
        return {
            "video_id": row["video_id"],
            "profile": row["profile"],
            "title": row["title"],
            "states": {
                "asr": row["asr_state"],
                "diarization": row["diarization_state"],
                "processing": row["postprocessing_state"],
            },
            "links": links,
            "artifacts": artifacts,
        }


def resolve(views, path):
    """Translate canonical URLs into the existing confined delivery handlers."""
    match = re.fullmatch(r"/videos/([^/]+)/transcripts/([^/]+)(?:/(.*))?", path)
    canonical = bool(match)
    if match:
        video, profile = segment(match[1]), segment(match[2])
        row = views.resource_transcript(video_id=video, profile=profile)
        leaf = match[3] or ""
        workflow = None
        if artifact := re.fullmatch(r"artifacts/([a-z][a-z0-9_-]{0,63})(?:/(.*))?", leaf):
            workflow, leaf = artifact[1], artifact[2] or ""
    else:
        old = re.fullmatch(
            r"/processed/(\d+)/(.+)|/transcripts/(\d+)/srt|/artifacts/(\d+)/([a-z][a-z0-9_-]{0,63})(?:/(.*))?",
            path,
        )
        if not old:
            return None
        row = views.resource_transcript(job_id=int(old[1] or old[3] or old[4]))
        workflow = old[5]
        leaf = old[6] or "" if workflow else "subtitles/" if old[3] else old[2]
        if leaf == "preview.html":
            leaf = ""
    if not row:
        raise FileNotFoundError("Transcript resource not found")
    base = transcript_url(row["video_id"], row["profile"])
    job = row["job_id"]
    raw = None
    if workflow:
        base = artifact_url(row["video_id"], row["profile"], workflow)
        if leaf and not re.fullmatch(r"artifact\.md|figures/frame_\d+\.png", leaf):
            raise FileNotFoundError()
        internal = f"/artifacts/{job}/{workflow}/" + leaf
    elif leaf in {"transcript.json", "transcript.txt", "transcript.md"}:
        if not row["json_path"]:
            raise FileNotFoundError()
        from pathlib import Path

        raw = Path(row["json_path"]).with_suffix("." + leaf.rsplit(".", 1)[1]).as_posix()
        internal = ""
    elif leaf in {"subtitles", "subtitles/"}:
        leaf = "subtitles/"
        internal = f"/transcripts/{job}/srt"
    elif not leaf and row["postprocessing_state"] != "complete":
        from pathlib import Path

        raw = Path(row["json_path"]).with_suffix(".md").as_posix() if row["json_path"] else None
        internal = ""
    elif not leaf or re.fullmatch(
        r"processed\.md|source\.srt|processed-with-figures\.zip|figures/frame_\d+\.png", leaf
    ):
        if leaf == "source.srt":
            from pathlib import Path

            if not row["json_path"]:
                raise FileNotFoundError()
            raw = Path(row["json_path"]).with_suffix(".srt").as_posix()
            internal = ""
        else:
            internal = f"/processed/{job}/" + (leaf or "preview.html")
    else:
        raise FileNotFoundError()
    return Resource(row, base, internal, leaf, workflow, raw, canonical)


def rewrite_links(text, resource):
    row = resource.row
    base = transcript_url(row["video_id"], row["profile"])
    job = row["job_id"]
    text = text.replace(f"/processed/{job}/preview.html", base)
    text = text.replace(f"/transcripts/{job}/srt", base + "subtitles/")
    return re.sub(
        rf"/artifacts/{job}/([a-z][a-z0-9_-]{{0,63}})/(?:run)?",
        lambda m: artifact_url(row["video_id"], row["profile"], m[1]),
        text,
    )


def video_metadata(views, video_id, origin=""):
    video = views.resource_video(video_id)
    if not video:
        raise FileNotFoundError()
    rows = views.resource_profiles(video_id)
    return {
        **video,
        "links": {"self": origin + "/videos/" + quote(video_id, safe=""), "youtube": video["url"]},
        "transcripts": [
            {"profile": r["profile"], "url": origin + transcript_url(video_id, r["profile"])}
            for r in rows
        ],
    }


def render_video(metadata):
    title = html.escape(metadata["title"])
    links = "".join(
        f'<li><a href="{html.escape(r["url"], quote=True)}">{html.escape(r["profile"])}</a></li>'
        for r in metadata["transcripts"]
    )
    return (
        f'<!doctype html><html lang="en"><meta charset="utf-8"><title>{title}</title>'
        f'<main><h1>{title}</h1><p><a href="{html.escape(metadata["url"], quote=True)}">Watch on YouTube</a></p>'
        f"<h2>Transcripts</h2><ul>{links}</ul></main></html>"
    )
