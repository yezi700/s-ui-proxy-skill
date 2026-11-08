# 协议选型与客户端兼容性

## 一、四种协议分别在解决什么

| 协议 | 传输 | 主要解决的问题 | 代价 |
|---|---|---|---|
| **AnyTLS** | TCP + TLS | TLS-in-TLS 嵌套握手指纹 | 需要自有域名 + 证书 |
| **Hysteria2** | QUIC (UDP) | 高丢包链路吞吐 | 激进拥塞控制；明文 QUIC 可能被限速（靠 obfs 缓解） |
| **VLESS + Reality** | TCP + TLS(借用) | 主动探测、无需自有证书 | 依赖外部 SNI 站点可用 |
| **Shadowsocks 2022** | TCP（+UDP relay） | 握手开销、兼容面 | 无 TLS 伪装，抗主动探测弱于前两者 |

### 1.1 AnyTLS

**解决什么**：代理流量里再套一层 TLS（访问 HTTPS 网站）时，外层和内层 TLS 的
握手包长分布会形成可识别的模式，DPI 能据此判定"这不是普通浏览"。
XTLS-Vision 用"分流内层 TLS 记录"来缓解，AnyTLS 用 **padding scheme**
按会话阶段插入不同长度的填充，把包长分布打散成普通 TLS 应用数据的样子。

**附带好处**：连接复用（session multiplexing），减少重复握手，延迟更好。

**限制**：
- 必须有真实 TLS（自有域名 + 证书），**不能配 Reality**（mihomo 明确不支持，
  未来也不会支持；S-UI 里配了也不工作）
- sing-box 1.12.0 起支持，客户端版本要够新
- `padding_scheme` 用默认值，**不要自己改** —— 改错了反而形成新指纹

### 1.2 Hysteria2

QUIC + 激进拥塞控制（Brutal），在高丢包链路上吞吐明显优于 TCP 系。
可以配 `masquerade` 让未认证探测看到真实网页。

**注意**：
- 也是 UDP，可能被运营商 QoS 限速
- **默认带 `salamander` 混淆**：实测这条线路上，不加混淆的明文 QUIC 会被限速到
  几百 KB/s，加了混淆的 Hysteria2 能跑几十 Mbps。如果哪天 Hy2 也掉速，
  **先确认 obfs 还开着**，再怀疑别的（§6.8）
- 实测 UDP 443 被阻断，默认用 2083/UDP
- 端口跳跃（`mport`）需要额外放行一段 UDP 端口并配 NAT REDIRECT

### 1.3 VLESS + Reality + Vision

借用真实网站（如 www.microsoft.com）的 TLS 握手，服务端不持有自己的证书。
GFW 主动探测时会被转发给真实网站，探针收到的是真证书。

**注意**：
- **不能走 CDN**（CDN 会终止 TLS，伪装失效）
- 依赖外部 SNI 站点持续可用且支持 TLS 1.3 + H2
- 443 端口曾被实测针对性干扰，**换到 2053 立刻恢复** —— 这类问题不是协议缺陷
- 需要 x25519 密钥对，格式与 Xray 的 `privateKey` 一致（43 字符 base64url）
- 不用 `/root/cert/` 里的证书（借用别人的 SNI）

### 1.4 Shadowsocks 2022

AEAD 加密的轻量协议，2022 版（`2022-blake3-*`）修掉了老版的重放与探测弱点。

**为什么把它放进默认组合**：它是**握手往返最少**的协议 —— 没有 TLS 握手、
没有额外认证往返。这条线路上它的延迟表现和 TCP 系最好的节点持平，
而且在 AnyTLS 被拥塞时是个独立的技术路径。

**顺带的好处**：兼容面比 AnyTLS 还广。Xray 内核（v2rayN / v2rayNG 默认）、
Surge、QuantumultX 都支持它，而这三家都不支持 AnyTLS。

**注意**：
- 客户端 config 的键是 `shadowsocks16`（不是 `shadowsocks`），
  服务端 `options` 必须同时有 `method` 和 `password`（§6.9）
- psk 长度由 method 决定：aes-128 是 16 字节，aes-256 / chacha20 是 32 字节
- 入站默认**同时监听 tcp 和 udp**（UDP relay），防火墙两个都要放行
- mihomo 里密码写成 `psk:psk`（服务器 psk:用户 psk），只有一个用户时两边相同

### 1.5 TUIC 为什么不在默认组合里

TUIC v5 是 QUIC 传输，设计上没问题，但在实测的这条线路上被限速到
**0.2~0.4 Mbps**（换端口、换客户端都一样，排除过程见 §6.8）。

所以本 skill 把它改成**可选**：`deploy_env` 里 `TUIC_PORT` 留空就不部署。
要用就填个端口，但先读 §6.8 知道它可能有多慢。

如果坚持要部署，注意两点：
- 需要 `alpn: ["h3"]`，且**必须用独立的 TLS 对象**，与 AnyTLS 共用会报
  `tls: server did not select an ALPN protocol`（§4.2）
- `congestion_control` 会被 S-UI 重置为 `cubic`，设 `bbr` 无效（不影响连通性）

## 二、客户端兼容性矩阵

**这张表必须整张给用户。** 只写一半会让人在两种"连不上"之间来回折腾。

| 客户端 / 内核 | AnyTLS | Hysteria2 | VLESS-Reality | Shadowsocks | 备注 |
|---|:--:|:--:|:--:|:--:|---|
| sing-box ≥1.12（Hiddify / Karing / NekoBox） | 是 | 是 | 是 | 是 | 四种全支持，首选 |
| mihomo / Clash Meta ≥1.19（Verge Rev / FlClash） | 是 | 是 | 是 | 是 | 四种全支持 |
| Xray 系（v2rayN / v2rayNG 默认内核） | 否 | 否 | 是 | 是 | 只认 Reality 和 SS |
| Shadowrocket（iOS，≥2.2.65） | 是 | 是 | 是 | 是 | 需较新版本 |
| Surge | 否 | 是 | 否 | 是 | 只支持 Hysteria2 和 SS |
| QuantumultX | 否 | 否 | 否 | 是 | 只支持 SS |

> 这张表由 `render_report.py` 按**实际入站类型**动态生成，加协议不用改渲染代码。
> 判定数据在 `PROTO_META` / `CLIENT_SUPPORT` 两个常量里，改那里即可。

### 2.1 为什么 Xray 内核只支持 Reality 和 SS

Xray-core 从未实现 TUIC / Hysteria2 / AnyTLS —— 这三个是 sing-box 生态的协议。
它支持 VLESS（含 Reality）和 Shadowsocks。

用户装了 v2rayN 却用默认 Xray 内核，就会看到"只有 Reality 能连"。

**排查顺序**：先问客户端用的什么内核，再看服务端日志。不要反过来。

### 2.2 订阅格式与客户端

| 订阅地址 | 返回 | 给谁 |
|---|---|---|
| `/<subPath>/<subId>` | base64 链接列表 | v2rayN / v2rayNG / NekoBox / Karing / Hiddify / Shadowrocket |
| `/<subPath>/<subId>?format=clash` | Clash YAML | Clash Verge Rev / Clash Meta for Android / FlClash |
| `/<subPath>/<subId>?format=json` | sing-box JSON | sing-box 原生配置 |

把 base64 那条填给 Clash，报 `yaml: unmarshal errors: cannot unmarshal !!str`。

## 三、选型建议

### 3.1 默认组合（本 skill 采用）

```
AnyTLS        8443/tcp   主力：日常浏览、办公（TLS-in-TLS 抗指纹）
Hysteria2     2083/udp   大流量：视频、下载（带 obfs，抗 QUIC 限速）
Reality       2053/tcp   保底：UDP 全被 QoS 时还能用
Shadowsocks   2087/tcp   低延迟 + 兼容：握手最省，老客户端也认
```

四种覆盖 TCP/UDP 两条路径、四种抗检测思路。任何一条被封都还有别的可用。

**别指望靠排名选节点**：同一台机器连续三轮压测，第一名每轮都不同
（12–40 Mbps 的波动）。真实做法是四个都留着，按当下延迟挑，
把「一个 TCP + 一个 UDP」配成故障切换（§7.3）。

### 3.2 只要一条

优先 **AnyTLS + TLS**：同时解决指纹和延迟，且不依赖外部站点。

如果连自有域名都没有，只能选 **Reality**（借用别人的 SNI）。

如果要给一台老设备 / 老客户端配，选 **Shadowsocks 2022**（兼容面最广）。

### 3.3 客户端受限时

| 约束 | 保留哪几个 |
|---|---|
| 只能用 Xray 内核（v2rayN 默认） | Reality + Shadowsocks |
| 只能用 Surge | Hysteria2 + Shadowsocks |
| 只能用 QuantumultX | Shadowsocks |
| iOS + Shadowrocket 旧版 | Reality（最保守） |
| 全功能客户端 | 全留 |

## 四、常见"连不上"的归因

按这个顺序排查，命中率从高到低：

1. **端口被封** → 换端口做对照实验（最常见，见 `pitfalls.md` §1.1）
2. **客户端内核不支持** → 看兼容性矩阵，问清内核（§6.1）
3. **订阅格式填错** → base64 那条填给 Clash 会直接解析失败（§6.6）
4. **UDP 被 QoS** → 换 TCP 系节点（AnyTLS / Reality / SS）验证
5. **证书过期 / SNI 不匹配** → `openssl s_client` 看实际返回的证书（§3.8）
6. **服务端 users 没注入** → 面板看订阅里有没有这个节点
7. **协议参数错** → 服务端日志会有明确报错（SS 缺 psk、TUIC 缺 ALPN）

不要一上来就重装面板或换协议 —— 先做对照实验定位层次。
完整症状索引见 `pitfalls.md` 开头的「症状速查」表。
