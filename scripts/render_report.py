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

# 客户端兼容性：这张表比任何参数都重要，写错会让人在两种"连不上"之间来回折腾
COMPAT = [
    # (客户端 / 内核, AnyTLS, TUIC, Hysteria2, VLESS-Reality, 说明)
    ("sing-box ≥1.12（Hiddify / Karing / NekoBox）", "是", "是", "是", "是", "四种全支持，首选"),
    ("mihomo / Clash Meta ≥1.19（Verge Rev / FlClash）", "是", "是", "是", "是", "四种全支持"),
    ("Xray 系（v2rayN / v2rayNG 默认内核）", "否", "否", "否", "是", "只认 VLESS-Reality"),
    ("Shadowrocket（iOS，≥2.2.65）", "是", "是", "是", "是", "需较新版本"),
    ("Surge", "否", "否", "是", "否", "只支持 Hysteria2"),
    ("QuantumultX", "否", "否", "否", "否", "不支持这四种"),
]

PROTO_DESC = {
    "anytls": "TLS-in-TLS 指纹打散 + 连接复用，日常主力",
    "tuic": "QUIC 传输，UDP 场景好，需 alpn=h3",
    "hysteria2": "QUIC + 拥塞控制激进，高丢包链路吞吐最好",
    "vless": "借用外部 SNI，抗主动探测，可作保底",
}


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
        "links": [],
        "tls": api.tls(),
        "server_ip": cfg.get("SERVER_IP") or cfg.get("VPS_HOST") or "",
    }

    # 入站 + 运行态用户数
    live = {ib.get("tag"): ib for ib in (api.singbox_config().get("inbounds") or [])}
    for ib in api.inbounds():
        tag = ib.get("tag")
        data["inbounds"].append({
            "tag": tag,
            "type": ib.get("type"),
            "port": ib.get("listen_port"),
            "tls_id": ib.get("tls_id"),
            "users": len((live.get(tag) or {}).get("users") or []),
            "desc": PROTO_DESC.get(ib.get("type"), ""),
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
    A("# %s 代理节点部署信息" % d["domain"])
    A("")
    A("> 由 `render_report.py` 自动生成，数据来自面板 API 与订阅端点实测回读。")
    A("> ⚠️ 含明文凭据，勿外传、勿提交公开仓库。")
    A("")
    A("## 一、面板")
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
    A("## 二、订阅地址")
    A("")
    A("| 格式 | 地址 | 给谁用 |")
    A("|---|---|---|")
    A("| 通用（base64） | %s | v2rayN / v2rayNG / NekoBox / Karing / Hiddify / Shadowrocket |" % d["sub_url"])
    A("| Clash YAML | %s | Clash Verge Rev / Clash Meta for Android / FlClash |" % d["sub_clash"])
    A("| sing-box JSON | %s | sing-box 原生配置 |" % d["sub_json"])
    A("")
    A("⚠️ **别把「通用」那条填给 Clash**，会报 `cannot unmarshal !!str`。")
    A("")
    A("## 三、节点")
    A("")
    A("| 节点名 | 协议 | 端口 | 运行态用户数 | 说明 |")
    A("|---|---|---|---|---|")
    for ib in d["inbounds"]:
        flag = "" if ib["users"] else " ⚠️"
        A("| %s | %s | %s | %d%s | %s |" % (
            ib["tag"], ib["type"], ib["port"], ib["users"], flag, ib["desc"]))
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

    A("## 四、客户端兼容性")
    A("")
    A("| 客户端 / 内核 | AnyTLS | TUIC | Hysteria2 | VLESS-Reality | 备注 |")
    A("|---|:--:|:--:|:--:|:--:|---|")
    for row in COMPAT:
        A("| %s | %s | %s | %s | %s | %s |" % row)
    A("")
    A("**这张表必须整张给用户。** 只写一半的话，用 Xray 内核的人会以为节点全挂了。")
    A("")
    A("## 五、端口与防火墙")
    A("")
    A("| 端口 | 协议 | 用途 |")
    A("|---|---|---|")
    A("| %s | TCP | 面板（HTTPS） |" % _port_of(d["panel_url"]))
    A("| %s | TCP | 订阅（HTTPS） |" % _port_of(d["sub_url"]))
    for ib in d["inbounds"]:
        A("| %s | %s | %s |" % (ib["port"],
                                "TCP" if ib["type"] in ("anytls", "vless") else "UDP",
                                ib["tag"]))
    A("")
    A("全部端口已写入 `/etc/iptables/rules.v4`，重启后仍生效。")
    A("")
    A("## 六、运维命令")
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
    A("## 七、注意事项")
    A("")
    A("1. **不要删 `/root/cert/`** —— 面板、订阅、AnyTLS、TUIC、Hysteria2 共用。")
    A("2. **改面板端口 / 路径 / 证书后必须 `systemctl restart s-ui`**，S-UI 不会自动重启。")
    A("3. **TUIC 与 AnyTLS 同用 8443**，一个是 UDP 一个是 TCP，互不影响；改防火墙时两个都要留。")
    A("4. 新增入站后 sing-box 会自动热加载，不需要重启。")
    A("")
    return "\n".join(L)


def _port_of(url: str) -> str:
    try:
        return str(urllib.parse.urlparse(url).port or "")
    except Exception:  # noqa: BLE001
        return ""


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
    parts.append('<tr><th>节点名</th><th>协议</th><th>端口</th><th>用户</th><th>说明</th></tr>')
    for ib in d["inbounds"]:
        cls = "ok" if ib["users"] else "bad"
        parts.append('<tr><td>%s</td><td>%s</td><td>%s</td><td class="%s">%d</td>'
                     '<td style="color:var(--mute)">%s</td></tr>'
                     % (esc(ib["tag"]), esc(ib["type"]), esc(ib["port"]),
                        cls, ib["users"], esc(ib["desc"])))
    parts.append('</table></div>')

    if d.get("links"):
        parts.append('<h2>分享链接</h2>')
        for ln in d["links"]:
            parts.append('<div class="card copy"><pre><code>%s</code></pre></div>' % esc(ln))

    parts.append('<h2>客户端兼容性</h2><div class="card"><table>')
    parts.append('<tr><th>客户端 / 内核</th><th>AnyTLS</th><th>TUIC</th><th>Hysteria2</th>'
                 '<th>VLESS-Reality</th><th>备注</th></tr>')
    for row in COMPAT:
        parts.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
                     '<td style="color:var(--mute)">%s</td></tr>'
                     % tuple(esc(x) for x in row))
    parts.append('</table></div>')
    parts.append('<div class="note">这张表要整张给用户。只写一半的话，'
                 '用 Xray 内核的人会以为节点全挂了。</div>')

    parts.append('<h2>端口与防火墙</h2><div class="card"><table>')
    parts.append('<tr><th>端口</th><th>协议</th><th>用途</th></tr>')
    parts.append('<tr><td>%s</td><td>TCP</td><td>面板 HTTPS</td></tr>' % esc(_port_of(d["panel_url"])))
    parts.append('<tr><td>%s</td><td>TCP</td><td>订阅 HTTPS</td></tr>' % esc(_port_of(d["sub_url"])))
    for ib in d["inbounds"]:
        parts.append('<tr><td>%s</td><td>%s</td><td>%s</td></tr>'
                     % (esc(ib["port"]),
                        "TCP" if ib["type"] in ("anytls", "vless") else "UDP",
                        esc(ib["tag"])))
    parts.append('</table></div>')

    parts.append('<h2>运维命令</h2><div class="card copy"><pre><code>'
                 'systemctl status s-ui          # 面板与 sing-box 同进程\n'
                 'systemctl restart s-ui         # 改端口/路径/证书后必须执行\n'
                 'journalctl -u s-ui -f          # 实时日志\n'
                 's-ui                           # 交互菜单\n'
                 'cp /usr/local/s-ui/db/s-ui.db /root/s-ui.db.bak.$(date +%Y%m%d-%H%M)'
                 '</code></pre></div>')

    parts.append('<h2>注意事项</h2><div class="card"><ol>'
                 '<li>不要删 <code>/root/cert/</code>，面板/订阅/各节点共用。</li>'
                 '<li>改面板端口、路径、证书后必须 <code>systemctl restart s-ui</code>。</li>'
                 '<li>TUIC 与 AnyTLS 同用 8443（一个 UDP 一个 TCP），改防火墙两个都要留。</li>'
                 '<li>新增入站 sing-box 会自动热加载，不需要重启。</li>'
                 '</ol></div>')

    return HTML_TPL.format(title=H.escape(d["domain"] + " 代理节点部署信息"),
                           body="\n".join(parts))


# ---------------------------------------------------------------- 入口

def run_remote(cfg: dict, outdir: str) -> int:
    from ssh_run import get_client, put_text, run, env_prefix

    cli = get_client(cfg)
    try:
        remote_dir = "/root/.sui-skill"
        run(cli, "mkdir -p " + remote_dir, timeout=20)
        for name in ("render_report.py", "sui_api.py", "ssh_run.py"):
            with open(os.path.join(HERE, name), "r", encoding="utf-8", newline="\n") as fh:
                put_text(cli, fh.read(), remote_dir + "/" + name)
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
