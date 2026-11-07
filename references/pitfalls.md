# 踩坑与修复

> 全部来自实际部署。每条都写清「现象 → 根因 → 修复」，
> 方便对照排查，而不是让你从头试。

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

### 3.5 删除对象传裸 id

`del` 的 data 是裸数字，不是 `{"id": n}`：

```bash
--data-urlencode "data=7"          # 对
--data-urlencode 'data={"id":7}'   # 错：cannot unmarshal object into Go value of type uint
```

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

---

## §4 证书

### 4.1 一份证书服务多个组件

`/root/cert/<域名>/{fullchain.pem,privkey.pem}` 同时被面板、订阅、AnyTLS、
TUIC、Hysteria2 引用。**删掉会同时挂掉五个地方。**

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

---

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
