import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import watcher
from test_reliability import response, subscriber


NOW = watcher._iso_epoch('2026-10-19T12:00:00Z')
DIRECT = {'ats': 'greenhouse', 'token': 'demo'}


def job(jid='new', posted=None):
    return {'id': jid, 'title': 'Software Engineer Intern', 'location': 'Toronto',
            'company': 'Example', 'url': 'https://example.com/jobs/' + jid, 'posted': posted}


def baseline(age=300, cfg=None):
    return {'signature': watcher._collection_signature(cfg or DIRECT), 'checked_at': NOW - age}


class FreshnessPolicyTests(unittest.TestCase):
    def test_posted_today_qualifies_but_three_and_ten_day_discoveries_do_not(self):
        for hours in (1, 23, 24, 72, 240):
            with self.subTest(hours=hours):
                expiry, _ = watcher.alert_freshness(job(posted=NOW - hours * 3600), DIRECT, baseline(), {}, NOW)
                self.assertEqual(hours < 24, expiry is not None)

    def test_linkedin_and_community_discoveries_cannot_certify_original_publication(self):
        for ats in watcher.DISCOVERY_ONLY_ATS:
            with self.subTest(ats=ats):
                expiry, reason = watcher.alert_freshness(job(posted=NOW - 60), {'ats': ats}, None, {}, NOW)
                self.assertIsNone(expiry)
                self.assertIn('unverified', reason)

    def test_unknown_date_requires_recent_unchanged_complete_baseline(self):
        expiry, note = watcher.alert_freshness(job(), DIRECT, baseline(), {}, NOW)
        self.assertEqual(NOW - 300 + 86400, expiry)
        self.assertIn('date unavailable', note)
        for previous in (None, baseline(age=6*3600), baseline(age=-60), baseline(cfg={'ats': 'ashby', 'token': 'demo'})):
            self.assertIsNone(watcher.alert_freshness(job(), DIRECT, previous, {}, NOW)[0])

    def test_known_old_or_invalid_date_never_falls_back_to_newly_observed(self):
        for posted in (NOW - 86400 * 10, NOW + 60, 'not a date', float('nan'), float('inf'), -1):
            with self.subTest(posted=posted):
                self.assertIsNone(watcher.alert_freshness(job(posted=posted), DIRECT, baseline(), {}, NOW)[0])

    def test_shorter_configured_window(self):
        self.assertIsNone(watcher.alert_freshness(job(posted=NOW-7200), DIRECT, baseline(), {'max_age_hours': 1}, NOW)[0])

    def test_utc_dates_amazon_dates_and_milliseconds(self):
        self.assertEqual(NOW, watcher._posted_epoch('2026-10-19T12:00:00'))
        self.assertEqual(NOW, watcher._posted_epoch(NOW*1000))
        self.assertEqual(NOW-12*3600, watcher._posted_epoch('October 19, 2026'))
        self.assertIsNone(watcher._posted_epoch(float('nan')))

    @patch('watcher.requests.get')
    def test_greenhouse_does_not_treat_edit_time_as_publication(self, get):
        get.return_value = response({'jobs': [{'id': '1', 'title': 'Intern', 'updated_at': '2026-10-19T11:00:00Z'}]})
        self.assertIsNone(watcher.fetch_greenhouse({'token': 'demo'})[0]['posted'])
        get.return_value = response({'jobs': [{'id': '1', 'title': 'Intern',
            'first_published': '2026-08-12T06:13:46-04:00', 'updated_at': '2026-10-19T11:00:00Z'}]})
        posting = watcher.fetch_greenhouse({'token': 'demo'})[0]
        self.assertIsNone(watcher.alert_freshness(posting, DIRECT, baseline(), {}, NOW)[0])

    @patch('watcher.requests.get')
    def test_repository_update_time_does_not_become_posting_date(self, get):
        get.return_value = response([{'id': 1, 'title': 'Intern', 'active': True, 'date_updated': NOW}])
        self.assertIsNone(watcher.fetch_github_listings({'repo': 'test/test'})[0]['posted'])


class FreshnessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = watcher.db_open(':memory:')
        self.state = Path(self.tmp.name) / 'state.json'
        self.patches = [patch.object(watcher, 'STATE_JSON', self.state), patch('watcher.time.time', return_value=NOW),
                        patch('watcher.load_subscribers', return_value=[subscriber()]), patch('watcher.log')]
        for p in self.patches:
            p.start()

    def tearDown(self):
        self.con.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def run_cycle(self, jobs, cfg=None):
        with patch('watcher.fetch_board', return_value=('demo', watcher.FetchResult(jobs))), patch('watcher.flush_outbox'):
            watcher.cycle({'boards': {'demo': cfg or DIRECT}, 'feed_flush_minutes': 5}, self.con)

    def seed_board(self, previous=None):
        self.con.execute("INSERT INTO boards(board,seeded) VALUES ('demo',1)")
        if previous:
            self.con.execute('INSERT INTO kv VALUES (?,?)', ('freshness_baseline:demo', json.dumps(previous)))

    def test_new_published_job_alerts_while_old_job_is_stored_only(self):
        self.seed_board()
        self.run_cycle([job('today', NOW-3600), job('ten-days', NOW-864000)])
        self.assertEqual(2, self.con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0])
        self.assertEqual([('https://example.com/jobs/today',)], self.con.execute('SELECT url FROM pending').fetchall())
        self.assertFalse(watcher.already_alerted(self.con, job('ten-days')))

    def test_undated_backfill_silent_then_observed_new_direct_job_alerts(self):
        self.seed_board()
        self.run_cycle([job('backfill')])
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])
        self.run_cycle([job('backfill'), job('new')])
        row = self.con.execute('SELECT url,posted,freshness_note FROM pending').fetchone()
        self.assertEqual('https://example.com/jobs/new', row[0])
        self.assertIsNone(row[1])
        self.assertIn('date unavailable', row[2])

    def test_recovery_and_changed_collection_do_not_create_undated_alerts(self):
        self.seed_board(baseline())
        self.con.execute("UPDATE boards SET failures=1 WHERE board='demo'")
        self.run_cycle([job('recovered')])
        self.run_cycle([job('recovered'), job('expanded')], {'ats': 'greenhouse', 'token': 'new-tenant'})
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])

    def test_known_job_with_refreshed_date_does_not_realert(self):
        self.seed_board()
        self.run_cycle([job('old', NOW-864000)])
        self.run_cycle([job('old', NOW-60)])
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])

    def test_discovery_only_echo_does_not_block_company_alert(self):
        self.seed_board()
        self.run_cycle([job('same', NOW-60)], {'ats': 'linkedin'})
        self.assertFalse(watcher.already_alerted(self.con, job('same')))
        self.con.execute("INSERT INTO boards(board,seeded) VALUES ('company',1)")
        with patch('watcher.fetch_board', return_value=('company', watcher.FetchResult([job('same', NOW-60)]))), patch('watcher.flush_outbox'):
            watcher.cycle({'boards': {'company': DIRECT}}, self.con)
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])

    def test_expired_and_legacy_messages_never_send(self):
        for expiry in (None, NOW-1, NOW+60):
            watcher._enqueue(self.con, 'test', 'feed:intern', {'content': str(expiry)}, expiry)
        with patch('watcher._post_with_retry', return_value=True) as send:
            watcher.flush_outbox(self.con, [subscriber()])
        self.assertEqual(1, send.call_count)
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])

    def test_expired_feed_job_is_removed_before_rendering(self):
        approved = job('old', NOW-23*3600)
        approved['_alert_expires_at'] = NOW+60
        watcher.deliver(subscriber(), [('demo', approved)], self.con)
        self.con.execute('UPDATE pending SET expires_at=?', (NOW-1,))
        watcher.flush_feeds({}, self.con, [subscriber()], force=True)
        self.assertEqual(0, self.con.execute("SELECT COUNT(*) FROM outbox WHERE route LIKE 'feed:%'").fetchone()[0])

    def test_state_roundtrip_preserves_baseline_and_notification_deadlines(self):
        self.seed_board(baseline())
        self.run_cycle([job('new', NOW-60)])
        watcher.export_state(self.con)
        restored = watcher.db_open(':memory:')
        watcher.import_state(restored)
        self.assertEqual(NOW-60+86400, restored.execute('SELECT expires_at FROM pending').fetchone()[0])
        self.assertEqual(NOW-60+86400, restored.execute('SELECT expires_at FROM outbox').fetchone()[0])
        self.assertIsNotNone(restored.execute("SELECT v FROM kv WHERE k='freshness_baseline:demo'").fetchone())
        restored.close()

    def test_existing_sqlite_queue_schema_migrates_without_losing_job_index(self):
        path = Path(self.tmp.name) / 'legacy.db'
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE pending(sub TEXT,tier TEXT,ts TEXT,company TEXT,title TEXT,url TEXT,location TEXT,salary TEXT,posted REAL)")
        old.execute("CREATE TABLE outbox(id TEXT PRIMARY KEY,sub TEXT,route TEXT,payload TEXT,created_at TEXT,attempts INTEGER,next_try REAL)")
        old.execute("INSERT INTO outbox VALUES ('old','test','feed:intern','{}','2026-10-19',0,0)")
        old.commit()
        old.close()
        upgraded = watcher.db_open(path)
        self.assertIsNone(upgraded.execute('SELECT expires_at FROM outbox').fetchone()[0])
        self.assertIn('freshness_note', {r[1] for r in upgraded.execute('PRAGMA table_info(pending)')})
        self.assertEqual(1, watcher.expire_notifications(upgraded))
        upgraded.close()

    def test_legacy_portable_queue_loads_but_is_retired_before_delivery(self):
        self.state.write_text(json.dumps({'version': 3, 'pending': [
            ['test', 'intern', watcher.now(), 'Example', 'Intern', 'https://x/old', 'Toronto', '', NOW]],
            'outbox': [['old', 'test', 'feed:intern', '{}', watcher.now(), 0, 0]]}))
        watcher.import_state(self.con)
        with patch('watcher.requests.post') as post:
            watcher.flush_outbox(self.con, [subscriber()])
        post.assert_not_called()
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
