"""记忆系统 P1 核心逻辑：召回 / 显式意图检测 / 写入 / 会话压缩。

设计：[[记忆系统-设计]] §4.1 §4.3 §6 §7。
纯逻辑，不依赖 FastAPI；DB 用 duck-typed AsyncSession（真跑用 SQLAlchemy，
单测用 Fake），LLM 用 duck-typed provider（有 async complete(system,user)）。
"""
from __future__ import annotations

import logging
from typing import Any


_log = logging.getLogger(__name__)


async def recall_memory_block(
    fs,                                          # 类型由 import 决定（避免循环导入 → 不在签名标注 MemoryFS）
    query: str,
    user_id: int,
    *,
    top_k: int = 5,
) -> str:
    """召回当前用户的记忆 L0 拼装为 system-prompt 块（S3 实现）。

    新签名（drop-in 替换 §22 DB 版）：fs + query + user_id；返回 str（空字符串
    表示无记忆，调用方 with_memory_block 据此跳过注入零开销）。
    内部委托 MemoryRecaller；S3 自包失败语义（embed/query 失败返 ""），
    调用方 try/except 不再必需（但 qa_router 既有 try 保留为防御性深度防护）。

    设计：[[文件式记忆重构-设计]] §4.1（drop-in 替换契约）+ §4.4（召回算法）。
    """
    # 局部 import：避免模块顶层循环引入（recall.py → memgen.py → 不回环 service.py）
    from src.service.memory.recall import MemoryRecaller, MemoryL0Store, _DefaultEmbedder
    # 单例 / 注入：本函数走 KE 部署期既定 Weaviate 配置；在生产里由 KE 框架装配
    # 注入；此处为简化集成层调用，按 KE 既有 Weaviate 客户端创建惯例就地构造。
    # 注：MemoryL0Store(BaseWeaviateStore) 的 __init__ 同步打开连接 / 检查 schema，
    # 重复构造代价不大（KE 既有 weaviate_*_store.py 同模式）；如需性能优化（连接池），
    # 由 S7 / 独立 hardening pass 引入单例（不在 S3 MVP）。
    # 为可测：本函数若被现存测试以 fake fs 调入但无 Weaviate 部署，会因
    # MemoryL0Store 构造抛 ConnectionError → 进入 S3 自包 try 路径返 ""。
    try:
        # 读 KE 既有部署配置（与 KE 既有 weaviate stores 一致的入口）
        import os
        url = os.getenv("WEAVIATE_URL", "http://127.0.0.1:8080")
        grpc_port = int(os.getenv("WEAVIATE_GRPC_PORT", "50051"))
        api_key = os.getenv("WEAVIATE_API_KEY") or None
        # 构造 MemoryL0Store（同步初始化连接 + 必要时建 collection）
        store = MemoryL0Store(
            url=url,
            grpc_port=grpc_port,
            collection_name="memory_l0",
            dimension=1024,
            api_key=api_key,
        )
        # MemoryL0Store._client 是 weaviate v4 client；MemoryRecaller 直接接收 client
        recaller = MemoryRecaller(embedder=_DefaultEmbedder(), weaviate_client=store._client)
        return await recaller.recall_memory_block(fs, query, user_id, top_k=top_k)
    except Exception as exc:
        # S3 自包失败语义：任何异常 → 返 ""（with_memory_block 走零开销不注入路径）。
        # 加 debug log 让运维在 Weaviate 不可达 / env 解析失败时有迹可循（修 T4 review I1）；
        # MemoryRecaller 内部各步已自记 debug log，本层覆盖 wrapper 构造期的 ConnectionError 等。
        _log.debug("recall_memory_block wrapper failed: %r", exc)
        return ""


_FOCUS_MAX = 10  # 聚焦实体上限，控 prompt 体积


def _extract_focus_entity_ids(messages: Any) -> list[str]:
    """从一组消息的 msg_metadata 聚合本次会话聚焦的 entity_id。

    来源：assistant 消息 msg_metadata 里的 cited_entities + entry_points
    （回答时已落库，复用零额外 LLM 成本）。按首见序去重，截 _FOCUS_MAX。
    全程防御：缺属性 / None / 非 dict / 字段非 list / 非字符串项 一律安全跳过。
    设计：[[记忆系统-设计]] §17。
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in messages:
        meta = getattr(m, "msg_metadata", None)
        if not isinstance(meta, dict):
            continue
        for key in ("cited_entities", "entry_points"):
            vals = meta.get(key)
            if not isinstance(vals, list):
                continue
            for v in vals:
                if isinstance(v, str) and v and v not in seen:
                    seen.add(v)
                    out.append(v)
                    if len(out) >= _FOCUS_MAX:
                        return out
    return out
