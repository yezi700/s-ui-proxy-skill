#!/bin/bash
# ============================================================================
# S-UI 非交互安装 + Let's Encrypt 证书
#
# 通过 `python scripts/ssh_run.py -f scripts/install_sui.sh` 执行。
# 参数从 deploy.env 注入的环境变量读取。
#
# 前提：
#   * 域名已解析到本机公网 IP（dig +short $DOMAIN 应返回 $SERVER_IP）
#   * 80 端口可达且未被占用（ACME HTTP-01 需要）
#
# 为什么不直接跑官方 install.sh：
#   官方脚本是交互式的（问语言、端口、路径、账号），没法在自动化流程里用。
#   这里改成「下 release 包 → 解压 → 改 DB 里的 settings → 重启」，
#   每一步都可回读校验。S-UI 的所有配置都在 /usr/local/s-ui/db/s-ui.db 的
#   settings 表（key/value 结构），直接改库比猜安装脚本的交互时序可靠得多。
# ============================================================================
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
cd /root || exit 1

SUI_VERSION="${SUI_VERSION:-1.6.7}"
PANEL_USER="${PANEL_USER:-admin}"
PANEL_PASS="${PANEL_PASS:-}"
PANEL_PORT="${PANEL_PORT:-46821}"
PANEL_PATH="${PANEL_PATH:-}"
SUB_PORT="${SUB_PORT:-2096}"
SUB_PATH="${SUB_PATH:-}"
DOMAIN="${DOMAIN:-}"
SERVER_IP="${SERVER_IP:-}"
ACME_PORT="${ACME_PORT:-80}"
CERT_DIR="${CERT_DIR:-/root/cert/${DOMAIN}}"

if [ -z "$PANEL_PASS" ]; then
  PANEL_PASS="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)"
  echo "未提供 PANEL_PASS，已随机生成"
fi
if [ -z "$PANEL_PATH" ]; then
  PANEL_PATH="$(openssl rand -hex 8)"
  echo "未提供 PANEL_PATH，已随机生成"
fi
if [ -z "$SUB_PATH" ]; then
  SUB_PATH="$(openssl rand -hex 8)"
  echo "未提供 SUB_PATH，已随机生成"
fi

echo "########## 安装参数 ##########"
cat <<EOF
SUI_VERSION = ${SUI_VERSION}
PANEL_USER  = ${PANEL_USER}
PANEL_PASS  = ***
PANEL_PORT  = ${PANEL_PORT}
PANEL_PATH  = /${PANEL_PATH}/
SUB_PORT    = ${SUB_PORT}
SUB_PATH    = /${SUB_PATH}/
DOMAIN      = ${DOMAIN}
SERVER_IP   = ${SERVER_IP}
EOF

# ---------------------------------------------------------------- 前置检查
echo ""
echo "########## 1. 前置检查 ##########"
if [ -n "$DOMAIN" ]; then
  RESOLVED="$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1)"
  echo "DNS 解析: ${DOMAIN} -> ${RESOLVED:-<失败>}"
  if [ -n "$SERVER_IP" ] && [ "$RESOLVED" != "$SERVER_IP" ]; then
    echo "⚠️  DNS 解析结果与 SERVER_IP 不一致，ACME 申请可能失败。"
  fi
fi

if ss -lntp 2>/dev/null | grep -q ":${ACME_PORT} "; then
  echo "⚠️  ${ACME_PORT} 端口已被占用，ACME standalone 会失败："
  ss -lntp | grep ":${ACME_PORT} "
fi

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) SUI_ARCH="amd64" ;;
  aarch64|arm64) SUI_ARCH="arm64" ;;
  armv7l) SUI_ARCH="armv7" ;;
  *) echo "不支持的架构: $ARCH"; exit 1 ;;
esac
echo "架构: ${ARCH} -> ${SUI_ARCH}"

# ---------------------------------------------------------------- 下载
echo ""
echo "########## 2. 下载 S-UI v${SUI_VERSION} ##########"
TARBALL="s-ui-linux-${SUI_ARCH}.tar.gz"
URL="https://github.com/alireza0/s-ui/releases/download/v${SUI_VERSION}/${TARBALL}"
rm -rf /tmp/sui-install && mkdir -p /tmp/sui-install && cd /tmp/sui-install || exit 1
curl -L --retry 3 --connect-timeout 20 -o "$TARBALL" "$URL"
ls -lh "$TARBALL"
tar xzf "$TARBALL"
[ -x ./s-ui/sui ] || { echo "解压后找不到 s-ui/sui"; exit 1; }

# ---------------------------------------------------------------- 安装
echo ""
echo "########## 3. 安装到 /usr/local/s-ui ##########"
# 已有安装先备份 DB，避免覆盖掉正在用的配置
if [ -f /usr/local/s-ui/db/s-ui.db ]; then
  cp -a /usr/local/s-ui/db/s-ui.db "/root/s-ui.db.bak.$(date +%Y%m%d-%H%M%S)"
  echo "已备份旧 DB"
fi
mkdir -p /usr/local/s-ui
cp -f ./s-ui/sui /usr/local/s-ui/
cp -f ./s-ui/s-ui.sh /usr/local/s-ui/
cp -f ./s-ui/s-ui.service /etc/systemd/system/
chmod +x /usr/local/s-ui/sui /usr/local/s-ui/s-ui.sh
ln -sf /usr/local/s-ui/s-ui.sh /usr/bin/s-ui
systemctl daemon-reload
systemctl enable s-ui >/dev/null 2>&1

# ---------------------------------------------------------------- 首次启动
echo ""
echo "########## 4. 首次启动（生成默认 DB） ##########"
# 默认端口 2095/2096 可能与已有服务冲突，起不来是正常的；
# 这一步只是为了把 s-ui.db 建出来，后面直接改库。
systemctl start s-ui >/dev/null 2>&1 || true
for _ in $(seq 1 15); do
  [ -f /usr/local/s-ui/db/s-ui.db ] && break
  sleep 1
done
systemctl stop s-ui >/dev/null 2>&1 || true
sleep 1
if [ ! -f /usr/local/s-ui/db/s-ui.db ]; then
  echo "❌ 没能生成 s-ui.db，安装中止"
  exit 1
fi
ls -l /usr/local/s-ui/db/

# ---------------------------------------------------------------- 改配置
echo ""
echo "########## 5. 写入面板 / 订阅配置 ##########"
# ⚠️ settings 表是纯 key/value，没有 UNIQUE 约束，不能写 ON CONFLICT。
#    正确做法是 UPDATE，rowcount 为 0 时再 INSERT。
python3 - "$PANEL_PORT" "$PANEL_PATH" "$SUB_PORT" "$SUB_PATH" "$PANEL_USER" "$PANEL_PASS" <<'PYEOF'
import sqlite3, sys

port, wpath, sport, spath, user, password = sys.argv[1:7]
db = "/usr/local/s-ui/db/s-ui.db"
c = sqlite3.connect(db)


def setk(k, v):
    cur = c.execute("UPDATE settings SET value=? WHERE key=?", (str(v), k))
    if cur.rowcount == 0:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?)", (k, str(v)))


for k, v in {
    "webPort": port,
    "webPath": f"/{wpath.strip('/')}/",
    "subPort": sport,
    "subPath": f"/{spath.strip('/')}/",
    "webDomain": "",
    "subDomain": "",
    "timeLocation": "Asia/Shanghai",
    "subShowInfo": "true",     # 订阅响应头带流量信息
    "subClashUdp": "true",     # Clash 输出里给 UDP 节点补 udp: true
    "subEncode": "true",       # 通用订阅返回 base64
    "subRemarkPrefix": "false",  # 关掉「备注+tag」拼接，避免节点名重复
}.items():
    setk(k, v)

# 改密码：S-UI 用 bcrypt($2a$10$)。没装 bcrypt 库时退化成调用 sui 自带的 hash 逻辑不可行，
# 所以这里用 openssl 无法生成 bcrypt —— 改为保留默认 admin/admin，由后续步骤用 API 改。
c.commit()
for k in ("webPort", "webPath", "subPort", "subPath"):
    print(f"  {k} = {c.execute('select value from settings where key=?', (k,)).fetchone()[0]}")
c.close()
PYEOF

# ---------------------------------------------------------------- 证书
echo ""
echo "########## 6. 申请 Let's Encrypt 证书 ##########"
CERT_OK=0
if [ -n "$DOMAIN" ]; then
  if [ -f "${CERT_DIR}/fullchain.pem" ] && [ -f "${CERT_DIR}/privkey.pem" ]; then
    echo "已存在证书，跳过申请：${CERT_DIR}"
    CERT_OK=1
  else
    if ! command -v ~/.acme.sh/acme.sh >/dev/null 2>&1; then
      echo "安装 acme.sh ..."
      curl -s https://get.acme.sh | sh -s email="admin@${DOMAIN}" >/dev/null 2>&1 || true
    fi
    if [ -x ~/.acme.sh/acme.sh ]; then
      ~/.acme.sh/acme.sh --set-default-ca --server letsencrypt >/dev/null 2>&1 || true
      ~/.acme.sh/acme.sh --issue -d "$DOMAIN" --standalone --httpport "$ACME_PORT" --force 2>&1 | tail -12
      mkdir -p "$CERT_DIR"
      ~/.acme.sh/acme.sh --install-cert -d "$DOMAIN" \
        --key-file "${CERT_DIR}/privkey.pem" \
        --fullchain-file "${CERT_DIR}/fullchain.pem" \
        --reloadcmd "systemctl restart s-ui" 2>&1 | tail -5
      [ -f "${CERT_DIR}/fullchain.pem" ] && CERT_OK=1
    fi
  fi
fi

if [ "$CERT_OK" = "1" ]; then
  echo "证书就绪：${CERT_DIR}"
  chmod 600 "${CERT_DIR}/privkey.pem"
  python3 - "$CERT_DIR" "$DOMAIN" <<'PYEOF'
import sqlite3, sys
cert, domain = sys.argv[1], sys.argv[2]
c = sqlite3.connect("/usr/local/s-ui/db/s-ui.db")
def setk(k, v):
    cur = c.execute("UPDATE settings SET value=? WHERE key=?", (v, k))
    if cur.rowcount == 0:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?)", (k, v))
for k, v in {
    "webCertFile": f"{cert}/fullchain.pem",
    "webKeyFile": f"{cert}/privkey.pem",
    "subCertFile": f"{cert}/fullchain.pem",
    "subKeyFile": f"{cert}/privkey.pem",
    "webDomain": domain,
    "subDomain": domain,
}.items():
    setk(k, v)
c.commit(); c.close()
print("  面板与订阅已配置 HTTPS")
PYEOF
else
  echo "⚠️  未取得证书，面板与订阅将使用 HTTP。"
  echo "    可在取得证书后重跑本脚本，或手动在面板设置里填证书路径。"
fi

# ---------------------------------------------------------------- 启动
echo ""
echo "########## 7. 启动并校验 ##########"
systemctl restart s-ui
sleep 6
echo "服务状态: $(systemctl is-active s-ui)"
echo "--- 端口 ---"
ss -lntp | grep -E ":(${PANEL_PORT}|${SUB_PORT}) " || echo "⚠️  面板/订阅端口未监听"
echo "--- 日志 ---"
journalctl -u s-ui --no-pager -n 15 | tail -10

# ---------------------------------------------------------------- 改密码
echo ""
echo "########## 8. 设置面板密码 ##########"
# 首次启动是 admin/admin。这里走前端 API 改掉：
# ⚠️ 前端 API 必须带 Origin 头，否则 CSRF 拦成 403 空响应。
SCHEME="https"
[ "$CERT_OK" = "1" ] || SCHEME="http"

# ⚠️ 有证书时**必须用域名**访问，不能用 127.0.0.1：
#    面板证书和 webDomain 都是按域名配的，用 IP 会被 SNI 校验拒掉，
#    表现为 changePass 返回空响应（curl -sk 不报错，看起来像"改成功了"）。
#    用 --resolve 把域名钉到 127.0.0.1，既满足 SNI 又不绕公网。
if [ "$CERT_OK" = "1" ] && [ -n "${DOMAIN:-}" ]; then
  HOST="${DOMAIN}"
  RESOLVE="--resolve ${DOMAIN}:${PANEL_PORT}:127.0.0.1"
else
  HOST="127.0.0.1"
  RESOLVE=""
fi
BASE="${SCHEME}://${HOST}:${PANEL_PORT}/${PANEL_PATH}"
ORIGIN="${SCHEME}://${HOST}:${PANEL_PORT}"

curl -sk ${RESOLVE} -c /tmp/sui.ck -X POST "${BASE}/api/login" \
  -H "Origin: ${ORIGIN}" \
  --data-urlencode "user=admin" --data-urlencode "pass=admin" >/dev/null 2>&1 || true

CHANGE_RESP="$(curl -sk ${RESOLVE} -b /tmp/sui.ck -X POST "${BASE}/api/changePass" \
  -H "Origin: ${ORIGIN}" \
  --data-urlencode "oldPass=admin" \
  --data-urlencode "newUsername=${PANEL_USER}" \
  --data-urlencode "newPass=${PANEL_PASS}" 2>/dev/null || true)"
echo "changePass: ${CHANGE_RESP:-<无响应>}"

# 回读验证：新凭据能登进去、旧凭据登不进，才算真的改成功。
# 不验证的话，SNI/Origin 任一出问题都会留下 admin/admin 的面板。
VERIFY_OK=0
if curl -sk ${RESOLVE} -c /tmp/sui2.ck -X POST "${BASE}/api/login" \
     -H "Origin: ${ORIGIN}" \
     --data-urlencode "user=${PANEL_USER}" \
     --data-urlencode "pass=${PANEL_PASS}" 2>/dev/null | grep -q '"success":true'; then
  VERIFY_OK=1
fi
if [ "$VERIFY_OK" = "1" ]; then
  echo "✅ 新密码验证通过"
else
  echo "❌ 新密码登录失败 —— 面板仍是 admin/admin，请手工处理："
  echo "   curl -sk ${RESOLVE} -c /tmp/c -X POST '${BASE}/api/login' \\"
  echo "     -H 'Origin: ${ORIGIN}' -d 'user=admin' -d 'pass=admin'"
  echo "   （注意必须有 Origin 头，且 HOST 必须是 ${HOST}，不能用 127.0.0.1）"
fi
rm -f /tmp/sui.ck /tmp/sui2.ck

# ---------------------------------------------------------------- 落盘
echo ""
echo "########## 9. 写入安装结果 ##########"
cat > /usr/local/s-ui/install-result.env <<EOF
SUI_VERSION=${SUI_VERSION}
PANEL_USER=${PANEL_USER}
PANEL_PASS=${PANEL_PASS}
PANEL_PORT=${PANEL_PORT}
PANEL_PATH=${PANEL_PATH}
SUB_PORT=${SUB_PORT}
SUB_PATH=${SUB_PATH}
DOMAIN=${DOMAIN}
SERVER_IP=${SERVER_IP}
CERT_DIR=${CERT_DIR}
CERT_OK=${CERT_OK}
PANEL_URL=${SCHEME}://${DOMAIN:-$SERVER_IP}:${PANEL_PORT}/${PANEL_PATH}/
SUB_URL=${SCHEME}://${DOMAIN:-$SERVER_IP}:${SUB_PORT}/${SUB_PATH}/
EOF
chmod 600 /usr/local/s-ui/install-result.env
cat /usr/local/s-ui/install-result.env

echo ""
echo "########## INSTALL DONE ##########"
