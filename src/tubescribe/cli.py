from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import youtube
from .asr import Client
from .storage import Store, worker_lock
from .tunnel import tunnel
from .worker import work


def out(data):
    print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)


def main():
    p = argparse.ArgumentParser(description="Local YouTube archive using remote ASR")
    p.add_argument("--data", type=Path, default=Path(os.environ.get("TUBESCRIBE_DATA", "data")))
    p.add_argument("--endpoint", default=os.environ.get("TUBESCRIBE_ENDPOINT"))
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("channels")
    cs = c.add_subparsers(dest="action", required=True)
    cs.add_parser("list")
    a = cs.add_parser("add")
    a.add_argument("url")
    for command in ("sync", "latest"):
        c = sub.add_parser(command)
        c.add_argument("channel", nargs="?")
        c.add_argument("--profile", default="turbo-v1")
        if command == "sync":
            c.add_argument("--limit", type=int)
        else:
            c.add_argument("--count", type=int, default=3)
    c = sub.add_parser("work")
    c.add_argument("--video", action="append")
    c.add_argument("--channel")
    c.add_argument("--limit", type=int)
    c.add_argument("--profile", default="turbo-v1")
    c.add_argument("--max-attempts", type=int, default=3)
    c.add_argument("--wait", action="store_true")
    c.add_argument(
        "--keep-audio", action="store_true", help="Retain successful audio for quality review"
    )
    c = sub.add_parser("status")
    c.add_argument("--failures", action="store_true")
    c = sub.add_parser("retry")
    c.add_argument("--video", action="append")
    c = sub.add_parser("export")
    c.add_argument("destination", type=Path)
    c = sub.add_parser("subtitles", help="Generate/export SubRip (.srt) from saved transcripts")
    c.add_argument(
        "destination", type=Path, nargs="?", help="Optional directory for VIDEO.PROFILE.srt files"
    )
    c.add_argument("--video", action="append")
    c.add_argument("--profile")
    sub.add_parser("health")
    c = sub.add_parser("tunnel")
    c.add_argument("action", choices=["start", "stop", "status"])
    c.add_argument("--host", default="tubescribe-beast")
    c.add_argument("--local-port", type=int, default=18766)
    c.add_argument("--remote-port", type=int, default=18765)
    args = p.parse_args()
    if getattr(args, "profile", None) and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.profile):
        p.error("Profile must be a path-safe label of letters, numbers, underscores or hyphens")
    if (
        getattr(args, "count", 3) < 1
        or getattr(args, "max_attempts", 3) < 1
        or (getattr(args, "limit", None) is not None and args.limit < 1)
    ):
        p.error("Counts, limits and attempt bounds must be positive")
    store = Store(args.data)

    def endpoint():
        if args.endpoint:
            return args.endpoint
        config = store.root / "tunnel.json"
        if config.exists():
            return json.loads(config.read_text())["endpoint"]
        return "http://127.0.0.1:18766"

    try:
        if args.command == "channels":
            if args.action == "add":
                channel = youtube.resolve(args.url)
                store.channel(channel)
                out(channel)
            else:
                out([dict(r) for r in store.db.execute("SELECT * FROM channels")])
        elif args.command in ("sync", "latest"):
            channels = store.db.execute(
                "SELECT * FROM channels" + (" WHERE id=?" if args.channel else ""),
                (args.channel,) if args.channel else (),
            ).fetchall()
            if not channels:
                raise ValueError("No matching channels; add one first")
            errors = 0
            for channel in channels:
                candidates = []
                for kind in youtube.TABS:
                    store.discovery(channel["id"], kind, "active")
                    try:
                        videos = youtube.discover(
                            channel["id"],
                            kind,
                            args.limit if args.command == "sync" else args.count,
                        )
                        if args.command == "latest":
                            for v in videos:
                                info = youtube.details(v["id"])
                                if info.get("live_status") in (
                                    "is_live",
                                    "is_upcoming",
                                    "post_live",
                                ):
                                    continue
                                if info.get("channel_id") != channel["id"]:
                                    raise ValueError("Channel identity mismatch")
                                v["published_at"] = str(
                                    info.get("timestamp")
                                    or datetime.strptime(info["upload_date"], "%Y%m%d")
                                    .replace(tzinfo=UTC)
                                    .timestamp()
                                )
                                v["duration"] = info.get("duration")
                                candidates.append((kind, v))
                        else:
                            store.index(channel["id"], kind, videos, args.profile)
                        if args.command == "latest":
                            store.discovery(channel["id"], kind, "complete", len(videos))
                        out({"channel_id": channel["id"], "kind": kind, "discovered": len(videos)})
                    except Exception as e:
                        store.discovery(channel["id"], kind, "failed", error=str(e))
                        errors += 1
                        out({"channel_id": channel["id"], "kind": kind, "error": str(e)})
                        if isinstance(e, youtube.YouTubeError) and e.kind == "rate_limit":
                            raise
                if args.command == "latest":
                    if errors:
                        raise RuntimeError(
                            "Could not verify all channel tabs; latest selection is incomplete"
                        )
                    chosen = sorted(
                        candidates, key=lambda pair: float(pair[1]["published_at"]), reverse=True
                    )[: args.count]
                    for kind in youtube.TABS:
                        selected = [v for k, v in chosen if k == kind]
                        if selected:
                            store.index(channel["id"], kind, selected, args.profile)
                    out({"latest": [v for _, v in chosen]})
            if errors:
                return 1
        elif args.command == "work":

            def stop(signum, frame):
                raise KeyboardInterrupt()

            signal.signal(signal.SIGTERM, stop)
            result = work(
                store,
                endpoint(),
                ids=args.video,
                channel=args.channel,
                profile=args.profile,
                limit=args.limit,
                max_attempts=args.max_attempts,
                wait=args.wait,
                keep_audio=args.keep_audio,
            )
            out(result)
            return int(result["failed"] > 0)
        elif args.command == "status":
            out(
                {
                    "jobs": [
                        dict(r)
                        for r in store.db.execute(
                            "SELECT profile,state,download_state,asr_state,count(*) AS count FROM jobs GROUP BY profile,state,download_state,asr_state"
                        )
                    ],
                    "discovery": [dict(r) for r in store.db.execute("SELECT * FROM discovery")],
                    "transcripts": dict(
                        store.db.execute(
                            "SELECT COUNT(*) AS count, SUM(duration) AS audio_seconds,SUM(processing_seconds) AS asr_seconds FROM transcripts"
                        ).fetchone()
                    ),
                }
            )
            if args.failures:
                out(
                    [
                        dict(r)
                        for r in store.db.execute(
                            "SELECT video_id,failed_stage,error_kind,error,download_attempts,asr_attempts,next_retry,audio_path FROM jobs WHERE state='failed'"
                        )
                    ]
                )
        elif args.command == "retry":
            with worker_lock(store.root):
                out({"requeued": store.retry(args.video)})
        elif args.command == "subtitles":
            with worker_lock(store.root):
                paths = store.subtitles(
                    ids=args.video,
                    profile=args.profile,
                    destination=args.destination.resolve() if args.destination else None,
                )
                out({"format": "srt", "count": len(paths), "files": paths})
        elif args.command == "health":
            out(Client(endpoint()).health())
        elif args.command == "tunnel":
            out(tunnel(store.root, args.action, args.host, args.local_port, args.remote_port))
        elif args.command == "export":
            dest = args.destination.resolve()
            if dest == store.root or store.root in dest.parents:
                raise ValueError("Export outside the live archive directory")
            # Consistent DB/files snapshot; no inference worker can delete/update files during export.
            with worker_lock(store.root):
                dest.mkdir(parents=True, exist_ok=False)
                store.subtitles()
                with sqlite3.connect(dest / "archive.sqlite3") as target:
                    store.db.backup(target)
                if (store.root / "transcripts").exists():
                    shutil.copytree(store.root / "transcripts", dest / "transcripts")
                if (store.root / "audio").exists():
                    shutil.copytree(store.root / "audio", dest / "audio")
                out(
                    {
                        "exported_to": str(dest),
                        "includes": "SQLite, transcripts, retained retry audio",
                    }
                )
    except KeyboardInterrupt:
        print("Interrupted; stage state preserved. Rerun work to resume.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        return 1
    finally:
        store.db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
