"""语义向量生成：DashScope text-embedding-v4 后端（单一实现）。

设计：[[DashScope-Embedding-替换-Ollama-设计]]

历史：v0 用本地 Ollama bge-m3（5 entity/秒 + silent fake 回退）；
v1 切 DashScope text-embedding-v4 批量 API，云端推理 50-200 entity/秒，
失败 fail-fast（抛 EmbeddingError），不再 silent 回退伪向量。
"""
# `from __future__ import annotations`：让所有类型注解延后求值（字符串形式），
# 兼容 3.8/3.9 旧语法（虽然本项目 3.12，但保留与其他模块一致风格）
from __future__ import annotations

# os：标准库；用于读 DASHSCOPE_API_KEY env var（与 infra_health.py 同源）
import os
# time：用于重试间的 sleep 等待
import time
# Optional：类型注解工具，表示 `T | None`，给 last_err 标注用
from typing import Optional

# httpx：现代化 HTTP 客户端（项目已有依赖，infra_health.py 也在用）
# 优势：支持 timeout / connection pool / async；语法接近 requests
import httpx


# ─── 模块级常量 ─────────────────────────────────────────────────────────

# DashScope text-embedding-v4 输出维度（与 Weaviate vectordb.dimension 一致）
DIM = 1024

# 单次 API 调用最大批量（DashScope v4 文档限制 25 条 / 请求）
BATCH_MAX = 25

# 重试次数：网络错误 / 5xx 时最多重试 RETRY_TIMES 次
RETRY_TIMES = 3
# 指数退避（attempt 0 → sleep 1s，attempt 1 → sleep 2s，attempt 2 抛错不 sleep）
# 用 tuple 而非 list：常量、不可变、节省一点内存
RETRY_BACKOFF_SECS = (1.0, 2.0, 4.0)

# DashScope embedding API endpoint（与 infra_health.py:181 保持一致）
_DASHSCOPE_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
)


# ─── 自定义异常 ─────────────────────────────────────────────────────────

class EmbeddingError(RuntimeError):
    """DashScope embedding 调用失败（重试耗尽后）抛出。

    pipeline 调用方应让此异常向上传播（fail-fast），不要 catch 后写 fake 向量；
    断点续跑由 EmbeddingCheckpoint 处理 — 设计 §4.2/§4.3。

    继承 RuntimeError 而非 Exception：明确这是运行时错误（API/网络），
    不是参数类型错（那种用 TypeError/ValueError）。
    """


# ─── 对外 API ───────────────────────────────────────────────────────────

def get_embedding(text: str, dimension: int = DIM) -> list[float]:
    """单条 embedding（内部封装为 batch[1] 复用 batch 路径）。

    保留 `dimension` 参数是为兼容现有 5 个 caller（如 `get_embedding(query, self._dim)`），
    但 DashScope v4 固定 1024 维，传入值仅作占位 — 后续可在 cleanup feature 中
    让 caller 全量去掉此参数。

    :param text: 待 embedding 的文本；空 / 仅空白 → 返回零向量（不发 HTTP）
    :param dimension: 兼容参数，被忽略（v4 固定 1024）
    :returns: 长度 DIM 的 float list
    :raises EmbeddingError: DASHSCOPE_API_KEY 缺失，或重试耗尽
    """
    # 短路：空 / 仅空白 / None → 零向量；避免对 DashScope 发空 payload
    # Python 求值规则：`not text` 对 None/""/[] 都返 True；strip() 处理 "   " 情况
    if not text or not text.strip():
        return [0.0] * DIM
    # 单条复用批量路径：[text] → batch call → 取第 0 个结果
    return get_embeddings_batch([text])[0]


def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """批量 embedding。自动按 BATCH_MAX (25) 切片送 DashScope。

    :param texts: 待 embedding 的文本列表（任意长度）
    :returns: 与输入等长的向量列表，顺序一一对应
    :raises EmbeddingError: DASHSCOPE_API_KEY 缺失，或重试 3 次后仍失败
    """
    # 空 list 短路：不发请求，直接返空（避免 0 长度 chunk 的边界 case）
    if not texts:
        return []

    # 读 env：注意不缓存到全局变量；测试用 monkeypatch.setenv 时能立刻生效
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        # 缺 key 时 fail-fast，不发空请求让 DashScope 拒
        raise EmbeddingError("DASHSCOPE_API_KEY env var not configured")

    # results：累积所有 chunk 的结果，最后顺序拼接
    results: list[list[float]] = []
    # range(start, stop, step) 步进切片；用 BATCH_MAX=25 步长跨过整个 texts
    # 第一次循环 i=0 取 texts[0:25]，第二次 i=25 取 texts[25:50]，依此类推
    for i in range(0, len(texts), BATCH_MAX):
        chunk = texts[i:i + BATCH_MAX]
        # 把空白 / None text 替换为单空格：DashScope 拒绝 empty string
        # 列表推导式 + 三元表达式（ternary `a if cond else b`）；
        # 这里相当于 `for t in chunk: if t and t.strip(): out=t; else: out=" "`
        normalized = [t if (t and t.strip()) else " " for t in chunk]
        try:
            embs = _call_with_retry(api_key, normalized)
        except Exception as e:
            # 失败时附加 chunk 起始索引，便于运维定位是哪批挂了
            # `type(e).__name__` 拿到具体异常类名（如 TimeoutException）
            raise EmbeddingError(
                f"batch embedding failed at chunk starting index {i}: "
                f"{type(e).__name__}: {e}"
            ) from e
        # `extend` 把另一个 list 的元素逐个加入；不同于 `append`（整体追加）
        results.extend(embs)

    return results


# ─── 内部实现 ───────────────────────────────────────────────────────────

def _call_with_retry(api_key: str, texts: list[str]) -> list[list[float]]:
    """单次 batch HTTP 调用 + 指数退避重试。

    重试策略：
      - 仅重试网络 / HTTP 错误（TimeoutException / HTTPStatusError / RequestError）
      - 编程错误（ValueError 等）立即抛出不重试
      - 最后一次失败不 sleep（直接抛）
    """
    # last_err 跨循环保留，给最终错误信息引用
    # `Optional[Exception]` 等价于 `Exception | None`
    last_err: Optional[Exception] = None
    # `range(RETRY_TIMES)` 生成 0..RETRY_TIMES-1；attempt=2 是最后一次
    for attempt in range(RETRY_TIMES):
        try:
            return _dashscope_batch_call(api_key, texts)
        # 只捕获网络 / HTTP 错误；其它异常（如 KeyError）让它直接传出去
        # `except (A, B, C) as e` 一次捕获多个类型，避免重复 except 块
        except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError) as e:
            last_err = e
            # 最后一次失败不 sleep（直接走到 raise）；非最后一次按退避表 sleep
            if attempt < RETRY_TIMES - 1:
                time.sleep(RETRY_BACKOFF_SECS[attempt])
    # 重试耗尽：抛 EmbeddingError，附 last_err 信息
    raise EmbeddingError(
        f"DashScope retry exhausted ({RETRY_TIMES}x): "
        f"{type(last_err).__name__ if last_err else 'unknown'}: {last_err}"
    )


def _dashscope_batch_call(api_key: str, texts: list[str]) -> list[list[float]]:
    """单次 HTTP 调用 DashScope v4 batch embedding。

    response schema（DashScope 官方）::

      {
        "output": {"embeddings": [{"text_index": int, "embedding": [float, ...]}, ...]},
        "usage": {"total_tokens": int}
      }

    注意 response 中的 items 顺序不保证与请求顺序一致，需按 text_index 排序还原。
    """
    # `f"..."` 是 f-string，在大括号里求 Python 表达式插值
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # DashScope batch payload 结构：input 是嵌套 dict 含 texts 数组
    payload = {"model": "text-embedding-v4", "input": {"texts": texts}}

    # `with httpx.Client(...) as client:` 上下文管理器；
    # 退出 with 块时自动关闭 socket，避免连接泄漏
    with httpx.Client(timeout=30) as client:
        resp = client.post(_DASHSCOPE_URL, headers=headers, json=payload)
        # `raise_for_status`：4xx/5xx 抛 HTTPStatusError；
        # 该异常被外层 _call_with_retry catch → 触发重试
        resp.raise_for_status()
        data = resp.json()

    # `dict.get(key, default)` 链式取 nested 值，缺字段时返默认而非 KeyError
    items = data.get("output", {}).get("embeddings", [])
    # 按 text_index 排序还原输入顺序；DashScope response 顺序不稳定
    # `list.sort(key=...)` in-place 排序；key 是从元素映射到比较键的函数
    # 这里用 lambda（匿名函数）一行写完
    items.sort(key=lambda x: x.get("text_index", 0))
    # 取每条的 embedding 字段，组装成 list[list[float]] 返回
    return [item["embedding"] for item in items]


# ─── 工具函数（保留供下游 ke_search / weaviate store 使用） ────────────

def compute_embedding_id(entity_id: str, text: str) -> str:
    """为 (实体, 文本) 生成稳定 ID，用于向量库去重或索引。

    保持与旧实现完全一致：`vec://<sha256(entity_id|text)[:16]>`
    切换 backend 不应改 id 格式，避免已落库的向量 id 失效。
    """
    # hashlib：标准库；sha256 给定输入产生固定 64 字符 hex 摘要
    import hashlib
    # `entity_id + "|" + (text or "")` — `text or ""` 在 text=None 时退回 ""
    raw = entity_id + "|" + (text or "")
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"vec://{h}"


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度（保留与旧实现一致行为）。"""
    # 短路：任一输入为空 / 长度不一致 → 视为不可比较，返 0
    if not a or not b or len(a) != len(b):
        return 0.0
    # `zip(a, b)`：成对迭代两个序列；sum + 生成器表达式计算点积
    dot = sum(x * y for x, y in zip(a, b))
    # 各自的向量模长（L2 范数）
    # `** 0.5` 是 Python 幂运算符；等价于 math.sqrt
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    # 接近零向量时不除（避免数值精度爆炸）
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return dot / (na * nb)
