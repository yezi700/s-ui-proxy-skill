#!/bin/bash
# ============================================================================
# 端到端拨号验证：在 VPS 本机起真实客户端，逐个节点连出去访问外网。
#
# 通过 `python scripts/ssh_run.py -f scripts/verify_nodes.sh` 执行。
#
# ⚠️ 关键陷阱：VPS 访问自己的公网 IP 走 lo 接口，**不经过 INPUT 链**，
#    因此本脚本只能验证「协议与凭据是否正确」，
#    **不能**验证防火墙放行是否生效。外部可达性必须另测（见文件末尾）。
#
# ⚠️ 另一个坑：起客户端 → 测试 → 清理必须写在同一个脚本里。
#    如果拆成多次工具调用，后台进程会在命令结束时被回收，第二次调用就找不到它了。
# ============================================================================
set -uo pipefail

WORK=/root/.xui-skill
BIN=$WORK/bin
mkdir -p "$BIN"
cd "$WORK" || exit 1

REALITY_UUID="${VERIFY_REALITY_UUID:-}"
HY2_AUTH="${VERIFY_HY2_AUTH:-}"
TUIC_UUID="${VERIFY_TUIC_UUID:-}"
TUIC_PASSWORD="${VERIFY_TUIC_PASSWORD:-}"
REALITY_PORT="${REALITY_PORT:-443}"
HY2_PORT="${HY2_PORT:-443}"
TUIC_PORT="${TUIC_PORT:-8443}"
HY2_HOP_RANGE="${HY2_HOP_RANGE:-58888:60888}"
REALITY_SNI="${REALITY_SNI:-www.apple.com}"
HY2_OBFS_PW="${VERIFY_HY2_OBFS_PW:-}"

if [ -z "$REALITY_UUID" ] && [ -f "$WORK/node-credentials.json" ]; then
  REALITY_UUID="$(python3 -c "import json;print(json.load(open('$WORK/node-credentials.json')).get('reality_uuid',''))")"
  HY2_AUTH="$(python3     -c "import json;print(json.load(open('$WORK/node-credentials.json')).get('hy2_auth',''))")"
  TUIC_UUID="$(python3    -c "import json;print(json.load(open('$WORK/node-credentials.json')).get('tuic_uuid',''))")"
  TUIC_PASSWORD="$(python3 -c "import json;print(json.load(open('$WORK/node-credentials.json')).get('tuic_password',''))")"
  HY2_OBFS_PW="$(python3 -c "import json;print(json.load(open('$WORK/node-credentials.json')).get('hy2_obfs_pw',''))")"
fi

echo "########## 凭据 ##########"
echo "REALITY_UUID  = ${REALITY_UUID:-<空>}"
echo "HY2_AUTH      = ${HY2_AUTH:-<空>}"
echo "TUIC_UUID     = ${TUIC_UUID:-<空>}"
echo "TUIC_PASSWORD = ${TUIC_PASSWORD:-<空>}"

# ---------------------------------------------------------------- 清理函数
PIDS=()
cleanup() {
  echo ""
  echo "########## 清理 ##########"
  for p in "${PIDS[@]:-}"; do
    [ -n "$p" ] && kill "$p" 2>/dev/null && echo "已停止 PID $p"
  done
  sleep 1
  for p in "${PIDS[@]:-}"; do
    [ -n "$p" ] && kill -9 "$p" 2>/dev/null
  done
  rm -f "$WORK"/*.pid
}
trap cleanup EXIT

check_port_free() {
  local port="$1"
  if ss -lnt 2>/dev/null | grep -q ":${port} "; then
    echo "⚠️  端口 ${port} 已被占用，先释放："
    ss -lntp | grep ":${port} "
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------- 准备客户端
echo ""
echo "########## 1. 准备客户端二进制 ##########"
XRAY_BIN=/usr/local/x-ui/bin/xray-linux-amd64
if [ ! -x "$XRAY_BIN" ]; then
  XRAY_BIN="$BIN/xray"
  if [ ! -x "$XRAY_BIN" ]; then
    echo "下载 xray..."
    curl -sSL -o "$BIN/xray.zip" \
      "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip"
    (cd "$BIN" && unzip -oq xray.zip && chmod +x xray)
  fi
fi
echo "XRAY_BIN = $XRAY_BIN"
"$XRAY_BIN" -version 2>&1 | head -2

SB_BIN="$BIN/sing-box"
if [ ! -x "$SB_BIN" ]; then
  echo "下载 sing-box..."
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64) SBARCH=amd64 ;;
    aarch64) SBARCH=arm64 ;;
    *) SBARCH=amd64 ;;
  esac
  VER="$(curl -sSL https://api.github.com/repos/SagerNet/sing-box/releases/latest \
        | grep -o '"tag_name": *"v[^"]*"' | head -1 | tr -d '" ' | cut -d: -f2)"
  VER="${VER:-v1.11.15}"
  echo "sing-box $VER ($SBARCH)"
  curl -sSL -o "$BIN/sb.tar.gz" \
    "https://github.com/SagerNet/sing-box/releases/download/${VER}/sing-box-${VER#v}-linux-${SBARCH}.tar.gz"
  tar -xzf "$BIN/sb.tar.gz" -C "$BIN"
  find "$BIN" -name sing-box -type f -exec cp -f {} "$SB_BIN" \;
  chmod +x "$SB_BIN"
fi
echo "SB_BIN   = $SB_BIN"
"$SB_BIN" version 2>&1 | head -1

# ---------------------------------------------------------------- 2. REALITY
echo ""
echo "########## 2. REALITY (TCP ${REALITY_PORT}) ##########"
REALITY_SOCKS=21080

# ⚠️ 必须【先】把 publicKey / shortId / serverName 从入站回读出来，
#    再写客户端配置。顺序反了的话 publicKey 是空的，握手必然失败，
#    会被误判成"服务端坏了"。
REALITY_PUB="${REALITY_PUB:-}"
REALITY_SID="${REALITY_SID:-}"
if [ -n "${API_TOKEN:-}" ] && [ -n "${PANEL_PORT:-}" ] && [ -n "${PANEL_PATH:-}" ]; then
  curl -sk -H "Host: ${DOMAIN}" -H "Authorization: Bearer ${API_TOKEN}" \
    "https://127.0.0.1:${PANEL_PORT}/${PANEL_PATH}/panel/api/inbounds/list" \
    > "$WORK/ib.json" 2>/dev/null || true

  # ⚠️ settings/streamSettings 在不同 3x-ui 版本里可能是对象也可能是 JSON 字符串，
  #    两种都要处理（json.loads(dict) 会抛 TypeError）。
  python3 - "$WORK/ib.json" > "$WORK/reality_vars.sh" <<'PYEOF'
import json, shlex, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    sys.stderr.write("回读入站失败: %s\n" % e)
    raise SystemExit(0)

def obj(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except Exception:
            return {}
    return {}

for ib in (d.get("obj") or []):
    ss = obj(ib.get("streamSettings"))
    rs = ss.get("realitySettings") if isinstance(ss, dict) else None
    if ib.get("protocol") == "vless" and isinstance(rs, dict):
        pub = ((rs.get("settings") or {}).get("publicKey")) or ""
        sid = (rs.get("shortIds") or [""])[0] or ""
        sni = (rs.get("serverNames") or [""])[0] or ""
        print("REALITY_PUB=%s" % shlex.quote(pub))
        print("REALITY_SID=%s" % shlex.quote(sid))
        if sni:
            print("REALITY_SNI=%s" % shlex.quote(sni))
        break
PYEOF

  # shellcheck disable=SC1090
  [ -s "$WORK/reality_vars.sh" ] && . "$WORK/reality_vars.sh"
fi

echo "REALITY_PUB = ${REALITY_PUB:-<空>}"
echo "REALITY_SID = ${REALITY_SID:-<空>}"
echo "REALITY_SNI = ${REALITY_SNI:-<空>}"

if [ -z "$REALITY_PUB" ]; then
  echo "!!! 没能回读到 REALITY 公钥（publicKey），跳过 REALITY 拨号测试。"
  echo "    原因通常是 API_TOKEN / PANEL_PORT / PANEL_PATH 未注入。"
  echo "    临时办法：在 deploy.env 里显式填 REALITY_PUB 与 REALITY_SID。"
elif check_port_free $REALITY_SOCKS; then
  cat > "$WORK/cfg_reality.json" <<EOF
{
  "log": {"loglevel": "warning"},
  "inbounds": [{
    "tag": "socks-in", "listen": "127.0.0.1", "port": ${REALITY_SOCKS},
    "protocol": "socks", "settings": {"udp": true, "auth": "noauth"}
  }],
  "outbounds": [{
    "tag": "proxy", "protocol": "vless",
    "settings": {"vnext": [{
      "address": "127.0.0.1", "port": ${REALITY_PORT},
      "users": [{"id": "${REALITY_UUID}", "encryption": "none",
                 "flow": "xtls-rprx-vision"}]
    }]},
    "streamSettings": {
      "network": "tcp", "security": "reality",
      "realitySettings": {
        "serverName": "${REALITY_SNI}", "fingerprint": "chrome",
        "publicKey": "${REALITY_PUB}", "shortId": "${REALITY_SID}",
        "spiderX": "/"
      }
    }
  }]
}
EOF

  "$XRAY_BIN" run -c "$WORK/cfg_reality.json" > "$WORK/reality.log" 2>&1 &
  RPID=$!
  PIDS+=("$RPID")
  echo "$RPID" > "$WORK/reality.pid"
  sleep 4

  if kill -0 "$RPID" 2>/dev/null; then
    echo "客户端已启动 (PID $RPID)"
    OK=0
    for i in 1 2 3 4 5; do
      T0=$(date +%s.%N)
      CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 \
             --socks5-hostname "127.0.0.1:${REALITY_SOCKS}" \
             https://www.youtube.com/ 2>/dev/null || echo 000)
      T1=$(date +%s.%N)
      echo "  第 $i 次: HTTP $CODE  用时 $(echo "$T1 - $T0" | bc)s"
      [ "$CODE" = "200" ] && OK=$((OK+1))
    done
    echo "REALITY 成功 $OK/5"
  else
    echo "!!! REALITY 客户端启动失败，日志："
    tail -20 "$WORK/reality.log"
  fi
fi

# ---------------------------------------------------------------- 3. Hysteria2
echo ""
echo "########## 3. Hysteria2 (UDP ${HY2_PORT} + 跳跃 ${HY2_HOP_RANGE}) ##########"
HY2_SOCKS=21081
if check_port_free $HY2_SOCKS; then
  # ⚠️ sing-box ≥ 1.12 把 log.loglevel 改名为 log.level；
  #    写旧键名会直接 FATAL 退出（"unknown field loglevel"），被误判成客户端起不来。
  cat > "$WORK/cfg_hy2.json" <<EOF
{
  "log": {"level": "warn"},
  "inbounds": [{
    "type": "socks", "tag": "socks-in",
    "listen": "127.0.0.1", "listen_port": ${HY2_SOCKS}
  }],
  "outbounds": [{
    "type": "hysteria2", "tag": "proxy",
    "server": "127.0.0.1", "server_port": ${HY2_PORT},
    "password": "${HY2_AUTH}",
    "obfs": {"type": "salamander", "password": "${HY2_OBFS_PW}"},
    "tls": {"enabled": true, "server_name": "${DOMAIN}",
            "insecure": false, "alpn": ["h3"]}
  }]
}
EOF
  "$SB_BIN" run -c "$WORK/cfg_hy2.json" > "$WORK/hy2.log" 2>&1 &
  HPID=$!
  PIDS+=("$HPID")
  echo "$HPID" > "$WORK/hy2.pid"
  sleep 4

  if kill -0 "$HPID" 2>/dev/null; then
    echo "客户端已启动 (PID $HPID)"
    OK=0
    for i in 1 2 3 4 5; do
      T0=$(date +%s.%N)
      CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 \
             --socks5-hostname "127.0.0.1:${HY2_SOCKS}" \
             https://www.youtube.com/ 2>/dev/null || echo 000)
      T1=$(date +%s.%N)
      echo "  第 $i 次: HTTP $CODE  用时 $(echo "$T1 - $T0" | bc)s"
      [ "$CODE" = "200" ] && OK=$((OK+1))
    done
    echo "Hysteria2 成功 $OK/5"
  else
    echo "!!! Hysteria2 客户端启动失败，日志："
    tail -20 "$WORK/hy2.log"
  fi
fi

# ---------------------------------------------------------------- 4. TUIC
echo ""
echo "########## 4. TUIC v5 (UDP ${TUIC_PORT}) ##########"
TUIC_SOCKS=21082
if check_port_free $TUIC_SOCKS; then
  cat > "$WORK/cfg_tuic.json" <<EOF
{
  "log": {"level": "warn"},
  "inbounds": [{
    "type": "socks", "tag": "socks-in",
    "listen": "127.0.0.1", "listen_port": ${TUIC_SOCKS}
  }],
  "outbounds": [{
    "type": "tuic", "tag": "proxy",
    "server": "127.0.0.1", "server_port": ${TUIC_PORT},
    "uuid": "${TUIC_UUID}", "password": "${TUIC_PASSWORD}",
    "congestion_control": "bbr", "udp_relay_mode": "native",
    "zero_rtt_handshake": false,
    "tls": {"enabled": true, "server_name": "${DOMAIN}",
            "insecure": false, "alpn": ["h3"]}
  }]
}
EOF
  "$SB_BIN" run -c "$WORK/cfg_tuic.json" > "$WORK/tuic.log" 2>&1 &
  TPID=$!
  PIDS+=("$TPID")
  echo "$TPID" > "$WORK/tuic.pid"
  sleep 4

  if kill -0 "$TPID" 2>/dev/null; then
    echo "客户端已启动 (PID $TPID)"
    OK=0
    for i in 1 2 3 4 5; do
      T0=$(date +%s.%N)
      CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 \
             --socks5-hostname "127.0.0.1:${TUIC_SOCKS}" \
             https://www.youtube.com/ 2>/dev/null || echo 000)
      T1=$(date +%s.%N)
      echo "  第 $i 次: HTTP $CODE  用时 $(echo "$T1 - $T0" | bc)s"
      [ "$CODE" = "200" ] && OK=$((OK+1))
    done
    echo "TUIC 成功 $OK/5"

    echo "--- UDP relay 独立验证（DNS over UDP through TUIC）---"
    python3 - "$TUIC_SOCKS" <<'PYEOF' || echo "UDP relay 验证失败（不影响 TCP 可用性）"
import socket, struct, sys, random
socks_port = int(sys.argv[1])
s = socket.create_connection(("127.0.0.1", socks_port), timeout=10)
s.sendall(b"\x05\x01\x00")
if s.recv(2) != b"\x05\x00":
    raise SystemExit("SOCKS5 协商失败")
# UDP ASSOCIATE
s.sendall(b"\x05\x03\x00\x01" + socket.inet_aton("0.0.0.0") + struct.pack("!H", 0))
resp = s.recv(10)
if len(resp) < 10 or resp[1] != 0:
    raise SystemExit("UDP ASSOCIATE 失败: %r" % resp)
relay = (socket.inet_ntoa(resp[4:8]), struct.unpack("!H", resp[8:10])[0])
if relay[0] == "0.0.0.0":
    relay = ("127.0.0.1", relay[1])
print("UDP relay:", relay)
# 构造 DNS 查询 www.google.com A
tid = random.randint(0, 65535)
q = struct.pack("!HHHHHH", tid, 0x0100, 1, 0, 0, 0)
for part in b"www.google.com".split(b"."):
    q += bytes([len(part)]) + part
q += b"\x00" + struct.pack("!HH", 1, 1)
pkt = b"\x00\x00\x00\x01" + socket.inet_aton("8.8.8.8") + struct.pack("!H", 53) + q
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
u.settimeout(12)
u.sendto(pkt, relay)
data, _ = u.recvfrom(2048)
ancount = struct.unpack("!H", data[6:8])[0]
print("DNS 应答 A 记录数:", ancount)
assert ancount > 0, "无 A 记录"
print("UDP relay OK")
PYEOF
  else
    echo "!!! TUIC 客户端启动失败，日志："
    tail -20 "$WORK/tuic.log"
  fi
fi

# ---------------------------------------------------------------- 4.5 订阅端点
# 协议通了不代表订阅能用。这里同时验两条：通用（base64）与 Clash（YAML）。
# ⚠️ 判据是 Content-Type 与内容结构，不是 HTTP 200 —— 路径错时 3x-ui 会返回
#    404 页面而不是空响应，只看状态码很容易漏。
echo ""
echo "########## 4.5 订阅端点 ##########"
SP="${SUB_PATH#/}"; SP="${SP%/}"
if [ -z "${MERGED_SUBID:-}" ]; then
  echo "⚠️  未提供 MERGED_SUBID，跳过订阅检查（合并订阅后再跑一次）"
else
  # ---- ① 通用 ----
  GEN="https://127.0.0.1:${SUB_PORT}/${SP}/${MERGED_SUBID}"
  echo "① 通用: ${GEN}"
  CT="$(curl -fskS -H "Host: ${DOMAIN}" -D - -o "$WORK/sub_check.txt" "$GEN" 2>/dev/null \
        | grep -i '^content-type:' | tr -d '\r')"
  echo "   ${CT:-（无 content-type）}"
  python3 - <<'PYEOF'
import base64, pathlib
raw = pathlib.Path('/root/.xui-skill/sub_check.txt').read_bytes().strip()
try:
    txt = base64.b64decode(raw + b'=' * (-len(raw) % 4)).decode('utf-8', 'replace')
except Exception:
    txt = raw.decode('utf-8', 'replace')
links = [l.strip() for l in txt.splitlines() if '://' in l]
print('   解码后节点数: %d' % len(links))
for l in links:
    print('     - %s' % l.split('://')[0])
if not links:
    raise SystemExit('   !! 通用订阅里没有节点，检查 subId 是否绑定了入站')
PYEOF

  # ---- ② Clash / mihomo ----
  # subClashEnable 默认 false，此时该路径 404；必须先开再重启面板
  curl -fskS -X POST -H "Host: ${DOMAIN}" -H "Authorization: Bearer ${API_TOKEN}" \
    "https://127.0.0.1:${PANEL_PORT}/${PANEL_PATH}/panel/api/setting/all" \
    > "$WORK/settings_check.json" 2>/dev/null || echo '{}' > "$WORK/settings_check.json"
  cat > "$WORK/clashpath.py" <<'PYEOF'
import json
o = (json.load(open('/root/.xui-skill/settings_check.json')).get('obj') or {})
print((o.get('subClashPath') or '').strip('/') if o.get('subClashEnable') else '')
PYEOF
  CP="$(python3 "$WORK/clashpath.py" 2>/dev/null || true)"
  if [ -n "$CP" ]; then
    CL="https://127.0.0.1:${SUB_PORT}/${CP}/${MERGED_SUBID}"
    echo "② Clash: ${CL}"
    CT2="$(curl -fskS -H "Host: ${DOMAIN}" -D - -o "$WORK/clash_check.yaml" "$CL" 2>/dev/null \
           | grep -i '^content-type:' | tr -d '\r')"
    echo "   ${CT2:-（无 content-type）}"
    python3 - <<'PYEOF'
import pathlib, re
txt = pathlib.Path('/root/.xui-skill/clash_check.yaml').read_text(encoding='utf-8', errors='replace')
ok = True
for key in ('proxies:', 'proxy-groups:', 'rules:'):
    have = key in txt
    ok = ok and have
    print('   %-15s %s' % (key, '有' if have else '!! 缺失'))
print('   节点数: %d' % len(re.findall(r'^\s+name:\s', txt, re.M)))
if not ok:
    raise SystemExit('   !! Clash 配置结构不完整（可能拿到的是 404 页面）')
PYEOF
  else
    echo "② Clash: 未启用（subClashEnable=false）"
    echo "   Clash 系客户端需要它：面板「设置 → 订阅 → Clash」开启后重启面板"
    echo "   （deploy_nodes.py 会自动开启；手工面板需自行处理）"
  fi
fi

# ---------------------------------------------------------------- 5. 服务端状态
echo ""
echo "########## 5. 服务端监听与规则 ##########"
systemctl is-active x-ui
ss -lntup | grep -E ":(${REALITY_PORT}|${HY2_PORT}|${TUIC_PORT}|${PANEL_PORT})\b" || true
echo "--- INPUT 链 ---"
iptables -S INPUT
echo "--- nat PREROUTING ---"
iptables -t nat -S PREROUTING

echo ""
echo "########## VERIFY DONE ##########"
cat <<'NOTE'

⚠️ 以上验证全部在 VPS 本机完成，走的是 lo 回环，不经过 INPUT 链。
   因此它只能证明「协议与凭据正确」，不能证明「外部能连上」。

   要验证防火墙与外部可达性，必须从外部网络做：
     1. 在本机用真实客户端（v2rayN / mihomo / sing-box）导入订阅并拨号
     2. 或用在线端口探测（TCP 443/46821/2096、UDP 443/8443 需支持 UDP 的探测）
     3. 或从另一台机器执行：
          nc -vz <公网IP> 443
          nc -vzu <公网IP> 8443

   若外部不通但本机通 —— 那就是防火墙没放行，检查 INPUT 链。
NOTE
