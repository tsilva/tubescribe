"""Resolve logical document references when serving or exporting a publication."""

import json
import os
from dataclasses import dataclass
from urllib.parse import quote

from .blobs import checked_key


@dataclass(frozen=True)
class Delivery:
    base_url: str = ""
    export_directory: str | None = None

    def transcript(self, job_id, source):
        if self.export_directory is not None:
            return quote(
                os.path.relpath(
                    checked_key(source).replace("/processed.md", "/preview.html"),
                    self.export_directory,
                )
            )
        return self.base_url.rstrip("/") + f"/processed/{job_id}/preview.html"

    def resolve(self, markdown, references):
        source = references["transcript"]
        url = self.transcript(source["job_id"], source["key"])
        markdown = markdown.replace(
            f"(/processed/{source['job_id']}/preview.html)", f"(transcript:{source['job_id']})"
        )
        text = markdown.replace("(transcript:" + str(source["job_id"]) + ")", "(" + url + ")")
        for image_id, key in references.get("figures", {}).items():
            text = text.replace("(figure:" + image_id + ")", "(" + quote(checked_key(key)) + ")")
        return text, url


def render_bundle(directory, delivery=Delivery(), *, markdown=False):
    from .artifacts import render_artifact

    references = json.loads((directory / "references.json").read_text())
    report = json.loads((directory / "report.json").read_text())
    text, url = delivery.resolve((directory / "artifact.md").read_text(), references)
    if markdown:
        return text
    page = render_artifact(text, report["workflow"]["title"] + " · " + references["title"], url)
    if delivery.export_directory is not None:
        page = page.replace('href="artifact.md"', 'href="read.md"')
    return page


def export_publications(root):
    """Render the exported copy, including legacy bundles, without editing live files."""
    import sqlite3
    from contextlib import closing

    from .blobs import LocalArtifacts, publish_bundle

    with closing(sqlite3.connect(root / "archive.sqlite3")) as db, db:
        rows = db.execute(
            "SELECT a.job_id,a.output_dir,v.title FROM artifacts a JOIN jobs j ON j.id=a.job_id JOIN videos v ON v.id=j.video_id WHERE a.state='complete'"
        ).fetchall()
        for job_id, key, title in rows:
            directory = LocalArtifacts(root).path(key)
            reference_file = directory / "references.json"
            if not reference_file.exists():
                report = json.loads((directory / "report.json").read_text())
                reference_file.write_text(
                    json.dumps(
                        {
                            "title": title,
                            "transcript": {"job_id": job_id, "key": checked_key(report["source"])},
                            "figures": {},
                        }
                    )
                )
            delivery = Delivery(export_directory=key)
            (directory / "preview.html").write_text(render_bundle(directory, delivery))
            (directory / "read.md").write_text(render_bundle(directory, delivery, markdown=True))
            ref = publish_bundle(LocalArtifacts(root), directory, key)
            db.execute(
                "UPDATE artifacts SET manifest_sha256=? WHERE job_id=? AND output_dir=?",
                (ref.sha256, job_id, key),
            )
