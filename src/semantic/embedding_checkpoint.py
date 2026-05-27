"""embedding 断点续跑 checkpoint：项目目录持久 + Weaviate 双层 cache。

设计：[[DashScope-Embedding-替换-Ollama-设计]] §4.3

为什么需要：DashScope batch 7000+ entity 中途失败（重试 3 次仍挂），
重跑会再次烧 7000 次 token。checkpoint 让"已 embedded 的 entity_id"持久化，
重启时跳过——典型 80%+ 跳过率。

存储位置（持久化，不放 /tmp）::

    <auth_root>/data/checkpoints/<project_id>_embedding_checkpoint.json

  - 进程重启 / Mac 重启 都不丢
  - 走 .gitignore 排除（运行时数据，不入 git）

双层设计：

1. 优先文件 cache（fast path）→ 内存 set
2. 文件缺 entity_id → 回退 Weaviate 查重（source of truth 兜底）
3. 都没有 → 真的发 DashScope（caller 负责）
"""
# `from __future__ import annotations`：注解延后求值，避免循环引用 / forward ref 麻烦
from __future__ import annotations

# json：标准库，读写 checkpoint JSON
import json
# os：用于 os.replace（atomic rename，与单纯 rename 不同，os.replace 在目标存在时也覆盖）
import os
# datetime：标准库；timezone.utc 给出带时区的 UTC 时间，isoformat() 转 ISO-8601 字符串
from datetime import datetime, timezone
from pathlib import Path
# Optional[T]：等价于 `T | None`；标 weaviate_store 可不传
from typing import Optional


# ─── 模块级常量 ─────────────────────────────────────────────────────────

# checkpoint 文件根目录（相对 auth root）
_CHECKPOINT_DIR_NAME = "data/checkpoints"


# ─── 辅助函数 ───────────────────────────────────────────────────────────

def _resolve_checkpoint_dir() -> Path:
    """返回 <auth_root>/data/checkpoints/ 绝对路径；不存在则 mkdir。

    优先用 cwd（pipeline cli 通常从 auth root 跑），cwd 没 src/ 时回退到
    本文件向上 3 层（src/semantic/embedding_checkpoint.py 的 parents[2] = src/.. = auth root）。
    """
    # `Path.cwd()` 拿当前工作目录的 Path 对象（不依赖 os.getcwd）
    cwd = Path.cwd()
    # `(cwd / "src")` — Path 重载了 / 操作符做路径拼接
    if (cwd / "src").is_dir():
        root = cwd
    else:
        # `Path(__file__).resolve()` — 当前模块的绝对路径；
        # `.parents[2]` 拿到第 3 层父目录（src/semantic/x.py → src → 项目根）
        root = Path(__file__).resolve().parents[2]
    target = root / _CHECKPOINT_DIR_NAME
    # `parents=True, exist_ok=True` 等价于 mkdir -p：自动建中间目录 + 已存在不抛
    target.mkdir(parents=True, exist_ok=True)
    return target


# ─── 主类 ───────────────────────────────────────────────────────────────

class EmbeddingCheckpoint:
    """单 project 的 embedding checkpoint，跨 pipeline 跑次持久化。

    典型用法（caller 侧，semantic/runner.py）::

        ckpt = EmbeddingCheckpoint.load(project_id, force_full=args.force_full,
                                        weaviate_store=code_store_adapter)
        pending = [e for e in entities if not ckpt.has(e.id)]
        # ... 调 get_embeddings_batch(...) ...
        for e in pending:
            ckpt.mark_done(e.id)
        ckpt.flush()
    """

    def __init__(
        self,
        project_id: str,
        model: str,
        completed_ids: set[str],
        path: Path,
        weaviate_store: Optional[object] = None,
    ):
        """直接构造一般由 .load() 完成，不建议外部直接 new。

        :param project_id: 项目 ID（决定文件名）
        :param model: 当前 embedding 模型；写盘时记进 JSON
        :param completed_ids: 已完成的 entity_id 集合（set，O(1) 查询）
        :param path: checkpoint 文件 Path
        :param weaviate_store: 兜底存储；duck-typed，需有 exists/exists_many 两方法
        """
        self.project_id = project_id
        self.model = model
        # `set[str]`：Python 3.9+ 内置泛型语法（不再需要 typing.Set）
        # 用 set 而非 list 因为 has() 查询要 O(1)
        self._done: set[str] = completed_ids
        self._path = path
        self._weaviate_store = weaviate_store
        # 累积本次跑新增的 entity_id；flush 时一次性写盘减少 I/O
        self._pending_writes: list[str] = []

    # `@classmethod` 装饰器：方法第一个参数是类 cls 而非实例 self
    # 用法：EmbeddingCheckpoint.load(...) 而非先 new 再 .load()
    @classmethod
    def load(
        cls,
        project_id: str,
        force_full: bool = False,
        weaviate_store: Optional[object] = None,
        model: str = "text-embedding-v4",
    ) -> "EmbeddingCheckpoint":
        """加载 checkpoint；force_full=True 时删 disk 文件 + 返空 checkpoint。

        返回类型用字符串 `"EmbeddingCheckpoint"` 引用自己（前向引用），
        因为类定义还没完成；3.12 + `from __future__ import annotations` 后其实
        可以直接写裸标识符，但保留字符串风格更明显。

        :param project_id: 项目 ID，决定文件名（<id>_embedding_checkpoint.json）
        :param force_full: True 时清掉 checkpoint（运维 `--force-full`），跑全量
        :param weaviate_store: Weaviate 兜底查询用，duck-typed，需有
            `.exists(project_id, entity_id) -> bool` 和
            `.exists_many(project_id, entity_ids) -> set[str]` 方法
        :param model: 当前 embedding model 名；checkpoint 文件 model 不匹配则失效
        """
        path = _resolve_checkpoint_dir() / f"{project_id}_embedding_checkpoint.json"

        # force_full 分支：先删文件，再返空
        if force_full:
            # `Path.exists()` — 文件 / 目录是否存在
            if path.exists():
                # `Path.unlink()` 删文件（os.remove 的 Path 风格）
                path.unlink()
            return cls(project_id, model, set(), path, weaviate_store)

        # 文件不存在 → 空 checkpoint
        if not path.exists():
            return cls(project_id, model, set(), path, weaviate_store)

        # 尝试读 + 解析
        try:
            # `with ... as f` 上下文管理器：异常或正常退出都关 file handle
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        # `(OSError, JSONDecodeError)` — 一次捕获两种异常
        # 文件读失败（权限 / 损坏） 或 JSON 非法都视作"没 checkpoint"
        except (OSError, json.JSONDecodeError):
            return cls(project_id, model, set(), path, weaviate_store)

        # model 不匹配 → 视作 invalid（旧 ollama 残留 / 模型升级场景）
        # `dict.get(key)` 缺字段返 None，与字符串比较安全
        if data.get("model") != model:
            return cls(project_id, model, set(), path, weaviate_store)

        # 把 list 转 set 给内存表示：O(1) 查询
        completed = set(data.get("completed_entity_ids", []))
        return cls(project_id, model, completed, path, weaviate_store)

    def has(self, entity_id: str) -> bool:
        """单个 entity_id 存在性查询。

        优先内存 set；未命中且配了 weaviate_store 则查 Weaviate 兜底
        （命中后回填 set，下次直接命中）。
        """
        # `in set` — set 用 hash 查询，平均 O(1)
        if entity_id in self._done:
            return True
        if self._weaviate_store is None:
            return False
        try:
            if self._weaviate_store.exists(self.project_id, entity_id):
                # 回填到内存 set，下次同样 id 直接命中
                self._done.add(entity_id)
                return True
        # broad except：Weaviate 故障不影响主流程；视作未命中让 caller 重新 embed
        # （比让整条 pipeline 挂掉好）
        except Exception:
            pass
        return False

    def has_many(self, entity_ids: list[str]) -> dict[str, bool]:
        """批量判存在性：一次 Weaviate 查询，提速。

        :param entity_ids: 要查询的 entity_id 列表
        :returns: {entity_id: bool} 字典，与输入一一对应
        """
        # 字典推导式：`{key_expr: value_expr for x in iterable}`
        # 这里给每个 eid 标记内存是否命中
        result = {eid: (eid in self._done) for eid in entity_ids}
        # 找出内存未命中的，去查 Weaviate
        # `result.items()` 返回 dict_items 视图，可直接拆包 (k, v)
        unknown = [eid for eid, hit in result.items() if not hit]
        if unknown and self._weaviate_store is not None:
            try:
                # exists_many 返回 set[str]：unknown 子集中真存在的 id
                existing = self._weaviate_store.exists_many(self.project_id, unknown)
                for eid in existing:
                    self._done.add(eid)
                    result[eid] = True
            except Exception:
                # 同 has()：Weaviate 故障不抛，视作 unknown 部分仍未命中
                pass
        return result

    def mark_done(self, entity_id: str) -> None:
        """记一个 entity_id 已完成（内存 + pending 写队列）。

        幂等：同一 id mark 多次只算一次（避免 pending_writes 重复）。
        """
        if entity_id in self._done:
            return
        self._done.add(entity_id)
        self._pending_writes.append(entity_id)

    def flush(self) -> None:
        """把 pending 写盘（atomic rename 防半写）。

        如果没有 pending（_pending_writes 空），直接 return 不 I/O。
        """
        if not self._pending_writes:
            return

        data = {
            "project_id": self.project_id,
            "model": self.model,
            # `sorted(set)` 给出确定顺序，便于 git diff / 人工对照
            "completed_entity_ids": sorted(self._done),
            # 带时区 UTC + ISO-8601 字符串
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        # atomic write 模式：先写 .tmp，再 os.replace 原子重命名
        # 避免进程崩在 write 中间留半文件（半文件会让下次 load 走 JSONDecodeError 分支）
        # `Path.with_suffix(".json.tmp")` —— with_suffix 替换最后一段后缀；
        # 注意：".json.tmp" 实际上是把 ".json" 替成 ".json.tmp" 这个整段
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            # ensure_ascii=False 让中文等非 ASCII 字符不被 \uXXXX 转义
            json.dump(data, f, ensure_ascii=False)
        # `os.replace(src, dst)` 原子重命名（POSIX/Windows 都保证），
        # 与 Path.rename 行为类似但保证 dst 存在时覆盖；这里用 os 模块版本更通用
        os.replace(tmp, self._path)
        # 清空 pending 队列，下次 flush 仍可重入
        self._pending_writes = []
