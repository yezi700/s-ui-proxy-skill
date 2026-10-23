import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import check_dns


class DnsTests(unittest.TestCase):
    def test_domain_validation(self):
        self.assertEqual(check_dns.domain_name('JP.Example.com.'), 'jp.example.com')
        for value in ('https://example.com', 'example.com:443', 'a..com', '-a.com',
                      '*.example.com', '127.0.0.1', 'localhost', 'x' * 64 + '.com'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_dns.domain_name(value)

    def test_cname_and_duplicate_answers(self):
        payload = {'Status': 0, 'Answer': [
            {'type': 5, 'data': 'origin.example.com'},
            {'type': 1, 'data': '203.0.113.10'}, {'type': 1, 'data': '203.0.113.10'}]}
        self.assertEqual(check_dns.parse_answer(payload, 'A'), {'203.0.113.10'})
        self.assertEqual(check_dns.parse_answer({'Status': 0}, 'AAAA'), set())

    def test_error_is_not_empty_answer(self):
        for payload in ({'Status': 3}, {'Status': 2}, {}, [], {'Status': 0, 'TC': True},
                        {'Status': 0, 'Answer': None},
                        {'Status': 0, 'Answer': [{'type': 1, 'data': '::1'}]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                check_dns.parse_answer(payload, 'A')

    def test_pass_ipv4_and_dual_stack(self):
        for ipv6 in (None, '2001:db8::1'):
            def lookup(resolver, domain, kind, timeout):
                return {'203.0.113.10'} if kind == 'A' else ({ipv6} if ipv6 else set())
            passed, messages = check_dns.check('jp.example.com', '203.0.113.10', ipv6, lookup=lookup)
            self.assertTrue(passed)
            self.assertEqual(len(messages), 4)

    def test_extra_ip_stale_aaaa_and_resolver_disagreement_fail(self):
        for scenario in ('extra', 'aaaa', 'disagreement', 'timeout'):
            def lookup(resolver, domain, kind, timeout):
                if scenario == 'timeout' and kind == 'AAAA':
                    raise TimeoutError('offline')
                if kind == 'AAAA':
                    return {'2001:db8::1'} if scenario == 'aaaa' else set()
                if scenario == 'extra':
                    return {'203.0.113.10', '203.0.113.11'}
                if scenario == 'disagreement' and resolver == 'Google':
                    return set()
                return {'203.0.113.10'}
            with self.subTest(scenario=scenario):
                passed, _ = check_dns.check('jp.example.com', '203.0.113.10', lookup=lookup)
                self.assertFalse(passed)

    def test_query_uses_https_json_and_timeout(self):
        with patch.object(check_dns.urllib.request, 'urlopen') as opening:
            opening.return_value.__enter__.return_value = io.StringIO(json.dumps({'Status': 0}))
            self.assertEqual(check_dns.query('Google', 'jp.example.com', 'AAAA', 7), set())
            args, kwargs = opening.call_args
            self.assertEqual(args[0].full_url, 'https://dns.google/resolve?name=jp.example.com&type=AAAA')
            self.assertEqual(kwargs['timeout'], 7)

    def test_cli_exit_codes(self):
        for success, code in ((True, 0), (False, 1)):
            with patch.object(check_dns, 'check', return_value=(success, [])), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check_dns.main(['--domain', 'jp.example.com', '--ipv4', '203.0.113.10']), code)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            check_dns.main(['--domain', 'jp.example.com', '--ipv4', 'bad'])
        self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
