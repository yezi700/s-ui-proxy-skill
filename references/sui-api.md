# S-UI 面板 API 参考

> 基于 S-UI v1.6.7 实测。字段结构随版本可能变化，改代码前先 `GET` 一次确认。

## 一、两套 API

S-UI 有两套并行的 HTTP API，路径都在 `webPath` 之下：

| | 前端 API | 外部 API |
|---|---|---|
| 基础路径 | `/{webPath}/api/...` | `/{webPath}/apiv2/...` |
| 认证方式 | 会话 Cookie | `Token` 请求头 |
| Token 获取 | 登录即可 | **只能在面板 Admin 页手动创建** |
| 同源限制 | 有（必须带 `Origin`） | 无 |
| 适用场景 | 脚本自动化 | 长期集成、外部系统 |

`apiv2` **没有**"用户名密码换 Token"的接口 —— 管理 Token 的端点本身在 `/api` 分组下，
外部程序调不了。所以自动化流程用前端 API 更省事。

失败响应**仍返回 HTTP 200**，必须检查响应体里的 `success` 字段：

```json
{"success": false, "msg": "错误原因", "obj": null}
```

## 二、认证

### 2.1 登录

```bash
curl -sk -c cookies.txt -X POST \
  "https://<域名>:<端口>/<webPath>/api/login" \
  -H "Origin: https://<域名>:<端口>" \
  --data-urlencode "user=admin" \
  --data-urlencode "pass=<密码>"
```

- 字段是 `user` / `pass`，不是 `username` / `password`
- **必须带 `Origin`**，值与访问地址完全一致（含 scheme 和端口）
- 缺 `Origin` 返回 **403 且响应体为空**

### 2.2 改密码

```bash
curl -sk -b cookies.txt -X POST \
  "https://<域名>:<端口>/<webPath>/api/changePass" \
  -H "Origin: https://<域名>:<端口>" \
  --data-urlencode "oldPass=admin" \
  --data-urlencode "newUsername=admin" \
  --data-urlencode "newPass=<新密码>"
```

密码存 bcrypt（`$2a$10$...`）。

### 2.3 登录限速

同一来源连续失败 10 次会锁定 10 分钟（v1.6.1 起）。写脚本时别在循环里硬试。

## 三、读对象

| 端点 | 说明 | 返回 |
|---|---|---|
| `GET /api/inbounds` | 入站列表 | `{obj:{inbounds:[...]}}` |
| `GET /api/inbounds?id=N` | 单个入站 | 同上 |
| `GET /api/clients` | 客户端列表 | `{obj:{clients:[...]}}` |
| `GET /api/tls` | TLS 对象 | `{obj:{tls:[...]}}` |
| `GET /api/outbounds` | 出站 | `{obj:{outbounds:[...]}}` |
| `GET /api/settings` | 面板设置 | `{obj:{settings:{...}}}` |
| `GET /api/singbox-config` | 核心**实际运行**的配置 | 见下 |
| `GET /api/status?r=cpu,mem,sbd` | 系统与核心状态 | — |

`singbox-config` 返回结构随版本有差异，两种都要兼容：

```python
obj = payload.get("obj")
cfg = obj if isinstance(obj, dict) else (payload if "inbounds" in payload else {})
```

它是**回读校验的关键** —— 只有这里能看到 `inbounds[].users` 是否真的注入了。

## 四、写对象

统一入口：

```bash
POST /api/save
  object = clients | inbounds | tls | outbounds | endpoints | services | config | settings
  action = new | edit | del
  data   = <JSON 字符串>
```

⚠️ `del` 的 `data` 格式**按对象不同而不同**，写错会静默失败，见 §4.5。

### 4.1 保存客户端

```json
{
  "enable": true,
  "name": "us",              // 订阅 ID，URL 最后一段，必须唯一
  "remark": "",              // 空字符串 → 节点名直接用 inbound tag
  "inbounds": [1, 2, 3, 4],
  "links": [],               // ⚠️ 必须显式给
  "volume": 0,               // 字节，0 = 不限
  "expiry": 0,               // Unix 秒，0 = 不过期
  "config": {
    "anytls":        {"name": "us", "password": "..."},
    "vless":         {"name": "us", "uuid": "...", "flow": "xtls-rprx-vision"},
    "hysteria2":     {"name": "us", "password": "..."},
    "shadowsocks16": {"name": "us", "password": "<psk>"}
    // TUIC 启用时才有： "tuic": {"name": "us", "uuid": "...", "password": "..."}
  }
}
```

三个必须注意的点：

1. **`links` 不能省**。省了报 `save: unexpected end of JSON input` ——
   实际是 Go 侧对 nil 的 `json.RawMessage` 做 `Unmarshal`，报错信息完全误导。
2. **`inbounds` 不能省**，原因同上。
3. **`GET /api/clients` 不返回 `config`**。edit 前必须从 DB 补全，
   否则凭据被清空，随后报 `converting NULL to string is unsupported`。

`config` 的键**大部分**是协议名，但有例外 —— 这是最容易踩的地方：

| 入站类型 | config 的键 | 值 |
|---|---|---|
| `anytls` | `anytls` | `{name, password}` |
| `vless` | `vless` | `{name, uuid, flow}` |
| `hysteria2` | `hysteria2` | `{name, password}` |
| `tuic` | `tuic` | `{name, uuid, password}` |
| `shadowsocks`（2022 系列） | **`shadowsocks16`** | `{name, password}` |
| `shadowsocks`（老 AEAD） | `shadowsocks` | `{name, password}` |

SS2022 写成 `shadowsocks` 会让 `UpdateInboundsUsers` 里的
`json_extract(config,'$.shadowsocks16')` 拿到 NULL，报
`converting NULL to string is unsupported`，**整个核心起不来**（§6.9.1）。

一个客户端身份可以同时持有多种协议凭据，这正是"一条订阅出全部节点"的实现方式 ——
不需要任何合并脚本。反过来，**删了入站必须同步删这里的键**，否则订阅会继续生成
死节点（§3.11）。

### 4.2 保存入站

```json
{
  "type": "anytls",
  "tag": "US-AnyTLS-8443",     // 唯一，也是用户看到的节点名
  "listen": "::",
  "listen_port": 8443,
  "tls_id": 1,
  "addrs": [{"server": "<域名>", "server_port": 8443, "remark": ""}],
  "out_json": {
    "type": "anytls",
    "server": "<域名>",
    "server_port": 8443,
    "tls": {"enabled": true, "server_name": "<域名>", "insecure": false}
  }
}
```

- 除 `id` / `type` / `tag` / `tls_id` / `addrs` / `out_json` 外，**其余字段原样透传给 sing-box**，
  面板不做白名单校验 —— 写错字段不会提示，要等核心重启失败才发现。
- `addrs` 里每一项会生成一个独立节点，`server` / `server_port` 覆盖 `out_json` 里的同名字段。
  面板内部会把 `out_json.server` 改成 `127.0.0.1` 占位，**真正对外的是 `addrs`**。
- 新增入站后 sing-box 会自动热加载，日志出现
  `inbound/xxx[tag] tcp server started at [::]:port` 即成功。

### 4.3 保存 TLS

```json
{
  "name": "<域名>",
  "server": {
    "enabled": true,
    "server_name": "<域名>",
    "certificate_path": "/root/cert/<域名>/fullchain.pem",
    "key_path": "/root/cert/<域名>/privkey.pem"
  },
  "client": {"enabled": true, "server_name": "<域名>", "insecure": false}
}
```

三种典型形态：

| 用途 | 关键差异 |
|---|---|
| 域名证书（AnyTLS / Hysteria2） | 就是上面这样 |
| TUIC（可选） | `server.alpn = ["h3"]`、`client.alpn = ["h3"]`，**必须独立对象** |
| Reality | `server.reality = {enabled, handshake, private_key, short_id}`，`client.reality = {enabled, public_key, short_id}` |
| Shadowsocks | **不需要 TLS 对象**，入站 `tls_id` 填 `0` |

Reality 完整结构：

```json
{
  "name": "reality-<域名>",
  "server": {
    "enabled": true,
    "server_name": "www.apple.com",
    "reality": {
      "enabled": true,
      "handshake": {"server": "www.apple.com", "server_port": 443},
      "private_key": "<43 字符 base64url>",
      "short_id": ["<16 位 hex>"]
    }
  },
  "client": {
    "enabled": true,
    "server_name": "www.apple.com",
    "utls": {"enabled": true, "fingerprint": "chrome"},
    "reality": {"enabled": true, "public_key": "<43 字符>", "short_id": "<16 位 hex>"}
  }
}
```

### 4.4 保存设置

```json
{"webPort": "46821", "subPort": "2096", "subShowInfo": "true"}
```

- **所有值都是字符串**，布尔写 `"true"` / `"false"`
- 改 `webPort` / `webPath` / `webCertFile` / `webKeyFile` 后**必须手动
  `systemctl restart s-ui`**，不会自动生效

常用设置项：

| 键 | 说明 |
|---|---|
| `webPort` / `webPath` | 面板端口 / 路径 |
| `webCertFile` / `webKeyFile` | 面板 HTTPS 证书 |
| `webDomain` | 面板域名（影响 SNI 校验） |
| `subPort` / `subPath` | 订阅端口 / 路径 |
| `subCertFile` / `subKeyFile` | 订阅 HTTPS 证书 |
| `subShowInfo` | 订阅响应头带流量信息 |
| `subClashUdp` | Clash 输出给 UDP 节点补 `udp: true` |
| `subEncode` | 通用订阅返回 base64 |
| `subRemarkPrefix` | 节点名是否拼 client remark（建议 `false`） |
| `timeLocation` | 时区，默认 `Asia/Tehran`，改 `Asia/Shanghai` |

### 4.5 删除对象（clients 和 inbounds 格式不同）

**clients —— 裸数字 id：**

```bash
--data-urlencode "object=clients" --data-urlencode "action=del" --data-urlencode "data=7"
```

传 `{"id":7}` 报 `cannot unmarshal object into Go value of type uint`。

**inbounds —— tag 的 JSON 字符串字面量（带引号）：**

```bash
--data-urlencode "object=inbounds" --data-urlencode "action=del" \
  --data-urlencode 'data="US-Shadowsocks-2087"'
```

四种写法在 1.6.7 上逐个试过，只有最后一种真的删：

| 写法 | 结果 |
|---|---|
| `data=6` | `json: cannot unmarshal number into Go value of type string` |
| `data={"id":6}` | `json: cannot unmarshal object into Go value of type string` |
| `data="6"`（id 的字符串） | **`success:true`，但什么都没删** ← 最坑 |
| `data="<tag>"` | 真的删掉了 |

最后那种「静默成功」最要命：脚本以为删了，订阅里还挂着，重启后它又活了。
`sui_api.del_inbound(tag)` 已封装成正确形式，**别传 id**。

删完还要清 `clients.config` 里对应的协议键，否则订阅继续生成死节点（§3.11）。
删 TLS 对象前先确认没有入站引用它。

## 五、订阅服务

三条地址，同一个客户端身份：

```
https://<域名>:<subPort>/<subPath>/<subId>                     → base64 链接列表
https://<域名>:<subPort>/<subPath>/<subId>?format=clash        → Clash YAML
https://<域名>:<subPort>/<subPath>/<subId>?format=json         → sing-box JSON
```

响应头带流量信息（`subShowInfo=true` 时）：

```
Subscription-Userinfo: upload=8832; download=42625; total=0; expire=0
```

## 六、数据库

`/usr/local/s-ui/db/s-ui.db`（SQLite），主要表：

| 表 | 结构 |
|---|---|
| `settings` | `(id, key, value)`，纯 key/value，**无 UNIQUE 约束** |
| `users` | `(id, username, password)`，密码 bcrypt |
| `clients` | `(id, enable, name, config, inbounds, links, volume, expiry, ...)`，JSON 存 BLOB |
| `inbounds` | `(id, type, tag, tls_id, addrs, out_json, options)` |
| `tls` | `(id, name, server, client)` |

直接改库时注意：

- 改 `settings` 用 `UPDATE` + `rowcount==0` 再 `INSERT`，不要用 `ON CONFLICT`
- 改完要 `systemctl restart s-ui`（或至少触发一次保存）
- 备份：`cp db/s-ui.db /root/s-ui.db.bak.$(date +%Y%m%d-%H%M)`
- **`addrs` / `out_json` / `options` 是 BLOB 列**，`sqlite3` 取出来是 `bytes`，
  必须 `.decode()` 再 `json.loads`（否则 `dict(raw)` 报
  `cannot convert dictionary update sequence element #0 to a sequence`）
- **`inbounds` 表没有 `listen_port` 列** —— 端口在 `options` 里：
  `json_extract(options, '$.listen_port')`。而 `/api/inbounds` 返回的 JSON 里
  `listen_port` 是顶层字段，两处不一样，别混
- 改端口要改三处：`options.listen_port`、`out_json.server_port`、
  `addrs[0].server_port`，缺一处监听器不会动（§3.13）
- **协议专属参数（如 SS 的 `method` / `password`）不在 `options` 里传** ——
  它们走入站顶层，服务端自己序列化进 `options` 列。传 `{"options": {...}}` 会被拒：
  `json: unknown field "options"`（§3.10）

## 七、curl 快速上手

```bash
HOST=<域名>; PORT=46821; WP=<webPath>; PASS=<密码>
BASE="https://${HOST}:${PORT}/${WP}"
RESOLVE="--resolve ${HOST}:${PORT}:127.0.0.1"

# 登录
curl -sk $RESOLVE -c /tmp/ck -X POST "$BASE/api/login" \
  -H "Origin: https://${HOST}:${PORT}" \
  --data-urlencode "user=admin" --data-urlencode "pass=${PASS}"

# 看入站
curl -sk $RESOLVE -b /tmp/ck "$BASE/api/inbounds" | python3 -m json.tool

# 看核心实际配置（含 users）
curl -sk $RESOLVE -b /tmp/ck "$BASE/api/singbox-config" | python3 -m json.tool

# 保存设置
curl -sk $RESOLVE -b /tmp/ck -X POST "$BASE/api/save" \
  -H "Origin: https://${HOST}:${PORT}" \
  --data-urlencode "object=settings" \
  --data-urlencode 'data={"subShowInfo":"true"}'
```

`--resolve` 不能省 —— 面板证书按域名签发，直接连 IP 会因 SNI 不匹配被拒
（`tls: unrecognized server name`）。
