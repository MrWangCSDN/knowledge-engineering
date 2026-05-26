#!/usr/bin/env bash
# scripts/stop_mysql_tunnel.sh
#
# 关闭 scripts/start_mysql_tunnel.sh 起的 SSH 隧道。
# 通过 pgrep 找 ssh -fN -p 26666 ... -L 3307:127.0.0.1:3306 的进程并 kill。

set -euo pipefail

# 严格匹配本脚本起的命令行模式，避免误杀其他 ssh
PIDS=$(pgrep -fl "ssh.*-N.*-p 26666.*-L 3307:" | awk '{print $1}')

if [[ -z "${PIDS}" ]]; then
  echo "✓ 当前无 mysql tunnel 进程在跑（没什么要关的）"
  exit 0
fi

echo "→ 关闭 tunnel 进程：${PIDS}"
# shellcheck disable=SC2086  # 故意词分割
kill ${PIDS}

# 等一秒确认端口已释放
sleep 1
if lsof -nP -iTCP:3307 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✗ 3307 还有其他进程在监听（不是本脚本的 tunnel）。请用 lsof 排查："
  lsof -nP -iTCP:3307 -sTCP:LISTEN
  exit 1
fi
echo "✓ tunnel 已关闭，3307 已释放"
