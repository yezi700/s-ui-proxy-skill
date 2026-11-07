# 安装环境与参数

## 一、版本与架构

S-UI 的 release 包命名：

```
s-ui-linux-<arch>.tar.gz      # arch: amd64 / arm64 / armv7 / armv6 / armv5 / 386 / s390x
```

包内结构：

```
s-ui/
├── sui               # 主程序（面板 + 内嵌 sing-box）
├── s-ui.sh           # 交互式管理脚本
└── s-ui.service      # systemd 单元
```

**sing-box 内核是内嵌在 `sui` 二进制里的**（`bin/` 目录不存在），
所以不需要单独下载核心，也不能用 `strings` 找 `sing-box` 版本来判断。

查看实际内核版本：

```bash
journalctl -u s-ui -n 50 | grep -i sing-box
# 或
curl -sk -b cookies "$BASE/api/status?r=sbd"
```

指定版本安装：

```bash
SUI_VERSION=1.6.7 python scripts/ssh_run.py -f scripts/install_sui.sh
```

## 二、目录结构

```
/usr/local/s-ui/
├── sui                    # 主程序
├── s-ui.sh                # 管理脚本（/usr/bin/s-ui 软链到它）
├── db/
│   ├── s-ui.db            # 全部配置（SQLite）
│   ├── s-ui.db-shm
│   └── s-ui.db-wal
└── install-result.env     # 本 skill 写入的安装结果
```

证书默认放 `/root/cert/<域名>/`，与面板、订阅、各节点共用。

## 三、systemd 单元

```ini
[Unit]
Description=s-ui Service
After=network.target

[Service]
Type=simple
WorkingDirectory=/usr/local/s-ui/
ExecStart=/usr/local/s-ui/sui
Restart=on-failure
RestartSec=10s
LimitNOFILE=1048576        # 代理每连接两个 fd，默认 1024 几百个客户端就爆
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=read-only      # 读：acme.sh 的证书在 /root/cert 下
```

`ProtectHome=read-only` 不能改成 `true` —— 面板要读 `/root/cert/` 下的证书。

常用命令：

```bash
systemctl status s-ui
systemctl restart s-ui
journalctl -u s-ui -f
s-ui            # 交互菜单：start / stop / restart / log / update / uninstall
```

## 四、环境变量

| 变量 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `SUI_LOG_LEVEL` | debug/info/warn/error | info | 日志级别 |
| `SUI_DEBUG` | boolean | false | 调试模式 |
| `SUI_BIN_FOLDER` | string | bin | 外部二进制目录 |
| `SUI_DB_FOLDER` | string | db | 数据库目录 |
| `SINGBOX_API` | string | — | 外部 sing-box API 地址 |

改环境变量要改 `s-ui.service` 的 `Environment=` 然后 `systemctl daemon-reload`。

## 五、面板关键设置

全部存在 `db/s-ui.db` 的 `settings` 表，也可以走 API 改。

### 5.1 面板与订阅

| 键 | 说明 |
|---|---|
| `webPort` | 面板端口，默认 2095 |
| `webPath` | 面板路径，默认 `/app/`，**建议改成随机 16 位 hex** |
| `webDomain` | 面板域名，影响 SNI 校验与订阅里的地址 |
| `webCertFile` / `webKeyFile` | 面板 HTTPS 证书 |
| `subPort` | 订阅端口，默认 2096 |
| `subPath` | 订阅路径，默认 `/sub/` |
| `subDomain` | 订阅域名 |
| `subCertFile` / `subKeyFile` | 订阅 HTTPS 证书 |

⚠️ 改 `webPort` / `webPath` / `webCertFile` / `webKeyFile` 后**必须
`systemctl restart s-ui`**，不会自动生效。

### 5.2 订阅行为

| 键 | 建议值 | 说明 |
|---|---|---|
| `subShowInfo` | `true` | 响应头带流量信息 |
| `subClashUdp` | `true` | Clash 输出给 UDP 节点补 `udp: true` |
| `subEncode` | `true` | 通用订阅返回 base64 |
| `subRemarkPrefix` | `false` | 节点名不拼 client remark，避免 `US-US-xxx` |
| `subUpdates` | `12` | 订阅更新间隔提示（小时） |

### 5.3 其他

| 键 | 说明 |
|---|---|
| `timeLocation` | 时区，默认 `Asia/Tehran`，**改成 `Asia/Shanghai`** |
| `sessionMaxAge` | 会话有效期，0 = 不过期 |
| `maintenance` | 维护模式，开启后核心停止 |

## 六、升级

```bash
# 备份
cp /usr/local/s-ui/db/s-ui.db /root/s-ui.db.bak.$(date +%Y%m%d-%H%M)

# 用面板内一键更新（v1.6.7 起支持），或手动：
SUI_VERSION=<新版本> python scripts/ssh_run.py -f scripts/install_sui.sh
```

`install_sui.sh` 是幂等的：已存在的 DB 会先备份再保留，
settings 里的端口/路径不会被覆盖成默认值（脚本只写自己管的那几个键）。

跨大版本升级（如 1.5 → 1.6）时，S-UI 会做一次性配置迁移。
注意 v1.6.0 起有两类**不会自动迁移**的项：

1. 使用旧地址过滤器的 DNS 规则（`ip_cidr` / `ip_is_private` / IP 规则集）
2. 已弃用的 `strategy` DNS 规则动作

迁移日志会记在 `journalctl -u s-ui` 里，升级后先看一眼。

## 七、卸载

```bash
systemctl disable s-ui --now
rm -f /etc/systemd/system/s-ui.service
systemctl daemon-reload
rm -rf /usr/local/s-ui
rm -f /usr/bin/s-ui
```

证书和 `/etc/iptables/rules.v4` 不会动，需要单独清理。

## 八、从 3x-ui 迁移

| 项 | 3x-ui | S-UI |
|---|---|---|
| 内核 | Xray | sing-box（内嵌） |
| 数据目录 | `/etc/x-ui/` | `/usr/local/s-ui/db/` |
| 配置 | Xray JSON（`bin/config.json`） | SQLite（`db/s-ui.db`） |
| 热更新 | gRPC（不重写 config.json） | 保存即热加载 |
| 改设置后 | 需 `x-ui restart` | 面板端口类需 `systemctl restart s-ui` |

迁移步骤见 SKILL.md「从 3x-ui 迁移」一节。**3x-ui 只是 stop + disable，
数据和证书完整保留**，回退只需一条命令：

```bash
systemctl enable --now x-ui && systemctl disable --now s-ui
```
