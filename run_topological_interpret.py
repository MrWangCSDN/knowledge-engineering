#!/usr/bin/env python3
"""
自底向上拓扑解读 — 独立运行脚本

用法:
  source venv/bin/activate
  PYTHONPATH=. python run_topological_interpret.py [--max-per-level N] [--workers N]
"""
import argparse
import os
import sys
import time

for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"

sys.path.insert(0, ".")


def main():
    parser = argparse.ArgumentParser(description="自底向上拓扑解读 (带层级门禁)")
    parser.add_argument("--max-per-level", type=int, default=0,
                        help="每层最多解读N个方法 (0=全部, 用于测试)")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--config", default="config/project.yaml")
    parser.add_argument("--layer-gate", type=float, default=1.0,
                        help="层级门禁: 完成率阈值 0.0-1.0 (默认 1.0 = 100%)")
    parser.add_argument("--max-retry-cycles", type=int, default=5,
                        help="层级最大重试轮次 (默认 5)")
    parser.add_argument("--retry-delays", type=str, default="60,300,1800,3600,7200",
                        help="各轮重试的等待秒数, 逗号分隔 (默认 60,300,1800,3600,7200)")
    parser.add_argument("--state-file", default="out_ui/interpretation_state.json",
                        help="状态持久化文件路径")
    parser.add_argument("--reset-state", action="store_true",
                        help="清空历史 permanent_failed 状态, 重新尝试所有方法")
    args = parser.parse_args()

    # 解析 retry_delays
    try:
        retry_delays = [int(x.strip()) for x in args.retry_delays.split(",") if x.strip()]
    except Exception:
        retry_delays = [60, 300, 1800, 3600, 7200]

    # 可选: 重置状态
    if args.reset_state:
        from pathlib import Path
        p = Path(args.state_file)
        if p.exists():
            p.unlink()
            print(f"已删除状态文件: {p}")

    # 用 pipeline 的标准方式加载配置
    from src.pipeline.config_bootstrap import load_config
    config = load_config(args.config)
    k = config.knowledge

    # 加载 structure_facts
    from src.persistence.repositories.structure_facts_repository import FileStructureFactsRepository
    repo = FileStructureFactsRepository()
    print(f"加载 structure_facts...")
    facts = repo.load(config_path=args.config)
    print(f"  实体: {len(facts.entities)}, 关系: {len(facts.relations)}")

    # 构建 LLM
    from src.knowledge.llm import LLMProviderFactory
    mi = k.method_interpretation
    llm_sel = LLMProviderFactory.from_method_interpretation(mi)
    llm = llm_sel.provider
    print(f"LLM: {llm_sel.resolved_backend}")

    # 构建 Weaviate store
    from src.knowledge.weaviate_interpretation_store import WeaviateMethodInterpretStore
    vc = k.vectordb_interpret
    dim = int(vc.dimension) if vc.dimension else 1024
    store = WeaviateMethodInterpretStore(
        url=vc.weaviate_url,
        grpc_port=int(vc.weaviate_grpc_port or 50051),
        collection_name=vc.collection_name or "MethodInterpretation",
        dimension=dim,
        api_key=vc.weaviate_api_key,
    )

    # 运行拓扑解读
    from src.knowledge.topological_interpreter import TopologicalInterpreter

    def step(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def progress(c, t, msg):
        print(f"  [{c}/{t}] {msg}")

    interpreter = TopologicalInterpreter(
        structure_facts=facts,
        llm=llm,
        weaviate_store=store,
        language=mi.language or "zh",
        embedding_dim=dim,
        max_workers=args.workers,
        llm_timeout=int(mi.timeout_seconds or 90),
        repo_path=config.repo.path,
        layer_gate=args.layer_gate,
        max_retry_cycles=args.max_retry_cycles,
        retry_delays=retry_delays,
        state_file=args.state_file,
        step_callback=step,
        progress_callback=progress,
    )

    # 如果设了 max_per_level, 需要修改 run 方法
    # 简单处理: 直接用 monkey-patch
    if args.max_per_level > 0:
        original_run = interpreter._interpret_level
        def limited_run(method_ids, level, processed_before, total_todo):
            limited = method_ids[:args.max_per_level]
            step(f"  (限制每层最多 {args.max_per_level} 个)")
            return original_run(limited, level, processed_before, total_todo)
        interpreter._interpret_level = limited_run

    try:
        result = interpreter.run()
        print(f"\n{'='*60}")
        print(f"  结果: {result}")
        print(f"{'='*60}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
