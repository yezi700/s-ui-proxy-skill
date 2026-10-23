#!/usr/bin/env python3
"""只读 DNS 预检；仅使用 Python 标准库，无需 SSH 或 Cloudflare Token。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import re
import urllib.parse
import urllib.request

RESOLVERS = {
    'Cloudflare': 'https://cloudflare-dns.com/dns-query',
    'Google': 'https://dns.google/resolve',
}
TYPES = {'A': 1, 'AAAA': 28}


def domain_name(value):
    value = value.strip().rstrip('.').encode('idna').decode('ascii').lower()
    labels = value.split('.')
    if len(value) > 253 or len(labels) < 2 or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
        for label in labels
    ):
        raise ValueError('请填写完整域名，不含 https://、端口或路径')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError('域名不能是 IP 地址')


def parse_answer(payload, record_type):
    if not isinstance(payload, dict) or type(payload.get('Status')) is not int:
        raise ValueError('DNS 响应格式错误')
    if payload['Status'] != 0:
        raise ValueError('DNS 查询失败，状态码 %s（3=NXDOMAIN，2=SERVFAIL）' % payload['Status'])
    if payload.get('TC'):
        raise ValueError('DNS 响应被截断，无法确认完整记录')
    answer = payload.get('Answer', [])
    if not isinstance(answer, list):
        raise ValueError('DNS Answer 格式错误')
    addresses = set()
    for item in answer:
        if not isinstance(item, dict):
            raise ValueError('DNS 记录格式错误')
        if item.get('type') == TYPES[record_type]:
            address = ipaddress.ip_address(item['data'])
            if address.version != (4 if record_type == 'A' else 6):
                raise ValueError('DNS 地址类型不匹配')
            addresses.add(str(address))
    return addresses


def query(resolver, domain, record_type, timeout):
    url = RESOLVERS[resolver] + '?' + urllib.parse.urlencode({'name': domain, 'type': record_type})
    request = urllib.request.Request(url, headers={'Accept': 'application/dns-json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_answer(json.load(response), record_type)


def check(domain, ipv4, ipv6=None, timeout=10, lookup=query):
    """返回 (通过, 诊断列表)；查询错误不能当作无 AAAA 记录。"""
    expected = {'A': {ipv4}, 'AAAA': {ipv6} if ipv6 else set()}
    passed, messages = True, []
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [(resolver, kind, pool.submit(lookup, resolver, domain, kind, timeout))
                for resolver in RESOLVERS for kind in TYPES]
        for resolver, kind, job in jobs:
            try:
                actual = job.result()
                ok = actual == expected[kind]
                passed = passed and ok
                messages.append('%s %s %s: %s' % (
                    'PASS' if ok else 'FAIL', resolver, kind, ', '.join(sorted(actual)) or '(无记录)'))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                passed = False
                messages.append('ERROR %s %s: %s' % (resolver, kind, exc))
    return passed, messages


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain', required=True, help='部署使用的完整域名')
    parser.add_argument('--ipv4', required=True, help='预期 VPS IPv4')
    parser.add_argument('--ipv6', help='仅在已配置可用 IPv6 时填写；默认要求没有 AAAA')
    parser.add_argument('--timeout', type=int, default=10, help='每次 HTTPS 请求超时秒数（1-60）')
    args = parser.parse_args(argv)
    try:
        domain = domain_name(args.domain)
        ipv4 = str(ipaddress.IPv4Address(args.ipv4))
        ipv6 = str(ipaddress.IPv6Address(args.ipv6)) if args.ipv6 else None
        if not 1 <= args.timeout <= 60:
            raise ValueError('timeout 必须在 1-60 秒之间')
    except (ValueError, UnicodeError) as exc:
        parser.error(str(exc))
    passed, messages = check(domain, ipv4, ipv6, args.timeout)
    print('\n'.join(messages))
    if not passed:
        print('未通过：检查权威 DNS 中的 A/AAAA、灰云状态及缓存；查询错误时先排查本机网络。')
        return 1
    print('DNS 预检通过。仅代表两个递归解析器的当前结果；不验证 NS 激活、端口、证书或节点连通性。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
