#!/usr/bin/env python3
"""仅运行 LLM 解读阶段（跳过结构/语义/图谱构建）。
依赖已有的 structure_facts 缓存，增量续跑不重复解读。

用法:
  source venv/bin/activate
  PYTHONPATH=. python run_interpret.py [--config CONFIG] [--tech] [--biz] [--max-methods N]
"""
import argparse
import os
import sys
import time

# 清除代理，防止 Weaviate/Ollama 连接被拦截
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"


def main():
    parser = argparse.ArgumentParser(description="仅运行 LLM 解读（跳过图谱构建）")
    parser.add_argument("--config", "-c", default="config/project.yaml", help="配置文件路径")
    parser.add_argument("--tech", action="store_true", default=True, help="运行技术解读（默认开启）")
    parser.add_argument("--no-tech", action="store_true", help="跳过技术解读")
    parser.add_argument("--biz", action="store_true", default=True, help="运行业务解读（默认开启）")
    parser.add_argument("--no-biz", action="store_true", help="跳过业务解读")
    parser.add_argument("--max-methods", type=int, default=0, help="技术解读最大方法数（0=用配置值）")
    parser.add_argument("--max-classes", type=int, default=0, help="业务解读最大类数（0=用配置值）")
    args = parser.parse_args()

    include_tech = not args.no_tech
    include_biz = not args.no_biz

    print(f"{'='*60}")
    print(f"  LLM 解读阶段 — 仅解读，跳过图谱构建")
    print(f"  配置: {args.config}")
    print(f"  技术解读: {'ON' if include_tech else 'OFF'}")
    print(f"  业务解读: {'ON' if include_biz else 'OFF'}")
    if args.max_methods:
        print(f"  max_methods 覆盖: {args.max_methods}")
    print(f"{'='*60}")

    from src.pipeline.run import run_interpretations_only, load_config

    config_path = args.config
    # 注意：max_methods/max_classes 覆盖需要直接修改配置文件中的值
    # 如果需要临时覆盖，请直接编辑 config/project.yaml

    def step_cb(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def progress_cb(current, total, msg):
        print(f"[{time.strftime('%H:%M:%S')}] [{current}/{total}] {msg}")

    t0 = time.time()
    result = run_interpretations_only(
        config_path=config_path,
        include_method_interpretation=include_tech,
        include_business_interpretation=include_biz,
        step_callback=step_cb,
        progress_callback=progress_cb,
    )
    elapsed = time.time() - t0

    print(f"\n{'='*60}")
    print(f"  解读完成！耗时 {elapsed/60:.1f} 分钟")
    print(f"{'='*60}")
    if result:
        for k, v in result.items():
            if isinstance(v, dict):
                print(f"  {k}:")
                for kk, vv in v.items():
                    print(f"    {kk}: {vv}")
            else:
                print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
