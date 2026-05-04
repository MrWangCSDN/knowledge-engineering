"""语义层向量化：将 embed_text 转为向量，供知识层存储与检索。

支持多 backend（按 ``config/project.yaml`` 的 ``knowledge.semantic_embedding.backend`` 切换）：

- ``dashscope``：阿里通义百炼 ``text-embedding-v4``（OpenAI 兼容接口）；需要环境变量
  ``DASHSCOPE_API_KEY``（默认变量名，可通过 ``dashscope_api_key_env`` 覆盖）。
- ``ollama``：本地 Ollama 服务（如 ``bge-m3``），调 ``/api/embeddings``。

任一 backend 失败均回退到确定性伪向量（``_hash_vector``），保持流水线可跑、便于本地测试。
对外 API ``get_embedding(text, dimension)`` 与历史版本兼容，调用方无需改动。
"""
from __future__ import annotations

# 导入 hashlib：用于生成 SHA256 哈希，既给伪向量用，也给 entity_id 哈希用
import hashlib

# 导入 json：构造/解析 HTTP 请求体
import json

# 导入 os：从环境变量读取 API key（不写入 yaml/git，避免泄漏）
import os

# 导入 threading：保护配置单例的并发加载
import threading

# 导入 Path：定位项目根 + 读 config/project.yaml
from pathlib import Path

# 导入 Optional：类型注解
from typing import Optional

# 导入 urllib：仅用标准库做 HTTP 请求，避免引入 requests 依赖
import urllib.error
import urllib.request


# 默认向量维度：与设计文档约定 1024 对齐（DashScope v4 / Ollama bge-m3 均支持）
DEFAULT_DIM = 1024


# 配置缓存（首次加载后单例化），_cfg_lock 保护并发首次加载
_cfg: Optional[dict] = None
_cfg_lock = threading.Lock()


def get_embedding(text: str, dimension: int = DEFAULT_DIM) -> list[float]:
    """
    将文本转为固定维度向量。

    流程：
      1. 空文本直接返回零向量（避免无效 API 调用）
      2. 按配置 backend 分发到 dashscope / ollama
      3. 任一 backend 返回空时回退到 _hash_vector（确定性伪向量）
      4. 维度不匹配时截断/补零，保证调用方拿到固定长度

    Args:
        text: 待向量化文本
        dimension: 期望返回维度（默认 1024）

    Returns:
        长度恰为 dimension 的 float 列表
    """
    # 守卫：空文本直接返回零向量，节省 API 调用 + token 配额
    if not text or not text.strip():
        return [0.0] * dimension

    # 加载配置（懒加载、单例）
    cfg = _load_cfg()
    # 取 backend 名称，统一小写比较
    backend = (cfg.get("backend") or "ollama").lower()

    try:
        # 按 backend 分发 —— 当前支持 dashscope / ollama 两种
        if backend == "dashscope":
            vec = _dashscope_embedding(text, cfg, dimension)
        elif backend == "ollama":
            vec = _ollama_embedding(text, cfg)
        else:
            # 未知 backend：直接走 fallback，避免抛异常导致流水线中断
            vec = []

        # backend 返回空（失败/超时/无 key）→ 用伪向量兜底
        if not vec:
            return _hash_vector(text, dimension)

        # 维度规整：调用方期望 dimension 长度
        if len(vec) > dimension:
            return vec[:dimension]
        if len(vec) < dimension:
            return vec + [0.0] * (dimension - len(vec))
        return vec
    except Exception:
        # 任何未预期异常都回退到伪向量，保证流水线 never crash on embedding
        return _hash_vector(text, dimension)


def _load_cfg() -> dict:
    """
    从 ``config/project.yaml`` 加载 ``knowledge.semantic_embedding`` 段；缺失时使用默认。

    返回字段：
      - backend: dashscope | ollama
      - dashscope_*: dashscope provider 配置
      - ollama_*: ollama provider 配置

    使用双重检查锁（double-checked locking）确保并发首次加载只读一次 yaml。
    """
    global _cfg
    # 第一次检查（无锁，快路径）
    if _cfg is not None:
        return _cfg
    with _cfg_lock:
        # 第二次检查（有锁，防止多线程都进入加载）
        if _cfg is not None:
            return _cfg

        # 项目根：embedding.py 在 src/semantic/ 下，往上两层就是项目根
        base = Path(__file__).resolve().parents[2]
        cfg_path = base / "config" / "project.yaml"
        sem: dict = {}
        try:
            # 延迟 import yaml：仅在确实要读 yaml 时才依赖
            import yaml

            if cfg_path.exists():
                # 显式 utf-8 读取，避免 Windows / 不同 locale 下的编码问题
                raw = cfg_path.read_text(encoding="utf-8")
                # safe_load：禁止 yaml 中的任意 Python 对象反序列化
                full = yaml.safe_load(raw) or {}
                # 取 knowledge.semantic_embedding 段
                sem = (full.get("knowledge") or {}).get("semantic_embedding") or {}
        except Exception:
            # 配置缺失/不可读：用空 dict，下面的 .get() 都会用默认值
            sem = {}

        _cfg = {
            # backend 默认 ollama（保持与历史版本兼容）
            "backend": (sem.get("backend") or "ollama").lower(),
            # === Ollama（本地开发 / 内网部署） ===
            "ollama_base_url": sem.get("ollama_base_url") or "http://127.0.0.1:11434",
            "ollama_model": sem.get("ollama_model") or "bge-m3",
            # === DashScope（阿里通义百炼，OpenAI 兼容接口） ===
            "dashscope_base_url": sem.get("dashscope_base_url")
                or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "dashscope_model": sem.get("dashscope_model") or "text-embedding-v4",
            "dashscope_dimension": int(sem.get("dashscope_dimension") or DEFAULT_DIM),
            # API key 必须从环境变量读，避免明文写入 yaml 进 git
            "dashscope_api_key_env": sem.get("dashscope_api_key_env") or "DASHSCOPE_API_KEY",
        }
    return _cfg


def _dashscope_embedding(text: str, cfg: dict, dimension: int) -> list[float]:
    """
    通过阿里通义百炼 OpenAI 兼容接口获取向量。

    协议（OpenAI 兼容）::

        POST {base_url}/embeddings
        Authorization: Bearer <DASHSCOPE_API_KEY>
        Content-Type: application/json
        {
          "model": "text-embedding-v4",
          "input": "...",
          "dimensions": 1024,
          "encoding_format": "float"
        }
        响应:
        {
          "data": [{"embedding": [float, ...], "index": 0}],
          "usage": {"total_tokens": ...}
        }

    Returns:
        embedding 列表；任一异常或缺 key 返回空列表（由调用方走 fallback）
    """
    # 从环境变量取 key，**不**接受 yaml 中明文 key（避免 commit 进仓库）
    api_key = os.getenv(cfg.get("dashscope_api_key_env") or "DASHSCOPE_API_KEY", "")
    if not api_key:
        # 缺 key：返回空让上游 fallback；不抛异常以免阻塞流水线
        return []

    # base_url 标准化（去尾部 /）
    base = (cfg.get("dashscope_base_url") or "").rstrip("/")
    if not base:
        return []
    url = base + "/embeddings"

    # 构造请求体；ensure_ascii=False 保证中文不被 \uXXXX 转义（节省 token + 可读）
    payload = {
        "model": cfg.get("dashscope_model") or "text-embedding-v4",
        "input": text,
        "dimensions": int(cfg.get("dashscope_dimension") or dimension),
        "encoding_format": "float",
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # urllib.request.Request：标准库 HTTP 客户端，不引入 requests 依赖
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        # 30 秒超时：embedding 通常 <2s，30s 足以应对网络抖动
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            js = json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError):
        # 网络/解析异常：返回空让 fallback 接管
        return []

    # OpenAI 兼容响应格式：{"data": [{"embedding": [...]}, ...]}
    try:
        items = js.get("data") or []
        if items and isinstance(items, list):
            vec = items[0].get("embedding")
            if isinstance(vec, list) and vec:
                # 转 float 防止后端偶尔返回字符串数字
                return [float(x) for x in vec]
    except Exception:
        return []
    return []


def _ollama_embedding(text: str, cfg: dict) -> list[float]:
    """
    通过本地 Ollama /api/embeddings 获取向量。保留作本地开发 / 离线 fallback 用。

    Ollama API 格式::

        POST {base_url}/api/embeddings
        {"model": "bge-m3", "prompt": "..."}
        响应: {"embedding": [1024 个 float]}

    Returns:
        embedding 列表；任一异常返回空
    """
    base = (cfg.get("ollama_base_url") or "http://127.0.0.1:11434").rstrip("/")
    model = cfg.get("ollama_model") or "bge-m3"
    url = base + "/api/embeddings"
    # 注意：Ollama 用 "prompt" 参数（非 OpenAI 的 "input"）
    payload = {"model": model, "prompt": text}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        # 60 秒：本地 Ollama 大模型加载首次推理可能更慢
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            js = json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError):
        return []

    # Ollama 原生格式：{"embedding": [...]}
    vec = js.get("embedding")
    # 兼容 OpenAI 格式（某些 Ollama 兼容服务 / 代理可能返回这种）
    if not vec:
        items = js.get("data") or []
        if items and isinstance(items, list):
            vec = items[0].get("embedding")

    if not isinstance(vec, list) or not vec:
        return []

    # 转 float 列表，单元素异常用 0.0 兜底而不抛
    out: list[float] = []
    for x in vec:
        try:
            out.append(float(x))
        except Exception:
            out.append(0.0)
    return out


def _hash_vector(text: str, dimension: int) -> list[float]:
    """
    确定性伪向量：同一文本得到相同向量，便于复现与测试。

    用途：当 dashscope / ollama 都失败时的最后兜底，保证下游 Weaviate 写入不会因
    embedding 失败而崩。质量远低于真实 embedding，但能让流水线走完。
    """
    out = [0.0] * dimension
    # SHA256 hex (64 字符)：用作"伪随机但确定"的种子
    h = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    for i in range(dimension):
        # 滚动取 4 字符 hex slice 当一个分量
        sub = h[(i * 2) % len(h) : (i * 2 + 4) % len(h) + 4] or "0"
        # 映射到 [-1, 1]
        out[i] = (int(sub, 16) % 10000) / 5000.0 - 1.0
    # L2 归一化（与真实 embedding 通常 normalized 的形态一致）
    norm = (sum(x * x for x in out)) ** 0.5
    if norm > 1e-9:
        out = [x / norm for x in out]
    return out


def compute_embedding_id(entity_id: str, text: str) -> str:
    """为 (实体, 文本) 生成稳定 ID，用于向量库去重或索引。"""
    raw = entity_id + "|" + (text or "")
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"vec://{h}"


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度。两个等长向量的标准内积测度。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return dot / (na * nb)
