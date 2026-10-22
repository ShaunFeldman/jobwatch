import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import watcher
from test_reliability import response, subscriber


NOW = watcher._iso_epoch('2026-10-19T12:00:00Z')
DIRECT = {'ats': 'greenhouse', 'token': 'demo', 'min_interval': 0}


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

    def test_unknown_date_warns_unless_recent_complete_baseline(self):
        expiry, note = watcher.alert_freshness(job(), DIRECT, baseline(), {}, NOW)
        self.assertEqual(NOW - 300 + 86400, expiry)
        self.assertIn('date unavailable', note)
        for previous in (None, baseline(age=6*3600), baseline(age=-60), baseline(cfg={'ats': 'ashby', 'token': 'demo'})):
            self.assertIn("Age unverified", watcher.alert_freshness(job(), DIRECT, previous, {}, NOW)[1])

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

    def test_new_published_job_pings_and_old_job_goes_to_full_feed(self):
        self.seed_board()
        self.run_cycle([job('today', NOW-3600), job('ten-days', NOW-864000)])
        self.assertEqual(2, self.con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0])
        self.assertEqual(2, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])
        self.assertEqual(1, self.con.execute("SELECT COUNT(*) FROM outbox WHERE route LIKE 'ping:%'").fetchone()[0])
        self.assertFalse(watcher.already_alerted(self.con, job('ten-days')))

    def test_undated_discovery_warns_then_observed_new_job_has_baseline_note(self):
        self.seed_board()
        self.run_cycle([job('backfill')])
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])
        self.assertIn('Age unverified', self.con.execute('SELECT freshness_note FROM pending').fetchone()[0])
        self.run_cycle([job('backfill'), job('new')])
        row = self.con.execute("SELECT url,posted,freshness_note FROM pending WHERE url LIKE '%/new'").fetchone()
        self.assertEqual('https://example.com/jobs/new', row[0])
        self.assertIsNone(row[1])
        self.assertIn('date unavailable', row[2])

    def test_recovery_warns_but_changed_collection_seeds_silently(self):
        self.seed_board(baseline())
        self.con.execute("UPDATE boards SET failures=1 WHERE board='demo'")
        self.run_cycle([job('recovered')])
        self.run_cycle([job('recovered'), job('expanded')], {'ats': 'greenhouse', 'token': 'new-tenant', 'min_interval': 0})
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])

    def test_known_job_with_refreshed_date_does_not_realert(self):
        self.seed_board()
        self.run_cycle([job('old', NOW-864000)])
        self.run_cycle([job('old', NOW-60)])
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])
        self.assertEqual(0, self.con.execute("SELECT COUNT(*) FROM outbox WHERE route LIKE 'ping:%'").fetchone()[0])

    def test_discovery_only_echo_does_not_block_company_alert(self):
        self.seed_board()
        self.run_cycle([job('same', NOW-60)], {'ats': 'linkedin', 'min_interval': 0})
        self.assertFalse(watcher.already_alerted(self.con, job('same')))
        self.con.execute("INSERT INTO boards(board,seeded) VALUES ('company',1)")
        with patch('watcher.fetch_board', return_value=('company', watcher.FetchResult([job('same', NOW-60)]))), patch('watcher.flush_outbox'):
            watcher.cycle({'boards': {'company': DIRECT}}, self.con)
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])

    def test_unverified_linkedin_is_quiet_then_verified_new_job_pings_once(self):
        self.seed_board()
        row = dict(job('123', NOW-60), url='https://www.linkedin.com/jobs/view/123')
        self.run_cycle([row], {'ats': 'linkedin', 'min_interval': 0})
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        verified = dict(row, url='https://example.com/jobs/official', _discovery_url=row['url'],
                        _official_posted=True, posted=NOW-60)
        self.run_cycle([verified], {'ats': 'linkedin', 'min_interval': 0})
        self.run_cycle([dict(verified)], {'ats': 'linkedin', 'min_interval': 0})
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM recent').fetchone()[0])
        # Subsequent direct-board discovery is deduplicated in both channels.
        self.con.execute("INSERT INTO boards(board,seeded) VALUES ('company',1)")
        with patch('watcher.fetch_board', return_value=('company', watcher.FetchResult([job('official', NOW-60)]))), patch('watcher.flush_outbox'):
            watcher.cycle({'boards': {'company': DIRECT}}, self.con)
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])

    def test_verified_baseline_discoveries_stay_silent_on_next_check(self):
        row = dict(job('baseline', NOW-60), _official_posted=True)
        self.run_cycle([row], {'ats': 'linkedin', 'min_interval': 0})
        self.run_cycle([dict(row)], {'ats': 'linkedin', 'min_interval': 0})
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])

    def test_old_first_observation_survives_refreshed_official_date(self):
        self.seed_board()
        row = job('old', None)
        watcher.remember_publication(self.con, [row], DIRECT, [subscriber()])
        self.con.execute("UPDATE kv SET v=? WHERE k LIKE 'original_seen:%'", (str(NOW-864000),))
        repost = dict(job('old', NOW-60), _official_posted=True)
        watcher.remember_publication(self.con, [repost], {'ats': 'linkedin', 'min_interval': 0}, [subscriber()])
        expiry, note = watcher.alert_freshness(repost, {'ats': 'linkedin', 'min_interval': 0}, None, {}, NOW)
        self.assertIsNone(expiry)
        self.assertIn('previously observed', note)

    def test_verification_cache_and_publication_survive_state_roundtrip(self):
        self.seed_board()
        source = 'https://www.linkedin.com/jobs/view/123'
        row = dict(job('123', NOW-60), _official_posted=True, _discovery_url=source,
                   _verification={'source_url': source, 'url': 'https://example.com/jobs/123',
                                  'posted': NOW-60, 'retry_at': NOW+3600})
        self.run_cycle([row], {'ats': 'linkedin', 'min_interval': 0})
        watcher.export_state(self.con)
        restored = watcher.db_open(':memory:')
        try:
            watcher.import_state(restored)
            evidence = restored.execute("SELECT v FROM kv WHERE k=?", ('verification:' + source,)).fetchone()
            self.assertEqual(NOW-60, json.loads(evidence[0])['posted'])
            repost = dict(job('123', NOW-1))
            watcher.remember_publication(restored, [repost], {'ats': 'linkedin'}, [subscriber()])
            self.assertEqual(NOW-60, repost['posted'])
        finally:
            restored.close()

    def test_repost_timestamp_is_not_displayed_as_original_age(self):
        self.seed_board()
        self.run_cycle([job('unverified', NOW-60)], {'ats': 'linkedin', 'min_interval': 0})
        posted, note = self.con.execute('SELECT posted,freshness_note FROM pending').fetchone()
        self.assertIsNone(posted)
        self.assertIn('unverified', note)

    def test_jobright_just_posted_is_quiet_and_old_employer_evidence_wins(self):
        self.seed_board()
        cfg = {'ats': 'github_md', 'min_interval': 0}
        wrapper = dict(job('jobright', NOW-60), url='https://jobright.ai/jobs/info/123')
        self.run_cycle([wrapper], cfg)
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        posted, note = self.con.execute('SELECT posted,freshness_note FROM pending').fetchone()
        self.assertIsNone(posted)
        self.assertIn('unverified', note)
        original = job('employer', NOW-10*86400)
        watcher.remember_publication(self.con, [original], DIRECT, [subscriber()])
        resolved = dict(job('jobright', NOW-60), url=original['url'],
                        _discovery_url=wrapper['url'])
        self.run_cycle([resolved], cfg)
        self.assertEqual(NOW-10*86400, resolved['posted'])
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])

    def test_legacy_unverified_ping_is_retired_but_quiet_feed_is_kept(self):
        payload = {'embeds': [{'title': 'Example — Intern',
                              'url': 'https://www.linkedin.com/jobs/view/123',
                              'description': 'Age unverified — may be older'}]}
        watcher._enqueue(self.con, 'test', 'ping:intern', payload, NOW+3600)
        watcher._enqueue(self.con, 'test', 'feed:intern', payload, 0)
        with patch('watcher._post_with_retry', return_value=True) as send:
            watcher.flush_outbox(self.con, [subscriber()])
        self.assertEqual(1, send.call_count)

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
        self.assertEqual(0, restored.execute('SELECT expires_at FROM pending').fetchone()[0])
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
