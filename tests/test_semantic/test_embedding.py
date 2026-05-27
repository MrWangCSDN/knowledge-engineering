"""DashScope embedding 单测 — 全套 mock HTTP，不连真实 API。

设计：[[DashScope-Embedding-替换-Ollama-设计]] §4.1 + §5.1
"""
# pytest：测试框架；提供 fixture / mark / raises
import pytest

# 从待测模块导入符号：注意这些符号在旧 embedding.py（Ollama 版）里并不存在，
# 所以本文件运行时第一次 collect 会 ImportError —— 这就是 TDD 的 RED 阶段
from src.semantic.embedding import (
    DIM,
    BATCH_MAX,
    EmbeddingError,
    get_embedding,
    get_embeddings_batch,
)


# DashScope text-embedding-v4 endpoint（与 infra_health.py:181 / 实现保持一致）
DASHSCOPE_URL = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"


def _make_response(texts: list[str]) -> dict:
    """构造 DashScope 风格 response：每个 text 一个 1024 维向量（按 text_index）。

    Python 知识点：
      - 函数注解 `texts: list[str]` 是类型提示，不影响运行时；运行不会真的检查
      - 返回 dict 用 `{...}` 字面量，嵌套层级在视觉上对齐即可
      - 列表推导式 `[expr for i in range(n)]` 是 Python 特有的简洁语法
    """
    # `range(len(texts))` 生成 0..N-1 的整数序列
    return {
        "output": {
            "embeddings": [
                {"text_index": i, "embedding": [float(i)] * DIM}
                for i in range(len(texts))
            ]
        },
        "usage": {"total_tokens": len(texts)},
    }


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    """所有测试默认设 env，单测可覆盖（pytest 内置 monkeypatch fixture）。

    autouse=True：每个测试自动注入，不用显式声明依赖；
    monkeypatch.setenv：用例结束后自动还原 env，互不污染
    """
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-fake-test-key")


def test_get_embedding_empty_returns_zero_vector():
    """空 / 空白字符串 → [0.0]*DIM，不发 HTTP 请求。"""
    # 三种 falsy / blank 输入都应该走 short-circuit 分支
    assert get_embedding("") == [0.0] * DIM
    assert get_embedding("   ") == [0.0] * DIM
    # None 触发参数校验分支；type: ignore 抑制 mypy 报错（仅 runtime 行为测试）
    assert get_embedding(None) == [0.0] * DIM   # type: ignore[arg-type]


def test_get_embeddings_batch_under_25_one_call(httpx_mock):
    """10 条 text → 1 次 HTTP 请求。

    `httpx_mock` 是 pytest-httpx 插件提供的 fixture，会拦截 httpx 客户端发出的所有请求
    """
    # 用列表推导式快速造 10 个文本
    texts = [f"text-{i}" for i in range(10)]
    # 注册一次 mock response，pytest-httpx 会按顺序消费
    httpx_mock.add_response(url=DASHSCOPE_URL, json=_make_response(texts))

    result = get_embeddings_batch(texts)
    assert len(result) == 10
    # 每个向量是 DIM 维 float list
    assert all(len(v) == DIM for v in result)
    # 只发了 1 个请求（10 < BATCH_MAX=25，不需要分片）
    assert len(httpx_mock.get_requests()) == 1


def test_get_embeddings_batch_over_25_chunked(httpx_mock):
    """60 条 → 3 次 HTTP（25+25+10），结果按原序拼接。"""
    texts = [f"text-{i}" for i in range(60)]
    # 三批 response：每批输入按 [0:25], [25:50], [50:60] 切；text_index 在每批内 0-based
    httpx_mock.add_response(url=DASHSCOPE_URL, json=_make_response(texts[:25]))
    httpx_mock.add_response(url=DASHSCOPE_URL, json=_make_response(texts[25:50]))
    httpx_mock.add_response(url=DASHSCOPE_URL, json=_make_response(texts[50:60]))

    result = get_embeddings_batch(texts)
    assert len(result) == 60
    assert len(httpx_mock.get_requests()) == 3


def test_batch_normalizes_empty_to_space(httpx_mock):
    """输入 "" / "   " → DashScope 收到 " "（不报 empty string error）。"""
    # 一次 mock 即可；只关心发出去的 payload 内容
    httpx_mock.add_response(url=DASHSCOPE_URL, json=_make_response(["a", " ", " "]))
    get_embeddings_batch(["a", "", "   "])

    # `get_requests()` 返回所有拦截到的 httpx.Request 对象
    req = httpx_mock.get_requests()[0]
    # `json` 标准库：把 bytes body 解析成 dict 验证
    import json as _json
    body = _json.loads(req.content)
    # 空 / 空白被规范化成单空格
    assert body["input"]["texts"] == ["a", " ", " "]


def test_retry_on_timeout_then_success(httpx_mock):
    """第 1 次 timeout，第 2 次成功 → 返第 2 次结果。"""
    # 局部 import 避免污染其他测试 namespace
    import httpx as _httpx
    # `add_exception` 让下次请求抛 TimeoutException（模拟网络超时）
    httpx_mock.add_exception(_httpx.TimeoutException("read timeout"))
    httpx_mock.add_response(url=DASHSCOPE_URL, json=_make_response(["a"]))

    result = get_embeddings_batch(["a"])
    assert len(result) == 1
    # 重试触发了 2 次 HTTP 调用
    assert len(httpx_mock.get_requests()) == 2


def test_retry_exhausted_raises_embedding_error(httpx_mock):
    """3 次都 503 → raise EmbeddingError，含 retry 次数信息。"""
    # 连续 3 次 503，重试耗尽后应当抛 EmbeddingError
    httpx_mock.add_response(url=DASHSCOPE_URL, status_code=503)
    httpx_mock.add_response(url=DASHSCOPE_URL, status_code=503)
    httpx_mock.add_response(url=DASHSCOPE_URL, status_code=503)

    # `pytest.raises(..., match=...)` 校验异常类型 + 错误信息正则
    with pytest.raises(EmbeddingError, match="retry exhausted"):
        get_embeddings_batch(["a"])
    assert len(httpx_mock.get_requests()) == 3


def test_missing_api_key_raises_embedding_error(monkeypatch):
    """env 缺失 DASHSCOPE_API_KEY → raise EmbeddingError。"""
    # `delenv` 删除 env；raising=False 时 key 不存在也不报错
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(EmbeddingError, match="DASHSCOPE_API_KEY"):
        get_embeddings_batch(["a"])


def test_response_sorted_by_text_index(httpx_mock):
    """response items 乱序（text_index=[2,0,1]）→ 输出按原序还原。"""
    # 显式构造乱序的 response 来测排序逻辑
    httpx_mock.add_response(url=DASHSCOPE_URL, json={
        "output": {
            "embeddings": [
                {"text_index": 2, "embedding": [3.0] * DIM},
                {"text_index": 0, "embedding": [1.0] * DIM},
                {"text_index": 1, "embedding": [2.0] * DIM},
            ]
        },
        "usage": {"total_tokens": 3},
    })

    result = get_embeddings_batch(["a", "b", "c"])
    # 验证按 text_index 排序后，结果与输入位置对应：a→1.0, b→2.0, c→3.0
    assert result[0][0] == 1.0
    assert result[1][0] == 2.0
    assert result[2][0] == 3.0


def test_get_embedding_with_dimension_arg_compat(httpx_mock):
    """get_embedding 兼容旧签名：第二个 dimension 参数传入也能调通。"""
    httpx_mock.add_response(url=DASHSCOPE_URL, json=_make_response(["q"]))
    # 第二个参数 1024 = DIM，不会触发 warning；只是验证签名兼容
    result = get_embedding("hello", 1024)
    assert len(result) == DIM
