"""Plain UTF-8 SubRip subtitles derived from authoritative ASR segments."""

import math


def timestamp(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Subtitle timestamps must be finite and nonnegative")
    # Round the whole value before splitting, so 59.9996 carries into the next minute.
    total = math.floor(seconds * 1000 + 0.5)
    seconds, milliseconds = divmod(total, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def render_srt(segments: list[dict]) -> bytes:
    cues = []
    for index, segment in enumerate(segments, 1):
        start, end = segment["start"], segment["end"]
        if end <= start:
            raise ValueError("Subtitle end must follow its start")
        # Blank lines delimit cues in SRT; normalize embedded whitespace in ASR text.
        text = " ".join(segment["text"].split())
        if not text:
            raise ValueError("Subtitle cue is empty")
        first, last = timestamp(start), timestamp(end)
        if first == last:
            last = timestamp((math.floor(start * 1000 + 0.5) + 1) / 1000)
        cues.append(f"{index}\n{first} --> {last}\n{text}\n\n")
    if not cues:
        raise ValueError("No subtitle cues")
    return "".join(cues).encode("utf-8")
