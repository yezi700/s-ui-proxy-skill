#!/bin/bash
# ============================================================================
# 端到端验证（在 VPS 上执行）
#
# 通过 `python scripts/ssh_run.py -f scripts/verify_nodes.sh` 执行。
#
# ⚠️ 本脚本在 VPS 上跑，所有「连自己公网 IP」的探测都走 lo 接口，
#    **不经过 INPUT 链**，因此无法验证防火墙。
#    它只能证明：服务在跑、端口在听、协议配置正确、订阅能生成。
#    外部可达性必须从本机用真实客户端拨号（见 SKILL.md Step 7）。
# ============================================================================
set -uo pipefail

DOMAIN="${DOMAIN:-}"
PANEL_PORT="${PANEL_PORT:-46821}"
PANEL_PATH="${PANEL_PATH:-}"
SUB_PORT="${SUB_PORT:-2096}"
SUB_PATH="${SUB_PATH:-}"
SUB_ID="${SUB_ID:-us}"
ANYTLS_PORT="${ANYTLS_PORT:-8443}"
TUIC_PORT="${TUIC_PORT-}"           # 留空 = 不检查（默认不部署 TUIC）
SS_PORT="${SS_PORT-2087}"           # 留空 = 不检查
HY2_PORT="${HY2_PORT:-2083}"
REALITY_PORT="${REALITY_PORT:-2053}"
PANEL_USER="${PANEL_USER:-admin}"
PANEL_PASS="${PANEL_PASS:-}"

PASS_CNT=0
FAIL_CNT=0
ok()   { echo "  [PASS] $*"; PASS_CNT=$((PASS_CNT+1)); }
bad()  { echo "  [FAIL] $*"; FAIL_CNT=$((FAIL_CNT+1)); }
warn() { echo "  [WARN] $*"; }

# ---------------------------------------------------------------- 1. 服务
echo "########## 1. 服务状态 ##########"
if [ "$(systemctl is-active s-ui)" = "active" ]; then
  ok "s-ui 服务运行中"
else
  bad "s-ui 未运行"
  journalctl -u s-ui --no-pager -n 20 | tail -10
fi

echo "--- 最近错误 ---"
journalctl -u s-ui --no-pager -n 200 2>/dev/null \
  | grep -iE "error|fatal|panic" | grep -v "record not found" | tail -5 || echo "  (无)"

# ---------------------------------------------------------------- 2. 端口
echo ""
echo "########## 2. 端口监听 ##########"
check_listen() {
  local proto="$1" port="$2" name="$3"
  if [ "$proto" = "tcp" ]; then
    if ss -lnt 2>/dev/null | grep -q ":${port} "; then
      ok "${name} tcp/${port} 监听中"
    else
      bad "${name} tcp/${port} 未监听"
    fi
  else
    if ss -lnu 2>/dev/null | grep -q ":${port} "; then
      ok "${name} udp/${port} 监听中"
    else
      bad "${name} udp/${port} 未监听"
    fi
  fi
}
check_listen tcp "$PANEL_PORT"   "面板"
check_listen tcp "$SUB_PORT"     "订阅"
check_listen tcp "$ANYTLS_PORT"  "AnyTLS"
[ -n "$TUIC_PORT" ] && check_listen udp "$TUIC_PORT" "TUIC"
check_listen udp "$HY2_PORT"     "Hysteria2"
check_listen tcp "$REALITY_PORT" "Reality"
if [ -n "$SS_PORT" ]; then
  # SS 入站默认带 UDP relay，sing-box 会同时监听 tcp 和 udp；
  # 两个都要在，否则客户端开 UDP 转发会静默失败
  check_listen tcp "$SS_PORT" "Shadowsocks"
  check_listen udp "$SS_PORT" "Shadowsocks"
fi

# ---------------------------------------------------------------- 3. 核心配置
echo ""
echo "########## 3. sing-box 运行配置 ##########"
SCHEME="https"
# ⚠️ 必须用域名访问 + --resolve 钉到 127.0.0.1：
#    面板证书与 webDomain 都是按域名配的，直接用 127.0.0.1 会因 SNI 不匹配
#    被拒（tls: unrecognized server name）。
HOST="${DOMAIN:-127.0.0.1}"
RESOLVE="--resolve ${HOST}:${PANEL_PORT}:127.0.0.1 --resolve ${HOST}:${SUB_PORT}:127.0.0.1"
PANEL_BASE="${SCHEME}://${HOST}:${PANEL_PORT}/${PANEL_PATH}"
ORIGIN="${SCHEME}://${HOST}:${PANEL_PORT}"

curl -sk $RESOLVE -c /tmp/v.ck -X POST "${PANEL_BASE}/api/login" \
  -H "Origin: ${ORIGIN}" \
  --data-urlencode "user=${PANEL_USER}" \
  --data-urlencode "pass=${PANEL_PASS}" >/dev/null 2>&1 || true

SB_JSON="$(curl -sk $RESOLVE -b /tmp/v.ck "${PANEL_BASE}/api/singbox-config" 2>/dev/null || true)"
if [ -z "$SB_JSON" ]; then
  bad "取不到 sing-box 配置（登录失败？检查 PANEL_PASS）"
else
  printf '%s' "$SB_JSON" | python3 -c '
import json, sys
try:
    cfg = json.load(sys.stdin)
except Exception as e:
    print("  [FAIL] 配置不是 JSON:", e)
    raise SystemExit(1)
ibs = cfg.get("inbounds") or []
if not ibs:
    print("  [FAIL] sing-box 配置里没有 inbounds")
    raise SystemExit(1)
for ib in ibs:
    users = ib.get("users") or []
    tag = ib.get("tag", "?")
    typ = ib.get("type", "?")
    port = ib.get("listen_port")
    alpn = (ib.get("tls") or {}).get("alpn")
    if users:
        print("  [PASS] %-22s %-10s :%s  users=%d  alpn=%s" % (tag, typ, port, len(users), alpn))
    else:
        print("  [FAIL] %-22s %-10s :%s  无用户（订阅里不会有这个节点）" % (tag, typ, port))
'

  printf '%s' "$SB_JSON" | python3 -c '
import json, sys, base64
cfg = json.load(sys.stdin)
for ib in cfg.get("inbounds") or []:
    if ib.get("type") == "tuic":
        alpn = (ib.get("tls") or {}).get("alpn") or []
        if "h3" in alpn:
            print("  [PASS] TUIC alpn 含 h3")
        else:
            print("  [FAIL] TUIC alpn 不含 h3 → 客户端会报 server did not select an ALPN protocol")
        break
for ib in cfg.get("inbounds") or []:
    if ib.get("type") != "shadowsocks":
        continue
    m = ib.get("method") or ""
    p = ib.get("password") or ""
    # SS2022 的 psk 长度由 method 决定，长度不对 sing-box 起不来（invalid password）
    want = 16 if "128" in m else (32 if m.startswith("2022-") else 0)
    try:
        got = len(base64.b64decode(p))
    except Exception:
        got = -1
    if m.startswith("2022-") and got != want:
        print("  [FAIL] SS %s psk 解出 %d 字节，应为 %d 字节" % (m, got, want))
    elif not p:
        print("  [FAIL] SS 入站缺 psk → initialize inbound: missing psk")
    else:
        print("  [PASS] SS method=%s  psk=%d 字节" % (m, got))
'
fi

# ---------------------------------------------------------------- 4. 订阅
echo ""
echo "########## 4. 订阅端点 ##########"
SUB_BASE="${SCHEME}://${HOST}:${SUB_PORT}/${SUB_PATH}/${SUB_ID}"

check_sub() {
  local label="$1" url="$2" expect="$3"
  local code size
  code="$(curl -sk $RESOLVE --max-time 20 -o /tmp/sub.body -w '%{http_code}' "$url" 2>/dev/null)"
  code="${code:-000}"
  if [ "$code" != "200" ]; then
    bad "${label} HTTP ${code}"
    return
  fi
  size="$(wc -c < /tmp/sub.body)"
  case "$expect" in
    base64)
      if head -c 40 /tmp/sub.body | grep -qE '^[A-Za-z0-9+/=]+$'; then
        ok "${label} 200，base64（${size}B）"
      else
        bad "${label} 不是 base64"
      fi
      ;;
    clash)
      if grep -q "^proxies:" /tmp/sub.body; then
        local n
        # 节点是 proxies 列表项，缩进 4 空格 + "- name:"；
        # 用 6 空格的 "^      name:" 会把 proxy-groups 里的 Auto 也算进来，数目偏多
        n="$(grep -cE "^    - name: " /tmp/sub.body || true)"
        ok "${label} 200，Clash YAML，节点 ${n} 个"
        if grep -q "^      udp: true" /tmp/sub.body; then
          ok "${label} 含 udp: true"
        else
          warn "${label} 未见 udp: true（UDP 类协议可能不通）"
        fi
      else
        bad "${label} 不是 Clash YAML"
      fi
      ;;
    json)
      if python3 -c 'import json; json.load(open("/tmp/sub.body"))' 2>/dev/null; then
        ok "${label} 200，合法 JSON"
      else
        bad "${label} 不是合法 JSON"
      fi
      ;;
  esac
}

check_sub "通用订阅"   "${SUB_BASE}"              base64
check_sub "Clash 订阅" "${SUB_BASE}?format=clash" clash
check_sub "JSON 订阅"  "${SUB_BASE}?format=json"  json

if curl -sk $RESOLVE --max-time 15 -D /tmp/sub.hdr -o /dev/null "${SUB_BASE}" 2>/dev/null; then
  if grep -qi "subscription-userinfo" /tmp/sub.hdr; then
    ok "订阅响应头含 Subscription-Userinfo"
  else
    warn "订阅响应头缺 Subscription-Userinfo（面板设置 subShowInfo）"
  fi
fi

rm -f /tmp/v.ck /tmp/sub.body /tmp/sub.hdr

# ---------------------------------------------------------------- 5. 证书
echo ""
echo "########## 5. 证书 ##########"
CERT_DIR="${CERT_DIR:-/root/cert/${DOMAIN}}"
if [ -f "${CERT_DIR}/fullchain.pem" ]; then
  END="$(openssl x509 -in "${CERT_DIR}/fullchain.pem" -noout -enddate 2>/dev/null | cut -d= -f2)"
  SUBJ="$(openssl x509 -in "${CERT_DIR}/fullchain.pem" -noout -subject 2>/dev/null | sed 's/^subject=//')"
  ok "证书存在，到期 ${END}"
  echo "         ${SUBJ}"
  if [ -f "${CERT_DIR}/privkey.pem" ]; then
    ok "私钥存在"
  else
    bad "私钥缺失"
  fi
else
  warn "未找到证书 ${CERT_DIR}（面板可能走 HTTP）"
fi

# ---------------------------------------------------------------- 汇总
echo ""
echo "============================================================"
echo "  通过 ${PASS_CNT} 项，失败 ${FAIL_CNT} 项"
echo "============================================================"
echo ""
echo "⚠️  以上全部在 VPS 本机完成，走 lo 接口，**没有验证防火墙**。"
echo "   外部可达性请从本机用真实客户端拨号，或对每个端口做外部 TCP 探测。"

[ "$FAIL_CNT" -eq 0 ]
