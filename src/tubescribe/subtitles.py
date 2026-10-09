"""SubRip subtitles and Markdown speaker turns derived from ASR segments."""

import math


def render_markdown(segments: list[dict]) -> bytes:
    """Merge consecutive cues from the same voice without inventing speaker names."""
    turns = []
    for segment in segments:
        text = " ".join(segment["text"].split())
        if not text:
            raise ValueError("Transcript cue is empty")
        name = segment.get("speaker_name")
        identity = segment.get("speaker_id") or segment.get("speaker") or name or "Speaker"
        label = " ".join(str(name or identity).split())
        if turns and turns[-1]["identity"] == identity:
            turns[-1]["text"] += " " + text
        else:
            turns.append({"identity": identity, "label": label, "text": text})
    if not turns:
        raise ValueError("No transcript cues")

    # Escape label markup so IDs such as speaker_0 remain readable literally.
    def escape(label):
        for char in ("\\", "*", "_", "[", "]"):
            label = label.replace(char, "\\" + char)
        return label

    return ("\n\n".join(f"**{escape(t['label'])}:** {t['text']}" for t in turns) + "\n").encode(
        "utf-8"
    )


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
        speaker = segment.get("speaker_name") or segment.get("speaker_id") or segment.get("speaker")
        if speaker:
            text = f"{speaker}: {text}"
        first, last = timestamp(start), timestamp(end)
        if first == last:
            last = timestamp((math.floor(start * 1000 + 0.5) + 1) / 1000)
        cues.append(f"{index}\n{first} --> {last}\n{text}\n\n")
    if not cues:
        raise ValueError("No subtitle cues")
    return "".join(cues).encode("utf-8")
