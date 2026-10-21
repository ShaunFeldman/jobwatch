import json
import unittest
from unittest.mock import patch

import requests
import discovery_verification as verification


class VerificationTests(unittest.TestCase):
    def setUp(self):
        verification.CACHE.clear()
        self.job = {'id': '123', 'url': 'https://www.linkedin.com/jobs/view/123',
                    'company': 'Example', 'title': 'Software Engineer Intern', 'posted': 999}
        self.official = 'https://careers.example.com/jobs/456'
        self.detail = '<a class="apply-button" href="' + self.official + '">Apply</a>'
        self.page = '<script type="application/ld+json">' + json.dumps({
            '@type': 'JobPosting', 'title': self.job['title'], 'datePosted': '2026-10-20'}) + '</script>'

    def tearDown(self):
        verification.CACHE.clear()

    def test_resolves_employer_date_and_reuses_cached_result(self):
        with patch.object(verification, '_get', side_effect=[('', self.detail), (self.official, self.page)]) as get:
            rows = [dict(self.job)]
            verification.enrich(rows)
            self.assertEqual(self.official, rows[0]['url'])
            self.assertEqual('2026-10-20', rows[0]['posted'])
            self.assertTrue(rows[0]['_official_posted'])
            self.assertEqual(self.job['url'], rows[0]['_discovery_url'])
            verification.enrich([dict(self.job)])
            self.assertEqual(2, get.call_count)

    def test_blocked_page_retains_discovery_without_official_claim(self):
        with patch.object(verification, '_get', side_effect=requests.Timeout):
            verification.enrich([self.job])
        self.assertTrue(verification.linkedin(self.job['url']))
        self.assertNotIn('_official_posted', self.job)

    def test_cannot_use_linkedin_or_aggregator_structured_date(self):
        self.assertIsNone(verification.official_date(self.page, self.job['url'], self.job))
        self.assertIsNone(verification.official_date(self.page, 'https://aggregator.com/jobs/456', self.job))
        self.assertIsNone(verification.official_date(self.page.replace('Intern', 'Manager'), self.official, self.job))
        self.assertIsNone(verification.official_date(self.page.replace('datePosted', 'dateModified'), self.official, self.job))

    def test_community_employer_link_is_verified_without_linkedin_request(self):
        self.job['url'] = self.official
        with patch.object(verification, '_get', return_value=(self.official, self.page)) as get:
            verification.enrich([self.job])
        get.assert_called_once_with(self.official)
        self.assertTrue(self.job['_official_posted'])

    def test_request_batch_is_bounded_and_failed_urls_are_cached(self):
        rows = [dict(self.job, id=str(i), url=f'https://www.linkedin.com/jobs/view/{i}') for i in range(10)]
        with patch.object(verification, '_get', side_effect=requests.Timeout) as get:
            verification.enrich(rows, max_requests=2)
            self.assertEqual(2, get.call_count)
            verification.enrich(rows, max_requests=2)
            self.assertEqual(4, get.call_count)

    def test_private_application_addresses_are_rejected(self):
        with patch.object(verification.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]), patch.object(verification.requests, 'get') as get:
            with self.assertRaises(ValueError):
                verification._get('https://local.example/jobs')
        get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
