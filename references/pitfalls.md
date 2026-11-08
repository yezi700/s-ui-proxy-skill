# 踩坑与修复

> 全部来自实际部署。每条都写清「现象 → 根因 → 修复」，
> 方便对照排查，而不是让你从头试。

## 症状速查

**别从头读。** 先在下表找到你看到的那一行，再跳过去。

| 你看到的 | 最可能的原因 | 去这里 |
|---|---|---|
| 节点能连、能开网页，但吞吐只有几百 KB/s | 明文 QUIC 被限速 | §6.8 |
| Reality 报 `REALITY authentication failed` | 443 被针对性干扰 | §1.1 |
| Hysteria2 全部 `context deadline exceeded` | 同上 | §1.1 |
| 「我改了端口，怎么没生效」 | 只改了 out_json，没改 options | §3.13 |
| `initialize inbound[4]: missing psk` | SS 入站缺 password | §6.9.2 |
| `converting NULL to string is unsupported` | client config 缺 `shadowsocks16` | §6.9.1 |
| 核心起不来、所有端口一起消失 | 同上（SS 配置错会让整核挂掉） | §6.9.1 |
| 订阅里有个节点永远连不上，但面板里没有 | 删了入站没清 clients.config | §3.11 |
| `json: unknown field "options"` | 协议参数放错了层级 | §3.10 |
| `cannot unmarshal number into Go value of type string` | 删入站传了 id | §3.5 |
| 删除返回 `success:true` 但没删掉 | 传了 id 的字符串形式 | §3.5 |
| 登录接口 404 | `PANEL_PATH` 为空 | §2.8 |
| 改密码返回空响应，面板还是 admin/admin | 用了 127.0.0.1，撞 SNI | §2.9 |
| `tls: unrecognized server name ""` | 同上 | §3.8 |
| `tls: server did not select an ALPN protocol` | TUIC 与 AnyTLS 共用 TLS 对象 | §4.2 |
| 面板 API 返回 403 且响应体为空 | 缺 `Origin` 头 | §3.1 |
| `save: unexpected end of JSON input` | clients 缺 `links:[]` | §3.3 |
| 连自己刚开的端口超时，但 VPS 上能 curl 通 | 防火墙没放行 | §5.3 |
| 整个 SSH 断了 / 进程莫名被杀 | `pkill -f` 匹配到自己 | §8.4 |
| `缺少 paramiko` 但明明装了 | `py` 启动器读了脚本的 `#!` | §8.1 |
| Clash 里节点数比实际少 | 数法错了（YAML 缩进） | §6.7 |
| Clash 导入报 `cannot unmarshal !!str` | 填了 base64 那条订阅 | §6.6 |
| 只有 Reality 能连，其它全挂 | 客户端用的是 Xray 内核 | §6.1 |
| 偶发 `salt not unique` | 客户端复用 salt，不是服务端问题 | §6.9.6 |
| 只有 SS 不通，delay test 报错、curl 502 | 密码只填了用户 psk，缺 `服务器psk:` 前缀 | §6.9.4 |
| 某个节点掉到几百 KB/s，想换端口救 | 单端口 UDP 被 QoS | §6.10 |
| sing-box 订阅整份解析失败 `unknown outbound type` | 面板里有 naive 入站 | §6.11 |
| 脚本连面板 502，但 curl 能通 | 本机系统代理被 urllib 吃掉了 | §8.6 |
| 测速结果全是 0.0 Mbps | `-o /dev/null` 在 Windows 上写失败 | §8.7 |

**排查顺序**：先分清是哪一层 —— 服务端在跑？端口通？协议握手成功？
三层验证见 §7.1，不要一上来就重装面板。

---

## §1 端口与封锁

### 1.1 443 端口被针对性干扰（最重要）

**现象**

- VLESS-Reality @ 443/TCP：客户端日志 `REALITY authentication failed`，反复重试
- Hysteria2 @ 443/UDP：全部 `context deadline exceeded`
- TUIC @ 8443/UDP：3/3 正常

**容易误判的点**

在 VPS 上对 443 做未认证 TLS 握手，**能拿到上游真证书**：

```bash
openssl s_client -connect <IP>:443 -servername www.apple.com -tls1_3 </dev/null
# subject=... CN=www.apple.com   ← 看起来一切正常
```

于是很容易得出"服务端没问题，是客户端配错了"的结论。**但服务端确实没问题，
问题在链路中间**：认证握手的参数被干扰，Reality 把它当成未认证探测，
转发给了真实网站，客户端收到的是 apple 的响应而不是 Reality 的 → 认证失败。

**判定方法（做对照实验）**

把同一个 Reality 入站换到另一个端口（如 2053），其他参数一字不改：

- 换端口就通 → **端口被针对**，协议没问题
- 换端口仍不通 → 才是协议或配置问题

实测结果：Reality @ 2053 与 Hysteria2 @ 2083 都 3/3 通过。

**修复**

所有节点避开 443。本 skill 默认：

```
AnyTLS    8443/tcp
TUIC      8443/udp
Hysteria2 2083/udp
Reality   2053/tcp
```

都是标准 HTTPS/QUIC 常用端口，隐蔽性和 443 没有区别 —— **端口号本身不影响速度**，
影响速度的是线路、协议开销和运营商 QoS。

### 1.2 TCP 与 UDP 是两套独立命名空间

`443/tcp` 和 `443/udp` 可以各跑一个协议，互不冲突。本 skill 让 AnyTLS 用 8443/tcp、
TUIC 用 8443/udp，就是这个原理，**不是端口复用**。

改防火墙时两个都要留，否则会莫名其妙掉一半节点。

### 1.3 野鸡高位端口反而更显眼

`58231` 这类随机端口在跨国链路上没有任何正常业务会持续跑大流量，
DPI 一眼就能挑出来。选标准端口（443 / 2053 / 2083 / 8443 / 2096）更安全。

### 1.4 Cloudflare 只代理固定端口

需要套 CDN 时，HTTPS 只能选：`443, 2053, 2083, 2087, 2096, 8443`；
HTTP 只能选：`80, 8080, 8880, 2052, 2082, 2086, 2095`。

但 **Reality 不能走 CDN**（CDN 会终止 TLS，伪装机制直接失效）。
要套 Cloudflare 只能用 VMess/VLESS + WS + TLS 这类。

---

## §2 S-UI 安装与配置

### 2.1 官方 install.sh 是交互式的，没法自动化

它会问语言、端口、路径、账号密码，`bash <(curl ...)` 在自动化流程里会卡住。

**做法**：直接下 release 包手动安装，然后改 `db/s-ui.db` 的 settings 表。
S-UI 的全部配置都是 key/value，直接改库比猜安装脚本的交互时序可靠得多。

```bash
tar xzf s-ui-linux-amd64.tar.gz
cp s-ui/sui s-ui/s-ui.sh /usr/local/s-ui/
cp s-ui/s-ui.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now s-ui
```

### 2.2 settings 表没有 UNIQUE 约束

`INSERT ... ON CONFLICT(key) DO UPDATE` 会报：

```
sqlite3.OperationalError: ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint
```

**修复**：先 `UPDATE`，`rowcount == 0` 时再 `INSERT`。

```python
def setk(k, v):
    cur = c.execute("UPDATE settings SET value=? WHERE key=?", (v, k))
    if cur.rowcount == 0:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?)", (k, v))
```

### 2.3 首次启动会因端口冲突直接退出

默认面板 2095 / 订阅 2096。如果 2096 已被占（比如 3x-ui 还在跑），进程会：

```
INFO - web server run http on [::]:2095
listen tcp :2096: bind: address already in use
s-ui.service: Main process exited, code=exited, status=1/FAILURE
```

**这不是安装失败** —— DB 已经生成了。`systemctl stop` 后改库里的 `subPort`，
再 `systemctl restart` 即可。

### 2.4 改面板端口 / 路径 / 证书不会自动重启

`settings` 保存后 DB 里已经是新值，但监听端口不变。文档说"会重启面板"，
实测不生效（v1.6.7）。

**必须手动**：`systemctl restart s-ui`。

排查"改了没生效"时，先 `ss -lntp | grep <端口>` 确认，别去怀疑 API。

### 2.5 时区默认是 Asia/Tehran

新装面板的 `timeLocation` 默认 `Asia/Tehran`，统计图表时间会错 4.5 小时。
装完记得改成 `Asia/Shanghai`。

### 2.6 节点名重复成 `US-US-xxx`

S-UI 的节点名规则是 `<client remark><inbound tag><addr remark>`。
如果 client 的 `remark` 填了 "US"、inbound tag 又是 "US-AnyTLS-8443"，
就变成 `US-US-AnyTLS-8443`。

**修复**：把 settings 里的 `subRemarkPrefix` 设为 `false`（本 skill 默认），
节点名直接取 inbound tag。

### 2.7 升级会覆盖二进制但保留 DB

`/usr/local/s-ui/db/` 是数据目录，升级时只替换 `sui` 二进制即可。
升级前先 `cp db/s-ui.db /root/s-ui.db.bak.$(date +%Y%m%d-%H%M)`。

### 2.8 `webPath` / `subPath` 是安装时随机生成的，本地配不出来

`install_sui.sh` 会随机生成这两个路径并写进 `/usr/local/s-ui/install-result.env`。
但 `deploy.env` 是**安装之前**填的，不可能知道它们 —— 留空的话：

```
SuiError: HTTP 404            ← 登录接口
```

因为拼出来的地址是 `https://域名:46821/api/login`，少了 `/<webPath>` 这一段。
报错里完全看不出是路径没填。

**已经修好**：`ssh_run.fill_from_install_result()` 会在 `deploy_nodes.py` /
`render_report.py` 建立 SSH 连接后，把 `deploy.env` 里留空的关键参数从
`install-result.env` 读回来（只补空值，不覆盖你显式填的）。

### 2.9 改面板密码必须用域名，不能用 127.0.0.1

有证书时面板的 `webDomain` 和证书 CN 都是域名，用 `https://127.0.0.1:<端口>` 访问会被
SNI 校验拒掉：

```
http: TLS handshake error: tls: unrecognized server name ""
```

`curl -sk` **不报错**，`changePass` 返回空字符串 —— 脚本以为改完了，
实际面板还是 `admin/admin`。这是最危险的一种失败：静默、且留下默认口令。

**正确做法**：域名 + `--resolve` 钉到本机（满足 SNI，又不绕公网）：

```bash
curl -sk --resolve <域名>:<面板端口>:127.0.0.1 \
  -H "Origin: https://<域名>:<面板端口>" \
  -X POST "https://<域名>:<面板端口>/<webPath>/api/changePass" \
  --data-urlencode "oldPass=admin" \
  --data-urlencode "newUsername=admin" \
  --data-urlencode "newPass=<新密码>"
```

改完**必须回读验证**（新密码能登进去），别信空响应。
`install_sui.sh` 现在会自动验证，失败时打红字并给出补救命令。

---

## §3 面板 API

### 3.1 前端 API 必须带 Origin 头

`POST /{webPath}/api/login` 缺 `Origin` 会返回 **403 且响应体为空**，
看起来像"服务挂了"或"路径不对"。

```bash
curl -sk -X POST "https://<域名>:<端口>/<webPath>/api/login" \
  -H "Origin: https://<域名>:<端口>" \
  --data "user=admin&pass=<密码>"
```

`Origin` 必须与访问地址**完全一致**（含端口和 scheme）。

### 3.2 登录字段是 `user` / `pass`

不是 `username` / `password`。

### 3.3 保存 clients 必须带 `links:[]`

**现象**

```
{"success":false,"msg":"save: unexpected end of JSON input","obj":null}
```

**排查陷阱**：这个报错会让人以为是 JSON 格式错或 data 没传进去。
实测传合法 JSON、非法 JSON、空对象分别得到不同报错，说明 data 确实传到了：

| data | 报错 |
|---|---|
| 空 | `unexpected end of JSON input` |
| `{bad` | `invalid character 'b' looking for beginning of object key string` |
| `null` / `{}` | `client name must not be empty` |
| `{"name":"x"}` | `unexpected end of JSON input` ← 卡在这 |

**根因**：`service/client.go` 里

```go
json.Unmarshal(client.Inbounds, &inboundIds)   // Inbounds 为 nil → 报错
json.Unmarshal(client.Links, &clientLinks)     // Links 为 nil → 同样报错
```

`json.RawMessage` 为 nil 时 `Unmarshal(nil, ...)` 抛的就是 `unexpected end of JSON input`。

**修复**：保存时显式带上 `"links": []` 和 `"inbounds": [...]`。
`sui_api.save_client()` 已自动补齐。

### 3.4 `GET /api/clients` 不返回 `config`

返回体里只有 `id/enable/name/inbounds/volume/expiry/...`，**没有 config**。

直接把这个对象改一改再 `edit`，会把 config 覆盖成空。随后 S-UI 在
`UpdateInboundsUsers` 里执行：

```sql
json_extract(clients.config, '$.anytls')
```

拿到 NULL，报：

```
sql: Scan error on column index 0, name "json_extract(clients.config, "$.anytls")":
converting NULL to string is unsupported
```

**修复**：edit 前从 DB 把 config 读回来补上（`deploy_nodes.load_client_config()`）。

```python
conn = sqlite3.connect("file:/usr/local/s-ui/db/s-ui.db?mode=ro", uri=True)
row = conn.execute("select config from clients where id=?", (cid,)).fetchone()
```

### 3.5 删除对象：clients 传裸 id，inbounds 传 **tag 字符串**

两套对象的 `del` 格式**不一样**，而且写错的报错会误导人。

**clients** —— 裸数字：

```bash
--data-urlencode "data=7"          # 对
--data-urlencode 'data={"id":7}'   # 错：cannot unmarshal object into Go value of type uint
```

**inbounds** —— 必须是 `tag` 的 **JSON 字符串字面量**（带引号）。在 1.6.7 上逐个试过：

| 写法 | 结果 |
|---|---|
| `data=6`（数字） | `json: cannot unmarshal number into Go value of type string` |
| `data={"id":6}` | `json: cannot unmarshal object into Go value of type string` |
| `data="6"`（id 的字符串） | **返回 `success:true`，但什么都没删** ← 最坑 |
| `data="JP-Shadowsocks-2087"`（tag） | 真的删掉了 |

最后一行那种「静默成功」最要命：脚本以为删了，订阅里还挂着那个节点，重启后它又活了。
`sui_api.del_inbound(tag)` 已经封装成正确形式；**别传 id**。

删完还要看 §3.10 —— 入站删了，客户端 config 里的协议条目不会自动消失。

### 3.6 settings 的值全是字符串

布尔要写 `"true"` / `"false"`，数字要写 `"2096"`。传真布尔值会被忽略或报错。

### 3.7 两套 API 的取舍

| | `/{webPath}/api/...` | `/{webPath}/apiv2/...` |
|---|---|---|
| 认证 | Cookie（登录获得） | `Token` 头 |
| Token 来源 | 登录即可 | **只能在面板 Admin 页手动创建** |
| 适用 | 脚本自动化首选 | 长期集成、外部系统调用 |

`apiv2` 没有"登录换 Token"的接口，所以自动化流程用前端 API + Cookie 更省事。

### 3.8 SNI 不匹配导致连不上

面板证书和 `webDomain` 都是按域名配的。用 `https://127.0.0.1:46821` 访问会：

```
http: TLS handshake error: tls: unrecognized server name ""
```

**修复**：用域名访问，或 `curl --resolve 域名:端口:127.0.0.1`。
Python 里没有等价于 `--resolve` 的简单写法，所以 `sui_api.from_env()` 统一用域名 ——
在 VPS 上访问自己的域名会走 `lo`，同样能通。

### 3.9 `keypairs` 端点取不到 Reality 密钥

`GET /api/keypairs?k=reality`（也试过 `x25519` / `wireguard`）返回空。

**替代方案**：用 openssl 生成，取 DER 末 32 字节转 base64url：

```python
priv_der = subprocess.run(["openssl","genpkey","-algorithm","X25519","-outform","DER"],
                          capture_output=True, check=True).stdout   # 48 字节
pub_der = subprocess.run(["openssl","pkey","-inform","DER","-pubout","-outform","DER"],
                         input=priv_der, capture_output=True, check=True).stdout  # 44 字节
priv = base64.urlsafe_b64encode(priv_der[-32:]).decode().rstrip("=")  # 43 字符
```

生成的 43 字符私钥与 Xray 的 `privateKey` 格式完全一致。

### 3.10 协议专属参数放在入站**顶层**，不是 `options`

S-UI 的 `inbounds` 表里有一列 `options`，所以很容易以为 POST 时也该传 `options`。
**不行**：

```
save: options: json: unknown field "options"
```

服务端只接受它认识的顶层字段，然后自己把它们序列化进 `options` 列。SS2022 要这样传：

```json
{
  "type": "shadowsocks", "tag": "JP-Shadowsocks-2087",
  "listen": "::", "listen_port": 2087, "tls_id": 0,
  "addrs": [{"server": "<域名>", "server_port": 2087, "remark": ""}],
  "out_json": {"type": "shadowsocks", "tag": "...", "server": "...",
               "server_port": 2087, "method": "2022-blake3-aes-128-gcm",
               "password": "<psk>"},
  "method": "2022-blake3-aes-128-gcm",
  "password": "<psk>"
}
```

落库后 `options` 列会变成 `{"listen":"::","listen_port":2087,"method":...,"password":...}`。
`tls_id` 用 `0`（SS 自己就是加密协议，不挂 TLS 对象）。

### 3.11 删了入站，订阅里还挂着一个死节点

`clients.config` 是**按协议**存的键值对（`anytls` / `vless` / `hysteria2` / `tuic` /
`shadowsocks16` …），和入站表没有外键关系。删掉 TUIC 入站后：

- `/api/inbounds` 里没有 TUIC 了 ✅
- 但订阅的三种格式里**都还在生成 `tuic://` 链接** ❌

用户导进去就多一个永远连不上的节点。所以删入站的脚本必须同时清 `clients.config`：

```python
alive = {i["type"] for i in api.inbounds()}
KEY2TYPE = {"shadowsocks16": "shadowsocks", "shadowsocks": "shadowsocks", ...}
for k in [k for k in conf if KEY2TYPE.get(k) not in alive]:
    del conf[k]
api.save_client({**me, "config": conf, "links": me.get("links") or []})
```

⚠️ 改完要回读订阅确认，别信 `success:true`（见 §3.5 的静默成功）。

### 3.12 `options` / `out_json` / `addrs` 是 BLOB 列

`sqlite3` 取出来是 `bytes`，直接 `json.loads` 会报
`TypeError: the JSON object must be str, bytes or bytearray` —— 其实是能吃的，
真正的坑是 `dict(raw)` 报
`cannot convert dictionary update sequence element #0 to a sequence`。

统一先 `.decode()`：

```python
raw = row[0]
if isinstance(raw, bytes):
    raw = raw.decode("utf-8", "replace")
opts = json.loads(raw)
```

另外 **`inbounds` 表没有 `listen_port` 列**，端口在 `options` 里：
`json_extract(options, '$.listen_port')`。而 `/api/inbounds` 返回的 JSON 里
`listen_port` 是顶层字段 —— 两处不一样，别混。

### 3.13 改端口要改三处

只改 `out_json.server_port` 和 `addrs[].server_port`，监听器**不会动**，还留在老端口上。
因为真正的 `listen_port` 在 `options` 列里。三处都要写：

1. `options.listen_port`
2. `out_json.server_port`
3. `addrs[0].server_port`

改完 `restartSb`（或重启 s-ui）才会生效。

---

## §4 证书

### 4.1 一份证书服务多个组件

`/root/cert/<域名>/{fullchain.pem,privkey.pem}` 同时被面板、订阅、AnyTLS、
Hysteria2 引用。**删掉会同时挂掉这几处。**

（Shadowsocks 和 VLESS-Reality 不用它：前者自己就是加密协议，后者靠 Reality 借用外部 SNI。）

### 4.2 TUIC 需要独立的 TLS 对象

同一个 TLS 对象不能同时服务 AnyTLS 和 TUIC：TUIC 要求 `alpn:["h3"]`。

不满足时的客户端报错：

```
CRYPTO_ERROR 0x178 (local): tls: server did not select an ALPN protocol
```

**修复**：为 TUIC 单独建一个 TLS 对象（`tuic-<域名>`），`server.alpn` 和
`client.alpn` 都设 `["h3"]`。

### 4.3 acme.sh 的 reloadcmd 要指向 s-ui

```bash
~/.acme.sh/acme.sh --install-cert -d "$DOMAIN" \
  --key-file /root/cert/$DOMAIN/privkey.pem \
  --fullchain-file /root/cert/$DOMAIN/fullchain.pem \
  --reloadcmd "systemctl restart s-ui"
```

不设 `reloadcmd` 的话证书续期后进程还在用旧证书，直到下次重启。

---

## §5 防火墙与系统

### 5.1 别用 UFW

Debian 12+ 上 `ufw` 的包声明了 `Breaks: iptables-persistent`，
安装会卸掉后者并留下残缺规则，很容易把自己锁在外面。用纯 iptables +
`iptables-persistent`。

### 5.2 `ACCEPT all` 会让 `policy DROP` 失效

如果 INPUT 链第一条是无条件 `ACCEPT all`（常见于旧面板遗留规则），
后面的 `policy DROP` 等于没写，**所有端口都在对外开放**。

排查：

```bash
iptables -L INPUT -n --line-numbers | head -5
```

看到第一条是 `ACCEPT all -- 0.0.0.0/0 0.0.0.0/0` 且没有 `-i` / `-p` 限定时，
说明规则已经失效，应该重建。

### 5.3 VPS 自测验证不了防火墙

访问自己的公网 IP 走 `lo` 接口，**不经过 INPUT 链**。
在服务器上 `curl localhost:端口` 成功，完全不能证明外部能连。

要验证外部可达性，必须从外部网络拨号，或对端口做外部 TCP 探测。

### 5.4 本机有代理客户端时 curl 会被截胡

如果本机开着 `127.0.0.1:10808` 之类的代理，`curl` 会自动走它，
导致"时通时不通"的假象。验证外部可达性前先：

```bash
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
```

### 5.5 屏蔽 IPv6 的正确顺序

先换 DNS 再关 IPv6。顺序反了会导致域名解析卡住（IPv6 nameserver 不可达）。
脚本里的做法：备份 `resolv.conf` → 写纯 IPv4 DNS → 写 sysctl 关 IPv6 → 自检 →
失败自动回滚。

---

## §6 协议与客户端

### 6.1 兼容性矩阵必须整张给

只写"支持 Reality"或只写"支持 Hy2"，会让用户在两种"连不上"之间来回折腾：

- **Xray 系**（v2rayN / v2rayNG 默认内核）→ 连不上 TUIC / Hysteria2 / AnyTLS
- **sing-box 系 / mihomo 系** → 四种全支持

### 6.2 AnyTLS 解决什么

TLS-in-TLS 嵌套握手指纹 —— 代理流量里再套一层 TLS（访问 HTTPS 网站）时，
外层和内层的包长分布会呈现可识别模式。AnyTLS 用 padding scheme 打散，
让整条连接看起来像普通 TLS 应用数据，同时做连接复用降低延迟。

sing-box 1.12.0 引入。配置极简：`password` + TLS + `padding_scheme`（用默认值，
**不要自己改**，改错了反而形成新指纹）。

### 6.3 AnyTLS 不能配 Reality

- mihomo 官方明确：**不支持 AnyTLS+Reality，未来也不会支持**
- S-UI 里配 AnyTLS+Reality 入站也不工作

所以用 AnyTLS 必须走真实 TLS（自有域名 + 证书）。想隐藏 SNI 就叠
ShadowTLS v3 / ECH。

### 6.4 TUIC 的 `congestion_control` 会被重置

在 `out_json` 里设 `bbr`，保存后会被面板改回 `cubic`。
不影响连通性，只影响拥塞控制算法，不必纠结。

### 6.5 客户端默认 UDP 可能是关的

mihomo 的 AnyTLS 代理如果订阅里没有 `udp: true`，UDP 默认关闭，
表现为"能上网但某些应用不通"。

本 skill 在 settings 里设 `subClashUdp=true`，生成的 Clash 配置会带 `udp: true`。
`verify_nodes.sh` 会检查这一项。

### 6.6 订阅格式不能混用

| 地址 | 返回 | 给谁 |
|---|---|---|
| `/<subPath>/<subId>` | base64 链接列表 | v2rayN / v2rayNG / NekoBox / Karing / Hiddify / Shadowrocket |
| `/<subPath>/<subId>?format=clash` | Clash YAML | Clash Verge Rev / Clash Meta / FlClash |
| `/<subPath>/<subId>?format=json` | sing-box JSON | sing-box 原生 |

把 base64 那条填给 Clash，报 `yaml: unmarshal errors: cannot unmarshal !!str`。

### 6.7 数 Clash 订阅里的节点数，别按 `- name:` 数

S-UI 的 YAML 生成器只在每个节点的**第一个键**前面写 `- `，而键是按字母序排的：

```yaml
proxies:
    - name: JP-AnyTLS-8443        # name 排第一 → 有 "- name:"
      password: ...
    - alpn:                     # Hysteria2 的 alpn 排第一 → "- " 在 alpn 上
        - h3
      name: JP-Hysteria2-2083     # 这一行没有 "- "
```

所以 `grep -c '^    - name: '` 只会数到 AnyTLS 一个（再加 proxy-groups 里的），
四个节点被数成 1~2 个，很容易误判成「订阅少了节点」。

正确做法是数 `proxies:` 段里所有 `    - ` 开头的行：

```bash
awk '/^proxies:/{f=1;next} /^[a-zA-Z_-]+:/{f=0} f&&/^    - /{c++} END{print c+0}' sub.yaml
```

`verify_nodes.sh` 已按这个改。

---

### 6.8 TUIC 在部分线路上会被限速到不可用

**症状**：节点能连上、能认证、能打开纯文本页面，但吞吐只有 **0.2~0.4 Mbps**，
图片和视频基本加载不出来。延迟看起来还正常，所以很容易误判成「客户端配置错」。

**逐项排除**（2026-11 在一台日本 VPS 上做完整套）：

| 检查 | 结果 | 说明 |
|---|---|---|
| 换端口 8443 → 2084 | 同样慢 | 不是端口被封 |
| 换客户端 mihomo → sing-box 1.14.4 官方 | 同样慢 | 不是客户端实现问题 |
| 服务端日志时间戳 | 连接在同一秒内转发出去 | 服务端没有卡住 |
| `nstat -az UdpRcvbufErrors` 传输前后差值 | 0 | 不是收包缓冲区打满 |
| 同一台机器上的 Hysteria2 | 正常，几十 Mbps | 不是线路总带宽不够 |

**结论**：这条路径对**明文 QUIC** 做了限速/整形。Hysteria2 能活下来是因为它默认带
`salamander` 混淆，把 QUIC 包伪装过；TUIC 的握手特征太明显。

**处置**：别在 TUIC 上耗时间，直接换一个 **TCP 系的轻量协议**。
Shadowsocks 2022 是最省事的选择（无 TLS 握手、握手往返最少、客户端兼容面广）。
`deploy_nodes.py` 已经把 TUIC 改成可选、SS 改成默认（`TUIC_PORT` 留空即不部署）。

> 顺带一提：如果某天发现 Hysteria2 也掉了，先把 `obfs` 打开再排查别的。

---

### 6.9 Shadowsocks 2022 的必填项

漏一个核心就直接起不来，而且报错完全不提「SS」。

#### 6.9.1 客户端 config 的键是 `shadowsocks16`，不是 `shadowsocks`

`clients.config` 里 2022 系列要用 `shadowsocks16`：

```json
{"shadowsocks16": {"name": "jp", "password": "<psk>"}}
```

写成 `shadowsocks` 的后果：

```
restartSb: sql: Scan error ... json_extract(clients.config, "$.shadowsocks16"):
converting NULL to string is unsupported
```

然后 sing-box 核心**整个挂掉**，所有节点端口一起消失（不只是 SS）。
排查时容易以为是自己把面板搞崩了。

#### 6.9.2 服务端 `options` 必须有 `password`（psk）

只有 `method` 不够：

```
initialize inbound[4]: missing psk
```

psk 要同时写在入站顶层（API 传参）和 `out_json` 里，见 §3.10。

#### 6.9.3 psk 长度由 method 决定

| method | psk 字节数 | base64 长度 |
|---|---|---|
| `2022-blake3-aes-128-gcm` | 16 | 24（末尾 `==`） |
| `2022-blake3-aes-256-gcm` | 32 | 44（末尾 `=`） |
| `2022-blake3-chacha20-poly1305` | 32 | 44（末尾 `=`） |

必须是**标准 base64（带 `=` 填充）**，不能像别的密码那样用 urlsafe 去填充版。
长度不对 → `initialize inbound: invalid password`。

#### 6.9.4 客户端里的密码格式是 `psk:psk`

mihomo 的 `ss` 代理，2022 系列要写 `服务器psk:用户psk`。只有一个用户时两边相同：

```yaml
- name: JP-Shadowsocks-2087
  type: ss
  cipher: 2022-blake3-aes-128-gcm
  password: AAAAAAAAAAAAAAAAAAAAAA==:AAAAAAAAAAAAAAAAAAAAAA==
```

（上面是两个占位 psk，真实值由部署脚本生成；只有单用户时两边相同。）

只写一个 psk 会认证失败。订阅生成的是正确格式，手写配置时才容易漏。

**症状很隐蔽**：节点看起来是"通的"（TCP 能连上），但
`/proxies/<name>/delay` 只回 `An error occurred in the delay test`，
curl 经它访问返回 **502**，服务端日志里没有任何认证失败记录。
2026-11-09 实测就踩了这次：直接拿 `clients.config.shadowsocks16.password`
（那是**用户 psk**，不是完整凭据）去填 mihomo，四个节点里只有 SS 不通。
正确的完整凭据要从订阅里的 `ss://` 链接解出来：

```bash
# ss://base64(method:server_psk:user_psk)@host:port
echo 'MjAyMi1ibGFrZTMtYWVzLTEyOC1nY206WFhYWDpZWFhY' | base64 -d
# → 2022-blake3-aes-128-gcm:XXXX:YYYY
```

#### 6.9.5 SS 默认同时监听 tcp 和 udp

sing-box 的 shadowsocks 入站默认开 UDP relay，`ss -lntu` 里会看到
`tcp *:2087` **和** `udp *:2087` 两个 socket。

如果防火墙只放行了 tcp，UDP 那个 socket 白开着、静默丢包 —— 表现为
「网页能开，但客户端开 UDP 转发后某些应用不通」。
**tcp 和 udp 都要放行**（`setup_base.sh` 的 `SS_PORT` 已经两个都写）。

#### 6.9.6 `salt not unique` 不是服务端问题

高并发拨测时服务端日志会出现：

```
inbound/shadowsocks[...] process connection from x.x.x.x:port:
shadowsocks: serve TCP from ...: salt not unique
```

这是**客户端重复使用同一个 salt**（SS2022 的防重放机制触发）。
16 字节随机 salt 正常不会撞，所以基本是客户端实现的锅。
偶发几条无所谓；如果频繁出现导致连接失败，换客户端版本或换加密方式，不要改服务端。

---

### 6.10 Hysteria2 端口跳跃：几乎零成本，但订阅不会带你配

**为什么值得开**：UDP 被限速时，运营商/机房通常是按**单端口**做 QoS 的。
TUIC 就是死在 2083 一个端口上（§6.8）。Hy2 如果哪天也掉到几百 KB/s，
换端口的代价比换协议小得多 —— 前提是你早就把跳跃配好了。

**服务端**（`setup_base.sh` 已包含，变量 `HY2_HOP_RANGE`）：

```bash
# 把整段 UDP 端口重定向到真正的监听端口
-A INPUT      -p udp -m udp --dport 58888:60888 -j ACCEPT
-A PREROUTING -p udp -m udp --dport 58888:60888 -j REDIRECT --to-ports 2083
```

REDIRECT 是 NAT 表操作，**必须放 nat 表**，写进 filter 表不会报错但完全不生效。
`netfilter-persistent save` 之后重启也在。

**客户端**（订阅里**没有**这一段，必须手改一次）：

```yaml
# mihomo / Clash Meta
- name: JP-Hysteria2
  type: hysteria2
  server: jp.example.com
  ports: 58888-60888        # 注意：是 ports，不是 port
  hop-interval: 30          # 秒，默认 30
  password: "******"
  sni: jp.example.com
```

```json
// sing-box
{ "type": "hysteria2", "server": "jp.example.com",
  "server_ports": ["58888:60888"], "hop_interval": "30s", "password": "******" }
```

**实测代价为零**：固定端口 198ms / 78.4 Mbps，跳端口 207ms / 77.4 Mbps，
差异在噪声内。把 `hop-interval` 压到 5 秒跑完 100MB（11.6s，跨 2 次换端口）
也没断流 —— QUIC 的连接迁移确实生效了。

**验证方法**：光看 `ss -lunp` 是看不到跳跃端口的（内核在 nat 表里转发，
不产生新 socket）。要从**客户端**连一次，或者看 conntrack：

```bash
conntrack -L -p udp --dport 2083 2>/dev/null | head
```

### 6.11 Naive 能跑，但会毒掉 sing-box 订阅

S-UI 1.6.7 的入站类型里有 `naive`，sing-box 1.13+ 也原生支持，所以**技术上能建**。
2026-11-09 实测：207ms / 26.8 Mbps（短窗口），性能和 Hy2 持平，伪装性最强
（走的是真 Chromium 的 HTTP/2 栈）。**但不要部署**，两个原因：

**① 建入站会报 `save: missing users`。**
S-UI 的入站模型没有 users 字段（`database/model/inbounds.go` 里
`Options json:"-"`），用户全部从 `clients.config[<type>]` 派生
（`service/inbounds.go: fetchUsers`）。所以顺序必须是：

```
1. 先给客户端写 config.naive = {"username": "...", "password": "..."}
2. 再 POST /api/save，object=inbounds，action=new，
   额外带一个表单字段 initUsers=<客户端 id>
3. 最后把新入站 id 追加进 clients.inbounds
```

少任何一步都失败。注意 naive 的用户字段是 `username`，其他协议是 `name`。

**② 它会污染 sing-box JSON 订阅，导致整份配置解析失败。**

加了 naive 之后，`?format=json` 的 outbounds 里多出一条 `type: naive`：

```json
["selector","urltest","direct","anytls","hysteria2","vless","shadowsocks","naive"]
```

而**官方发行的 sing-box 客户端没有编译 `with_naive_outbound`**（这是个 build tag），
遇到未知 outbound 类型会直接报错 —— 整份订阅废掉，不只是这一个节点不生效。
Clash 格式不受影响（S-UI 生成 Clash 时会跳过 naive），所以问题只在
Hiddify / Karing / NekoBox 这类吃 JSON 订阅的客户端上出现，**很难联想到是 naive 干的**。

**结论**：naive 的瓶颈从来不是性能，是客户端。mihomo / sing-box / v2rayN
都不支持它，只能用 `naive.exe` 或 Chrome 插件。真要当预案，单独起一个
端口、**不要把客户端关联进去**（不关联就不会进订阅）。

## §7 验证方法

### 7.1 三层验证，缺一不可

1. **服务端**（`verify_nodes.sh`）：服务在跑、端口在听、users 注入、订阅可读
2. **外部可达**（本机 TCP 探测）：防火墙放行、端口没被封
3. **协议可用**（本机真实客户端拨号）：DNS、证书、握手、认证全链路

只做第 1 层是最常见的误判来源。

### 7.2 用出口 IP 判断是否真的走了代理

```bash
curl -s -x http://127.0.0.1:7899 https://api.ipify.org
# 应等于 VPS 公网 IP
```

比 ping 或看客户端"已连接"可靠得多。

### 7.3 测三次，不要测一次

网络抖动、连接复用、DNS 缓存都会造成单次误判。建议每个节点连续 3 次，
3/3 才算通过。

### 7.4 本地测 mihomo 配置时注意三件事

- 订阅生成的 YAML **本身已含 `rules` 段**，不要再追加第二个 `rules:`（重复 key 报错）
- `tun.enable: true` 在 Windows 非管理员下会失败，本地测试时改成 `false`
- `dns.enable: true` + `fake-ip` 在没有 tun 的情况下会干扰解析，测试时关掉

---

## §8 本机环境与工具链（Windows / Git Bash）

### 8.1 `py` 启动器会读脚本的 `#!` 行

`deploy_nodes.py` 开头是 `#!/usr/bin/env python`，Windows 的 `py.exe` 会照着
shebang 去找 `python` —— 找到的可能是一个**没装 paramiko** 的解释器，于是：

```
缺少 paramiko，无法建立 SSH 连接
当前解释器：C:\...\python\versions\3.13.12\python.exe
```

`py -c "import paramiko"` 明明能过，`py scripts/deploy_nodes.py` 却报缺包，就是这个原因。

**解法**：用装好依赖的解释器全路径执行，不要用 `py 脚本.py`：

```bash
"C:/Users/<you>/AppData/Local/Programs/Python/Python311/python.exe" scripts/deploy_nodes.py
```

### 8.2 没有 `bc`、没有 `/usr/bin/time`

Git Bash 里这两个都不存在。计时用 `date +%s.%N` 或直接 Python；
算术用 `awk`：

```bash
awk -v a=1.5 -v b=2 'BEGIN{printf "%.2f\n", a/b}'
```

### 8.3 `grep -c $'\r'` 测不出 CRLF

`grep -c` 数的是**行**，不是字符。要数字节：

```bash
tr -dc '\r' < file | wc -c
```

### 8.4 `pkill -f` / `pgrep -f` 会匹配到自己

`pkill -f "sleep 180"` 会把自己这条命令也匹配上，**连 SSH 会话一起杀掉**。
用方括号技巧规避：

```bash
ps -eo pid,args | grep -F "[s]leep 180"
```

### 8.5 MSYS 会把 `/path` 自动转成 Windows 路径

`curl -d "data=/inbounds/del/4"` 里的 `/inbounds/del/4` 会被改写成
`D:/.../PortableGit/inbounds/del/4`，请求直接 404。
加 `MSYS_NO_PATHCONV=1`，或者干脆把这类调用封进 Python 子命令里
（本 skill 的 `sui_api.py` 就是 Python 实现，绕开了这个问题）。

### 8.6 本机装了系统代理，`sui_api` 会 502

这台机器上 `HTTPS_PROXY` 是全局设的（Clash Verge 的混合端口，不是 10808）。
Python 的 `urllib` **默认读环境变量**，于是访问面板时被塞进代理，
代理不认识这个域名/端口，返回：

```
urllib.error.URLError: <urlopen error Tunnel connection failed: 502 Bad Gateway>
```

而 `curl` 加了 `-x` 时不受影响，所以「curl 能通、脚本不通」很容易误判成面板问题。

**修法**：在脚本里显式清掉，别指望 `no_proxy`：

```python
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)
```

反向的坑也要注意：**测速时要清掉 `--noproxy '*'`**，否则 curl 会走本机代理，
量到的是别的线路的带宽（表现为 connect=0.000000s，那是连到 127.0.0.1 了）。

### 8.7 `curl -o /dev/null` 在 Windows 上会被当成文件路径

从 Python `subprocess` 直接调 `curl.exe`（不经过 bash）时，`/dev/null`
是**字面路径**，Windows 上没有这个文件，curl 返回：

```
rc 23 (CURLE_WRITE_ERROR)，stdout 里的 %{speed_download} 是 0
```

表现为「测速全是 0.0 Mbps」但请求其实是通的。用 `os.devnull`
（Windows 上是 `nul`）代替：

```python
NULL = os.devnull
subprocess.run(["curl", "-o", NULL, "-w", "%{speed_download}", url])
```
