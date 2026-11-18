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
    """默认节点集只有 AnyTLS + Hysteria2。

    Reality / Shadowsocks / TUIC 都要在 deploy.env 里**显式填端口**才部署
    （理由见 pitfalls §6.12：丢包线路上三个 TCP 协议吞吐几乎同值）。
    node_specs 内部有进程内缓存，所以每个用例都构造不同的 cfg 键值。
    """

    def setUp(self):
        self.cfg = {"DOMAIN": "example.com", "NODE_PREFIX": "T1"}
        self.specs = deploy_nodes.node_specs(self.cfg)
        self.by_type = {s["type"]: s for s in self.specs}

    def _with(self, prefix, **kw):
        cfg = {"DOMAIN": "example.com", "NODE_PREFIX": prefix}
        cfg.update({k: str(v) for k, v in kw.items()})
        return deploy_nodes.node_specs(cfg)

    def test_default_two_nodes(self):
        self.assertEqual(len(self.specs), 2)
        self.assertEqual(set(self.by_type), {"anytls", "hysteria2"})

    def test_reality_off_by_default(self):
        self.assertNotIn("vless", self.by_type)

    def test_ss_off_by_default(self):
        self.assertNotIn("shadowsocks", self.by_type)

    def test_tuic_off_by_default(self):
        self.assertNotIn("tuic", self.by_type)

    def test_reality_on_when_port_given(self):
        specs = {s["type"]: s for s in self._with("T2", REALITY_PORT=2443)}
        self.assertEqual(specs["vless"]["port"], 2443)
        self.assertEqual(specs["vless"]["out_extra"].get("flow"), "xtls-rprx-vision")

    def test_ss_on_when_port_given(self):
        """SS 自己就是加密协议，不该挂 TLS 对象。"""
        specs = {s["type"]: s for s in self._with("T3", SS_PORT=2087)}
        self.assertIsNone(specs["shadowsocks"]["tls"])

    def test_tuic_uses_dedicated_tls(self):
        """TUIC 必须用带 alpn h3 的独立 TLS 对象，共用 LE 会报 no ALPN。"""
        specs = {s["type"]: s for s in self._with("T4", TUIC_PORT=8443)}
        self.assertEqual(specs["tuic"]["tls"], "tuic")

    def test_explicit_blank_means_off(self):
        """显式留空 = 关掉（现在键不存在也关，两种写法结果一致）。"""
        for kw in ({"REALITY_PORT": ""}, {"SS_PORT": ""}, {"TUIC_PORT": ""}):
            self.assertEqual(len(self._with("T5", **kw)), 2, kw)

    def test_no_443(self):
        """443 被针对性干扰，任何节点都不该用 443。"""
        for s in self._with("T6", REALITY_PORT=2053, SS_PORT=2087, TUIC_PORT=8443):
            self.assertNotEqual(s["port"], 443, "%s 不该用 443" % s["tag"])

    def test_ports_unique_per_transport(self):
        """同号端口只允许出现在不同传输层（如 8443/tcp 与 8443/udp）。"""
        specs = self._with("T7", REALITY_PORT=2053, SS_PORT=2087, TUIC_PORT=8443)
        tcp = [s["port"] for s in specs
               if s["type"] in ("anytls", "vless", "shadowsocks")]
        udp = [s["port"] for s in specs if s["type"] in ("tuic", "hysteria2")]
        self.assertEqual(len(tcp), len(set(tcp)))
        self.assertEqual(len(udp), len(set(udp)))

    def test_ports_overridable(self):
        specs = {s["type"]: s for s in self._with(
            "T8", ANYTLS_PORT=9443, REALITY_PORT=2443)}
        self.assertEqual(specs["anytls"]["port"], 9443)
        self.assertEqual(specs["vless"]["port"], 2443)

    def test_specs_cached_per_config(self):
        """同一配置两次调用必须拿到同一把 SS psk，否则入站与客户端凭据对不上。"""
        cfg = {"DOMAIN": "example.com", "NODE_PREFIX": "T9", "SS_PORT": "2087"}
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


# ---------------------------------------------------------------- 端口轮换

class TestPortRotation(unittest.TestCase):
    """端口会随时间被逐个封锁，体检与轮换是长期维护的一部分。"""

    def setUp(self):
        import check_ports
        import rotate_port
        self.cp = check_ports
        self.rp = rotate_port

    def test_dead_ports_parsed(self):
        self.assertEqual(self.rp.dead_ports({"DEAD_PORTS": "443,2053"}), {443, 2053})

    def test_dead_ports_tolerates_noise(self):
        cfg = {"DEAD_PORTS": " 443 , abc , 2053 ,, "}
        self.assertEqual(self.rp.dead_ports(cfg), {443, 2053})

    def test_dead_ports_empty(self):
        self.assertEqual(self.rp.dead_ports({}), set())

    def test_no_443_in_pools(self):
        """443 实测被针对性干扰，候选池里不该出现。"""
        self.assertNotIn(443, self.rp.TCP_POOL)
        self.assertNotIn(443, self.rp.UDP_POOL)

    def test_pools_are_standard_ports(self):
        """候选池应是标准 HTTPS/QUIC 端口，不要野鸡高位端口。"""
        standard = {80, 443, 2052, 2053, 2082, 2083, 2086, 2087,
                    2095, 2096, 8080, 8443, 8880, 9443}
        for port in self.rp.TCP_POOL + self.rp.UDP_POOL:
            self.assertIn(port, standard, "%d 不是标准端口" % port)

    def test_node_map_covers_all_deployable_nodes(self):
        """每个可能部署的节点都要能换端口。"""
        keys = {v[0] for v in self.rp.NODE_MAP.values()}
        for need in ("ANYTLS_PORT", "HY2_PORT", "REALITY_PORT", "SS_PORT"):
            self.assertIn(need, keys)

    def test_node_map_transport_matches_protocol(self):
        proto = {v[2]: v[1] for v in self.rp.NODE_MAP.values()}
        self.assertEqual(proto["anytls"], "tcp")
        self.assertEqual(proto["vless"], "tcp")
        self.assertEqual(proto["shadowsocks"], "tcp")
        self.assertEqual(proto["hysteria2"], "udp")
        self.assertEqual(proto["tuic"], "udp")

    def test_check_ports_specs_cover_all_nodes(self):
        keys = {k for k, _, _ in self.cp.PORT_SPECS}
        for need in ("ANYTLS_PORT", "REALITY_PORT", "SS_PORT"):
            self.assertIn(need, keys)

    def test_check_ports_marks_udp(self):
        """UDP 端口必须标 udp，否则会被 TCP 探测误判成 REFUSED。"""
        proto = {k: p for k, _, p in self.cp.PORT_SPECS}
        self.assertEqual(proto["TUIC_PORT"], "udp")
        self.assertEqual(proto["HY2_PORT"], "udp")
        self.assertEqual(proto["ANYTLS_PORT"], "tcp")
        self.assertEqual(proto["SS_PORT"], "tcp")

    # ------------------------------------------------------ 诊断矩阵
    # 「端口被封」和「防火墙没放行」从外部看都是 TIMEOUT，但处理方式完全不同：
    # 前者要换端口（动配置、重拉订阅），后者补一条 iptables 就好。
    # 这组测试锁住这个区分逻辑。

    def _row(self, proto="tcp", status="TIMEOUT", port=2087):
        return {"proto": proto, "status": status, "port": port}

    def _srv(self, listen=(), fw=(), allow_all=False, proto="tcp"):
        return {
            "tcp_listen": set(listen) if proto == "tcp" else set(),
            "udp_listen": set(listen) if proto == "udp" else set(),
            "allowed": set(fw),
            "allow_all": allow_all,
        }

    def test_diagnose_blocked(self):
        """在听 + 放行 + 外部超时 = 端口被封锁。"""
        v = self.cp.diagnose(self._row(status="TIMEOUT"),
                             self._srv(listen=[2087], fw=[("tcp", 2087)]))
        self.assertEqual(v, self.cp.BLOCKED)

    def test_diagnose_no_firewall(self):
        """在听 + 没放行 + 外部超时 = 防火墙问题，别换端口。"""
        v = self.cp.diagnose(self._row(status="TIMEOUT"),
                             self._srv(listen=[2087], fw=[]))
        self.assertEqual(v, self.cp.NO_FW)

    def test_diagnose_no_listen(self):
        """服务端根本没在听。"""
        v = self.cp.diagnose(self._row(status="TIMEOUT"),
                             self._srv(listen=[], fw=[("tcp", 2087)]))
        self.assertEqual(v, self.cp.NO_LISTEN)

    def test_diagnose_ok(self):
        v = self.cp.diagnose(self._row(status="OPEN"),
                             self._srv(listen=[2087], fw=[("tcp", 2087)]))
        self.assertEqual(v, self.cp.OK)

    def test_diagnose_udp_needs_client(self):
        """UDP 在听且放行时，只能说「需客户端验证」，不能断言正常。"""
        v = self.cp.diagnose(self._row(proto="udp", status="—", port=2083),
                             self._srv(listen=[2083], fw=[("udp", 2083)], proto="udp"))
        self.assertEqual(v, self.cp.UDP_ONLY)

    def test_diagnose_udp_no_firewall(self):
        v = self.cp.diagnose(self._row(proto="udp", status="—", port=2083),
                             self._srv(listen=[2083], fw=[], proto="udp"))
        self.assertEqual(v, self.cp.NO_FW)

    def test_diagnose_without_server_falls_back(self):
        """拿不到服务端状态时，只能区分通/不通。"""
        self.assertEqual(self.cp.diagnose(self._row(status="OPEN"), None), self.cp.OK)
        self.assertEqual(self.cp.diagnose(self._row(status="TIMEOUT"), None), self.cp.BLOCKED)

    # ------------------------------------------------------ 规则解析

    def test_parse_firewall_ignores_conditional(self):
        """带 -i / --ctstate 的规则不是无条件的，不该误报 ACCEPT all。"""
        text = "\n".join([
            "-A INPUT -i lo -j ACCEPT",
            "-A INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
            "-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT",
            "-A INPUT -m conntrack --ctstate INVALID -j DROP",
        ])
        allowed, allow_all = self.cp.parse_firewall(text)
        self.assertFalse(allow_all, "有条件的规则被误判成 ACCEPT all")
        self.assertIn(("tcp", 22), allowed)

    def test_parse_firewall_detects_bare_accept(self):
        """光板 `-A INPUT -j ACCEPT` 会架空 policy DROP，必须识别出来。"""
        text = "\n".join([
            "-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT",
            "-A INPUT -j ACCEPT",
        ])
        _, allow_all = self.cp.parse_firewall(text)
        self.assertTrue(allow_all)

    def test_parse_firewall_collects_both_protos(self):
        text = "\n".join([
            "-A INPUT -p tcp -m tcp --dport 8443 -j ACCEPT",
            "-A INPUT -p udp -m udp --dport 2083 -j ACCEPT",
        ])
        allowed, _ = self.cp.parse_firewall(text)
        self.assertIn(("tcp", 8443), allowed)
        self.assertIn(("udp", 2083), allowed)

    def test_parse_listen(self):
        text = "\n".join([
            "LISTEN 0 4096 *:8443 *:*",
            "LISTEN 0 4096 [::]:2053 [::]:*",
            "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*",
        ])
        self.assertEqual(self.cp.parse_listen(text), {8443, 2053, 22})


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

    def test_all_config_keys_declared(self):
        """Python 脚本读的配置键必须在 load_env 白名单里。

        不在白名单的键，deploy.env 能读到（load_env 会读所有行），
        但用环境变量覆盖时会静默失效 —— 这类问题很难在部署时发现。
        只扫 .py：shell 里的 ${VAR} 大多是脚本局部变量，扫进来全是误报。
        """
        import inspect

        src = inspect.getsource(ssh_run.load_env)
        declared = set(re.findall(r'"([A-Z][A-Z0-9_]{2,})"', src))

        missing = {}
        scripts_dir = os.path.join(ROOT, "scripts")
        for fn in sorted(os.listdir(scripts_dir)):
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(scripts_dir, fn), encoding="utf-8") as fh:
                text = fh.read()
            keys = set(re.findall(r'cfg\.get\("([A-Z][A-Z0-9_]+)"', text))
            keys |= set(re.findall(r'cfg\["([A-Z][A-Z0-9_]+)"\]', text))
            bad = keys - declared
            if bad:
                missing[fn] = sorted(bad)

        self.assertEqual(missing, {},
                         "这些键没进 load_env 白名单，环境变量覆盖会失效：%s" % missing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
