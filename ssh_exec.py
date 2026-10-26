"""远程执行命令的小工具（运维辅助，非项目必需）。

全部配置走环境变量，不含任何硬编码路径或凭据：

    export SPARK_HOST=1.2.3.4          # 必填
    export SPARK_USER=ubuntu           # 可选，默认 root
    export SPARK_PASS=******           # 密码登录（二选一）
    export SPARK_KEY_FILE=~/.ssh/id_rsa  # 或私钥登录
    export SPARK_SSH_TIMEOUT=120       # 可选，单条命令超时秒数

用法：

    python ssh_exec.py "systemctl status spark-console"
    python ssh_exec.py "sudo journalctl -u douyin-spark@a -n 30 --no-pager"

只在本地终端使用；密码不会写进日志或命令行参数。
"""
import os
import sys

import paramiko

HOST = os.environ.get("SPARK_HOST", "")
USER = os.environ.get("SPARK_USER", "root")
PASSWORD = os.environ.get("SPARK_PASS", "")
KEY_FILE = os.path.expanduser(os.environ.get("SPARK_KEY_FILE", ""))
TIMEOUT = int(os.environ.get("SPARK_SSH_TIMEOUT", "120"))


def connect() -> paramiko.SSHClient:
    if not HOST:
        sys.exit("缺少 SPARK_HOST，例：export SPARK_HOST=1.2.3.4")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    kwargs = {"hostname": HOST, "port": 22, "username": USER, "timeout": 20,
              "look_for_keys": False, "allow_agent": False}
    if KEY_FILE:
        kwargs["key_filename"] = KEY_FILE
    else:
        kwargs["password"] = PASSWORD

    client.connect(**kwargs)
    return client


def run(client: paramiko.SSHClient, cmd: str, timeout: int | None = None) -> tuple[int, str, str]:
    """执行一条命令，返回 (退出码, 标准输出, 标准错误)。"""
    _, out, err = client.exec_command(cmd, timeout=timeout or TIMEOUT)
    return (
        out.channel.recv_exit_status(),
        out.read().decode("utf-8", "ignore").strip(),
        err.read().decode("utf-8", "ignore").strip(),
    )


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)

    client = connect()
    try:
        rc, out, err = run(client, " ".join(sys.argv[1:]))
        if out:
            print(out)
        if err:
            print("[stderr]", err, file=sys.stderr)
        sys.exit(rc)
    finally:
        client.close()


if __name__ == "__main__":
    main()
