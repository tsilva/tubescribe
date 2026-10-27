"""Offline transfer accounting, query behavior, and billing-period warning guards."""

import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tubescribe.database import PostgreSQLConnection
from tubescribe.transfer import PROJECT, Meter, metrics, observe, operation


class Result:
    def __init__(self, rows):
        self.rows = rows
        self.ntuples = len(rows)
        self.nfields = len(rows[0]) if rows else 0

    def get_value(self, row, column):
        return self.rows[row][column]


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_records_buffered_values_without_fetching_or_logging_contents(self):
        secret = b"private transcript and credentials"
        cursor = Mock(pgresult=Result([[secret, None, "é".encode()], [b"", b"123", b"x"]]))
        output = io.StringIO()
        with patch("psycopg.connect") as connect, redirect_stderr(output):
            connect.return_value.execute.return_value = cursor
            db = PostgreSQLConnection("secret-url", root=self.root)

            @operation("agent.status")
            def status():
                return db.execute("SELECT private FROM agent_runs WHERE id=?", ("secret-id",))

            self.assertIs(status(), cursor)
            db.close()
        cursor.fetchone.assert_not_called()
        cursor.fetchall.assert_not_called()
        record = metrics(self.root)["operations"][0]
        self.assertEqual(record["result_bytes"], len(secret) + 6)
        self.assertEqual(
            (record["rows"], record["queries"], record["operation"]), (2, 1, "agent.status")
        )
        self.assertNotIn("secret", output.getvalue())
        self.assertNotIn("private", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["event"], "database_transfer")
        self.assertEqual((self.root / "monitoring/transfer.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_original_database_error_and_measurement_failure_are_preserved(self):
        error = RuntimeError("query failure")
        with patch("psycopg.connect") as connect, redirect_stderr(io.StringIO()):
            connect.return_value.execute.side_effect = error
            db = PostgreSQLConnection("secret-url", root=self.root)
            with self.assertRaises(RuntimeError) as raised:
                db.execute("SELECT 1")
            self.assertIs(raised.exception, error)
            with patch("tubescribe.transfer._open", side_effect=OSError("secret-path")):
                db.close()
            self.assertTrue(db.meter.pending)
            db.meter.flush()
        self.assertEqual(metrics(self.root)["operations"][0]["errors"], 1)

    def test_concurrent_connections_add_and_nested_operation_resets(self):
        @operation("feed.read")
        def read():
            meter = Meter(self.root)
            meter.record(SimpleNamespace(pgresult=Result([[b"abc"]])), 0.1)

            @operation("feed.snapshot")
            def snapshot():
                meter.record(SimpleNamespace(pgresult=Result([[b"defgh"]])), 0.2)

            snapshot()
            meter.record(SimpleNamespace(pgresult=Result([])), 0.1)
            meter.flush()

        with redirect_stderr(io.StringIO()), ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: read(), range(12)))
        records = {r["operation"]: r for r in metrics(self.root)["operations"]}
        self.assertEqual(records["feed.read"]["queries"], 24)
        self.assertEqual(records["feed.read"]["result_bytes"], 36)
        self.assertEqual(records["feed.snapshot"]["result_bytes"], 60)

    def test_broken_telemetry_storage_and_logging_cannot_replace_query_result(self):
        cursor = Mock(pgresult=Result([[b"abc"]]))
        with patch("psycopg.connect") as connect:
            connect.return_value.execute.return_value = cursor
            db = PostgreSQLConnection("secret-url", root=self.root)
            with (
                patch("tubescribe.transfer._open", side_effect=OSError("unavailable")),
                patch("tubescribe.transfer.sys.stderr") as stderr,
            ):
                stderr.write.side_effect = OSError("broken logging")
                db.meter.last_flush = 0
                self.assertIs(db.execute("SELECT 1"), cursor)
                db.close()

    def sample(self, used, month=10, **overrides):
        values = dict(
            project=PROJECT,
            used_bytes=used,
            period_start=f"2026-{month:02}-01T00:00:00Z",
            period_end=f"2026-{month + 1:02}-01T00:00:00Z",
            now=datetime(2026, month, 27, tzinfo=UTC),
        )
        return observe(self.root, **(values | overrides))

    def test_exact_thresholds_deduplicate_across_restart_and_reset_next_period(self):
        self.assertEqual(self.sample(2_999_999_999)["warnings"], [])
        self.assertEqual(self.sample(3_000_000_000)["warnings"], [3_000_000_000])
        self.assertEqual(self.sample(3_900_000_000)["warnings"], [])
        self.assertEqual(self.sample(4_000_000_000)["warnings"], [4_000_000_000])
        self.assertEqual(self.sample(7_280_000_000)["warnings"], [])
        self.assertEqual(self.sample(1)["used_bytes"], 7_280_000_000)
        self.assertEqual(self.sample(0, month=11)["warnings"], [])
        self.assertEqual(
            self.sample(4_500_000_000, month=11)["warnings"], [3_000_000_000, 4_000_000_000]
        )

    def test_wrong_project_stale_period_or_invalid_values_do_not_change_state(self):
        for override in (
            dict(project="other"),
            dict(used_bytes=-1),
            dict(used_bytes=3.0),
            dict(period_start="2026-09-01T00:00:00Z"),
            dict(period_end="2026-11-01T00:00:00"),
            dict(now=datetime(2026, 11, 1, tzinfo=UTC)),
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.sample(3_000_000_000, **override)
        self.assertFalse((self.root / "monitoring").exists())

    def test_reading_empty_metrics_does_not_open_database(self):
        with patch("psycopg.connect", side_effect=AssertionError("network")):
            self.assertEqual(metrics(self.root)["operations"], [])
        self.assertFalse((self.root / "monitoring").exists())
