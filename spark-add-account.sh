#!/usr/bin/env bash
# spark-add-account —— 新增一个抖音账号实例（同一套代码、独立数据与端口）
# 用法: sudo spark-add-account <账号标识> <端口>
# 示例: sudo spark-add-account c 8002
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "用法: sudo spark-add-account <账号标识> <端口>"; exit 1
fi
id="$1"
port="$2"
dir="/opt/douyin-spark-data/$id"

case "$id" in
  *[!a-zA-Z0-9_-]*) echo "账号标识只能用字母数字下划线横线"; exit 1 ;;
esac

if ss -tln 2>/dev/null | grep -q ":$port "; then
  echo "端口 $port 已被占用，请换一个（可先执行 spark-status 看已用端口）"; exit 1
fi

mkdir -p "$dir"
if [ -f "$dir/env" ]; then
  token=$(grep '^AUTH_TOKEN=' "$dir/env" | cut -d= -f2)
  echo "目录已存在，沿用原令牌"
else
  token=$(openssl rand -hex 16)
  {
    echo "PORT=$port"
    echo "AUTH_TOKEN=$token"
    echo "HOST=0.0.0.0"
  } > "$dir/env"
  chmod 600 "$dir/env"
fi

systemctl enable --now "douyin-spark@$id" >/dev/null 2>&1
sleep 5

ip=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo "<服务器公网IP>")
echo "---------- 账号 $id 已创建 ----------"
echo "面板地址 : http://$ip:$port"
echo "访问令牌 : $token"
echo "数据目录 : $dir"
echo "服务状态 : $(systemctl is-active "douyin-spark@$id") / 开机自启 $(systemctl is-enabled "douyin-spark@$id")"
echo
echo "接下来 4 步："
echo "  1) 在本机用该账号运行 extract_cookie.py 扫码，得到 state.json"
echo "  2) 打开上面的面板，输入令牌，上传 state.json"
echo "  3) 「好友与消息」→ 获取聊天列表 → 勾选火花好友 → 保存"
echo "  4) 「定时设置」→ 设时间，务必与其他账号错开 1.5 小时以上"
echo
echo "别忘了在腾讯云防火墙放行 TCP $port（来源填你自己 IP 或 0.0.0.0/0）"
