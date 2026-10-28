#!/usr/bin/env bash
# 续火花：让线上所有账号实例真正跑上最新代码。
#
# 为什么需要这个脚本：
#   1. a、b 两个实例共用一份代码（/opt/douyin-spark），只重启一个会出现
#      "A 好了、B 还是老报错"；
#   2. git pull 只改磁盘文件，已在跑的进程仍持有旧模块，必须 restart；
#   3. 之前手敲命令时服务名/目录任一不符就会静默失败，且没有结果校验。
#
# 用法（在服务器上，root）：
#   bash /opt/douyin-spark/deploy/redeploy.sh
#
# 如果是从 Windows 传上来的，先去掉 CRLF，否则会报 $'\r': command not found：
#   sed -i 's/\r$//' /opt/douyin-spark/deploy/redeploy.sh

set -uo pipefail

ok()   { printf '  \033[32m[OK]\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; }
info() { printf '  ....   %s\n' "$*"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行：sudo bash $0"; exit 1; }

# ---------------------------------------------------------------- 1. 找实例
head_ "1/5 发现实例"
UNITS=$(systemctl list-units --type=service --all --no-legend 'douyin-spark*' 2>/dev/null \
        | awk '{print $1}' | sed 's/\.service$//' | sort -u)
if [ -z "$UNITS" ]; then
  bad "没找到任何 douyin-spark* 服务，请确认这台机器上确实部署过"
  exit 1
fi
for u in $UNITS; do
  state=$(systemctl is-active "$u" 2>/dev/null)
  info "$u（$state）"
done

# ---------------------------------------------------------------- 2. 找代码目录
head_ "2/5 定位代码目录"
CODE_DIR=""
for u in $UNITS; do
  d=$(systemctl show -p WorkingDirectory --value "$u" 2>/dev/null)
  if [ -n "$d" ] && [ -f "$d/app.py" ]; then CODE_DIR="$d"; break; fi
done
if [ -z "$CODE_DIR" ]; then
  bad "没能从服务配置里读到代码目录，请手动指定：CODE_DIR=/你的路径 bash $0"
  exit 1
fi
ok "代码目录：$CODE_DIR"

# ---------------------------------------------------------------- 3. 拉代码
head_ "3/5 更新代码"
if [ -d "$CODE_DIR/.git" ]; then
  before=$(git -C "$CODE_DIR" rev-parse --short HEAD 2>/dev/null)
  info "当前提交：$before"
  if git -C "$CODE_DIR" pull --ff-only 2>&1 | sed 's/^/         /'; then
    after=$(git -C "$CODE_DIR" rev-parse --short HEAD 2>/dev/null)
    [ "$before" = "$after" ] && info "已是最新（$after）" || ok "已更新 $before -> $after"
  else
    bad "git pull 失败（多半是服务器上有本地改动）"
    echo "         查看：git -C $CODE_DIR status"
    echo "         处理：git -C $CODE_DIR stash && 重新运行本脚本"
  fi
else
  bad "$CODE_DIR 不是 git 仓库，跳过拉代码（请确认文件是手工同步的）"
fi

WANT=$(grep -m1 '^BUILD = ' "$CODE_DIR/app.py" 2>/dev/null | cut -d'"' -f2)
[ -n "$WANT" ] && info "磁盘上的版本号：$WANT" || bad "读不到 app.py 里的 BUILD"

# ---------------------------------------------------------------- 4. 重启
head_ "4/5 重启所有实例"
for u in $UNITS; do
  if systemctl restart "$u" 2>/dev/null; then
    ok "已重启 $u"
  else
    bad "重启 $u 失败：$(systemctl status "$u" --no-pager 2>&1 | tail -3 | tr '\n' ' ')"
  fi
done
sleep 4

# ---------------------------------------------------------------- 5. 核对
head_ "5/5 核对实际跑起来的版本"
ALL_OK=1
for u in $UNITS; do
  pid=$(systemctl show -p MainPID --value "$u" 2>/dev/null)
  if [ -z "$pid" ] || [ "$pid" = "0" ]; then
    bad "$u 没有主进程（启动即退出）"
    systemctl status "$u" --no-pager 2>&1 | tail -5 | sed 's/^/         /'
    ALL_OK=0
    continue
  fi
  port=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^PORT=//p' | head -1)
  [ -z "$port" ] && port=8000
  data=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^SPARK_DATA_DIR=//p' | head -1)
  started=$(ps -o lstart= -p "$pid" 2>/dev/null | xargs)

  resp=$(curl -s --max-time 8 "http://127.0.0.1:$port/api/health" 2>/dev/null)
  build=$(printf '%s' "$resp" | grep -o '"build"[[:space:]]*:[[:space:]]*"[^"]*"' | cut -d'"' -f4)
  pool=$(printf '%s' "$resp" | grep -c '"pool"')

  if [ -z "$build" ]; then
    bad "$u 端口 $port 无响应或不是新版（/api/health 无 build 字段）"
    echo "         原始返回：${resp:-<空>}"
    ALL_OK=0
  elif [ "$pool" -lt 1 ]; then
    bad "$u build=$build 但缺 pool 字段 —— 还是旧代码"
    ALL_OK=0
  elif [ -n "$WANT" ] && [ "$build" != "$WANT" ]; then
    bad "$u build=$build，与磁盘 $WANT 不一致"
    ALL_OK=0
  else
    ok "$u 端口 $port | build=$build | data=${data:-?} | 启动于 $started"
  fi
done

head_ "结果"
if [ "$ALL_OK" -eq 1 ]; then
  echo "  全部实例已运行最新代码。"
  echo "  下一步：打开面板，确认每个账号的「实时监听 · 已运行 N 轮」已归零重计，"
  echo "         再点一次「扫描收件箱」。"
else
  echo "  有实例没起来或仍是旧版本，按上面的 FAIL 行逐条处理。"
  echo "  常用排查："
  echo "    journalctl -u <实例> -n 50 --no-pager"
  echo "    systemctl status <实例> --no-pager"
  exit 1
fi
