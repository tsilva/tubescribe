"""Match request-local diarization against real audio references, without local inference."""

import io
from collections import Counter
from dataclasses import dataclass, field

RATE = 16000
REFERENCE_SECONDS = 10
REFERENCE_BUDGET = 50
PAUSE_SECONDS = 0.15
MAX_REFERENCES = 32


def speaker(word):
    value = word.get("speaker_label")
    if value is None:
        value = word.get("speaker")
    return str(value) if type(value) in (str, int) and str(value).strip() else None


def wav(pcm):
    import wave

    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, RATE, 0, "NONE", ""))
        audio.writeframes(pcm)
    return output.getvalue()


@dataclass
class SpeakerContinuity:
    # Insertion order is stable when replaying saved receipts after interruption.
    anchors: dict = field(default_factory=dict)
    next_id: int = 1
    reference_seconds: float = 0
    matched: int = 0
    unresolved: int = 0

    def request(self, pcm):
        selected = list(self.anchors.items())[-MAX_REFERENCES:]
        frames = bytearray()
        references = []
        if selected:
            cap = int((REFERENCE_BUDGET / len(selected) - PAUSE_SECONDS) * RATE)
            for identity, anchor in selected:
                sample = anchor[:max(0, cap) * 2]
                start = len(frames) / (RATE * 2)
                frames.extend(sample)
                end = len(frames) / (RATE * 2)
                references.append((identity, start, end))
                frames.extend(b"\0\0" * int(PAUSE_SECONDS * RATE))
        prefix = len(frames) / (RATE * 2)
        self.reference_seconds += prefix
        frames.extend(pcm)
        return wav(bytes(frames)), prefix, references

    def apply(self, response, pcm, prefix, references):
        words = response.get("words")
        if not isinstance(words, list):
            if prefix:
                raise ValueError("MAI omitted word timing; saved response retained for review")
            return response, {}
        duration = len(pcm) / (RATE * 2)
        mapping, candidates = {}, {}
        for identity, start, end in references:
            votes = Counter()
            for word in words:
                label = speaker(word)
                if label is not None:
                    overlap = max(0, min(word["end"], end - .05) - max(word["start"], start + .05))
                    votes[label] += overlap
            ranked = votes.most_common()
            if ranked and ranked[0][1] >= .35 and ranked[0][1] >= sum(votes.values()) * .7:
                candidates.setdefault(ranked[0][0], []).append(identity)
        ambiguous = {label for label, ids in candidates.items() if len(ids) > 1}
        for label, identities in candidates.items():
            if label not in ambiguous:
                mapping[label] = identities[0]
        self.matched += len(mapping)
        unresolved = len(references) - len(mapping) + max(0, len(self.anchors) - len(references))
        self.unresolved += unresolved
        body = []
        for original in words:
            # Reference transcripts never enter the publication or its timestamps.
            if (original["start"] + original["end"]) / 2 < prefix:
                continue
            start = max(0, original["start"] - prefix)
            end = min(duration, original["end"] - prefix)
            if end <= start or start >= duration:
                continue
            label = speaker(original)
            identity = None
            if label is not None and label not in ambiguous:
                if label not in mapping and not unresolved:
                    mapping[label] = f"Speaker {self.next_id}"
                    self.next_id += 1
                identity = mapping.get(label)
            word = {**original, "start": start, "end": end, "speaker": identity}
            word.pop("speaker_label", None)
            body.append(word)
        self.remember(body, pcm)
        return {**response, "text": " ".join(w["word"] for w in body), "words": body}, {
            identity: identity for identity in mapping.values()
        }

    def remember(self, words, pcm):
        groups = []
        previous = None
        for word in words:
            identity = speaker(word)
            if identity is None:
                previous = None
                continue
            if (groups and previous == identity
                    and word["start"] - groups[-1][2] <= .6):
                groups[-1][2] = max(groups[-1][2], word["end"])
            else:
                groups.append([identity, word["start"], word["end"]])
            previous = identity
        for identity, start, end in groups:
            stop = min(end, start + REFERENCE_SECONDS)
            anchor = pcm[int(start * RATE) * 2:int(stop * RATE) * 2]
            if len(anchor) > len(self.anchors.get(identity, b"")):
                self.anchors[identity] = anchor
