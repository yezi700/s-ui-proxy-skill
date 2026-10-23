#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""3x-ui 面板 API 客户端（库 + 命令行）。

关键设计：
  * 自动带 `Host: <域名>` 头 —— 面板启用 webDomain 后，缺这个头会返回 403 空响应
  * 自动带 `Authorization: Bearer <apiToken>`
  * 统一解析 {success, msg, obj}，失败时抛出带 msg 的异常
  * `settings` / `streamSettings` / `sniffing` 自动做 JSON 字符串 ↔ dict 转换

作为库使用：
    from xui_api import XuiApi, load_env
    api = XuiApi.from_env()
    inbounds = api.inbounds_list()

作为命令行使用：
    python xui_api.py list-inbounds
    python xui_api.py all-links
    python xui_api.py list-clients
    python xui_api.py get-settings
    python xui_api.py raw GET  /inbounds/list
    python xui_api.py raw POST /setting/all
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request

try:
    from ssh_run import load_env
except ImportError:  # 允许从其它目录导入
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ssh_run import load_env

JSON_STRING_FIELDS = ("settings", "streamSettings", "sniffing", "allocate")


class XuiError(RuntimeError):
    pass


class XuiApi:
    """3x-ui 面板 API 客户端。默认从 127.0.0.1 调用（需配合 SSH 端口转发或远端执行）。"""

    def __init__(self, base: str, token: str, host_header: str | None = None,
                 verify_tls: bool = False):
        """
        base         : 形如 https://127.0.0.1:46821/<面板路径>/panel/api
        token        : API Token
        host_header  : 面板 webDomain；非空时作为 Host 头发送（关键！）
        verify_tls   : 面板用自签/IP 证书时保持 False
        """
        self.base = base.rstrip("/")
        self.token = token
        self.host_header = host_header
        self.ctx = ssl.create_default_context()
        if not verify_tls:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_env(cls, cfg: dict | None = None) -> "XuiApi":
        cfg = cfg or load_env()
        host = cfg.get("VPS_HOST", "")
        domain = cfg.get("DOMAIN", "")
        port = cfg.get("PANEL_PORT", "46821")
        path = (cfg.get("PANEL_PATH") or "").strip("/")
        token = cfg.get("API_TOKEN", "")
        if not token:
            raise XuiError("deploy.env 里没有 API_TOKEN")

        # 面板路径前缀：优先用 PANEL_PATH
        base = f"https://{host or '127.0.0.1'}:{port}"
        if path:
            base += f"/{path}"
        base += "/panel/api"
        return cls(base, token, host_header=domain or None)

    # ------------------------------------------------------------ 底层请求

    def request(self, method: str, endpoint: str, body=None,
                timeout: int = 30, raw: bool = False):
        url = self.base + "/" + endpoint.lstrip("/")
        data = None
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json",
            "User-Agent": "xui-skill/1.0",
        }
        if self.host_header:
            headers["Host"] = self.host_header

        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
                text = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            status = e.code
            if status == 403 and not text.strip():
                raise XuiError(
                    "HTTP 403 且响应体为空。\n"
                    "  面板启用了 webDomain，请求必须带正确的 Host 头。\n"
                    f"  当前 Host 头: {self.host_header or '(未设置)'}\n"
                    "  检查 deploy.env 的 DOMAIN 是否与面板 webDomain 一致。"
                ) from e
            raise XuiError(f"HTTP {status}: {text[:500]}") from e
        except urllib.error.URLError as e:
            raise XuiError(f"连接失败（{url}）：{e.reason}") from e

        if raw:
            return status, text

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise XuiError(f"响应不是 JSON（HTTP {status}）：{text[:300]}")

        if isinstance(payload, dict) and payload.get("success") is False:
            raise XuiError(f"面板返回失败：{payload.get('msg') or payload}")
        return payload

    # ------------------------------------------------------------ 便捷方法

    def _obj(self, method: str, endpoint: str, body=None):
        return self.request(method, endpoint, body).get("obj")

    def inbounds_list(self) -> list:
        return self._obj("GET", "/inbounds/list") or []

    def inbound_get(self, iid: int) -> dict:
        return self._obj("GET", f"/inbounds/get/{iid}") or {}

    def inbound_add(self, payload: dict):
        return self.request("POST", "/inbounds/add", self._encode(payload))

    def inbound_update(self, iid: int, payload: dict):
        return self.request("POST", f"/inbounds/update/{iid}", self._encode(payload))

    def inbound_del(self, iid: int):
        return self.request("POST", f"/inbounds/del/{iid}")

    def all_links(self) -> list:
        return self._obj("GET", "/inbounds/allLinks") or []

    def clients_list(self) -> list:
        return self._obj("GET", "/clients/list") or []

    def client_add(self, client: dict, inbound_ids: list[int]):
        return self.request("POST", "/clients/add",
                            {"client": client, "inboundIds": inbound_ids})

    def client_update(self, email: str, client: dict):
        return self.request("POST", f"/clients/update/{email}", client)

    def client_del(self, email: str):
        return self.request("POST", f"/clients/del/{email}")

    def settings_all(self) -> dict:
        # ⚠️ 是 POST，不是 GET
        return self._obj("POST", "/setting/all") or {}

    def settings_update(self, full: dict):
        """⚠️ 整体覆盖：必须传入 settings_all() 的完整对象再改字段。"""
        return self.request("POST", "/setting/update", full)

    def set_share_addr(self, addr: str, strategy: str = "custom"):
        cur = self.settings_all()
        cur["shareAddrStrategy"] = strategy
        cur["shareAddr"] = addr
        return self.settings_update(cur)

    def restart_xray(self):
        return self.request("POST", "/setting/restartXrayService")

    def restart_panel(self):
        return self.request("POST", "/setting/restartPanel")

    # ------------------------------------------------------------ 工具

    @staticmethod
    def _encode(payload: dict) -> dict:
        """把 settings/streamSettings/sniffing 的 dict 转成 JSON 字符串。"""
        out = dict(payload)
        for key in JSON_STRING_FIELDS:
            if isinstance(out.get(key), (dict, list)):
                out[key] = json.dumps(out[key], ensure_ascii=False)
        return out

    @staticmethod
    def decode_inbound(inbound: dict) -> dict:
        """把入站里的 JSON 字符串字段还原成 dict，便于阅读。"""
        out = dict(inbound)
        for key in JSON_STRING_FIELDS:
            val = out.get(key)
            if isinstance(val, str) and val.strip():
                try:
                    out[key] = json.loads(val)
                except json.JSONDecodeError:
                    pass
        return out


# ---------------------------------------------------------------- CLI

def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description="3x-ui 面板 API 客户端")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-inbounds")
    sub.add_parser("list-clients")
    sub.add_parser("all-links")
    sub.add_parser("get-settings")

    p = sub.add_parser("get-inbound")
    p.add_argument("id", type=int)

    p = sub.add_parser("del-inbound",
                       help="删除入站（用数字 id，避开 Windows Git Bash 的路径转换）")
    p.add_argument("id", type=int)

    p = sub.add_parser("set-share-addr")
    p.add_argument("addr")

    p = sub.add_parser("raw",
                       help="直接调用任意端点。⚠️ Windows Git Bash 会把以 / 开头的参数"
                            "转成路径（/inbounds/del/4 → D:/.../inbounds/del/4）；"
                            "请改用专用子命令，或先设 MSYS_NO_PATHCONV=1")
    p.add_argument("method")
    p.add_argument("endpoint")
    p.add_argument("--body", help="JSON 字符串")

    args = ap.parse_args()
    api = XuiApi.from_env()

    if args.cmd == "list-inbounds":
        for ib in api.inbounds_list():
            d = XuiApi.decode_inbound(ib)
            print(f"#{d.get('id')}  {d.get('remark')}  "
                  f"{d.get('protocol')}/{d.get('port')}  "
                  f"enable={d.get('enable')}  "
                  f"clients={len((d.get('settings') or {}).get('clients', []))}")
    elif args.cmd == "list-clients":
        _print(api.clients_list())
    elif args.cmd == "all-links":
        for link in api.all_links():
            print(link.get("remark"), "->", link.get("uri"))
    elif args.cmd == "get-settings":
        _print(api.settings_all())
    elif args.cmd == "get-inbound":
        _print(XuiApi.decode_inbound(api.inbound_get(args.id)))
    elif args.cmd == "del-inbound":
        _print(api.inbound_del(args.id))
    elif args.cmd == "set-share-addr":
        _print(api.set_share_addr(args.addr))
    elif args.cmd == "raw":
        body = json.loads(args.body) if args.body else None
        _print(api.request(args.method, args.endpoint, body))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except XuiError as exc:
        sys.stderr.write(f"[XuiError] {exc}\n")
        raise SystemExit(1)
