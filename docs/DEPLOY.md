# 部署指引

从裸机到 HTTPS 的完整流程。示例用 Ubuntu 22.04/24.04，其他发行版改包管理器即可。

---

## 一、前置准备

```bash
# 系统依赖（Playwright 需要这些库）
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip git \
     libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
     libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
     libgbm1 libasound2t64
```

> `libasound2t64` 是 24.04 的包名，22.04 用 `libasound2`。

**内存**：单实例约 300~500MB（Chromium 占大头），2 个账号建议 2GB 以上。
**磁盘**：Chromium 约 450MB，数据文件很小。

---

## 二、安装主程序

```bash
sudo mkdir -p /opt/douyin-spark && cd /opt/douyin-spark
# 把代码放进来（git clone 或 scp 上传）

sudo python3.11 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt

# Chromium：国内必须用镜像
sudo env PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright \
     PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright \
     .venv/bin/playwright install chromium
```

> 服务以 root 运行时，浏览器缓存要放在 `/root/.cache/ms-playwright`，否则找不到。

---

## 三、单实例运行（先跑通再上多账号）

```bash
cd /opt/douyin-spark

# 先生成并记下令牌，再传给进程——否则进程里随机生成一个，
# 你根本不知道要输什么，会卡在登录页
TOKEN=$(openssl rand -hex 16)
echo "面板令牌（请记下）: $TOKEN"

sudo env PORT=8000 AUTH_TOKEN="$TOKEN" \
     SPARK_DATA_DIR=/opt/douyin-spark-data/a .venv/bin/python app.py
```

浏览器打开 `http://<IP>:8000`，输入上面打印的令牌进入面板。

**第一步永远是**：概览 → 上传登录态 → 输入一个好友名 → 干跑测试。确认能切会话、能读到消息，再谈定时。

---

## 四、多账号（systemd 模板）

### 1. 模板 service

```ini
# /etc/systemd/system/douyin-spark@.service
[Unit]
Description=douyin-spark (account %i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
EnvironmentFile=/etc/douyin-spark/%i.env
WorkingDirectory=/opt/douyin-spark
ExecStart=/opt/douyin-spark/.venv/bin/python app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### 2. 每个账号一份 env

```bash
sudo mkdir -p /etc/douyin-spark /opt/douyin-spark-data/{a,b}

# 账号 a
sudo tee /etc/douyin-spark/a.env >/dev/null <<EOF
PORT=8000
AUTH_TOKEN=$(openssl rand -hex 16)
SPARK_DATA_DIR=/opt/douyin-spark-data/a
TZ=Asia/Shanghai
EOF

# 账号 b：PORT=8001, SPARK_DATA_DIR=.../b，AUTH_TOKEN 重新生成
```

```bash
sudo chmod 600 /etc/douyin-spark/*.env
sudo systemctl daemon-reload
sudo systemctl enable --now douyin-spark@a douyin-spark@b
```

### 3. 数据目录权限

```bash
sudo chmod 700 /opt/douyin-spark-data/*
sudo chmod 600 /opt/douyin-spark-data/*/state.json 2>/dev/null
```

---

## 五、控制台（可选）

控制台把多个账号聚合成一个入口，并做令牌注入，这样浏览器里不用保存令牌。

```bash
sudo mkdir -p /opt/spark-console && cd /opt/spark-console
# 把 console/ 下的文件放进来：app.py、index.html、scan.html、
# nginx-spark.conf.tpl、enable-https.sh

# 控制台与面板依赖完全相同（fastapi / uvicorn），软链复用，不必重装
sudo ln -sfn /opt/douyin-spark/.venv /opt/spark-console/.venv

sudo tee /etc/systemd/system/spark-console.service >/dev/null <<EOF
[Unit]
Description=spark-console
After=network-online.target

[Service]
Environment=PORT=80
Environment=CONSOLE_PASSWORD=<自己设一个强口令>
WorkingDirectory=/opt/spark-console
ExecStart=/opt/spark-console/.venv/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF
```

账号列表由 `ACCOUNTS_ROOT` 环境变量决定（默认 `/opt/douyin-spark-data`）：控制台会扫描 `<ACCOUNTS_ROOT>/<账号名>/env` 自动发现账号，不需要改代码。

---

## 六、HTTPS

两个前提：
1. 域名已备案（大陆服务器必须）
2. 80/443 已在云厂商防火墙放行

```bash
# 证书放到 /opt/https-ready/
sudo /opt/https-ready/enable-https.sh <域名> /opt/https-ready/cert.pem /opt/https-ready/cert.key
```

脚本会：
1. 控制台从 `0.0.0.0:80` 挪到 `127.0.0.1:8080`（systemd drop-in，不动原 unit）
2. 装 nginx，写站点配置（80 跳 443，443 反代 8080）
3. 校验并重载

**可重复执行**，失败会自动回滚端口设置。

回滚：

```bash
sudo rm -rf /etc/systemd/system/spark-console.service.d
sudo systemctl daemon-reload && sudo systemctl restart spark-console
sudo rm -f /etc/nginx/sites-enabled/spark.conf && sudo systemctl reload nginx
```

免费证书 90 天到期，建议换 Let's Encrypt 自动续期：

```bash
sudo apt install certbot
sudo certbot --nginx -d <你的域名>
```

---

## 七、排障速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 面板按钮一直是 loading | `runtime.json` 里 `running` 残留 | 重启服务（新版已自动清理） |
| 「会话列表已渲染」但一个好友都没找到 | 选择器过期 | 更新 `automation.py` 的选择器 |
| 抓列表只拿到几个人 | 滚动深度不够 | 增大第二阶段步数（默认 28） |
| 火花数明显少于预期 | 异步渲染未补齐 | 看日志的「火花扫描第 N 轮」是否有增长 |
| 点击报 `Element is not visible` | 虚拟列表行在视口外 | 确认三级兜底代码在位 |
| 扫码一直弹滑块 | 服务器 IP 被风控 | 改用本机扫码导出 state.json |
| Playwright 报找不到浏览器 | 缓存路径不匹配 | 检查 `PLAYWRIGHT_BROWSERS_PATH` |
| 定时没执行 | 抖动配置过大 / 名单为空 | 查日志「随机延迟 N 秒」「未配置任何好友」 |
| 改了 .py 文件但行为没变化 | Python 进程启动时加载模块，改文件后内存里仍是旧代码 | `systemctl restart` 重启对应实例（改前端 HTML/JS 除外，刷新即可） |
| 域名访问被拦截 | 未备案 | 走 ICP 备案流程 |

**看日志**：

```bash
sudo journalctl -u douyin-spark@a -n 50 --no-pager
```

面板「日志」标签页也能看，带自动刷新。

---

## 八、日常运维

```bash
# 状态总览
sudo systemctl status 'douyin-spark@*' --no-pager

# 重启单个账号
sudo systemctl restart douyin-spark@a

# 查看数据目录占用
sudo du -sh /opt/douyin-spark-data/*

# 消息归档过大时（>8MB 自动轮转，也可手动清理）
sudo ls -lh /opt/douyin-spark-data/a/messages.jsonl
```
