#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通用 SSH 执行器。

用法：
    python ssh_run.py -c "uname -a"            # 跑单条命令
    python ssh_run.py -f scripts/foo.sh        # 上传并执行整个脚本文件
    python ssh_run.py -f scripts/foo.sh -u     # 以 root 身份 sudo 执行

配置来源（优先级从高到低）：
    1. 命令行参数
    2. 环境变量
    3. ./deploy.env 或 ../deploy.env

依赖：
    pip install paramiko

⚠️ 关于 Python 解释器
    本脚本只用标准库 + paramiko，任何 Python 3.8+ 解释器都能跑。
    若当前解释器没装 paramiko，换一个装了 paramiko 的解释器运行即可。
"""
from __future__ import annotations

import argparse
import io
import os
import posixpath
import re
import shlex
import sys

try:
    import paramiko
except ImportError:
    sys.stderr.write(
        "缺少 paramiko。请执行：pip install paramiko\n"
        "若当前解释器不方便安装，改用系统 Python 运行本脚本。\n"
    )
    raise SystemExit(2)


# ---------------------------------------------------------------- 配置加载

def _candidate_env_files() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    return [
        os.path.join(os.getcwd(), "deploy.env"),
        os.path.join(here, "deploy.env"),
        os.path.join(here, "..", "deploy.env"),
    ]


def load_env() -> dict:
    """读 deploy.env（简单 KEY=VALUE 格式）并叠加真实环境变量。"""
    cfg: dict[str, str] = {}

    for path in _candidate_env_files():
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                if key:
                    cfg[key] = val
        break  # 只用找到的第一个

    # 环境变量覆盖文件
    for key in list(cfg.keys()) + [
        "VPS_HOST", "VPS_PORT", "VPS_USER", "VPS_PASS", "VPS_KEY",
        "DOMAIN", "SERVER_IP", "PANEL_PORT", "PANEL_PATH", "PANEL_USER",
        "PANEL_PASS", "SUB_PORT", "SUB_PATH", "API_TOKEN", "CERT_DIR",
        "REALITY_PORT", "HY2_PORT", "TUIC_PORT", "HY2_HOP_RANGE",
        "NODE_PREFIX", "MERGED_EMAIL", "MERGED_SUBID", "ACME_PORT", "SSH_PORT",
        "REALITY_DEST", "REALITY_SNI", "REALITY_PRIV", "REALITY_PUB", "REALITY_SID",
        "DISABLE_IPV6", "DNS_SERVERS",
        "VERIFY_REALITY_UUID", "VERIFY_HY2_AUTH", "VERIFY_HY2_OBFS_PW",
        "VERIFY_TUIC_UUID", "VERIFY_TUIC_PASSWORD",
    ]:
        if key in os.environ:
            cfg[key] = os.environ[key]

    return cfg


def _need(cfg: dict, key: str, hint: str = "") -> str:
    val = cfg.get(key)
    if not val:
        sys.stderr.write(f"缺少配置 {key}。{hint}\n请在 deploy.env 中设置。\n")
        raise SystemExit(2)
    return val


# ---------------------------------------------------------------- SSH 连接

def get_client(cfg: dict | None = None) -> paramiko.SSHClient:
    cfg = cfg or load_env()
    host = _need(cfg, "VPS_HOST", "例：VPS_HOST=1.2.3.4")
    port = int(cfg.get("VPS_PORT") or 22)
    user = _need(cfg, "VPS_USER", "例：VPS_USER=root")
    password = cfg.get("VPS_PASS") or None
    key_file = cfg.get("VPS_KEY") or None

    if not password and not key_file:
        sys.stderr.write("必须提供 VPS_PASS 或 VPS_KEY 之一。\n")
        raise SystemExit(2)

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(
        hostname=host,
        port=port,
        username=user,
        password=password,
        key_filename=key_file,
        timeout=20,
        banner_timeout=30,
        auth_timeout=30,
        look_for_keys=bool(key_file),
        allow_agent=bool(key_file),
    )
    return cli


def run(cli: paramiko.SSHClient, command: str, timeout: int = 300,
        pty: bool = False, get_pty: bool | None = None):
    """执行命令，返回 (exit_code, stdout, stderr)。"""
    if get_pty is not None:      # 兼容旧调用
        pty = get_pty
    stdin, stdout, stderr = cli.exec_command(command, timeout=timeout, get_pty=pty)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def env_exports(cfg: dict) -> str:
    """把配置转成一段 shell export 语句，供远端脚本读取。

    这样 `ssh_run.py -f xxx.sh` 里的脚本可以直接用 $DOMAIN / $PANEL_PORT 等变量，
    不必把值硬编码进脚本。
    """
    lines = []
    for key, val in cfg.items():
        if not key or not isinstance(val, str):
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"无效的环境变量名: {key!r}")
        lines.append(f"export {key}={shlex.quote(val)}")
    return "\n".join(lines)


def put_text(cli: paramiko.SSHClient, text: str, remote_path: str) -> None:
    """把字符串内容写到远端文件（走 SFTP）。"""
    sftp = cli.open_sftp()
    try:
        parent = posixpath.dirname(remote_path)
        if parent:
            try:
                sftp.stat(parent)
            except IOError:
                rc = cli.exec_command(f"mkdir -p -- {shlex.quote(parent)}")[1].channel.recv_exit_status()
                if rc:
                    raise OSError("无法创建远端脚本目录")
        with sftp.file(remote_path, "w") as fh:
            fh.write(text)
    finally:
        sftp.close()


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="通用 SSH 执行器")
    ap.add_argument("-c", "--command", help="要执行的命令")
    ap.add_argument("-f", "--file", help="要上传并执行的本地脚本")
    ap.add_argument("-t", "--timeout", type=int, default=600, help="超时秒数")
    ap.add_argument("-p", "--pty", action="store_true", help="分配伪终端")
    ap.add_argument("--no-cleanup", action="store_true",
                    help="执行完不删除远端临时脚本")
    args = ap.parse_args()

    if not args.command and not args.file:
        ap.error("必须指定 -c 或 -f")
    if args.file and not args.file.endswith(".sh"):
        ap.error("-f 仅支持 Bash .sh 文件；Python 编排脚本请在本地直接运行")

    cfg = load_env()
    cli = get_client(cfg)
    try:
        if args.command:
            rc, out, err = run(cli, args.command, timeout=args.timeout, pty=args.pty)
            sys.stdout.write(out)
            if err.strip():
                sys.stderr.write("\n[stderr]\n" + err)
            return rc

        # -f：上传到 /root/.xui-skill/<name> 再执行
        local = os.path.abspath(args.file)
        if not os.path.isfile(local):
            sys.stderr.write(f"文件不存在：{local}\n")
            return 2
        name = os.path.basename(local)
        remote = f"/root/.xui-skill/{name}"

        with io.open(local, "r", encoding="utf-8", newline="\n") as fh:
            content = fh.read()
        put_text(cli, content, remote)

        run(cli, f"chmod 700 -- {shlex.quote(remote)}", timeout=30)
        exports = env_exports(cfg)
        rc, out, err = run(
            cli,
            f"set -e\ncd /root\n{exports}\nbash {shlex.quote(remote)} 2>&1",
            timeout=args.timeout, pty=args.pty,
        )
        sys.stdout.write(out)
        if err.strip():
            sys.stderr.write("\n[stderr]\n" + err)

        if not args.no_cleanup:
            run(cli, f"rm -f -- {shlex.quote(remote)}", timeout=30)
        return rc
    finally:
        cli.close()


if __name__ == "__main__":
    raise SystemExit(main())
