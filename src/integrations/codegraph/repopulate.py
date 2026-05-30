# src/integrations/codegraph/repopulate.py
"""把 CodeGraph 方法节点重灌进 Weaviate CodeEntity，按 durable_key 落库。

独立例程：不动 graph.build_from。依赖以「可注入」方式传入（embed_batch / store），
便于单测用假实现，也便于将来换 embedder/store。
"""
from __future__ import annotations  # PEP 563：允许类型注解里提前引用未定义的类名

import logging  # 标准库日志模块；__name__ 让日志归属到当前模块路径
from typing import Any, Callable, Optional  # 类型注解辅助：Any=任意类型, Callable=可调用对象

# 引入 Phase 1 已有的只读 DB 访问层
from src.integrations.codegraph.db import CodeGraphDB
# 引入 durable_key：生成与行号无关的持久节点身份
from src.integrations.codegraph.durable_key import durable_key
# 引入源码片段读取函数（Task 2a.1 新建）
from src.integrations.codegraph.source_reader import read_snippet

# 获取模块级 logger；logging.getLogger 是单例，同名 logger 在整个进程里共享
_LOG = logging.getLogger(__name__)

# embed_batch 的类型别名：给一批文本列表、返回一批向量列表
# Callable[[list[str]], list[list[float]]] 表示"接受一个字符串列表，返回浮点数列表的列表"
EmbedBatch = Callable[[list[str]], list[list[float]]]


def repopulate_code_entities(
    *,                          # * 强制后续所有参数必须以关键字方式传入，防止位置参数出错
    db: CodeGraphDB,            # 只读 SQLite 访问层（Phase 1）
    repo_root: str,             # 工程源码根目录路径（读片段用）
    project_id: str,            # 多租户 ID，等同 tenant（如 'mall-swarm'）
    embed_batch: EmbedBatch,    # 批量 embed 函数（真用 get_embeddings_batch，测试用假的）
    store: Any,                 # 向量库（需有 add 方法；Any 保留灵活性，不强绑 Weaviate）
    batch_size: int = 10,       # 每批 embed 多少条（DashScope 上限 10）
    skip_keys: Optional[set[str]] = None,  # 已完成的 durable_key 集合（断点续跑用）
) -> dict:
    """全量重灌：遍历方法节点 → durable_key + 源码片段 → embed → store.add。

    设计要点：
    - 依赖注入（embed_batch / store）：测试易 mock，生产可替换
    - skip_keys：支持断点续跑/增量（传入已完成 key 集合即可跳过）
    - batch_size：配合 DashScope API 上限，避免单次 embed 超限

    Args:
        db:         Phase 1 的只读 CodeGraphDB
        repo_root:  工程源码根（读片段用），通常 = repo_local_path
        project_id: 多租户 tenant
        embed_batch: 批量 embed 函数（真用 get_embeddings_batch，测试用假的）
        store:      向量库（需有 add(entity_id, vector, entity_type, name, code_snippet, *, tenant)）
        batch_size: 每批 embed 多少条（DashScope 上限 10）
        skip_keys:  已完成的 durable_key 集合（断点续跑/增量时传，跳过它们）
    Returns:
        统计字典 {"scanned": int, "embedded": int, "skipped": int}
    """
    # None → 空集合；用 or 做默认值，避免可变默认参数陷阱（直接写 skip_keys=set() 会跨调用共享）
    skip_keys = skip_keys or set()

    # pending：待 embed 的三元组列表；元组 = (durable_key, 短名, 源码片段)
    # list[tuple[str, str, str]] 是类型注解，说明列表里存的是三个字符串的元组
    pending: list[tuple[str, str, str]] = []
    scanned = 0  # 扫描过的节点计数器（含跳过的）

    # 遍历所有 method 节点（iter_method_nodes 返回 list[CgNode]）
    for node in db.iter_method_nodes():
        scanned += 1                                # 每扫一个节点计数+1
        key = durable_key(node)                     # 派生持久身份：qualified_name#签名
        if key in skip_keys:                        # 增量/续跑：已完成则跳过
            continue                               # continue：跳过当前循环体的剩余部分，直接进入下一轮
        # 从源码文件里读方法体片段（1-indexed 闭区间 [start, end]）
        snippet = read_snippet(repo_root, node.file_path, node.start_line, node.end_line)
        if not snippet:                             # 读不到源码（文件不存在/行号超界）→ 跳过
            continue                               # 无法 embed 空字符串，避免 embed API 报错
        # 把合法的三元组追加到待处理列表
        pending.append((key, node.name, snippet))

    embedded = 0  # 实际成功 embed 并写入 store 的节点计数

    # range(start, stop, step)：生成等差整数序列；这里用来把 pending 切成 batch_size 大小的块
    for i in range(0, len(pending), batch_size):
        # 列表切片：取 pending[i] ~ pending[i+batch_size-1]，最后一块自动截断不越界
        chunk = pending[i:i + batch_size]
        # 列表推导式：从每个三元组里取第 3 个元素（snippet），组成文本列表给 embedder
        # embed_batch 一次接收整块文本，返回等长的向量列表
        vecs = embed_batch([snip for _, _, snip in chunk])

        # zip(chunk, vecs)：同步迭代两个等长序列，每次分别拿到 (key,name,snip) 和对应向量
        for (key, name, snip), vec in zip(chunk, vecs):
            store.add(
                key, vec,                           # 主键（durable_key）+ 向量
                entity_type="method",               # 节点类型标签
                name=name,                          # 短方法名（便于检索结果展示）
                code_snippet=snip,                  # 源码片段（存进向量库供召回后展示）
                tenant=project_id,                  # 多租户隔离键（Weaviate 用）
            )
            embedded += 1  # 每成功写入一条计数+1

    # 构造并记录统计信息
    stats = {"scanned": scanned, "embedded": embedded, "skipped": scanned - embedded}
    # %d 是整数格式化占位符；logging 的延迟格式化只有真正输出时才拼串，节省 CPU
    _LOG.info("[codegraph] CodeEntity 重灌：扫描 %d，embed %d", scanned, embedded)
    return stats


def repopulate_incremental(
    *,
    db: CodeGraphDB,            # 只读 SQLite 访问层（Phase 1）
    repo_root: str,             # 工程源码根目录（读片段用）
    project_id: str,            # 多租户 ID，等同 tenant
    embed_batch: EmbedBatch,    # 批量 embed 函数（真用 get_embeddings_batch，测试用假的）
    store: Any,                 # 向量库（需有 add + delete 方法；Any 保留灵活性）
    checkpoint: Any,            # ContentCheckpoint 实例（内容 hash 持久化）
    batch_size: int = 10,       # 每批 embed 多少条（DashScope 上限 10）
) -> dict:
    """增量重灌：只 embed 内容变化/新增的方法；删 CodeGraph 已不存在的孤儿。

    与全量 repopulate_code_entities 的区别：
    - 全量：每次都 embed 所有 skip_keys 以外的节点
    - 增量：通过 ContentCheckpoint 比对代码 hash，内容没变就不重 embed，节省 API 调用

    store 需有 add(...) 与 delete(entity_id, *, tenant)（删孤儿用；无 delete 则只记日志）。

    Args:
        db:          只读 CodeGraphDB
        repo_root:   工程源码根
        project_id:  多租户 tenant
        embed_batch: 批量 embed 函数
        store:       向量库（需有 add；有 delete 则删孤儿）
        checkpoint:  ContentCheckpoint 实例（内容 hash 比对 + 持久化）
        batch_size:  每批 embed 多少条
    Returns:
        统计字典 {"embedded": int, "deleted": int, "current": int}
    """
    # pending：需要重新 embed 的三元组列表；(durable_key, 短名, 代码片段)
    # list[tuple[str, str, str]] 是类型注解，说明列表元素是三个字符串的元组
    pending: list[tuple[str, str, str]] = []
    # current_keys：当前 CodeGraph 里所有方法的 durable_key 集合（用于后续算孤儿）
    current_keys: set[str] = set()

    # 遍历所有 method 节点，收集需要重 embed 的节点
    for node in db.iter_method_nodes():
        key = durable_key(node)                     # 派生持久身份（行号无关）
        current_keys.add(key)                       # 无论是否需要重 embed，都加入当前集合
        snippet = read_snippet(repo_root, node.file_path, node.start_line, node.end_line)
        if not snippet:                             # 读不到源码则跳过（文件缺失/行号越界）
            continue
        # checkpoint.changed：比对内容 hash；新 key 或代码改了才需要重 embed
        if checkpoint.changed(key, snippet):
            pending.append((key, node.name, snippet))

    embedded = 0  # 实际 embed 并写入 store 的条数

    # range(0, len, batch_size) 按批次切分，配合 DashScope API 每次 ≤10 条限制
    for i in range(0, len(pending), batch_size):
        chunk = pending[i:i + batch_size]
        # 批量向量化：只传 snippet（文本），返回等长的向量列表
        vecs = embed_batch([s for _, _, s in chunk])
        # zip 同步迭代 chunk 和 vecs，一一对应处理
        for (key, name, snip), vec in zip(chunk, vecs):
            store.add(key, vec, entity_type="method", name=name, code_snippet=snip, tenant=project_id)
            checkpoint.mark(key, snip)              # 记录新 hash（embed 成功后才记）
            embedded += 1

    deleted = 0  # 从 Weaviate 删除的孤儿条数

    # getattr(obj, "delete", None)：安全取属性；store 没有 delete 方法时返回 None（best-effort）
    delete_method = getattr(store, "delete", None)
    # checkpoint.orphans(current_keys)：返回已记录但当前 CodeGraph 里不存在的 key
    for orphan in checkpoint.orphans(current_keys):
        # callable() 判断对象是否可调用；None 不可调用，真实 delete 方法可调用
        if callable(delete_method):
            delete_method(orphan, tenant=project_id)  # 从 Weaviate 删除孤儿向量
        checkpoint.drop(orphan)                     # 从 checkpoint 移除（避免下次又算孤儿）
        deleted += 1

    checkpoint.flush()                              # 原子写盘（临时文件 → rename）
    return {"embedded": embedded, "deleted": deleted, "current": len(current_keys)}
