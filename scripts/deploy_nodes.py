#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在 S-UI 上创建入站与订阅用户。

默认四个协议，全部避开 443（实测 443 被针对性干扰，详见 references/pitfalls.md §1）：

    AnyTLS + TLS      8443/tcp   解决 TLS-in-TLS 指纹，自带连接复用
    Hysteria2         2083/udp   高丢包链路吞吐最好（salamander 混淆抗 QUIC 限速）
    VLESS+Reality     2053/tcp   抗主动探测，可作保底
    Shadowsocks 2022  2087/tcp   无 TLS 握手、延迟最低，实测这条线路最快

TUIC 默认**不再部署**：实测被限速到 0.2~0.4 Mbps（换端口、换客户端都一样），
想开就把 deploy.env 里的 TUIC_PORT 填上，见 references/pitfalls.md §8。

用法：
    python scripts/deploy_nodes.py              # 本地跑，自动上传到 VPS 执行
    python scripts/deploy_nodes.py --local      # 已在 VPS 上时直接执行
    python scripts/deploy_nodes.py --dry-run    # 只打印将要创建的内容

设计说明：
    脚本自身既是「本地编排器」也是「远端执行体」。本地跑时通过 SSH 把自己上传到
    VPS 再以 --local 执行 —— 这样所有面板 API 调用都在 127.0.0.1 上完成，
    不用操心 SNI / DNS / 防火墙 / 证书域名匹配。
"""
from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
import uuid as uuidlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SUI_DB = "/usr/local/s-ui/db/s-ui.db"

# node_specs() 的进程内缓存，见该函数的 docstring
_SPEC_CACHE = {}


# ---------------------------------------------------------------- 小工具

def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def gen_x25519() -> tuple:
    """生成 Reality 用的 x25519 密钥对，返回 (私钥, 公钥)，均为 base64url 无填充。

    走 openssl 而不是纯 Python：VPS 上一定有 openssl，cryptography 库不一定装。
    X25519 的 PKCS8 私钥 DER 固定 48 字节、SPKI 公钥 DER 固定 44 字节，
    末尾 32 字节就是原始密钥。
    """
    priv_der = subprocess.run(
        ["openssl", "genpkey", "-algorithm", "X25519", "-outform", "DER"],
        capture_output=True, check=True,
    ).stdout
    pub_der = subprocess.run(
        ["openssl", "pkey", "-inform", "DER", "-pubout", "-outform", "DER"],
        input=priv_der, capture_output=True, check=True,
    ).stdout
    return b64url(priv_der[-32:]), b64url(pub_der[-32:])


def gen_password(n: int = 22) -> str:
    return b64url(os.urandom(32)).replace("-", "").replace("_", "")[:n]


def short_id() -> str:
    return os.urandom(8).hex()


def gen_ss_key(method: str) -> str:
    """生成 SS2022 的 psk。

    长度由 method 决定：aes-128-gcm = 16 字节，aes-256-gcm / chacha20-poly1305 = 32 字节。
    长度不对 sing-box 会直接起不来（日志里是 initialize inbound: invalid password）。
    必须是标准 base64（带 `=` 填充），不能像别的密码那样用 urlsafe 去填充版。
    """
    size = 16 if "128" in method else 32
    return base64.b64encode(os.urandom(size)).decode()


def load_inbound_out(tag: str) -> dict:
    """从面板 DB 读回某个入站的 out_json（客户端侧配置）。

    GET /api/inbounds 只返回 id/tag/type/port/tls_id/users，不含 out_json，
    所以要拿 SS 的 method / psk 只能直接读库。
    ⚠️ out_json 是 BLOB 列，sqlite3 返回的是 bytes，必须 decode 再 json.loads。
    """
    import json
    import sqlite3
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % SUI_DB, uri=True)
        try:
            row = conn.execute("select out_json from inbounds where tag=?", (tag,)).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return {}
    if not row or not row[0]:
        return {}
    raw = row[0]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}


def load_client_config(cid: int) -> dict:
    """从面板 DB 读回某个 client 的 config。

    GET /api/clients 出于安全考虑不返回 config，而 edit 时又必须带上，
    否则 S-UI 会在 UpdateInboundsUsers 里对 NULL 做 json_extract 而报错。
    """
    import json
    import sqlite3
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % SUI_DB, uri=True)
        try:
            row = conn.execute("select config from clients where id=?", (cid,)).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return {}
    if not row or not row[0]:
        return {}
    try:
        return json.loads(row[0])
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------- 节点定义

def node_specs(cfg: dict) -> list:
    """返回要部署的节点声明。顺序即创建顺序。

    节点名（inbound tag）形如 `<NODE_PREFIX>-AnyTLS-8443`，
    NODE_PREFIX 取自 deploy.env（默认 US）—— 换地区时只改一处，
    不要像以前那样把地区写死在脚本里。

    TUIC / SS 都是可选的：deploy.env 里对应端口留空就不生成，
    这样不会出现「建了节点但防火墙没开端口」的半成品。

    ⚠️ 结果按配置缓存：本函数里会**随机生成** SS 的 psk，
    而 deploy() 里要调用两次（建入站一次、写客户端凭据一次）。
    不缓存的话两次拿到两把不同的钥匙，客户端必然连不上。
    """
    cache_key = (cfg.get("NODE_PREFIX"), cfg.get("ANYTLS_PORT"), cfg.get("HY2_PORT"),
                 cfg.get("REALITY_PORT"), cfg.get("TUIC_PORT"), cfg.get("SS_PORT"),
                 cfg.get("SS_METHOD"), cfg.get("SS_PASSWORD"))
    if cache_key in _SPEC_CACHE:
        return _SPEC_CACHE[cache_key]

    prefix = (cfg.get("NODE_PREFIX") or "US").strip().upper() or "US"
    anytls_port = int(cfg.get("ANYTLS_PORT") or 8443)
    hy2_port = int(cfg.get("HY2_PORT") or 2083)
    reality_port = int(cfg.get("REALITY_PORT") or 2053)
    # 键不存在时用默认值；**显式留空**（TUIC_PORT= / SS_PORT=）表示关掉这个节点。
    # 所以不能用 `or 默认值` —— 那样空字符串会被当成「没设置」又填回默认。
    def _opt(key, default):
        raw = cfg.get(key)
        return default if raw is None else int(raw or 0)

    tuic_port = _opt("TUIC_PORT", 0)      # 默认关（§8）
    ss_port = _opt("SS_PORT", 2087)       # 默认开

    specs = [
        {
            "tag": "%s-AnyTLS-%d" % (prefix, anytls_port),
            "type": "anytls",
            "port": anytls_port,
            "tls": "le",
            "out_extra": {},
        },
        {
            "tag": "%s-Hysteria2-%d" % (prefix, hy2_port),
            "type": "hysteria2",
            "port": hy2_port,
            "tls": "le",
            "out_extra": {},
        },
        {
            "tag": "%s-Reality-%d" % (prefix, reality_port),
            "type": "vless",
            "port": reality_port,
            "tls": "reality",
            "out_extra": {"flow": "xtls-rprx-vision"},
        },
    ]

    if ss_port:
        method = cfg.get("SS_METHOD") or "2022-blake3-aes-128-gcm"
        specs.append({
            "tag": "%s-Shadowsocks-%d" % (prefix, ss_port),
            "type": "shadowsocks",
            "port": ss_port,
            "tls": None,             # SS 自己就是加密协议，不挂 TLS 对象
            "out_extra": {"method": method,
                          "password": cfg.get("SS_PASSWORD") or gen_ss_key(method)},
        })

    if tuic_port:
        # TUIC 实测在本机线路被限速（§8），默认关掉，填了端口才建
        specs.append({
            "tag": "%s-TUIC-%d" % (prefix, tuic_port),
            "type": "tuic",
            "port": tuic_port,
            "tls": "tuic",   # 必须用带 alpn:["h3"] 的独立 TLS 对象
            "out_extra": {"congestion_control": "bbr", "udp_relay_mode": "native"},
        })

    _SPEC_CACHE[cache_key] = specs
    return specs


# ---------------------------------------------------------------- 核心

def deploy(cfg: dict, dry_run: bool = False) -> int:
    from sui_api import SuiApi

    domain = cfg["DOMAIN"]
    cert_dir = cfg.get("CERT_DIR") or "/root/cert/" + domain
    sni = cfg.get("REALITY_SNI") or "www.apple.com"
    sub_id = cfg.get("SUB_ID") or "us"

    api = SuiApi.from_env(cfg)
    api.login()
    print("已登录面板 %s" % api.base)

    # ---------------------------------------------------------- 1. TLS 对象
    tls_by_name = {t["name"]: t for t in api.tls()}

    def ensure_tls(name, server, client):
        exist = tls_by_name.get(name)
        if exist:
            print("[tls] %s 已存在 (id=%s)，跳过" % (name, exist["id"]))
            return exist
        if dry_run:
            print("[tls] 将创建 %s" % name)
            return {"id": -1, "name": name, "server": server, "client": client}
        api.save_tls(name, server, client)
        fresh = {t["name"]: t for t in api.tls()}[name]
        print("[tls] %s 创建成功 (id=%s)" % (name, fresh["id"]))
        return fresh

    # 1a. 域名证书（AnyTLS / Hysteria2 共用）
    le_server = {
        "enabled": True, "server_name": domain,
        "certificate_path": cert_dir + "/fullchain.pem",
        "key_path": cert_dir + "/privkey.pem",
    }
    le_client = {"enabled": True, "server_name": domain, "insecure": False}
    tls_le = ensure_tls(domain, le_server, le_client)

    # 1b. TUIC 专用 —— 必须带 alpn h3，否则客户端报
    #     `tls: server did not select an ALPN protocol`
    #     只有真的建 TUIC 时才创建这个对象，避免留一个没人用的 TLS
    tls_tuic = None
    if int(cfg.get("TUIC_PORT") or 0):
        tls_tuic = ensure_tls(
            "tuic-" + domain,
            dict(le_server, alpn=["h3"]),
            dict(le_client, alpn=["h3"]),
        )

    # 1c. Reality —— 优先复用已有对象里的密钥，避免重复运行时凭据"漂移"
    reality_name = "reality-" + domain
    exist_rt = tls_by_name.get(reality_name)
    if exist_rt:
        srv = exist_rt.get("server") or {}
        cli_ = exist_rt.get("client") or {}
        rt_srv = srv.get("reality") or {}
        rt_cli = cli_.get("reality") or {}
        priv = rt_srv.get("private_key") or ""
        pub = rt_cli.get("public_key") or ""
        sids = rt_srv.get("short_id") or []
        sid = sids[0] if sids else ""
        sni = srv.get("server_name") or sni
        print("[reality] 复用已有对象 (id=%s)  pub=%s..." % (exist_rt["id"], pub[:12]))
    else:
        priv = cfg.get("REALITY_PRIV") or ""
        pub = cfg.get("REALITY_PUB") or ""
        sid = cfg.get("REALITY_SID") or ""
        if not (priv and pub and sid):
            priv, pub = gen_x25519()
            sid = short_id()
            print("[reality] 已生成新密钥对  pub=%s...  sid=%s" % (pub[:12], sid))

    reality_server = {
        "enabled": True, "server_name": sni,
        "reality": {
            "enabled": True,
            "handshake": {"server": sni, "server_port": 443},
            "private_key": priv,
            "short_id": [sid],
        },
    }
    reality_client = {
        "enabled": True, "server_name": sni,
        "utls": {"enabled": True, "fingerprint": "chrome"},
        "reality": {"enabled": True, "public_key": pub, "short_id": sid},
    }
    tls_reality = ensure_tls(reality_name, reality_server, reality_client)

    tls_map = {"le": tls_le, "tuic": tls_tuic, "reality": tls_reality, None: None}

    # ---------------------------------------------------------- 2. 入站
    existing = {i["tag"]: i for i in api.inbounds()}

    for spec in node_specs(cfg):
        tag = spec["tag"]
        if tag in existing:
            print("[inbound] %s 已存在 (id=%s)，跳过" % (tag, existing[tag]["id"]))
            continue

        tls_obj = tls_map[spec["tls"]]
        out = {
            "type": spec["type"],
            "server": domain,
            "server_port": spec["port"],
        }
        if tls_obj:                       # SS 不挂 TLS
            client_tls = dict(tls_obj.get("client") or {})
            client_tls.setdefault("enabled", True)
            out["tls"] = client_tls
        out.update(spec["out_extra"])

        inbound = {
            "type": spec["type"],
            "tag": tag,
            "listen": "::",
            "listen_port": spec["port"],
            "tls_id": tls_obj["id"] if tls_obj else 0,
            "addrs": [{"server": domain, "server_port": spec["port"], "remark": ""}],
            "out_json": out,
        }
        # ⚠️ SS 的 method / password 必须放在入站**顶层**，
        #    面板服务端会自己把它们塞进 DB 的 options 列。
        #    传 {"options": {...}} 会直接被拒：json: unknown field "options"。
        if spec["type"] == "shadowsocks":
            inbound["method"] = spec["out_extra"]["method"]
            inbound["password"] = spec["out_extra"]["password"]

        if dry_run:
            print("[inbound] 将创建 %s (%s/%s)" % (tag, spec["type"], spec["port"]))
            continue

        api.save_inbound(inbound)
        print("[inbound] %s (%s/%s) 创建成功" % (tag, spec["type"], spec["port"]))

    # ---------------------------------------------------------- 3. 订阅用户
    ib_ids = [i["id"] for i in api.inbounds() if i["id"] > 0]
    me = next((c for c in api.clients() if c["name"] == sub_id), None)

    specs = node_specs(cfg)

    config = {
        "anytls": {"name": sub_id, "password": cfg.get("ANYTLS_PASSWORD") or gen_password()},
        "vless": {"name": sub_id,
                  "uuid": cfg.get("REALITY_UUID") or str(uuidlib.uuid4()),
                  "flow": "xtls-rprx-vision"},
        "hysteria2": {"name": sub_id, "password": cfg.get("HY2_PASSWORD") or gen_password()},
    }

    ss_spec = next((s for s in specs if s["type"] == "shadowsocks"), None)
    if ss_spec:
        method = ss_spec["out_extra"]["method"]
        psk = ss_spec["out_extra"]["password"]
        # 入站已存在时以服务端实际存下来的为准，否则重跑脚本会让客户端 psk 漂移
        live_ss = load_inbound_out(ss_spec["tag"])
        if live_ss:
            method = live_ss.get("method") or method
            psk = live_ss.get("password") or psk
        # ⚠️ SS2022 的 client config key 是 `shadowsocks16`，不是 `shadowsocks`。
        #    写成 shadowsocks 会让 UpdateInboundsUsers 里
        #    json_extract(config,'$.shadowsocks16') 拿到 NULL，报
        #    "converting NULL to string is unsupported"，整个核心直接起不来。
        key = "shadowsocks16" if str(method).startswith("2022-") else "shadowsocks"
        config[key] = {"name": sub_id, "password": psk}

    if any(s["type"] == "tuic" for s in specs):
        config["tuic"] = {"name": sub_id,
                          "uuid": cfg.get("TUIC_UUID") or str(uuidlib.uuid4()),
                          "password": cfg.get("TUIC_PASSWORD") or gen_password()}

    if me is None:
        payload = {
            "enable": True,
            "name": sub_id,
            "remark": "",          # 留空：节点名直接用 inbound tag，避免 "JP-JP-xxx"
            "desc": "", "group": "",
            "inbounds": ib_ids,
            "links": [],           # ⚠️ 必须显式给，见 sui_api.save_client
            "volume": 0, "expiry": 0,
            "config": config,
        }
        if dry_run:
            print("[client] 将创建订阅用户 %s" % sub_id)
        else:
            api.save_client(payload)
            print("[client] 订阅用户 %s 创建成功" % sub_id)
    else:
        old_ids = me.get("inbounds") or []
        if isinstance(old_ids, str):
            import json as _json
            old_ids = _json.loads(old_ids)
        if sorted(old_ids) == sorted(ib_ids):
            print("[client] %s 已存在且 inbounds 一致，跳过" % sub_id)
        else:
            print("[client] %s 已存在，同步 inbounds %s -> %s" % (sub_id, old_ids, ib_ids))
            if not dry_run:
                me["inbounds"] = ib_ids
                me.setdefault("links", [])
                # ⚠️ GET /api/clients 不返回 config 字段，直接回存会把凭据清空，
                #    随后 UpdateInboundsUsers 里 json_extract(config,'$.anytls')
                #    拿到 NULL，报 "converting NULL to string is unsupported"。
                #    所以从 DB 里把 config 读回来补上。
                if not me.get("config"):
                    me["config"] = load_client_config(me["id"])
                api.save_client(me)

    # ---------------------------------------------------------- 4. 汇总
    print("")
    print("=" * 62)
    print("节点凭据（务必记入交付文档）")
    print("=" * 62)
    for proto, cred in config.items():
        parts = ["%s=%s" % (k, v) for k, v in cred.items() if k != "name"]
        print("  %-10s %s" % (proto, "  ".join(parts)))
    print("")
    print("  Reality  SNI=%s  pbk=%s" % (sni, pub))
    print("  Reality  sid=%s" % sid)
    print("=" * 62)

    if not dry_run:
        verify_core(api)
    return 0


def verify_core(api) -> None:
    """回读 sing-box 实际运行的配置，确认 users 已注入。"""
    try:
        cfg = api.singbox_config()
    except Exception as exc:  # noqa: BLE001
        print("⚠️  回读 sing-box 配置失败：%s" % exc)
        return
    inbounds = cfg.get("inbounds") or []
    print("")
    print("sing-box 运行中的入站：")
    for ib in inbounds:
        users = ib.get("users") or []
        flag = "OK  " if users else "⚠️  无用户"
        print("  %s %-22s %-10s :%s  users=%d" % (
            flag, ib.get("tag", ""), ib.get("type", ""),
            ib.get("listen_port"), len(users)))
    if not inbounds:
        print("  ⚠️  读不到 inbounds，检查 journalctl -u s-ui")


# ---------------------------------------------------------------- 入口

def run_remote(cfg: dict, argv: list) -> int:
    """本地模式：把自己上传到 VPS，用 --local 执行。"""
    from ssh_run import get_client, put_text, run, env_prefix, fill_from_install_result

    cli = get_client(cfg)
    try:
        remote_dir = "/root/.sui-skill"
        # deploy.env 里 PANEL_PATH / SUB_PATH 留空时，从 VPS 的 install-result.env 补上
        fill_from_install_result(cli, cfg)
        run(cli, "mkdir -p " + remote_dir, timeout=20)
        for name in ("deploy_nodes.py", "sui_api.py", "ssh_run.py"):
            with open(os.path.join(HERE, name), "r", encoding="utf-8", newline="\n") as fh:
                put_text(cli, fh.read(), remote_dir + "/" + name)
        # 配置走环境变量，不落盘
        cmd = "cd %s && %s python3 deploy_nodes.py --local %s" % (
            remote_dir, env_prefix(cfg), " ".join(argv))
        rc, out, err = run(cli, cmd, timeout=300)
        sys.stdout.write(out)
        if err.strip():
            sys.stderr.write("\n[stderr]\n" + err)
        return rc
    finally:
        cli.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="在 S-UI 上创建四个协议节点")
    ap.add_argument("--local", action="store_true", help="已在 VPS 上，直接执行")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写入")
    args = ap.parse_args()

    on_vps = os.path.exists(SUI_DB)
    if not (args.local or on_vps):
        from ssh_run import load_env
        passthrough = [a for a in sys.argv[1:] if a != "--local"]
        return run_remote(load_env(), passthrough)

    from ssh_run import load_env
    cfg = load_env()
    if not cfg.get("DOMAIN"):
        sys.stderr.write("deploy.env 缺少 DOMAIN\n")
        return 2
    return deploy(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
