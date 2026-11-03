from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import youtube
from .asr import Client
from .config import default_data
from .storage import Store, worker_lock
from .submission import execute_cli
from .tunnel import tunnel
from .worker import work


def out(data):
    print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)


def main():
    p = argparse.ArgumentParser(description="Local YouTube archive using remote ASR")
    p.add_argument("--data", type=Path, default=default_data(), help="Archive/scratch directory")
    p.add_argument("--endpoint", default=os.environ.get("TUBESCRIBE_ENDPOINT"))
    p.add_argument("--submission", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("migrate", help="Inspect or explicitly upgrade the database schema")
    c.add_argument("action", choices=["status", "apply"])
    c = sub.add_parser("queue", help="Durable worker queue")
    c.add_argument("action", choices=["serve", "once", "submit", "status"])
    c.add_argument("--request", type=Path, help="JSON JobRequest for submit")
    c = sub.add_parser("reconcile", help="Repair expired work and report orphaned bundles")
    c.add_argument("--purge", action="store_true")
    c.add_argument("--retention-days", type=int, default=7)
    c = sub.add_parser("agent", help="Durable gateway beside a synchronous AgentBridge")
    c.add_argument("action", choices=["serve", "status", "cancel"])
    c.add_argument("--upstream", default="http://127.0.0.1:8082")
    c.add_argument("--port", type=int, default=8083)
    c.add_argument("--run-id")
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
    c = sub.add_parser("ensure-recent", help="Fully process tracked channels' recent videos")
    c.add_argument("channel", nargs="?")
    c.add_argument("--days", type=int, default=7)
    c.add_argument("--profile", default="turbo-v1")
    c.add_argument("--max-attempts", type=int, default=3)
    c.add_argument("--enqueue-only", action="store_true", help="Submit work for queue serve")
    c.add_argument("--bridge")
    c.add_argument("--video-cache")
    c = sub.add_parser(
        "feed-poll", help="Incrementally discover channel publications and queue full processing"
    )
    c.add_argument("--days", type=int, default=7)
    c.add_argument("--profile", default="turbo-v1")
    c.add_argument("--bridge")
    c.add_argument("--watch", action="store_true", help="Repeat with fresh per-poll ownership")
    c.add_argument("--interval", type=int, default=900, help="Watch interval in seconds (minimum 30)")
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
    c = sub.add_parser("rollups", help="Refresh rolling seven-day channel and library summaries")
    c.add_argument("--bridge")
    c.add_argument("--force", action="store_true")
    c = sub.add_parser("status")
    c.add_argument("--failures", action="store_true")
    c = sub.add_parser("retry")
    c.add_argument("--video", action="append")
    c = sub.add_parser("export")
    c.add_argument("destination", type=Path)
    c = sub.add_parser("restore", help="Restore a verified backup into an empty archive")
    c.add_argument("source", type=Path)
    c = sub.add_parser("operations", help="Durable-service readiness and ledger metrics")
    c.add_argument("action", choices=["ready", "metrics"])
    c = sub.add_parser("transfer", help="Local result-byte measurements and Neon quota warnings")
    c.add_argument("action", choices=["metrics", "observe"])
    c.add_argument("--project", default="twilight-dream-42009655")
    c.add_argument("--used-bytes", type=int)
    c.add_argument("--period-start", help="Provider billing-period start, ISO timestamp")
    c.add_argument("--period-end", help="Provider billing-period end, ISO timestamp")
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
    if args.command == "feed-poll" and args.interval < 30:
        p.error("Feed watch interval must be at least 30 seconds")
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
        or getattr(args, "days", 7) < 1
        or getattr(args, "max_attempts", 3) < 1
        or (getattr(args, "limit", None) is not None and args.limit < 1)
    ):
        p.error("Days, counts, limits and attempt bounds must be positive")
    if args.command == "transfer":
        from .transfer import metrics, observe

        if args.action == "observe" and any(
            value is None
            for value in (
                args.used_bytes,
                args.period_start,
                args.period_end,
            )
        ):
            p.error("transfer observe requires --used-bytes, --period-start and --period-end")
        try:
            out(
                metrics(args.data)
                if args.action == "metrics"
                else observe(
                    args.data,
                    project=args.project,
                    used_bytes=args.used_bytes,
                    period_start=args.period_start,
                    period_end=args.period_end,
                )
            )
            return 0
        except (ValueError, OSError) as error:
            p.error(str(error))
    if args.command == "operations":
        from .operations import metrics, readiness

        try:
            result = readiness(args.data) if args.action == "ready" else metrics(args.data)
            out(result)
            return int(result.get("ready") is False)
        except Exception:
            out({"error": "Durable services unavailable"})
            return 1
    if args.command == "migrate":
        from .migrations import manage

        try:
            result = manage(args.data, args.action)
            out(result)
            return int(not result["compatible"])
        except Exception as error:
            print(f"ERROR: {error}", file=sys.stderr, flush=True)
            return 1
    try:
        store = Store(args.data)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        return 1

    def stop_worker(signum, frame):
        raise KeyboardInterrupt()

    if args.command in {
        "work",
        "diarize",
        "postprocess",
        "artifacts",
        "rollups",
        "queue",
        "agent",
        "ensure-recent",
        "feed-poll",
    }:
        signal.signal(signal.SIGTERM, stop_worker)

    def endpoint():
        if store.config.manual_queue:
            return None
        if args.endpoint:
            return args.endpoint
        config = store.root / "tunnel.json"
        if config.exists():
            return json.loads(config.read_text())["endpoint"]
        return "http://127.0.0.1:18766"

    try:
        if args.command == "queue":
            from .submission import Dispatcher, JobRequest, consume

            if args.action == "submit":
                if store.config.manual_queue:
                    raise ValueError("Select videos in the library's Discovered view")
                if not args.request:
                    raise ValueError("queue submit requires --request")
                request = JobRequest(**json.loads(args.request.read_text()))
                out({"submission": Dispatcher(store).submit(request)})
            elif args.action == "status":
                out(store.submission_records())
            else:
                out(consume(store, once=args.action == "once"))
        elif args.command == "reconcile":
            from .maintenance import reconcile

            out(reconcile(store, retention_days=args.retention_days, purge=args.purge))
        elif args.command == "agent":
            from .agent_gateway import Runs, make_gateway

            if args.action == "serve":
                server = make_gateway(store.root, args.upstream, port=args.port)
                try:
                    out(
                        {
                            "url": f"http://{store.config.host}:{server.server_port}",
                            "protocol": "durable-v1",
                        }
                    )
                    server.serve_forever()
                finally:
                    server.server_close()
            else:
                if not args.run_id:
                    raise ValueError("agent status/cancel requires --run-id")
                with __import__("contextlib").closing(Runs(store.root)) as runs:
                    row = runs.get(args.run_id)
                    if not row:
                        raise ValueError("Run not found")
                    if args.action == "cancel":
                        runs.update(
                            args.run_id,
                            cancelled=1,
                            state="cancelled" if row["state"] == "queued" else row["state"],
                        )
                        row = runs.get(args.run_id)
                    out(
                        {
                            k: row[k]
                            for k in ["id", "state", "error", "cancelled", "created", "updated"]
                        }
                    )
        elif args.command == "diarization-tunnel":
            from .diarization_tunnel import forward

            out(forward(store.root, args.action, args.local_port, args.remote_port))
        elif args.command == "postprocess":
            from .postprocessing_worker import run as postprocess

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
        elif args.command == "rollups":
            from .rollups import run

            result = execute_cli(
                store, args, lambda: run(store, bridge=args.bridge, force=args.force)
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
                out(store.channels())
        elif args.command in ("sync", "latest"):
            channels = store.channels(args.channel)
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
                            store.index(channel["id"], kind, videos, None if store.config.manual_queue else args.profile)
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
                            store.index(channel["id"], kind, selected, None if store.config.manual_queue else args.profile)
                    out({"latest": [v for _, v in chosen]})
            if errors:
                return 1
        elif args.command == "feed-poll":
            from .feed_watch import poll_feeds, watch_feeds

            if args.watch:
                store.db.close()
                return watch_feeds(
                    args.data, endpoint(), profile=args.profile, days=args.days,
                    bridge=args.bridge, interval=args.interval, on_poll=out,
                )
            result = poll_feeds(
                store, endpoint(), profile=args.profile, days=args.days, bridge=args.bridge
            )
            out(result)
            return int(bool(result["errors"]))
        elif args.command == "ensure-recent":
            from .recent import ensure_recent

            result = ensure_recent(
                store,
                endpoint(),
                days=args.days,
                channel=args.channel,
                profile=args.profile,
                max_attempts=args.max_attempts,
                enqueue_only=args.enqueue_only,
                bridge=args.bridge,
                video_cache=args.video_cache,
            )
            out(result)
            return int(
                bool(
                    result["discovery_errors"]
                    or result.get("incomplete")
                    or result.get("processing", {}).get("failed")
                )
            )
        elif args.command == "work":
            if store.config.manual_queue:
                args.endpoint = None
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
            out(store.status_data())
            if args.failures:
                for rows in store.failures():
                    out(rows)
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
            from .backup import export_archive

            out(export_archive(store, args.destination))
        elif args.command == "restore":
            from .backup import restore_archive

            out(restore_archive(store, args.source))
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
