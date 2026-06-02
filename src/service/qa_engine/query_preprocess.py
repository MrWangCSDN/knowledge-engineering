"""召回 query 预处理：剥离"渲染指令"与"口水填充词"，提纯代码语义后再做向量召回。

设计：Obsidian [[召回链路缺陷诊断与修复方案]] 快赢 A。

背景（探针实测）：用户问"下单流程是怎么实现的，用流程图展示"时，整条问题被原样 embedding，
"用流程图展示 / 是怎么实现的"等与"找哪段代码"无关的词稀释了"下单"的代码语义，导致真正的入口
方法 generateOrder 掉出召回 top-5。提纯后 "下单流程" 的代码语义更突出，入口更易被召回。

关键约束：**仅用于召回时的向量化 query**；展示给用户、喂给 LLM 作答、召回门控判定都仍用
用户的**原始问题**（保留"怎么实现"等意图，让答案能正面回应）。
"""
from __future__ import annotations

# re 是 Python 标准库的正则模块；re.sub(pattern, repl, s) 把 s 中所有匹配 pattern 的片段替换成 repl
import re


# ── 渲染 / 格式指令：用户想要的"输出形态"（画成什么图），与"找哪段代码"无关，是召回噪声 ──
# 每个元素是一个正则；`?` 表示前一个字符/分组可有可无；`(a|b)` 表示择一匹配
_RENDER_PATTERNS: list[str] = [
    r"用?流程图(来)?(展示|表示|画出来?|画一下)?",   # "用流程图展示" / "流程图" / "流程图画出来"
    r"用?时序图(来)?(展示|表示)?",                  # "用时序图展示"
    r"用?架构图(来)?(展示|表示)?",                  # "架构图展示"
    r"用?类图",                                     # "类图"
    r"用\s*(UML|PlantUML|Mermaid|ReactFlow)\s*图?(来)?(展示|画)?",  # "用 UML 图展示"
    r"画(一下|一张|个|出)?(流程图|时序图|架构图|类图|图)",  # "画个流程图" / "画图"
    r"可视化(地)?(展示|表示)?",                      # "可视化展示"
    r"图形化(地)?(展示)?",                          # "图形化"
    r"展示(一下|出来)?",                            # 通用"展示一下"（代码问答里几乎只表渲染意图）
    r"给我(看一?看|画|展示)",                        # "给我看看" / "给我画"
]

# ── 口水 / 礼貌填充：去掉不影响"找哪段代码"的语义 ──
_FILLER_PATTERNS: list[str] = [
    r"帮我", r"请", r"麻烦",                         # 礼貌前缀
    r"我想(知道|了解|看看?)", r"想知道",              # "我想知道"
    r"是怎么(实现|做)的", r"怎么(实现|做)的", r"是如何(实现|工作)的",  # "是怎么实现的"（每个代码问题都隐含，区分度为 0）
    r"一下", r"呢", r"吧", r"啊", r"嘛",             # 语气助词
]

# 预编译所有正则（模块加载时编译一次，避免每次调用重复编译，提升性能）
# re.compile 返回 Pattern 对象；列表推导式 [expr for x in seq] 一次性编译全部
_COMPILED: list[re.Pattern[str]] = [re.compile(p) for p in (_RENDER_PATTERNS + _FILLER_PATTERNS)]

# 标点 / 空白噪声：中英文标点 + 连续空白统一折叠成单个空格
_PUNCT_WS = re.compile(r"[，。、！？；：,.!?;:\s]+")

# 提纯后若短于此长度，判定为"原问题几乎全是指令/口水"，回退原文以免丢失全部召回信号
_MIN_CLEANED_LEN = 2

# 残留词集合：多 pattern 顺序替换后可能漏下孤立的渲染残词（如"流程图展示"被吃掉只剩"画个"）。
# 若提纯结果整体就是这些残词之一 → 说明原问题几乎没有代码语义 → 回退原文。
# frozenset 是不可变集合（建后不能改），`in` 判定 O(1)，比 list 更适合做查表
_RESIDUE_ONLY: frozenset[str] = frozenset(
    {"画", "画个", "画出", "画出来", "画一下", "画一张", "绘制", "展示", "图", "看", "看看"}
)


def clean_recall_query(text: str) -> str:
    """提纯召回 query：剥离渲染指令 + 口水填充词。

    Args:
        text: 用户原始问题
    Returns:
        提纯后的 query；若清理后为空 / 过短，则回退原始问题（去首尾空白）

    示例：
        "下单流程是怎么实现的，用流程图展示" -> "下单流程"
        "generateOrder 的调用链路"           -> "generateOrder 的调用链路"（无渲染/口水，基本不变）
    """
    # 空 / 纯空白输入：直接返回（or "" 保证返回值一定是 str，不会是 None）
    if not text or not text.strip():
        return text or ""

    cleaned = text
    # 逐个正则把匹配片段替换成空格（用空格而非空串，避免把两侧词粘连成新词）
    for pat in _COMPILED:
        cleaned = pat.sub(" ", cleaned)

    # 折叠标点与多余空白为单空格，再去首尾空白
    cleaned = _PUNCT_WS.sub(" ", cleaned).strip()

    # 清理过度（几乎全被剥光，或只剩渲染残词）→ 回退原文，宁可带点噪声也别零信号
    if len(cleaned) < _MIN_CLEANED_LEN or cleaned in _RESIDUE_ONLY:
        return text.strip()

    return cleaned
