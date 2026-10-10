"""A script-free transcript reader linking each cue to its YouTube timestamp."""

from html import escape
from urllib.parse import quote

from .postprocessing import srt_cues


def render_srt(srt, *, title, video_id, download_url, processed_url=None):
    youtube = "https://www.youtube.com/watch?v=" + quote(video_id, safe="")
    cues = srt_cues(srt)
    rows = []
    for cue in cues:
        start, _, end = cue["timing"].partition(" --> ")
        timestamp_url = escape(youtube + "&t=" + str(int(cue["timestamp"])) + "s", quote=True)
        rows.append(
            f'<li id="cue-{cue["cue"]}" class="srt-cue">'
            '<div class="srt-time">'
            f'<a href="{timestamp_url}" target="_blank" rel="noopener noreferrer" '
            f'aria-label="Play on YouTube at {escape(start)}">{escape(start)}</a>'
            f'<span class="muted">→ {escape(end)}</span></div>'
            f'<p class="srt-text">{escape(cue["body"])}</p></li>'
        )
    processed = (
        f'<a class="button" href="{escape(processed_url, quote=True)}">Processed transcript</a>'
        if processed_url
        else ""
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)} · SRT · TubeScribe</title>"
        '<link rel="stylesheet" href="/style.css"></head><body><main class="srt-viewer">'
        '<header class="page-heading"><div><a class="brand" href="/">TubeScribe</a>'
        f"<h1>{escape(title)}</h1><p>{len(cues)} cues · Click a timestamp to play on YouTube.</p>"
        '</div></header><nav class="srt-actions" aria-label="Transcript actions">'
        f'<a class="button" href="{escape(youtube, quote=True)}" target="_blank" '
        'rel="noopener noreferrer">Open on YouTube</a>'
        f'<a class="button" href="{escape(download_url, quote=True)}" download>Download SRT</a>'
        f'{processed}</nav><ol class="srt-cues" aria-label="Transcript cues">'
        + "".join(rows)
        + "</ol></main></body></html>"
    )
