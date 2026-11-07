#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S-UI 面板 API 客户端（库 + 命令行）。

设计要点（都是实测踩出来的）：

  * S-UI 有两套 API：
      - `/{webPath}/api/...`   前端 API，Cookie 认证，**必须带 Origin 头**，否则 403
      - `/{webPath}/apiv2/...` 外部 API，`Token` 头认证（Token 只能在面板里创建）
    本模块默认走前端 API + Cookie，因为它不需要事先创建 Token。

  * 登录表单字段是 `user` / `pass`（不是 username / password）。

  * 保存 clients 时 **必须显式带 `links:[]` 和 `inbounds`**，
    否则 Go 侧 `json.Unmarshal(nil, ...)` 会抛
    `save: unexpected end of JSON input` —— 这个报错完全误导，别去查 JSON 格式。
    本模块的 `save_client()` 会自动补齐。

  * 改 `webPort` / `webPath` / `webCertFile` 之后面板**不会自动重启**，
    必须 `systemctl restart s-ui`，否则新端口不生效（settings 已经写进 DB 了，容易误判）。

作为库使用：
    from sui_api import SuiApi
    api = SuiApi.from_env()
    api.login()
    print(api.inbounds())

作为命令行使用：
    python sui_api.py inbounds
    python sui_api.py clients
    python sui_api.py tls
    python sui_api.py settings
    python sui_api.py singbox-config
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    from ssh_run import load_env
except ImportError:  # 允许从其它目录导入
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ssh_run import load_env


class SuiError(RuntimeError):
    pass


class SuiApi:
    """S-UI 面板 API 客户端。默认指向 127.0.0.1（配合 SSH 转发或远端执行）。"""

    def __init__(self, base: str, verify_tls: bool = False):
        """
        base       : 形如 https://127.0.0.1:46821/<webPath>
        verify_tls : 面板用 IP / 自签访问时保持 False
        """
        self.base = base.rstrip("/")
        self.origin = self.base.rsplit("/", 1)[0] if "/" in self.base[len("https://"):] else self.base
        self.cj = http.cookiejar.CookieJar()
        self.ctx = ssl.create_default_context()
        if not verify_tls:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj),
            urllib.request.HTTPSHandler(context=self.ctx),
        )

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_env(cls, cfg: dict | None = None, host: str | None = None) -> "SuiApi":
        cfg = cfg or load_env()
        # 统一用域名：面板证书和 webDomain 都是按域名配的，
        # 连 127.0.0.1 会因 SNI 不匹配被拒（tls: unrecognized server name）。
        # 在 VPS 上访问自己的域名会走 lo，同样能通。
        host = host or cfg.get("DOMAIN") or cfg.get("VPS_HOST") or "127.0.0.1"
        port = cfg.get("PANEL_PORT") or "46821"
        path = (cfg.get("PANEL_PATH") or "").strip("/")
        scheme = "https" if cfg.get("PANEL_TLS", "1") not in ("0", "false", "no") else "http"
        base = f"{scheme}://{host}:{port}"
        if path:
            base += f"/{path}"
        return cls(base)

    # ------------------------------------------------------------ 底层请求

    def _request(self, method: str, endpoint: str, form: dict | None = None,
                 timeout: int = 30) -> dict:
        url = self.base + "/api/" + endpoint.lstrip("/")
        body = urllib.parse.urlencode(form).encode() if form else None
        req = urllib.request.Request(url, data=body, method=method.upper())
        # 缺 Origin 会被 CSRF 拦成 403 且响应体为空
        req.add_header("Origin", self.origin)
        req.add_header("Referer", self.base + "/")
        req.add_header("Accept", "application/json")
        if body:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code == 403 and not text.strip():
                raise SuiError(
                    "HTTP 403 且响应体为空：缺 Origin 头或 Origin 与面板地址不一致。\n"
                    f"  当前 Origin: {self.origin}"
                ) from e
            raise SuiError(f"HTTP {e.code}: {text[:400]}") from e
        except urllib.error.URLError as e:
            raise SuiError(f"连接失败（{url}）：{e.reason}") from e

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise SuiError(f"响应不是 JSON：{text[:300]}")
        if isinstance(payload, dict) and payload.get("success") is False:
            raise SuiError(payload.get("msg") or str(payload))
        return payload

    def _get(self, endpoint: str) -> dict:
        return self._request("GET", endpoint)

    def _obj(self, endpoint: str):
        return self._get(endpoint).get("obj")

    # ------------------------------------------------------------ 认证

    def login(self, user: str | None = None, password: str | None = None,
              cfg: dict | None = None) -> dict:
        cfg = cfg or load_env()
        user = user or cfg.get("PANEL_USER") or "admin"
        password = password or cfg.get("PANEL_PASS")
        if not password:
            raise SuiError("deploy.env 里没有 PANEL_PASS")
        r = self._request("POST", "login", {"user": user, "pass": password})
        if not r.get("success"):
            raise SuiError(f"登录失败：{r.get('msg')}")
        return r

    def change_pass(self, old: str, new_user: str, new_pass: str) -> dict:
        return self._request("POST", "changePass",
                             {"oldPass": old, "newUsername": new_user, "newPass": new_pass})

    # ------------------------------------------------------------ 读对象

    def inbounds(self, iid: int | None = None) -> list:
        q = f"?id={iid}" if iid else ""
        return (self._obj("inbounds" + q) or {}).get("inbounds") or []

    def clients(self, cid: int | None = None) -> list:
        q = f"?id={cid}" if cid else ""
        return (self._obj("clients" + q) or {}).get("clients") or []

    def tls(self) -> list:
        return (self._obj("tls") or {}).get("tls") or []

    def outbounds(self) -> list:
        return (self._obj("outbounds") or {}).get("outbounds") or []

    def settings(self) -> dict:
        return (self._obj("settings") or {}).get("settings") or {}

    def singbox_config(self) -> dict:
        """核心当前实际运行的配置（已合并 inbounds / outbounds / users）。

        返回结构随版本有差异：多数版本是 {"success":true,"obj":{...}}，
        也有直接返回配置本身的情况，两种都兼容。
        """
        payload = self._request("GET", "singbox-config")
        obj = payload.get("obj")
        if isinstance(obj, dict):
            return obj
        if "inbounds" in payload:
            return payload
        return {}

    def status(self, resource: str = "cpu,mem,net,sys,sbd") -> dict:
        return self._obj(f"status?r={resource}") or {}

    # ------------------------------------------------------------ 写对象

    def save(self, obj: str, action: str, data) -> dict:
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return self._request("POST", "save",
                             {"object": obj, "action": action, "data": payload})

    def save_tls(self, name: str, server: dict, client: dict, tls_id: int | None = None) -> dict:
        data = {"name": name, "server": server, "client": client}
        if tls_id:
            data["id"] = tls_id
        return self.save("tls", "edit" if tls_id else "new", data)

    def save_inbound(self, inbound: dict) -> dict:
        return self.save("inbounds", "edit" if inbound.get("id") else "new", inbound)

    def save_client(self, client: dict) -> dict:
        """保存客户端。自动补齐 S-UI 要求的 links / inbounds 字段。

        不补的话会报 `save: unexpected end of JSON input` —— 原因是
        service/client.go 对 nil 的 json.RawMessage 直接 Unmarshal。
        """
        data = dict(client)
        data.setdefault("links", [])
        data.setdefault("inbounds", [])
        data.setdefault("volume", 0)
        data.setdefault("expiry", 0)
        return self.save("clients", "edit" if data.get("id") else "new", data)

    def del_client(self, cid: int) -> dict:
        # 注意：del 的 data 是裸数字，不是 {"id": n}
        return self.save("clients", "del", cid)

    def del_inbound(self, iid: int) -> dict:
        return self.save("inbounds", "del", iid)

    def save_settings(self, kv: dict) -> dict:
        """settings 的值必须全是字符串（布尔也要写成 "true"/"false"）。"""
        return self.save("settings", "set", {k: str(v).lower() if isinstance(v, bool) else str(v)
                                             for k, v in kv.items()})

    def restart_core(self) -> dict:
        return self._request("POST", "restartSb", {})

    # ------------------------------------------------------------ 工具

    @staticmethod
    def inbound_users(inbound: dict) -> list:
        return inbound.get("users") or []


# ---------------------------------------------------------------- CLI

def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description="S-UI 面板 API 客户端")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("inbounds", "clients", "tls", "settings", "singbox-config", "status", "outbounds"):
        sub.add_parser(name)
    p = sub.add_parser("login", help="验证凭据是否可用")
    p.add_argument("--user")
    p.add_argument("--pass", dest="password")
    p = sub.add_parser("save", help="直接保存对象（data 为 JSON 字符串）")
    p.add_argument("object")
    p.add_argument("action")
    p.add_argument("data")
    args = ap.parse_args()

    api = SuiApi.from_env()
    api.login()

    if args.cmd == "inbounds":
        for ib in api.inbounds():
            print(f"#{ib.get('id')}  {ib.get('tag'):<24} {ib.get('type'):<10} "
                  f"port={ib.get('listen_port')}  tls_id={ib.get('tls_id')}  "
                  f"users={len(api.inbound_users(ib))}")
    elif args.cmd == "clients":
        for c in api.clients():
            print(f"#{c.get('id')}  {c.get('name'):<16} enable={c.get('enable')}  "
                  f"inbounds={c.get('inbounds')}")
    elif args.cmd == "tls":
        for t in api.tls():
            srv = t.get("server") or {}
            print(f"#{t.get('id')}  {t.get('name'):<28} "
                  f"sni={srv.get('server_name')}  reality={(srv.get('reality') or {}).get('enabled')}")
    elif args.cmd == "settings":
        _print(api.settings())
    elif args.cmd == "singbox-config":
        _print(api.singbox_config())
    elif args.cmd == "outbounds":
        _print(api.outbounds())
    elif args.cmd == "status":
        _print(api.status())
    elif args.cmd == "login":
        print("登录成功")
    elif args.cmd == "save":
        _print(api.save(args.object, args.action, args.data))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SuiError as exc:
        sys.stderr.write(f"[SuiError] {exc}\n")
        raise SystemExit(1)
