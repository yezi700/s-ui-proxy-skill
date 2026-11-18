#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从外部探测节点端口是否还活着。

为什么需要这个脚本：
    端口不是"配好就永久有效"。实测同一条线路，2053 端口在 11 天后从 3/3 通
    变成 0/3 全超时，而 8443 一直正常。所以需要定期体检，早发现早换。

用法（必须在本机跑，VPS 上自测走 lo 不经过防火墙，结论无意义）：
    python scripts/check_ports.py
    python scripts/check_ports.py --timeout 5 --retries 1
    python scripts/check_ports.py --json

判读：
    OPEN      TCP 三次握手成功 —— 端口至少没被完全阻断
    TIMEOUT   握手超时 —— 典型封锁特征，换端口
    REFUSED   连接被拒 —— 服务没监听（UDP 端口用 TCP 探测必然如此，正常）

⚠️ 本脚本只测 TCP。UDP 端口（TUIC / Hysteria2）的 TCP 探测必然 REFUSED，
   不代表节点挂了 —— 那是正常的。UDP 是否可用只能用真实客户端拨号验证。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ssh_run import load_env  # noqa: E402

# 端口 → (用途, 传输层, 是否可被 TCP 探测)
PORT_SPECS = [
    ("PANEL_PORT", "面板", "tcp"),
    ("SUB_PORT", "订阅", "tcp"),
    ("ANYTLS_PORT", "AnyTLS", "tcp"),
    ("REALITY_PORT", "Reality", "tcp"),
    ("SS_PORT", "Shadowsocks", "tcp"),
    ("TUIC_PORT", "TUIC", "udp"),
    ("HY2_PORT", "Hysteria2", "udp"),
]


def probe(host: str, port: int, timeout: float, retries: int) -> tuple:
    """返回 (状态, 耗时毫秒)。状态: OPEN / REFUSED / TIMEOUT / ERROR:<原因>"""
    last_err = ""
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
    return last_err or "ERROR", int(timeout * 1000)


def main() -> int:
    ap = argparse.ArgumentParser(description="外部探测节点端口可达性")
    ap.add_argument("--timeout", type=float, default=6.0, help="单次超时秒数")
    ap.add_argument("--retries", type=int, default=1, help="重试次数")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    cfg = load_env()
    host = cfg.get("SERVER_IP") or cfg.get("VPS_HOST")
    if not host:
        sys.stderr.write("deploy.env 缺少 SERVER_IP / VPS_HOST\n")
        return 2

    rows = []
    for key, label, proto in PORT_SPECS:
        raw = cfg.get(key)
        if not raw:
            continue
        try:
            port = int(str(raw).split()[0])
        except ValueError:
            continue
        status, ms = probe(host, port, args.timeout, args.retries)
        rows.append({"key": key, "label": label, "port": port,
                     "proto": proto, "status": status, "ms": ms})

    if args.json:
        print(json.dumps({"host": host, "ports": rows}, ensure_ascii=False, indent=2))
        return 0

    print("探测目标: %s" % host)
    print("")
    print("  %-12s %-8s %-6s %-10s %s" % ("用途", "端口", "传输", "状态", "说明"))
    print("  " + "-" * 62)

    bad_tcp = []
    for r in rows:
        if r["proto"] == "udp":
            note = "UDP 无法用 TCP 探测，需真实客户端验证"
            shown = "—"
        elif r["status"] == "OPEN":
            note = "%dms" % r["ms"]
            shown = "OPEN"
        elif r["status"] == "TIMEOUT":
            note = "握手超时 —— 典型封锁特征"
            shown = "TIMEOUT"
            bad_tcp.append(r)
        elif r["status"] == "REFUSED":
            note = "服务未监听"
            shown = "REFUSED"
            bad_tcp.append(r)
        else:
            note = r["status"]
            shown = "ERROR"
            bad_tcp.append(r)

        print("  %-12s %-8d %-6s %-10s %s" % (r["label"], r["port"], r["proto"].upper(), shown, note))

    print("")
    if bad_tcp:
        print("⚠️  以下 TCP 端口不可用：")
        for r in bad_tcp:
            print("     %s (%s/%d)" % (r["label"], r["proto"], r["port"]))
        print("")
        print("    处理：换一个端口重试")
        print("      python scripts/rotate_port.py --node reality")
        print("    换完再跑一次本脚本确认。")
    else:
        print("✅ 所有 TCP 端口可达。")
        print("   注意：这只证明 TCP 握手成功，不证明协议可用 ——")
        print("   Reality 认证失败、证书错误等问题仍需真实客户端拨号验证。")

    print("")
    print("   UDP 端口（TUIC / Hysteria2）请用 mihomo 或客户端实测，")
    print("   TCP 探测对 UDP 无效。")

    return 0 if not bad_tcp else 1


if __name__ == "__main__":
    raise SystemExit(main())
