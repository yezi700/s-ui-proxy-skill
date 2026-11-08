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
- **默认没有开 obfs**，实测也不需要：本 skill 建的 Hy2 入站只有 `listen` /
  `listen_port`，没有 `obfs` 字段，照样跑 78 Mbps。所以「明文 QUIC 必被限速」
  这个结论不成立 —— TUIC 被限速是它自己的问题（§6.8），不代表所有 UDP 都慢。
  真遇到 QUIC 被针对性限速，再考虑加 `salamander`：服务端
  `obfs: {type: salamander, password: ...}`，客户端同步配 `obfs: salamander`
- 实测 UDP 443 被阻断，默认用 2083/UDP
- **端口跳跃已默认开启**：服务端把 `58888-60888/udp` REDIRECT 到 2083，
  客户端加一行 `ports: 58888-60888` 即可，实测零代价（§6.10）

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

**为什么把它放进默认组合**：它是**唯一不需要任何握手**的协议 —— 首包就是
加密数据。但注意，这只省掉了 TLS 那一跳：SS 没有连接复用，每个新连接仍然
要付一次 TCP 握手 + 一次请求往返，实测延迟 402 ms，是 AnyTLS（176 ms）的
两倍多（§1.6）。

所以它真正的价值是**兼容面**和**TCP 兜底**，不是低延迟。Xray 内核
（v2rayN / v2rayNG 默认）、Surge、QuantumultX 都支持它，而这三家都不支持 AnyTLS。

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

### 1.6 实测对比（2026-11-09，同一台机器同一时段）

测法与原始数据在 `measured.json`。三轮独立复测（含一次内核重启前后），排序稳定。

| 节点 | 延迟中位 | 持续吞吐（100MB 整包） | 短窗口 6 秒 | UDP relay |
|---|---:|---:|---:|:--:|
| **AnyTLS** 8443/tcp | **176 ms** | **108.4 Mbps** | 22.6 | ✓ |
| Hysteria2 2083/udp | 198 ms | 78.4 Mbps | 25.6 | ✓ |
| Hysteria2（跳端口） | 207 ms | 77.4 Mbps | 25.4 | ✓ |
| Shadowsocks 2087 | 402 ms | 67.0 Mbps | 23.8 | ✓ |
| VLESS-Reality 2053 | 612 ms | 80.5 Mbps | 20.9 | ✓ |
| ~~Naive 8444~~（已删） | 207 ms | 未跑满 | 26.8 | 未测 |

两条结论，都反直觉：

**① 延迟差异来自连接复用，不是协议快慢。**
AnyTLS 和 Hysteria2 在客户端侧有连接池，一个请求只付 1 个 RTT；
SS 没有复用，每次新建 TCP（2 RTT）；Reality 还要再做一次 TLS 握手（3 RTT）。
612 ÷ 205 ≈ 3、402 ÷ 205 ≈ 2，和实测完全对得上。
所以「SS 没有握手所以最快」是错的 —— 省掉的那一跳远不如连接复用值钱。

**② 6 秒窗口会把吞吐低估 3~4 倍。**
TCP 慢启动前几秒还没爬到窗口上限，短窗口只有 20~26 Mbps，
跑满 100MB 才看到 67~108 Mbps。**不要用短窗口数据选节点**，这是上一版
`measured.json` 里 12~40 Mbps 大幅波动的真正原因。

Reality 是唯一「两样都差」的：延迟 3 倍、吞吐中等。它的价值只剩抗主动探测
（不依赖自有域名和证书），当应急通道留着，别当主力。

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
AnyTLS        8443/tcp   主力：176ms / 108Mbps，延迟和吞吐双料第一
Hysteria2     2083/udp   大流量 + 抗 QoS：78Mbps，58888-60888 跳端口已开
Shadowsocks   2087/tcp   兼容兜底：客户端面最广，67Mbps
Reality       2053/tcp   应急：抗主动探测，但 612ms（3 个 RTT），别当主力
```

四种覆盖 TCP/UDP 两条路径、四种抗检测思路。任何一条被封都还有别的可用。

**节点不是按"谁快"排的，是按"解决什么问题"排的**：
主力用 AnyTLS，UDP 场景切 Hy2，客户端不认 AnyTLS 时降级到 SS，
域名/证书出问题时才动 Reality。四个都留着，配成故障切换（§7.3）。

上一版说「第一名每轮都不同、不要排名」——那是因为用的是 6 秒窗口，
量到的是 TCP 慢启动而不是带宽。跑满 100MB 之后排序三轮一致（§1.6）。

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
