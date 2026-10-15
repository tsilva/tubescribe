#!/usr/bin/env python3
"""Collect saved TubeScribe summaries without reading transcripts or mutating storage."""

import argparse
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_ARCHIVE = Path(__file__).resolve().parents[4] / 'data'


def timestamp(value):
    if value is None or str(value).strip() == '':
        return None
    try:
        number = float(value)
        if not math.isfinite(number):
            return None
        return datetime.fromtimestamp(number, timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except ValueError:
        return None


def collect(archive, database, objects, days, end, zone):
    if days < 1 or end.tzinfo is None:
        raise ValueError('Days must be positive and the window end must have a timezone')
    end = end.astimezone(timezone.utc)
    start = end - timedelta(days=days)
    database = database.resolve()
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=3) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        channels = [dict(row) for row in db.execute('SELECT id,title,url FROM channels ORDER BY title,id')]
        rows = [dict(row) for row in db.execute('''
            SELECT v.id AS video_id,v.channel_id,v.title,v.url,v.published_at,
                   j.profile,a.state AS summary_state,a.output_dir AS summary_dir,
                   a.updated_at AS summary_updated_at
            FROM videos v JOIN channels c ON c.id=v.channel_id
            LEFT JOIN jobs j ON j.video_id=v.id
            LEFT JOIN artifacts a ON a.job_id=j.id AND a.workflow='summary'
        ''')]
    names = {channel['id']: channel['title'] for channel in channels}
    videos, unknown = {}, {}
    for row in rows:
        published = timestamp(row['published_at'])
        metadata = {key: row[key] for key in ('video_id', 'channel_id', 'title', 'url')}
        metadata['channel_title'] = names[row['channel_id']]
        if published is None:
            unknown[row['video_id']] = {**metadata, 'published_at': row['published_at']}
        elif start <= published <= end:
            video = videos.setdefault(row['video_id'], {
                **metadata, 'published_at': published.astimezone(zone).isoformat(),
                'candidates': [],
            })
            video['candidates'].append(row)

    summaries, missing = [], []
    object_root = objects.resolve()
    artifact_root = (object_root / 'artifacts').resolve()
    for video in sorted(videos.values(), key=lambda v: (timestamp(v['published_at']), v['video_id']), reverse=True):
        candidates = video.pop('candidates')
        candidates.sort(key=lambda r: (
            timestamp(r['summary_updated_at']) or datetime.min.replace(tzinfo=timezone.utc),
            r['profile'] or '',
        ), reverse=True)
        issues = []
        for row in candidates:
            if row['summary_state'] != 'complete' or not row['summary_dir']:
                continue
            relative = Path(row['summary_dir'])
            path = (object_root / relative / 'artifact.md').resolve()
            if (relative.is_absolute() or '..' in relative.parts
                    or 'summary' not in relative.parts or not path.is_relative_to(artifact_root)):
                issues.append('Summary path is outside the recognized summary artifact location')
                continue
            try:
                content = path.read_text(encoding='utf-8')
                if not content.strip():
                    raise ValueError('Summary file is empty')
            except (OSError, UnicodeError, ValueError) as exc:
                issues.append(f'{row["profile"]}: {exc}')
                continue
            summaries.append({
                **video, 'profile': row['profile'], 'summary_path': str(path),
                'summary_updated_at': row['summary_updated_at'], 'summary': content,
            })
            break
        else:
            missing.append({**video, 'reason': '; '.join(issues) if issues else 'No completed saved summary'})

    in_window = {channel['id']: 0 for channel in channels}
    used = dict(in_window)
    for video in videos.values():
        in_window[video['channel_id']] += 1
    for summary in summaries:
        used[summary['channel_id']] += 1
    for channel in channels:
        channel['in_window_videos'] = in_window[channel['id']]
        channel['summaries_used'] = used[channel['id']]
        channel['unknown_date_videos'] = sum(v['channel_id'] == channel['id'] for v in unknown.values())
    return {
        'window': {'days': days, 'start': start.astimezone(zone).isoformat(),
                   'end': end.astimezone(zone).isoformat(), 'timezone': str(zone),
                   'selection': 'video publication time; inclusive endpoints'},
        'archive': str(archive.resolve()), 'database': str(database),
        'coverage': {'followed_channels': len(channels), 'in_window_videos': len(videos),
                     'summaries_used': len(summaries), 'missing_summaries': len(missing),
                     'unknown_date_videos': len(unknown)},
        'coverage_note': 'Indexed videos only; discovery and summary generation were not run.',
        'channels': channels, 'summaries': summaries, 'missing_summaries': missing,
        'unknown_date_videos': sorted(unknown.values(), key=lambda v: (v['channel_title'], v['video_id'])),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--archive', type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--objects', type=Path)
    parser.add_argument('--timezone', default='Europe/Lisbon')
    parser.add_argument('--as-of', help='ISO 8601 timestamp with an explicit timezone')
    args = parser.parse_args()
    try:
        if args.days < 1:
            raise ValueError('--days must be a positive integer')
        if os.environ.get('TUBESCRIBE_DATABASE_URL') or os.environ.get('TUBESCRIBE_S3_BUCKET'):
            raise ValueError('Network/S3 storage selected: collect summaries through the existing dashboard')
        zone = ZoneInfo(args.timezone)
        end = timestamp(args.as_of) if args.as_of else datetime.now(timezone.utc)
        if end is None:
            raise ValueError('--as-of must be an ISO timestamp with an explicit timezone')
        database = args.database or Path(os.environ.get('TUBESCRIBE_DATABASE', args.archive / 'archive.sqlite3'))
        objects = args.objects or Path(os.environ.get('TUBESCRIBE_OBJECTS', args.archive))
        packet = collect(args.archive, database, objects, args.days, end, zone)
    except (OSError, ValueError, sqlite3.Error, KeyError, OverflowError) as exc:
        parser.exit(1, f'Cannot collect summaries: {exc}\n')
    print(json.dumps(packet, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
