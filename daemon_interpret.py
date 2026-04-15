#!/usr/bin/env python3
"""
守护进程：自动断点续跑 LLM 解读，直到全量完成。

特性：
- 增量续跑：每轮自动跳过 Weaviate 中已有的解读
- 超时/限流自动重试：单轮失败后等待 5 分钟重试
- API 额度耗尽：检测连续失败，自动延长等待时间（5→15→30分钟）
- 全量完成自动停止：当 todo_this_run=0 时结束
- 完整日志：所有输出写入 interpret_daemon.log

用法：
  cd /Users/wangshanhe/Desktop/myproject/knowledge-engineering
  source venv/bin/activate
  PYTHONPATH=. nohup python daemon_interpret.py > /dev/null 2>&1 &

  # 查看进度
  tail -f interpret_daemon.log

  # 查看汇总
  cat interpret_daemon_summary.txt
"""
import os
import sys
import time
import json
import traceback
from datetime import datetime
from pathlib import Path

# ===== 清除代理 =====
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"

# ===== 配置 =====
CONFIG_PATH = "config/project.yaml"
LOG_FILE = "interpret_daemon.log"
SUMMARY_FILE = "interpret_daemon_summary.txt"
MAX_RETRIES = 50          # 最大重试轮数
INITIAL_WAIT = 300        # 首次失败后等待秒数 (5分钟)
MAX_WAIT = 1800           # 最大等待秒数 (30分钟)
CONSECUTIVE_FAIL_LIMIT = 3  # 连续失败 N 次后延长等待


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_summary(rounds, total_tech, total_biz, total_fail, status):
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.write(f"{'='*60}\n")
        f.write(f"  LLM 解读守护进程 — 最终汇总\n")
        f.write(f"  更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'='*60}\n")
        f.write(f"  状态: {status}\n")
        f.write(f"  总轮数: {rounds}\n")
        f.write(f"  技术解读累计成功: {total_tech}\n")
        f.write(f"  业务解读累计成功: {total_biz}\n")
        f.write(f"  累计失败: {total_fail}\n")
        f.write(f"{'='*60}\n")


def run_one_round(round_num, include_tech=True, include_biz=True):
    """执行一轮解读，返回 (result_dict, error_msg)"""
    from src.pipeline.run import run_interpretations_only

    log(f"===== 第 {round_num} 轮开始 =====")

    def step_cb(msg):
        log(f"  {msg}")

    def progress_cb(current, total, msg):
        log(f"  [{current}/{total}] {msg}")

    t0 = time.time()
    try:
        result = run_interpretations_only(
            config_path=CONFIG_PATH,
            include_method_interpretation=include_tech,
            include_business_interpretation=include_biz,
            step_callback=step_cb,
            progress_callback=progress_cb,
        )
        elapsed = time.time() - t0
        log(f"===== 第 {round_num} 轮完成，耗时 {elapsed/60:.1f} 分钟 =====")
        return result, None
    except Exception as e:
        elapsed = time.time() - t0
        err_msg = f"{type(e).__name__}: {e}"
        log(f"===== 第 {round_num} 轮失败（{elapsed/60:.1f}分钟）: {err_msg} =====")
        log(traceback.format_exc())
        return None, err_msg


def is_all_done(result):
    """判断是否全量完成"""
    if not result:
        return False

    # 检查技术解读
    interp = result.get("interpretation", {})
    if interp.get("skipped"):
        tech_done = True  # 被跳过视为完成
    else:
        tech_todo = interp.get("todo_this_run", 0)
        tech_total = interp.get("total_candidates", 0)
        tech_already = interp.get("already_done_before", 0)
        tech_written = interp.get("written", 0)
        tech_done = (tech_todo == 0) or (tech_already + tech_written >= tech_total)

    # 检查业务解读
    biz = result.get("business_interpretation", {})
    if biz.get("skipped"):
        biz_done = True
    else:
        biz_todo = biz.get("todo_this_run", 0)
        biz_done = (biz_todo == 0) or (biz.get("written", 0) == 0 and biz.get("failed", 0) == 0)

    return tech_done and biz_done


def main():
    log("=" * 60)
    log("  守护进程启动 — 自动断点续跑 LLM 解读")
    log(f"  配置: {CONFIG_PATH}")
    log(f"  日志: {LOG_FILE}")
    log(f"  汇总: {SUMMARY_FILE}")
    log(f"  PID: {os.getpid()}")
    log("=" * 60)

    total_tech = 0
    total_biz = 0
    total_fail = 0
    consecutive_fails = 0
    wait_time = INITIAL_WAIT

    for round_num in range(1, MAX_RETRIES + 1):
        # 第一轮跑技术+业务，后续只跑还没完成的
        result, err = run_one_round(round_num, include_tech=True, include_biz=True)

        if err:
            # 本轮失败
            total_fail += 1
            consecutive_fails += 1

            if consecutive_fails >= CONSECUTIVE_FAIL_LIMIT:
                wait_time = min(wait_time * 2, MAX_WAIT)
                log(f"  连续失败 {consecutive_fails} 次，延长等待至 {wait_time//60} 分钟")

            write_summary(round_num, total_tech, total_biz, total_fail, f"失败重试中（等待{wait_time//60}分钟）")
            log(f"  等待 {wait_time//60} 分钟后重试...")
            time.sleep(wait_time)
            continue

        # 本轮成功
        consecutive_fails = 0
        wait_time = INITIAL_WAIT  # 重置等待时间

        # 统计本轮成果
        interp = result.get("interpretation", {})
        biz = result.get("business_interpretation", {})
        round_tech = interp.get("written", 0) if not interp.get("skipped") else 0
        round_biz = biz.get("written", 0) if not biz.get("skipped") else 0
        round_fail_tech = interp.get("failed", 0) if not interp.get("skipped") else 0
        round_fail_biz = biz.get("failed", 0) if not biz.get("skipped") else 0
        total_tech += round_tech
        total_biz += round_biz
        total_fail += round_fail_tech + round_fail_biz

        log(f"  本轮: 技术+{round_tech} 业务+{round_biz} 失败{round_fail_tech + round_fail_biz}")
        log(f"  累计: 技术={total_tech} 业务={total_biz}")

        # 检查是否全量完成
        if is_all_done(result):
            log("=" * 60)
            log("  全量解读完成！守护进程退出。")
            log(f"  总计: 技术解读 {total_tech} 条，业务解读 {total_biz} 条")
            log("=" * 60)
            write_summary(round_num, total_tech, total_biz, total_fail, "全量完成")
            return

        # 还有未完成的，继续下一轮（短暂等待避免 API 限流）
        log(f"  还有未完成的方法，30 秒后开始下一轮...")
        write_summary(round_num, total_tech, total_biz, total_fail, "进行中")
        time.sleep(30)

    log("达到最大轮数限制，守护进程退出")
    write_summary(MAX_RETRIES, total_tech, total_biz, total_fail, f"达到最大轮数 {MAX_RETRIES}")


if __name__ == "__main__":
    main()
