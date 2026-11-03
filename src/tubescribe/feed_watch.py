"""Incremental public-feed discovery feeding the existing durable video pipeline."""

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import youtube
from .execution import LeaseLost, assert_owned, capacity, defer_downloads, lease
from .pipeline import Plan
from .recent import ensure_recent, publication_time
from .submission import Dispatcher, JobRequest


def fetch_feed(channel_id):
    playlist = "UU" + channel_id[2:]
    data = youtube.run(
        [
            "--flat-playlist",
            "--dump-single-json",
            "--skip-download",
            "--playlist-end",
            "15",
            "--extractor-args",
            "youtubetab:approximate_date",
            "https://www.youtube.com/playlist?list=" + playlist,
        ],
        timeout=180,
    )
    if data.get("id") != playlist or data.get("channel_id", channel_id) != channel_id:
        raise ValueError("Publication feed channel identity mismatch")
    videos = {}
    for entry in data.get("entries") or []:
        identifier = entry.get("id") or ""
        if (
            not youtube.ID.fullmatch(identifier)
            or entry.get("channel_id", channel_id) != channel_id
        ):
            raise ValueError("Publication feed entry identity mismatch")
        stamp = publication_time(entry.get("timestamp") or entry.get("upload_date"))
        videos[identifier] = {
            "id": identifier,
            "title": entry.get("title") or identifier,
            "url": "https://www.youtube.com/watch?v=" + identifier,
            "published_at": str(stamp) if stamp is not None else None,
            "duration": entry.get("duration"),
            "live_status": entry.get("live_status"),
            "kind": "stream"
            if entry.get("was_live") or entry.get("live_status") == "was_live"
            else "upload",
        }
    return list(videos.values())


def poll_feeds(store, endpoint, *, profile="turbo-v1", days=7, bridge=None, clock=None):
    """One bounded poll; completed/exhausted jobs never trigger new metadata/inference."""
    if days < 1:
        raise ValueError("Days must be positive")
    end = clock or datetime.now(UTC)
    cutoff = (end - timedelta(days=days)).timestamp()
    result = {"checked_at": end.isoformat(), "videos": [], "errors": [], "reconciled": []}
    selected = set()
    catalog_only = store.config.manual_queue

    def reconcile(source):
        store.discovery(source["id"], "feed-reconcile", "active")
        try:
            recovery = ensure_recent(
                store,
                endpoint,
                days=days,
                channel=source["id"],
                profile=profile,
                enqueue_only=True,
                bridge=bridge,
                clock=end,
                catalog_only=catalog_only,
            )
        except BaseException as error:
            store.discovery(source["id"], "feed-reconcile", "failed", error=str(error))
            raise
        failed = bool(recovery["discovery_errors"] or recovery["rate_limited"])
        store.discovery(source["id"], "feed-reconcile", "failed" if failed else "complete")
        result["reconciled"].append(source["id"])
        result["errors"].extend(recovery["discovery_errors"])
        if recovery["rate_limited"]:
            defer_downloads(store.root)
        return recovery["rate_limited"]

    with lease(store.root, "video:feed-discovery"):
        pending_gaps = {
            r[0]
            for r in store.db.execute(
                "SELECT channel_id FROM discovery WHERE kind='feed-reconcile' AND state IN ('active','failed')"
            )
        }
        covered = set()
        if not catalog_only:
            for pending in store.db.execute(
                "SELECT request FROM submissions WHERE state IN ('queued','active')"
            ):
                request = json.loads(pending[0])
                if request["options"].get("profile", "turbo-v1") == profile:
                    covered.update(request["options"].get("video") or [])
        for source in store.channels():
            try:
                if not catalog_only and source["id"] in pending_gaps:
                    if reconcile(source):
                        break
                    continue
                with capacity(store.root, "download"):
                    feed = fetch_feed(source["id"])
                if not feed:
                    continue
                ids = [v["id"] for v in feed]
                if catalog_only:
                    known = {
                        row[0]
                        for row in store.db.execute(
                            "SELECT id FROM videos WHERE id IN ("
                            + ",".join("?" for _ in ids) + ")", ids
                        )
                    }
                    # The uploads listing already carries catalogue metadata. Keep the
                    # watcher bounded even when a channel has a large unindexed history.
                    additions = {"upload": [], "stream": []}
                    for video in feed:
                        stamp = publication_time(video["published_at"])
                        if video["id"] in known or video.get("live_status") in (
                            "is_live", "is_upcoming", "post_live"
                        ):
                            continue
                        if stamp is not None and not cutoff - 86400 <= stamp <= end.timestamp():
                            continue
                        additions[video.get("kind", "upload")].append(video)
                    for kind, videos in additions.items():
                        if videos:
                            store.index(source["id"], kind, videos, None)
                            selected.update(video["id"] for video in videos)
                    continue
                known = {
                    r["video_id"]: r
                    for r in store.pipeline_jobs(profile=profile, ids=ids)
                }
                # Feeds expose only the latest 15 publications. An overlap anchors
                # normal incremental polling; a full unanchored recent feed needs
                # authoritative all-tab reconciliation, rather than silently skipping.
                dates = [publication_time(v["published_at"]) for v in feed]
                prefetched = {}
                gap = (
                    len(feed) >= 15
                    and not known
                    and not any(stamp is not None and stamp + 86400 < cutoff for stamp in dates)
                )
                if gap and not all(stamp is not None and stamp >= cutoff for stamp in dates):
                    with capacity(store.root, "download"):
                        tail = youtube.details(feed[-1]["id"])
                    if tail.get("channel_id") != source["id"]:
                        raise ValueError("Watch-page channel identity mismatch")
                    prefetched[feed[-1]["id"]] = tail
                    tail_date = publication_time(tail.get("timestamp"))
                    gap = tail_date is None or tail_date >= cutoff
                if gap:
                    if reconcile(source):
                        break
                    continue
                for video in feed:
                    if video["id"] in covered:
                        continue
                    row = known.get(video["id"])
                    stamp = publication_time(row.get("published_at")) if row else None
                    if stamp is not None and not cutoff <= stamp <= end.timestamp():
                        continue
                    approximate = publication_time(video["published_at"])
                    if stamp is None and approximate is not None and approximate + 86400 < cutoff:
                        continue
                    if row:
                        if row["channel_id"] != source["id"]:
                            raise ValueError("Indexed channel identity mismatch")
                        if Plan().complete(row) or Plan().due(row) is None:
                            continue
                        if stamp is not None and (
                            row.get("audio_path") or row.get("asr_state") == "complete"
                        ):
                            selected.add(video["id"])
                            continue
                    try:
                        info = prefetched.get(video["id"])
                        if info is None:
                            with capacity(store.root, "download"):
                                info = youtube.details(video["id"])
                        if (
                            info.get("id", video["id"]) != video["id"]
                            or info.get("channel_id") != source["id"]
                        ):
                            raise ValueError("Watch-page identity mismatch")
                        if info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
                            continue
                        stamp = publication_time(info.get("timestamp"))
                        if stamp is None:
                            raise ValueError("Watch-page exact publication date unavailable")
                        if stamp > end.timestamp():
                            continue
                        kind = "stream" if info.get("was_live") else "upload"
                        store.index(
                            source["id"],
                            kind,
                            [
                                {
                                    **video,
                                    "published_at": str(stamp),
                                    "title": info.get("title") or video["title"],
                                    "duration": info.get("duration"),
                                }
                            ],
                            profile,
                        )
                        if stamp >= cutoff:
                            selected.add(video["id"])
                    except LeaseLost:
                        raise
                    except Exception as error:
                        if isinstance(error, youtube.YouTubeError) and error.kind == "rate_limit":
                            raise
                        result["errors"].append(
                            {
                                "channel_id": source["id"],
                                "video_id": video["id"],
                                "error": str(error),
                            }
                        )
            except LeaseLost:
                raise
            except Exception as error:
                result["errors"].append({"channel_id": source["id"], "error": str(error)})
                if isinstance(error, youtube.YouTubeError) and error.kind == "rate_limit":
                    defer_downloads(store.root)
                    break
        if catalog_only:
            result["videos"] = sorted(selected)
            return result
        # Avoid overlapping requests as well as exact submission deduplication.
        for row in store.db.execute(
            "SELECT request FROM submissions WHERE state IN ('queued','active')"
        ):
            request = json.loads(row[0])
            if (
                request["command"] in {"work", "diarize", "postprocess", "artifacts"}
                and request["options"].get("profile", "turbo-v1") == profile
            ):
                covered = request["options"].get("video")
                if covered:
                    selected.difference_update(covered)
                elif request["options"].get("channel"):
                    selected.difference_update(
                        r["video_id"]
                        for r in store.pipeline_jobs(
                            profile=profile, channel=request["options"]["channel"]
                        )
                    )
                else:
                    selected.clear()
        result["videos"] = sorted(selected)
        if selected:
            result["submission"] = Dispatcher(store).submit(
                JobRequest(
                    "work",
                    {
                        "video": sorted(selected),
                        "profile": profile,
                        "wait": True,
                        "endpoint": endpoint,
                        "bridge": bridge,
                    },
                )
            )
    return result


def watch_feeds(root, endpoint, *, profile="turbo-v1", days=7, bridge=None,
                interval=900, on_poll=None, stop=None, clock=None):
    """Reopen ownership and connections for each poll, including after sleep/loss."""
    from .blobs import atomic_write
    from .storage import Store

    if interval < 30:
        raise ValueError("Feed watch interval must be at least 30 seconds")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    checkpoint = root / "last-poll.json"
    stop = stop or threading.Event()
    clock = clock or (lambda: datetime.now(UTC))

    def deadline():
        try:
            value = datetime.fromisoformat(json.loads(checkpoint.read_text())["next_poll_at"])
            return value if value.tzinfo else datetime.min.replace(tzinfo=UTC)
        except (OSError, ValueError, KeyError, TypeError):
            return datetime.min.replace(tzinfo=UTC)

    while not stop.is_set():
        delay = max(0, min(interval, (deadline() - clock()).total_seconds()))
        if delay and stop.wait(delay):
            break
        started = clock()
        store = None
        try:
            store = Store(root)
            # Do not hold ownership or a database session across the idle interval.
            with lease(root, "feed-watcher", reentrant=False):
                # Another supervised instance may already have completed this poll.
                if deadline() > clock():
                    continue
                result = poll_feeds(
                    store, endpoint, profile=profile, days=days, bridge=bridge, clock=started
                )
                with store.db:
                    assert_owned(store.db, root)
                due = max(started + timedelta(seconds=interval), clock() + timedelta(seconds=30))
                result["next_poll_at"] = due.isoformat()
                atomic_write(checkpoint, json.dumps(result, indent=2).encode())
        except Exception as error:
            # The lease context has unwound before retrying. In particular, never
            # continue using thread-local fencing for an expired watcher owner.
            result = {
                "checked_at": clock().isoformat(),
                "videos": [],
                "errors": [{"error": str(error)}],
                "next_poll_at": (clock() + timedelta(seconds=30)).isoformat(),
            }
            # A competing poller owns the shared checkpoint; don't postpone its work.
            from .execution import ClaimBusy

            if isinstance(error, ClaimBusy):
                if stop.wait(30):
                    break
                continue
            if deadline() <= started:
                atomic_write(checkpoint, json.dumps(result, indent=2).encode())
        finally:
            if store is not None:
                store.db.close()
        if on_poll:
            on_poll(result)
