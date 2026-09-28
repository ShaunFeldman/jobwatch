import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import watcher


class PortableStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "state.json"
        self.state_patch = patch.object(watcher, "STATE_JSON", self.state_path)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def database():
        with patch.object(watcher, "DB_PATH", ":memory:"):
            return watcher.db_open()

    def test_round_trip_preserves_ages_used_for_pruning(self):
        source = self.database()
        source.execute(
            "INSERT INTO boards(board, seeded, last_poll) VALUES ('demo', 1, 123)")
        source.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
            ("demo", "job-1", "", "", "", "2025-01-01T00:00:00+00:00",
             "2025-02-01T00:00:00+00:00"))
        source.execute(
            "INSERT INTO alerted VALUES (?,?)",
            ("url:example.test/job-1", "2025-02-02T00:00:00+00:00"))

        watcher.export_state(source)
        exported = json.loads(self.state_path.read_text())
        self.assertEqual(2, exported["version"])

        restored = self.database()
        watcher.import_state(restored)
        self.assertEqual(
            "2025-02-01T00:00:00+00:00",
            restored.execute("SELECT last_seen FROM jobs").fetchone()[0])
        self.assertEqual(
            "2025-02-02T00:00:00+00:00",
            restored.execute("SELECT ts FROM alerted").fetchone()[0])

    def test_imports_legacy_id_and_alert_lists(self):
        self.state_path.write_text(json.dumps({
            "boards": {"demo": {"seeded": 1, "ids": ["old-job"]}},
            "alerted": ["url:example.test/old-job"],
        }))
        restored = self.database()
        watcher.import_state(restored)
        self.assertEqual(
            "old-job",
            restored.execute("SELECT job_id FROM jobs").fetchone()[0])
        self.assertEqual(
            "url:example.test/old-job",
            restored.execute("SELECT key FROM alerted").fetchone()[0])


class NotificationQualityTests(unittest.TestCase):
    def test_iso_posting_dates_render_freshness(self):
        posted = watcher.datetime.fromtimestamp(
            time.time() - 3 * 3600, watcher.timezone.utc).isoformat()
        self.assertEqual("🔥 3h ago", watcher._rel_age(posted))

    @patch("watcher.send_discord_ping", return_value=True)
    def test_loud_alert_cap_keeps_every_job_in_quiet_feed(self, send_ping):
        with patch.object(watcher, "DB_PATH", ":memory:"):
            con = watcher.db_open()

        sub = {
            "name": "test",
            "watch": re.compile("cool", re.I),
            "ping_categories": {"intern", "new_grad"},
            "max_ping_jobs": 2,
            "discord": "hook",
            "ping_hooks": {},
            "feeds": {},
            "mention": "",
            "telegram_chat": "",
        }
        jobs = [
            ("cool", {
                "id": "1",
                "company": "Cool AI",
                "title": "SWE Intern",
                "location": "Remote",
                "url": "https://x/1",
                "posted": 100,
            }),
            ("cool", {
                "id": "2",
                "company": "Cool AI",
                "title": "New Grad SWE",
                "location": "NY",
                "url": "https://x/2",
                "posted": 300,
            }),
            ("cool", {
                "id": "3",
                "company": "Cool AI",
                "title": "Junior Engineer",
                "location": "NY",
                "url": "https://x/3",
                "posted": 200,
            }),
            ("cool", {
                "id": "4",
                "company": "Cool AI",
                "title": "Software Engineer",
                "location": "NY",
                "url": "https://x/4",
                "posted": 400,
            }),
        ]

        self.assertTrue(watcher.deliver(sub, jobs, con))

        loud_jobs = [
            job
            for call in send_ping.call_args_list
            for _, job in call.args[1]
        ]
        self.assertEqual(
            ["1", "2"],
            sorted(job["id"] for job in loud_jobs),
        )
        self.assertEqual(
            4,
            con.execute("SELECT COUNT(*) FROM pending").fetchone()[0],
        )
        self.assertTrue(
            any(
                call.kwargs["omitted"] == 1
                for call in send_ping.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()