"""CLI 入口：run 子命令。"""
from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="代码知识工程流水线")
    parser.add_argument(
        "--config", "-c",
        default="config/project.yaml",
        help="项目配置 YAML 路径，默认 config/project.yaml",
    )
    parser.add_argument(
        "--until",
        choices=["structure", "semantic", "knowledge"],
        default=None,
        help="执行到该层后停止（默认执行到 knowledge）",
    )
    parser.add_argument("--output-dir", "-o", default=None, help="中间结果输出目录（structure_facts.json 等）")
    ig = parser.add_mutually_exclusive_group()
    ig.add_argument(
        "--with-interpretation",
        action="store_true",
        help="清空并重建技术解读（LLM，极慢）；默认以配置 knowledge.pipeline.include_method_interpretation_build 为准",
    )
    ig.add_argument(
        "--without-interpretation",
        action="store_true",
        help="仅重建图谱与代码向量，不跑方法技术解读",
    )
    bg = parser.add_mutually_exclusive_group()
    bg.add_argument(
        "--with-business-interpretation",
        action="store_true",
        help="本趟执行业务解读（类/API/模块，增量）；默认读 pipeline.include_business_interpretation_build",
    )
    bg.add_argument(
        "--without-business-interpretation",
        action="store_true",
        help="本趟不执行业务解读",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        # 相对当前工作目录
        config_path = Path.cwd() / config_path

    from src.pipeline.run import run_pipeline

    include_interp = None
    if args.with_interpretation:
        include_interp = True
    elif args.without_interpretation:
        include_interp = False

    include_biz = None
    if args.with_business_interpretation:
        include_biz = True
    elif args.without_business_interpretation:
        include_biz = False

    result = run_pipeline(
        config_path=config_path,
        until=args.until,
        output_dir=args.output_dir,
        include_method_interpretation=include_interp,
        include_business_interpretation=include_biz,
    )
    print("Pipeline stage:", result.get("stage", "?"))
    if "graph_nodes" in result:
        print("Graph nodes:", result["graph_nodes"], "edges:", result.get("graph_edges", 0))
    if "message" in result:
        print(result["message"])


if __name__ == "__main__":
    main()
