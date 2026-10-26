#!/usr/bin/env bash
# ============================================================================
# 抖音续火花 · 服务器端一键部署脚本
# 目标项目: Xiaowu-0916/douyin-spark (FastAPI + Playwright + APScheduler, MIT)
# 适用系统: Ubuntu 22.04 / 24.04, Debian 12 (2核2G 及以上)
#
# 用法(在服务器上以 root 执行):
#   bash bootstrap-douyin-spark.sh
#
# 脚本特性:
#   - 幂等: 重复执行不会破坏已完成的环境, 可安全重跑
#   - 不依赖仓库自带的 deploy.sh(作者脚本可能变化), 全部流程自控
#   - GitHub 直连失败时自动回退国内镜像
#   - 每步都有明确的成功/失败判定, 失败立即中断并给出排查提示
#
# 注意: 本脚本不处理登录态. 部署完成后仍需在面板上传 state.json
#       (由本机 extract_cookie.py 扫码生成), 这一步必须真人扫码.
# ============================================================================
set -euo pipefail

APP_DIR="/opt/douyin-spark"
REPO="https://github.com/Xiaowu-0916/douyin-spark.git"
PORT="8000"
SWAP_SIZE="2G"
SERVICE="douyin-spark"

C_R="\033[1;31m"; C_G="\033[1;32m"; C_Y="\033[1;33m"; C_B="\033[1;36m"; C_0="\033[0m"
log(){ printf "\n${C_B}==>${C_0} ${C_Y}%s${C_0}\n" "$*"; }
ok(){  printf "  ${C_G}✓${C_0} %s\n" "$*"; }
inf(){ printf "  · %s\n" "$*"; }
die(){ printf "\n  ${C_R}✗ %s${C_0}\n" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 0. 前置检查
log "0/9 环境检查"
[[ "${EUID}" -eq 0 ]] || die "请以 root 运行: sudo bash $0"
[[ -f /etc/os-release ]] || die "无法识别系统 (缺少 /etc/os-release)"
# shellcheck disable=SC1091
. /etc/os-release
ok "系统: ${PRETTY_NAME:-unknown}"

command -v systemctl >/dev/null || die "此系统没有 systemd, 请换用 Ubuntu/Debian 官方镜像"

MEM_MB=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo)
ok "内存: ${MEM_MB} MB"
if [[ "${MEM_MB}" -lt 1700 ]]; then
  inf "内存偏小, Chromium 运行峰值约 500-700MB, 后续必须依赖 swap"
fi

DISK_AVAIL=$(df -Pm / | awk 'NR==2{print $4}')
ok "根分区可用: ${DISK_AVAIL} MB"
[[ "${DISK_AVAIL}" -gt 3000 ]] || die "磁盘可用空间不足 3GB, 装不下 Chromium 与依赖"

ARCH=$(uname -m)
ok "架构: ${ARCH}"

# ---------------------------------------------------------------- 1. 系统更新
log "1/9 安装基础依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || die "apt-get update 失败, 请检查 /etc/apt/sources.list 或网络"
# 只装必需组件, 不做全量 upgrade(避免升级内核导致需要重启/驱动异常)
for pkg in git curl ca-certificates tzdata openssl; do
  dpkg -s "${pkg}" >/dev/null 2>&1 || apt-get install -y -qq "${pkg}"
done
ok "基础依赖就绪 (git / curl / tzdata / openssl)"

# ---------------------------------------------------------------- 2. 时区
log "2/9 设置时区 (决定定时任务在几点执行)"
timedatectl set-timezone Asia/Shanghai
CUR_TZ=$(date '+%Z')
[[ "${CUR_TZ}" == "CST" ]] || die "时区设置异常, 当前为 ${CUR_TZ}"
ok "时区: $(date '+%Z %Y-%m-%d %H:%M:%S')"
inf "如果这里不是北京时间, 你设的 21:00 会偏移 8 小时"

# ---------------------------------------------------------------- 3. swap
log "3/9 配置 ${SWAP_SIZE} swap (防止 Chromium 峰值被 OOM 杀掉)"
if swapon --show 2>/dev/null | grep -q '/swapfile'; then
  ok "swap 已挂载, 跳过创建"
else
  if [[ ! -f /swapfile ]]; then
    fallocate -l "${SWAP_SIZE}" /swapfile 2>/dev/null \
      || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
  fi
  swapon /swapfile
  ok "swap 已启用"
fi
# 关键: 不写入 fstab 的话重启后 swap 就没了
if ! grep -q '^/swapfile[[:space:]]' /etc/fstab; then
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  ok "已写入 /etc/fstab (重启后自动挂载)"
else
  ok "/etc/fstab 已有 swap 记录"
fi
# 降低 swappiness 让系统尽量用物理内存, 但它仍然存在作为兜底
sysctl -qw vm.swappiness=20 || true

# ---------------------------------------------------------------- 4. 取代码
log "4/9 获取项目代码"
if [[ -d "${APP_DIR}/.git" ]]; then
  ok "目录已存在, 尝试更新"
  ( cd "${APP_DIR}" && git pull --ff-only --depth 1 2>/dev/null ) && ok "已更新到最新" || inf "更新失败, 继续使用现有代码"
else
  mkdir -p "$(dirname "${APP_DIR}")"
  CLONED=0
  # 依次尝试: 官方源 -> 加速镜像 -> 备用镜像
  for url in "${REPO}" \
             "https://gh-proxy.com/${REPO}" \
             "https://ghfast.top/${REPO}" \
             "https://gitclone.com/github.com/Xiaowu-0916/douyin-spark.git"; do
    inf "尝试: ${url}"
    if timeout 180 git clone --depth 1 "${url}" "${APP_DIR}" 2>/dev/null; then
      CLONED=1; ok "克隆成功"; break
    fi
    rm -rf "${APP_DIR}"
  done
  [[ "${CLONED}" -eq 1 ]] || die "所有源均克隆失败. 请在本机下载 zip 后用 scp 上传解压到 ${APP_DIR}"
fi

[[ -f "${APP_DIR}/app.py" ]] || die "${APP_DIR}/app.py 不存在, 项目结构可能已变更"
ok "入口文件确认: app.py"

# ---------------------------------------------------------------- 5. 虚拟环境
log "5/9 创建 Python 虚拟环境并安装依赖"
if ! command -v python3 >/dev/null; then
  apt-get install -y -qq python3
fi
# Ubuntu 24.04 最小镜像不带 ensurepip，必须先装 venv 包，否则 venv 创建会失败
if ! python3 -c 'import ensurepip, venv' >/dev/null 2>&1; then
  apt-get install -y -qq python3-venv python3-pip >/dev/null 2>&1 \
    || apt-get install -y -qq python3.12-venv python3.12-dev python3-pip >/dev/null 2>&1 \
    || apt-get install -y -qq python3.11-venv python3.11-dev python3-pip >/dev/null 2>&1 \
    || true
  python3 -c 'import ensurepip, venv' >/dev/null 2>&1 \
    || die "python3-venv 安装失败，请手动执行 apt install python3-venv 后重跑"
  ok "已补装 python3-venv"
fi
PYV=$(python3 -c 'print(".".join(map(str,__import__("sys").version_info[:2])))')
ok "Python ${PYV}"
[[ "$(python3 -c 'import sys;print(sys.version_info>=(3,10))')" == "True" ]] \
  || inf "Python 低于 3.10, 项目可能要求 3.10+, 如报错请升级"

# 清理上次创建失败留下的半成品 venv（可能缺 pip），否则会被误判为"已存在"
if [[ -d "${APP_DIR}/.venv" ]]; then
  if ! "${APP_DIR}/.venv/bin/python" -c 'import sys' >/dev/null 2>&1 \
     || ! "${APP_DIR}/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
    rm -rf "${APP_DIR}/.venv"
    inf "已清理不完整的 venv（缺 pip 或无法启动）"
  fi
fi

if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  python3 -m venv "${APP_DIR}/.venv"
  ok "虚拟环境已创建"
else
  ok "虚拟环境已存在"
fi
VENV_PY="${APP_DIR}/.venv/bin/python"

"${VENV_PY}" -m pip install -q --upgrade pip setuptools wheel

if [[ -f "${APP_DIR}/requirements.txt" ]]; then
  inf "安装 requirements.txt"
  "${VENV_PY}" -m pip install -q -r "${APP_DIR}/requirements.txt" \
    || die "依赖安装失败. 常见原因: 缺少编译工具, 尝试 apt install -y build-essential python3-dev"
  ok "requirements.txt 安装完成"
else
  inf "未找到 requirements.txt, 按项目 README 依赖清单安装"
  "${VENV_PY}" -m pip install -q fastapi "uvicorn[standard]" playwright apscheduler python-dotenv
  ok "依赖安装完成(兜底清单)"
fi

# ---------------------------------------------------------------- 6. 浏览器
log "6/9 安装 Chromium 与系统库 (约 150MB, 较慢)"
# 国内服务器直连 cdn.playwright.dev 实测极慢(16 分钟仅 10%)，默认走 npmmirror 镜像
# 如需官方源：PLAYWRIGHT_DOWNLOAD_HOST= bash 本脚本
export PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST-https://cdn.npmmirror.com/binaries/playwright}"
if [[ -n "${PLAYWRIGHT_DOWNLOAD_HOST}" ]]; then
  inf "下载源: ${PLAYWRIGHT_DOWNLOAD_HOST}"
else
  inf "下载源: 官方 CDN"
fi
if "${VENV_PY}" -m playwright install --with-deps chromium >/tmp/pw-install.log 2>&1; then
  ok "Chromium 安装完成"
else
  inf "完整安装失败, 回退分步安装..."
  apt-get install -y -qq libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 2>/dev/null || true
  "${VENV_PY}" -m playwright install chromium >>/tmp/pw-install.log 2>&1 \
    || { tail -20 /tmp/pw-install.log; die "Chromium 安装失败, 日志见 /tmp/pw-install.log"; }
  ok "Chromium 安装完成(分步模式)"
fi

# ---------------------------------------------------------------- 7. 配置
log "7/9 生成配置与访问令牌"
mkdir -p "${APP_DIR}/data"

TOKEN=""
if [[ -f "${APP_DIR}/.env" ]] && grep -q '^AUTH_TOKEN=' "${APP_DIR}/.env"; then
  TOKEN=$(grep '^AUTH_TOKEN=' "${APP_DIR}/.env" | head -1 | cut -d= -f2-)
  ok "沿用已有 AUTH_TOKEN (不重新生成, 避免面板登录失效)"
else
  TOKEN=$(openssl rand -hex 16)
  cat > "${APP_DIR}/.env" <<EOF
# 由 bootstrap-douyin-spark.sh 生成于 $(date '+%F %T')
AUTH_TOKEN=${TOKEN}
PORT=${PORT}
HOST=0.0.0.0
TZ=Asia/Shanghai
DATA_DIR=./data
MAX_BROWSERS=3
EOF
  chmod 600 "${APP_DIR}/.env"
  ok ".env 已生成 (权限 600)"
fi

if [[ ! -f "${APP_DIR}/config.json" && -f "${APP_DIR}/config.example.json" ]]; then
  cp "${APP_DIR}/config.example.json" "${APP_DIR}/config.json"
  ok "config.json 已从示例创建"
fi

# ---------------------------------------------------------------- 8. systemd
log "8/9 注册 systemd 服务 (开机自启 + 崩溃自动拉起)"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Douyin Spark Keeper (Xiaowu-0916/douyin-spark)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai
ExecStart=${VENV_PY} ${APP_DIR}/app.py
Restart=always
RestartSec=10
# 内存保护: 单进程超过 1.5G 就重启, 避免 OOM 拖垮整机
MemoryMax=1536M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE}" >/dev/null 2>&1
systemctl restart "${SERVICE}"
sleep 6
ok "服务已注册并启动"

# ---------------------------------------------------------------- 9. 验证
log "9/9 验证 (三项全过才算成功)"
FAIL=0
if systemctl is-active --quiet "${SERVICE}"; then
  ok "服务状态: active (running)"
else
  printf "  ${C_R}✗ 服务未运行${C_0}\n"; FAIL=1
fi

if ss -tlnp 2>/dev/null | grep -q ":${PORT} "; then
  ok "端口 ${PORT}: 已监听"
else
  printf "  ${C_R}✗ 端口 ${PORT} 未监听${C_0}\n"; FAIL=1
fi

HTTP=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:${PORT}/" || echo "000")
if [[ "${HTTP}" =~ ^(200|401|403|302)$ ]]; then
  ok "HTTP 响应: ${HTTP}"
else
  printf "  ${C_R}✗ HTTP 响应异常: ${HTTP}${C_0}\n"; FAIL=1
fi

FAILED_UNIT=$(systemctl is-failed "${SERVICE}" 2>/dev/null || true)
if [[ "${FAILED_UNIT}" == "failed" ]] || [[ "${FAIL}" -eq 1 ]]; then
  printf "\n${C_R}部署未完全成功, 最近 60 行日志:${C_0}\n"
  journalctl -u "${SERVICE}" -n 60 --no-pager || true
  printf "\n排查提示:\n"
  printf "  · ModuleNotFoundError  -> 依赖没装全, 检查 requirements.txt\n"
  printf "  · 缺少 .so 文件        -> Chromium 系统库缺失, 重跑第 6 步\n"
  printf "  · Address already in use -> 改 .env 的 PORT 后 systemctl restart\n"
  exit 1
fi

# ---------------------------------------------------------------- 完成
IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo "<你的服务器IP>")
printf "\n${C_G}════════════════════════════════════════════════════════${C_0}\n"
printf "${C_G}  部署完成${C_0}\n"
printf "${C_G}════════════════════════════════════════════════════════${C_0}\n\n"
printf "  面板地址 : ${C_B}http://%s:%s${C_0}\n" "${IP}" "${PORT}"
printf "  访问令牌 : ${C_B}%s${C_0}\n" "${TOKEN}"
printf "  安装目录 : %s\n" "${APP_DIR}"
printf "  数据目录 : %s/data\n" "${APP_DIR}"
printf "  服务名   : %s\n\n" "${SERVICE}"

printf "${C_Y}接下来必须手动完成的两件事:${C_0}\n"
printf "  1. 在腾讯云控制台防火墙放行 ${PORT} 端口 (来源填你自己的 IP, 不要 0.0.0.0/0)\n"
printf "  2. 浏览器打开面板, 上传本机扫码得到的 state.json, 再同步联系人与勾选好友\n\n"
printf "${C_Y}常用命令:${C_0}\n"
printf "  看日志   : journalctl -u %s -f\n" "${SERVICE}"
printf "  重启     : systemctl restart %s\n" "${SERVICE}"
printf "  看令牌   : cat %s/.env\n" "${APP_DIR}"
printf "  看内存   : free -h\n\n"
printf "${C_Y}重要提醒:${C_0} .env 与 data/ 含账号凭据, 不要提交到任何仓库或发送给他人.\n\n"
