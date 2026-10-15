"""Discover a rolling publication window and submit the complete existing pipeline."""

import math
from datetime import UTC, datetime, timedelta

from . import youtube
from .pipeline import Plan
from .submission import Dispatcher, Inline, JobRequest
from .worker import work


def publication_time(value):
    if value is None or value == "":
        return None
    try:
        text = str(value)
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC).timestamp()
        stamp = float(value)
        return stamp if math.isfinite(stamp) and stamp > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def ensure_recent(
    store,
    endpoint,
    *,
    days=7,
    channel=None,
    profile="turbo-v1",
    max_attempts=3,
    enqueue_only=False,
    bridge=None,
    video_cache=None,
    clock=None,
):
    if days < 1 or max_attempts < 1:
        raise ValueError("Days and attempt bounds must be positive")
    channels = store.channels(channel)
    if not channels:
        raise ValueError("No matching channels; add one first")
    end = clock or datetime.now(UTC)
    cutoff = (end - timedelta(days=days)).timestamp()
    selected, date_only, errors, seen = set(), set(), [], set()
    rate_limited = False
    for source in channels:
        for kind in youtube.TABS:
            store.discovery(source["id"], kind, "active")
            tab_errors = []
            indexed = 0
            try:
                videos = youtube.discover(source["id"], kind)
                for video in videos:
                    key = (source["id"], video["id"])
                    if key in seen:
                        continue
                    seen.add(key)
                    # Flat dates can eliminate old entries, but never assume tab ordering:
                    # pinned uploads and old recordings may precede recent entries.
                    stamp = publication_time(video.get("published_at"))
                    if stamp is not None and stamp < cutoff - 86400:
                        continue
                    try:
                        info = youtube.details(video["id"])
                        if info.get("channel_id") != source["id"]:
                            raise ValueError("Channel identity mismatch")
                        if info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
                            continue
                        stamp = publication_time(info.get("timestamp"))
                        exact = stamp is not None
                        if stamp is None:
                            stamp = publication_time(info.get("upload_date"))
                        if stamp is None:
                            raise ValueError("Publication date unavailable")
                        # A date alone cannot resolve the boundary day's publication
                        # hour. Include that day conservatively rather than miss uploads.
                        in_window = stamp >= cutoff if exact else stamp + 86400 > cutoff
                        if not in_window or stamp > end.timestamp():
                            continue
                        store.index(
                            source["id"],
                            kind,
                            [
                                {
                                    **video,
                                    "published_at": str(stamp),
                                    "duration": info.get("duration"),
                                }
                            ],
                            profile,
                        )
                        selected.add(video["id"])
                        if not exact:
                            date_only.add(video["id"])
                        indexed += 1
                    except Exception as error:
                        tab_errors.append({"video_id": video["id"], "error": str(error)})
                        if isinstance(error, youtube.YouTubeError) and error.kind == "rate_limit":
                            rate_limited = True
                            break
            except Exception as error:
                tab_errors.append({"error": str(error)})
                rate_limited = (
                    isinstance(error, youtube.YouTubeError) and error.kind == "rate_limit"
                )
            store.discovery(
                source["id"],
                kind,
                "failed" if tab_errors else "complete",
                indexed,
                error="; ".join(item["error"] for item in tab_errors) or None,
            )
            errors.extend({"channel_id": source["id"], "kind": kind, **e} for e in tab_errors)
            if rate_limited:
                break
        if rate_limited:
            break
    result = {
        "days": days,
        "since": datetime.fromtimestamp(cutoff, UTC).isoformat(),
        "until": end.isoformat(),
        "videos": sorted(selected),
        "date_only_videos": sorted(date_only),
        "discovery_errors": errors,
        "rate_limited": rate_limited,
    }
    # Never let an empty selection become an unscoped worker request. Defer processing
    # altogether after a bot/rate-limit response instead of issuing more downloads.
    if not selected or rate_limited:
        return result
    request = JobRequest(
        "work",
        {
            "video": sorted(selected),
            "profile": profile,
            "wait": True,
            "max_attempts": max_attempts,
            "endpoint": endpoint,
            "bridge": bridge,
            "video_cache": video_cache,
        },
    )
    dispatcher = Dispatcher(store)
    identifier = dispatcher.submit(request)
    result["submission"] = identifier
    if enqueue_only:
        result["queued"] = True
        return result
    result["processing"] = dispatcher.execute(
        request,
        Inline(
            lambda: work(
                store,
                endpoint,
                ids=sorted(selected),
                profile=profile,
                wait=True,
                max_attempts=max_attempts,
                bridge=bridge,
                video_cache=video_cache,
            )
        ),
        identifier=identifier,
    )
    # An exhausted retry budget yields no eligible jobs; this is not success.
    result["incomplete"] = [
        {"video_id": job["video_id"], "stage": Plan().next_stage(job).name}
        for job in store.pipeline_jobs(profile=profile, ids=sorted(selected))
        if not Plan().complete(job)
    ]
    return result
