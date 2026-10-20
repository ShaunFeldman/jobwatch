import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import watcher
from notification_fit import assess, geography
from test_reliability import subscriber

POLICY = json.loads(Path('subscribers.json').read_text())['shaun']['notification_fit']
WATCH = re.compile('Netflix|Palantir|Citadel|CIBC', re.I)


def job(title, company='Netflix', location='New York, NY', jid='1', posted=None):
    return dict(id=jid, title=title, company=company, location=location,
                url='https://example.com/jobs/'+jid, posted=posted)


class FitTests(unittest.TestCase):
    def test_interest_examples(self):
        cases = [
            ('Software Engineer', 'Palantir', 'Palo Alto, CA', True),
            ('Software Engineer L3', 'Netflix', 'Los Gatos, CA', True),
            ('Software Engineer, New Grad', 'Palantir', 'Washington, D.C.', True),
            ('Software Engineer', 'Netflix', 'New York, NY', True),
            ('Software Engineer', 'Netflix', 'Remote, US', True),
            ('Deployment Strategist', 'Palantir', 'New York, NY', True),
            ('Investment Banking Intern', 'CIBC', 'Toronto, ON', False),
            ('Investment Banking Intern', 'CIBC', 'New York, NY', False),
            ('Investment Analyst Intern', 'Citadel', 'New York, NY', True),
            ('Investment Banking Intern', 'Citadel', 'New York, NY', True),
            ('Quantitative Research Intern', 'Citadel', 'Chicago, IL', True),
            ('Software Engineer Intern', 'CIBC', 'Toronto, ON', True),
            ('Software Engineer Intern', 'CIBC', 'New York, NY', True),
            ('Backend Engineer Intern', 'New Startup', 'San Francisco, CA', True),
            ('Backend Engineer Intern', 'New Startup', 'Austin, TX', True),
            ('Software Engineer Intern', 'Palantir', 'London, UK (Remote)', False),
            ('Recruiting Intern', 'Netflix', 'San Francisco, CA', True),
            ('Senior Software Engineer', 'Netflix', 'New York, NY', False),
            ('Software Engineer L5', 'Netflix', 'New York, NY', False),
            ('Software Engineer II', 'Netflix', 'New York, NY', False),
            ('Research Scientist PhD Intern', 'Netflix', 'New York, NY', False),
            ('Software Engineer Intern', 'Netflix', 'Remote', True),
        ]
        for title, company, location, expected in cases:
            with self.subTest(title=title, company=company, location=location):
                result = assess(job(title, company, location), POLICY, WATCH)
                self.assertEqual(expected, result['ping'])
                self.assertTrue(result['reasons'])

    def test_company_role_and_location_tradeoffs_generalize(self):
        cases = [
            ('Capital Markets Quantitative Analyst', 'CIBC', 'Toronto', True),
            ('Quantitative Research Intern', 'RBC', 'Toronto', True),
            ('Machine Learning Intern', 'BMO', 'Toronto', True),
            ('Backend Engineer Intern', 'Unknown Startup', 'Toronto', True),
            ('Investment Banking Intern', 'CIBC', 'Toronto', False),
            ('Business Operations Intern', 'Citadel', 'Toronto', True),
            ('Business Operations Intern', 'CIBC', 'Toronto', False),
            ('Strategy Intern', 'Jane Street', 'New York, NY', True),
            ('Product Management Intern', 'Google', 'San Francisco, CA', True),
            ('Marketing Intern', 'Citadel', 'New York, NY', True),
            ('Marketing Intern', 'Unknown Company', 'Toronto', False),
            ('Software Engineer', 'Unknown Company', 'San Francisco, CA', True),
            ('Software Engineer', 'Unknown Company', 'Toronto', False),
            ('Legal Counsel', 'Citadel', 'New York, NY', False),
            ('Senior Business Operations Analyst', 'Citadel', 'New York, NY', False),
        ]
        for title, company, location, expected in cases:
            with self.subTest(title=title, company=company, location=location):
                self.assertEqual(expected, assess(job(title, company, location), POLICY, WATCH)['ping'])

    def test_bank_finance_remains_in_full_feed(self):
        result = assess(job('Investment Banking Intern', 'CIBC', 'Toronto'), POLICY, WATCH)
        self.assertTrue(result['feed'])
        self.assertFalse(result['ping'])

    def test_hybrid_or_remote_does_not_override_foreign_location(self):
        for location in ('Hybrid - Singapore', 'Remote in Europe', 'London (Remote)', 'Bengaluru, India'):
            self.assertEqual('other', geography(location)[0])

    def test_us_state_codes_and_multiple_locations(self):
        for location in ('Denver, CO', 'Reston, VA', 'Miami, FL', 'Seattle, WA', 'London | San Francisco, CA'):
            self.assertIn(geography(location)[0], {'us', 'nyc_sf'})


class FitDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_patch = patch.object(watcher, 'STATE_JSON', Path(self.temp.name)/'state.json')
        self.state_patch.start()
        self.con = watcher.db_open(':memory:')
        # Legacy filters intentionally reject SF and generic finance; the opted-in
        # policy must run before those old gates could discard opportunities.
        self.sub = subscriber(notification_fit=POLICY, watch=WATCH,
                              filt=watcher.Filt({'include': 'intern', 'location': 'Toronto'}))

    def tearDown(self):
        self.con.close()
        self.state_patch.stop()
        self.temp.cleanup()

    def cycle(self, rows):
        with patch('watcher.load_subscribers', return_value=[self.sub]), patch('watcher.fetch_board', return_value=('demo', watcher.FetchResult(rows))), patch('watcher.flush_outbox'):
            watcher.cycle({'boards': {'demo': {'ats': 'greenhouse', 'min_interval': 0}}}, self.con)

    def test_generic_sf_tech_role_reaches_important_full_time_channel(self):
        self.con.execute("INSERT INTO boards(board,seeded) VALUES ('demo',1)")
        self.cycle([job('Software Engineer', 'Palantir', 'Palo Alto, CA', posted=time.time()-60)])
        row = self.con.execute('SELECT route,payload FROM outbox').fetchone()
        self.assertEqual('ping:full_time', row[0])
        self.assertIn('Why:', row[1])
        self.assertIn('NYC/SF preferred', row[1])
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])

    def test_policy_upgrade_recovers_only_recent_known_official_jobs_once(self):
        self.con.execute("INSERT INTO boards(board,seeded) VALUES ('demo',1)")
        rows = [job('Software Engineer', jid='recent', posted=time.time()-60),
                job('Software Engineer', jid='old', posted=time.time()-864000),
                job('Software Engineer', jid='unknown')]
        for row in rows:
            self.con.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                             ('demo', row['id'], row['title'], row['location'], row['url'], watcher.now(), watcher.now()))
        self.cycle(rows)
        self.cycle(rows)
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        self.assertEqual(['https://example.com/jobs/recent'], [r[0] for r in self.con.execute('SELECT url FROM pending')])

    def test_new_source_baseline_stays_silent_under_fit_policy(self):
        self.cycle([job('Software Engineer', posted=time.time()-60)])
        self.cycle([job('Software Engineer', posted=time.time()-60)])
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])

    def test_old_good_fit_job_does_not_ping(self):
        self.con.execute("INSERT INTO boards(board,seeded) VALUES ('demo',1)")
        self.cycle([job('Software Engineer', posted=time.time()-864000)])
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])

    def test_bank_intern_only_queues_full_feed(self):
        self.con.execute("INSERT INTO boards(board,seeded) VALUES ('demo',1)")
        self.cycle([job('Investment Banking Intern', 'CIBC', 'Toronto', posted=time.time()-60)])
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])

    def test_preference_change_filters_old_important_queue(self):
        watcher._enqueue(self.con, 'test', 'ping:intern', {'embeds': [{
            'title': 'CIBC — Investment Banking Intern', 'description': '📍 Toronto'}]}, time.time()+3600)
        with patch('watcher.requests.post') as post:
            watcher.flush_outbox(self.con, [self.sub])
        post.assert_not_called()
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
