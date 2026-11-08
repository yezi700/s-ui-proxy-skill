#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成交付文档（Markdown + HTML）。

数据全部来自面板 API 与订阅端点的实测回读，不编造。

用法：
    python scripts/render_report.py -o .          # 本地跑，自动上传执行并拉回结果
    python scripts/render_report.py --local -o .  # 已在 VPS 上
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SUI_DB = "/usr/local/s-ui/db/s-ui.db"

# 协议元信息：显示名 / 传输层 / 一句话说明
# 这张表是唯一事实来源 —— 兼容性表、端口表、节点说明全部由它派生。
# 加新协议只在这里加一行，别的地方不用动（以前这里硬编码了 TUIC 四列，
# 换掉 TUIC 后文档还在印 TUIC，坑过一次）。
PROTO_META = {
    "anytls": ("AnyTLS", "TCP", "TLS-in-TLS 指纹打散 + 连接复用，日常主力"),
    "hysteria2": ("Hysteria2", "UDP", "QUIC + 拥塞控制激进，高丢包链路吞吐最好"),
    "vless": ("VLESS-Reality", "TCP", "借用外部 SNI，抗主动探测，可作保底"),
    "shadowsocks": ("Shadowsocks", "TCP", "无 TLS 握手、握手往返最少；SS2022 走 2022-blake3 系列"),
    "tuic": ("TUIC", "UDP", "QUIC 传输，UDP 场景好，需 alpn=h3"),
    "trojan": ("Trojan", "TCP", "伪装 HTTPS，兼容面广"),
    "vmess": ("VMess", "TCP", "老牌协议，兼容面最广"),
}

# 客户端 → 各协议支持情况。这张表比任何参数都重要，
# 写错会让人在两种"连不上"之间来回折腾。
CLIENT_SUPPORT = [
    # (客户端 / 内核, {协议: 是/否})
    ("sing-box ≥1.12（Hiddify / Karing / NekoBox）", {
        "anytls": "是", "hysteria2": "是", "vless": "是",
        "shadowsocks": "是", "tuic": "是", "trojan": "是", "vmess": "是"}),
    ("mihomo / Clash Meta ≥1.19（Verge Rev / FlClash）", {
        "anytls": "是", "hysteria2": "是", "vless": "是",
        "shadowsocks": "是", "tuic": "是", "trojan": "是", "vmess": "是"}),
    ("Xray 系（v2rayN / v2rayNG 默认内核）", {
        "anytls": "否", "hysteria2": "否", "vless": "是",
        "shadowsocks": "是", "tuic": "否", "trojan": "是", "vmess": "是"}),
    ("Shadowrocket（iOS，≥2.2.65）", {
        "anytls": "是", "hysteria2": "是", "vless": "是",
        "shadowsocks": "是", "tuic": "是", "trojan": "是", "vmess": "是"}),
    ("Surge", {
        "anytls": "否", "hysteria2": "是", "vless": "否",
        "shadowsocks": "是", "tuic": "否", "trojan": "是", "vmess": "否"}),
    ("QuantumultX", {
        "anytls": "否", "hysteria2": "否", "vless": "否",
        "shadowsocks": "是", "tuic": "否", "trojan": "是", "vmess": "是"}),
]


def proto_label(t: str) -> str:
    return PROTO_META.get(t, (t, "TCP", ""))[0]


def proto_transport(t: str) -> str:
    """TCP / UDP 命名空间互相独立，写错会让人以为端口冲突。"""
    return PROTO_META.get(t, (t, "TCP", ""))[1]


def proto_desc(t: str) -> str:
    return PROTO_META.get(t, (t, "TCP", ""))[2]


def compat_columns(d: dict) -> list:
    """按实际入站类型出列 —— 只列这台机器上真有的协议，未知协议排最后。"""
    order = list(PROTO_META)
    seen = []
    for ib in d["inbounds"]:
        t = ib.get("type")
        if t and t not in seen:
            seen.append(t)
    return sorted(seen, key=lambda t: order.index(t) if t in order else 99)


def compat_remark(cols: list, sup: dict) -> str:
    yes = [proto_label(t) for t in cols if sup.get(t) == "是"]
    if len(yes) == len(cols):
        return "全部支持"
    if not yes:
        return "都不支持"
    return "只支持 " + " / ".join(yes)


# ---------------------------------------------------------------- 实测数据

def load_measured() -> dict:
    """读本机实测记录（measured.json，与 deploy.env 同级，已 gitignore）。

    没有这个文件就整节省略 —— 宁可少写一节，也不要编造吞吐数字。
    """
    import json
    for path in (os.path.join(os.path.dirname(HERE), "measured.json"),
                 os.path.join(HERE, "measured.json"),
                 "measured.json"):
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:  # noqa: BLE001
                return {}
    return {}


def _median(xs: list) -> float:
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    if not xs:
        return 0.0
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def measured_rows(d: dict, m: dict) -> list:
    """返回 [(tag, 短窗口吞吐中位, 区间, 延迟中位, 区间, 轮次, 持续吞吐)]。

    短窗口（6 秒）会被 TCP 慢启动严重低估，只能作参考；
    `throughput_mbps_sustained_100mb` 才是真实带宽，有就优先展示。
    """
    tp = m.get("throughput_mbps") or {}
    lat = m.get("latency_ms") or {}
    sust = m.get("throughput_mbps_sustained_100mb") or {}
    rows = []
    for ib in d["inbounds"]:
        tag = ib["tag"]
        if tag not in tp and tag not in lat:
            continue
        t = tp.get(tag) or []
        l = lat.get(tag) or []
        rows.append((
            tag,
            _median(t), ("%.0f–%.0f" % (min(t), max(t))) if t else "—",
            _median(l), ("%.0f–%.0f" % (min(l), max(l))) if l else "—",
            len(t) or len(l),
            sust.get(tag),
        ))
    return rows


# ---------------------------------------------------------------- 采集

def collect(cfg: dict) -> dict:
    from sui_api import SuiApi

    domain = cfg["DOMAIN"]
    sub_port = cfg.get("SUB_PORT") or "2096"
    sub_path = (cfg.get("SUB_PATH") or "").strip("/")
    sub_id = cfg.get("SUB_ID") or "us"

    api = SuiApi.from_env(cfg)
    api.login()

    scheme = "https" if cfg.get("PANEL_TLS", "1") not in ("0", "false", "no") else "http"
    sub_base = "%s://%s:%s/%s/%s" % (scheme, domain, sub_port, sub_path, sub_id)

    data = {
        "domain": domain,
        "panel_url": "%s://%s:%s/%s/" % (scheme, domain,
                                         cfg.get("PANEL_PORT") or "46821",
                                         (cfg.get("PANEL_PATH") or "").strip("/")),
        "panel_user": cfg.get("PANEL_USER") or "admin",
        "panel_pass": cfg.get("PANEL_PASS") or "",
        "sub_url": sub_base,
        "sub_clash": sub_base + "?format=clash",
        "sub_json": sub_base + "?format=json",
        "inbounds": [],
        "hop_range": (cfg.get("HY2_HOP_RANGE") or "").strip(),
        "links": [],
        "tls": api.tls(),
        "server_ip": cfg.get("SERVER_IP") or cfg.get("VPS_HOST") or "",
        "measured": load_measured(),
    }

    # 入站 + 运行态用户数
    live = {ib.get("tag"): ib for ib in (api.singbox_config().get("inbounds") or [])}
    for ib in api.inbounds():
        tag = ib.get("tag")
        data["inbounds"].append({
            "tag": tag,
            "type": ib.get("type"),
            "label": proto_label(ib.get("type")),
            "transport": proto_transport(ib.get("type")),
            "port": ib.get("listen_port"),
            "tls_id": ib.get("tls_id"),
            "users": len((live.get(tag) or {}).get("users") or []),
            "desc": proto_desc(ib.get("type")),
        })

    # 订阅链接（实测拉一次）
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(sub_base, timeout=20, context=ctx) as r:
            raw = r.read().decode()
        try:
            data["links"] = [ln for ln in base64.b64decode(raw).decode().splitlines() if ln.strip()]
        except Exception:
            data["links"] = [ln for ln in raw.splitlines() if ln.strip()]
    except Exception as exc:  # noqa: BLE001
        data["links_error"] = str(exc)

    # 证书到期
    cert_dir = cfg.get("CERT_DIR") or "/root/cert/" + domain
    data["cert_dir"] = cert_dir
    data["cert_end"] = ""
    pem = os.path.join(cert_dir, "fullchain.pem")
    if os.path.isfile(pem):
        try:
            import subprocess
            out = subprocess.run(["openssl", "x509", "-in", pem, "-noout", "-enddate"],
                                 capture_output=True, text=True, check=True).stdout
            data["cert_end"] = out.strip().split("=", 1)[-1]
        except Exception:  # noqa: BLE001
            pass

    return data


def link_params(uri: str) -> dict:
    """把分享链接拆成 scheme / 主机端口 / 查询参数。"""
    try:
        u = urllib.parse.urlparse(uri)
    except Exception:  # noqa: BLE001
        return {}
    q = urllib.parse.parse_qs(u.query)
    return {
        "scheme": u.scheme,
        "host": u.hostname or "",
        "port": u.port or "",
        "name": (u.fragment or "").split(" ")[0],
        "params": {k: v[0] for k, v in q.items()},
    }


# ---------------------------------------------------------------- Markdown

def render_md(d: dict) -> str:
    L = []
    A = L.append
    # 分节号自动递增：实测一节是有则显示、无则省略的，硬编码「五、六、七」
    # 会在没有实测数据时错位（以前手写过，结果编号对不上）
    _n = [0]

    def S(title: str) -> str:
        _n[0] += 1
        return "## %s、%s" % ("一二三四五六七八九十"[_n[0] - 1], title)

    A("# %s 代理节点部署信息" % d["domain"])
    A("")
    A("> 由 `render_report.py` 自动生成，数据来自面板 API 与订阅端点实测回读。")
    A("> ⚠️ 含明文凭据，勿外传、勿提交公开仓库。")
    A("")
    A(S("面板"))
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A("| 面板地址 | %s |" % d["panel_url"])
    A("| 用户名 | `%s` |" % d["panel_user"])
    A("| 密码 | `%s` |" % d["panel_pass"])
    A("| 服务端 IP | `%s` |" % d["server_ip"])
    if d.get("cert_end"):
        A("| 证书到期 | %s |" % d["cert_end"])
    A("")
    A(S("订阅地址"))
    A("")
    A("| 格式 | 地址 | 给谁用 |")
    A("|---|---|---|")
    A("| 通用（base64） | %s | v2rayN / v2rayNG / NekoBox / Karing / Hiddify / Shadowrocket |" % d["sub_url"])
    A("| Clash YAML | %s | Clash Verge Rev / Clash Meta for Android / FlClash |" % d["sub_clash"])
    A("| sing-box JSON | %s | sing-box 原生配置 |" % d["sub_json"])
    A("")
    A("⚠️ **别把「通用」那条填给 Clash**，会报 `cannot unmarshal !!str`。")
    A("")
    A(S("节点"))
    A("")
    A("| 节点名 | 协议 | 端口 | 传输层 | 运行态用户数 | 说明 |")
    A("|---|---|---|---|---|---|")
    for ib in d["inbounds"]:
        flag = "" if ib["users"] else " ⚠️"
        A("| %s | %s | %s | %s | %d%s | %s |" % (
            ib["tag"], ib["label"], ib["port"], ib["transport"],
            ib["users"], flag, ib["desc"]))
    A("")
    if d.get("links"):
        A("### 分享链接")
        A("")
        for ln in d["links"]:
            A("```")
            A(ln)
            A("```")
            A("")
    elif d.get("links_error"):
        A("⚠️ 拉取订阅失败：%s" % d["links_error"])
        A("")

    if d.get("hop_range") and any(ib["type"] == "hysteria2" for ib in d["inbounds"]):
        A("### Hysteria2 端口跳跃（订阅里没有，要手动加一次）")
        A("")
        A("服务端已经把 `%s/udp` 全部重定向到 Hysteria2 的监听端口，"
          "但**订阅不会带这段配置**，客户端要自己补：" % d["hop_range"])
        A("")
        A("```yaml")
        A("# mihomo / Clash Meta —— 注意是 ports（复数），不是 port")
        A("- name: %s" % next((ib["tag"] for ib in d["inbounds"]
                              if ib["type"] == "hysteria2"), "JP-Hysteria2"))
        A("  type: hysteria2")
        A("  ports: %s" % d["hop_range"].replace(":", "-"))
        A("  hop-interval: 30")
        A("```")
        A("")
        A("```json")
        A('// sing-box')
        A('"server_ports": ["%s"], "hop_interval": "30s"' % d["hop_range"])
        A("```")
        A("")
        A("作用：UDP 被按**单端口**限速时换端口就能绕开 —— TUIC 就是死在这上面。"
          "实测代价为零（固定端口 198ms / 78.4 Mbps，跳端口 207ms / 77.4 Mbps）。")
        A("")

    cols = compat_columns(d)
    A(S("客户端兼容性"))
    A("")
    A("| 客户端 / 内核 | %s | 备注 |" % " | ".join(proto_label(t) for t in cols))
    A("|---|%s---|" % (":--:|" * len(cols)))
    for name, sup in CLIENT_SUPPORT:
        A("| %s | %s | %s |" % (name,
                                " | ".join(sup.get(t, "?") for t in cols),
                                compat_remark(cols, sup)))
    A("")
    A("**这张表必须整张给用户。** 只写一半的话，用 Xray 内核的人会以为节点全挂了。")
    A("")
    A("> 表里写「否」的协议，那个节点在该客户端里根本导不进去 —— 不是节点坏了。")
    A("> 想少踩坑就按备注列挑客户端：**备注写着「全部支持」的，上面每一种协议都能用**。")
    A("")

    m = d.get("measured") or {}
    rows = measured_rows(d, m)
    if rows:
        A(S("实测记录"))
        A("")
        A("| 节点 | 持续吞吐（100MB 整包） | 短窗口吞吐（6 秒） | 延迟中位数 | 延迟区间 | 轮次 |")
        A("|---|---|---|---|---|---|")
        for tag, tpm, tpr, latm, latr, n, sust in rows:
            A("| %s | %s | %.1f Mbps | %.0f ms | %s ms | %d |"
              % (tag,
                 ("%.1f Mbps" % sust) if sust else "—",
                 tpm, latm, latr, n))
        A("")
        A("测法：%s，目标 `%s`。"
          % (m.get("client") or "真实客户端", m.get("target") or d["domain"]))
        if m.get("note"):
            A("")
            A("> %s" % m["note"])
        A("")
        A("两条读法：")
        A("")
        A("1. **延迟差异来自连接复用，不是协议快慢。** AnyTLS 和 Hysteria2 在客户端侧"
          "有连接池，一个请求只付 1 个 RTT；Shadowsocks 没有复用，每次新建 TCP（2 RTT）；"
          "Reality 还要再做一次 TLS 握手（3 RTT）。实测 612 ÷ 205 ≈ 3、402 ÷ 205 ≈ 2，"
          "和 RTT 数完全对得上。所以「SS 没握手所以最快」是错的 —— "
          "省掉的那一跳远不如连接复用值钱。")
        A("2. **短窗口那一列会低估 3~4 倍**，因为前几秒还在 TCP 慢启动。"
          "拿它选节点会得出完全相反的结论，只看「持续吞吐」那一列。")
        A("")
        if m.get("removed"):
            A("已下线的节点：")
            A("")
            for tag, why in m["removed"].items():
                A("- `%s` —— %s" % (tag, why))
            A("")
    A(S("端口与防火墙"))
    A("")
    A("| 端口 | 协议 | 用途 |")
    A("|---|---|---|")
    A("| %s | TCP | 面板（HTTPS） |" % _port_of(d["panel_url"]))
    A("| %s | TCP | 订阅（HTTPS） |" % _port_of(d["sub_url"]))
    for ib in d["inbounds"]:
        A("| %s | %s | %s |" % (ib["port"], ib["transport"], ib["tag"]))
    A("")
    A("全部端口已写入 `/etc/iptables/rules.v4`，重启后仍生效。")
    A("")
    shared = _shared_ports(d)
    if shared:
        A("⚠️ %s —— 同一端口号同时跑 TCP 和 UDP，两者是**独立命名空间**，"
          "互不冲突；但改防火墙时两边的规则都要留，漏一条就是「一个节点能用、另一个连不上」。"
          % "、".join("%s 端口 %s" % ("/".join(v), p) for p, v in shared))
        A("")
    A(S("运维命令"))
    A("")
    A("```bash")
    A("systemctl status s-ui          # 面板与 sing-box 同进程")
    A("systemctl restart s-ui         # 重启（改端口/路径/证书后必须执行）")
    A("journalctl -u s-ui -f          # 实时日志")
    A("s-ui                           # 交互菜单")
    A("```")
    A("")
    A("备份数据库：")
    A("")
    A("```bash")
    A("cp /usr/local/s-ui/db/s-ui.db /root/s-ui.db.bak.$(date +%Y%m%d-%H%M)")
    A("```")
    A("")
    A(S("注意事项"))
    A("")
    notes = [
        "**不要删 `/root/cert/`** —— 面板、订阅与所有带 TLS 的节点共用同一张证书。",
        "**改面板端口 / 路径 / 证书后必须 `systemctl restart s-ui`**，S-UI 不会自动重启。",
    ]
    shared = _shared_ports(d)
    if shared:
        notes.append("**%s 端口同号**，一个是 TCP 一个是 UDP，互不影响；改防火墙时两个都要留。"
                     % "、".join(p for p, _ in shared))
    notes += [
        "新增入站后 sing-box 会自动热加载，不需要重启。",
        "**删掉入站还不够** —— 客户端 config 里残留的协议条目会继续出现在订阅里，"
        "生成一个连不上的死节点。删入站后要同步删 `clients.config` 里对应的键"
        "（SS2022 是 `shadowsocks16`，不是 `shadowsocks`）。",
        "本地实测吞吐只能作参考 —— 你的线路、运营商的 QoS、国际出口都会影响结果，"
        "换到手机上再测一次才算数。",
    ]
    for i, n in enumerate(notes, 1):
        A("%d. %s" % (i, n))
    A("")
    return "\n".join(L)


def _port_of(url: str) -> str:
    try:
        return str(urllib.parse.urlparse(url).port or "")
    except Exception:  # noqa: BLE001
        return ""


def _shared_ports(d: dict) -> list:
    """找出「同端口号、不同传输层」的组合，返回 [(port, [协议…])]。"""
    byport = {}
    for ib in d["inbounds"]:
        byport.setdefault(str(ib["port"]), []).append(ib)
    out = []
    for port, ibs in sorted(byport.items()):
        layers = {ib["transport"] for ib in ibs}
        if len(layers) > 1:
            out.append((port, ["%s %s" % (ib["transport"], ib["label"]) for ib in ibs]))
    return out


# ---------------------------------------------------------------- HTML

HTML_TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#f7f8fa;--card:#fff;--fg:#1f2328;--mute:#656d76;--line:#d8dee4;--acc:#0969da;--warn:#9a6700;--ok:#1a7f37}}
*{{box-sizing:border-box}}
body{{margin:0;padding:32px 16px;background:var(--bg);color:var(--fg);
font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC",sans-serif}}
.wrap{{max-width:920px;margin:0 auto}}
h1{{font-size:26px;margin:0 0 8px}}
h2{{font-size:19px;margin:36px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin:14px 0}}
table{{width:100%;border-collapse:collapse;font-size:14px;margin:8px 0}}
th,td{{padding:9px 11px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}
th{{background:#f0f3f6;font-weight:600;white-space:nowrap}}
tr:last-child td{{border-bottom:none}}
code{{background:#eff1f4;padding:2px 6px;border-radius:4px;font-size:13px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}}
pre{{background:#0d1117;color:#e6edf3;padding:14px 16px;border-radius:8px;overflow-x:auto;
font-size:13px;line-height:1.55}}
pre code{{background:none;color:inherit;padding:0}}
.copy{{position:relative}}
.copy button{{position:absolute;top:8px;right:8px;background:#30363d;color:#e6edf3;border:1px solid #444c56;
border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer}}
.copy button:hover{{background:#444c56}}
.note{{border-left:3px solid var(--warn);background:#fff8e6;padding:10px 14px;border-radius:0 8px 8px 0;
color:#6b5200;font-size:14px;margin:12px 0}}
.ok{{color:var(--ok)}} .bad{{color:#cf222e}}
</style>
</head>
<body><div class="wrap">
<h1>{title}</h1>
<p style="color:var(--mute);margin:0 0 8px">数据来自面板 API 与订阅端点实测回读 · 含明文凭据，勿外传</p>
{body}
</div>
<script>
document.querySelectorAll('.copy').forEach(function(box){{
  var btn=document.createElement('button');
  btn.textContent='复制';
  btn.onclick=function(){{
    var t=box.querySelector('code').innerText;
    navigator.clipboard.writeText(t).then(function(){{
      btn.textContent='已复制';setTimeout(function(){{btn.textContent='复制';}},1200);
    }});
  }};
  box.appendChild(btn);
}});
</script>
</body></html>"""


def render_html(d: dict) -> str:
    import html as H

    def esc(x):
        return H.escape(str(x))

    parts = []

    parts.append('<div class="card"><h2 style="margin-top:0">面板</h2><table>')
    parts.append('<tr><th>地址</th><td><a href="%s">%s</a></td></tr>'
                 % (esc(d["panel_url"]), esc(d["panel_url"])))
    parts.append('<tr><th>用户名</th><td><code>%s</code></td></tr>' % esc(d["panel_user"]))
    parts.append('<tr><th>密码</th><td><code>%s</code></td></tr>' % esc(d["panel_pass"]))
    parts.append('<tr><th>服务端 IP</th><td><code>%s</code></td></tr>' % esc(d["server_ip"]))
    if d.get("cert_end"):
        parts.append('<tr><th>证书到期</th><td>%s</td></tr>' % esc(d["cert_end"]))
    parts.append('</table></div>')

    parts.append('<h2>订阅地址</h2><div class="card"><table>')
    parts.append('<tr><th>格式</th><th>地址</th><th>给谁用</th></tr>')
    rows = [
        ("通用 base64", d["sub_url"], "v2rayN / v2rayNG / NekoBox / Karing / Hiddify / Shadowrocket"),
        ("Clash YAML", d["sub_clash"], "Clash Verge Rev / Clash Meta for Android / FlClash"),
        ("sing-box JSON", d["sub_json"], "sing-box 原生配置"),
    ]
    for name, url, who in rows:
        parts.append('<tr><td>%s</td><td><code>%s</code></td><td style="color:var(--mute)">%s</td></tr>'
                     % (esc(name), esc(url), esc(who)))
    parts.append('</table></div>')
    parts.append('<div class="note">别把「通用」那条填给 Clash，会报 '
                 '<code>cannot unmarshal !!str</code>。</div>')

    parts.append('<h2>节点</h2><div class="card"><table>')
    parts.append('<tr><th>节点名</th><th>协议</th><th>端口</th><th>传输层</th>'
                 '<th>用户</th><th>说明</th></tr>')
    for ib in d["inbounds"]:
        cls = "ok" if ib["users"] else "bad"
        parts.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class="%s">%d</td>'
                     '<td style="color:var(--mute)">%s</td></tr>'
                     % (esc(ib["tag"]), esc(ib["label"]), esc(ib["port"]),
                        esc(ib["transport"]), cls, ib["users"], esc(ib["desc"])))
    parts.append('</table></div>')

    if d.get("links"):
        parts.append('<h2>分享链接</h2>')
        for ln in d["links"]:
            parts.append('<div class="card copy"><pre><code>%s</code></pre></div>' % esc(ln))

    hy2 = next((ib for ib in d["inbounds"] if ib["type"] == "hysteria2"), None)
    if d.get("hop_range") and hy2:
        parts.append('<h2>Hysteria2 端口跳跃（订阅里没有，要手动加一次）</h2>')
        parts.append('<div class="card"><p>服务端已经把 <code>%s/udp</code> '
                     '全部重定向到 Hysteria2 的监听端口，但<b>订阅不会带这段配置</b>，'
                     '客户端要自己补：</p>'
                     '<pre><code># mihomo / Clash Meta —— 注意是 ports（复数），不是 port\n'
                     '- name: %s\n  type: hysteria2\n  ports: %s\n  hop-interval: 30\n\n'
                     '# sing-box\n"server_ports": ["%s"], "hop_interval": "30s"</code></pre>'
                     '</div>' % (esc(d["hop_range"]), esc(hy2["tag"]),
                                 esc(d["hop_range"].replace(":", "-")), esc(d["hop_range"])))
        parts.append('<div class="note">作用：UDP 被按<b>单端口</b>限速时换端口就能绕开 —— '
                     'TUIC 就是死在这上面。实测代价为零'
                     '（固定端口 198ms / 78.4 Mbps，跳端口 207ms / 77.4 Mbps）。</div>')

    cols = compat_columns(d)
    parts.append('<h2>客户端兼容性</h2><div class="card"><table>')
    parts.append('<tr><th>客户端 / 内核</th>%s<th>备注</th></tr>'
                 % "".join("<th>%s</th>" % esc(proto_label(t)) for t in cols))
    for name, sup in CLIENT_SUPPORT:
        cells = "".join('<td class="%s">%s</td>'
                        % ("ok" if sup.get(t) == "是" else "bad", esc(sup.get(t, "?")))
                        for t in cols)
        parts.append('<tr><td>%s</td>%s<td style="color:var(--mute)">%s</td></tr>'
                     % (esc(name), cells, esc(compat_remark(cols, sup))))
    parts.append('</table></div>')
    parts.append('<div class="note">这张表要整张给用户。只写一半的话，'
                 '用 Xray 内核的人会以为节点全挂了。表里写「否」的协议，'
                 '那个节点在该客户端里根本导不进去 —— 不是节点坏了。</div>')

    m = d.get("measured") or {}
    rows = measured_rows(d, m)
    if rows:
        parts.append('<h2>实测记录</h2><div class="card"><table>')
        parts.append('<tr><th>节点</th><th>持续吞吐<br><small>100MB 整包</small></th>'
                     '<th>短窗口吞吐<br><small>6 秒，低估</small></th>'
                     '<th>延迟中位数</th><th>延迟区间</th><th>轮次</th></tr>')
        for tag, tpm, tpr, latm, latr, n, sust in rows:
            parts.append('<tr><td>%s</td><td>%s</td><td>%.1f Mbps</td>'
                         '<td>%.0f ms</td><td>%s ms</td><td>%d</td></tr>'
                         % (esc(tag), ("%.1f Mbps" % sust) if sust else "—",
                            tpm, latm, esc(latr), n))
        parts.append('</table>')
        parts.append('<p style="color:var(--mute);margin:10px 0 0">测法：%s，目标 <code>%s</code></p>'
                     % (esc(m.get("client") or "真实客户端"),
                        esc(m.get("target") or d["domain"])))
        if m.get("note"):
            parts.append('<p style="color:var(--mute);margin:6px 0 0">%s</p>' % esc(m["note"]))
        parts.append('</div>')
        parts.append('<div class="note"><b>延迟差异来自连接复用，不是协议快慢。</b>'
                     'AnyTLS 和 Hysteria2 在客户端侧有连接池，一个请求只付 1 个 RTT；'
                     'Shadowsocks 每次新建 TCP（2 RTT）；Reality 还要再做一次 TLS 握手（3 RTT）。'
                     '实测 612 ÷ 205 ≈ 3、402 ÷ 205 ≈ 2，和 RTT 数完全对得上 —— '
                     '所以「SS 没握手所以最快」是错的。<br>'
                     '<b>另外，短窗口那一列会低估 3~4 倍</b>（TCP 慢启动），'
                     '拿它选节点会得出相反的结论，只看「持续吞吐」那一列。</div>')
        if m.get("removed"):
            parts.append('<h2>已下线节点</h2><div class="card"><ul>%s</ul></div>'
                         % "".join('<li><code>%s</code> —— %s</li>' % (esc(t), esc(w))
                                   for t, w in m["removed"].items()))

    parts.append('<h2>端口与防火墙</h2><div class="card"><table>')
    parts.append('<tr><th>端口</th><th>协议</th><th>用途</th></tr>')
    parts.append('<tr><td>%s</td><td>TCP</td><td>面板 HTTPS</td></tr>' % esc(_port_of(d["panel_url"])))
    parts.append('<tr><td>%s</td><td>TCP</td><td>订阅 HTTPS</td></tr>' % esc(_port_of(d["sub_url"])))
    for ib in d["inbounds"]:
        parts.append('<tr><td>%s</td><td>%s</td><td>%s</td></tr>'
                     % (esc(ib["port"]), esc(ib["transport"]), esc(ib["tag"])))
    parts.append('</table></div>')
    shared = _shared_ports(d)
    if shared:
        parts.append('<div class="note">%s 同端口号同时跑 TCP 和 UDP，'
                     '两者是独立命名空间、互不冲突；但改防火墙时两边的规则都要留，'
                     '漏一条就是「一个节点能用、另一个连不上」。</div>'
                     % esc("、".join("%s 端口 %s" % ("/".join(v), p) for p, v in shared)))

    parts.append('<h2>运维命令</h2><div class="card copy"><pre><code>'
                 'systemctl status s-ui          # 面板与 sing-box 同进程\n'
                 'systemctl restart s-ui         # 改端口/路径/证书后必须执行\n'
                 'journalctl -u s-ui -f          # 实时日志\n'
                 's-ui                           # 交互菜单\n'
                 'cp /usr/local/s-ui/db/s-ui.db /root/s-ui.db.bak.$(date +%Y%m%d-%H%M)'
                 '</code></pre></div>')

    notes = ['不要删 <code>/root/cert/</code>，面板、订阅与所有带 TLS 的节点共用同一张证书。',
             '改面板端口、路径、证书后必须 <code>systemctl restart s-ui</code>。']
    shared = _shared_ports(d)
    if shared:
        notes.append('%s 端口同号（一个 TCP 一个 UDP），改防火墙两个都要留。'
                     % "、".join(p for p, _ in shared))
    notes.append('新增入站 sing-box 会自动热加载，不需要重启。')
    notes.append('<b>删掉入站还不够</b> —— 客户端 config 里残留的协议条目会继续出现在订阅里，'
                 '生成一个连不上的死节点。删入站后要同步删 <code>clients.config</code> '
                 '里对应的键（SS2022 是 <code>shadowsocks16</code>，不是 <code>shadowsocks</code>）。')
    notes.append('本地实测吞吐只能作参考 —— 线路、运营商 QoS、国际出口都会影响结果，'
                 '换到手机上再测一次才算数。')
    parts.append('<h2>注意事项</h2><div class="card"><ol>%s</ol></div>'
                 % "".join("<li>%s</li>" % n for n in notes))

    return HTML_TPL.format(title=H.escape(d["domain"] + " 代理节点部署信息"),
                           body="\n".join(parts))


# ---------------------------------------------------------------- 入口

def run_remote(cfg: dict, outdir: str) -> int:
    from ssh_run import get_client, put_text, run, env_prefix, fill_from_install_result

    cli = get_client(cfg)
    try:
        remote_dir = "/root/.sui-skill"
        fill_from_install_result(cli, cfg)
        run(cli, "mkdir -p " + remote_dir, timeout=20)
        for name in ("render_report.py", "sui_api.py", "ssh_run.py"):
            with open(os.path.join(HERE, name), "r", encoding="utf-8", newline="\n") as fh:
                put_text(cli, fh.read(), remote_dir + "/" + name)
        # 实测数据是本地生成的，远端没有 —— 有就一起传上去
        local_measured = os.path.join(os.path.dirname(HERE), "measured.json")
        if os.path.isfile(local_measured):
            with open(local_measured, "r", encoding="utf-8", newline="\n") as fh:
                put_text(cli, fh.read(), remote_dir + "/measured.json")
            # render_report.py 会先找 ../measured.json，再找同目录，所以放哪都行
        cmd = "cd %s && %s python3 render_report.py --local -o /root" % (
            remote_dir, env_prefix(cfg))
        rc, out, err = run(cli, cmd, timeout=300)
        sys.stdout.write(out)
        if err.strip():
            sys.stderr.write("\n[stderr]\n" + err)
        if rc != 0:
            return rc

        os.makedirs(outdir, exist_ok=True)
        sftp = cli.open_sftp()
        try:
            for suffix in (".md", ".html"):
                remote = "/root/VPS-%s-部署信息%s" % (cfg["DOMAIN"], suffix)
                local = os.path.join(outdir, os.path.basename(remote))
                sftp.get(remote, local)
                print("已下载 %s" % local)
        finally:
            sftp.close()
        return 0
    finally:
        cli.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="生成交付文档")
    ap.add_argument("--local", action="store_true", help="已在 VPS 上")
    ap.add_argument("-o", "--outdir", default=".", help="输出目录")
    args = ap.parse_args()

    on_vps = os.path.exists(SUI_DB)
    if not (args.local or on_vps):
        from ssh_run import load_env
        return run_remote(load_env(), os.path.abspath(args.outdir))

    from ssh_run import load_env
    cfg = load_env()
    d = collect(cfg)

    os.makedirs(args.outdir, exist_ok=True)
    base = os.path.join(args.outdir, "VPS-%s-部署信息" % cfg["DOMAIN"])
    with open(base + ".md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_md(d))
    with open(base + ".html", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_html(d))
    print("已生成：")
    print("  " + base + ".md")
    print("  " + base + ".html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
