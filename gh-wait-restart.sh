#!/bin/bash
# 等今日定时发送结束后再重启 douyin-spark@a，让 scheduler 的抖动修复生效。
LOG=/var/log/gh-jitter-restart.log
echo "$(date '+%F %T') 守候开始：等今日发送结束再重启" >> "$LOG"

# 至少等到 01:00，避免打断 00:20 左右才真正开始的那一轮
target=$(date -d "today 01:00" +%s)
now=$(date +%s)
if [ "$now" -lt "$target" ]; then
  sleep $((target - now))
fi

TOKEN=$(grep '^AUTH_TOKEN=' /opt/douyin-spark-data/a/env | cut -d= -f2)

for _ in $(seq 1 30); do
  r=$(curl -s -H "X-Auth-Token: $TOKEN" http://127.0.0.1:8000/api/status | grep -o '"running":[a-z]*')
  if [ "$r" = '"running":false' ]; then
    sleep 15
    systemctl restart douyin-spark@a
    echo "$(date '+%F %T') 已重启，抖动修复生效" >> "$LOG"
    exit 0
  fi
  echo "$(date '+%F %T') 任务仍在运行，60 秒后再检查" >> "$LOG"
  sleep 60
done

echo "$(date '+%F %T') 超时 30 分钟仍未空闲，请手动重启" >> "$LOG"
