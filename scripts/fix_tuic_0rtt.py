#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""关闭 TUIC 入站的 0-RTT，修复部分客户端"重连即认证失败"的问题。

为什么需要：
    3x-ui 的 TUIC 认证依赖 TLS Keying Material Exporter，而
    internal/tuic/auth.go 里有硬性检查 `if !cs.HandshakeComplete { ... }`。
    0-RTT 连接在认证时握手尚未完成 → 认证被拒 → 连接以 0x100 关闭。
    服务端 Allow0RTT=true 时，任何启用 0-RTT 的客户端（mihomo 的
    reduce-rtt、sing-box 的 zero_rtt_handshake，以及面板自己导出的
    Clash 配置默认 reduce-rtt: true）都会踩中。

用法：
    python scripts/fix_tuic_0rtt.py                 # 自动找第一个 TUIC 入站
    TUIC_INBOUND_ID=3 python scripts/fix_tuic_0rtt.py

⚠️ 备份默认写到 ~/.xui-skill/backup/，**故意不放在仓库目录内**——
   备份里含真实凭据（uuid / password），放进仓库有泄露风险。
   可用环境变量 BACKUP_DIR 覆盖。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xui_api import XuiApi, XuiError  # noqa: E402

BK_DIR = os.environ.get("BACKUP_DIR") or os.path.join(
    os.path.expanduser("~"), ".xui-skill", "backup"
)


def _settings_of(inbound: dict) -> dict:
    st = inbound.get("settings")
    if isinstance(st, str):
        st = json.loads(st) if st.strip() else {}
    return st or {}


def main() -> int:
    api = XuiApi.from_env()

    # 定位 TUIC 入站
    want = os.environ.get("TUIC_INBOUND_ID")
    target = None
    for ib in api.inbounds_list():
        if want and str(ib.get("id")) == str(want):
            target = ib
            break
        if not want and ib.get("protocol") == "tuic":
            target = ib
            break

    if target is None:
        print("!! 没找到 TUIC 入站。请检查 deploy.env 与面板配置。")
        return 1

    tid = int(target["id"])
    print(f"目标 TUIC 入站: #{tid}  {target.get('remark')}  port={target.get('port')}")

    raw = api.inbound_get(tid)
    if not raw:
        print(f"!! 拿不到 inbound {tid} 的完整对象")
        return 1

    # 本地备份（仓库目录之外）
    os.makedirs(BK_DIR, exist_ok=True)
    bk = os.path.join(BK_DIR, f"inbound-{tid}-before.json")
    with open(bk, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, ensure_ascii=False, indent=1)
    print(f"已备份到 {os.path.abspath(bk)}")

    ib = XuiApi.decode_inbound(raw)
    st = _settings_of(ib)
    srv = st.setdefault("server", {})
    before = srv.get("zero_rtt_handshake")

    if before is False:
        print("\nzero_rtt_handshake 已经是 false，无需修改。")
        return 0

    print("\n--- 修改前 settings.server ---")
    print(json.dumps(srv, ensure_ascii=False, indent=1))

    srv["zero_rtt_handshake"] = False
    # 顶层也写一份，兼容两种存放位置（InstanceFromInbound 优先读 server.*）
    st["zero_rtt_handshake"] = False
    ib["settings"] = st
    print(f"\nzero_rtt_handshake: {before} -> False")

    try:
        api.inbound_update(tid, ib)
        print("更新成功。")
    except XuiError as e:
        print("!! 更新失败:", e)
        return 1

    # 复核
    after = XuiApi.decode_inbound(api.inbound_get(tid))
    srv2 = _settings_of(after).get("server", {})
    print("\n--- 修改后 settings.server.zero_rtt_handshake ---")
    print(f"  {srv2.get('zero_rtt_handshake')!r}")

    print("\n--- 修改后分享链接 ---")
    for lk in api.all_links():
        if "tuic://" in lk:
            print(lk)

    print("\n提示：TUIC 监听由 x-ui 进程自行重启，无需手动 x-ui restart。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except XuiError as exc:
        sys.stderr.write(f"[XuiError] {exc}\n")
        raise SystemExit(1)
