# 3x-ui 面板 API 参考

> 基于 **3x-ui v3.9.0**（内置 Xray-core 26.9.30）实测整理。不同版本字段可能有差异，
> 升级后请以 `internal/web/controller/*.go` 与 `internal/web/service/*.go` 为准。

---

## 1. 鉴权与调用前提

### 1.1 两种鉴权方式

| 方式 | 请求头 | 用途 |
|---|---|---|
| 会话 | `Cookie: 3x-ui=<session>` | 浏览器登录后自动带 |
| **API Token** | `Authorization: Bearer <apiToken>` | 脚本首选，不会过期 |

API Token 在面板 **设置 → 安全 → API Token** 里新建/重置。

⚠️ **v3.9.0 起不能从数据库直接读明文**：Token 挪到了独立的 `api_tokens` 表，
并且**只存 SHA-256 哈希**，明文**只在创建那一刻返回一次**。
老教程里的 `sqlite3 ... where key='apiToken'` 在 v3.9.0 上返回空。

获取方式（登录会话 + 创建）：

```bash
BASE="https://127.0.0.1:${PANEL_PORT}/${PANEL_PATH}"
JAR="$(mktemp)"

# ① CSRF Token
CSRF="$(curl -sk -c "$JAR" "$BASE/csrf-token" \
        | python3 -c 'import json,sys;print(json.load(sys.stdin)["obj"])')"

# ② 登录（Cookie 落 $JAR）
curl -sk -b "$JAR" -c "$JAR" -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "username=${PANEL_USER}" \
  --data-urlencode "password=${PANEL_PASS}" "$BASE/login" >/dev/null

# ③ 新建 Token，明文在返回体的 obj 里 —— 立刻保存
curl -sk -b "$JAR" -c "$JAR" -X POST \
  -H "X-CSRF-Token: $CSRF" -H "Content-Type: application/json" \
  -d '{"name":"skill"}' "$BASE/panel/api/setting/apiTokens/create"
# → {"success":true,"obj":"<明文 Token>"}   ← 只出现这一次
```

⚠️ ②③ 都要带 `X-CSRF-Token` 头，否则 403。
⚠️ Token 忘了就**重新建一个**，别想着从库里捞回来。
详见 `pitfalls.md` §2.7。

### 1.2 ⚠️ 最大的坑：`webDomain` 启用后的 Host 头校验

只要面板设置里 `webDomain` 非空，`DomainValidatorMiddleware` 就会**校验请求的 Host 头**：

- Host 头 == 配置的域名 → 放行
- 否则 → **HTTP 403，且响应体为空**（不是 JSON 错误，就是空）

后果：从 VPS 本机 `curl https://127.0.0.1:46821/...` 会拿到 403 空响应，
极易误判成"API 坏了 / Token 错了 / 面板挂了"。

**正确做法**——脚本里调 API 一律显式带 Host 头：

```bash
curl -sS -k \
  -H "Host: jp.example.com" \
  -H "Authorization: Bearer $API_TOKEN" \
  "https://127.0.0.1:46821/panel/api/inbounds/list"
```

用域名直连（`https://jp.example.com:46821/...`）当然也行，但会绕一圈公网 DNS + 回源，
在面板还没放行公网或 DNS 未生效时会失败，本机调用推荐走 127.0.0.1 + Host 头。

> 另一个副作用：启用 `webDomain` 后面板**只能通过域名访问**，用 IP 访问会 403。
> 这是设计如此（防 IP 扫描），不要当成 bug 去修。

### 1.3 返回体约定

```json
{ "success": true,  "msg": "",        "obj": { ... } }
{ "success": false, "msg": "错误原因", "obj": null }
```

判断成功**必须看 `success` 字段**，HTTP 200 不代表业务成功。
写脚本时把 `success=false` 连同 `msg` 一起抛出来，否则排查会很痛苦。

---

## 2. 端点总览

统一前缀：`https://127.0.0.1:<面板端口>/<面板路径前缀>/panel/api/`

面板路径前缀 = 面板访问路径去掉首尾斜杠（如 `https://host:46821/a1b2c3d4e5f6g7h8/` → 前缀 `a1b2c3d4e5f6g7h8`）。

| 端点 | 方法 | 作用 |
|---|---|---|
| `/server/status` | GET | 面板状态（CPU/内存/流量） |
| `/inbounds/list` | GET | 入站列表（含完整 `settings`/`streamSettings`） |
| `/inbounds/get/:id` | GET | 单个入站 |
| `/inbounds/add` | POST | 新增入站 |
| `/inbounds/update/:id` | POST | 更新入站（**整体覆盖**） |
| `/inbounds/del/:id` | POST | 删除入站 |
| `/inbounds/allLinks` | GET | 全部入站的分享链接（明文） |
| `/clients/list` | GET | 客户端列表（v3.9.0 规范化表） |
| `/clients/get/:email` | GET | 单客户端 |
| `/clients/add` | POST | 新增客户端并可绑定多个入站 |
| `/clients/update/:email` | POST | 更新客户端（带 `inboundIds` 可改绑定） |
| `/clients/del/:email` | POST | 删除客户端 |
| ~~`/clients/attach`~~ | — | ⚠️ **v3.9.0 不存在（404）**，改用 `/clients/update/:email` |
| ~~`/clients/detach`~~ | — | ⚠️ 同上，未验证 |
| `/setting/all` | **POST** | 读取全部设置（注意是 POST） |
| `/setting/update` | POST | 更新设置（**整体覆盖**） |
| `/setting/restartXrayService` | POST | 重启 Xray |
| `/setting/restartPanel` | POST | 重启面板 |

订阅（**不在** `/panel/api` 下）：

```
https://<域名>:<订阅端口>/<订阅路径>/<subId>
```

---

## 3. 入站（Inbound）字段结构

`POST /inbounds/add` 的 body 是一个 `Inbound` 对象。核心字段：

```json
{
  "up": 0, "down": 0, "total": 0,
  "remark": "JP-Reality-Vision-443",
  "enable": true,
  "expiryTime": 0,
  "listen": "",
  "port": 443,
  "protocol": "vless",
  "settings": "{...JSON 字符串...}",
  "streamSettings": "{...JSON 字符串...}",
  "sniffing": "{...JSON 字符串...}",
  "allocate": "{...JSON 字符串...}"
}
```

### 3.1 ⚠️ 三个嵌套字段是「JSON 字符串」不是对象

`settings` / `streamSettings` / `sniffing` / `allocate` 在 HTTP body 里都是**字符串**，
内容才是 JSON。写错成嵌套对象会被解析失败。

用 Python 拼的时候：

```python
payload = {
    "remark": remark, "port": 443, "protocol": "vless", "enable": True,
    "settings": json.dumps(settings_obj),          # 注意 dumps
    "streamSettings": json.dumps(stream_obj),
    "sniffing": json.dumps({"enabled": True, "destOverride": ["http", "tls", "quic"]}),
}
```

反过来，`GET /inbounds/list` 返回的 `obj[]` 里这几个字段是字符串，需要 `json.loads` 才能改。

### 3.2 Reality（VLESS）settings

```json
{
  "clients": [
    {
      "id": "<uuid>",
      "flow": "xtls-rprx-vision",
      "email": "reality-443",
      "limitIp": 0, "totalGB": 0, "expiryTime": 0, "enable": true,
      "tgId": "", "subId": "<随机 16 位>", "reset": 0
    }
  ],
  "decryption": "none",
  "fallbacks": []
}
```

### 3.3 Reality streamSettings（重点）

```json
{
  "network": "tcp",
  "security": "reality",
  "externalProxy": [],
  "realitySettings": {
    "show": false,
    "xver": 0,
    "dest": "www.apple.com:443",
    "serverNames": ["www.apple.com"],
    "privateKey": "<服务端私钥>",
    "minClientVer": "1.0.0",
    "maxClientVer": "",
    "maxTimeDiff": 0,
    "shortIds": ["0123456789abcdef"],
    "settings": {
      "publicKey": "<公钥 = 客户端 pbk>",
      "fingerprint": "chrome",
      "serverName": "",
      "spiderX": "/641f526a8251639"
    }
  },
  "tcpSettings": { "acceptProxyProtocol": false, "header": { "type": "none" } }
}
```

要点：

- **`minClientVer`，不是 `minClient`**。教程里常见的 `minClient` / `maxClient` 是错的键名，
  3x-ui v3.9.0 不认，写了会被静默丢弃（不报错，也不生效）。
- `dest` 是**回落目标**，必须是真实存在、支持 TLS1.3 且支持 HTTP/2 的境外站点
  （`www.apple.com:443`、`www.microsoft.com:443` 之类）。填错会导致握手被识别。
- `serverNames` 要与 `dest` 的证书 SAN 匹配，否则 TLS 校验不过。
- `shortIds` 每项为偶数长度 hex（8/16 位常见）。
- `spiderX` 建议随机，别留空——空值在部分客户端会暴露特征。
- 服务端只存 `privateKey`；客户端分享链接里是 `pbk`（公钥）。公钥可从私钥推导，
  3x-ui 生成入站时自动填到 `realitySettings.settings.publicKey`。

### 3.4 Hysteria2 settings

3x-ui 里协议名是 `hysteria`（不是 `hysteria2`）。

```json
{
  "version": 2,
  "clients": [
    { "id": "<uuid>", "email": "hy2-443", "enable": true,
      "limitIp": 0, "totalGB": 0, "expiryTime": 0, "subId": "<...>" }
  ]
}
```

> Hysteria2 的**认证密码就是客户端的 `id`（uuid）**。分享链接里
> `hysteria2://<password>@host:port` 的 password 即此 uuid。

### 3.5 Hysteria2 streamSettings

```json
{
  "network": "hysteria",
  "security": "tls",
  "tlsSettings": {
    "serverName": "jp.example.com",
    "minVersion": "1.3",
    "maxVersion": "1.3",
    "cipherSuites": "",
    "certificates": [
      {
        "certificateFile": "/root/cert/jp.example.com/fullchain.pem",
        "keyFile": "/root/cert/jp.example.com/privkey.pem",
        "ocspStapling": 3600, "oneTimeLoading": false, "usage": "encipherment"
      }
    ],
    "alpn": ["h3"],
    "enableSessionResumption": false,
    "settings": { "allowInsecure": false, "fingerprint": "chrome" }
  },
  "hysteriaSettings": { "version": 2, "udpIdleTimeout": 60 },
  "udpMask": { "type": "salamander", "settings": { "password": "<obfs 密码>" } }
}
```

要点：

- **TLS 必须 1.3**（QUIC 硬性要求），`minVersion`/`maxVersion` 都写 `1.3`。
- `alpn` 固定 `["h3"]`。
- 证书直接引用 acme.sh 目录下的 `fullchain.pem` / `privkey.pem`，
  **不需要**复制或改权限（`/root/cert/` 是 acme.sh 的 install-cert 目录）。
- 混淆（obfs）走 `udpMask`，`type: "salamander"` + `settings.password`。
- **端口跳跃不在这里配置**。`mport` 是客户端参数，服务端靠 iptables NAT 把
  整段 UDP 端口 REDIRECT 到 443 实现，见 `scripts/setup_base.sh` 与 `deploy_nodes.py`。

### 3.6 TUIC v5 settings

⚠️ 3x-ui v3.9.0 的 TUIC 是**面板原生实现**（`internal/tuic/`），
**不是** Xray 的 TUIC——配置结构完全不同，别照抄 Xray 文档。

```json
{
  "server": {
    "certificate": "/root/cert/jp.example.com/fullchain.pem",
    "private_key": "/root/cert/jp.example.com/privkey.pem",
    "congestion_control": "bbr",
    "alpn": ["h3"],
    "udp_relay_mode": "native",
    "zero_rtt_handshake": true,
    "log_level": "info",
    "max_idle_time": 15,
    "authentication_timeout": 3,
    "max_udp_relay_packet_size": 1500,
    "sni": "jp.example.com"
  },
  "clients": [
    {
      "id": "<uuid>",
      "uuid": "<同一个 uuid>",
      "password": "<随机密码>",
      "email": "tuic-8443",
      "enable": true, "limitIp": 0, "totalGB": 0, "expiryTime": 0,
      "subId": "<...>"
    }
  ]
}
```

**⚠️ 客户端必须同时给 `id` 和 `uuid`，且值相同。**

- 只给 `uuid` → 报 `Something went wrong (empty client ID)`。
  原因：`internal/web/service/inbound.go` 校验时 `case "tuic": if client.ID == ""`。
- 只给 `id` → 分享链接生成 / 运行时 `tuic.InstanceFromInbound` 拿不到 uuid。
- 两个都给、值一致 → 正常。

`streamSettings` 对 TUIC 基本不用填（证书在 settings.server 里），保持
`{"network": "tuic", "security": "none"}` 之类的最小结构即可。

### 3.7 支持的协议清单

3x-ui v3.9.0 的入站协议白名单（面板 HTTP 层实际接受的值）：

```
vmess | vless | tunnel | http | trojan | shadowsocks | mixed |
wireguard | hysteria | mtproto | amneziawg | tuic
```

**AnyTLS 不在其中 —— 加不了。** 确认方法（比对二进制字符串，不要靠猜）：

```bash
# ⚠️ Debian 默认没装 strings（见 pitfalls.md §1.4），用 grep -a 代替
grep -a -o -i -- "anytls" /usr/local/x-ui/bin/xray-linux-amd64 | wc -l   # → 0
grep -a -o -i -- "anytls" /usr/local/x-ui/x-ui                           | wc -l   # → 0
```

> 另有两条**实现位置**的坑，容易误判：
> - **Hysteria2 在 Xray 里**（`protocol: "hysteria"`），
>   `grep -a -o -i hysteria xray-linux-amd64 | wc -l` 有大量命中；
> - **TUIC v5 不在 Xray 里**，由 `x-ui` 面板进程承载（转发给内部 socks 入站
>   127.0.0.1:64003），所以 Xray 二进制里查不到 `tuic` 属**正常**。
>
> 详见 `pitfalls.md` §6.5。

---

## 4. 客户端模型（v3.9.0 重大变化）

### 4.1 规范化表结构

v3.9.0 起客户端从入站的 `settings.clients[]` 里**抽出来**，落到两张表：

- `clients` —— 客户端身份（email / uuid / subId / 流量 / 到期…）
- `client_inbounds` —— 客户端 ↔ 入站 的多对多关联

**这意味着：直接改 `inbound.settings.clients[].subId` 完全没有效果**，
入站写回时会被规范化层忽略。改客户端属性必须走 `/panel/api/clients/*`。

症状：改了入站的 clients 数组、重启、看订阅——内容纹丝不动，也不报错。

### 4.2 `subId` 全局唯一 → 一条订阅合并节点的正确姿势

`subId` 在 v3.9.0 里是**全局唯一**的，把三个入站的客户端都设成同一个 subId 会报：

```
Duplicate subId: dk7rb4au857uhyb6
```

所以**不能靠共享 subId 来合并订阅**。正确做法是
**一个客户端身份绑定多个入站**：

```bash
CB="$BASE/clients"
curl -sS -k -H "Host: $DOMAIN" -H "Authorization: Bearer $API_TOKEN" \
     -H "Content-Type: application/json" \
     -X POST "$CB/add" -d '{
  "client": {
    "id": "<uuid>",
    "email": "merged",
    "subId": "x7k2m9p4q6t8w3z5",
    "enable": true, "limitIp": 0, "totalGB": 0, "expiryTime": 0
  },
  "inboundIds": [1, 2, 3]
}'
```

`inboundIds` 就是 `client_inbounds` 关联。之后一条订阅 URL
（`.../<订阅路径>/<subId>`）就会返回全部 3 个节点。

### 4.3 ⚠️ `id` 字段的类型陷阱

- `model.Client.id` 在 Go 里是 **`string`**（承载 UUID 凭据）。
- 但 `GET /clients/get/:email` 返回的 `obj.id` 是**数据库行号（number）**。

于是"读出来 → 改一下 → 写回去"的朴素写法会炸：

```
json: cannot unmarshal number into Go struct field .id of type string
```

**规避**：不要回写 GET 的原始对象。手工构造 body，或先把 `id` 覆盖成 UUID 字符串。

### 4.4 合并后 Reality 的 `flow` 会丢

用 `/clients/add` 建出来的客户端默认没有 `flow`。
Reality+Vision 节点必须有 `flow: "xtls-rprx-vision"`，否则客户端连上但流量异常。

合并后补一次：

```bash
curl -sS -k -H "Host: $DOMAIN" -H "Authorization: Bearer $API_TOKEN" \
     -H "Content-Type: application/json" \
     -X POST "$CB/update/merged" -d '{
  "id": "<uuid>", "email": "merged", "flow": "xtls-rprx-vision",
  "subId": "x7k2m9p4q6t8w3z5", "enable": true,
  "limitIp": 0, "totalGB": 0, "expiryTime": 0
}'
```

### 4.5 清理旧客户端

合并完成后删掉每个入站上的原始客户端，避免订阅里出现重复节点：

```bash
for e in reality-443 hy2-443 tuic-8443; do
  curl -sS -k -H "Host: $DOMAIN" -H "Authorization: Bearer $API_TOKEN" \
       -X POST "$CB/del/$e"
done
```

---

## 5. 分享链接生成

`GET /inbounds/allLinks` 返回 `obj[]`，每项形如：

```json
{ "remark": "JP-Reality-Vision-443", "type": "reality",
  "uri": "vless://...", "port": 443, "protocol": "vless" }
```

### 5.1 `shareAddrStrategy` / `shareAddr`

生成链接时用什么地址，由面板设置决定：

| `shareAddrStrategy` | 行为 |
|---|---|
| `auto` | 自动取（常常是 `127.0.0.1` 或内网 IP ← **坑**） |
| `custom` | 用 `shareAddr` 里指定的值 |
| `host` | 用请求的 Host |

**必设 `custom` + 域名**，否则分享链接里会出现 `127.0.0.1`，客户端拿去当然连不上。

```bash
curl -sS -k -H "Host: $DOMAIN" -H "Authorization: Bearer $API_TOKEN" \
     -H "Content-Type: application/json" \
     -X POST "$BASE/setting/update" -d '{
  "webDomain": "jp.example.com",
  "shareAddrStrategy": "custom",
  "shareAddr": "jp.example.com",
  ...
}'
```

### 5.2 ⚠️ `/setting/all` 是 POST，且 update 是整体覆盖

- 读设置：`POST /setting/all`（不是 GET，GET 会 404）。
- 写设置：`POST /setting/update` 会**用你提交的对象整体替换**现有设置。

  所以必须先 `POST /setting/all` 拿到全量，改掉要改的键，再整体提交。
  **只提交 `{shareAddr: "..."}` 会把其它设置清空**（webDomain、端口、路径全丢，
  面板直接失联）。

  安全写法：

  ```python
  cur = api("POST", "/setting/all")["obj"]
  cur["shareAddrStrategy"] = "custom"
  cur["shareAddr"] = DOMAIN
  api("POST", "/setting/update", cur)   # 整体回写
  ```

### 5.3 REALITY 链接里的 `sni` 为空

如果 Reality 入站的 `realitySettings.settings.serverName` 与
`realitySettings.serverNames` 不一致，生成的链接可能出现空 `sni`。
确保 `serverNames` 至少一项，且 `settings.serverName` 与之相同（或留空由客户端回退到
`serverNames[0]`）。

---

## 6. 常用 curl 片段

```bash
BASE="https://127.0.0.1:46821/a1b2c3d4e5f6g7h8/panel/api"
H=(-H "Host: jp.example.com" -H "Authorization: Bearer $API_TOKEN")
J=(-H "Content-Type: application/json")

# 列出入站
curl -sS -k "${H[@]}" "$BASE/inbounds/list" | python3 -m json.tool

# 列出客户端
curl -sS -k "${H[@]}" "$BASE/clients/list" | python3 -m json.tool

# 全部分享链接
curl -sS -k "${H[@]}" "$BASE/inbounds/allLinks" | python3 -m json.tool

# 读设置（POST！）
curl -sS -k "${H[@]}" -X POST "$BASE/setting/all" | python3 -m json.tool

# 重启 Xray
curl -sS -k "${H[@]}" -X POST "$BASE/setting/restartXrayService"
```

> ⚠️ **Windows Git Bash 用户**：上面这些以 `/` 开头的端点参数会被 MSYS 转换成
> Windows 路径（`/inbounds/del/4` → `D:/.../PortableGit/inbounds/del/4`），
> 结果是莫名其妙的 **404**。优先用本仓库的 CLI：
>
> ```bash
> python scripts/xui_api.py list-inbounds
> python scripts/xui_api.py del-inbound 4      # 数字 id 参数，不经过 shell 路径转换
>
> # 非要手写端点时，关掉本次命令的转换
> MSYS_NO_PATHCONV=1 python scripts/xui_api.py raw POST /inbounds/del/4
> ```
>
> 详见 `pitfalls.md` §1.6。

---

## 7. 调试检查表

遇到 API 调用异常，按顺序排查：

1. **403 + 空响应体** → Host 头没带 / 带了错的域名（见 §1.2）
2. **404** → 面板路径前缀写错，或该端点方法用错（`setting/all` 是 POST）
3. **`success: false` + `msg`** → 读 msg，通常能直接定位（如 `Duplicate subId`）
4. **改了没效果** → 是不是在改 `settings.clients[]`？v3.9.0 要改 `/clients/*`（见 §4.1）
5. **`cannot unmarshal number into ... .id of type string`** → 回写了 GET 的原始对象（见 §4.3）
6. **`empty client ID`** → TUIC 客户端缺 `id` 字段（见 §3.6）
7. **分享链接里是 127.0.0.1** → `shareAddrStrategy` 没设 custom（见 §5.1）
8. **`request body failed validation`** → 协议名不在白名单里（见 §3.7），
   或 `settings` 结构写错（VLESS/TUIC 用 `clients`，HTTP/Mixed 用 `accounts`）
9. **`/clients/attach` 404** → v3.9.0 没有该端点；改用 `POST /clients/update/<email>` 带 `inboundIds`（见 §4.2）
10. **订阅里少节点** → 3x-ui 只为 `vmess/vless/trojan/shadowsocks/tuic` 生成链接，
    其它协议（http/mixed/wireguard/mtproto 等）**本来就不进订阅**，这是设计如此
11. **`allLinks` / 订阅返回空** → 订阅端点（2096）同样要带 `Host` 头，否则 403 空响应（见 §1.2）
12. **API 调用全部成功，但重启 Xray 后节点全挂** →
    v3.9.0 增删入站走 gRPC 热更新，**不重写 `bin/config.json`**；
    改完必须 `systemctl restart x-ui`（见 `pitfalls.md` §2.6）
13. **`sqlite3 ... where key='apiToken'` 读不到 Token** → v3.9.0 只存 SHA-256（见 §1.1）
14. **想加 AnyTLS** → 3x-ui v3.9.0 不支持，换 sing-box / mihomo 服务端（见 §3.7）
15. **同一端点：命令行里调返回 404，写在 Python 里调却 200** →
    Windows Git Bash 的 MSYS 路径转换改写了参数（见 `pitfalls.md` §1.6）。
    改用 `xui_api.py del-inbound <id>` 这类数字 id 子命令，或 `MSYS_NO_PATHCONV=1`。
    诊断窍门：看报错回显的完整 URL 里是否混进了 `D:/.../PortableGit/...`
