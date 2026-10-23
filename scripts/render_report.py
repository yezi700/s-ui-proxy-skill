#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成交付文档 VPS-<域名>-部署信息.md / .html。

数据来源全部是「实测回读」，不是硬编码：
  * 面板信息  ← /etc/x-ui/install-result.env
  * 节点链接  ← /panel/api/inbounds/allLinks
  * 订阅 URL  ← deploy.env 的 SUB_PORT / SUB_PATH / MERGED_SUBID
  * 防火墙    ← iptables -S INPUT / -t nat -S PREROUTING
  * 证书有效期 ← openssl x509 -enddate
  * BBR       ← sysctl

执行：
    python scripts/render_report.py                 # 输出到当前目录
    python scripts/render_report.py -o ./docs       # 指定输出目录
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ssh_run import env_exports, get_client, load_env, run  # noqa: E402

COLLECT = r"""
set -uo pipefail
W=/root/.xui-skill
echo "@@@INSTALL"
cat /etc/x-ui/install-result.env 2>/dev/null || echo "(无)"
echo "@@@CREDS"
# ⚠️ node-credentials.json 由 json.dump 写出，末尾没有换行；
#    不补一个 echo 的话下一行的 @@@LINKS 会粘在 "}" 后面，整段解析崩掉。
{ cat "$W/node-credentials.json" 2>/dev/null || echo "{}"; echo; }
echo "@@@LINKS"
curl -sk -H "Host: ${DOMAIN}" -H "Authorization: Bearer ${API_TOKEN}" \
  "https://127.0.0.1:${PANEL_PORT}/${PANEL_PATH}/panel/api/inbounds/allLinks" 2>/dev/null || echo "{}"
echo "@@@INBOUNDS"
curl -sk -H "Host: ${DOMAIN}" -H "Authorization: Bearer ${API_TOKEN}" \
  "https://127.0.0.1:${PANEL_PORT}/${PANEL_PATH}/panel/api/inbounds/list" 2>/dev/null || echo "{}"
echo "@@@SUB"
# 订阅内容才是用户实际导入的节点集合（合并客户端视角），优先用它渲染节点表。
# ⚠️ 订阅端点同样受 webDomain 的 Host 头校验影响，必须带 Host。
if [ -n "${SUB_PATH:-}" ] && [ -n "${MERGED_SUBID:-}" ]; then
  SP="${SUB_PATH#/}"; SP="${SP%/}"
  curl -sk -H "Host: ${DOMAIN}" \
    "https://127.0.0.1:${SUB_PORT}/${SP}/${MERGED_SUBID}" 2>/dev/null \
    | base64 -d 2>/dev/null || echo "(订阅读取失败)"
else
  echo "(未配置 SUB_PATH / MERGED_SUBID)"
fi
echo "@@@PANELDOM"
# webDomain 决定面板是否强制 Host 头校验；subClash* 决定 Clash 订阅是否可用。
curl -sk -X POST -H "Host: ${DOMAIN}" -H "Authorization: Bearer ${API_TOKEN}" \
  "https://127.0.0.1:${PANEL_PORT}/${PANEL_PATH}/panel/api/setting/all" 2>/dev/null \
  | python3 -c 'import json,sys
try:
    o = json.load(sys.stdin).get("obj") or {}
except Exception:
    o = {}
print("webDomain=" + (o.get("webDomain") or ""))
print("subClashEnable=" + ("1" if o.get("subClashEnable") else "0"))
print("subClashPath=" + (o.get("subClashPath") or "").strip("/"))'
echo "@@@BENCH"
# 实测数据由 bench 脚本写入，格式 [{"node":..,"avg":..,"success":..}, ...]
{ cat "$W/bench.json" 2>/dev/null || echo "[]"; echo; }
echo "@@@FW4"
iptables -S INPUT 2>/dev/null || echo "(无)"
echo "@@@NAT"
iptables -t nat -S PREROUTING 2>/dev/null || echo "(无)"
echo "@@@CERT"
if [ -f "/root/cert/${DOMAIN}/fullchain.pem" ]; then
  openssl x509 -in "/root/cert/${DOMAIN}/fullchain.pem" -noout \
    -subject -issuer -enddate 2>/dev/null
else
  echo "(无证书)"
fi
echo "@@@SYS"
. /etc/os-release 2>/dev/null && echo "OS=$PRETTY_NAME"
echo "KERNEL=$(uname -r)"
echo "ARCH=$(uname -m)"
echo "UPTIME=$(uptime -p 2>/dev/null || uptime)"
echo "@@@BBR"
sysctl net.ipv4.tcp_congestion_control 2>/dev/null
sysctl net.core.default_qdisc 2>/dev/null
echo "@@@VER"
/usr/local/x-ui/x-ui -v 2>/dev/null || echo "(未知)"
/usr/local/x-ui/bin/xray-linux-amd64 -version 2>/dev/null | head -2 || true
echo "@@@END"
"""


def parse_sections(text: str) -> dict[str, str]:
    """按 @@@NAME 分段。

    ⚠️ 不能用「整行等于 @@@NAME」来判定：`curl` 的输出末尾没有换行，
    紧跟其后的 `echo "@@@NEXT"` 会和上一段的最后一个字符粘在同一行
    （例如 `}@@@INBOUNDS`），导致**两个**段落同时丢失，表现为
    「节点数：0」这类莫名其妙的空结果。所以按出现位置切分，而不是按行。
    """
    out: dict[str, str] = {}
    marks = list(re.finditer(r"@@@(\w+)", text))
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[m.group(1)] = text[start:end].strip()
    return out


def kv_block(text: str) -> dict[str, str]:
    d = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip()
    return d


def mask(secret: str, keep: int = 4) -> str:
    if not secret:
        return ""
    if len(secret) <= keep * 2:
        return "*" * len(secret)
    return secret[:keep] + "*" * (len(secret) - keep * 2) + secret[-keep:]


# ---------------------------------------------------------------- Markdown

def render_md(ctx: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    d = ctx
    L: list[str] = []
    A = L.append

    A(f"# {d['domain']} 代理部署信息")
    A("")
    A(f"> 生成时间：{now}　|　服务器：`{d['host']}`　|　系统：{d.get('os', '')}")
    A("")
    A("---")
    A("")

    # 1. 面板
    A("## 1. 3x-ui 面板")
    A("")
    A("| 项目 | 值 |")
    A("|---|---|")
    A(f"| 面板地址 | {d['panel_url']} |")
    A(f"| 用户名 | `{d['panel_user']}` |")
    A(f"| 密码 | `{d['panel_pass']}` |")
    A(f"| 面板端口 | `{d['panel_port']}` |")
    A(f"| 面板路径 | `{d['panel_path']}` |")
    A(f"| API Token | `{d['api_token']}` |")
    A(f"| 证书有效期 | {d.get('cert_end', '(未知)')} |")
    A(f"| 面板版本 | {d.get('ver', '(未知)')} |")
    A("")
    if d.get("panel_dom"):
        A("> ⚠️ 面板启用了 `webDomain` = `" + d["panel_dom"] + "`，"
          "**只能用域名访问，用 IP 会返回 403**（设计如此）。")
        A("> 从服务器本机调 API 必须带 `Host: " + d["panel_dom"] + "` 头，否则 403 且响应体为空。")
    else:
        A("> ℹ️ 面板 `webDomain` 为空 —— 域名与 IP 都能访问，本机调 API 不必带 `Host` 头。")
        A("> 想只允许域名访问（防 IP 扫描），可在「面板设置 → 常规」里填写域名后重启面板。")
    A("")

    # 2. 订阅
    A("## 2. 订阅地址（推荐）")
    A("")
    A("一条订阅返回全部节点。**按客户端类型选对应的那一条**，两条内容相同、格式不同：")
    A("")
    A("**① 通用格式（base64 节点列表）** —— v2rayN / v2rayNG / NekoBox / Karing / Hiddify / Shadowrocket")
    A("")
    A("```")
    A(d["sub_url"])
    A("```")
    A("")
    if d.get("clash_url"):
        A("**② Clash / mihomo 格式（YAML）** —— Clash Verge Rev / Clash Meta for Android / FlClash")
        A("")
        A("```")
        A(d["clash_url"])
        A("```")
        A("")
        A("> ⚠️ Clash 系**不能**用 ①：mihomo 只认 YAML，直接导入 ① 会报")
        A("> `yaml: unmarshal errors: cannot unmarshal !!str`。反过来通用客户端也不吃 ②。")
        A("")
    A(f"合并客户端 email：`{d['merged_email']}`　subId：`{d['merged_subid']}`")
    A("")

    # 3. 节点
    A("## 3. 节点明细")
    A("")
    for i, node in enumerate(d["nodes"], 1):
        A(f"### 节点 {i}：{node['remark']}")
        A("")
        A(f"- 协议：`{node['protocol']}`　端口：`{node['port']}`")
        A("")
        A("```")
        A(node["uri"])
        A("```")
        A("")
        if node.get("params"):
            A("| 参数 | 值 |")
            A("|---|---|")
            for k, v in node["params"]:
                A(f"| {k} | `{v}` |")
            A("")

    # 4. 兼容性
    A("## 4. ⚠️ 客户端兼容性（重要）")
    A("")
    A("两条「连不上」的根因**完全不同**，别搞混：")
    A("")
    A("- Xray-core ≥ **26.9.8** 起，REALITY 服务端要求客户端 ClientHello 携带")
    A("  `X25519MLKEM768`（后量子混合密钥交换）→ **sing-box 系客户端连不上 REALITY**。")
    A("- **Xray-core 根本没有实现 Hysteria2 / TUIC** → **Xray 系客户端连不上这两个**。")
    A("")
    A("| 客户端 | 内核 | REALITY | Hysteria2 | TUIC |")
    A("|---|---|---|---|---|")
    A("| v2rayN（默认配置） | Xray | ✅ | ❌ | ❌ |")
    A("| v2rayN（切到 sing-box 内核） | sing-box | ❌ | ✅ | ✅ |")
    A("| v2rayNG (Android) | Xray（无法切） | ✅ | ❌ | ❌ |")
    A("| Clash Verge Rev / Clash Meta | mihomo | ✅ | ✅ | ✅ |")
    A("| Hiddify / Karing / NekoBox | sing-box | ❌ | ✅ | ✅ |")
    A("| Shadowrocket 旧版（2.2.92） | 自研 | ❌ | ✅ | ✅ |")
    A("")
    A("**结论（两条都要看，别只记住一条）**：")
    A("")
    A("1. **sing-box 系**（Hiddify / Karing / NekoBox / 旧版小火箭）")
    A("   → 连不上 **REALITY**，请改用 **Hysteria2 或 TUIC**。")
    A("2. **Xray 系**（v2rayN 默认 / v2rayNG）")
    A("   → 连不上 **Hysteria2 / TUIC**，只能用 **REALITY**。")
    A("   v2rayN 可在「设置 → 参数设置」把默认内核切成 sing-box；v2rayNG 没有内核切换。")
    A("3. **mihomo 系**（Clash Verge Rev / Clash Meta）三个节点全部可用，最省心。")
    A("")
    A("以上都不是配置错误，是客户端内核的实现差异。")
    A("")

    # 5. 防火墙
    A("## 5. 防火墙放行")
    A("")
    A("| 端口 | 协议 | 用途 |")
    A("|---|---|---|")
    for port, proto, use in d["fw_ports"]:
        A(f"| {port} | {proto} | {use} |")
    A("")
    A("UDP 端口跳跃（Hysteria2）通过 iptables NAT REDIRECT 实现：")
    A("")
    A("```")
    A(d["nat_rule"] or "(无)")
    A("```")
    A("")

    # 6. 实测
    if d.get("bench"):
        A("## 6. 实测数据")
        A("")
        A("| 节点 | 平均建连 | 成功率 |")
        A("|---|---|---|")
        for row in d["bench"]:
            # bench 数据既可能是 (node, avg, success) 元组，也可能是
            # {"node":..,"avg":..,"success":..} 字典（bench 脚本落盘格式）
            if isinstance(row, dict):
                A(f"| {row.get('node', '')} | {row.get('avg', '')} | {row.get('success', '')} |")
            else:
                A(f"| {row[0]} | {row[1]} | {row[2]} |")
        A("")

    # 7. 运维
    A("## 7. 常用运维命令")
    A("")
    A("```bash")
    A("systemctl status x-ui          # 面板状态")
    A("systemctl restart x-ui         # 重启面板（改配置后必做）")
    A("/usr/local/x-ui/x-ui -v        # 面板版本")
    A("")
    A("# 备份数据库（动手前先做）")
    A(f"cp /etc/x-ui/x-ui.db /root/x-ui-backup-$(date +%Y%m%d-%H%M).db")
    A("")
    A("# 查看入站/客户端")
    A("x-ui settings                  # 查看面板设置")
    A("")
    A("# 防火墙")
    A("iptables -S INPUT              # 查看规则")
    A("iptables -t nat -S PREROUTING  # 查看 NAT")
    A("netfilter-persistent save      # 持久化（改完必做）")
    A("")
    A("# 证书续期")
    A(f"/root/.acme.sh/acme.sh --cron --home /root/.acme.sh")
    A("```")
    A("")

    # 8. 注意事项
    A("## 8. 注意事项")
    A("")
    A("1. **不要删除 `/root/cert/`** —— 面板、Hysteria2、TUIC 共用这里的证书。")
    A("2. **443 同时承载 TCP（REALITY）与 UDP（Hysteria2）**，改防火墙时两个都要留。")
    A("3. **改任何入站配置后必须 `systemctl restart x-ui`** —— v3.9.0 增删入站走 Xray 的")
    A("   gRPC API 热更新，**不会重写 `bin/config.json`**；不重启的话该文件留着旧入站，")
    A("   下次 Xray 重启会因端口冲突起不来（REALITY 与 Hysteria2 一起挂）。")
    A("4. 从服务器本机自测代理**无法验证防火墙**（走 lo，不经 INPUT 链），外部可达性需另测。")
    A("5. `subId` 全局唯一，合并订阅靠「一个客户端绑多个入站」，不能共享 subId。")
    A("6. **API Token 只在创建时返回一次**（v3.9.0 的 `api_tokens` 表只存 SHA-256）；")
    A("   本文件里的 Token 请立刻备份到密码管理器，丢了只能在面板里重新建一个。")
    A("7. 面板「入站列表 → 全部链接」会列出 **6 条**（3 条原始客户端 + 3 条 `merged`），")
    A("   而订阅只返回 `merged` 的 3 条 —— 这是合并流程的正常状态，不是重复部署。")
    A("   想让面板也只保留 3 条，先确认订阅导入可用，再删掉原始客户端（不可逆）：")
    A("   ```bash")
    A("   for e in reality-443 hy2-443 tuic-8443; do \\")
    A("     curl -sk -X POST -H \"Authorization: Bearer $API_TOKEN\" \\")
    A("       \"$BASE/clients/del/$e\"; done")
    A("   ```")
    A("8. 本文件含明文凭据，请妥善保管，不要提交到公开仓库。")
    A("")

    return "\n".join(L)


# ---------------------------------------------------------------- HTML

HTML_TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{domain} 代理部署信息</title>
<style>
:root{{
  --bg:#f6f7f9; --card:#fff; --fg:#1f2328; --muted:#656d76;
  --border:#d8dee4; --accent:#0969da; --code-bg:#f0f2f5;
  --ok:#1a7f37; --warn:#9a6700; --err:#cf222e;
}}
@media (prefers-color-scheme: dark){{
  :root{{
    --bg:#0d1117; --card:#161b22; --fg:#e6edf3; --muted:#8b949e;
    --border:#30363d; --accent:#58a6ff; --code-bg:#1c2128;
    --ok:#3fb950; --warn:#d29922; --err:#f85149;
  }}
}}
*{{box-sizing:border-box}}
body{{margin:0;padding:32px 20px;background:var(--bg);color:var(--fg);
  font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
  "Hiragino Sans GB","Microsoft YaHei",sans-serif;}}
.wrap{{max-width:920px;margin:0 auto}}
h1{{font-size:26px;margin:0 0 8px}}
h2{{font-size:19px;margin:36px 0 12px;padding-bottom:8px;
  border-bottom:1px solid var(--border)}}
h3{{font-size:16px;margin:24px 0 8px}}
.meta{{color:var(--muted);font-size:13px;margin-bottom:8px}}
.card{{background:var(--card);border:1px solid var(--border);
  border-radius:10px;padding:20px 22px;margin:14px 0}}
table{{border-collapse:collapse;width:100%;margin:10px 0;font-size:14px}}
th,td{{border:1px solid var(--border);padding:8px 10px;text-align:left;
  vertical-align:top;word-break:break-all}}
th{{background:var(--code-bg);font-weight:600;white-space:nowrap}}
code,pre{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:13px}}
code{{background:var(--code-bg);padding:2px 5px;border-radius:4px}}
pre{{background:var(--code-bg);padding:12px 14px;border-radius:8px;
  overflow-x:auto;margin:8px 0;position:relative}}
.link{{position:relative}}
.copy{{position:absolute;top:8px;right:8px;background:var(--card);
  color:var(--muted);border:1px solid var(--border);border-radius:6px;
  padding:3px 9px;font-size:12px;cursor:pointer}}
.copy:hover{{color:var(--accent);border-color:var(--accent)}}
.pill{{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;
  border:1px solid var(--border)}}
.pill.y{{color:var(--ok);border-color:var(--ok)}}
.pill.n{{color:var(--err);border-color:var(--err)}}
.warn{{background:rgba(154,103,0,.1);border-left:4px solid var(--warn);
  padding:12px 16px;border-radius:6px;margin:12px 0;font-size:14px}}
.note{{background:rgba(28,110,164,.08);border-left:4px solid var(--accent);
  padding:12px 16px;border-radius:6px;margin:12px 0;font-size:14px}}
.toc{{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}}
.toc a{{color:var(--accent);text-decoration:none;font-size:13px;
  border:1px solid var(--border);border-radius:20px;padding:3px 12px}}
.toc a:hover{{border-color:var(--accent)}}
</style>
</head>
<body>
<div class="wrap">
<h1>{domain} 代理部署信息</h1>
<div class="meta">生成时间 {now}　·　服务器 <code>{host}</code>　·　{os}</div>
<div class="toc">
  <a href="#panel">面板</a><a href="#sub">订阅</a><a href="#nodes">节点</a>
  <a href="#compat">兼容性</a><a href="#fw">防火墙</a><a href="#ops">运维</a>
  <a href="#notes">注意事项</a>
</div>

<h2 id="panel">1. 3x-ui 面板</h2>
<div class="card">
<table>
<tr><th>面板地址</th><td><a href="{panel_url}" target="_blank">{panel_url}</a></td></tr>
<tr><th>用户名</th><td><code>{panel_user}</code></td></tr>
<tr><th>密码</th><td><code>{panel_pass}</code></td></tr>
<tr><th>面板端口</th><td><code>{panel_port}</code></td></tr>
<tr><th>面板路径</th><td><code>{panel_path}</code></td></tr>
<tr><th>API Token</th><td><code>{api_token}</code></td></tr>
<tr><th>证书有效期</th><td>{cert_end}</td></tr>
<tr><th>面板版本</th><td>{ver}</td></tr>
</table>
{panel_dom_note}
</div>

<h2 id="sub">2. 订阅地址（推荐）</h2>
<div class="card">
<p>一条订阅返回全部节点，导入客户端后自动更新。<b>按客户端类型选对应的那一条</b>，两条内容相同、格式不同：</p>
<p><b>① 通用格式（base64 节点列表）</b> —— v2rayN / v2rayNG / NekoBox / Karing / Hiddify / Shadowrocket</p>
<pre class="link"><code>{sub_url}</code><button class="copy">复制</button></pre>
{sub_clash_block}
<p class="meta">合并客户端 email：<code>{merged_email}</code>　subId：<code>{merged_subid}</code></p>
</div>

<h2 id="nodes">3. 节点明细</h2>
{nodes_html}

<h2 id="compat">4. ⚠️ 客户端兼容性（重要）</h2>
<div class="card">
<p>两条「连不上」的根因<b>完全不同</b>，别搞混：</p>
<ul>
<li>Xray-core ≥ <b>26.9.8</b> 起，REALITY 服务端要求客户端 ClientHello 携带
<code>X25519MLKEM768</code>（后量子混合密钥交换）→ <b>sing-box 系客户端连不上 REALITY</b>。</li>
<li><b>Xray-core 根本没有实现 Hysteria2 / TUIC</b> → <b>Xray 系客户端连不上这两个</b>。</li>
</ul>
<table>
<tr><th>客户端</th><th>内核</th><th>REALITY</th><th>Hysteria2</th><th>TUIC</th></tr>
<tr><td>v2rayN（默认配置）</td><td>Xray</td>
    <td><span class="pill y">可用</span></td><td><span class="pill n">不可用</span></td><td><span class="pill n">不可用</span></td></tr>
<tr><td>v2rayN（切到 sing-box 内核）</td><td>sing-box</td>
    <td><span class="pill n">不可用</span></td><td><span class="pill y">可用</span></td><td><span class="pill y">可用</span></td></tr>
<tr><td>v2rayNG (Android)</td><td>Xray（无法切）</td>
    <td><span class="pill y">可用</span></td><td><span class="pill n">不可用</span></td><td><span class="pill n">不可用</span></td></tr>
<tr><td>Clash Verge Rev / Clash Meta</td><td>mihomo</td>
    <td><span class="pill y">可用</span></td><td><span class="pill y">可用</span></td><td><span class="pill y">可用</span></td></tr>
<tr><td>Hiddify / Karing / NekoBox</td><td>sing-box</td>
    <td><span class="pill n">不可用</span></td><td><span class="pill y">可用</span></td><td><span class="pill y">可用</span></td></tr>
<tr><td>Shadowrocket 旧版（2.2.92）</td><td>自研</td>
    <td><span class="pill n">不可用</span></td><td><span class="pill y">可用</span></td><td><span class="pill y">可用</span></td></tr>
</table>
<p><b>结论（两条都要看，别只记住一条）</b>：</p>
<ol>
<li><b>sing-box 系</b>（Hiddify / Karing / NekoBox / 旧版小火箭）→ 连不上 <b>REALITY</b>，
    请改用 <b>Hysteria2 或 TUIC</b>。</li>
<li><b>Xray 系</b>（v2rayN 默认 / v2rayNG）→ 连不上 <b>Hysteria2 / TUIC</b>，只能用 <b>REALITY</b>。
    v2rayN 可在「设置 → 参数设置」把默认内核切成 sing-box；v2rayNG 没有内核切换。</li>
<li><b>mihomo 系</b>（Clash Verge Rev / Clash Meta）三个节点全部可用，最省心。</li>
</ol>
<p>以上都不是配置错误，是客户端内核的实现差异。</p>
</div>

<h2 id="fw">5. 防火墙放行</h2>
<div class="card">
<table><tr><th>端口</th><th>协议</th><th>用途</th></tr>
{fw_rows}
</table>
<p>UDP 端口跳跃（Hysteria2）通过 iptables NAT REDIRECT 实现：</p>
<pre><code>{nat_rule}</code></pre>
</div>

<h2 id="ops">6. 常用运维命令</h2>
<div class="card">
<pre class="link"><code>{ops_text}</code><button class="copy">复制</button></pre>
</div>

<h2 id="notes">7. 注意事项</h2>
<div class="card">
<ol>
<li><b>不要删除 <code>/root/cert/</code></b> —— 面板、Hysteria2、TUIC 共用这里的证书。</li>
<li><b>443 同时承载 TCP（REALITY）与 UDP（Hysteria2）</b>，改防火墙时两个都要留。</li>
<li><b>改任何入站配置后必须 <code>systemctl restart x-ui</code></b> —— v3.9.0 增删入站走 Xray 的
gRPC API 热更新，<b>不会重写 <code>bin/config.json</code></b>；不重启的话该文件留着旧入站，
下次 Xray 重启会因端口冲突起不来（REALITY 与 Hysteria2 一起挂）。</li>
<li>从服务器本机自测代理<b>无法验证防火墙</b>（走 lo，不经 INPUT 链），外部可达性需另测。</li>
<li><code>subId</code> 全局唯一，合并订阅靠「一个客户端绑多个入站」，不能共享 subId。</li>
<li><b>API Token 只在创建时返回一次</b>（v3.9.0 的 <code>api_tokens</code> 表只存 SHA-256）；
本文件里的 Token 请立刻备份到密码管理器，丢了只能在面板里重新建一个。</li>
<li>面板「入站列表 → 全部链接」会列出 <b>6 条</b>（3 条原始客户端 + 3 条 <code>merged</code>），
订阅只返回 <code>merged</code> 的 3 条 —— 这是合并流程的正常状态，不是重复部署。
想让面板也只保留 3 条，先确认订阅导入可用，再删掉原始客户端（不可逆）。</li>
<li>本文件含明文凭据，请妥善保管，不要提交到公开仓库。</li>
</ol>
</div>

</div>
<script>
document.querySelectorAll('.copy').forEach(function(b){{
  b.addEventListener('click', function(){{
    var t = b.parentElement.querySelector('code').innerText;
    navigator.clipboard.writeText(t).then(function(){{
      var old = b.innerText; b.innerText = '已复制';
      setTimeout(function(){{ b.innerText = old; }}, 1200);
    }});
  }});
}});
</script>
</body>
</html>
"""


def render_html(ctx: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    e = html.escape

    if ctx.get("panel_dom"):
        pd = e(ctx["panel_dom"])
        panel_dom_note = (
            f'<div class="warn">面板启用了 <code>webDomain</code> = <code>{pd}</code>，'
            f'<b>只能用域名访问，用 IP 会 403</b>（设计如此）。'
            f'从服务器本机调 API 必须带 <code>Host: {pd}</code> 头，否则 403 且响应体为空。</div>'
        )
    else:
        panel_dom_note = (
            '<div class="note">面板 <code>webDomain</code> 为空 —— 域名与 IP 都能访问，'
            '本机调 API 不必带 <code>Host</code> 头。想只允许域名访问（防 IP 扫描），'
            '可在「面板设置 → 常规」填写域名后重启面板。</div>'
        )

    if ctx.get("clash_url"):
        sub_clash_block = (
            '<p><b>② Clash / mihomo 格式（YAML）</b> —— Clash Verge Rev / '
            'Clash Meta for Android / FlClash</p>'
            f'<pre class="link"><code>{e(ctx["clash_url"])}</code>'
            '<button class="copy">复制</button></pre>'
            '<div class="warn">Clash 系<b>不能</b>用 ①：mihomo 只认 YAML，直接导入 ① 会报 '
            '<code>yaml: unmarshal errors: cannot unmarshal !!str</code>。'
            '反过来通用客户端也不吃 ②。</div>'
        )
    else:
        sub_clash_block = (
            '<div class="warn">面板未启用 Clash 订阅（<code>subClashEnable=false</code>），'
            'Clash 系客户端无法直接导入。可在「面板设置 → 订阅 → Clash」里启用。</div>'
        )

    nodes_html = []
    for i, node in enumerate(ctx["nodes"], 1):
        rows = "".join(
            f"<tr><th>{e(k)}</th><td><code>{e(v)}</code></td></tr>"
            for k, v in node.get("params", [])
        )
        nodes_html.append(f"""
<h3>节点 {i}：{e(node['remark'])}　<span class="pill">{e(node['protocol'])}</span>
<span class="pill">:{e(str(node['port']))}</span></h3>
<div class="card">
<pre class="link"><code>{e(node['uri'])}</code><button class="copy">复制</button></pre>
{f'<table>{rows}</table>' if rows else ''}
</div>""")

    fw_rows = "".join(
        f"<tr><td><code>{e(str(p))}</code></td><td>{e(pr)}</td><td>{e(u)}</td></tr>"
        for p, pr, u in ctx["fw_ports"]
    )

    ops_text = (
        "systemctl status x-ui          # 面板状态\n"
        "systemctl restart x-ui         # 重启面板（改配置后必做）\n"
        "/usr/local/x-ui/x-ui -v        # 面板版本\n"
        "\n"
        "# 备份数据库（动手前先做）\n"
        "cp /etc/x-ui/x-ui.db /root/x-ui-backup-$(date +%Y%m%d-%H%M).db\n"
        "\n"
        "# 防火墙\n"
        "iptables -S INPUT              # 查看规则\n"
        "iptables -t nat -S PREROUTING  # 查看 NAT\n"
        "netfilter-persistent save      # 持久化（改完必做）\n"
        "\n"
        "# 证书续期\n"
        "/root/.acme.sh/acme.sh --cron --home /root/.acme.sh"
    )

    return HTML_TPL.format(
        domain=e(ctx["domain"]), now=now, host=e(ctx["host"]),
        os=e(ctx.get("os", "")),
        panel_url=e(ctx["panel_url"]), panel_user=e(ctx["panel_user"]),
        panel_pass=e(ctx["panel_pass"]), panel_port=e(ctx["panel_port"]),
        panel_path=e(ctx["panel_path"]), api_token=e(ctx["api_token"]),
        cert_end=e(ctx.get("cert_end", "(未知)")), ver=e(ctx.get("ver", "")),
        sub_url=e(ctx["sub_url"]), merged_email=e(ctx["merged_email"]),
        merged_subid=e(ctx["merged_subid"]),
        nodes_html="".join(nodes_html), fw_rows=fw_rows,
        nat_rule=e(ctx["nat_rule"] or "(无)"), ops_text=e(ops_text),
        panel_dom_note=panel_dom_note,
        sub_clash_block=sub_clash_block,
    )


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="生成交付文档")
    ap.add_argument("-o", "--out", default=".", help="输出目录")
    args = ap.parse_args()

    cfg = load_env()
    for key in ("VPS_HOST", "VPS_USER", "DOMAIN", "PANEL_PORT", "PANEL_PATH"):
        if not cfg.get(key):
            sys.stderr.write(f"deploy.env 缺少 {key}\n")
            return 2

    cli = get_client(cfg)
    try:
        exports = env_exports({k: cfg.get(k, "") for k in (
            "DOMAIN", "PANEL_PORT", "PANEL_PATH", "API_TOKEN", "SUB_PORT",
            "SUB_PATH", "MERGED_SUBID", "MERGED_EMAIL"
        )})
        rc, out, err = run(cli, f"{exports}\nbash -s <<'EOS'\n{COLLECT}\nEOS",
                           timeout=180)
    finally:
        cli.close()

    if rc:
        sys.stderr.write(err or "远端信息采集失败，未生成交付文档\n")
        return rc

    sec = parse_sections(out)
    install = kv_block(sec.get("INSTALL", ""))
    creds = json.loads(sec.get("CREDS", "{}") or "{}")

    links_raw = sec.get("LINKS", "{}")
    try:
        links = json.loads(links_raw).get("obj") or []
    except json.JSONDecodeError:
        links = []

    # ⚠️ 版本差异：
    #   v3.9.0  /inbounds/allLinks 的 obj 是 [{remark, uri, port, protocol}, ...]
    #   v3.10.2 /inbounds/allLinks 的 obj 是 ["vless://...", ...]（纯字符串）
    #   这里统一成对象，后面解析逻辑不分叉。
    def _norm_link(it):
        if isinstance(it, dict):
            return it
        uri = it if isinstance(it, str) else ""
        m = re.search(r"@[^/@:]+:(\d+)", uri)
        return {
            "uri": uri,
            "remark": uri.rsplit("#", 1)[-1] if "#" in uri else "",
            "protocol": uri.split("://", 1)[0] if "://" in uri else "",
            "port": int(m.group(1)) if m else "",
        }

    links = [_norm_link(it) for it in links if isinstance(it, (dict, str))]

    # 节点表以「订阅实际返回的节点」为准（= 用户真正导入的内容）。
    # allLinks 会同时列出合并客户端与原始客户端的重复节点，直接渲染会让人困惑。
    sub_lines = [ln.strip() for ln in sec.get("SUB", "").splitlines()
                 if ln.strip() and "://" in ln]
    if sub_lines:
        links = [_norm_link(ln) for ln in sub_lines]

    inbounds_raw = sec.get("INBOUNDS", "{}")
    try:
        inbounds = json.loads(inbounds_raw).get("obj") or []
    except json.JSONDecodeError:
        inbounds = []

    # 解析节点参数
    nodes = []
    for it in links:
        uri = it.get("uri") or ""
        proto = it.get("protocol") or it.get("type") or ""
        params = []
        if uri.startswith("vless://"):
            q = uri.split("?", 1)[-1].split("#")[0]
            for kv in q.split("&"):
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    params.append((k, v))
        elif uri.startswith("hysteria2://"):
            pwd = uri.split("://", 1)[1].split("@")[0]
            params.append(("password", pwd))
            q = uri.split("?", 1)[-1].split("#")[0] if "?" in uri else ""
            for kv in q.split("&"):
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    params.append((k, v))
        elif uri.startswith("tuic://"):
            body = uri.split("://", 1)[1].split("@")[0]
            if ":" in body:
                uid, _, pwd = body.partition(":")
                params.append(("uuid", uid))
                params.append(("password", pwd))
            q = uri.split("?", 1)[-1].split("#")[0] if "?" in uri else ""
            for kv in q.split("&"):
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    params.append((k, v))
        nodes.append({
            "remark": it.get("remark") or proto,
            "protocol": proto,
            "port": it.get("port") or "",
            "uri": uri,
            "params": params,
        })

    # 证书
    cert_txt = sec.get("CERT", "")
    cert_end = ""
    for line in cert_txt.splitlines():
        if "notAfter" in line:
            cert_end = line.split("=", 1)[-1].strip()

    # 系统
    sysd = kv_block(sec.get("SYS", ""))

    # 实测数据（由 bench 脚本落盘，缺失时该节自动省略）
    try:
        bench = json.loads(sec.get("BENCH", "[]") or "[]")
    except json.JSONDecodeError:
        bench = []
    if not isinstance(bench, list):
        bench = []

    # 面板侧设置（webDomain / subClash*），COLLECT 里以 KEY=VALUE 形式输出
    panel_kv = kv_block(sec.get("PANELDOM", ""))

    # 版本
    ver_txt = sec.get("VER", "")
    ver = ""
    for line in ver_txt.splitlines():
        if line.strip() and not ver:
            ver = line.strip()

    # 防火墙
    fw_ports = [
        (cfg.get("SSH_PORT", "22"), "TCP", "SSH"),
        (cfg.get("ACME_PORT", "80"), "TCP", "ACME 证书签发"),
        (cfg.get("REALITY_PORT", "443"), "TCP", "VLESS-REALITY"),
        (cfg.get("HY2_PORT", "443"), "UDP", "Hysteria2"),
        (cfg.get("HY2_HOP_RANGE", "58888:60888"), "UDP", "Hysteria2 端口跳跃"),
        (cfg.get("TUIC_PORT", "8443"), "UDP", "TUIC v5"),
        (cfg.get("SUB_PORT", "2096"), "TCP", "订阅服务"),
        (cfg.get("PANEL_PORT", "46821"), "TCP", "3x-ui 面板"),
    ]
    nat_rule = ""
    for line in sec.get("NAT", "").splitlines():
        if "REDIRECT" in line:
            nat_rule = line.strip()
            break

    panel_path = cfg.get("PANEL_PATH", "")
    domain = cfg["DOMAIN"]
    panel_port = cfg.get("PANEL_PORT", "46821")
    ctx = {
        "domain": domain,
        "host": cfg["VPS_HOST"],
        "os": sysd.get("OS", ""),
        "panel_url": f"https://{domain}:{panel_port}/{panel_path}/",
        "panel_user": install.get("PANEL_USER") or cfg.get("PANEL_USER", ""),
        "panel_pass": install.get("PANEL_PASS") or cfg.get("PANEL_PASS", ""),
        "panel_port": panel_port,
        "panel_path": panel_path,
        "api_token": install.get("API_TOKEN") or cfg.get("API_TOKEN", ""),
        "cert_end": cert_end or "(未知)",
        "ver": ver,
        "sub_url": (f"https://{domain}:{cfg.get('SUB_PORT', '2096')}"
                    f"/{cfg.get('SUB_PATH', '')}/{cfg.get('MERGED_SUBID', '')}"),
        "merged_email": cfg.get("MERGED_EMAIL", "merged"),
        "merged_subid": cfg.get("MERGED_SUBID", ""),
        "nodes": nodes,
        "fw_ports": fw_ports,
        "nat_rule": nat_rule,
        "panel_dom": panel_kv.get("webDomain", ""),
        "bench": bench,
    }

    # Clash / mihomo 订阅：3x-ui 用独立的 subClashPath，且必须 subClashEnable=true。
    # 只有启用时才给出第二条 URL，否则会把 404 当订阅发给用户。
    if panel_kv.get("subClashEnable") == "1" and panel_kv.get("subClashPath"):
        ctx["clash_url"] = (f"https://{domain}:{cfg.get('SUB_PORT', '2096')}"
                            f"/{panel_kv['subClashPath']}/{cfg.get('MERGED_SUBID', '')}")
    else:
        ctx["clash_url"] = ""

    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)
    base = os.path.join(outdir, f"VPS-{domain}-部署信息")
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write(render_md(ctx))
    with open(base + ".html", "w", encoding="utf-8") as fh:
        fh.write(render_html(ctx))

    print("已生成：")
    print("  " + base + ".md")
    print("  " + base + ".html")
    print(f"节点数：{len(nodes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
