#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本机真机拨测：把订阅里的每个节点都跑一遍，量延迟和**持续**吞吐。

为什么不用 6 秒窗口：TCP 慢启动前几秒还没爬满窗口，6 秒窗口会把带宽
低估 3~4 倍（实测 25 Mbps vs 真实 78 Mbps），据此选节点会得出完全相反的结论。
所以默认拉 100MB 整包。

用法：
    # 1. 先准备一个 mihomo 可执行文件（任何版本都行）
    # 2. 直接拿 Clash 订阅地址开跑
    python scripts/bench_nodes.py \
        --sub "https://<域名>:2096/<subPath>/<subId>?format=clash" \
        --mihomo /path/to/mihomo

    # 只测延迟，不跑吞吐（快）
    python scripts/bench_nodes.py --sub ... --mihomo ... --size 0

    # 指定输出
    python scripts/bench_nodes.py ... --out measured.json --rounds 5

原理：不解析 YAML，直接让 mihomo 以 proxy-provider 方式加载订阅，
再通过它的 RESTful API（/proxies/GLOBAL）逐个切换出口 —— 这样节点名、
凭据、协议参数全都由 mihomo 自己处理，脚本不需要认识任何协议。

⚠️ 本脚本必须**直连**跑：如果本机设了 HTTP_PROXY/HTTPS_PROXY，
   mihomo 拉订阅会走代理，测出来的就不是这条线路了。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

CTRL = "http://127.0.0.1:9090"
LAT_URL = "http://www.gstatic.com/generate_204"
TP_URL = "https://proof.ovh.net/files/%dMb.dat"
NULL = os.devnull


def _api(path, data=None, method="GET", timeout=10):
    req = urllib.request.Request(CTRL + path,
                                 data=json.dumps(data).encode() if data else None,
                                 method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    return json.loads(body) if body else {}


def _curl(args, timeout=60):
    p = subprocess.run(["curl", "-s", "--noproxy", "", "-x", "http://127.0.0.1:7899"]
                       + args, capture_output=True, text=True, timeout=timeout)
    return p.stdout.strip()


def latency() -> int | None:
    out = _curl(["-o", NULL, "-w", "%{time_total}", "--max-time", "10", LAT_URL], 20)
    try:
        return round(float(out) * 1000)
    except ValueError:
        return None


def throughput(size_mb: int) -> float | None:
    out = _curl(["-o", NULL, "-w", "%{speed_download}",
                 "--max-time", str(max(30, size_mb * 3)), TP_URL % size_mb],
                timeout=max(40, size_mb * 3))
    try:
        return round(float(out) * 8 / 1e6, 1)
    except ValueError:
        return None


CONFIG_HEAD = """\
mixed-port: 7899
allow-lan: false
mode: global
log-level: error
ipv6: false
external-controller: 127.0.0.1:9090
"""


def fetch_proxies(sub_url: str) -> str:
    """下载 Clash 订阅，只抽出 `proxies:` 那一段。

    不引 PyYAML：订阅是 mihomo 生成的完整配置，只要顶层 `proxies:` 块即可，
    按行切比解析整个 YAML 稳（也更少依赖）。缩进 4 空格是 mihomo 的输出约定。
    """
    req = urllib.request.Request(sub_url, headers={"User-Agent": "clash.meta"})
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8", "replace")
    out, inside = [], False
    for line in text.splitlines():
        if not inside:
            if line.strip() == "proxies:":
                inside = True
                out.append("proxies:")
            continue
        if line and not line[0].isspace():   # 下一个顶层键，结束
            break
        out.append(line)
    if len(out) <= 1:
        raise RuntimeError("订阅里没找到 proxies: 段（确认地址带 ?format=clash）")
    return "\n".join(out) + "\n"


def wait_api(proc, seconds=25) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            _api("/version", timeout=2)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", required=True, help="Clash 格式订阅地址")
    ap.add_argument("--mihomo", required=True, help="mihomo 可执行文件路径")
    ap.add_argument("--rounds", type=int, default=3, help="延迟采样轮次（默认 3）")
    ap.add_argument("--size", type=int, default=100, help="吞吐测试文件大小 MB，0=跳过")
    ap.add_argument("--out", default="", help="结果写入的 JSON 路径")
    args = ap.parse_args()

    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)

    work = tempfile.mkdtemp(prefix="bench-")
    try:
        body = fetch_proxies(args.sub)
    except Exception as e:  # noqa: BLE001
        print("拉订阅失败：%s" % e, file=sys.stderr)
        shutil.rmtree(work, ignore_errors=True)
        return 1
    with open(os.path.join(work, "config.yaml"), "w", encoding="utf-8") as fh:
        fh.write(CONFIG_HEAD + body + "rules:\n  - MATCH,GLOBAL\n")

    proc = subprocess.Popen([args.mihomo, "-d", work, "-f",
                             os.path.join(work, "config.yaml")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_api(proc):
            print("mihomo 起不来（检查 --mihomo 路径，或订阅地址是否可达）", file=sys.stderr)
            return 1
        nodes = [n for n in (_api("/proxies/GLOBAL").get("all") or [])
                 if n not in ("DIRECT", "REJECT", "COMPATIBLE", "PASS", "GLOBAL")]
        if not nodes:
            print("订阅里没解析出节点", file=sys.stderr)
            return 1
        print("节点：%s\n" % ", ".join(nodes))

        res = {}
        for n in nodes:
            _api("/proxies/GLOBAL", {"name": n}, "PUT")
            latency()  # 预热，丢掉
            lat = []
            for _ in range(args.rounds):
                lat.append(latency())
            mb = throughput(args.size) if args.size else None
            res[n] = {"latency_ms": lat, "mbps": mb}
            ok = [x for x in lat if x]
            print("  %-28s 延迟 %-22s 吞吐 %s"
                  % (n, "/".join(str(x) for x in lat),
                     ("%.1f Mbps" % mb) if mb else "—"), flush=True)

        print()
        print("%-28s %-10s %-12s %s" % ("节点", "延迟中位", "持续吞吐", "延迟样本"))
        for n, r in res.items():
            lat = [x for x in r["latency_ms"] if x]
            med = sorted(lat)[len(lat) // 2] if lat else None
            print("%-28s %-10s %-12s %s"
                  % (n, ("%d ms" % med) if med else "—",
                     ("%.1f Mbps" % r["mbps"]) if r["mbps"] else "—",
                     "/".join(str(x) for x in lat)))

        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(res, fh, ensure_ascii=False, indent=2)
            print("\n已写入 %s" % args.out)
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
