"""MVP 占位 retriever — 不查任何真实后端，返回空 context。

存在意义：让 chat 端到端管道（前端 → SSE → LLM → 流式回答）在主仓 KnowledgeGraph
和 BusinessInterpretation Weaviate collection 真实数据接通之前就能跑通。

W6 起会被真实的 QARetriever 替换（接 BusinessInterpretationStore + KnowledgeGraph）。
"""
from __future__ import annotations

from src.service.qa_engine.retriever import RetrievedContext


class StubRetriever:
    """空检索器 — 始终返回空 context。

    搭配 QASynthesizer 使用时：
      - prompts.build_user_prompt 看到空 candidates 会输出"未命中"提示
      - LLM 收到提示后会按"context 不足以回答 → 只输出 overview 段"的规则
      - 用户能看到 LLM 流式回答（哪怕是"未找到相关业务逻辑"），而不是 503
    """

    async def retrieve(
        self,
        *,
        question: str,
        project_id: str,
        top_k: int = 5,
    ) -> RetrievedContext:
        return RetrievedContext(question=question, project_id=project_id)
