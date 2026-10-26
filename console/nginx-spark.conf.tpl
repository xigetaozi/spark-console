# 续火花控制台 · nginx 站点配置模板
# 占位符：__DOMAIN__ __CERT__ __KEY__
# 由 enable-https.sh 替换后写入 /etc/nginx/sites-available/spark.conf

server {
    listen 80;
    listen [::]:80;
    server_name __DOMAIN__;

    # Let's Encrypt 续期验证用
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name __DOMAIN__;

    ssl_certificate     __CERT__;
    ssl_certificate_key __KEY__;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 1d;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer-when-downgrade always;

    # 扫码页要上传图片（后端限制 8MB）
    client_max_body_size 16m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 抓取聊天列表（多轮补火花）可能要 3~5 分钟
        proxy_read_timeout   600s;
        proxy_send_timeout   600s;
        proxy_connect_timeout 15s;

        # 进度条 / 日志轮询需要实时，不要缓冲
        proxy_buffering off;
    }
}
