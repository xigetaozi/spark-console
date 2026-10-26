#!/usr/bin/env bash
# spark-status —— 查看所有抖音账号实例的运行状态
# 用法: sudo spark-status
printf "%-4s %-6s %-9s %-9s %-20s %s\n" 账号 端口 服务 登录态 下次执行 上轮结果
printf -- "--------------------------------------------------------------------------------\n"
found=0
for envf in /opt/douyin-spark-data/*/env; do
  [ -f "$envf" ] || continue
  found=1
  id=$(basename "$(dirname "$envf")")
  port=$(grep '^PORT=' "$envf" | cut -d= -f2)
  token=$(grep '^AUTH_TOKEN=' "$envf" | cut -d= -f2)
  svc=$(systemctl is-active "douyin-spark@$id" 2>/dev/null || echo "-")
  js=$(curl -s -m 6 -H "X-Auth-Token: $token" "http://127.0.0.1:$port/api/status" 2>/dev/null)
  if [ -z "$js" ]; then
    printf "%-4s %-6s %-9s %s\n" "$id" "$port" "$svc" "接口无响应"
    continue
  fi
  sess=$(echo "$js" | grep -o '"session_status":"[^"]*"' | cut -d'"' -f4)
  nxt=$(echo "$js" | grep -o '"next_run":"[^"]*"' | cut -d'"' -f4 | sed 's/+08:00//' | tr 'T' ' ')
  okn=$(echo "$js" | grep -o '"ok":\[[^]]*\]' | tr ',' '\n' | grep -c '"' || true)
  failn=$(echo "$js" | grep -o '"failed":\[[^]]*\]' | grep -o 'name' | wc -l)
  case "$sess" in
    ok) state="正常" ;;
    expired) state="已过期" ;;
    unknown) state="未验证" ;;
    *) state="$sess" ;;
  esac
  printf "%-4s %-6s %-9s %-9s %-20s 成功%s 失败%s\n" "$id" "$port" "$svc" "$state" "$nxt" "$okn" "$failn"
done
[ "$found" -eq 1 ] || echo "（还没有任何账号实例）"
echo
echo "提示: 服务异常看日志 -> sudo journalctl -u douyin-spark@<账号> -n 50 --no-pager"
