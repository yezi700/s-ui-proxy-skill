#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端口体检：从外部探测 + 从服务端对照，判断「端口被封」还是「防火墙没放行」。

为什么需要对照：
    这两件事从外部看完全一样 —— 都是 TIMEOUT，但处理方式天差地别：
      端口被封       → 换端口（动配置，要重新拉订阅）
      防火墙没放行   → 补一条 iptables（几秒钟）
    只做外部探测会把后者误判成前者，白折腾一轮。

为什么需要这个脚本：
    端口不是"配好就永久有效"。实测同一台机器，2053 在 11 天后从 3/3 通
    变成 0/3 全超时，而 8443 一直正常。

用法（必须在本机跑，VPS 自测走 lo 不经过防火墙，结论无意义）：
    python scripts/check_ports.py              # 外部探测 + 服务端对照
    python scripts/check_ports.py --no-ssh     # 只做外部探测（不依赖 paramiko）
    python scripts/check_ports.py --json

诊断结论：
    正常            外部可连
    端口被封锁      服务端在听、防火墙也放行，但外部连不上 → 换端口
    防火墙未放行    服务端在听，但 iptables 没放行 → 补规则
    服务未监听      服务端没有进程在听 → 查 journalctl -u s-ui
    需客户端验证    UDP 端口，TCP 探测无意义
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ssh_run import load_env  # noqa: E402

# (配置键, 用途, 传输层)
PORT_SPECS = [
    ("PANEL_PORT", "面板", "tcp"),
    ("SUB_PORT", "订阅", "tcp"),
    ("ANYTLS_PORT", "AnyTLS", "tcp"),
    ("REALITY_PORT", "Reality", "tcp"),
    ("SS_PORT", "Shadowsocks", "tcp"),
    ("TUIC_PORT", "TUIC", "udp"),
    ("HY2_PORT", "Hysteria2", "udp"),
]

# 诊断结论
OK = "正常"
BLOCKED = "端口被封锁"
NO_FW = "防火墙未放行"
NO_LISTEN = "服务未监听"
UDP_ONLY = "需客户端验证"


def probe(host: str, port: int, timeout: float, retries: int) -> tuple:
    """TCP 探测。返回 (状态, 耗时毫秒)。状态: OPEN / REFUSED / TIMEOUT / ERROR"""
    last_err = "ERROR"
    for attempt in range(retries + 1):
        t0 = time.time()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            return "OPEN", int((time.time() - t0) * 1000)
        except socket.timeout:
            last_err = "TIMEOUT"
        except ConnectionRefusedError:
            return "REFUSED", int((time.time() - t0) * 1000)
        except OSError as exc:
            last_err = "ERROR:%s" % exc.errno
        finally:
            sock.close()
        if attempt < retries:
            time.sleep(1)
    return last_err, int(timeout * 1000)


# ---------------------------------------------------------------- 服务端对照

def parse_listen(block: str) -> set:
    """从 `ss -tlnH` / `ss -ulnH` 输出里提取监听端口。

    行形如 `LISTEN 0 4096 *:8443 *:*` / `[::]:2053` / `0.0.0.0:22`。
    """
    ports = set()
    for line in block.splitlines():
        for m in re.finditer(r"[:.](\d{1,5})\s", line):
            ports.add(int(m.group(1)))
    return ports


def parse_firewall(fw_text: str) -> tuple:
    """从 `iptables -S INPUT` 输出解析放行规则。

    返回 (allowed, allow_all)：
      allowed   {(proto, port), ...}
      allow_all 是否存在**无条件的** ACCEPT all（会架空 policy DROP）

    ⚠️ 判定 allow_all 必须看"去掉链名和动作后还剩什么"：
       `-A INPUT -i lo -j ACCEPT`              → 有 -i，不是无条件
       `-A INPUT -m conntrack --ctstate ... -j ACCEPT` → 有匹配，不是无条件
       `-A INPUT -j ACCEPT`                    → 光板规则，这才是问题
    """
    allowed = set()
    allow_all = False
    for line in fw_text.splitlines():
        if not line.startswith("-A INPUT") or "-j ACCEPT" not in line:
            continue
        cond = line.replace("-A INPUT", "").replace("-j ACCEPT", "").strip()
        if not cond:
            allow_all = True
            continue
        proto_m = re.search(r"-p (\w+)", line)
        port_m = re.search(r"--dport (\d+)", line)
        if proto_m and port_m:
            allowed.add((proto_m.group(1), int(port_m.group(1))))
    return allowed, allow_all


def collect_server(cfg: dict) -> dict | None:
    """通过 SSH 采集服务端的监听与防火墙状态。

    失败时返回 None，调用方降级为纯外部探测 —— 体检不该因为 SSH 不通就跑不了。
    """
    try:
        from ssh_run import get_client, run
    except Exception:  # noqa: BLE001
        return None
    try:
        cli = get_client(cfg)
    except (SystemExit, Exception):  # noqa: BLE001
        return None
    try:
        cmd = ("ss -tlnH 2>/dev/null; echo '@@@UDP'; ss -ulnH 2>/dev/null; "
               "echo '@@@FW'; iptables -S INPUT 2>/dev/null")
        _, out, _ = run(cli, cmd, timeout=30)
    except Exception:  # noqa: BLE001
        return None
    finally:
        cli.close()

    tcp_part, _, rest = out.partition("@@@UDP")
    udp_part, _, fw_part = rest.partition("@@@FW")

    allowed, allow_all = parse_firewall(fw_part)
    return {"tcp_listen": parse_listen(tcp_part),
            "udp_listen": parse_listen(udp_part),
            "allowed": allowed, "allow_all": allow_all}


def diagnose(row: dict, srv: dict | None) -> str:
    """给出结论。srv 为 None 时只按外部结果判断。"""
    if row["proto"] == "udp":
        if srv is None:
            return UDP_ONLY
        listening = row["port"] in srv["udp_listen"]
        fw = srv["allow_all"] or ("udp", row["port"]) in srv["allowed"]
        if not listening:
            return NO_LISTEN
        if not fw:
            return NO_FW
        return UDP_ONLY

    # TCP
    if row["status"] == "OPEN":
        return OK
    if srv is None:
        return BLOCKED if row["status"] == "TIMEOUT" else NO_LISTEN

    listening = row["port"] in srv["tcp_listen"]
    fw = srv["allow_all"] or ("tcp", row["port"]) in srv["allowed"]
    if not listening:
        return NO_LISTEN
    if not fw:
        return NO_FW
    return BLOCKED


def main() -> int:
    ap = argparse.ArgumentParser(description="端口体检：外部探测 + 服务端对照")
    ap.add_argument("--timeout", type=float, default=6.0, help="单次超时秒数")
    ap.add_argument("--retries", type=int, default=1, help="重试次数")
    ap.add_argument("--no-ssh", action="store_true", help="跳过服务端对照")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    cfg = load_env()
    host = cfg.get("SERVER_IP") or cfg.get("VPS_HOST")
    if not host:
        sys.stderr.write("deploy.env 缺少 SERVER_IP / VPS_HOST\n")
        return 2

    srv = None if args.no_ssh else collect_server(cfg)

    rows = []
    for key, label, proto in PORT_SPECS:
        raw = cfg.get(key)
        if not raw:
            continue
        try:
            port = int(str(raw).split()[0])
        except ValueError:
            continue
        if proto == "tcp":
            status, ms = probe(host, port, args.timeout, args.retries)
        else:
            status, ms = "—", 0
        row = {"key": key, "label": label, "port": port, "proto": proto,
               "status": status, "ms": ms}
        row["verdict"] = diagnose(row, srv)
        rows.append(row)

    if args.json:
        print(json.dumps({"host": host, "server_checked": srv is not None,
                          "ports": rows}, ensure_ascii=False, indent=2))
        return 0

    print("探测目标: %s" % host)
    if srv is None:
        print("服务端对照: 未启用（--no-ssh 或 SSH 不可用，结论只能区分「通/不通」）")
    else:
        note = "，⚠️ 存在无条件的 ACCEPT all（policy DROP 已失效）" if srv["allow_all"] else ""
        print("服务端对照: 已启用（监听 + 防火墙）%s" % note)
    print("")

    print("  %-11s %-6s %-5s %-9s %-6s %s" % ("用途", "端口", "传输", "外部", "耗时", "诊断"))
    print("  " + "-" * 68)
    for r in rows:
        ms = "%dms" % r["ms"] if r["proto"] == "tcp" and r["status"] == "OPEN" else "—"
        print("  %-11s %-6d %-5s %-9s %-6s %s" % (
            r["label"], r["port"], r["proto"].upper(), r["status"], ms, r["verdict"]))

    # ---------------------------------------------------------- 行动建议
    groups = {}
    for r in rows:
        groups.setdefault(r["verdict"], []).append(r)

    print("")
    if groups.get(BLOCKED):
        print("❌ 端口被封锁（服务端在听、防火墙也放行，但外部连不上）：")
        for r in groups[BLOCKED]:
            print("     %s %s/%d" % (r["label"], r["proto"], r["port"]))
        print("   → 换端口：python scripts/rotate_port.py --node %s"
              % groups[BLOCKED][0]["key"].replace("_PORT", "").lower())

    if groups.get(NO_FW):
        print("⚠️  防火墙没放行（服务在听，但 iptables 缺规则）：")
        for r in groups[NO_FW]:
            print("     %s %s/%d" % (r["label"], r["proto"], r["port"]))
        print("   → 补规则：iptables -I INPUT 1 -p %s --dport %d -j ACCEPT && "
              "iptables-save > /etc/iptables/rules.v4"
              % (groups[NO_FW][0]["proto"], groups[NO_FW][0]["port"]))
        print("   别急着换端口 —— 这个只要几秒钟就能修好。")

    if groups.get(NO_LISTEN):
        print("❌ 服务未监听：")
        for r in groups[NO_LISTEN]:
            print("     %s %s/%d" % (r["label"], r["proto"], r["port"]))
        print("   → journalctl -u s-ui -n 50 看核心是否起来了")

    if groups.get(UDP_ONLY):
        names = "、".join("%s/%d" % (r["label"], r["port"]) for r in groups[UDP_ONLY])
        print("ℹ️  UDP 端口（%s）TCP 探测无意义，需真实客户端拨号。" % names)
        print("   要量化吞吐用：python scripts/bench_nodes.py --sub <Clash订阅> --mihomo <路径>")

    healthy = groups.get(OK, [])
    if healthy and not (groups.get(BLOCKED) or groups.get(NO_FW) or groups.get(NO_LISTEN)):
        print("✅ 所有 TCP 端口可达，服务端监听与防火墙一致。")
        print("   注意：这只证明握手成功，不证明协议可用 ——")
        print("   Reality 认证失败、证书错误仍需真实客户端验证。")

    bad = groups.get(BLOCKED, []) + groups.get(NO_FW, []) + groups.get(NO_LISTEN, [])
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
