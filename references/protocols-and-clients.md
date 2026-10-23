# 协议选型与客户端兼容性

> 本文回答两个问题：**该装哪几种协议**，以及**"某个客户端连不上"该怎么解释**。

---

## 1. 协议横向对比

| 协议 | 传输 | 端口 | 抗封锁 | 延迟 | 高丢包吞吐 | 需要自有证书 | 备注 |
|---|---|---|---|---|---|---|---|
| **VLESS + REALITY + Vision** | TCP | 443 | ★★★★★ | 中 | 中 | ❌ 借用目标站证书 | 主力。无 TLS 指纹特征，最像正常 HTTPS |
| **Hysteria2** | QUIC/UDP | 443 | ★★★★ | 低 | ★★★★★ | ✅ | 暴力拥塞控制，丢包链路吞吐碾压 |
| **TUIC v5** | QUIC/UDP | 8443 | ★★★★ | ★最低 | ★★★★ | ✅ | 0-RTT，连接建立最快 |
| VLESS + TLS（自签/LE） | TCP | 443 | ★★★ | 中 | 中 | ✅ | 有 TLS 指纹，不如 REALITY |
| Trojan | TCP | 443 | ★★★ | 中 | 中 | ✅ | 老牌，特征已被识别得差不多 |
| VMess + WS + TLS | TCP | 443 | ★★ | 中 | 中 | ✅ | 需要 CDN 配合才好用 |
| Shadowsocks | TCP | 任意 | ★★ | 低 | 中 | ❌ | 协议简单，抗主动探测差 |
| WireGuard | UDP | 任意 | ★ | 低 | ★★★★ | ❌ | 特征明显，易被 QoS/封 |
| AnyTLS | — | — | — | — | — | — | **3x-ui v3.9.0 不支持**（见 §1.1） |

### 1.1 AnyTLS 的确认方法

AnyTLS 是 sing-box / mihomo 生态的协议，**3x-ui v3.9.0 没有实现**
（面板协议下拉里没有，直接调 API 也会被拒）。

不要凭印象说"支持/不支持"，直接查二进制：

```bash
# ⚠️ Debian 默认没装 strings（见 pitfalls.md §1.4），用 grep -a 代替
grep -a -o -i -- "anytls" /usr/local/x-ui/bin/xray-linux-amd64 | wc -l   # → 0
grep -a -o -i -- "anytls" /usr/local/x-ui/x-ui                           | wc -l   # → 0

# 面板实际支持的协议白名单
grep -a -o -E "amneziawg|hysteria|mtproto|wireguard|shadowsocks|tunnel|trojan|vless|vmess|tuic" \
  /usr/local/x-ui/x-ui | sort -u
```

若用户坚持要 AnyTLS，只能换服务端（sing-box 官方 / mihomo）；
留在 3x-ui 的话，用 REALITY 达成相近目的（借真实站点证书，被动探测同样很难区分）。

### 为什么推荐"REALITY + Hysteria2 + TUIC"这三件套

三者**互补**，覆盖不同网络条件：

- **REALITY** 走 TCP 443。**在 UDP 被限速/封锁的网络里是唯一活路**，
  且因为借用真实站点证书，被动探测几乎无法区分。作为兜底主力。
- **Hysteria2** 走 UDP 443 + 端口跳跃。**丢包率高时吞吐最好**（比如跨境晚高峰、
  移动网络），但 UDP 可能被 QoS。
- **TUIC** 走 UDP 8443。**连接建立最快**（0-RTT），网页首包体验最好。
  与 Hysteria2 分端口，避免互相抢占。

三个同时可用时，客户端侧按网络情况切换即可。

---

## 2. 端口规划（三件套参考）

| 端口 | 协议 | 说明 |
|---|---|---|
| 22/tcp | SSH | 务必保留，否则自己都进不去 |
| 80/tcp | HTTP | ACME 证书签发（可只用一次） |
| 443/tcp | REALITY | TCP |
| 443/udp | Hysteria2 | 与 TCP 443 共存，**改防火墙时两个都别漏** |
| 58888:60888/udp | Hysteria2 端口跳跃 | 客户端 `mport`，服务端 iptables NAT REDIRECT → 443 |
| 8443/udp | TUIC v5 | |
| 2096/tcp | 订阅服务 | |
| 46821/tcp | 面板 | 建议只对特定 IP 开放，或走域名 |

### 2.1 ⚠️ 自定义端口时要避开机房的封禁名单（1080 是最典型的坑）

**默认的 443 / 8443 / 2096 / 46821 一般没问题。**
但只要你想换端口（比如 443 被占用、想多开一条 REALITY），就必须先验一遍 ——
很多机房在**更上游的位置**封了一批「代理常用端口」。
表现是：服务端监听正常、iptables 规则正常、VPS 自连正常，**但从公网就是连不上**。

实测（日本机房）对照结果：

```
1080 → 被封锁        1081 → OK
2080 → OK            7080 → OK
```

**结论：避开 1080 / 1081 / 3128 / 8888 等常见代理端口，优先选 2000-3000 或 7000-9000 段。**

定位方法与完整排查见 `pitfalls.md` §4.5。

### 端口跳跃的实现

`mport=58888-60888` 是**客户端**参数。服务端不需要监听整段端口，
而是用 iptables 把整段 UDP 重定向到 443：

```bash
iptables -t nat -A PREROUTING -i eth0 -p udp --dport 58888:60888 \
         -j REDIRECT --to-ports 443
```

配合 INPUT 链放行：

```bash
iptables -A INPUT -p udp --dport 58888:60888 -j ACCEPT
```

⚠️ 漏了 INPUT 放行只加 NAT，包会在 nat 表被改写后仍被 filter 表丢掉。

---

## 3. ⚠️ 客户端兼容性矩阵（重点）

### 3.1 根因：Xray ≥ 26.9.8 的 REALITY 强制要求后量子混合密钥交换

Xray-core 从 **26.9.8** 起，REALITY 服务端要求客户端 ClientHello 中携带
**`X25519MLKEM768`**（X25519 + ML-KEM-768 混合密钥交换）扩展。
不带该扩展的客户端会在 REALITY 校验阶段失败。

实测报错：

```
reality verification failed
```

排查过程与结论：

- 换 5 种 uTLS 指纹（chrome / firefox / safari / ios / edge）**全部失败** → 不是指纹问题
- 用 Xray-core 客户端连 → **成功** → 确认是服务端要求变了
- 上游 issue：sing-box#4520、Xray#6477

### 3.2 ⚠️ 根因二：Xray 内核**根本没有** TUIC / Hysteria2

这是比 §3.1 更常见、也更容易被误判的一类问题。

**Xray-core 从未实现 TUIC 协议**（也没有 Hysteria2）。在 Xray-core 源码里：

```bash
# 在 Xray 源码目录执行，确认不存在 tuic 协议实现
grep -rn "tuic" infra/conf/ | wc -l        # → 0
strings /usr/local/x-ui/bin/xray-linux-amd64 | grep -ci 'tuic'   # 只有 3x-ui 面板自己的字符串，与 Xray 无关
```

**因此**：用 Xray 内核的客户端导入 `tuic://` / `hysteria2://` 链接后，
节点会一直**测速超时**，表现就是"连不上""登录不进去""识别不出来"。

而 **v2rayN 与 v2rayNG 的默认内核恰恰都是 Xray**：

| 客户端 | 能否切内核 | 结果 |
|---|---|---|
| **v2rayN (Windows)** | ✅ 可以切到 sing-box | 切完 TUIC / Hysteria2 就能用 |
| **v2rayNG (Android)** | ❌ **没有内核切换** | TUIC / Hysteria2 **永久不可用**，只能用 REALITY |

#### v2rayN 的正确配置（Windows）

「设置 → 参数设置 → 默认内核类型」改为 **sing-box**（或 `Core 类型` 里按协议分别指定），
保存后重启内核，再重新导入订阅。

#### v2rayNG 用户的替代方案（Android）

v2rayNG 只有 Xray 内核，无法使用 TUIC / Hysteria2。需要换客户端：

- **NekoBox / NekoRay**（sing-box 内核）
- **Hiddify**（sing-box 内核）
- **Clash Meta for Android / ClashMetaForAndroid**（mihomo 内核）
- **Karing**（sing-box 内核）

或者**继续用 v2rayNG，但只用 REALITY 节点**（TCP 443）——三件套里 REALITY 是主力，
这样也能正常上网，只是少了 QUIC 的低延迟优势。

### 3.3 兼容性矩阵（合并两张表）

| 客户端 | 内核 | REALITY 节点 | Hysteria2 | TUIC |
|---|---|---|---|---|
| **v2rayN 默认配置** | Xray | ✅ | ❌ | ❌ |
| **v2rayN 切到 sing-box 内核** | sing-box | ❌ | ✅ | ✅ |
| **v2rayNG (Android)** | Xray（无法切） | ✅ | ❌ | ❌ |
| mihomo ≥ 1.19.30 | mihomo | ✅ | ✅ | ✅ |
| Clash Verge Rev / Clash Meta for Android | mihomo | ✅ | ✅ | ✅ |
| **sing-box ≤ 1.15.0-alpha** | sing-box | ❌ | ✅ | ✅ |
| **Hiddify / NekoBox / Karing** | sing-box | ❌ | ✅ | ✅ |
| **Shadowrocket 2.2.92（旧版）** | 自研 | ❌ | ✅ | ✅ |
| Shadowrocket 新版 | 自研 | ✅ | ✅ | ✅ |
| **Surge / Stash** | 自研 | ✅ | ✅ | ✅ |

**两句话结论**：

1. **sing-box 系**（Hiddify / Karing / NekoBox）和旧版 Shadowrocket：
   连不上 **REALITY**，但 Hysteria2 / TUIC 正常（§3.1）。
2. **Xray 系**（v2rayN 默认 / v2rayNG）：
   连不上 **Hysteria2 / TUIC**，但 REALITY 正常（§3.2）。
3. **mihomo 系**（Clash Verge / Clash Meta）三个节点全部正常，**是最省心的选择**。

> ⚠️ 交付时必须**同时**告知这两条，否则用户会在"REALITY 连不上"和
> "TUIC 连不上"之间来回折腾，误以为是自己配置错了。

### 3.4 给用户的建议话术

交付文档里必须写清楚，否则用户会以为是自己配置错了：

> **用 Hiddify / Karing / sing-box 系或旧版 Shadowrocket**：
> 请用 **Hysteria2 或 TUIC**，不要用 REALITY。这是 Xray 26.9.8+ 的后量子密钥交换要求，客户端内核尚未跟进。
>
> **用 v2rayN（Windows）**：到「设置 → 参数设置」把默认内核改成 **sing-box**，
> 否则 TUIC / Hysteria2 永远连不上。
>
> **用 v2rayNG（Android）**：它只有 Xray 内核，**用不了 TUIC / Hysteria2**，
> 请改用 NekoBox / Hiddify / Clash Meta for Android，或只用 REALITY 节点。
>
> **用 Clash Verge / Clash Meta（mihomo）**：三个节点都正常，无需任何调整。

### 3.5 可选的降级方案

如果确实需要 sing-box 系客户端也能用 REALITY，可以**降级 Xray-core**：

- 降级到 **≤ 26.3.27**（`v26.3.27` 是当时最新的非预发布版；26.9.8 起全是 prerelease）
- 代价：放弃上游安全修复与新特性，且 3x-ui 后续升级可能又覆盖回去

**默认建议**：保持 26.9.30 不降级，靠"Hysteria2/TUIC 兜底 + 文档说明"解决兼容性问题。
只有在用户明确要求"必须让 Hiddify 也能连 REALITY"时才降级。

---

## 4. 排查"某客户端连不上"的流程

1. **先确认是哪个节点不通**，两种典型组合直接对应两个根因：
   - 只有 **REALITY** 不通，Hy2/TUIC 正常 → **§3.1**（客户端内核缺后量子密钥交换，是 sing-box 系）
   - 只有 **Hy2/TUIC** 不通，REALITY 正常 → **§3.2**（客户端是 Xray 内核，v2rayN 默认 / v2rayNG）
2. **先看服务端日志再下结论**。3x-ui 的 TUIC 日志能直接区分"服务端问题"和"客户端问题"：

   ```bash
   journalctl -u x-ui --no-pager -n 500 | grep -iE "tuic|quic|auth"
   # 看到 "tuic: inbound N (...): TCP relay started"  → 服务端正常，认证与转发都成功
   # 看到 "client authentication rejected" / "authentication timed out" → 真的认证失败
   ```

   再看 UDP 包计数，判断客户端到底有没有把包发过来：

   ```bash
   iptables -t filter -L INPUT -v -n | grep 8443   # 命中数长期为 0 → 客户端根本没连过来
   ```

3. **确认分享链接里的地址不是 `127.0.0.1`**（见 `xui-api.md` §5.1）
4. **确认 `sni` 不为空**（见 `xui-api.md` §5.3）
5. **确认 flow 字段在**：Reality+Vision 必须有 `flow=xtls-rprx-vision`，
   合并订阅后容易丢（见 `xui-api.md` §4.4）
6. **确认端口真的通**：TCP 端口先做裸探测（`/dev/tcp/<IP>/<PORT>`，连续 5-10 次），
   UDP 端口要单独测（TCP 通不代表 UDP 通）。注意**在 VPS 上自测无效**（走 lo，不经 INPUT 链）。
7. **确认证书**：`openssl s_client -connect <域名>:443 -servername <域名>` 看有效期
8. **换一个内核完全不同的客户端**交叉验证（Xray 系 vs sing-box 系 vs mihomo 系），
   这一步能最快区分"服务端问题"和"客户端内核问题"。
   最快的三连测：Xray 客户端测 REALITY、sing-box 测 Hy2/TUIC、mihomo 测全部。

---

## 5. 实测数据（参考基线）

同一 VPS、同一时段，从中国境外/境内各测一轮，取平均建连时间：

| 节点 | 平均建连 | 成功率 | YouTube 可达 |
|---|---|---|---|
| REALITY (443/tcp) | 0.761 s | 5/5 | ✅ |
| TUIC (8443/udp) | **0.375 s** | 5/5 | ✅ |
| Hysteria2 (443/udp) | **0.370 s** | 5/5 | ✅ |

> 结论符合预期：QUIC 系（TUIC / Hysteria2）建连明显快于 TCP+TLS 系。
> 但**抗封锁能力 REALITY 最强**，所以三者都留着，让用户按场景切换。

---

## 6. 选型速查

| 用户场景 | 推荐 |
|---|---|
| 就要一套最稳的 | REALITY（主力）+ Hysteria2（备用） |
| 网络丢包严重（跨境晚高峰、移动网） | Hysteria2 优先 |
| 追求最低延迟（游戏、实时） | TUIC |
| 客户端是 Hiddify / Karing / NekoBox / 旧 Shadowrocket | 只用 Hysteria2 + TUIC |
| 客户端是 v2rayNG（Android，Xray 内核） | **只能 REALITY**；要用 TUIC/Hy2 得换客户端 |
| 客户端是 v2rayN（Windows，Xray 内核） | 先切内核到 sing-box，再三个都用 |
| 客户端是 Clash Verge / Clash Meta | 三个都能用，无需调整 |
| UDP 被完全封锁的网络 | 只能 REALITY（TCP） |
| 要接 CDN | VLESS + WS + TLS（本文三件套不适用） |
| 想要"填充 + 抗流量特征分析"的新协议（AnyTLS 等） | 3x-ui v3.9.0 不支持，见 §1.1 |
