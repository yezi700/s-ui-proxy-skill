"""订阅相关的离线回归测试：不连 VPS、不访问网络。

锁死两类真实踩过的坑：
1. `@@@` 分段被无换行的 curl 输出粘行 → 段落丢失 → 报告「节点数：0」
2. allLinks 在 v3.9 / v3.10 返回形状不同；订阅优先于 allLinks
3. Clash 订阅（subClashEnable）未启用时不能把 404 地址写进交付文档
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import render_report as rr


class SectionParseTests(unittest.TestCase):
    def test_normal_sections(self):
        text = '@@@A\n1\n2\n@@@B\n3\n@@@END\n'
        self.assertEqual(rr.parse_sections(text), {'A': '1\n2', 'B': '3', 'END': ''})

    def test_marker_glued_to_previous_output(self):
        """curl 输出末尾无换行时，`}` 与 `@@@NEXT` 会粘在同一行。"""
        text = '@@@LINKS\n{"success":true}@@@INBOUNDS\n{"success":true}@@@SUB\nvless://x@@@FW4\nok'
        sec = rr.parse_sections(text)
        # 两个段落都不能丢
        self.assertEqual(sec['LINKS'], '{"success":true}')
        self.assertEqual(sec['INBOUNDS'], '{"success":true}')
        self.assertEqual(sec['SUB'], 'vless://x')
        self.assertEqual(sec['FW4'], 'ok')

    def test_empty_text(self):
        self.assertEqual(rr.parse_sections(''), {})


class KvBlockTests(unittest.TestCase):
    def test_empty_value_is_kept(self):
        # webDomain 为空是「未启用」的合法状态，不能被吞掉
        self.assertEqual(rr.kv_block('webDomain=\nsubClashEnable=1'),
                         {'webDomain': '', 'subClashEnable': '1'})

    def test_value_containing_equals(self):
        self.assertEqual(rr.kv_block('token=a=b')['token'], 'a=b')


class NormalizeLinkTests(unittest.TestCase):
    def test_dict_passthrough(self):
        item = {'remark': 'JP-Reality', 'uri': 'vless://u@h:443?x=1', 'port': 443}
        self.assertEqual(rr.normalize_link(item), item)

    def test_v310_string_shape(self):
        """v3.10.x 的 allLinks 返回纯字符串数组。"""
        got = rr.normalize_link('hysteria2://pw@us.example.com:443?alpn=h3#US-HY2')
        self.assertEqual(got['protocol'], 'hysteria2')
        self.assertEqual(got['port'], 443)
        self.assertEqual(got['remark'], 'US-HY2')
        self.assertEqual(got['uri'], 'hysteria2://pw@us.example.com:443?alpn=h3#US-HY2')

    def test_string_without_port(self):
        got = rr.normalize_link('vless://uuid@host?type=tcp')
        self.assertEqual(got['port'], '')
        self.assertEqual(got['remark'], '')

    def test_tuic_userinfo_with_colon(self):
        """tuic 的 userinfo 是 uuid:password，端口要取 @ 之后那段。"""
        got = rr.normalize_link('tuic://uuid:pass@us.example.com:8443?alpn=h3#T')
        self.assertEqual(got['port'], 8443)
        self.assertEqual(got['protocol'], 'tuic')


class ResolveLinksTests(unittest.TestCase):
    def test_subscription_wins_over_all_links(self):
        """allLinks 会同时列出原始客户端与 merged（实测 6 条），只该展示订阅的 3 条。"""
        all_links = ['vless://a@h:443#orig', 'vless://a@h:443#merged']
        sub_text = 'vless://a@h:443#merged\nhysteria2://b@h:443#hy2\ntuic://c:d@h:8443#tuic'
        got = rr.resolve_links(all_links, sub_text)
        self.assertEqual([n['protocol'] for n in got], ['vless', 'hysteria2', 'tuic'])

    def test_falls_back_to_all_links(self):
        got = rr.resolve_links(['vless://a@h:443#x'], '')
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['port'], 443)

    def test_ignores_junk_lines(self):
        got = rr.resolve_links([], '(订阅读取失败)\n\nvless://a@h:443#x\n')
        self.assertEqual(len(got), 1)

    def test_skips_non_link_types(self):
        got = rr.resolve_links([{'uri': 'vless://a@h:443#x'}, None, 42], '')
        self.assertEqual(len(got), 1)


class SubUrlTests(unittest.TestCase):
    def test_path_with_slashes(self):
        self.assertEqual(
            rr.build_sub_url('us.example.com', '2096', '/abc/', 'sid'),
            'https://us.example.com:2096/abc/sid')

    def test_path_without_slashes(self):
        self.assertEqual(
            rr.build_sub_url('us.example.com', '2096', 'abc', 'sid'),
            'https://us.example.com:2096/abc/sid')

    def test_empty_path(self):
        self.assertEqual(
            rr.build_sub_url('us.example.com', 2096, '', 'sid'),
            'https://us.example.com:2096/sid')


class ClashUrlTests(unittest.TestCase):
    def test_disabled_returns_empty(self):
        """subClashEnable=false 时该路径是 404，绝不能写进文档。"""
        self.assertEqual(
            rr.build_clash_url('h', '2096', '/clash/', 'sid', enabled=False), '')

    def test_enabled_returns_url(self):
        self.assertEqual(
            rr.build_clash_url('us.example.com', '2096', '/xp41/', 'sid', enabled=True),
            'https://us.example.com:2096/xp41/sid')

    def test_enabled_but_missing_path(self):
        self.assertEqual(
            rr.build_clash_url('h', '2096', '', 'sid', enabled=True), '')


class RenderSmokeTests(unittest.TestCase):
    def _ctx(self, clash_url=''):
        node = {'remark': 'US-Reality', 'protocol': 'vless', 'port': 443,
                'uri': 'vless://u@h:443?security=reality#US-Reality',
                'params': [('security', 'reality')]}
        return {
            'domain': 'us.example.com', 'host': '203.0.113.10',
            'os': 'Debian 13', 'panel_url': 'https://us.example.com:46821/abc/',
            'panel_user': 'admin', 'panel_pass': 'p', 'panel_port': '46821',
            'panel_path': 'abc', 'api_token': 't', 'cert_end': 'Jan 1 2027',
            'ver': '3.10.2', 'sub_url': 'https://us.example.com:2096/sub/sid',
            'clash_url': clash_url, 'merged_email': 'merged',
            'merged_subid': 'sid', 'nodes': [node],
            'fw_ports': [('443', 'TCP', 'REALITY')], 'nat_rule': '-A PREROUTING',
            'panel_dom': '', 'bench': [{'node': 'vless', 'avg': '300 ms',
                                        'success': '5/5'}],
        }

    def test_md_without_clash_says_not_enabled(self):
        md = rr.render_md(self._ctx())
        self.assertIn('Clash 系', md)
        self.assertIn('subClashEnable', md)      # 未启用时提示原因
        self.assertNotIn('subClashPath/<subId>', md)

    def test_md_with_clash_lists_both_urls(self):
        md = rr.render_md(self._ctx('https://us.example.com:2096/xp41/sid'))
        self.assertIn('https://us.example.com:2096/sub/sid', md)
        self.assertIn('https://us.example.com:2096/xp41/sid', md)
        self.assertIn('cannot unmarshal !!str', md)

    def test_html_escapes_and_includes_clash(self):
        html = rr.render_html(self._ctx('https://us.example.com:2096/xp41/sid'))
        self.assertIn('xp41/sid', html)
        self.assertIn('<h2 id="sub">', html)


if __name__ == '__main__':
    unittest.main()
