import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import watcher


def response(data, status=200, headers=None):
    r = Mock(status_code=status, headers=headers or {})
    r.json.return_value = data
    r.raise_for_status.return_value = None
    if status >= 400:
        r.raise_for_status.side_effect = watcher.requests.HTTPError(f"HTTP {status}", response=r)
    return r


def job(jid='1', **extra):
    return dict(id=jid, title='Software Engineer Intern', location='Toronto',
                url=f'https://example.com/jobs/{jid}', company='Example', **extra)


def subscriber(name='test', **extra):
    result = dict(name=name, companies={'all'}, filt=watcher.Filt({}), watch=re.compile('Example'),
                  ping_categories={'intern'}, max_ping_jobs=2, discord='https://secret.example/hook',
                  ping_hooks={}, feeds={}, mention='', telegram_chat='', ops=False, digest=False)
    result.update(extra)
    return result


class PaginationTests(unittest.TestCase):
    @patch('watcher.requests.post')
    def test_workday_reads_past_300_and_keeps_first_total(self, post):
        def fetch(*args, **kwargs):
            offset = kwargs['json']['offset']
            rows = [{'externalPath': f'/job/{i}', 'title': 'Engineer'} for i in range(offset, min(offset + 20, 325))]
            return response({'jobPostings': rows, **({'total': 325} if offset == 0 else {})})
        post.side_effect = fetch
        result = watcher.fetch_workday(dict(host='test', tenant='test', site='test'))
        self.assertEqual(325, len(result))
        self.assertFalse(result.warnings)
        self.assertEqual(17, post.call_count)

    def test_malformed_row_does_not_hide_valid_jobs_on_later_pages(self):
        fetch = Mock(side_effect=[([{'id': 1}, {}], 3), ([{'id': 3}], 3)])
        result = watcher._pages(fetch, lambda j: j.get('id'), {})
        self.assertEqual([1, 3], [j['id'] for j in result])
        self.assertTrue(result.warnings)
        self.assertEqual(2, fetch.call_count)

    @patch('watcher.requests.get')
    def test_smartrecruiters_follows_reported_total(self, get):
        get.side_effect = [response({'content': [{'id': 1, 'name': 'One'}], 'totalFound': '2'}),
                           response({'content': [{'id': 2, 'name': 'Two'}], 'totalFound': '2'})]
        result = watcher.fetch_smartrecruiters({'token': 'test'})
        self.assertEqual(['1', '2'], [j['id'] for j in result])
        self.assertEqual(1, get.call_args_list[1].kwargs['params']['offset'])

    @patch('watcher.requests.get')
    def test_amazon_follows_hits(self, get):
        get.side_effect = [response({'jobs': [{'id_icims': 'a', 'job_path': '/a'}], 'hits': 2}),
                           response({'jobs': [{'id_icims': 'b', 'job_path': '/b'}], 'hits': 2})]
        self.assertEqual(2, len(watcher.fetch_amazon_jobs({})))
        self.assertEqual(1, get.call_args_list[1].kwargs['params']['offset'])

    @patch('watcher.requests.get')
    def test_eightfold_no_longer_stops_at_100(self, get):
        def fetch(*args, **kwargs):
            offset = kwargs['params']['start']
            return response({'positions': [{'id': i + 1, 'name': 'Engineer'} for i in range(offset, min(offset + 10, 125))], 'count': 125})
        get.side_effect = fetch
        self.assertEqual(125, len(watcher.fetch_eightfold({'host': 'test', 'domain': 'test'})))

    @patch('watcher.requests.post')
    def test_phenom_follows_total_hits(self, post):
        post.side_effect = [response({'refineSearch': {'data': {'jobs': [{'jobId': '1'}], 'totalHits': 2}}}),
                            response({'refineSearch': {'data': {'jobs': [{'jobId': '2'}], 'totalHits': 2}}})]
        self.assertEqual(2, len(watcher.fetch_phenom({'host': 'test'})))
        self.assertEqual(1, post.call_args_list[1].kwargs['json']['from'])

    def test_page_error_preserves_recovered_jobs_as_partial(self):
        fetch = Mock(side_effect=[([{'id': 1}], 2), watcher.requests.Timeout('slow')])
        result = watcher._pages(fetch, lambda j: j['id'], {})
        self.assertEqual([{'id': 1}], result)
        self.assertIn('Timeout', result.warnings[0])

    def test_repeated_page_cannot_loop_forever(self):
        fetch = Mock(return_value=([{'id': 1}], 20))
        result = watcher._pages(fetch, lambda j: j['id'], {})
        self.assertEqual(1, len(result))
        self.assertIn('repeated', result.warnings[0])
        self.assertEqual(2, fetch.call_count)

    def test_empty_before_total_is_incomplete(self):
        fetch = Mock(side_effect=[([{'id': 1}], 2), ([], None)])
        self.assertTrue(watcher._pages(fetch, lambda j: j['id'], {}).warnings)

    @patch('watcher.time.sleep')
    @patch('watcher.requests.get')
    def test_page_retry_honors_retry_after(self, get, sleep):
        get.side_effect = [response({}, 429, {'Retry-After': '3'}), response({'jobs': []})]
        watcher._source_request('get', 'https://example.test')
        sleep.assert_called_once_with(3)

    @patch('watcher.time.sleep')
    @patch('watcher.requests.get')
    def test_long_retry_after_returns_to_scheduler(self, get, sleep):
        get.return_value = response({}, 429, {'Retry-After': '120'})
        with self.assertRaises(watcher.requests.HTTPError):
            watcher._source_request('get', 'https://example.test')
        sleep.assert_not_called()


class ListingTests(unittest.TestCase):
    def parse(self, text):
        with patch('watcher.requests.get', return_value=Mock(text=text)):
            return watcher.fetch_github_md({'repo': 'test/test', 'cols': {'company': 0, 'title': 1, 'location': 2, 'url': 6}})

    def test_reordered_header_and_multiple_requisitions(self):
        rows = self.parse('| Company | Role | Apply | Location |\n|---|---|---|---|\n| Demo | SWE Intern | [Apply](https://x/1) [#2](https://x/2) | Toronto |')
        self.assertEqual(2, len(rows))
        self.assertEqual(['https://x/1', 'https://x/2'], [j['url'] for j in rows])
        self.assertEqual('Toronto', rows[0]['location'])

    def test_html_posting_column(self):
        rows = self.parse('| Company | Position | Location | Salary | Posting | Age |\n| Demo | SWE Intern | Seattle | $50/hr | <a href="https://x/1"><img src="apply.png"></a> | 2d |')
        self.assertEqual('https://x/1', rows[0]['url'])
        self.assertEqual('$50/hr', rows[0]['salary'])

    def test_continuation_company_survives_closed_row(self):
        rows = self.parse('| Company | Role | Apply | Location |\n| Demo | SWE | 🔒 | NY |\n| ↳ | Intern | [Apply](https://x/1) | Toronto |')
        self.assertEqual('Demo', rows[0]['company'])

    def test_changed_format_does_not_report_successful_empty_board(self):
        with self.assertRaises(ValueError):
            self.parse('# Site moved\nNo parseable job table')

    @patch('watcher.requests.get')
    def test_hightouch_reads_official_embedded_data(self, get):
        data = {'props': {'pageProps': {'jobPostings': [{'id': 'abc', 'title': 'Intern', 'locationName': 'Remote'}]}}}
        get.return_value = Mock(text='<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script>')
        self.assertEqual('https://hightouch.com/careers/abc', watcher.fetch_hightouch({})[0]['url'])

    @patch('watcher.requests.get')
    def test_ashby_preserves_secondary_locations_for_filters(self, get):
        get.return_value = response({'jobs': [{'id': 'a', 'title': 'Intern',
            'jobUrl': 'https://x/a', 'location': 'San Francisco',
            'secondaryLocations': [{'location': 'New York'}]}]})
        rows = watcher.fetch_ashby({'token': 'test'})
        self.assertTrue(watcher.Filt({'location': 'New York'}).ok(rows[0]))

    def test_partial_linkedin_queries_are_visible_without_dropping_other_jobs(self):
        def query(cfg, q, location, out, seen):
            if q == 'bad':
                raise watcher.requests.Timeout()
            out.extend([job('1000000'), job('99999999')])
        with patch('watcher._linkedin_query', side_effect=query):
            rows = watcher.fetch_linkedin({'queries': ['good', 'bad']})
        self.assertEqual(2, len(rows))  # Old ID drift heuristic discarded the first.
        self.assertTrue(rows.warnings)

    def test_distinct_job_query_ids_survive_tracking_cleanup(self):
        a = {'url': 'https://example.com/jobs?jobId=123&utm_source=email'}
        b = {'url': 'https://example.com/jobs?jobId=456&utm_source=email'}
        self.assertNotEqual(watcher.dedupe_keys(a), watcher.dedupe_keys(b))
        self.assertEqual(watcher.canon_url(a['url']), 'example.com/jobs?jobId=123')

    def test_distinct_same_title_jobs_not_suppressed_by_legacy_name_key(self):
        con = watcher.db_open(':memory:')
        watcher.mark_alerted(con, job('1'))
        con.execute('INSERT OR IGNORE INTO alerted VALUES (?,?)', (watcher.name_key('Example', 'Software Engineer Intern'), watcher.now()))
        self.assertFalse(watcher.already_alerted(con, job('2')))
        self.assertTrue(watcher.already_alerted(con, job('1')))
        con.close()


class DurableDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state.json'
        self.patch = patch.object(watcher, 'STATE_JSON', self.state)
        self.patch.start()
        self.con = watcher.db_open(':memory:')

    def tearDown(self):
        self.con.close()
        self.patch.stop()
        self.tmp.cleanup()

    def test_failed_ping_survives_restart_without_replaying_successful_feed(self):
        sub = subscriber()
        watcher.deliver(sub, [('demo', job())], self.con)
        watcher.flush_feeds({}, self.con, [sub], force=True)
        self.assertEqual(2, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        with patch('watcher._post_with_retry', side_effect=[False, True]):
            watcher.flush_outbox(self.con, [sub])
        remaining = self.con.execute('SELECT route, attempts FROM outbox').fetchall()
        self.assertEqual([('ping:intern', 1)], remaining)
        watcher.export_state(self.con)
        self.assertNotIn('secret.example', self.state.read_text())
        restored = watcher.db_open(':memory:')
        watcher.import_state(restored)
        restored.execute('UPDATE outbox SET next_try=0')
        with patch('watcher._post_with_retry', return_value=True) as send:
            watcher.flush_outbox(restored, [sub])
        self.assertEqual(1, send.call_count)
        self.assertEqual(0, restored.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        restored.close()

    def test_missing_delivery_secret_never_discards_queued_jobs(self):
        sub = subscriber(discord='')
        watcher.deliver(sub, [('demo', job())], self.con)
        watcher.flush_feeds({}, self.con, [sub], force=True)
        with patch('watcher.requests.post') as post:
            watcher.flush_outbox(self.con, [sub])
        post.assert_not_called()
        self.assertEqual(2, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])

    @patch('watcher.requests.post')
    @patch('watcher.time.sleep')
    def test_delivery_rate_limit_persists_provider_cooldown(self, sleep, post):
        watcher._enqueue(self.con, 'test', 'feed:intern', {'content': 'hello'})
        post.return_value = response({'retry_after': 600}, 429, {'Retry-After': '600'})
        before = time.time()
        watcher.flush_outbox(self.con, [subscriber()])
        retry = self.con.execute('SELECT next_try FROM outbox').fetchone()[0]
        self.assertGreaterEqual(retry, before + 600)
        sleep.assert_not_called()
        post.reset_mock()
        watcher.flush_outbox(self.con, [subscriber()])
        post.assert_not_called()

    def test_health_round_trip(self):
        self.con.execute("INSERT INTO boards(board,seeded,last_ok,failures,last_error,last_count,next_poll) VALUES ('test',1,'2026-10-19',4,'HTTP 429',42,9999999999)")
        watcher.export_state(self.con)
        restored = watcher.db_open(':memory:')
        watcher.import_state(restored)
        self.assertEqual(('2026-10-19', 4, 'HTTP 429', 42, 9999999999), restored.execute('SELECT last_ok,failures,last_error,last_count,next_poll FROM boards').fetchone())
        restored.close()

    def run_cycle(self, result, boards=None, subs=None):
        config = {'boards': boards or {'demo': {'ats': 'greenhouse', 'token': 'test'}}}
        with patch('watcher.fetch_board', return_value=('demo', result)), patch('watcher.load_subscribers', return_value=subs or []), patch('watcher.flush_outbox'):
            watcher.cycle(config, self.con)

    def test_suspicious_empty_result_is_failure_and_does_not_erase_known_jobs(self):
        self.con.execute("INSERT INTO boards(board,seeded) VALUES ('demo',1)")
        for i in range(5):
            self.con.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)', ('demo', str(i), 'Engineer', 'Toronto', 'https://x/1', watcher.now(), watcher.now()))
        self.run_cycle(watcher.FetchResult())
        self.assertEqual((1, 0), self.con.execute('SELECT failures,last_count FROM boards').fetchone())
        self.assertEqual(5, self.con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0])
        self.assertEqual((0, 1), self.con.execute('SELECT ok_count,fail_count FROM runs').fetchone())

    def test_partial_result_is_stored_and_retried_after_backoff(self):
        self.run_cycle(watcher.FetchResult([job()], ['page 2 timed out']))
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0])
        row = self.con.execute('SELECT failures,last_ok,last_error,next_poll FROM boards').fetchone()
        self.assertEqual((1, None, 'page 2 timed out'), row[:3])
        self.assertGreater(row[3], time.time())
        with patch('watcher.fetch_board') as fetch, patch('watcher.load_subscribers', return_value=[]), patch('watcher.flush_outbox'):
            watcher.cycle({'boards': {'demo': {'ats': 'greenhouse'}}}, self.con)
            fetch.assert_not_called()

    def test_per_subscriber_source_selection_precedes_cross_source_dedupe(self):
        boards = {'direct': {'ats': 'greenhouse'}, 'repo': {'ats': 'github_listings'}}
        for board in boards:
            self.con.execute('INSERT INTO boards(board,seeded) VALUES (?,1)', (board,))
        self.run_cycle(watcher.FetchResult([job()]), boards, [subscriber(companies={'repo'})])
        self.assertEqual(1, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])

    def test_first_run_is_silent(self):
        self.run_cycle(watcher.FetchResult([job()]), subs=[subscriber()])
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM pending').fetchone()[0])
        self.assertEqual(0, self.con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
