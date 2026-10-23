# 3xui-proxy-skill

一个通用的 **Agent Skill**（`SKILL.md` 约定）：把一台全新的 Linux VPS 变成一套可用的代理服务
—— 3x-ui 管理面板 + 三种互补的抗封锁协议节点（VLESS-REALITY / Hysteria2 / TUIC v5），
并输出可直接导入客户端的分享链接与订阅地址。

> 这不是又一个"复制粘贴命令"的教程。
> 它把**真实部署中踩到的几十个坑**（系统版本差异、面板 API 陷阱、机房端口封锁、
> gRPC 热更新陷阱、客户端内核破坏性变更）固化成了可复用的流程与脚本。

**三件套为什么是这三个**：REALITY 走 TCP 443，UDP 被封锁时是唯一活路；
Hysteria2 走 UDP 443 + 端口跳跃，高丢包链路吞吐最强；TUIC 走 UDP 8443，建连最快。
三者互补，客户端按网络情况切换。

---

## 目录

- [它能解决什么](#它能解决什么)
- [安装](#安装)
- [快速开始](#快速开始)
- [使用](#使用)
- [客户端兼容性速查](#客户端兼容性速查)
- [目录结构](#目录结构)
- [依赖](#依赖)
- [开发与验证](#开发与验证)
- [安全说明](#安全说明)

---

## 它能解决什么

| 常见坑 | 本 skill 的处理 |
|---|---|
| 教程用 UFW，但 Debian 13 上 UFW 与 iptables-persistent 互斥，照抄会卸载 UFW 并留下残缺规则 | 改用纯 iptables + netfilter-persistent |
| 教程写 `/etc/sysctl.conf`，Debian 13 已废弃该文件 | 改用 `/etc/sysctl.d/*.conf` |
| 面板 API 突然全部 403，响应体为空 | 根因是 `webDomain` 的 Host 头校验，脚本自动带 `Host:` |
| 分享链接里主机名变成 `localhost` | 设置入站 `shareAddrStrategy=custom` + `shareAddr=域名` |
| Hysteria2 链接 `sni=` 为空 | 删除 `tlsSettings.settings.serverName` 空字段 |
| 三个节点只有三条订阅 | 用「一个客户端绑定多入站」合并为一条 |
| 在服务器上自测"通了"，但外面连不上 | VPS 访问自身公网 IP 走 `lo`，绕过 INPUT 链 —— 必须从外部验证 |
| 某客户端连不上 REALITY | 已知的 Xray ≥26.9.8 破坏性变更，文档给出完整兼容性矩阵 |
| 代理**出口 IP 是 IPv6**，但需要固定 IPv4 出口 | 根因是 Xray `domainStrategy=AsIs` + 系统有 IPv6；`setup_base.sh` 可一键屏蔽 IPv6（**先换 DNS 再关**，带断网自动回滚） |
| TUIC 节点**时通时不通**，重连就失败 | 3x-ui TUIC 认证要求 `HandshakeComplete`，0-RTT 连接必然认证失败；`fix_tuic_0rtt.py` 一键关闭 0-RTT |
| **v2rayN / v2rayNG 导入 TUIC 后一直测速超时** | **Xray 内核根本没有 TUIC/Hysteria2**。v2rayN 切 sing-box 内核即可；v2rayNG 无内核切换，只能用 REALITY |
| 查二进制"确认协议是否支持"，结果**所有协议都是 0 命中** | Debian 默认没装 `strings`（属于 `binutils`），命令静默失败。改用 `grep -a -o -i -- <关键字> <bin> \| wc -l` |
| 增删入站后重启 Xray，**REALITY 与 Hysteria2 一起挂掉** | v3.9.0 增删入站走 Xray 的 gRPC API 热更新，**不重写 `bin/config.json`**；该文件留着旧入站，重启即端口冲突。改完入站必须 `systemctl restart x-ui` |
| 以为 TUIC 不在 Xray 里就是"没装" | TUIC v5 由 **`x-ui` 面板进程自己承载**（转发给内部 socks 入站 127.0.0.1:64003），`xray-linux-amd64` 里查不到属正常 |
| 想加 AnyTLS 节点 | **3x-ui v3.9.0 不支持**（`xray-linux-amd64` 与 `x-ui` 二进制里 `anytls` 均 0 命中），只有 sing-box / mihomo 实现 |
| 装完 API Token 读不出来 | v3.9.0 的 `api_tokens` 表**只存 SHA-256**，明文仅创建时返回一次。脚本改用「登录会话 + `POST /panel/api/setting/apiTokens/create`」获取 |
| Windows 上 `git clone` 后 `.sh` 脚本报 `$'\r': command not found` | Git 的 `core.autocrlf` 把脚本换成了 CRLF。仓库已加 `.gitattributes` 强制 `*.sh`/`*.py` 用 LF；老工作区若仍是 CRLF，用 [`pitfalls.md` §1.5.1](references/pitfalls.md) 的手法重写 |
| 在 Git Bash 里调面板 API，端点参数 `/inbounds/del/4` 返回 **404**，同一端点写在 Python 里却 200 | MSYS 把以 `/` 开头的参数转成了 Windows 路径。改用 `xui_api.py del-inbound 4`，或设 `MSYS_NO_PATHCONV=1`（§1.6） |
| 测新代理**时通时不通**，以为服务端不稳 | 本机开着的代理客户端会截胡 `curl -x`。先用 TCP 裸探测（`/dev/tcp`）连续验证再下结论 |

完整的「现象 → 根因 → 修复」见 [`references/pitfalls.md`](references/pitfalls.md)。

---

## 安装

本仓库遵循 **Agent Skills** 约定：根目录一份 `SKILL.md`（YAML frontmatter + Markdown 正文），
配套 `scripts/`（可执行脚本）与 `references/`（按需加载的参考文档）。
**任何支持该约定的 agent 运行时都能直接加载，不需要额外适配。**

把仓库克隆到你所用的运行时的 skills 目录即可（把 `<skills-dir>` 换成实际路径）：

```bash
git clone https://github.com/yezi700/3xui-proxy-skill.git \
  <skills-dir>/3xui-proxy-skill
```

不同运行时的 skills 目录位置不一（常见形态是 `~/.<runtime>/skills/` 或
`~/.config/<runtime>/skills/`），**以你所用的运行时文档为准** —— 只要该运行时支持
`SKILL.md` 约定，放进它的 skills 目录就能被识别。

Windows（PowerShell）：

```powershell
git clone https://github.com/yezi700/3xui-proxy-skill.git `
  "<skills-dir>\3xui-proxy-skill"
```

放好后重开会话即可。运行时会读取 `SKILL.md` 的 `description`，
在识别到"部署代理节点"类请求时自动加载。

> 也支持把本仓库作为**普通目录**分发：拷贝整个文件夹到 skills 目录同样生效，
> 不依赖任何特定的安装器或包管理器。

### 不想装成 skill？直接当脚本用也行

`scripts/` 下都是独立的命令行工具，不依赖 agent 运行时：

```bash
pip install paramiko                      # 唯一的外部依赖

cp examples/deploy.env.example deploy.env && $EDITOR deploy.env
python scripts/check_dns.py --domain jp.example.com --ipv4 203.0.113.10
python scripts/ssh_run.py -f scripts/setup_base.sh
```

### 打包分发

```bash
# 只打包 git 已跟踪的文件（不会带入 .git / __pycache__ / deploy.env）
git archive --format=zip --prefix=3xui-proxy-skill/ \
  -o ../3xui-proxy-skill.zip HEAD
```

> ⚠️ **Windows 用户**：仓库根目录的 `.gitattributes` 已强制 `*.sh` / `*.py` 使用 LF 换行，
> 克隆后脚本可以直接传到 Linux 执行。若你的 Git 全局设了 `core.autocrlf=true`
> 且克隆的是旧版本，先 `git pull` 拉取 `.gitattributes`，
> 或参考 [`references/pitfalls.md`](references/pitfalls.md) §1.5 就地转换。

---

## 快速开始

### 第 0 步：先准备域名

还没有域名或不会解析？先看
[域名申请、Cloudflare 接入与 DNS 引导](references/domain-and-dns.md)：
申请免费域名 → 注册 Cloudflare（可选）→ 修改 NS → 添加指向 VPS 的 A 记录（灰云）→ DNS 预检。
已有可用域名时直接检查解析，无需重新注册或迁移 DNS。

```bash
# 替换为实际完整域名和 VPS 公网 IPv4；示例 IP 不可用于部署
python scripts/check_dns.py --domain jp.example.com --ipv4 203.0.113.10
```

预检仅使用 Python 标准库，无需凭据，不改 DNS；默认要求没有 AAAA。
免费域名可能需要续期，实际规则以服务商说明为准。

### 第 1 步：填参数

```bash
cp examples/deploy.env.example deploy.env
$EDITOR deploy.env
```

`deploy.env` 是**唯一**的配置入口，所有脚本都从这里（或同名环境变量）读取。
它已在 `.gitignore` 中排除 —— **里面有明文凭据，不要提交、不要外传**。

### 第 2 步：按顺序跑

```bash
python scripts/ssh_run.py -c "cat /etc/os-release | head -3; uname -m"
python scripts/ssh_run.py -f scripts/setup_base.sh        # 系统基线 + 屏蔽 IPv6
python scripts/ssh_run.py -f scripts/install_3xui.sh      # 装面板 + 证书，记下 API_TOKEN
python scripts/deploy_nodes.py                            # 建 3 个节点
python scripts/fix_tuic_0rtt.py                           # 关闭 TUIC 0-RTT
python scripts/merge_subscription.py                      # 合并成一条订阅
python scripts/ssh_run.py -f scripts/verify_nodes.sh      # 端到端拨号验证
python scripts/render_report.py -o .                      # 生成交付文档
```

> `install_3xui.sh` 会打印 **API Token**，把它回填到 `deploy.env` 的 `API_TOKEN` 再继续。
> 该 Token 在 3x-ui v3.9.0 里**只在创建时返回一次**（库里只存 SHA-256），务必立即保存。

---

## 使用

装成 skill 后，在对话里直接说需求即可：

> 帮我在这台 VPS 上部署 3x-ui 和 Reality / Hysteria2 节点：`1.2.3.4` `22` `root` `密码`，
> 域名 `jp.example.com` 已解析

skill 会先按需引导域名准备与 DNS 预检，再按 Step 0→8 执行：
探测环境 → 系统基线 → 装面板 + 证书 → 建节点 → 合并订阅 → 放行端口 → 端到端验证 → 生成交付文档。

也可以只调用其中一步，例如"给这个面板再加一个 TUIC 节点"、
"这个客户端连不上 TUIC，帮我看看"。

**不适用**：客户端软件的界面操作（v2rayN / Shadowrocket 怎么点）、
纯 DNS 分流规则、商业机场选购。

---

## 客户端兼容性速查

两条"连不上"的根因完全不同，别搞混：

| 客户端内核 | REALITY | Hysteria2 | TUIC | 怎么办 |
|---|---|---|---|---|
| **Xray**（v2rayN 默认 / v2rayNG） | ✅ | ❌ | ❌ | v2rayN 切 sing-box 内核；v2rayNG 只能用 REALITY |
| **sing-box**（Hiddify / Karing / NekoBox / 旧 Shadowrocket） | ❌ | ✅ | ✅ | 用 Hy2 / TUIC |
| **mihomo**（Clash Verge / Clash Meta） | ✅ | ✅ | ✅ | 无需调整，最省心 |

- **sing-box 系连不上 REALITY**：Xray-core ≥ 26.9.8 起 REALITY 要求客户端
  ClientHello 携带 `X25519MLKEM768`（后量子混合密钥交换）。
- **Xray 系连不上 Hysteria2 / TUIC**：Xray-core **根本没有实现**这两个协议。

详见 [`references/protocols-and-clients.md`](references/protocols-and-clients.md)。

---

## 目录结构

```
3xui-proxy-skill/
├── SKILL.md                              # 主流程（agent 入口）
├── README.md
├── LICENSE
├── .gitignore
├── .gitattributes                        # 强制 .sh/.py 用 LF（Windows 克隆不踩 CRLF 坑）
├── references/                           # 按需加载，不要一次性全读
│   ├── domain-and-dns.md                 # 免费域名、Cloudflare、A/AAAA 与预检
│   ├── pitfalls.md                       # 踩坑大全（核心价值，每次部署都该扫一遍）
│   ├── xui-api.md                        # 3x-ui API 备忘（鉴权、字段、客户端模型）
│   ├── protocols-and-clients.md          # 协议选型 + 客户端兼容性矩阵
│   └── install-env.md                    # 3x-ui 非交互安装参数
├── scripts/
│   ├── check_dns.py                      # 只读 DNS 预检（仅标准库）
│   ├── ssh_run.py                        # 通用 SSH 执行器
│   ├── xui_api.py                        # 面板 API 客户端（也可当 CLI 用）
│   ├── setup_base.sh                     # 系统基线：时区/依赖/iptables/屏蔽 IPv6/BBR
│   ├── install_3xui.sh                   # 安装面板 + ACME 证书 + 获取 API Token
│   ├── deploy_nodes.py                   # 创建 Reality / Hysteria2 / TUIC 入站
│   ├── fix_tuic_0rtt.py                  # 关闭 TUIC 0-RTT（会先备份）
│   ├── merge_subscription.py             # 合并为一条订阅（一个客户端绑多入站）
│   ├── verify_nodes.sh                   # 端到端拨号验证（含 TUIC UDP relay 验证）
│   └── render_report.py                  # 生成交付文档 .md / .html
└── examples/
    └── deploy.env.example                # 参数模板
```

---

## 依赖

- **本地**：Python 3.8+、`bash`、可选 `git`
- **VPS**：Debian 11+ / Ubuntu 20.04+（x86_64 或 arm64），root 权限，
  能访问 GitHub 与 Let's Encrypt
- 不需要在本地安装 Xray / 3x-ui

```bash
pip install paramiko        # 唯一的外部 Python 依赖
```

---

## 开发与验证

```bash
python -m pip install paramiko
python -m unittest discover -s tests -v
python -m compileall -q scripts tests
```

测试在本地模拟 SSH 与面板响应，**不连接真实 VPS、不访问网络**。
Bash 行为测试在 Linux/macOS 使用系统 `bash`，Windows 使用 Git for Windows 的 Bash；
未安装时会跳过对应测试。建议在 Linux CI 中对 Python 3.8 / 3.11 / 3.13 运行上述完整测试。

几点设计约束：

- 部署入口 `deploy_nodes.py`、`merge_subscription.py` 在本地用 Python 直接运行，
  SSH 编排在内部完成；`ssh_run.py -f` 只用于 `.sh` 脚本。
- 部署与合并遇到失败会**停止并返回非零状态**；停止不等于自动回滚，
  重试前应先回读已有入站。
- 合并只选择配置端口对应的三个入站，并**保留原客户端**；
  从外部验证新订阅可用后，再按需清理旧身份。
- 新建 TUIC 默认关闭 0-RTT；`fix_tuic_0rtt.py` 用于修复早期版本建的入站。
- 改动入站后**必须 `systemctl restart x-ui`** —— v3.9.0 走 gRPC 热更新，
  不重启就不会重写 `bin/config.json`（见 `pitfalls.md` §2.6）。

---

## 安全说明

- 脚本会以 **root** 身份在你的 VPS 上执行命令（装软件、改防火墙、写 systemd 服务）——
  执行前请自行审阅 `scripts/` 下所有文件。
- `deploy.env` 含明文凭据，**已在 `.gitignore` 中排除**，请勿提交。
  同理，`render_report.py` 生成的 `VPS-*.md` / `.html` 也含明文凭据，同样被忽略。
- 建议部署完成后在面板里修改默认用户名与密码。
- 只在你**拥有或获授权管理**的服务器上使用本仓库。

---

## 许可

MIT
