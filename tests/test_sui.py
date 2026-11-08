#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线回归测试：不连 VPS、不依赖网络。

跑法：
    python -m unittest discover -s tests -v
    python tests/test_sui.py
"""
import base64
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import deploy_nodes  # noqa: E402
import render_report  # noqa: E402
import ssh_run  # noqa: E402
import sui_api  # noqa: E402


# ---------------------------------------------------------------- 密钥与随机

class TestKeyGen(unittest.TestCase):
    def test_b64url_no_padding(self):
        raw = os.urandom(32)
        out = deploy_nodes.b64url(raw)
        self.assertNotIn("=", out)
        self.assertNotIn("+", out)
        self.assertNotIn("/", out)
        # 43 字符 = 32 字节 base64url 无填充，与 Xray 的 privateKey 长度一致
        self.assertEqual(len(out), 43)

    def test_gen_password_charset(self):
        for _ in range(20):
            pw = deploy_nodes.gen_password()
            self.assertEqual(len(pw), 22)
            self.assertRegex(pw, r"^[A-Za-z0-9]+$")

    def test_short_id_is_16_hex(self):
        sid = deploy_nodes.short_id()
        self.assertEqual(len(sid), 16)
        self.assertRegex(sid, r"^[0-9a-f]{16}$")

    def test_ss_key_length_matches_method(self):
        """SS2022 的 psk 长度由 method 决定，长度不对核心起不来。"""
        import base64
        cases = {
            "2022-blake3-aes-128-gcm": 16,
            "2022-blake3-aes-256-gcm": 32,
            "2022-blake3-chacha20-poly1305": 32,
        }
        for method, want in cases.items():
            for _ in range(5):
                psk = deploy_nodes.gen_ss_key(method)
                self.assertEqual(len(base64.b64decode(psk)), want,
                                 "%s 的 psk 应是 %d 字节" % (method, want))
                # 必须是标准 base64（带 = 填充），不能用 urlsafe
                self.assertNotIn("-", psk)
                self.assertNotIn("_", psk)


# ---------------------------------------------------------------- 节点声明

class TestNodeSpecs(unittest.TestCase):
    """默认节点集：AnyTLS / Hysteria2 / Reality / Shadowsocks 2022。

    TUIC 默认关（pitfalls §8），要开得显式给 TUIC_PORT。
    node_specs 内部有进程内缓存，所以每个用例都构造不同的 cfg 键值。
    """

    def setUp(self):
        self.cfg = {"DOMAIN": "example.com", "NODE_PREFIX": "T1"}
        self.specs = deploy_nodes.node_specs(self.cfg)
        self.by_type = {s["type"]: s for s in self.specs}

    def test_default_four_nodes(self):
        self.assertEqual(len(self.specs), 4)
        self.assertEqual(set(self.by_type),
                         {"anytls", "hysteria2", "vless", "shadowsocks"})

    def test_tuic_off_by_default(self):
        self.assertNotIn("tuic", self.by_type)

    def test_tuic_only_when_port_given(self):
        specs = deploy_nodes.node_specs(
            {"DOMAIN": "example.com", "NODE_PREFIX": "T2", "TUIC_PORT": "8443"})
        self.assertIn("tuic", {s["type"] for s in specs})

    def test_ss_off_when_port_blank(self):
        """显式留空 = 关掉（区别于键不存在）。"""
        specs = deploy_nodes.node_specs(
            {"DOMAIN": "example.com", "NODE_PREFIX": "T3", "SS_PORT": ""})
        self.assertNotIn("shadowsocks", {s["type"] for s in specs})

    def test_no_443(self):
        """443 被针对性干扰，任何节点都不该用 443。"""
        for s in self.specs:
            self.assertNotEqual(s["port"], 443, "%s 不该用 443" % s["tag"])

    def test_ports_unique_per_transport(self):
        """同号端口只允许出现在不同传输层（如 8443/tcp 与 8443/udp）。"""
        tcp = [s["port"] for s in self.specs
               if s["type"] in ("anytls", "vless", "shadowsocks")]
        udp = [s["port"] for s in self.specs if s["type"] in ("tuic", "hysteria2")]
        self.assertEqual(len(tcp), len(set(tcp)))
        self.assertEqual(len(udp), len(set(udp)))

    def test_tuic_uses_dedicated_tls(self):
        """TUIC 必须用带 alpn h3 的独立 TLS 对象，共用 LE 会报 no ALPN。"""
        specs = {s["type"]: s for s in deploy_nodes.node_specs(
            {"DOMAIN": "example.com", "NODE_PREFIX": "T4", "TUIC_PORT": "8443"})}
        self.assertEqual(specs["tuic"]["tls"], "tuic")

    def test_shadowsocks_has_no_tls(self):
        """SS 自己就是加密协议，不该挂 TLS 对象。"""
        self.assertIsNone(self.by_type["shadowsocks"]["tls"])

    def test_reality_has_vision_flow(self):
        self.assertEqual(self.by_type["vless"]["out_extra"].get("flow"), "xtls-rprx-vision")

    def test_ports_overridable(self):
        cfg = {"DOMAIN": "x.com", "NODE_PREFIX": "T5",
               "ANYTLS_PORT": "9443", "REALITY_PORT": "2443"}
        specs = {s["type"]: s for s in deploy_nodes.node_specs(cfg)}
        self.assertEqual(specs["anytls"]["port"], 9443)
        self.assertEqual(specs["vless"]["port"], 2443)

    def test_specs_cached_per_config(self):
        """同一配置两次调用必须拿到同一把 SS psk，否则入站与客户端凭据对不上。"""
        cfg = {"DOMAIN": "example.com", "NODE_PREFIX": "T6"}
        a = [s for s in deploy_nodes.node_specs(cfg) if s["type"] == "shadowsocks"][0]
        b = [s for s in deploy_nodes.node_specs(cfg) if s["type"] == "shadowsocks"][0]
        self.assertEqual(a["out_extra"]["password"], b["out_extra"]["password"])


# ---------------------------------------------------------------- 配置解析

class TestLoadEnv(unittest.TestCase):
    def _write(self, text):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".env")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return path

    def _load(self, text):
        path = self._write(text)
        old = os.getcwd()
        try:
            os.chdir(os.path.dirname(path))
            os.rename(path, os.path.join(os.getcwd(), "deploy.env"))
            return ssh_run.load_env()
        finally:
            os.chdir(old)
            try:
                os.remove(os.path.join(os.path.dirname(path), "deploy.env"))
            except OSError:
                pass

    def test_inline_comment_stripped(self):
        """`ANYTLS_PORT=8443  # TCP` 里的值必须只是 8443。"""
        cfg = self._load("DOMAIN=a.com\nANYTLS_PORT=8443     # TCP\n")
        self.assertEqual(cfg["ANYTLS_PORT"], "8443")

    def test_quoted_value_keeps_hash(self):
        cfg = self._load('DOMAIN=a.com\nPANEL_PASS="ab#cd"\n')
        self.assertEqual(cfg["PANEL_PASS"], "ab#cd")

    def test_plain_values(self):
        cfg = self._load("DOMAIN=a.com\nPANEL_PORT=46821\n")
        self.assertEqual(cfg["DOMAIN"], "a.com")
        self.assertEqual(cfg["PANEL_PORT"], "46821")


# ---------------------------------------------------------------- 链接解析

class TestLinkParams(unittest.TestCase):
    def test_anytls(self):
        p = render_report.link_params(
            "anytls://pw@a.com:8443?security=tls&sni=a.com#US-AnyTLS-8443 x")
        self.assertEqual(p["scheme"], "anytls")
        self.assertEqual(p["host"], "a.com")
        self.assertEqual(p["port"], 8443)
        self.assertEqual(p["name"], "US-AnyTLS-8443")
        self.assertEqual(p["params"]["sni"], "a.com")

    def test_reality(self):
        p = render_report.link_params(
            "vless://uuid@a.com:2053?security=reality&pbk=KEY&sid=abc&sni=www.apple.com#N")
        self.assertEqual(p["params"]["pbk"], "KEY")
        self.assertEqual(p["params"]["sni"], "www.apple.com")

    def test_malformed_returns_empty_fields(self):
        p = render_report.link_params("not a url")
        self.assertEqual(p["host"], "")
        self.assertEqual(p["params"], {})


# ---------------------------------------------------------------- 兼容性表

class TestCompat(unittest.TestCase):
    """兼容性表按实际入站类型动态出列，这里验证「支持/不支持」的判定没错。"""

    def _sup(self, keyword):
        return next(sup for name, sup in render_report.CLIENT_SUPPORT if keyword in name)

    def test_columns_follow_inbounds(self):
        d = {"inbounds": [
            {"tag": "a", "type": "anytls"},
            {"tag": "b", "type": "vless"},
            {"tag": "c", "type": "shadowsocks"},
        ]}
        self.assertEqual(render_report.compat_columns(d), ["anytls", "vless", "shadowsocks"])

    def test_unknown_type_goes_last(self):
        d = {"inbounds": [{"tag": "x", "type": "wireguard"}, {"tag": "a", "type": "anytls"}]}
        self.assertEqual(render_report.compat_columns(d), ["anytls", "wireguard"])

    def test_xray_row(self):
        """Xray 内核只认 Reality 和 Shadowsocks —— 这行不能写错。"""
        sup = self._sup("Xray")
        self.assertEqual(sup["anytls"], "否")
        self.assertEqual(sup["hysteria2"], "否")
        self.assertEqual(sup["vless"], "是")
        self.assertEqual(sup["shadowsocks"], "是")

    def test_singbox_all_yes(self):
        sup = self._sup("sing-box")
        for proto in ("anytls", "hysteria2", "vless", "shadowsocks"):
            self.assertEqual(sup[proto], "是", "sing-box 应该支持 %s" % proto)

    def test_every_client_covers_every_known_proto(self):
        """漏写一个协议会让渲染出 "?" —— 表里不该出现问号。"""
        for name, sup in render_report.CLIENT_SUPPORT:
            for proto in render_report.PROTO_META:
                self.assertIn(proto, sup, "%s 缺 %s 的判定" % (name, proto))

    def test_remark_wording(self):
        sup = {"anytls": "是", "vless": "是"}
        self.assertEqual(render_report.compat_remark(["anytls", "vless"], sup), "全部支持")
        self.assertEqual(render_report.compat_remark(["tuic"], sup), "都不支持")


# ---------------------------------------------------------------- API 客户端

class TestSuiApi(unittest.TestCase):
    def test_save_client_fills_links(self):
        """不补 links 会报 unexpected end of JSON input。"""
        captured = {}

        class Dummy(sui_api.SuiApi):
            def __init__(self):
                pass

            def save(self, obj, action, data):
                captured["obj"] = obj
                captured["action"] = action
                captured["data"] = data
                return {}

        Dummy().save_client({"name": "us", "enable": True})
        self.assertEqual(captured["obj"], "clients")
        self.assertEqual(captured["action"], "new")
        self.assertEqual(captured["data"]["links"], [])
        self.assertEqual(captured["data"]["inbounds"], [])

    def test_save_client_edit_when_id(self):
        captured = {}

        class Dummy(sui_api.SuiApi):
            def __init__(self):
                pass

            def save(self, obj, action, data):
                captured["action"] = action
                return {}

        Dummy().save_client({"id": 3, "name": "us"})
        self.assertEqual(captured["action"], "edit")

    def test_del_client_sends_bare_id(self):
        """clients 的 del 的 data 是裸数字，不是 {"id": n}。"""
        captured = {}

        class Dummy(sui_api.SuiApi):
            def __init__(self):
                pass

            def save(self, obj, action, data):
                captured["data"] = data
                return {}

        Dummy().del_client(7)
        self.assertEqual(captured["data"], 7)

    def test_del_inbound_sends_tag_as_json_string(self):
        """inbounds 的 del 传的是 tag 的 JSON 字符串字面量。

        传 id 数字 → cannot unmarshal number；传 {"id":n} → cannot unmarshal object；
        传 '"<id>"' → 返回 success 但什么都没删（pitfalls §3.5）。
        """
        captured = {}

        class Dummy(sui_api.SuiApi):
            def __init__(self):
                pass

            def save(self, obj, action, data):
                captured["data"] = data
                return {}

        Dummy().del_inbound("JP-Shadowsocks-2087")
        # save() 收到的是「已经序列化好的 JSON 文本」，所以这里必须带引号
        self.assertEqual(captured["data"], '"JP-Shadowsocks-2087"')

    def test_settings_bools_become_lowercase_strings(self):
        captured = {}

        class Dummy(sui_api.SuiApi):
            def __init__(self):
                pass

            def save(self, obj, action, data):
                captured["data"] = data
                return {}

        Dummy().save_settings({"subShowInfo": True, "subPort": 2096})
        self.assertEqual(captured["data"]["subShowInfo"], "true")
        self.assertEqual(captured["data"]["subPort"], "2096")


# ---------------------------------------------------------------- 脚本静态检查

class TestScripts(unittest.TestCase):
    def test_no_vendor_words(self):
        """仓库里不该出现厂商字样。"""
        # 拼接构造，避免本文件自身被自己的规则命中
        bad = re.compile("work" + "buddy|code" + "buddy|clau" + "de", re.I)
        hits = []
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules")]
            for fn in files:
                if not fn.endswith((".md", ".py", ".sh", ".example", ".txt")):
                    continue
                path = os.path.join(base, fn)
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        if bad.search(line):
                            hits.append("%s:%d" % (os.path.relpath(path, ROOT), i))
        self.assertEqual(hits, [], "发现厂商字样：" + ", ".join(hits))

    def test_scripts_have_shebang(self):
        for name in os.listdir(os.path.join(ROOT, "scripts")):
            if not name.endswith((".py", ".sh")):
                continue
            path = os.path.join(ROOT, "scripts", name)
            with open(path, encoding="utf-8") as fh:
                first = fh.readline()
            self.assertTrue(first.startswith("#!"), "%s 缺 shebang" % name)

    def test_verify_mentions_firewall_limit(self):
        """验证脚本必须明确说明它验证不了防火墙。"""
        path = os.path.join(ROOT, "scripts", "verify_nodes.sh")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("不经过 INPUT 链", text)
        self.assertIn("没有验证防火墙", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
