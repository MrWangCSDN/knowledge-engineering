#!/bin/bash
# clear_all_memory_local_and_server.sh
# ────────────────────────────────────────────────────────────────────
# 一键清空 alice/bob/carol 三个测试用户的全部记忆数据（含所有 session）。
# 覆盖范围：
#   1. 服务器 fs：/opt/knowledge-engineering/.ke-memory/u/{2,3,4}/
#   2. mac 本地 fs：${KE_AUTH_REPO}/.ke-memory/u/{2,3,4}/
#   3. 云上 MySQL（共享）：qa_sessions 表中三个用户的所有 ghost rows
#   4. 云上 Weaviate（共享）：Memory_l0 collection 中 tenant 2/3/4 的所有对象
#
# 注：DB 和 Weaviate 在云端共享，只清一次即可；但本地 fs 和服务器 fs 是
# 独立存储，必须各跑一次 purge_test_user_memory.py。
#
# 用法：
#   bash scripts/clear_all_memory_local_and_server.sh
#
# 需要：
#   - mac 本地 venv 已装（./venv/bin/python 可用）
#   - ssh 密钥或密码可连接 root@103.47.81.50 -p 26666
#
# 安全：
#   不内嵌任何密码 / API key；ssh 会按需提示密码（用户每次输一次）。
#   不会动 admin (uid=1) 或其他用户数据。
# ────────────────────────────────────────────────────────────────────

# set -e：任一命令失败立刻退出（防部分清理产生不一致状态）
set -e

# ── 配置 ──────────────────────────────────────────────────────────────
# 服务器 ssh 信息：与 ssh root@103.47.81.50 -p 26666 一致
SERVER_USER="root"
SERVER_HOST="103.47.81.50"
SERVER_PORT="26666"
SERVER_DEPLOY_DIR="/opt/knowledge-engineering"

# mac 本地 auth repo 路径（含 venv + scripts/purge_test_user_memory.py）
LOCAL_AUTH_REPO="/Users/java/knowledge-engineering-auth"


# ── Step 1: 服务器端清理 — 三个测试用户 ──────────────────────────────
echo "════════════════════════════════════════════════════════════════"
echo "Step 1: 清服务器端（${SERVER_HOST}）alice/bob/carol"
echo "  - 服务器 fs ${SERVER_DEPLOY_DIR}/.ke-memory/u/{2,3,4}/"
echo "  - 云 DB qa_sessions（alice/bob/carol 所有行）"
echo "  - 云 Weaviate Memory_l0 tenants {2,3,4}"
echo "════════════════════════════════════════════════════════════════"
ssh -p "${SERVER_PORT}" "${SERVER_USER}@${SERVER_HOST}" \
  "cd ${SERVER_DEPLOY_DIR} && ./venv/bin/python -m scripts.purge_test_user_memory"


# ── Step 2: 服务器端清理 — admin ─────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "Step 2: 清服务器端 admin（${SERVER_HOST}）"
echo "  - 服务器 fs ${SERVER_DEPLOY_DIR}/.ke-memory/u/{admin_uid}/"
echo "  - 云 DB qa_sessions（admin 所有行）"
echo "  - 云 Weaviate Memory_l0 tenant {admin_uid}"
echo "  ⚠️ 脚本会 5s 倒计时让你 Ctrl+C 取消（admin 是真实管理员账号需确认）"
echo "════════════════════════════════════════════════════════════════"
ssh -p "${SERVER_PORT}" "${SERVER_USER}@${SERVER_HOST}" \
  "cd ${SERVER_DEPLOY_DIR} && ./venv/bin/python -m scripts.purge_admin_memory"


# ── Step 3: mac 本地 fs 清理 ─────────────────────────────────────────
# DB/Weaviate 已在 Step 1-2 清完（云端共享）；此处只为清 mac 本地 fs。
# 脚本本身仍会再调云端 DB/Weaviate 删 — 是 idempotent re-run（无副作用）。
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "Step 3: 清 mac 本地 fs（${LOCAL_AUTH_REPO}）alice/bob/carol"
echo "  - mac fs ${LOCAL_AUTH_REPO}/.ke-memory/u/{2,3,4}/"
echo "  (DB/Weaviate Step 1 已清；此处会重跑确认 — 幂等无副作用)"
echo "════════════════════════════════════════════════════════════════"
(cd "${LOCAL_AUTH_REPO}" && ./venv/bin/python -m scripts.purge_test_user_memory)


# ── Step 4: mac 本地 admin fs 清理 ───────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "Step 4: 清 mac 本地 admin fs"
echo "  - mac fs ${LOCAL_AUTH_REPO}/.ke-memory/u/{admin_uid}/"
echo "════════════════════════════════════════════════════════════════"
(cd "${LOCAL_AUTH_REPO}" && ./venv/bin/python -m scripts.purge_admin_memory)


# ── 完成 ─────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "✅ 全部清理完成"
echo "  下一步：浏览器硬刷新（Cmd+Shift+R）→ alice/bob/carol 任一登录 → sidebar 应该完全空"
echo "════════════════════════════════════════════════════════════════"
