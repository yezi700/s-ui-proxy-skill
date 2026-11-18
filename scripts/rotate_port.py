#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给某个节点换端口。

端口会随时间被逐步封锁（实测 2053 在 11 天后从 3/3 通变成 0/3 全超时），
所以需要一条低成本的轮换路径，而不是每次手动改面板 + 改防火墙。

用法：
    python scripts/rotate_port.py --list                 # 看候选端口
    python scripts/rotate_port.py --node reality         # 自动挑一个可用端口
    python scripts/rotate_port.py --node reality --port 2087
    python scripts/rotate_port.py --node anytls --dry-run

做的事：
    1. 改入站的 listen_port / tag / addrs / out_json.server_port
    2. 放行新端口并持久化 iptables
    3. 确认 sing-box 已在新端口监听
    4. 提示重新拉订阅

⚠️ 旧端口的 iptables 规则不会自动删（保留一段时间方便回退），
   确认新端口稳定后可手动清理。
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from sui_api import SuiApi, SuiError  # noqa: E402
from ssh_run import get_client, load_env, run  # noqa: E402

# 节点别名 → (配置键, 传输层, 入站 type)
NODE_MAP = {
    "anytls": ("ANYTLS_PORT", "tcp", "anytls"),
    "hysteria2": ("HY2_PORT", "udp", "hysteria2"),
    "hy2": ("HY2_PORT", "udp", "hysteria2"),
    "reality": ("REALITY_PORT", "tcp", "vless"),
    "vless": ("REALITY_PORT", "tcp", "vless"),
    "shadowsocks": ("SS_PORT", "tcp", "shadowsocks"),
    "ss": ("SS_PORT", "tcp", "shadowsocks"),
    "tuic": ("TUIC_PORT", "udp", "tuic"),
}

# 候选端口池：全是各家的标准 HTTPS/QUIC 端口，比野鸡高位端口更不显眼。
# 443 故意不列 —— 实测被针对性干扰。
TCP_POOL = [2087, 2053, 2083, 2095, 2096, 8080, 8880, 8443]
UDP_POOL = [2087, 2053, 2083, 8443, 9443]


def dead_ports(cfg: dict) -> set:
    """deploy.env 的 DEAD_PORTS 里记录已确认失效的端口，自动挑端口时跳过。

    为什么需要：端口被封后服务端仍然正常监听，外部探测无法区分
    "被封" 和 "没监听"，所以没法自动判断哪些端口已经废了，只能人工记账。
    """
    raw = cfg.get("DEAD_PORTS") or ""
    out = set()
    for item in raw.replace(" ", "").split(","):
        if item.isdigit():
            out.add(int(item))
    return out


def pick_port(api, proto: str, cfg: dict) -> int:
    """挑一个既没被本机占用、也没被现有入站使用、且不在失效名单里的端口。"""
    used = set()
    for ib in api.inbounds():
        used.add(int(ib["listen_port"]))
    for key in ("PANEL_PORT", "SUB_PORT"):
        raw = cfg.get(key)
        if raw:
            used.add(int(str(raw).split()[0]))
    dead = dead_ports(cfg)

    pool = TCP_POOL if proto == "tcp" else UDP_POOL
    for port in pool:
        if port not in used and port not in dead:
            return port
    raise SuiError("候选端口池全部被占用或已失效，请用 --port 手动指定")


def apply_firewall(cli, port: int, proto: str, dry_run: bool) -> None:
    if dry_run:
        print("  [dry-run] 将放行 %s/%d" % (proto, port))
        return
    check = 'iptables -C INPUT -p %s --dport %d -j ACCEPT' % (proto, port)
    rc, _, _ = run(cli, check, timeout=20)
    if rc == 0:
        print("  iptables: %s/%d 已存在" % (proto, port))
    else:
        run(cli, 'iptables -I INPUT 1 -p %s --dport %d -j ACCEPT' % (proto, port), timeout=20)
        print("  iptables: 已放行 %s/%d" % (proto, port))
    run(cli, "iptables-save > /etc/iptables/rules.v4", timeout=20)
    print("  iptables: 规则已持久化")


def drop_firewall(cli, port: int, proto: str) -> None:
    """删掉旧端口的放行规则。可能有重复添加的多条，循环删干净。"""
    removed = 0
    for _ in range(10):
        rc, _, _ = run(cli, 'iptables -D INPUT -p %s --dport %d -j ACCEPT' % (proto, port),
                       timeout=20)
        if rc != 0:
            break
        removed += 1
    if removed:
        run(cli, "iptables-save > /etc/iptables/rules.v4", timeout=20)
        print("  iptables: 已删除旧端口 %s/%d 的 %d 条规则" % (proto, port, removed))
    else:
        print("  iptables: 旧端口 %s/%d 无规则可删" % (proto, port))


def verify_from_outside(host: str, port: int, proto: str, timeout: float = 8.0):
    """从本机验证新端口。UDP 测不了，返回 None。"""
    if proto != "tcp":
        return None
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        sock.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="给 S-UI 节点换端口")
    ap.add_argument("--node", help="anytls / hysteria2 / reality / shadowsocks / tuic")
    ap.add_argument("--port", type=int, help="指定端口；不给则自动挑")
    ap.add_argument("--list", action="store_true", help="列出候选端口")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不改动")
    ap.add_argument("--cleanup-old", action="store_true",
                    help="换完后删掉旧端口的 iptables 规则（默认保留，方便回退）")
    args = ap.parse_args()

    if args.list:
        print("TCP 候选:", ", ".join(str(p) for p in TCP_POOL))
        print("UDP 候选:", ", ".join(str(p) for p in UDP_POOL))
        print("(443 不在池中 —— 实测被针对性干扰)")
        return 0

    if not args.node:
        ap.error("需要 --node，或使用 --list")

    key = args.node.lower()
    if key not in NODE_MAP:
        ap.error("--node 只能是 %s" % " / ".join(sorted(set(NODE_MAP))))

    cfg_key, proto, ib_type = NODE_MAP[key]
    cfg = load_env()
    api = SuiApi.from_env(cfg)
    api.login()

    # 找到目标入站。必须按 id 单条查：全量列表会裁掉 out_json（见 pitfalls §3.10）
    target = None
    for summary in api.inbounds():
        if summary["type"] != ib_type:
            continue
        target = api.inbounds(summary["id"])[0]
        break
    if target is None:
        sys.stderr.write("找不到类型为 %s 的入站（该节点可能没部署）\n" % ib_type)
        return 1

    old = int(target["listen_port"])
    new = args.port or pick_port(api, proto, cfg)
    if new == old:
        sys.stderr.write("新端口与当前端口相同（%d），请换一个\n" % new)
        return 1

    print("节点 %s: %d -> %d (%s)" % (target["tag"], old, new, proto))

    if not args.dry_run:
        target["listen_port"] = new
        target["tag"] = target["tag"].replace(str(old), str(new))
        target["addrs"] = [{"server": cfg["DOMAIN"], "server_port": new, "remark": ""}]
        if isinstance(target.get("out_json"), dict):
            target["out_json"]["server_port"] = new
            target["out_json"]["tag"] = target["tag"]
        api.save_inbound(target)
        print("  面板: 入站已更新")

    cli = get_client(cfg)
    try:
        apply_firewall(cli, new, proto, args.dry_run)

        if not args.dry_run:
            import time
            time.sleep(4)
            rc, out, _ = run(cli, 'ss -lntup 2>/dev/null | grep ":%d "' % new, timeout=20)
            if out.strip():
                print("  核心: 已在 :%d 监听" % new)
            else:
                print("  ⚠️  核心未在 :%d 监听，看 journalctl -u s-ui -n 30" % new)

            # 从本机验证 —— 服务端在听不等于外部连得上（防火墙、端口封锁都可能拦在外面）
            host = cfg.get("SERVER_IP") or cfg.get("VPS_HOST")
            ok = verify_from_outside(host, new, proto) if host else None
            if ok is True:
                print("  外部: %s:%d 可连 ✅" % (host, new))
            elif ok is False:
                print("  ❌ 外部: %s:%d 连不上 —— 可能新端口也被封，或防火墙没生效" % (host, new))
                print("     先跑 check_ports.py 看服务端对照，再决定要不要再换一个")
            else:
                print("  外部: UDP 端口无法从本机验证，需真实客户端拨号")

            if args.cleanup_old:
                drop_firewall(cli, old, proto)
    finally:
        cli.close()

    print("")
    print("下一步：")
    print("  1. 重新拉订阅（客户端会自动拿到新端口）")
    if proto == "tcp":
        print("  2. 复核：python scripts/check_ports.py")
    else:
        print("  2. UDP 需真实客户端验证：python scripts/bench_nodes.py --sub <Clash订阅> --mihomo <路径>")
    print("  3. 把旧端口记进 deploy.env 的 DEAD_PORTS，避免下次又挑到它：")
    print("     DEAD_PORTS=%s" % ",".join(str(p) for p in sorted(dead_ports(cfg) | {old})))
    if not args.cleanup_old:
        print("  4. 旧端口的 iptables 规则还在（方便回退）。稳定后可删，或换端口时加 --cleanup-old：")
        print("     iptables -D INPUT -p %s --dport %d -j ACCEPT" % (proto, old))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SuiError as exc:
        sys.stderr.write("[SuiError] %s\n" % exc)
        raise SystemExit(1)
