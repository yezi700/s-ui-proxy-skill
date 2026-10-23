# 域名申请、Cloudflare 接入与 DNS 预检

适用：为本仓库的 VPS + 3x-ui 部署准备域名。已有可用域名和 DNS 服务商时，直接跳到第 4 节。
Cloudflare 可选；这里使用它管理 DNS，不需要创建 Workers、Pages 或 KV。

## 1. 选择已有域名或申请免费域名

先确认：是否已有域名、是否能管理 DNS、VPS 公网 IPv4 是什么。
域名可以免费申请，也可以使用已经购买的域名，不必重新注册。

免费申请入口（核对日期：2026-10-09）：

- [DNSHE](https://www.dnshe.com/)：从官网进入注册，完成邮箱/人机验证，在控制台选择可用名称及后缀。官网说明支持修改 NS；默认有效期一年，可免费续期，免续期升级另有条件。记录实际到期日，不能把“免费”理解为无需维护。
- [DigitalPlat Domains](https://domain.digitalplat.org/)：点击 Register a domain 创建账号，按页面要求完成验证、选择可用名称，再配置外部 NS。后缀、额度、验证及续期要求以实际控制台为准。

这些平台分配的是其命名空间下的名称。能修改 NS 不代表所有后缀一定能以独立区域接入 Cloudflare Free；添加时以 Cloudflare 实际接受结果为准。若不接受，可使用提供商自己的 DNS（如有），或选择兼容的域名。

账号密码、邮箱验证码、二次验证由用户在官方页面填写；引导无需收集这些信息。
如申请页面必须先填 NS，可先在 Cloudflare 添加申请中的完整名称以获取分配值；若 Cloudflare 拒绝，按服务商指引完成注册或使用其 DNS，不要编造 NS 地址。

## 2. 注册 Cloudflare 并添加域名

1. 打开 [Cloudflare 控制台](https://dash.cloudflare.com/)，选择注册并完成邮箱验证。已有账号直接登录。
2. 进入域名管理，选择添加/接入域名（Onboard a domain），输入你实际拥有的完整区域名称，选择 Free 计划。免费的是 DNS 计划，不是 Cloudflare Registrar 送域名。
3. 核对导入的 DNS 记录。已有网站或邮箱时，保留并核对 A、CNAME、MX、TXT 等原记录，自动扫描不保证完整。
4. 记下 Cloudflare 为这个区域分配的两条 NS；回到域名服务商的 Nameservers 设置，替换为这两条值。
5. 返回 Cloudflare，等待区域状态变为 Active。不要只在普通 DNS 记录表添加两条 NS 就认为完成委派。

已有域名启用 DNSSEC 时，应按官方迁移流程处理旧 DS/DNSSEC，否则切换 NS 后可能出现 SERVFAIL；不要直接更换 NS 后忽略验证。

[Cloudflare 官方接入与 NS 迁移说明](https://developers.cloudflare.com/dns/zone-setups/full-setup/setup/)

## 3. 添加指向 VPS 的 A 记录

以下假设你管理的区域是 `example.com`，节点域名选 `jp.example.com`。
`203.0.113.10` 仅是文档示例，必须替换为 VPS 控制台给出的真实公网 IPv4。

在 Cloudflare 的 DNS → Records 中添加：

| 字段 | 填写内容 |
|---|---|
| Type / 类型 | `A`（IPv4） |
| Name / 名称 | `jp`，完整结果为 `jp.example.com` |
| IPv4 address / 内容 | VPS 公网 IPv4，不带端口、不带 `https://` |
| Proxy status | **DNS only / 仅 DNS / 灰云** |
| TTL | Auto |

若直接使用区域本身（`example.com`），名称填 `@`。免费分配的名称如 `myname.<平台后缀>`，以你接入的整个名称作为区域；`jp` 会得到 `jp.myname.<平台后缀>`，不要把平台后缀本身误当成自己的域名。

使用其他 DNS 提供商时，同样添加 A 记录即可，不必迁移到 Cloudflare。
同名若已有冲突 CNAME，先核实用途；若已有旧 A，核对后更新，避免同时指向新旧 VPS。

**本仓库的节点域名保持灰云。** 普通橙云面向受支持端口的 HTTP/HTTPS，不能直接代理当前 REALITY TCP、Hysteria2/TUIC UDP 配置。默认面板端口 `46821` 也不在支持列表中。灰云返回 VPS 地址，不会隐藏源站 IP。

[Cloudflare 支持端口与其他协议说明](https://developers.cloudflare.com/fundamentals/reference/network-ports/)

AAAA 表示 IPv6。默认 `DISABLE_IPV6=1` 的流程只添加 A；若已有 AAAA，应核实并移除该节点名称下不再使用的 IPv6 记录。保留双栈时，先配置 VPS IPv6、监听及防火墙，并把 `DISABLE_IPV6=0`，再添加正确 AAAA。

## 4. 只读预检并填写 deploy.env

在本地技能目录运行（Windows / Linux / macOS，Python 3.8+，只用标准库）：

```bash
python scripts/check_dns.py --domain jp.example.com --ipv4 203.0.113.10
```

参数必须替换为实际值，脚本不读取 `deploy.env`，不需要 SSH 密码或 API Token。
它通过 HTTPS 分别查询 Cloudflare 与 Google 的 A/AAAA；仅当两者都返回唯一预期 IPv4，且默认无 AAAA 时通过。启用双栈时额外传 `--ipv6 <实际IPv6>`，要求 AAAA 也精确匹配。

- 退出码 `0`：当前 DNS 结果符合预期。
- 退出码 `1`：解析不匹配、DNS 错误或网络查询失败；查看每项 FAIL / ERROR 后处理。
- 退出码 `2`：域名/IP/命令行参数无效。

查询有超时，不会无限等待传播。不匹配时核对记录和 TTL 后再运行；若出现网络错误，先确认本机能访问这两个 DoH 服务。解析结果不匹配不等于一定开启了橙云。

通过后填写配置：

```dotenv
VPS_HOST=203.0.113.10
SERVER_IP=203.0.113.10
DOMAIN=jp.example.com
DISABLE_IPV6=1
```

保持 `VPS_HOST` 为真实 SSH 目标，`DOMAIN` 为完整服务域名。当前部署默认面板、证书和订阅使用同一 DOMAIN；暂不引导拆分多个域名。

预检只反映两个公共递归解析器的当前结果，不检查权威 NS 委派、Cloudflare Active 状态、全球传播、证书签发或端口连通性。Cloudflare 用户仍需手动确认 Active；随后继续 Skill 的环境探测和部署步骤，并在申请证书前再次预检。

## 5. 常见问题

| 现象 | 先检查 |
|---|---|
| Cloudflare 拒绝添加域名 | 是否填写实际拥有的区域、后缀是否被支持；必要时使用原 DNS 服务 |
| Pending nameserver update | 服务商 Nameservers 是否为分配值；是否仍有旧 NS |
| NXDOMAIN | 名称拼写、是否创建记录、区域是否生效 |
| SERVFAIL | DNSSEC/DS 是否匹配、权威 DNS 是否正常 |
| A 返回其他地址 | 旧 A、多条 A、橙云或缓存；逐项核实 |
| 存在不认识的 AAAA | 是否残留旧 IPv6；默认 IPv4 部署需处理该记录 |
| DNS 通过但证书失败 | ACME 验证方式、80 端口、防火墙、CAA 与 CA 返回的具体错误 |
| DNS 通过但节点连不上 | 监听端口、TCP/UDP 防火墙、客户端协议；DNS 成功不等于服务成功 |

## 参考与维护

注册页面与免费政策会变化，使用时以官方页面为准，不固定承诺可申请数量、永久有效或特定后缀可用。
用户提供的[零度教程](https://www.freedidi.com/23618.html)用于域名准备思路参考；其中 Pages/Workers/KV 部署属于另一套方案，本仓库无需执行。

预检使用的只读接口：[Cloudflare DNS JSON](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/)、[Google DNS JSON](https://developers.google.com/speed/public-dns/docs/doh/json)。
