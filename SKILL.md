---
name: s-ui-proxy-skill
description: 在全新 VPS 上部署 S-UI（sing-box 内核）面板与抗封锁代理节点（AnyTLS / Hysteria2 / VLESS-Reality / Shadowsocks 2022，可选 TUIC），覆盖非交互安装、Let's Encrypt 证书、iptables 白名单、BBR 调优、面板 API 建站、三格式订阅与端到端真机验证。当用户要求「搭梯子 / 部署机场节点 / 装 S-UI / 配 AnyTLS、Hysteria2、Reality、Shadowsocks / 照教程部署代理 / 自建科学上网 / 从 3x-ui 迁移到 S-UI」时使用。Deploy a censorship-resistant proxy stack (S-UI panel + sing-box, with AnyTLS, Hysteria2, VLESS-Reality and Shadowsocks 2022) on a fresh VPS with non-interactive install, ACME certs, iptables hardening and end-to-end verification.
metadata:
  description_zh: 在 VPS 上部署 S-UI（sing-box）面板与 AnyTLS / Hysteria2 / Reality / Shadowsocks 节点
  description_en: Deploy an S-UI (sing-box) panel with AnyTLS, Hysteria2, VLESS-Reality and Shadowsocks 2022 nodes
  version: 1.1.0
---

# S-UI 代理节点部署

## 用途

把一台**全新的 Linux VPS** 变成一套可用的代理服务：S-UI 管理面板 + 四种互补的代理协议节点，
并输出可直接导入客户端的分享链接与三格式订阅地址。

| 产物 | 说明 |
|---|---|
| S-UI 面板 | 随机路径 + 随机端口 + Let's Encrypt 证书（HTTPS），内嵌 sing-box |
| 节点 1：AnyTLS | TCP 8443，解决 TLS-in-TLS 指纹，自带连接复用，日常主力 |
| 节点 2：Hysteria2 | UDP 2083，高丢包链路吞吐最强，带 salamander 混淆 |
| 节点 3：VLESS + Reality + Vision | TCP 2053，抗主动探测，可作保底 |
| 节点 4：Shadowsocks 2022 | TCP 2087，无 TLS 握手、延迟最低，客户端兼容面最广 |
| （可选）TUIC v5 | UDP 8443，`TUIC_PORT` 填了才部署 —— 见下方「TUIC 默认关闭」 |
| 三条订阅 | 通用 base64 / Clash YAML / sing-box JSON，同一客户端身份天然合并 |
| 交付文档 | 面板凭据、节点参数、兼容性矩阵、防火墙、运维命令、实测数据 |

## 何时使用

- 用户给了 VPS 凭据，要求搭代理、部署节点、"搭梯子"、自建科学上网
- 用户要求安装 / 配置 S-UI、sing-box、AnyTLS、Hysteria2、TUIC、Reality、Shadowsocks
- 用户想把 3x-ui / x-ui 换成 sing-box 系面板
- 用户提供一份代理部署教程并要求照着做
- 用户要求为已有面板新增节点、排查节点连不上

**不适用**：客户端软件界面操作、纯 DNS 分流规则、商业机场选购。

## ⚠️ 第一条原则：避开 443

**这不是偏好，是实测结论。** 2026-11 实测数据：

| 节点 | 结果 |
|---|---|
| VLESS-Reality @ **443**/TCP | `REALITY authentication failed` |
| Hysteria2 @ **443**/UDP | 全部 `context deadline exceeded` |
| Shadowsocks @ 2087/TCP | 3/3 通过（无 TLS 握手，受 443 干扰影响最小） |

同时 VPS 的 443 端口**未认证握手能正常返回上游真证书**，说明服务端没问题 ——
是 443 端口被针对性干扰。**把 Reality 换到 2053、Hysteria2 换到 2083，立刻 3/3 全通。**

所以本 skill 的所有默认端口都避开 443：AnyTLS 8443/TCP、Hysteria2 2083/UDP、
Reality 2053/TCP、Shadowsocks 2087/TCP。详见 `references/pitfalls.md` §1。

排查用户"节点突然不能用了"时，**先做这个对照实验**（换端口重试），
不要一上来就怀疑协议失效或重装面板。

## TUIC 默认关闭

2026-11 在一台日本 VPS 上实测：TUIC 能连上、能认证、延迟也正常，
但吞吐只有 **0.2~0.4 Mbps**，图片和视频基本打不开。

换端口（8443 → 2084）、换客户端（mihomo → sing-box 1.14.4 官方）都是同样结果；
服务端日志显示连接在同一秒内转发出去，`UdpRcvbufErrors` 增量为 0 ——
**不是端口被封、不是客户端问题、不是服务端卡住**，是这条路径对明文 QUIC 做了限速。
同一台机器上的 Hysteria2 几十 Mbps，因为它带 `salamander` 混淆。

所以本 skill 默认**不部署 TUIC**，改用 Shadowsocks 2022 作为 TCP 系补充：
无 TLS 握手、握手往返最少、客户端兼容面比 AnyTLS 还广（Xray / Surge / QuantumultX 都认）。

用户明确要 TUIC 时：`TUIC_PORT` 填个端口即可，但**先告知上面这个实测结论**，
别让人装完发现慢再回来排查。详见 `references/pitfalls.md` §8。

## 前置条件

1. VPS 公网 IP、SSH 端口、用户名、密码或密钥
2. 一个指向 VPS 的完整域名（面板、证书、订阅都要用）；没有时按 `references/domain-and-dns.md` 引导
3. 期望的面板端口 / 订阅端口（未指定则默认面板 46821、订阅 2096）

## 工作流

按顺序执行，**每一步验证通过再进入下一步**。

### 准备向导 · 域名与 DNS

先确认用户是否已有域名和 DNS 权限。没有则读 `references/domain-and-dns.md`，
引导免费域名申请 → Cloudflare 可选接入 → NS 激活 → A 记录指向 VPS（**灰云**）。

注册验证由用户在官方页面完成；不要索取验证码，不要把账号注册说成全自动步骤。

```bash
python scripts/check_dns.py --domain us.example.com --ipv4 203.0.113.10
```

预检不修改 DNS、不读 deploy.env。默认要求无 AAAA（否则代理出口会变 IPv6）。
退出码非零时先解决诊断问题再继续。

### Step 0 · 准备参数

```bash
cp examples/deploy.env.example deploy.env
$EDITOR deploy.env
```

所有脚本都从 `deploy.env`（或同名环境变量）读配置。
**行内注释会被正确剥离**（`ANYTLS_PORT=8443  # TCP` 解析为 `8443`），可以放心加注释。

### Step 1 · 探测环境

```bash
python scripts/ssh_run.py -c "cat /etc/os-release | head -3; uname -m; free -m | head -2; df -h / | tail -1"
python scripts/ssh_run.py -c "systemctl is-active s-ui 2>/dev/null || echo 's-ui not installed'"
```

记录：发行版（影响防火墙方案）、架构（影响二进制选择）、内存。

### Step 2 · 系统基线

```bash
python scripts/ssh_run.py -f scripts/setup_base.sh
```

五件事：设时区 → 装依赖 → iptables 白名单 → 屏蔽 IPv6（可选）→ 启用 BBR。

⚠️ **不要用 UFW**：Debian 12+ 上 `ufw` 与 `iptables-persistent` 互斥，
装了会把后者卸掉并留下残缺规则。用纯 iptables。

IPv6 默认屏蔽（`DISABLE_IPV6=1`）：sing-box 的域名解析会优先拿 AAAA，
VPS 有 IPv6 时代理出口就变 IPv6，容易被针对性封锁。脚本会先把 DNS 换成纯 IPv4
再关 IPv6，并做一次自检，失败自动回滚。

### Step 3 · 安装面板 + 申请证书

```bash
python scripts/ssh_run.py -f scripts/install_sui.sh
```

**不走官方 install.sh**（它是交互式的，问语言、端口、路径、账号）。
本脚本改成「下 release 包 → 解压 → 改 DB 里的 settings → 重启」，每步可回读校验。

自动完成：
1. 下载 `s-ui-linux-<arch>.tar.gz` 并解压到 `/usr/local/s-ui`
2. 首次启动生成 `db/s-ui.db`（默认端口可能冲突，起不来是正常的）
3. 直接改 settings 表：面板端口/路径、订阅端口/路径、时区、订阅格式开关
4. acme.sh 申请 Let's Encrypt 证书，写入面板与订阅的 HTTPS 配置
5. 重启并改掉默认的 `admin/admin`

结果落在 `/usr/local/s-ui/install-result.env`，脚本会打印。

### Step 4 · 创建节点

```bash
python scripts/deploy_nodes.py
```

幂等：已存在的 TLS 对象 / 入站 / 用户会跳过，不会重复创建或覆盖凭据。
脚本既是本地编排器也是远端执行体 —— 本地跑时把自己上传到 VPS 再以 `--local` 执行，
所有 API 调用都在 VPS 本机完成。

创建内容：
- 2 个 TLS 对象：域名证书（LE）、`reality-<域名>`。
  只有真的部署 TUIC 时才会额外创建 `tuic-<域名>`（**必须带 alpn h3**）
- 4 个入站（AnyTLS / Hysteria2 / Reality / Shadowsocks 2022），全部带 `addrs` 与 `out_json`。
  Shadowsocks 的 `method` / `password` 放在入站**顶层**，不是 `options`（见 pitfalls §3.10）
- 1 个订阅用户，一个身份绑定全部入站（S-UI 天然一条订阅出全部节点，不需要合并脚本）

节点集是可配的：`TUIC_PORT` / `SS_PORT` 留空就不生成对应节点，
`setup_base.sh` 的防火墙规则也跟着走 —— 不会出现「建了节点但端口没开」的半成品。

Reality 密钥对用 `openssl genpkey -algorithm X25519` 生成，
取 PKCS8/SPKI DER 的末 32 字节转 base64url —— 与 Xray 的 `privateKey` 格式一致。

### Step 5 · 端到端验证

```bash
python scripts/ssh_run.py -f scripts/verify_nodes.sh
```

检查 15 项：服务状态、各节点端口监听（SS 的 tcp+udp 两个都查）、
每个入站的 users 注入、SS 的 method 与 psk 字节数、TUIC 的 alpn（有才查）、
三格式订阅可读、Clash YAML 含 `udp: true`、订阅响应头、证书有效期。

⚠️ **这个脚本验证不了防火墙**：VPS 访问自己走 `lo`，不经过 INPUT 链。
外部可达性必须从本机用真实客户端拨号，或对每个端口做外部 TCP 探测。

### Step 6 · 本机真机拨测（必做）

在**本机**（中国大陆）用真实客户端逐个节点拨号，这是唯一能同时验证
DNS、防火墙、证书、协议配置的方法。

```bash
# 1. 拉订阅
curl -s "https://<域名>:2096/<subPath>/<subId>?format=clash" -o sub.yaml

# 2. 用 mihomo / Clash Verge 加载，逐个节点测出口 IP
```

判据：出口 IP 等于 VPS 公网 IP 即为通过。建议每个节点跑 3 次。

### Step 7 · 输出交付文档

```bash
python scripts/render_report.py -o .
```

生成 `VPS-<域名>-部署信息.md` 与 `.html`（带复制按钮），数据全部来自实测回读。

**兼容性矩阵必须整张给出**，不能只写一行：

| 客户端 / 内核 | AnyTLS | Hysteria2 | VLESS-Reality | Shadowsocks |
|---|:--:|:--:|:--:|:--:|
| sing-box ≥1.12（Hiddify / Karing / NekoBox） | 是 | 是 | 是 | 是 |
| mihomo / Clash Meta ≥1.19（Verge Rev / FlClash） | 是 | 是 | 是 | 是 |
| Xray 系（v2rayN / v2rayNG 默认内核） | 否 | 否 | 是 | 是 |
| Shadowrocket（≥2.2.65） | 是 | 是 | 是 | 是 |
| Surge | 否 | 是 | 否 | 是 |
| QuantumultX | 否 | 否 | 否 | 是 |

（这张表由 `render_report.py` 按**实际入站类型**动态生成，加协议不用改渲染代码。）

只写一半的话，用 Xray 内核的人会以为节点全挂了。详见 `references/pitfalls.md` §6。

## 关键约束

1. **所有节点避开 443**（见上）。用户坚持要 443 时，先做对照实验说明风险。
2. **TUIC 默认不部署**：实测在部分线路上被限速到 0.2~0.4 Mbps，换端口换客户端都一样
   （pitfalls §8）。要开就把 `TUIC_PORT` 填上，且**必须用独立 TLS 对象带 `alpn:["h3"]`**，
   与 AnyTLS 共用会报 `tls: server did not select an ALPN protocol`。
3. **保存 clients 必须带 `"links":[]` 和 `inbounds`**，否则报
   `save: unexpected end of JSON input`（Go 侧对 nil 做 Unmarshal，报错完全误导）。
4. **`GET /api/clients` 不返回 `config`**，edit 前必须从 DB 补全，否则凭据被清空，
   随后报 `converting NULL to string is unsupported`。
5. **改 `webPort` / `webPath` / `webCertFile` 后必须 `systemctl restart s-ui`** ——
   S-UI 不会自动重启，settings 已经写进 DB 了，极易误判成"改了没生效"。
6. **前端 API 必须带 `Origin` 头**，否则 CSRF 拦成 403 且响应体为空。
7. **访问面板要用域名**（或 `curl --resolve` 钉到 127.0.0.1）。直接用 127.0.0.1 会因
   SNI 不匹配被拒：`tls: unrecognized server name`。
8. **不要删 `/root/cert/`** —— 面板、订阅、AnyTLS、Hysteria2 共用（SS 和 Reality 不用）。
9. **新增入站 sing-box 会自动热加载**，不需要重启；但改面板自身的端口/路径要重启。
10. **不要用 `pkill -f` / `pgrep -f` 匹配自己的脚本**（会匹配到脚本自身命令行导致自杀）。
11. **删入站要连 `clients.config` 一起清**，否则订阅里继续生成死节点（pitfalls §3.11）。
   删入站的 `del` 传的是 **tag 字符串**，不是 id（pitfalls §3.5）。
12. **SS2022 的客户端 config 键是 `shadowsocks16`**，服务端 `options` 必须同时有
   `method` 和 `password`，否则核心整个起不来（pitfalls §9）。
13. 涉及删除、覆盖、改端口等破坏性操作前，先向用户说明并取得确认。

## 参考资料

按需加载，不要一次性全读：

| 文件 | 何时读 |
|---|---|
| `references/pitfalls.md` | **每次部署都读** —— 汇集了实际踩过的坑与修复方式 |
| `references/sui-api.md` | 需要直接调面板 API 时（两套 API、鉴权、对象结构） |
| `references/protocols-and-clients.md` | 选协议、排查"某客户端连不上"、写兼容性矩阵时 |
| `references/install-env.md` | 需要自定义安装参数（版本、端口、证书路径）时 |
| `references/domain-and-dns.md` | 未准备域名、接入 Cloudflare、DNS 预检失败时 |

## 脚本清单

| 脚本 | 作用 |
|---|---|
| `scripts/check_dns.py` | 只读检查公共 DNS 的 A/AAAA 是否符合预期 |
| `scripts/ssh_run.py` | 通用 SSH 执行器（跑单条命令或整个脚本，自动注入 deploy.env） |
| `scripts/sui_api.py` | 面板 API 客户端（Cookie 认证 + Origin 头；也可当 CLI 用） |
| `scripts/setup_base.sh` | 系统基线：时区、依赖、iptables 白名单、屏蔽 IPv6、BBR |
| `scripts/install_sui.sh` | 非交互安装 S-UI + ACME 证书 + 改默认密码 |
| `scripts/deploy_nodes.py` | 创建 4 个入站 + 订阅用户（幂等，自动生成凭据） |
| `scripts/verify_nodes.sh` | 端到端验证（15 项检查，明确标注验证不了防火墙） |
| `scripts/render_report.py` | 生成交付文档 `.md` / `.html`（数据实测回读） |

### 典型执行顺序

```bash
cp examples/deploy.env.example deploy.env && $EDITOR deploy.env

python scripts/check_dns.py --domain <域名> --ipv4 <IP>
python scripts/ssh_run.py -c "cat /etc/os-release | head -3; uname -m"
python scripts/ssh_run.py -f scripts/setup_base.sh
python scripts/ssh_run.py -f scripts/install_sui.sh
python scripts/deploy_nodes.py
python scripts/ssh_run.py -f scripts/verify_nodes.sh
# 本机用真实客户端逐个节点拨测
python scripts/render_report.py -o .
```

## 从 3x-ui 迁移

1. **先备份**：`cp /etc/x-ui/x-ui.db /root/backup-3x-ui-$(date +%Y%m%d-%H%M)/`，
   连同 `/root/cert` 与 `iptables-save` 一起。
2. 装 S-UI 时先用**不冲突的端口**（面板 2095、订阅 2097），验证面板能起。
3. `deploy_nodes.py` 会避开 3x-ui 已占用的端口；确认新节点可用后再停 3x-ui：
   `systemctl disable --now x-ui`
4. 把 S-UI 的端口改回正式值，`systemctl restart s-ui`。
5. 回退只需：`systemctl enable --now x-ui && systemctl disable --now s-ui`。

## 风格要求

- 全程**边做边验证**，每步都回读确认，不要"发完命令就假定成功"。
- 所有凭据最终必须落到交付文档里，不要让用户去面板里自己找。
- 遇到与教程不一致的地方，**按实际系统版本修正并在文档里说明差异**。
- 排查问题先做对照实验（换端口、换客户端、换网络），不要凭猜测改配置。
