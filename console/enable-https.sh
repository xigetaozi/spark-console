#!/bin/bash
# 一键把续火花控制台切到「nginx + HTTPS」
#
# 用法：
#   sudo /opt/https-ready/enable-https.sh <域名> <证书全链.pem> <私钥.key>
# 例：
#   sudo /opt/https-ready/enable-https.sh cagophn.top /opt/https-ready/cagophn.top.pem /opt/https-ready/cagophn.top.key
#
# 做的事：
#   1. 控制台从 0.0.0.0:80 挪到 127.0.0.1:8080（systemd drop-in，不动原 unit）
#   2. 安装 nginx，写入站点配置（80 跳 443，443 反代到 8080）
#   3. 校验并重载 nginx
#
# 可重复执行；每步都做了幂等判断。

set -euo pipefail

DOMAIN="${1:-}"
CERT="${2:-}"
KEY="${3:-}"
TPL="/opt/https-ready/nginx-spark.conf.tpl"
SITE="/etc/nginx/sites-available/spark.conf"
DROPIN_DIR="/etc/systemd/system/spark-console.service.d"
EXPECT_IP="${EXPECT_IP:-}"   # 可留空；设置后用于校验域名解析是否指向本机

if [ -z "$DOMAIN" ] || [ -z "$CERT" ] || [ -z "$KEY" ]; then
  echo "用法: $0 <域名> <证书.pem> <私钥.key>"
  exit 1
fi
[ -f "$CERT" ] || { echo "证书不存在: $CERT"; exit 1; }
[ -f "$KEY" ]  || { echo "私钥不存在: $KEY"; exit 1; }
[ -f "$TPL" ]  || { echo "模板缺失: $TPL"; exit 1; }

echo "== 1/5 检查域名解析 =="
RESOLVED=$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1 || true)
if [ -z "$EXPECT_IP" ]; then
  echo "   ⏭  未设置 EXPECT_IP，跳过解析校验（当前解析到 ${RESOLVED:-<空>}）"
elif [ "$RESOLVED" = "$EXPECT_IP" ]; then
  echo "   ✅ $DOMAIN → $RESOLVED"
else
  echo "   ⚠️  $DOMAIN 解析到 '${RESOLVED:-<空>}'，期望 $EXPECT_IP"
  echo "      请先在 DNSPod 添加 A 记录；若刚添加，等 TTL（约 10 分钟）后重试。"
  read -r -p "   仍要继续吗？[y/N] " ans
  [ "$ans" = "y" ] || exit 1
fi

echo "== 2/5 控制台改监听 127.0.0.1:8080 =="
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_DIR/port.conf" <<'EOF'
[Service]
Environment=PORT=8080
Environment=HOST=127.0.0.1
EOF
systemctl daemon-reload
systemctl restart spark-console
sleep 3
if ss -tln | grep -q '127.0.0.1:8080'; then
  echo "   ✅ 控制台已在 127.0.0.1:8080"
else
  echo "   ❌ 控制台没起来，回滚端口设置"
  rm -f "$DROPIN_DIR/port.conf"; systemctl daemon-reload; systemctl restart spark-console
  exit 1
fi

echo "== 3/5 安装 nginx =="
if ! command -v nginx >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx
fi
mkdir -p /var/www/certbot
echo "   ✅ $(nginx -v 2>&1)"

echo "== 4/5 写入站点配置 =="
sed -e "s|__DOMAIN__|$DOMAIN|g" -e "s|__CERT__|$CERT|g" -e "s|__KEY__|$KEY|g" "$TPL" > "$SITE"
ln -sf "$SITE" /etc/nginx/sites-enabled/spark.conf
rm -f /etc/nginx/sites-enabled/default
if nginx -t; then
  echo "   ✅ 配置校验通过"
else
  echo "   ❌ nginx 配置有误，已中止（控制台仍在 8080，可用 IP 访问恢复：删掉 $DROPIN_DIR/port.conf 后重启）"
  exit 1
fi

echo "== 5/5 重载 nginx =="
systemctl enable nginx >/dev/null 2>&1 || true
systemctl restart nginx
sleep 2
CODE=$(curl -sk -o /dev/null -w '%{http_code}' -H "Host: $DOMAIN" https://127.0.0.1/ || true)
echo "   本机 HTTPS 自检: HTTP $CODE （200/302 即正常）"

echo
echo "完成。接下来请确认："
echo "  1) 腾讯云防火墙放行 443（现在只开了 22/80/8000/8001）"
echo "  2) 浏览器访问 https://$DOMAIN/ 应能打开控制台"
echo "  3) 手机打开 https://$DOMAIN/scan 应能调用摄像头"
echo
echo "回滚：rm -rf $DROPIN_DIR && systemctl daemon-reload && systemctl restart spark-console && rm -f /etc/nginx/sites-enabled/spark.conf && systemctl reload nginx"
