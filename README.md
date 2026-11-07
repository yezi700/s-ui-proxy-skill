# s-ui-proxy-skill

在全新 Linux VPS 上一键部署 **S-UI（sing-box 内核）** 面板与四协议抗封锁节点。

```
AnyTLS      8443/tcp   TLS-in-TLS 指纹打散 + 连接复用，日常主力
TUIC v5     8443/udp   QUIC 传输，与 AnyTLS 同号但互不影响
Hysteria2   2083/udp   激进拥塞控制，高丢包链路吞吐最好
VLESS+Reality 2053/tcp 借用外部 SNI，抗主动探测，保底
```

产出：面板凭据、4 个节点、3 种格式的订阅地址、完整的交付文档。

## 为什么全部避开 443

这不是个人偏好，是实测结论。2026-11 的对照实验：

| 节点 | 结果 |
|---|---|
| VLESS-Reality @ **443**/TCP | `REALITY authentication failed` |
| Hysteria2 @ **443**/UDP | 全部 `context deadline exceeded` |
| TUIC @ 8443/UDP | 3/3 通过 |

在同一台 VPS 上对 443 做未认证 TLS 握手，**能正常拿到上游真证书** ——
说明服务端没问题，是 443 端口在链路中间被针对性干扰。

把 Reality 换到 2053、Hysteria2 换到 2083，**其他参数一字不改，立刻 3/3 全通**。

所以：**遇到"节点突然不能用了"，先换端口做对照实验，不要急着重装或换协议。**

## 特性

- **非交互安装**：不走官方交互式 install.sh，直接下 release + 改 DB，每步可回读校验
- **幂等部署**：重复执行不会重复创建对象，也不会让凭据"漂移"
- **三格式订阅**：通用 base64 / Clash YAML / sing-box JSON，一条身份出全部节点
- **三层验证**：服务端状态 → 外部可达 → 真实客户端拨号，明确标注每层能证明什么
- **离线测试**：32 个单元测试，覆盖密钥格式、端口规划、配置解析、API 参数构造
- **实战坑位**：`references/pitfalls.md` 记录了 20+ 个实际踩过的问题与根因

## 快速开始

```bash
git clone <this-repo> && cd s-ui-proxy-skill
cp examples/deploy.env.example deploy.env
$EDITOR deploy.env

python scripts/check_dns.py --domain <域名> --ipv4 <IP>
python scripts/ssh_run.py -c "cat /etc/os-release | head -3; uname -m"
python scripts/ssh_run.py -f scripts/setup_base.sh
python scripts/ssh_run.py -f scripts/install_sui.sh
python scripts/deploy_nodes.py
python scripts/ssh_run.py -f scripts/verify_nodes.sh
python scripts/render_report.py -o .
```

依赖：Python 3.8+、`paramiko`、`openssl`。

## 目录结构

```
├── SKILL.md                          # 完整工作流（给 agent 读的）
├── scripts/
│   ├── check_dns.py                  # DNS 只读预检
│   ├── ssh_run.py                    # 通用 SSH 执行器
│   ├── sui_api.py                    # 面板 API 客户端
│   ├── setup_base.sh                 # 系统基线 + iptables + BBR
│   ├── install_sui.sh                # 安装 + ACME 证书
│   ├── deploy_nodes.py               # 创建 4 个入站 + 订阅用户
│   ├── verify_nodes.sh               # 端到端验证（14 项）
│   └── render_report.py              # 交付文档生成
├── references/
│   ├── pitfalls.md                   # 踩坑记录（每次部署都该看）
│   ├── sui-api.md                    # 面板 API 参考
│   ├── protocols-and-clients.md      # 协议选型与客户端兼容性
│   ├── install-env.md                # 安装参数、目录、升级
│   └── domain-and-dns.md             # 域名与 DNS 引导
├── tests/
│   ├── test_sui.py                   # 离线回归测试
│   └── test_dns.py
└── examples/deploy.env.example
```

## 客户端兼容性

**这张表必须整张看** —— 只按一半选，会在两种"连不上"之间来回折腾。

| 客户端 / 内核 | AnyTLS | TUIC | Hysteria2 | VLESS-Reality |
|---|:--:|:--:|:--:|:--:|
| sing-box ≥1.12（Hiddify / Karing / NekoBox） | 是 | 是 | 是 | 是 |
| mihomo / Clash Meta ≥1.19（Verge Rev / FlClash） | 是 | 是 | 是 | 是 |
| Xray 系（v2rayN / v2rayNG 默认内核） | 否 | 否 | 否 | 是 |
| Shadowrocket（≥2.2.65） | 是 | 是 | 是 | 是 |
| Surge | 否 | 否 | 是 | 否 |
| QuantumultX | 否 | 否 | 否 | 否 |

## 订阅地址

```
https://<域名>:2096/<subPath>/<subId>                 通用（base64）
https://<域名>:2096/<subPath>/<subId>?format=clash    Clash YAML
https://<域名>:2096/<subPath>/<subId>?format=json     sing-box JSON
```

把通用那条填给 Clash 会报 `cannot unmarshal !!str`。

## 测试

```bash
python -m unittest discover -s tests -v
```

不需要 VPS、不需要网络。

## 从 3x-ui 迁移

3x-ui 只需 `systemctl stop x-ui`（数据完整保留），回退一条命令：

```bash
systemctl enable --now x-ui && systemctl disable --now s-ui
```

详见 `SKILL.md` 的「从 3x-ui 迁移」。

## 免责声明

本项目仅用于个人学习与网络技术研究。使用者应遵守所在地区的法律法规，
作者不对任何滥用行为负责。

## License

GPL-3.0
