#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把三个入站合并成"一条订阅 URL 返回全部节点"。

背景（3x-ui v3.9.0）：
    * subId 全局唯一 → 三个入站不能共用一个 subId（会报 Duplicate subId）
    * 客户端被抽到 clients / client_inbounds 两张表 →
      直接改 inbound.settings.clients[] 无效，必须走 /panel/api/clients/*

正确做法：
    建 **一个** 客户端身份，用 inboundIds:[1,2,3] 绑定三个入站。
    model.Client 自带 id / password / auth / flow 四个独立字段，
    因此同一条身份可以同时承载：
        id       -> VLESS-REALITY 的 uuid，也是 TUIC 的 uuid
        password -> TUIC 的密码
        auth     -> Hysteria2 的认证密码
        flow     -> xtls-rprx-vision（Vision 必需，合并后最容易丢）

执行：
    python scripts/merge_subscription.py
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ssh_run import env_exports, get_client, load_env, run  # noqa: E402

REMOTE_SCRIPT = r"""
set -euo pipefail
umask 077
api_check() {
  python3 -c 'import json,sys
r=json.load(sys.stdin)
if not isinstance(r, dict) or r.get("success") is not True:
    raise SystemExit("面板操作失败: " + str(r.get("msg", "")) if isinstance(r, dict) else "面板响应格式错误")'
}
BASE="https://127.0.0.1:${PANEL_PORT}/${PANEL_PATH}/panel/api"
H1="Host: ${DOMAIN}"
AUTH="Authorization: Bearer ${API_TOKEN}"
CB="$BASE/clients"
CRED=/root/.xui-skill/node-credentials.json

[ -f "$CRED" ] || { echo "缺少 $CRED，请先跑 deploy_nodes.py"; exit 1; }

echo "=== 0. 备份数据库 ==="
cp -f /etc/x-ui/x-ui.db "/root/.xui-skill/x-ui-backup-$(date +%Y%m%d-%H%M).db"

echo "=== 1. 读取凭据 ==="
REALITY_UUID="$(python3 -c "import json;print(json.load(open('$CRED'))['reality_uuid'])")"
HY2_AUTH="$(python3     -c "import json;print(json.load(open('$CRED'))['hy2_auth'])")"
TUIC_PASSWORD="$(python3 -c "import json;print(json.load(open('$CRED'))['tuic_password'])")"
echo "reality_uuid  = $REALITY_UUID"
echo "hy2_auth      = $HY2_AUTH"
echo "tuic_password = $TUIC_PASSWORD"

echo "=== 2. 收集入站 ID ==="
curl -fskS -H "$H1" -H "$AUTH" "$BASE/inbounds/list" > /root/.xui-skill/inbounds.json
python3 - <<'PYEOF'
import json, os
d = json.load(open("/root/.xui-skill/inbounds.json"))
if d.get("success") is not True:
    raise SystemExit("读取入站失败")
ids = []
expected = {"vless": int(os.environ["REALITY_PORT"]),
            "hysteria": int(os.environ["HY2_PORT"]), "tuic": int(os.environ["TUIC_PORT"])}
for ib in (d.get("obj") or []):
    if ib.get("protocol") in expected and ib.get("port") == expected[ib["protocol"]]:
        ids.append(ib.get("id"))
        print("#%s %s/%s %s" % (ib.get("id"), ib.get("protocol"),
                                ib.get("port"), ib.get("remark")))
if len(ids) != 3 or len(set(ids)) != 3:
    raise SystemExit("必须找到配置端口对应的三个入站，停止合并")
open("/root/.xui-skill/ids.txt", "w").write(",".join(str(i) for i in ids))
PYEOF
IDS="$(cat /root/.xui-skill/ids.txt)"
echo "inboundIds = [$IDS]"
[ -n "$IDS" ] || { echo "没找到可合并的入站"; exit 1; }

echo "=== 3. 创建合并客户端 ==="
# ⚠️ 必须手工构造 body。直接回写 GET 的对象会踩 id 类型陷阱：
#    model.Client.id 是 string，但 GET /clients/get/:email 返回的 id 是数字行号
# ⚠️ heredoc 用的是「引号定界符」，bash 不会展开 ${VAR}，
#    所以 email / subId 必须通过 sys.argv 传进去
python3 - "$IDS" "$REALITY_UUID" "$HY2_AUTH" "$TUIC_PASSWORD" \
        "$MERGED_EMAIL" "$MERGED_SUBID" <<'PYEOF' > /root/.xui-skill/merge.json
import json, sys
ids, rid, hauth, tpass, email, subid = sys.argv[1:7]
print(json.dumps({
    "client": {
        "id": rid,                    # VLESS uuid，同时也是 TUIC uuid
        "password": tpass,            # TUIC 密码
        "auth": hauth,                # Hysteria2 认证
        "flow": "xtls-rprx-vision",   # Reality Vision 必需
        "email": email,
        "subId": subid,
        "enable": True,
        "limitIp": 0,
        "totalGB": 0,
        "expiryTime": 0,
        "comment": "merged",
        "reset": 0,
    },
    "inboundIds": [int(x) for x in ids.split(",") if x],
}, ensure_ascii=False))
PYEOF
cat /root/.xui-skill/merge.json; echo

RESP="$(curl -fskS -X POST -H "$H1" -H "$AUTH" -H 'Content-Type: application/json' \
        --data-binary @/root/.xui-skill/merge.json "$CB/add")"
printf '%.600s\n' "$RESP"
printf '%s\n' "$RESP" | api_check

echo "=== 4. 保留原始客户端，待外部验证新订阅后再按需清理 ==="

echo "=== 5. 校验 flow 是否保留 ==="
# ⚠️ /clients/get/<email> 的返回结构在不同版本上不一致：
#    有时 obj 是空的、有时包了一层 client、有时 id 是数据库行号。
#    所以这里同时查 /clients/get 与 /clients/list，任一处能看到 flow 就算通过。
curl -fskS -H "$H1" -H "$AUTH" "$CB/get/${MERGED_EMAIL}" \
  > /root/.xui-skill/merged-client.json 2>/dev/null || true
curl -fskS -H "$H1" -H "$AUTH" "$CB/list" \
  > /root/.xui-skill/clients.json 2>/dev/null || true
python3 - "$MERGED_EMAIL" <<'PYEOF'
import json, sys
email = sys.argv[1]


def load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def find_flow(d):
    obj = d.get("obj") if isinstance(d, dict) else None
    if isinstance(obj, dict):
        if obj.get("email") == email:
            return obj.get("flow")
        inner = obj.get("client")
        if isinstance(inner, dict) and inner.get("email") == email:
            return inner.get("flow")
        # 有些版本只回一个裸 client 对象，不带 email
        if "flow" in obj and obj.get("email") in (None, email):
            return obj.get("flow")
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and item.get("email") == email:
                return item.get("flow")
    return None


flow = find_flow(load("/root/.xui-skill/merged-client.json"))
if flow is None:
    flow = find_flow(load("/root/.xui-skill/clients.json"))
print("merged flow = %r" % flow)
if flow != "xtls-rprx-vision":
    raise SystemExit("合并客户端回读失败或 flow 丢失；原客户端保持不变")
PYEOF

echo "=== 6. 重启 Xray ==="
# ⚠️ v3.10.x 已移除该端点（404）。真正生效的是下面的 systemctl restart x-ui，
#    端点缺失时不能中断（set -e 下裸调用会终止脚本）。
curl -fskS -X POST -H "$H1" -H "$AUTH" "$BASE/setting/restartXrayService" 2>/dev/null | api_check \
  || echo "(该版本无 restartXrayService 端点，跳过；由 systemctl restart x-ui 完成)"
echo
systemctl restart x-ui
sleep 8
systemctl is-active --quiet x-ui
echo "x-ui: active"

echo "=== 7. 合并后的分享链接 ==="
# obj 形状随版本变化：v3.9.0 是对象数组，v3.10.x 是纯字符串数组
curl -fskS -H "$H1" -H "$AUTH" "$BASE/inbounds/allLinks" \
  | python3 -c 'import json,sys
d=json.load(sys.stdin)
for it in (d.get("obj") or []):
    if isinstance(it, str):
        print(it.rsplit("#",1)[-1], "|", it)
    else:
        print(it.get("remark"), "|", it.get("uri"))'

echo "=== 8. 订阅内容（两条，格式不同）==="
SP="${SUB_PATH#/}"; SP="${SP%/}"
GENERAL_URL="https://${DOMAIN}:${SUB_PORT}/${SP}/${MERGED_SUBID}"
echo "① 通用（base64 节点列表）: ${GENERAL_URL}"
curl -fskS "$GENERAL_URL" -H "$H1" -o /root/.xui-skill/sub.txt -w "   HTTP %{http_code}\n"
echo "   --- 解码后 ---"
base64 -d /root/.xui-skill/sub.txt 2>/dev/null \
  | sed 's/#.*//' | cut -c1-80 || cat /root/.xui-skill/sub.txt | head -5

# ② Clash / mihomo：独立的 subClashPath，且必须 subClashEnable=true 才有内容
# ⚠️ 这里用 heredoc 而不是 `python3 -c '...'`：后者要在一个命令行里嵌套两种引号，
#    在 Windows 上经 `bash -c` 传入时会被 argv 转义搞坏（unexpected EOF）。
curl -fskS -X POST -H "$H1" -H "$AUTH" "$BASE/setting/all" > /root/.xui-skill/settings.json
cat > /root/.xui-skill/clashpath.py <<'PYEOF'
import json
o = (json.load(open('/root/.xui-skill/settings.json')).get('obj') or {})
print((o.get('subClashPath') or '').strip('/') if o.get('subClashEnable') else '')
PYEOF
CLASH_PATH="$(python3 /root/.xui-skill/clashpath.py)"
if [ -n "$CLASH_PATH" ]; then
  CLASH_URL="https://${DOMAIN}:${SUB_PORT}/${CLASH_PATH}/${MERGED_SUBID}"
  echo "② Clash / mihomo（YAML）   : ${CLASH_URL}"
  CT="$(curl -fskS "$CLASH_URL" -H "$H1" -D - -o /root/.xui-skill/clash.yaml \
        | grep -i '^content-type:' | tr -d '\r')"
  echo "   ${CT}"
  head -2 /root/.xui-skill/clash.yaml
  echo "CLASH_SUB_URL=${CLASH_URL}"
else
  echo "② Clash / mihomo（YAML）   : 未启用（subClashEnable=false）"
  echo "   启用后需重启面板；deploy_nodes.py 会自动打开，手工面板请到"
  echo "   「设置 → 订阅 → Clash」开启，否则该路径是 404。"
  echo "CLASH_SUB_URL="
fi

echo "=== MERGE DONE ==="
"""


def main() -> int:
    cfg = load_env()
    cfg.setdefault("MERGED_EMAIL", "merged")
    cfg.setdefault("MERGED_SUBID", "")
    cfg.setdefault("SUB_PORT", "2096")
    cfg.setdefault("SUB_PATH", "")
    cfg.setdefault("REALITY_PORT", "443")
    cfg.setdefault("HY2_PORT", "443")
    cfg.setdefault("TUIC_PORT", "8443")

    if not cfg.get("MERGED_SUBID"):
        import secrets
        import string
        alphabet = string.ascii_lowercase + string.digits
        cfg["MERGED_SUBID"] = "".join(secrets.choice(alphabet) for _ in range(16))
        print(f"未提供 MERGED_SUBID，已随机生成: {cfg['MERGED_SUBID']}")

    for key in ("VPS_HOST", "VPS_USER", "DOMAIN", "PANEL_PORT", "PANEL_PATH",
                "API_TOKEN"):
        if not cfg.get(key):
            sys.stderr.write(f"deploy.env 缺少 {key}\n")
            return 2

    cli = get_client(cfg)
    try:
        encoded = base64.b64encode(REMOTE_SCRIPT.encode("utf-8")).decode("ascii")
        rc, out, err = run(cli, f"umask 077; echo '{encoded}' | base64 -d > /root/.xui-skill/_merge.sh",
                           timeout=60)
        if rc:
            sys.stderr.write(err)
            return rc

        keys = ("PANEL_PORT", "PANEL_PATH", "DOMAIN", "API_TOKEN", "SUB_PORT",
                "SUB_PATH", "MERGED_EMAIL", "MERGED_SUBID", "REALITY_PORT",
                "HY2_PORT", "TUIC_PORT")
        exports = env_exports({k: cfg.get(k, "") for k in keys})
        rc, out, err = run(
            cli, f"{exports}\nbash /root/.xui-skill/_merge.sh 2>&1",
            timeout=600,
        )
        sys.stdout.write(out)
        if err.strip():
            sys.stderr.write("\n[stderr]\n" + err)
    finally:
        cli.close()

    if rc:
        return rc

    # 从远端输出里取回 Clash 订阅地址（未启用时为空）
    clash_url = ""
    m = re.search(r"^CLASH_SUB_URL=(.*)$", out, re.M)
    if m:
        clash_url = m.group(1).strip()

    sub_path = (cfg.get("SUB_PATH") or "").strip("/")
    general_url = (f"https://{cfg['DOMAIN']}:{cfg['SUB_PORT']}/"
                   + (f"{sub_path}/" if sub_path else "")
                   + cfg["MERGED_SUBID"])

    print("\n" + "=" * 64)
    print("合并结果")
    print("=" * 64)
    print(f"客户端 email : {cfg['MERGED_EMAIL']}")
    print(f"subId        : {cfg['MERGED_SUBID']}")
    print(f"① 通用订阅   : {general_url}")
    print("               → v2rayN / v2rayNG / NekoBox / Karing / Hiddify / Shadowrocket")
    if clash_url:
        print(f"② Clash 订阅 : {clash_url}")
        print("               → Clash Verge Rev / Clash Meta for Android / FlClash")
    else:
        print("② Clash 订阅 : 未启用（subClashEnable=false，需在面板启用后重启）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
