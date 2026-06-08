"""symbol_resolver.py — IDE 化光标→实体三级解析编排（纯函数，便于单测）。

设计 [[代码查看器-IDE化导航-设计]] §4.1：
  1. 位置 → graph.resolve_at_position（边命中：calls/references/instantiates）
  2. 落空 + token → graph.find_by_name（nodes_fts 全文索引）
  3. 命中接口方法 → graph.resolve_impl（implements 边）改写为 impl entity_id
出：{entity_id, has_source, kind, signature?, summary?} 或 None（全落空）。

fail-soft：任意 graph 原语抛错都视为该级未命中，继续走下一级；
最终 resolve_first / interp_store 抛错也不上抛，元数据按现有信息尽量填。
"""
# 标准库 logging：降级时打 warning，留可观测痕迹（同 adapter 风格）
import logging
# os.path.join / os.path.exists：拼绝对路径 + 判文件存在（驱动 has_source）
import os
# re 模块：用正则匹配中英文句末标点，截 hover summary 首句
import re
# typing.Optional：Optional[X] = X | None，签名与 db.py 同款
from typing import Optional

# 模块级 logger：以模块全路径命名，便于按模块过滤日志
_LOG = logging.getLogger(__name__)

# 句末标点正则：中文 。！？ + 英文 .!? — 用于截 hover 解读首句
# re.compile 提前编译，反复调用时省 parse 开销
_SENT_END = re.compile(r"[。.!?！？]")


def _first_sentence(text: str) -> str:
    """从 2b 解读全文截首句（句末标点前的子串）；无终结标点 → 兜底取前 80 字符。

    Args:
        text: 解读全文（可能多句）
    Returns:
        首句字符串（已 strip）；空输入返 ""
    """
    if not text:
        return ""
    # search：返回首个匹配的 Match 对象；无匹配返 None
    m = _SENT_END.search(text)
    if m:
        # m.start()：句末标点所在下标；切片到该下标得到"首句不含标点"
        return text[: m.start()].strip()
    # 无句末标点（如英文方法说明无句号）→ 截前 80 字防 hover 过长
    return text[:80].strip()


def resolve_symbol_at(
    graph,                                  # GraphProto-like：见上方原语清单
    interp_store,                           # composite / interpretation store；可为 None
    *,                                      # 之后参数均 keyword-only，防误传位置
    file_path: Optional[str],
    line: Optional[int],
    col: Optional[int],
    token: Optional[str],
    context_entity_id: Optional[str],       # 预留：当前查看实体 ID（本版未强用，留作精度提升）
    repo_local_path: Optional[str] = None,  # 工程源码根；拼绝对路径判 has_source
    want_doc: bool = False,                 # True 时附 signature + summary（hover 路径）
) -> Optional[dict]:
    """光标位置 / token → 实体三级解析。

    Args:
        graph: 适配器对象，需具备 resolve_at_position / find_by_name / resolve_impl / resolve_first
        interp_store: 解读库（含 get_by_entity）；None 时 summary 取 None
        file_path: 光标所在文件相对路径；None / 空串 → 跳过位置级
        line, col: 光标位置（1-indexed，UTF-16 列约定与 Monaco 一致）
        token: 光标处词；位置级落空时按名回退（用 FTS）
        context_entity_id: 调用方当前查看实体 id；本版未强用，留作位置匹配精度提升点
        repo_local_path: 工程源码根目录绝对路径；None 时 has_source 一律 False
        want_doc: True 启用 hover：附 signature（来自节点）+ summary（解读首句）
    Returns:
        三级中任一命中：{entity_id, has_source, kind} (+ signature, summary if want_doc)
        全落空：None（前端据此显示"暂无源码" / 静默）
    """
    # 解析结果占位；三级流程逐级填充
    entity_id: Optional[str] = None

    # ── 第一级：位置 → 边命中 ──────────────────────────────────────────────────
    # 三参数齐全才走位置级；任一为 None 直接跳过（None 不是 0，col=0 视为有效）
    if file_path and line is not None and col is not None:
        try:
            entity_id = graph.resolve_at_position(file_path, line, col)
        except Exception as exc:
            # fail-soft：任何异常（sqlite / 调用错 / mock 抛错）视为该级未命中
            # 留 warning 让运维知道，但不阻断流程
            _LOG.warning(
                "resolve_at_position 抛错（视为未命中）file=%s line=%s col=%s: %s",
                file_path, line, col, exc,
            )
            entity_id = None

    # ── 第二级：名字 FTS 回退 ──────────────────────────────────────────────────
    # entity_id 仍空且有 token → 走名字索引；候选限 5 条
    if not entity_id and token:
        try:
            # find_by_name 返回 durable_key 列表（adapter 已做 CamelCase 优先 class/interface）
            candidates = graph.find_by_name(token, limit=5) or []
        except Exception as exc:
            _LOG.warning("find_by_name 抛错（视为空）token=%s: %s", token, exc)
            candidates = []
        if candidates:
            entity_id = candidates[0]               # 取首个候选

    # 三级全落空 → None（前端据此降级；不显示任何 tooltip / 不跳转）
    if not entity_id:
        return None

    # ── 第三级：接口 → 实现 改写 ───────────────────────────────────────────────
    # resolve_impl 已自带"非接口返 None"的判断；这里直接接收结果
    try:
        impl_id = graph.resolve_impl(entity_id)
    except Exception as exc:
        _LOG.warning("resolve_impl 抛错（视为不改写）entity_id=%s: %s", entity_id, exc)
        impl_id = None
    if impl_id:
        # 改写 entity_id：后续元数据 / 解读都查 impl，对前端透明
        entity_id = impl_id

    # ── 元数据：node + has_source ─────────────────────────────────────────────
    # resolve_first 拿到代表性 CgNode（重载取首个）；抛错降级 None
    try:
        node = graph.resolve_first(entity_id)
    except Exception as exc:
        _LOG.warning("resolve_first 抛错 entity_id=%s: %s", entity_id, exc)
        node = None

    # kind 取节点 kind；node 缺失（极端：图原语命中但 resolve_first 又拿不到）兜底 'unknown'
    kind = node.kind if node else "unknown"

    # has_source 判定：node 拿到 + 有 file_path + 拼绝对路径后磁盘文件存在
    # 三条件任一不满足 → False（前端显示"暂无源码"）
    has_source = False
    if node and node.file_path and repo_local_path:
        # os.path.join：跨平台拼接；POSIX 与 Windows 分隔符差异由它处理
        abs_path = os.path.join(repo_local_path, node.file_path)
        # exists 只判存在性；不读文件，避免大文件拖慢 hover/click
        has_source = os.path.exists(abs_path)

    # ── 组装返回 dict ─────────────────────────────────────────────────────────
    out: dict = {
        "entity_id": entity_id,
        "has_source": has_source,
        "kind": kind,
    }

    # hover 路径才附 signature/summary，避免 click 路径多走一次解读库查询
    if want_doc:
        # signature 取节点 signature；节点缺失或无签名（如类）返 None
        out["signature"] = node.signature if node else None
        # summary 取 2b 解读首句；interp_store / get_by_entity / 解读文本 任一缺失 → None
        summary: Optional[str] = None
        if interp_store is not None:
            try:
                # composite.get_by_entity 返 dict（含 interpretation_text）或 None
                hit = interp_store.get_by_entity(entity_id)
            except Exception as exc:
                _LOG.warning(
                    "interp_store.get_by_entity 抛错 entity_id=%s: %s", entity_id, exc
                )
                hit = None
            if hit:
                # 解读字段名与 weaviate_interpretation_store 对齐；缺字段时 .get → ''
                text = hit.get("interpretation_text") or ""
                # _first_sentence 返 ""（无内容）→ summary 仍 None；非空才回填
                summary = _first_sentence(text) or None
        out["summary"] = summary

    return out
