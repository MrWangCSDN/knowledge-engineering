# 生产部署 Runbook — release-0513 全量升级

> **目标**：把生产服务器 [[server-ldclouda30562]] 从 Phase 3 (release-0426) 升级到 release-0513
> （含 auth + 多租户 RBAC + 文件式记忆 S1-S7 + 多模型切换）+ **首次部署 web 前端**。
>
> **服务器**：ldclouda30562（凭据见 Obsidian [[server-ldclouda30562]] 笔记，不内嵌本文）
> **执行人**：你（手动逐步 ssh 执行）；遇错可在每步 checkpoint 停下排查。
> **预计耗时**：1.5–2.5 小时（含装依赖 + alembic + npm build + scp + nginx）。
> **风险等级**：中。Phase 3 的代码会被覆盖，但 Neo4j/Weaviate 数据不动 → 可逆。

> ⚠️ **凭据安全**：本 runbook 不含任何明文密码 / API key / token。
> 所有凭据来源用"占位符 + 来源指针"表达：
> - `<SSH_PWD>` → Obsidian [[server-ldclouda30562]] § 连接信息 "登录密码"
> - `<MYSQL_PWD>` → `.env.local` 中 `KE_DB_URL` 的 password 字段
> - `<NEO4J_PWD>` → 服务器 `/root/.ke-neo4j-password`
> - `<MINIMAX_API_KEY>` → `.env.local` 中 `MINIMAX_API_KEY`
> 执行时手动替换 / 复制粘贴。

---

## Pre-flight：本地准备（mac 端）

### P0. 确认本地 commit 已 push（runbook 创建时即此状态）

```bash
cd /Users/java/knowledge-engineering-auth && git log --oneline -3
# 预期最新 3 commit：aa518db / 105fffa / 9706732 (release-0513 head)

cd /Users/java/knowledge-engineering-web && git log --oneline -3
# 预期最新 3 commit：c93f9ed / 87b3f7e / 2312833 (feat/chit-chat-skill head)
```

### P1. 本地 build web 产物（dist）

```bash
cd /Users/java/knowledge-engineering-web
npm install                # 仅首次或 lock 变化时；2-3 min
npm run build              # 输出到 ./dist；约 30s
ls -lh dist/               # 验证 index.html + assets/ 存在
```

> ⚠️ 如 build 报错（tsc / vite），先在 mac 修复；千万别 scp 旧 dist 上线。

### P2. 准备生产 .env

```bash
# 在 mac 端 copy 一份到 /tmp（不入 git）
cp /Users/java/knowledge-engineering-auth/.env.local /tmp/ke-prod.env
chmod 600 /tmp/ke-prod.env

# 编辑：把 KE_DB_URL 的 host 从 localhost:3307（mac SSH tunnel）改为 127.0.0.1:3306（服务器本机）
# 编辑器用 vim/nano：
vim /tmp/ke-prod.env

# 改动点：
#   KE_DB_URL  → 把 @localhost:3307/ 改成 @127.0.0.1:3306/
#   其他字段（含 MINIMAX_API_KEY / DASHSCOPE_API_KEY / WEAVIATE_URL / JWT secrets）保持不变
```

---

## Phase A：后端部署（约 30–45 min）

### A1. SSH 上服务器 + 备份当前 release-0426

```bash
# 在 mac 端跑
ssh root@103.47.81.50 -p 26666
# 密码：<SSH_PWD>（见 Obsidian server 笔记）

# ── 以下命令在服务器上 ──

# 备份当前部署目录（可逆兜底）
tar -czf /root/ke-backup-0426-$(date +%Y%m%d).tar.gz /opt/knowledge-engineering
ls -lh /root/ke-backup-0426-*.tar.gz   # 应见 ~50 MB（含 venv）

# 备份 Neo4j 数据（保险）
NEO4J_DATA=/var/lib/neo4j/data/databases/neo4j
tar -czf /root/neo4j-backup-$(date +%Y%m%d).tar.gz -C $(dirname $NEO4J_DATA) neo4j
ls -lh /root/neo4j-backup-*.tar.gz

# 停 ke-api 服务（部署期间不接客）
systemctl stop ke-api
systemctl status ke-api    # 应显示 inactive
```

### A2. 拉 release-0513 代码

```bash
# /opt/knowledge-engineering 是 git archive 上传的（无 .git），
# 走"新建一份 → 验证 → 切换"而非 in-place pull，更稳。

which git || apt install -y git

# 克隆 release-0513 到新目录
cd /opt
git clone --branch release-0513 --depth 1 \
  https://github.com/MrWangCSDN/knowledge-engineering.git \
  knowledge-engineering-new

cd /opt/knowledge-engineering-new
git log --oneline -3
# 应见：aa518db / 105fffa / 9706732
```

> ⚠️ github 若提示需要 token：先 `git config --global credential.helper store`
> 在 mac 端生成 personal access token（github.com → Settings → Developer settings → PAT）
> 上服务器输用户名 + token 一次后自动 cache。

### A3. 迁 venv + 装新依赖

```bash
# 拷旧 venv（省 pip install 全量重装的时间）
cp -r /opt/knowledge-engineering/venv /opt/knowledge-engineering-new/venv

# 带上 javaparser-bridge jar（结构层 Java AST 用）
mkdir -p /opt/knowledge-engineering-new/javaparser-bridge/target
cp /opt/knowledge-engineering/javaparser-bridge/target/javaparser-bridge-*-shaded.jar \
   /opt/knowledge-engineering-new/javaparser-bridge/target/

# 装新依赖：auth extras（python-jose / passlib / asyncmy / alembic / pydantic[email]）
cd /opt/knowledge-engineering-new
./venv/bin/pip install -e ".[auth,neo4j,vector,owl,llm-openai]" \
  -i https://pypi.tuna.tsinghua.edu.cn/simple
# 预期：装 ~8 个新包（asyncmy/alembic/python-jose/passlib/bcrypt/python-multipart/sqlalchemy[asyncio]/pydantic[email]）
# 耗时约 1-2 min

# 验证关键 import
./venv/bin/python -c "
from src.service.api import app
from src.service.qa_engine.llm_factory import SUPPORTED_MODELS
from src.service.qa_engine.llm_minimax import MiniMaxProvider
print('imports OK, models:', [m['id'] for m in SUPPORTED_MODELS])
"
# 应输出：imports OK, models: ['qwen-plus', 'MiniMax-M2']
```

### A4. 上传 .env

```bash
# ── 回到 mac 端 ──
scp -P 26666 /tmp/ke-prod.env root@103.47.81.50:/opt/knowledge-engineering-new/.env
# 密码：<SSH_PWD>

# ── 回到服务器 ──
chmod 600 /opt/knowledge-engineering-new/.env
ls -la /opt/knowledge-engineering-new/.env
# 应见：-rw------- 1 root root ... .env
```

### A5. 初始化 MySQL + alembic 建表

```bash
# 服务器上：先确认 MySQL 跑着
systemctl status mysql || systemctl status mysqld   # 应 active

# 建库 + 建账户
# <MYSQL_PWD> 来源：mac 本地 .env.local 中 KE_DB_URL 的 password 字段
# 用法：把下面 <MYSQL_PWD> 整体替换成实际密码字符串
mysql -u root <<SQL
CREATE DATABASE IF NOT EXISTS knowledge_engineering CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'ke_app'@'localhost' IDENTIFIED BY '<MYSQL_PWD>';
GRANT ALL PRIVILEGES ON knowledge_engineering.* TO 'ke_app'@'localhost';
FLUSH PRIVILEGES;
SQL

# 跑 alembic 建所有表（users / projects / groups / qa_sessions + preferred_model 列）
cd /opt/knowledge-engineering-new
./venv/bin/alembic upgrade head
# 应见多个 "Running upgrade ... -> ..." 直到 "add_user_preferred_model"

# 验证表 + preferred_model 列
mysql -u ke_app -p'<MYSQL_PWD>' knowledge_engineering -e "SHOW TABLES;"
# 应见 10+ 张表

mysql -u ke_app -p'<MYSQL_PWD>' knowledge_engineering -e "DESC users;" | grep preferred_model
# 应见：preferred_model | varchar(64) | YES | ... NULL
```

### A6. 建测试用户 + 工程

```bash
cd /opt/knowledge-engineering-new
./venv/bin/python -m scripts.setup_test_users
# 应输出：user[new] alice/bob/carol + proj-a/b/c + access matrix
```

### A7. 切换 + 启动 ke-api

```bash
# 切换路径（保留旧目录作回滚点）
mv /opt/knowledge-engineering /opt/knowledge-engineering-old-0426
mv /opt/knowledge-engineering-new /opt/knowledge-engineering

systemctl start ke-api
systemctl status ke-api    # 应 active
journalctl -u ke-api -n 30 --no-pager  # 看启动日志，无 ERROR / Traceback

# 健康检查
curl http://127.0.0.1:8000/openapi.json | head -c 200
# 应见 OpenAPI JSON

# 登录验证（alice / test12345 是 setup_test_users.py 的默认密码）
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"test12345"}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")
echo "alice token 长度：${#TOKEN}"   # 应 > 100

# /auth/me 验证 preferred_model 字段
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/auth/me
# 应见：{"id":2,...,"preferred_model":null}
```

### A8. 后端 smoke test checkpoint

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"test12345"}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")

echo "1. /auth/me：" && curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/auth/me | head -c 200; echo
echo "2. /projects：" && curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/projects | head -c 200; echo
echo "3. /groups：" && curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/groups | head -c 100; echo
echo "4. /projects/proj-a/qa/sessions：" && curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/projects/proj-a/qa/sessions | head -c 200; echo

# 全部 200 + 数据合理 → 后端部署成功
```

> ❌ **任一步失败**：执行回滚清单（文末）→ Phase A 整体回滚到 release-0426

---

## Phase B：前端部署（约 30 min）

### B1. dist scp 到服务器

```bash
# ── mac 端 ──（dist 已在 P1 构建好）
scp -P 26666 -r /Users/java/knowledge-engineering-web/dist \
  root@103.47.81.50:/opt/knowledge-engineering-web-dist

# ── 服务器验证 ──
ls -lh /opt/knowledge-engineering-web-dist/
# 应见 index.html + assets/ + ...
```

### B2. 装 nginx + 配 vhost

```bash
# 服务器上
which nginx || apt install -y nginx

# 写 vhost 配置（注：本 cat heredoc 不含任何凭据，纯 nginx 路由配置）
cat > /etc/nginx/sites-available/ke <<'NGINX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;        # 监听所有 host header；将来有域名改成具体值

    # ── 1. SPA 静态资源 ───────────────────────────────────────────
    root /opt/knowledge-engineering-web-dist;
    index index.html;

    # SPA fallback：所有非 /api 路由都返 index.html，由 React Router 接管
    location / {
        try_files $uri $uri/ /index.html;
    }

    # ── 2. /api/* 反向代理到 FastAPI 127.0.0.1:8000 ─────────────
    # apiClient baseURL='/api' + vite rewrite 去掉 /api
    # 生产 nginx 同样 strip /api 前缀再转发，保持前后端契约一致
    location /api/ {
        proxy_pass http://127.0.0.1:8000/;   # 末尾 / = strip /api 前缀
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # ── SSE 流式必备：关 buffer + 长 timeout ────────────────
        proxy_buffering off;
        proxy_read_timeout 300s;        # /qa/explain SSE 流可能长达 5 min
        proxy_send_timeout 300s;
        chunked_transfer_encoding on;
    }

    # ── 3. /auth/* /projects/* 等无 /api 前缀也兜底 ──────────────
    location ~ ^/(auth|projects|groups)/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }

    # ── 4. gzip 静态压缩 ─────────────────────────────────────────
    gzip on;
    gzip_types text/plain text/css application/json application/javascript
               text/javascript application/x-javascript image/svg+xml;
    gzip_min_length 1024;
    gzip_comp_level 6;
}
NGINX

# 启用 vhost + 关 default
ln -sf /etc/nginx/sites-available/ke /etc/nginx/sites-enabled/ke
rm -f /etc/nginx/sites-enabled/default

# 校验 + reload
nginx -t
systemctl reload nginx
systemctl status nginx
```

### B3. 验证前端可访问

```bash
# 服务器自检
curl -s http://127.0.0.1/ | head -c 200
# 应见 index.html 内容（<title>knowledge-engineering-web</title> ...）

# /api proxy 验证
curl -s http://127.0.0.1/api/openapi.json | head -c 200
# 应见 OpenAPI JSON

# ── mac 端浏览器 ──
# 打开 http://103.47.81.50/
# 应见 KE 登录页；用 alice / test12345 登录可进
```

---

## Phase C：收尾（约 5 min）

### C1. 防火墙（可选）

```bash
ufw status
# 若 inactive 跳过；若 active：
ufw allow 26666/tcp  # ssh（避免锁自己）
ufw allow 80/tcp     # http
# ufw allow 443/tcp  # https（未来 certbot）
```

### C2. 清理旧 backup（30 天后再做）

```bash
# 暂保留 /opt/knowledge-engineering-old-0426 作回滚点
# rm -rf /opt/knowledge-engineering-old-0426
```

### C3. 更新 Obsidian 部署文档

回 mac 端，编辑 `[[生产部署-蓝队云]]`：Phase 4/5 打勾 ✅，加 release-0513 部署日志条目。

---

## 回滚清单（任一步失败用）

```bash
# 服务器上
systemctl stop ke-api
mv /opt/knowledge-engineering /opt/knowledge-engineering-failed
mv /opt/knowledge-engineering-old-0426 /opt/knowledge-engineering
systemctl start ke-api

# nginx 回滚（如已配过）
rm -f /etc/nginx/sites-enabled/ke
[ -e /etc/nginx/sites-available/default ] && ln -sf /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
systemctl reload nginx

# DB alembic 回滚（如已升级）— 谨慎，会删 preferred_model 列等
# ./venv/bin/alembic downgrade -1
```

---

## 已知风险点

1. **MySQL 凭据存 .env**：.env 已 chmod 600 + 在 .gitignore；KE_DB_URL 内含密码 — 知风险
2. **HTTP 明文传输**：登录 token 走 HTTP（无 HTTPS）— 仅适合内部 demo；正式上线需 certbot 配 HTTPS
3. **Weaviate 30621 公网开**：43.228.76.163:8080 全网可访问，仅 API key 防护；建议 ufw 限源 IP
4. **MINIMAX/DASHSCOPE/JWT 凭据复用 dev**：服务器 / mac 同一套；production 严格应分离
5. **alembic upgrade 不可幂等回滚**：先备份 MySQL（A1 中 mysqldump 缺失；DB 当前闲置可接受）

---

## 部署完成 checklist

- [ ] mac 本地 git push 已完成（aa518db / c93f9ed head）
- [ ] mac npm run build 产出 dist/
- [ ] 服务器备份 ke-backup-0426-*.tar.gz 已存 /root/
- [ ] /opt/knowledge-engineering 已是 release-0513 内容
- [ ] alembic upgrade head 跑到 add_user_preferred_model
- [ ] users 表有 alice/bob/carol；preferred_model 列存在
- [ ] ke-api.service active + curl /auth/me 返 200
- [ ] /opt/knowledge-engineering-web-dist 含 index.html
- [ ] nginx -t 通过 + systemctl status nginx active
- [ ] 浏览器 http://103.47.81.50/ 看到登录页 + alice 可登入
- [ ] Obsidian [[生产部署-蓝队云]] 更新日志
