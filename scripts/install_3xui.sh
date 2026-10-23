#!/bin/bash
# ============================================================================
# 3x-ui 非交互安装 + Let's Encrypt 证书
#
# 通过 `python scripts/ssh_run.py -f scripts/install_3xui.sh` 执行。
# 参数从 deploy.env 注入的环境变量读取。
#
# 前提：
#   * 域名已解析到本机公网 IP（dig +short $DOMAIN 应返回 $SERVER_IP）
#   * 80 端口可达且未被占用（ACME HTTP-01 需要）
# ============================================================================
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
cd /root || exit 1

PANEL_USER="${PANEL_USER:-admin}"
PANEL_PASS="${PANEL_PASS:-}"
PANEL_PORT="${PANEL_PORT:-46821}"
PANEL_PATH="${PANEL_PATH:-}"
DOMAIN="${DOMAIN:-}"
SERVER_IP="${SERVER_IP:-}"
ACME_PORT="${ACME_PORT:-80}"

if [ -z "$PANEL_PASS" ]; then
  PANEL_PASS="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)"
  echo "未提供 PANEL_PASS，已随机生成"
fi
if [ -z "$PANEL_PATH" ]; then
  PANEL_PATH="$(head -c 24 /dev/urandom | base64 | tr -dc 'a-z0-9' | head -c 16)"
  echo "未提供 PANEL_PATH，已随机生成"
fi

echo "########## 安装参数 ##########"
echo "PANEL_USER = ${PANEL_USER}"
echo "PANEL_PASS = ***"
echo "PANEL_PORT = ${PANEL_PORT}"
echo "PANEL_PATH = ${PANEL_PATH}"
echo "DOMAIN     = ${DOMAIN}"
echo "SERVER_IP  = ${SERVER_IP}"

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

# ---------------------------------------------------------------- 安装
echo ""
echo "########## 2. 下载官方安装脚本 ##########"
curl -Ls --retry 3 --connect-timeout 20 -o /root/3xui-install.sh \
  https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh
ls -l /root/3xui-install.sh
head -3 /root/3xui-install.sh

echo ""
echo "########## 3. 设置非交互参数 ##########"
export XUI_NONINTERACTIVE=1
export XUI_USERNAME="$PANEL_USER"
export XUI_PASSWORD="$PANEL_PASS"
export XUI_PANEL_PORT="$PANEL_PORT"
export XUI_WEB_BASE_PATH="$PANEL_PATH"
export XUI_ACME_HTTP_PORT="$ACME_PORT"

if [ -n "$DOMAIN" ]; then
  export XUI_SSL_MODE="domain"
  export XUI_DOMAIN="$DOMAIN"
else
  export XUI_SSL_MODE="ip"
  export XUI_SERVER_IP="$SERVER_IP"
fi

env | grep '^XUI_' | sed 's/\(PASSWORD=\).*/\1***/'

echo ""
echo "########## 4. 执行安装 ##########"
bash /root/3xui-install.sh 2>&1 | tee /root/3xui-install.log
INSTALL_RC="${PIPESTATUS[0]}"
echo "安装脚本退出码: ${INSTALL_RC}"

# ---------------------------------------------------------------- 校验
echo ""
echo "########## 5. 服务状态 ##########"
systemctl is-active x-ui || true
systemctl is-enabled x-ui || true
ss -lntp | grep -E ":${PANEL_PORT} " || echo "⚠️  面板端口 ${PANEL_PORT} 未监听"

echo ""
echo "########## 6. 面板设置 ##########"
/usr/local/x-ui/x-ui setting -show 2>/dev/null || true

echo ""
echo "########## 7. 证书 ##########"
if [ -n "$DOMAIN" ]; then
  ls -l "/root/cert/${DOMAIN}/" 2>/dev/null || \
  ls -l "/root/.acme.sh/${DOMAIN}_ecc/" 2>/dev/null || \
  echo "⚠️  未找到证书目录，检查 acme.sh 日志"
fi

echo ""
echo "########## 8. 获取 API Token ##########"
# ⚠️ 3x-ui v3.9.0 起，API Token 挪到了独立的 api_tokens 表，且**只存 SHA-256**。
#    老教程里的 `sqlite3 /etc/x-ui/x-ui.db "select value from settings where key='apiToken'"`
#    在 v3.9.0 上**返回空**（settings 表里已无该键）。
#    正确姿势：CSRF → 登录 → POST /panel/api/setting/apiTokens/create，明文只在创建时返回一次。
#    详见 references/xui-api.md §1.1 与 references/pitfalls.md §2.7。
API_TOKEN=""
# ⚠️ 用域名拼 URL + `--resolve` 把域名钉到 127.0.0.1，而不是「URL 写 127.0.0.1 + 加 Host 头」。
#    原因：curl 的 cookie 引擎按 **URL 的 host** 匹配 cookie，不看 Host 头。
#    URL 写 127.0.0.1 时，登录拿到的 `3x-ui=<session>` cookie 属于域名，
#    后续请求不会带上 → 登录接口 403 空响应、Token 建不出来（v3.10.2 实测踩到）。
if [ -n "$DOMAIN" ]; then
  PANEL_BASE="https://${DOMAIN}:${PANEL_PORT}"
else
  PANEL_BASE="https://127.0.0.1:${PANEL_PORT}"
fi
[ -n "$PANEL_PATH" ] && PANEL_BASE="${PANEL_BASE}/${PANEL_PATH}"

api_curl() {
  if [ -n "$DOMAIN" ]; then
    curl -sk --max-time 20 --resolve "${DOMAIN}:${PANEL_PORT}:127.0.0.1" "$@"
  else
    curl -sk --max-time 20 "$@"
  fi
}

# 面板刚重启，给它几秒钟起来
for _ in 1 2 3 4 5 6 7 8 9 10; do
  ss -lnt 2>/dev/null | grep -q ":${PANEL_PORT} " && break
  sleep 1
done

COOKIE_JAR="$(mktemp)"
CSRF="$(api_curl -c "$COOKIE_JAR" "${PANEL_BASE}/csrf-token" 2>/dev/null \
        | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("obj") or "")
except Exception:
    print("")' 2>/dev/null || true)"

if [ -n "$CSRF" ]; then
  # ⚠️ 登录接口也要带 X-CSRF-Token，否则 403 空响应
  api_curl -b "$COOKIE_JAR" -c "$COOKIE_JAR" -X POST \
    -H "X-CSRF-Token: ${CSRF}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-urlencode "username=${PANEL_USER}" \
    --data-urlencode "password=${PANEL_PASS}" \
    "${PANEL_BASE}/login" >/dev/null 2>&1 || true

  TOKEN_RESP="$(api_curl -b "$COOKIE_JAR" -c "$COOKIE_JAR" -X POST \
    -H "X-CSRF-Token: ${CSRF}" -H "Content-Type: application/json" \
    -d '{"name":"skill-install"}' \
    "${PANEL_BASE}/panel/api/setting/apiTokens/create" 2>/dev/null || true)"

  # ⚠️ 返回体形状随版本变化：
  #   v3.9.x  → {"success":true,"obj":"<明文 token>"}
  #   v3.10.x → {"success":true,"obj":{"id":1,"name":"..","token":"<明文>"}}
  API_TOKEN="$(printf '%s' "$TOKEN_RESP" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); raise SystemExit
o = d.get("obj")
if isinstance(o, str):
    print(o)
elif isinstance(o, dict):
    print(o.get("token") or "")
else:
    print("")' 2>/dev/null || true)"
fi
rm -f "$COOKIE_JAR"

if [ -n "$API_TOKEN" ]; then
  echo "API_TOKEN = ${API_TOKEN}"
else
  echo "⚠️  自动获取 API Token 失败（CSRF/登录路径与当前版本不符，或面板尚未就绪）。"
  echo "    请打开面板「设置 → 安全 → API Token」手动新建，再回填 deploy.env 的 API_TOKEN。"
  echo "    参考 references/xui-api.md §1.1。"
fi

# ---------------------------------------------------------------- 结果落盘
echo ""
echo "########## 9. 写入 /etc/x-ui/install-result.env ##########"
cat > /etc/x-ui/install-result.env <<EOF
PANEL_USER=${PANEL_USER}
PANEL_PASS=${PANEL_PASS}
PANEL_PORT=${PANEL_PORT}
PANEL_PATH=${PANEL_PATH}
DOMAIN=${DOMAIN}
SERVER_IP=${SERVER_IP}
API_TOKEN=${API_TOKEN}
PANEL_URL=https://${DOMAIN:-$SERVER_IP}:${PANEL_PORT}/${PANEL_PATH}/
INSTALL_RC=${INSTALL_RC}
EOF
chmod 600 /etc/x-ui/install-result.env
cat /etc/x-ui/install-result.env

echo ""
echo "########## INSTALL DONE ##########"
