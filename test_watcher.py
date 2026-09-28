import json
import tempfile
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
        self.assertEqual("old-job", restored.execute(
            "SELECT job_id FROM jobs").fetchone()[0])
        self.assertEqual("url:example.test/old-job", restored.execute(
            "SELECT key FROM alerted").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
