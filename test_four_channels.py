"""Acceptance tests for the existing four Discord channels (no live sends)."""
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import watcher
from test_reliability import job as base_job, subscriber, response

def job(jid="1", **extra):
    return dict(base_job(jid), **extra)


class FourChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = watcher.db_open(':memory:')
        self.state = Path(self.tmp.name) / 'state.json'
        self.sub = subscriber(watch=re.compile('Pinterest|Netflix'), ping_categories={'intern', 'new_grad'})
        self.patches = [patch.object(watcher, 'STATE_JSON', self.state),
                        patch('watcher.load_subscribers', return_value=[self.sub]), patch('watcher.log')]
        for p in self.patches:
            p.start()

    def tearDown(self):
        self.con.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def seed(self, *boards):
        for board in boards:
            self.con.execute('INSERT INTO boards(board,seeded,last_ok) VALUES (?,1,?)', (board, watcher.now()))

    def collect(self, rows, cfg=None):
        cfg = cfg or {'ats': 'greenhouse', 'min_interval': 0}
        with patch('watcher.fetch_board', return_value=('pinterest', watcher.FetchResult(rows))), patch('watcher.flush_outbox'):
            watcher.cycle({'boards': {'pinterest': cfg}}, self.con)

    def test_linkedin_discoveries_never_queue_either_channel(self):
        for url in ('https://www.linkedin.com/jobs/view/123', 'https://ca.linkedin.com/jobs/view/456'):
            row = job(company='Pinterest', url=url, _alert_expires_at=time.time()+3600)
            watcher.deliver(self.sub, [('community', row)], self.con)
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        # Rejecting a repost must not mark the employer's job as delivered.
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM alerted').fetchone()[0])

    def test_restored_linkedin_ping_is_removed_without_sending(self):
        watcher._enqueue(self.con, self.sub['name'], 'ping:intern', {'embeds': [{
            'title': 'Pinterest — Software Engineer Intern',
            'url': 'https://www.linkedin.com/jobs/view/123'}]}, time.time()+3600)
        with patch('watcher.requests.post') as post:
            watcher.flush_outbox(self.con, [self.sub])
        post.assert_not_called()
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])

    def test_mixed_queued_feed_keeps_official_links(self):
        payload = {'content': '2 new jobs', 'embeds': [{
            'title': '2 new internships',
            'description': '**Pinterest**\n[Intern](https://www.linkedin.com/jobs/view/123)\n'
                           '[Engineer](https://careers.pinterest.com/jobs/456)\n\n'
                           '**Other**\n[Intern](https://ca.linkedin.com/jobs/view/789)'}]}
        filtered = watcher.strip_linkedin_embeds(payload)
        self.assertNotIn('linkedin.com', json.dumps(filtered))
        self.assertNotIn('**Other**', json.dumps(filtered))
        self.assertIn('careers.pinterest.com', json.dumps(filtered))
        self.assertNotIn('content', filtered)
        self.assertEqual('Job discoveries', filtered['embeds'][0]['title'])

    def test_restored_pending_linkedin_feed_is_removed(self):
        self.con.execute('INSERT INTO pending VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (self.sub['name'], 'intern', watcher.now(), 'Pinterest', 'Software Engineer Intern',
             'https://www.linkedin.com/jobs/view/123', 'NYC', '', None, 0, ''))
        watcher.flush_feeds({}, self.con, [self.sub], force=True)
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])

    def test_new_intern_goes_to_both_channels_only_important_notifies(self):
        self.seed('pinterest')
        self.collect([job(company='Pinterest', posted=time.time()-60)])
        watcher.flush_feeds({}, self.con, [self.sub], force=True)
        rows = dict(self.con.execute('SELECT route,payload FROM outbox'))
        self.assertEqual({'ping:intern', 'feed:intern'}, set(rows))
        self.assertFalse(json.loads(rows['ping:intern']).get('flags', 0) & 4096)
        self.assertTrue(json.loads(rows['feed:intern'])['flags'] & 4096)
        self.assertEqual([], json.loads(rows['feed:intern'])['allowed_mentions']['parse'])

    def test_full_time_uses_its_two_destinations(self):
        self.seed('pinterest')
        self.collect([job(company='Pinterest', title='New Grad Software Engineer', posted=time.time()-60)])
        watcher.flush_feeds({}, self.con, [self.sub], force=True)
        self.assertEqual({'ping:full_time', 'feed:full_time'},
                         {row[0] for row in self.con.execute('SELECT route FROM outbox')})

    def test_twelve_important_matches_all_send_in_paced_drains(self):
        self.seed('pinterest')
        self.collect([job(str(i), company='Pinterest', posted=time.time()-60) for i in range(12)])
        self.assertEqual(12, self.con.execute("SELECT COUNT(*) FROM outbox WHERE route='ping:intern'").fetchone()[0])
        with patch('watcher._post_with_retry', return_value=True) as send:
            watcher.flush_outbox(self.con, [self.sub], max_messages=8)
            self.assertEqual(8, send.call_count)
            watcher.flush_outbox(self.con, [self.sub], max_messages=8)
            self.assertEqual(12, send.call_count)
        self.assertEqual(12, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])

    def test_old_discovery_retained_in_full_feed_not_important(self):
        self.seed('pinterest')
        self.collect([job(company='Pinterest', posted=time.time()-864000)])
        watcher.flush_feeds({}, self.con, [self.sub], force=True)
        route, payload, expiry = self.con.execute('SELECT route,payload,expires_at FROM outbox').fetchone()
        self.assertEqual('feed:intern', route)
        self.assertIn('Older posting', payload)
        self.assertEqual(0, expiry)
        with patch('watcher.time.time', return_value=time.time()+8640000):
            self.assertEqual(0, watcher.expire_notifications(self.con))

    def test_expired_important_job_does_not_remove_younger_job_or_feeds(self):
        for i in range(2):
            j = job(str(i), company='Pinterest')
            j['_alert_expires_at'] = time.time()+60+i*3600
            watcher.deliver(self.sub, [('pinterest', j)], self.con)
        with patch('watcher.time.time', return_value=time.time()+120):
            watcher.expire_notifications(self.con)
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        self.assertEqual(2, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])

    def test_cross_source_dedupe_survives_restart_for_both_routes(self):
        j = job(company='Pinterest')
        watcher.deliver(self.sub, [('pinterest', j)], self.con)
        watcher.export_state(self.con)
        other = watcher.db_open(':memory:')
        try:
            watcher.import_state(other)
            watcher.deliver(self.sub, [('community', dict(j, id='different'))], other)
            self.assertEqual(1, other.execute('SELECT COUNT(*) FROM pending').fetchone()[0])
            self.assertEqual(1, other.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        finally:
            other.close()

    def test_full_feed_discovery_does_not_suppress_later_important_evidence(self):
        j = job(company='Pinterest')
        j['_alert_expires_at'] = None
        watcher.deliver(self.sub, [('community', j)], self.con)
        j['_alert_expires_at'] = time.time()+3600
        watcher.deliver(self.sub, [('pinterest', j)], self.con)
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])

    def test_slow_source_cannot_hold_completed_netflix_alert(self):
        self.seed('netflix', 'slow')
        sent = threading.Event()
        slow_saw_delivery = []
        def fetch(name, cfg):
            if name == 'slow':
                slow_saw_delivery.append(sent.wait(3))
                return name, watcher.FetchResult()
            return name, watcher.FetchResult([job(company='Netflix', posted=time.time()-60)])
        def send(fn, route, **kwargs):
            sent.set()
            return True
        with patch('watcher.fetch_board', side_effect=fetch), patch('watcher._post_with_retry', side_effect=send):
            watcher.cycle({'boards': {'slow': {'ats': 'linkedin'}, 'netflix': {'ats': 'eightfold'}}}, self.con)
        self.assertEqual([True], slow_saw_delivery)

    def test_first_partial_then_complete_baseline_is_silent(self):
        cfg = {'boards': {'pinterest': {'ats': 'greenhouse', 'min_interval': 0}}}
        rows = [job(company='Pinterest')]
        for result in (watcher.FetchResult(rows, ['page 2 failed']), watcher.FetchResult(rows+[job('2', company='Pinterest')])):
            self.con.execute('UPDATE boards SET next_poll=0')
            with patch('watcher.fetch_board', return_value=('pinterest', result)), patch('watcher.flush_outbox'):
                watcher.cycle(cfg, self.con)
        self.assertEqual(2, self.con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0])
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])

    def test_delivery_checkpoint_failure_prevents_sending(self):
        watcher.deliver(self.sub, [('pinterest', job(company='Pinterest'))], self.con)
        with patch('watcher.requests.post') as post:
            with self.assertRaises(OSError):
                watcher.flush_outbox(self.con, [self.sub], checkpoint=lambda: (_ for _ in ()).throw(OSError('disk full')))
        post.assert_not_called()
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])

    def test_health_notices_once_and_recovery_once(self):
        self.seed('pinterest')
        self.con.execute("UPDATE boards SET failures=3 WHERE board='pinterest'")
        config = {'boards': {'pinterest': {'ats': 'greenhouse'}}}
        sub = dict(self.sub, ops=True)
        watcher.coverage_notices(config, self.con, [sub])
        watcher.coverage_notices(config, self.con, [sub])
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        self.con.execute('UPDATE boards SET failures=0')
        watcher.coverage_notices(config, self.con, [sub])
        watcher.coverage_notices(config, self.con, [sub])
        self.assertEqual(2, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        self.assertEqual('stale', watcher.source_health(config['boards']['pinterest'],
            {'seeded': 1, 'last_ok': '2020-01-01T00:00:00Z', 'failures': 0}))

    def test_netflix_secondary_locations_are_preserved(self):
        with patch('watcher.requests.get', return_value=response({'positions': [{'id': '1', 'name': 'Intern',
                'location': 'Los Angeles', 'locations': ['Los Angeles', 'New York'],
                'work_location_option': 'remote'}], 'count': 1})):
            rows = watcher.fetch_eightfold({'host': 'example', 'domain': 'example'})
        self.assertIn('New York', rows[0]['location'])
        self.assertIn('Remote', rows[0]['location'])

    def test_malformed_conversion_preserves_other_rows(self):
        rows = watcher._mapped([{'id': '1'}, {}, {'id': '3'}], lambda row: row['id'])
        self.assertEqual(['1', '3'], rows)
        self.assertTrue(rows.warnings)

    def test_duplicate_rows_cannot_claim_full_reported_count(self):
        rows = watcher._pages(lambda offset: ([{'id': '1'}, {'id': '1'}], 2), lambda row: row['id'], {})
        self.assertEqual(1, len(rows))
        self.assertTrue(rows.warnings)

    def test_continuous_scheduler_rechecks_fast_board_while_slow_board_runs(self):
        release = threading.Event()
        fast_calls = []
        slow_released = []
        def fetch(name, cfg):
            if name == 'slow':
                slow_released.append(release.wait(3))
            else:
                fast_calls.append(name)
                if len(fast_calls) >= 2:
                    release.set()
            return name, watcher.FetchResult()
        config = {'boards': {'slow': {'ats': 'linkedin', 'min_interval': 999},
                             'fast': {'ats': 'greenhouse', 'min_interval': 0}}}
        with patch('watcher.fetch_board', side_effect=fetch), patch('watcher.flush_outbox'), patch('watcher.export_state'):
            watcher.cycle(config, self.con, run_seconds=0.1)
        self.assertTrue(release.is_set())
        self.assertEqual([True], slow_released)
        self.assertGreaterEqual(len(fast_calls), 2)

    def test_known_old_official_posting_overrides_recent_repost(self):
        original = job(company='Pinterest', posted=time.time()-864000)
        watcher.remember_publication(self.con, [original], {'ats': 'greenhouse'}, [self.sub])
        repost = job(company='Pinterest', posted=time.time()-60)
        watcher.remember_publication(self.con, [repost], {'ats': 'linkedin'}, [self.sub])
        expiry, reason = watcher.alert_freshness(repost, {'ats': 'linkedin'}, None, {}, time.time())
        self.assertIsNone(expiry)
        self.assertIn('Older posting', reason)

    def test_v4_full_feed_is_retained_on_import_but_ping_keeps_its_expiry(self):
        expiry = time.time()+60
        watcher._enqueue(self.con, 'test', 'feed:intern', {'content': 'feed'}, expiry)
        watcher._enqueue(self.con, 'test', 'ping:intern', {'content': 'ping'}, expiry)
        watcher.export_state(self.con)
        state = json.loads(self.state.read_text())
        state['version'] = 4
        self.state.write_text(json.dumps(state))
        restored = watcher.db_open(':memory:')
        try:
            watcher.import_state(restored)
            self.assertEqual({'feed:intern': 0, 'ping:intern': expiry},
                             dict(restored.execute('SELECT route,expires_at FROM outbox')))
        finally:
            restored.close()

    def test_rate_limited_ping_does_not_starve_full_feed(self):
        for i in range(12):
            watcher._enqueue(self.con, 'test', 'ping:intern', {'content': str(i)}, time.time()+3600)
        self.con.execute("UPDATE outbox SET next_try=? WHERE id=(SELECT id FROM outbox LIMIT 1)", (time.time()+600,))
        watcher._enqueue(self.con, 'test', 'feed:intern', {'content': 'feed'}, 0)
        with patch('watcher._post_with_retry', return_value=True) as send:
            watcher.flush_outbox(self.con, [self.sub])
        self.assertEqual(1, send.call_count)
        self.assertEqual('feed:intern', send.call_args.args[1])

    def test_partial_pagination_preserves_provider_retry_after(self):
        error = watcher.requests.HTTPError('rate limited', response=response({}, 429, {'Retry-After': '600'}))
        calls = iter([([{'id': '1'}], 2), error])
        def page(offset):
            result = next(calls)
            if isinstance(result, Exception):
                raise result
            return result
        result = watcher._mapped(watcher._pages(page, lambda r: r['id'], {}), lambda r: r)
        self.assertEqual(600, result.retry_after)
        self.assertEqual(1, len(result))


if __name__ == '__main__':
    unittest.main()
