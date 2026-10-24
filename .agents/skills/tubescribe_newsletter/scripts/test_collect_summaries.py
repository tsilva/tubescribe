"""Exercise newsletter source selection with isolated archives."""

import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


SCRIPT = Path(__file__).with_name('collect_summaries.py')
spec = importlib.util.spec_from_file_location('collector', SCRIPT)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
END = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / 'archive.sqlite3'
        self.db = sqlite3.connect(self.database)
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE channels(id TEXT,title TEXT,url TEXT);
            CREATE TABLE videos(id TEXT,channel_id TEXT,title TEXT,url TEXT,published_at TEXT);
            CREATE TABLE jobs(id INTEGER,video_id TEXT,profile TEXT);
            CREATE TABLE artifacts(job_id INTEGER,workflow TEXT,state TEXT,output_dir TEXT,updated_at TEXT);
            INSERT INTO channels VALUES('a','Alpha','https://example.com/a'),('b','Beta','https://example.com/b');
        ''')

    def video(self, identity, date, channel='a'):
        self.db.execute('INSERT INTO videos VALUES(?,?,?,?,?)',
                        (identity, channel, identity, f'https://www.youtube.com/watch?v={identity}', date))
        self.db.commit()

    def summary(self, video, job, text, updated='2026-10-14T11:00:00Z', location=None, objects=None):
        location = location or f'artifacts/{video}/p{job}/summary/bundle'
        self.db.execute('INSERT INTO jobs VALUES(?,?,?)', (job, video, f'p{job}'))
        self.db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)',
                        (job, 'summary', 'complete', location, updated))
        self.db.commit()
        if text is not None:
            path = (objects or self.root) / location / 'artifact.md'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def collect(self, objects=None):
        return collector.collect(self.root, self.database, objects or self.root, 7, END, ZoneInfo('Europe/Lisbon'))

    def test_publication_window_deduplicates_and_covers_all_channels(self):
        self.video('start', str(datetime(2026, 10, 7, 12, tzinfo=timezone.utc).timestamp()))
        self.video('end', '2026-10-14T13:00:00+01:00', 'b')
        self.video('old', '2026-10-07T11:59:59Z')
        self.video('future', '2026-10-14T12:00:01Z')
        self.video('undated', 'bad')
        self.summary('start', 1, 'Older summary', '2026-10-08T12:00:00Z')
        self.summary('start', 2, 'Newest readable summary')
        self.summary('start', 3, None, '2026-10-14T11:30:00Z')
        self.summary('end', 4, 'Beta insight')
        self.summary('old', 5, 'Newly summarized old video')
        before = self.database.read_bytes()
        result = self.collect()
        self.assertEqual(result['coverage'], {
            'followed_channels': 2, 'in_window_videos': 2, 'summaries_used': 2,
            'missing_summaries': 0, 'unknown_date_videos': 1,
        })
        self.assertEqual({r['summary'] for r in result['summaries']}, {'Newest readable summary', 'Beta insight'})
        self.assertEqual(before, self.database.read_bytes())

    def test_gaps_do_not_read_transcripts_or_paths_outside_summary_storage(self):
        self.video('pending', '2026-10-13T12:00:00Z')
        self.video('empty', '2026-10-13T12:00:00Z')
        self.video('escape', '2026-10-13T12:00:00Z')
        self.summary('empty', 1, '')
        self.summary('escape', 2, 'Transcript sentinel', location='transcripts/escape/summary')
        reads = []
        original = Path.read_text
        def read(path, *args, **kwargs):
            reads.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            result = self.collect()
        self.assertEqual(result['coverage']['missing_summaries'], 3)
        self.assertEqual(result['summaries'], [])
        self.assertTrue(all(path.is_relative_to(self.root / 'artifacts') for path in reads))

    def test_directory_objects_and_empty_window(self):
        self.assertEqual(self.collect()['coverage']['in_window_videos'], 0)
        objects = self.root / 'object-store'
        self.video('external', '2026-10-13T12:00:00Z')
        self.summary('external', 1, 'Saved directory summary', objects=objects)
        self.assertEqual(self.collect(objects)['summaries'][0]['summary'], 'Saved directory summary')

    def test_cli_refuses_missing_database_bad_days_and_network_fallback(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('TUBESCRIBE_')}
        for arguments, extra in [
            (['--archive', str(self.root / 'missing')], {}),
            (['--days', '0'], {}),
            (['--as-of', '2026-10-14'], {}),
            ([], {'TUBESCRIBE_DATABASE_URL': 'postgresql://example.invalid/archive'}),
        ]:
            result = subprocess.run([sys.executable, str(SCRIPT), *arguments],
                                    env={**env, **extra}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')
        self.assertFalse((self.root / 'missing').exists())


if __name__ == '__main__':
    unittest.main()
