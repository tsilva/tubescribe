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
from .submission import execute_cli
from .tunnel import tunnel
from .worker import work


def out(data):
    print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)


def main():
    p = argparse.ArgumentParser(description="Local YouTube archive using remote ASR")
    p.add_argument("--data", type=Path, default=Path(os.environ.get("TUBESCRIBE_DATA", "data")))
    p.add_argument("--endpoint", default=os.environ.get("TUBESCRIBE_ENDPOINT"))
    p.add_argument("--submission", help=argparse.SUPPRESS)
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
    c.add_argument("--postprocessing", choices=["video", "text", "off"], default="video")
    c.add_argument("--no-summary", action="store_true", help="Stop after processed Markdown")
    c.add_argument(
        "--bridge", help="Loopback AgentBridge URL (defaults to saved config or port 8082)"
    )
    c.add_argument("--video-cache", help="Read-only local or ssh://HOST/path video cache directory")
    c = sub.add_parser("postprocess", help="Finish existing diarized transcripts without ASR")
    c.add_argument("--video", action="append")
    c.add_argument("--channel")
    c.add_argument("--profile", default="turbo-v1")
    c.add_argument("--limit", type=int)
    c.add_argument("--max-attempts", type=int, default=3)
    c.add_argument("--wait", action="store_true")
    c.add_argument("--mode", choices=["video", "text"], default="video")
    c.add_argument("--no-summary", action="store_true", help="Stop after processed Markdown")
    c.add_argument("--bridge")
    c.add_argument("--video-cache")
    c.add_argument(
        "--force",
        action="store_true",
        help="Regenerate selected outputs, preserving earlier bundles",
    )
    c = sub.add_parser(
        "artifacts", help="Build or retry artifacts from completed processed Markdown"
    )
    c.add_argument("--workflow", default="summary")
    c.add_argument("--video", action="append")
    c.add_argument("--profile", default="turbo-v1")
    c.add_argument("--limit", type=int)
    c.add_argument("--bridge")
    c.add_argument("--force", action="store_true")
    c.add_argument("--max-attempts", type=int, default=3)
    c.add_argument(
        "--wait", action="store_true", help="Wait for an active archive worker to finish"
    )
    c.add_argument("--list-workflows", action="store_true")
    c = sub.add_parser("status")
    c.add_argument("--failures", action="store_true")
    c = sub.add_parser("retry")
    c.add_argument("--video", action="append")
    c = sub.add_parser("export")
    c.add_argument("destination", type=Path)
    c = sub.add_parser("subtitles", help="Generate/export SRT and Markdown speaker turns")
    c.add_argument(
        "destination",
        type=Path,
        nargs="?",
        help="Optional directory for VIDEO.PROFILE.srt/.md files",
    )
    c.add_argument("--video", action="append")
    c.add_argument("--profile")
    sub.add_parser("health")
    c = sub.add_parser("diarize", help="Diarize saved ASR transcripts without rerunning ASR")
    c.add_argument("--video", action="append")
    c.add_argument("--profile", default="turbo-v1")
    c.add_argument("--max-attempts", type=int, default=3)
    c.add_argument("--wait", action="store_true")
    c = sub.add_parser(
        "diarization-tunnel", help="Forward diarization through the main SSH connection"
    )
    c.add_argument("action", choices=["start", "status", "stop"])
    c.add_argument("--local-port", type=int, default=18768)
    c.add_argument("--remote-port", type=int, default=18767)
    c = sub.add_parser("dashboard", help="Local admin dashboard for monitoring and file browsing")
    c.add_argument(
        "action", nargs="?", default="start", choices=["start", "status", "stop", "serve"]
    )
    c.add_argument("--port", default="auto", help="Local port, or auto for an available port")
    c = sub.add_parser("tunnel")
    c.add_argument("action", choices=["start", "stop", "status"])
    c.add_argument("--host", default="tubescribe-beast")
    c.add_argument("--local-port", type=int, default=18766)
    c.add_argument("--remote-port", type=int, default=18765)
    args = p.parse_args()
    if args.command == "dashboard":
        try:
            args.port = 0 if args.port == "auto" else int(args.port)
            if not 0 <= args.port <= 65535:
                raise ValueError()
        except ValueError:
            p.error("Dashboard port must be auto or an integer from 0 to 65535")
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
        if args.command == "diarization-tunnel":
            from .diarization_tunnel import forward

            out(forward(store.root, args.action, args.local_port, args.remote_port))
        elif args.command == "postprocess":
            from .postprocessing_worker import run as postprocess

            def stop_postprocessing(signum, frame):
                raise KeyboardInterrupt()

            signal.signal(signal.SIGTERM, stop_postprocessing)
            result = execute_cli(
                store,
                args,
                lambda: postprocess(
                    store,
                    bridge=args.bridge,
                    ids=args.video,
                    channel=args.channel,
                    profile=args.profile,
                    limit=args.limit,
                    max_attempts=args.max_attempts,
                    wait=args.wait,
                    mode=args.mode,
                    video_cache=args.video_cache,
                    force=args.force,
                    summary=not args.no_summary,
                ),
            )
            out(result)
            return int(result["failed"] > 0)
        elif args.command == "artifacts":
            from .artifacts import run, workflows

            if args.list_workflows:
                out(workflows(store.root))
            else:
                result = execute_cli(
                    store,
                    args,
                    lambda: run(
                        store,
                        name=args.workflow,
                        ids=args.video,
                        profile=args.profile,
                        limit=args.limit,
                        bridge=args.bridge,
                        force=args.force,
                        max_attempts=args.max_attempts,
                        wait=args.wait,
                    ),
                )
                out(result)
                return int(result["failed"] > 0)
        elif args.command == "diarize":
            from .diarization_tunnel import endpoint as diarization_endpoint
            from .diarization_tunnel import ready
            from .diarization_worker import run as diarize

            def stop_diarization(signum, frame):
                raise KeyboardInterrupt()

            signal.signal(signal.SIGTERM, stop_diarization)
            ready(store.root)
            out(
                execute_cli(
                    store,
                    args,
                    lambda: diarize(
                        store,
                        diarization_endpoint(store.root),
                        ids=args.video,
                        profile=args.profile,
                        max_attempts=args.max_attempts,
                        wait=args.wait,
                    ),
                )
            )
        elif args.command == "dashboard":
            from .dashboard import dashboard

            result = dashboard(store.root, args.action, args.port)
            if result is not None:
                out(result)
        elif args.command == "channels":
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
            result = execute_cli(
                store,
                args,
                lambda: work(
                    store,
                    endpoint(),
                    ids=args.video,
                    channel=args.channel,
                    profile=args.profile,
                    limit=args.limit,
                    max_attempts=args.max_attempts,
                    wait=args.wait,
                    keep_audio=args.keep_audio,
                    postprocessing=args.postprocessing,
                    bridge=args.bridge,
                    video_cache=args.video_cache,
                    summary=not args.no_summary,
                ),
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
                    "diarization": [
                        dict(r)
                        for r in store.db.execute(
                            "SELECT j.profile,d.state,d.stage,COUNT(*) AS count FROM diarizations d JOIN jobs j ON j.id=d.job_id GROUP BY j.profile,d.state,d.stage"
                        )
                    ],
                    "completed_jobs": store.db.execute(
                        "SELECT COUNT(*) FROM jobs j JOIN diarizations d ON d.job_id=j.id JOIN postprocessings p ON p.job_id=j.id JOIN artifacts a ON a.job_id=j.id AND a.workflow='summary' WHERE j.state='complete' AND d.state='complete' AND p.state='complete' AND a.state='complete'"
                    ).fetchone()[0],
                    "awaiting_diarization": store.db.execute(
                        "SELECT COUNT(*) FROM jobs j LEFT JOIN diarizations d ON d.job_id=j.id WHERE j.state='complete' AND COALESCE(d.state,'pending')!='complete'"
                    ).fetchone()[0],
                    "postprocessing": [
                        dict(r)
                        for r in store.db.execute(
                            "SELECT j.profile,p.state,p.stage,COUNT(*) AS count FROM postprocessings p JOIN jobs j ON j.id=p.job_id GROUP BY j.profile,p.state,p.stage"
                        )
                    ],
                    "awaiting_postprocessing": store.db.execute(
                        "SELECT COUNT(*) FROM jobs j JOIN diarizations d ON d.job_id=j.id LEFT JOIN postprocessings p ON p.job_id=j.id WHERE j.state='complete' AND d.state='complete' AND COALESCE(p.state,'pending')!='complete'"
                    ).fetchone()[0],
                    "artifacts": [
                        dict(r)
                        for r in store.db.execute(
                            "SELECT j.profile,a.workflow,a.state,a.stage,COUNT(*) AS count FROM artifacts a JOIN jobs j ON j.id=a.job_id GROUP BY j.profile,a.workflow,a.state,a.stage"
                        )
                    ],
                    "awaiting_summary": store.db.execute(
                        "SELECT COUNT(*) FROM jobs j JOIN postprocessings p ON p.job_id=j.id LEFT JOIN artifacts a ON a.job_id=j.id AND a.workflow='summary' WHERE p.state='complete' AND COALESCE(a.state,'pending')!='complete'"
                    ).fetchone()[0],
                }
            )
            if args.failures:
                out(
                    [
                        dict(r)
                        for r in store.db.execute(
                            "SELECT j.video_id,j.profile,a.workflow,a.stage AS failed_stage,a.error,a.attempts,a.save_attempts,a.next_retry FROM artifacts a JOIN jobs j ON j.id=a.job_id WHERE a.state='failed'"
                        )
                    ]
                )
                out(
                    [
                        dict(r)
                        for r in store.db.execute(
                            "SELECT video_id,profile,failed_stage,error_kind,error,download_attempts,asr_attempts,next_retry,audio_path FROM jobs WHERE state='failed'"
                        )
                    ]
                )
                out(
                    [
                        dict(r)
                        for r in store.db.execute(
                            "SELECT j.video_id,j.profile,p.stage AS failed_stage,p.error,p.download_attempts,p.attempts,p.save_attempts,p.next_retry FROM postprocessings p JOIN jobs j ON j.id=p.job_id WHERE p.state='failed'"
                        )
                    ]
                )
                out(
                    [
                        dict(r)
                        for r in store.db.execute(
                            "SELECT j.video_id,j.profile,d.stage AS failed_stage,d.error,d.download_attempts,d.attempts AS diarization_attempts,d.save_attempts,d.next_retry,d.audio_path FROM diarizations d JOIN jobs j ON j.id=d.job_id WHERE d.state='failed'"
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
                out({"formats": ["srt", "md"], "count": len(paths), "files": paths})
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
                    target.execute("DELETE FROM claims")
                    target.execute(
                        "UPDATE submissions SET state='interrupted',pid=NULL WHERE state IN ('queued','active')"
                    )
                if (store.root / "transcripts").exists():
                    shutil.copytree(store.root / "transcripts", dest / "transcripts")
                if (store.root / "audio").exists():
                    shutil.copytree(store.root / "audio", dest / "audio")
                if (store.root / "artifacts").exists():
                    shutil.copytree(store.root / "artifacts", dest / "artifacts")
                if (store.root / "workflows").exists():
                    shutil.copytree(store.root / "workflows", dest / "workflows")
                from .publication import export_publications

                export_publications(dest)
                out(
                    {
                        "exported_to": str(dest),
                        "includes": "SQLite, transcripts, artifacts, workflow definitions, retained retry audio",
                    }
                )
    except KeyboardInterrupt:
        message = (
            "Dashboard stopped."
            if args.command == "dashboard"
            else "Interrupted; stage state preserved. Rerun work to resume."
        )
        print(message, file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        return 1
    finally:
        store.db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
