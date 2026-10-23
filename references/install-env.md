# 3x-ui 非交互安装参数

3x-ui 官方安装脚本默认是**交互式**的（会依次问你端口、路径、用户名、密码、SSL 方式）。
自动化部署必须让它闭嘴。它支持通过环境变量传入全部答案。

> 参考实现：`scripts/install_3xui.sh`

---

## 1. 总开关

```bash
export XUI_NONINTERACTIVE=1
```

只要设了这个，脚本就不再读 stdin，全部参数从环境变量取。**缺哪个就用默认值**，
所以宁可多设几个，也别指望它报错提醒你。

---

## 2. 环境变量清单

| 变量 | 含义 | 示例 | 备注 |
|---|---|---|---|
| `XUI_NONINTERACTIVE` | 关闭交互 | `1` | 必设 |
| `XUI_USERNAME` | 面板用户名 | `admin` | |
| `XUI_PASSWORD` | 面板密码 | `YourStrongPanelPassword` | 建议随机生成 |
| `XUI_PANEL_PORT` | 面板端口 | `46821` | 别用 54321（太常见） |
| `XUI_WEB_BASE_PATH` | 面板 URL 路径前缀 | `a1b2c3d4e5f6g7h8` | 随机 16 位，去掉首尾 `/` |
| `XUI_SSL_MODE` | SSL 模式 | `domain` | 见 §3 |
| `XUI_DOMAIN` | 域名 | `jp.example.com` | `XUI_SSL_MODE=domain` 时必填 |
| `XUI_ACME_HTTP_PORT` | ACME HTTP-01 端口 | `80` | 需 80 端口可达 |
| `XUI_SERVER_IP` | 服务器公网 IP | `203.0.113.10` | `XUI_SSL_MODE=ip` 时用 |

`XUI_SSL_MODE` 取值：

| 值 | 行为 | 前提 |
|---|---|---|
| `domain` | 用 acme.sh 申请 Let's Encrypt 证书 | 域名已解析到本机 + 80 端口可达 |
| `ip` | 自签 IP 证书 | 浏览器会报不受信任 |
| `none` | 纯 HTTP | 面板明文传输，**不推荐** |

---

## 3. 完整安装片段

```bash
#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive
cd /root || exit 1

# 1) 下载官方安装脚本
curl -Ls --retry 3 --connect-timeout 20 -o /root/3xui-install.sh \
  https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh

# 2) 非交互参数
export XUI_NONINTERACTIVE=1
export XUI_USERNAME="$PANEL_USER"
export XUI_PASSWORD="$PANEL_PASS"
export XUI_PANEL_PORT="$PANEL_PORT"
export XUI_WEB_BASE_PATH="$PANEL_PATH"
export XUI_SSL_MODE="domain"
export XUI_DOMAIN="$DOMAIN"
export XUI_ACME_HTTP_PORT="80"
export XUI_SERVER_IP="$SERVER_IP"

# 3) 打印参数（打码）
env | grep '^XUI_' | sed 's/\(PASSWORD=\).*/\1***/'

# 4) 执行
bash /root/3xui-install.sh 2>&1 | tee /root/3xui-install.log
echo "退出码: ${PIPESTATUS[0]}"
```

⚠️ **用 `tee` 保留日志**。安装脚本偶发失败（acme 申请超时、网络抖动），
没有日志就只能重装。`${PIPESTATUS[0]}` 才是安装脚本本身的退出码，
`$?` 拿到的是 `tee` 的。

---

## 4. 安装后回读结果

安装脚本会把关键结果写到 `/etc/x-ui/install-result.env`（3x-ui 较新版本的行为）：

```bash
cat /etc/x-ui/install-result.env
# 一般包含 PANEL_URL / USERNAME / PASSWORD / WEB_BASE_PATH / API_TOKEN 等
```

**必须回读并校验**，不能假设安装成功：

```bash
systemctl is-active x-ui        # 期望 active
systemctl is-enabled x-ui       # 期望 enabled
ss -lntp | grep -E ":${PANEL_PORT}"   # 端口在听

# 面板设置（含 API Token）
/usr/local/x-ui/x-ui setting -show

# 证书目录
ls -l /root/cert/${DOMAIN}/
```

拿 API Token（脚本没给的话）：

```bash
sqlite3 /etc/x-ui/x-ui.db "select value from settings where key='apiToken';"
```

---

## 5. 证书（acme.sh）

3x-ui 安装脚本内部用 acme.sh，安装后证书落在：

```
/root/cert/<域名>/fullchain.pem
/root/cert/<域名>/privkey.pem
```

**Hysteria2 与 TUIC 直接引用这两个文件**，不要复制、不要改属主/权限。

手动续期与重新签发：

```bash
# 强制续期
/root/.acme.sh/acme.sh --cron --home /root/.acme.sh

# 重新签发并安装到 /root/cert
/root/.acme.sh/acme.sh --issue -d "$DOMAIN" --standalone --keylength ec-256 --force
/root/.acme.sh/acme.sh --install-cert -d "$DOMAIN" --ecc \
  --fullchain-file /root/cert/$DOMAIN/fullchain.pem \
  --key-file      /root/cert/$DOMAIN/privkey.pem \
  --reloadcmd     "systemctl restart x-ui"
```

⚠️ `--standalone` 需要 80 端口空闲。面板本身也监听 443，不冲突，
但如果之前有 nginx/caddy 占着 80，先停掉。

⚠️ **续期后要重启 x-ui**（或让 `--reloadcmd` 做掉），否则面板/节点还在用旧证书句柄。

---

## 6. 面板设置项（安装后再通过 API 调）

安装脚本只设基础项。以下建议用 `/setting/update` 补上（**注意是整体覆盖**，
先 `POST /setting/all` 拿全量再改，见 `xui-api.md` §5.2）：

| 键 | 建议值 | 原因 |
|---|---|---|
| `webDomain` | 你的域名 | 启用 Host 校验，防 IP 扫描；副作用见 `xui-api.md` §1.2 |
| `shareAddrStrategy` | `custom` | 否则分享链接里是 `127.0.0.1` |
| `shareAddr` | 你的域名 | 同上 |
| `subPort` | `2096` | 订阅端口 |
| `subPath` | 随机 | 订阅路径 |
| `subEnable` | `true` | 开启订阅服务 |
| `expireDiff` / `trafficDiff` | 按需 | 到期/流量提醒 |

---

## 7. 版本与升级

```bash
# 查看当前版本
/usr/local/x-ui/x-ui -v

# 内置 Xray 版本
/usr/local/x-ui/bin/xray-linux-amd64 -version
```

3x-ui v3.9.0 内置 **Xray-core 26.9.30**。

⚠️ **不要盲目升级 Xray**。26.9.8 起 REALITY 强制要求客户端支持
`X25519MLKEM768`，会导致 sing-box 系客户端全部连不上（详见
`protocols-and-clients.md` §3）。

升级前务必：

```bash
cp /etc/x-ui/x-ui.db /root/x-ui-backup-$(date +%Y%m%d-%H%M).db
```

---

## 8. 卸载（谨慎）

```bash
# 官方卸载
x-ui uninstall

# 残留清理
rm -rf /etc/x-ui /usr/local/x-ui /root/cert
```

⚠️ 会**连证书一起删掉**，且不可恢复。执行前确认用户意图。
