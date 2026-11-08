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

    TUIC 默认关（pitfalls §6.8），要开得显式给 TUIC_PORT。
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


# ---------------------------------------------------------------- 文档一致性

def _md_headings(text):
    return {h.lstrip("§") for h in re.findall(r"^#{2,4} (§?\d+(?:\.\d+)*)", text, re.M)}


class TestDocs(unittest.TestCase):
    """文档和代码最容易各自漂移 —— 这里把它们钉在一起。

    这两条测试都是被真实事故逼出来的：
    - 兼容性表曾经在 README 里写「Xray 只认 Reality」，而代码里早就加了 SS
    - pitfalls 章节重排后 §引用 会集体失效，但没人会去逐个点
    """

    def _read(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_no_dangling_section_refs(self):
        """所有 §x.y 引用都必须能在 references/ 的某个文档里找到对应标题。

        §1.6 这类引用可能指向 protocols-and-clients.md 而不是 pitfalls.md，
        所以"存在性"按整个 references/ 目录算，再叠加同文件自引用。
        """
        have = set()
        refdir = os.path.join(ROOT, "references")
        for fn in os.listdir(refdir):
            if fn.endswith(".md"):
                have |= _md_headings(self._read("references", fn))
        bad = []
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for fn in files:
                if not (fn.endswith((".md", ".py", ".sh", ".example")) or fn == "deploy.env"):
                    continue
                path = os.path.join(base, fn)
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                local = _md_headings(text) if fn.endswith(".md") else set()
                for m in re.finditer(r"§(\d+(?:\.\d+)*)", text):
                    ref = m.group(1)
                    if ref in local or ref in have:
                        continue
                    bad.append("%s:%d §%s" % (os.path.relpath(path, ROOT),
                                              text[:m.start()].count("\n") + 1, ref))
        self.assertEqual(bad, [], "悬空的 § 引用：" + "; ".join(bad))

    def test_symptom_index_targets_exist(self):
        """症状速查表里每一行指向的章节都必须真实存在。"""
        text = self._read("references", "pitfalls.md")
        start = text.index("## 症状速查")
        end = text.index("## §1 ")
        table = text[start:end]
        have = _md_headings(text)
        refs = re.findall(r"§(\d+(?:\.\d+)*)", table)
        self.assertGreaterEqual(len(refs), 15, "症状表行数太少，是不是被删了")
        missing = [r for r in refs if r not in have]
        self.assertEqual(missing, [], "症状表指向了不存在的章节：%s" % missing)

    def test_compat_table_matches_code(self):
        """README 里的兼容性表必须和 render_report.CLIENT_SUPPORT 完全一致。"""
        text = self._read("README.md")
        lines = [l.strip() for l in text.splitlines() if l.strip().startswith("|")]
        header_i = next(i for i, l in enumerate(lines) if "AnyTLS" in l and "Hysteria2" in l)
        header = [c.strip() for c in lines[header_i].strip("|").split("|")]
        # 表头协议列 → 内部协议名
        label2proto = {v[0]: k for k, v in render_report.PROTO_META.items()}
        cols = [label2proto.get(h) for h in header[1:]]
        self.assertNotIn(None, cols, "README 表头有 PROTO_META 里没有的协议：%s" % header)

        code = dict(render_report.CLIENT_SUPPORT)
        rows = 0
        for line in lines[header_i + 2:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) != len(header):
                break
            name, vals = cells[0], cells[1:]
            match = [k for k in code if name.split("（")[0].split(" ≥")[0][:6] in k]
            self.assertTrue(match, "README 里的客户端 %r 在 CLIENT_SUPPORT 里找不到" % name)
            expect = code[match[0]]
            for proto, got in zip(cols, vals):
                self.assertEqual(got, expect.get(proto),
                                 "%s 的 %s：README 写 %s，代码是 %s"
                                 % (name, proto, got, expect.get(proto)))
            rows += 1
        self.assertGreaterEqual(rows, 5, "兼容性表行数异常")


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
