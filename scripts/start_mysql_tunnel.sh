#!/usr/bin/env bash
# scripts/start_mysql_tunnel.sh
#
# 开发用 SSH 隧道：把本地 3307 端口转发到蓝队云 103.47.81.50:3306（MySQL）
# 应用 .env.local 里 KE_DB_URL=mysql+asyncmy://***@localhost:3307/... 走这条隧道。
#
# 关键细节（容易踩坑）：
#   - 蓝队云 SSH **不在 22 端口**，在 **26666**（见 Obsidian: 02 Servers/server-ldclouda30562.md）
#   - 默认 `ssh root@103.47.81.50` 会连 22 → "Connection closed by ... port 22"，错误！
#
# 用法：
#   bash scripts/start_mysql_tunnel.sh        # 启动（已起则跳过）
#   bash scripts/stop_mysql_tunnel.sh         # 关闭
#
# 幂等：若 3307 已有监听 → 直接退出，不重复起。
# 校验：起完用 `nc -z 127.0.0.1 3307` 验证可达；失败时打印 ssh -vvv 提示。

set -euo pipefail

# ─── 配置 ───
REMOTE_HOST="103.47.81.50"
REMOTE_SSH_PORT="26666"      # 蓝队云非标 SSH 端口，**不要漏写**
REMOTE_USER="root"
LOCAL_PORT="3307"            # .env.local 的 KE_DB_URL 期望本地端口
REMOTE_DB_HOST="127.0.0.1"   # 远端视角，MySQL 监听 127.0.0.1
REMOTE_DB_PORT="3306"

# ─── 1) 已起则跳过（幂等） ───
if lsof -nP -iTCP:"${LOCAL_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✓ tunnel 已在 127.0.0.1:${LOCAL_PORT} 监听（跳过启动）"
  lsof -nP -iTCP:"${LOCAL_PORT}" -sTCP:LISTEN | head -3
  exit 0
fi

# ─── 2) 启动隧道（-fN 后台只转发） ───
echo "→ 启动 SSH 隧道：local:${LOCAL_PORT} → ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_SSH_PORT} → ${REMOTE_DB_HOST}:${REMOTE_DB_PORT}"

# -f  : 后台
# -N  : 不执行远端命令（纯端口转发）
# -p  : 远端 SSH 端口（关键，蓝队云用 26666）
# -L  : 本地端口绑定
# -o ServerAliveInterval=30 / CountMax=3 : 保活，30s 心跳，3 次失败即断
# -o ExitOnForwardFailure=yes : 端口已占用 / 远端拒绝时不挂"幽灵"，立即退出
if ! ssh -fN \
    -p "${REMOTE_SSH_PORT}" \
    -L "${LOCAL_PORT}:${REMOTE_DB_HOST}:${REMOTE_DB_PORT}" \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    "${REMOTE_USER}@${REMOTE_HOST}"; then
  echo "✗ 启动失败。请用 verbose 模式排查："
  echo "    ssh -vvv -p ${REMOTE_SSH_PORT} -L ${LOCAL_PORT}:${REMOTE_DB_HOST}:${REMOTE_DB_PORT} ${REMOTE_USER}@${REMOTE_HOST}"
  exit 1
fi

# ─── 3) 验证 ───
sleep 1
if nc -z 127.0.0.1 "${LOCAL_PORT}" 2>/dev/null; then
  echo "✓ 隧道已就绪：127.0.0.1:${LOCAL_PORT} 通"
  lsof -nP -iTCP:"${LOCAL_PORT}" -sTCP:LISTEN | head -3
else
  echo "✗ ssh 命令返回成功但 3307 不通 — 可能 ssh -fN 已 fork 但握手未完。再等几秒重试 nc。"
  exit 2
fi
