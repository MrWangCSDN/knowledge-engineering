"""QA 引擎：检索 + 合成代码知识答案。

模块结构：
  retriever.py     - 业务概念 → 候选实体 + 调用链上下文
  prompts.py       - LLM prompt 模板 + few-shot 示例 + 业务术语词典
  synthesizer.py   - 调 LLM 合成 6 段式答案
  sse_emitter.py   - 流式输出辅助（W4）

设计文档：[[首页设计]] §6.1 端到端 sequence
"""

from src.service.qa_engine.retriever import QARetriever, RetrievedContext

__all__ = ["QARetriever", "RetrievedContext"]
