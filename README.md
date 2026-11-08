# s-ui-proxy-skill

在全新 Linux VPS 上一键部署 **S-UI（sing-box 内核）** 面板与四协议抗封锁节点。

```
AnyTLS        8443/tcp  TLS-in-TLS 指纹打散 + 连接复用，日常主力
Hysteria2     2083/udp  激进拥塞控制 + salamander 混淆，高丢包链路吞吐最好
VLESS+Reality 2053/tcp  借用外部 SNI，抗主动探测，保底
Shadowsocks22 2087/tcp  无 TLS 握手、延迟最低，客户端兼容面最广
```

产出：面板凭据、4 个节点、3 种格式的订阅地址、完整的交付文档。

> TUIC 改成**可选**（`TUIC_PORT` 留空即不部署）：实测在部分线路上被限速到
> 0.2~0.4 Mbps，换端口、换客户端都一样。详见下方「TUIC 为什么不默认开」。

## 为什么全部避开 443

这不是个人偏好，是实测结论。2026-11 的对照实验：

| 节点 | 结果 |
|---|---|
| VLESS-Reality @ **443**/TCP | `REALITY authentication failed` |
| Hysteria2 @ **443**/UDP | 全部 `context deadline exceeded` |
| Shadowsocks @ 2087/TCP | 3/3 通过（无 TLS 握手，受影响最小） |

在同一台 VPS 上对 443 做未认证 TLS 握手，**能正常拿到上游真证书** ——
说明服务端没问题，是 443 端口在链路中间被针对性干扰。

把 Reality 换到 2053、Hysteria2 换到 2083，**其他参数一字不改，立刻 3/3 全通**。

所以：**遇到"节点突然不能用了"，先换端口做对照实验，不要急着重装或换协议。**

## TUIC 为什么不默认开

同一台机器、同一时段，TUIC 能连上、能认证、延迟也正常，但吞吐只有 **0.2~0.4 Mbps**。

排除过程：

| 检查 | 结果 |
|---|---|
| 换端口 8443 → 2084 | 一样慢 |
| 换客户端 mihomo → sing-box 1.14.4 官方 | 一样慢 |
| 服务端日志时间戳 | 连接在同一秒内转发，没卡 |
| `UdpRcvbufErrors` 增量 | 0，不是缓冲区问题 |
| 同机 Hysteria2 | 几十 Mbps，正常 |

结论：这条路径对**明文 QUIC** 做了限速；Hysteria2 活着是因为 `salamander` 混淆。
替代方案选了 Shadowsocks 2022 —— 无 TLS 握手、握手往返最少，Xray / Surge /
QuantumultX 这些不支持 AnyTLS 的客户端也认它。

## 特性

- **非交互安装**：不走官方交互式 install.sh，直接下 release + 改 DB，每步可回读校验
- **幂等部署**：重复执行不会重复创建对象，也不会让凭据"漂移"
- **三格式订阅**：通用 base64 / Clash YAML / sing-box JSON，一条身份出全部节点
- **三层验证**：服务端状态 → 外部可达 → 真实客户端拨号，明确标注每层能证明什么
- **离线测试**：42 个单元测试，覆盖密钥格式、端口规划、配置解析、API 参数构造
- **可配节点集**：`TUIC_PORT` / `SS_PORT` 留空就不部署，防火墙规则同步跟着走
- **实战坑位**：`references/pitfalls.md` 记录了 30+ 个实际踩过的问题与根因

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
│   ├── deploy_nodes.py               # 创建入站 + 订阅用户
│   ├── verify_nodes.sh               # 端到端验证（15 项）
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

| 客户端 / 内核 | AnyTLS | Hysteria2 | VLESS-Reality | Shadowsocks |
|---|:--:|:--:|:--:|:--:|
| sing-box ≥1.12（Hiddify / Karing / NekoBox） | 是 | 是 | 是 | 是 |
| mihomo / Clash Meta ≥1.19（Verge Rev / FlClash） | 是 | 是 | 是 | 是 |
| Xray 系（v2rayN / v2rayNG 默认内核） | 否 | 否 | 是 | 是 |
| Shadowrocket（≥2.2.65） | 是 | 是 | 是 | 是 |
| Surge | 否 | 是 | 否 | 是 |
| QuantumultX | 否 | 否 | 否 | 是 |

> 用 Xray 内核（v2rayN / v2rayNG 默认）的人只有 Reality 和 Shadowsocks 能用 ——
> 这不是节点坏了。

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
