import json
import tempfile
import time
import unittest
from pathlib import Path

import watcher
from dashboard import JobwatchService
from job_intelligence import analyze_job, board_tags, location_bucket, role_track, seniority


class IntelligenceTests(unittest.TestCase):
    def test_role_families_cover_core_and_broad_searches(self):
        self.assertEqual("quant", role_track("Quantitative Researcher, Systematic Equities"))
        self.assertEqual("software", role_track("Backend Software Engineer — Distributed Systems"))
        self.assertEqual("data_ml", role_track("Machine Learning Engineer"))
        self.assertEqual("product", role_track("Product Manager, Growth"))
        self.assertEqual("design", role_track("Senior Product Designer"))
        self.assertEqual("operations", role_track("Business Operations Associate"))

    def test_seniority_and_location_are_independent_facets(self):
        self.assertEqual("internship", seniority("Software Engineer Intern — Summer 2027"))
        self.assertEqual("new_grad", seniority("2027 Graduate Quantitative Trader"))
        self.assertEqual("senior", seniority("Staff Infrastructure Engineer"))
        self.assertEqual("us", location_bucket("New York, NY, United States"))
        self.assertEqual("canada", location_bucket("Toronto, ON, Canada"))
        self.assertEqual("remote", location_bucket("Remote — US or Canada"))

    def test_explainable_profile_prioritizes_target_fit(self):
        target = analyze_job({
            "title": "Software Engineer Intern, Rust / Distributed Systems",
            "location": "Seattle, WA",
            "posted": time.time() - 3600,
        }, tags={"startup"})
        unrelated = analyze_job({
            "title": "Senior Sales Director",
            "location": "London, UK",
            "posted": time.time() - 3600,
        })
        self.assertGreaterEqual(target["score"], 90)
        self.assertLess(unrelated["score"], 40)
        self.assertIn("target career stage", target["reasons"])
        self.assertTrue(any(reason.startswith("skills:") for reason in target["reasons"]))

    def test_target_year_and_degree_level_affect_ranking(self):
        base = {"location": "New York, NY", "posted": time.time() - 3600}
        target = analyze_job({**base, "title": "2027 Software Engineer Intern"})
        old = analyze_job({**base, "title": "2026 Software Engineer Intern"})
        phd = analyze_job({**base, "title": "2027 Software Engineer PhD Intern"})
        self.assertGreater(target["score"], old["score"])
        self.assertGreater(target["score"], phd["score"])
        self.assertIn("target 2027 season", target["reasons"])

    def test_board_sections_become_company_tags(self):
        config = {"boards": {
            "_quant_and_trading": "---", "jane": {},
            "_ai_and_startups": "---", "linear": {},
        }}
        tags = board_tags(config)
        self.assertIn("quant", tags["jane"])
        self.assertEqual({"startup"}, tags["linear"] & {"startup", "quant"})


class DashboardServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "test.db"
        self.config_path = root / "config.json"
        self.subscribers_path = root / "subscribers.json"
        self.config_path.write_text(json.dumps({
            "profile": {"headline": "Test search"},
            "boards": {
                "_ai_and_startups": "---",
                "demo_startup": {"ats": "greenhouse", "token": "demo"},
                "_quant_and_trading": "---",
                "demo_quant": {"ats": "greenhouse", "token": "quant"},
            },
        }))
        self.subscribers_path.write_text(json.dumps({
            "test": {"watchlist": "demo", "companies": ["all"]}
        }))
        con = watcher.db_open(self.db_path)
        now = watcher.now()
        con.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
            ("demo_startup", "one", "Backend Software Engineer Intern", "New York, NY", "https://example.com/one", now, now),
        )
        con.execute(
            "INSERT INTO job_details VALUES (?,?,?,?,?)",
            ("demo_startup", "one", "Demo Labs", "$50/hr", time.time()),
        )
        con.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
            ("demo_quant", "two", "Quantitative Researcher", "Toronto, ON", "https://example.com/two", now, now),
        )
        con.execute(
            "INSERT INTO job_details VALUES (?,?,?,?,?)",
            ("demo_quant", "two", "Demo Capital", "", time.time() - 7200),
        )
        con.execute("INSERT INTO boards(board, seeded, last_ok) VALUES (?,?,?)", ("demo_startup", 1, now))
        con.commit()
        con.close()
        self.service = JobwatchService(self.db_path, self.config_path, self.subscribers_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_search_filters_and_overview(self):
        results = self.service.jobs({"track": ["software"], "location": ["us"]})
        self.assertEqual(1, results["total"])
        self.assertEqual("Demo Labs", results["jobs"][0]["company"])
        self.assertIn("startup", results["jobs"][0]["company_tags"])
        overview = self.service.overview()
        self.assertEqual(2, overview["stats"]["searchable_jobs"])
        self.assertEqual("Test search", overview["profile"]["headline"])

    def test_application_pipeline_round_trip(self):
        job = self.service.jobs({})["jobs"][0]
        result = self.service.save_application({
            "job": job,
            "status": "interview",
            "next_step": "Prepare systems design",
            "notes": "Recruiter screen complete",
        })
        self.assertTrue(result["ok"])
        pipeline = self.service.pipeline()["pipeline"]
        self.assertEqual(1, len(pipeline["interview"]))
        self.assertEqual("Prepare systems design", pipeline["interview"][0]["next_step"])
        self.service.delete_application(job["key"])
        self.assertEqual(0, self.service.pipeline()["total"])

    def test_invalid_status_is_rejected(self):
        job = self.service.jobs({})["jobs"][0]
        with self.assertRaises(ValueError):
            self.service.save_application({"job": job, "status": "ghosted"})


if __name__ == "__main__":
    unittest.main()
