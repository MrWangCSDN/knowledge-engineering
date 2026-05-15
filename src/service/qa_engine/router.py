"""问题路由器：把用户的自然语言问题映射到 1 个 skill_id。

设计文档：[[首页设计]] v1.1 §11. 决策日志「路由方案 C」

策略（v1.1）：
  - 同步快路径：纯关键词匹配，命中即返回；都不命中 → architecture 兜底
  - 异步慢路径：route_async() —— 关键词不命中时调 LLM 让它在 4 个 skill 里选一个
    - LLM 返回值不合法 → 兜底 architecture（source='llm-fallback'）
    - LLM 调用异常 → 兜底 architecture（source='llm-error'）
  - LLM provider 注入式：单测能 mock，生产用 DashScopeProvider

API:
  router.classify(q)       -> str          便捷接口，纯关键词
  router.route(q)          -> RouteDecision  完整决策（关键词路径）
  await router.route_async(q) -> RouteDecision  关键词不命中时兜底到 LLM
"""
from __future__ import annotations

# dataclasses：标准库；@dataclass 自动生成 __init__/__repr__/__eq__ 等样板代码
from dataclasses import dataclass, field
# typing.Protocol：结构化子类型（duck typing 的类型注解版本）
# Optional[X] 是 X | None 的 3.10 之前写法；3.10+ 推荐用 `X | None`
from typing import Any, Optional, Protocol


# `Protocol` 类似 Java/TS 的 interface，但运行时不强制 —— 只要 mock 长得像就行
# 这样 SkillRouter 不需要 import DashScopeProvider，从而：
#   - 单测不打真模型（注 AsyncMock 进去）
#   - 模块边界清晰：router 只依赖"LLM 能 await complete()"这件事
class LLMProviderProto(Protocol):
    """LLM provider 应有的最小接口（与 synthesizer.LLMProviderProto 形状一致）。"""

    # `async def` + 任意 **kwargs：让 mock 也能传 max_tokens 之类
    async def complete(self, *, system: str, user: str, **kwargs: Any) -> str: ...


# 5 个 skill 的合法值；用 frozenset 不允许后续修改（防御性编程）
# 放模块级而不是类级 —— 它属于"全局常量"语义
_VALID_SKILL_IDS = frozenset({"business", "dependency", "data-flow", "architecture", "chit-chat"})


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """路由决策结果。

    Attributes:
        skill_id: 'business' | 'dependency' | 'data-flow' | 'architecture'
        matched_keywords: 关键词命中时的命中词列表；LLM 路径下为空 []
        source: 决策来源 —— 'keyword' / 'llm' / 'llm-fallback' / 'llm-error'
                便于追踪 / UI 解释 / 调优词典
    """
    skill_id: str
    matched_keywords: list[str] = field(default_factory=list)
    # `source` 默认是 'keyword' —— 同步 route() 走的就是这条路
    source: str = "keyword"


# `_SYS_PROMPT_LLM_ROUTE` 是模块级私有常量；用于 LLM fallback 时让模型只选 1 个 skill
# 用 textwrap.dedent 风格保持缩进干净（这里直接顶头写）
_LLM_ROUTE_SYSTEM = """你是问题分类器。把用户问题归到下面 5 个 skill 中的 1 个，**只输出 skill 名**，不要任何其他文字。

可选 skill：
  - business      业务规则 / 约束 / 校验
  - dependency    调用 / 依赖 / 谁调了谁
  - data-flow     数据流 / 表 / 持久化
  - chit-chat     闲聊 / 社交问候 / 产品问询（如「你好」「你是谁」「能做什么」「KE 是什么」）
  - architecture  整体架构 / 是什么 / 怎么实现 （兜底类）

输出示例（**严格遵守，只输出这 5 个字符串之一**）：
dependency
"""


class SkillRouter:
    """关键词路由器，可选 LLM fallback。无状态，可放进 app.state 全局共享。"""

    def __init__(self, llm_provider: Optional[LLMProviderProto] = None) -> None:
        """
        :param llm_provider: 可选 LLM 实例；只有调 `route_async` 才会用到。
                             同步 `route`/`classify` 路径不依赖 LLM。
        """
        # 关键词词典：值是字符串列表，遇到任意一个就归到 key 对应的 skill
        # 优先级 = dict 插入顺序（Python 3.7+ 保证）：dependency → data-flow → business → chit-chat
        # 一句话同时命中多个 skill 时，越靠前的赢
        # 设计直觉：dependency 最具体（"调用"），chit-chat 最通用（"你好"），所以前者优先
        self._keywords: dict[str, list[str]] = {
            "dependency": ["调用", "依赖", "调了"],
            "data-flow": ["写表", "写到哪些表", "数据流", "怎么流", "数据库表"],
            "business": ["业务规则", "约束", "限制", "校验"],
            # v1.2 新增：闲聊 / 社交问候 / 产品问询（放最后，让业务词先命中）
            "chit-chat": [
                "你好", "您好", "嗨", "hi", "hello", "hey",
                "在吗", "在么", "在不在",
                "早上好", "晚上好", "下午好", "早安", "晚安",
                "谢谢", "感谢", "辛苦", "thank",
                "再见", "拜拜", "bye", "goodbye",
                "抱歉", "对不起", "sorry",
                "你是谁", "你叫什么", "你能做什么", "你是干嘛的",
                "KE 是什么", "怎么用", "有什么用",
            ],
        }
        # 注入式 LLM provider；None 表示"没有 LLM 兜底，硬走默认"
        self._llm = llm_provider

    # ─── 同步快路径 ────────────────────────────────────────────────────────

    def route(self, question: str) -> RouteDecision:
        """纯关键词路由。返回 RouteDecision，永不抛错。

        v1.2.1：兜底从 architecture 改为 chit-chat。
        理由：业务问题通常含明显关键词（调用 / 数据流 / 业务规则）；
        关键词不命中说明大概率是社交语 / 模糊问询 / 用户措辞不精准 —
        走 chit-chat 让 LLM 友好引导回业务能力，比强行走 architecture
        KG 查询（很可能查空 + 报错）体验更好。

        :param question: 用户问题原文
        :return: RouteDecision，source='keyword' 或兜底 chit-chat（matched_keywords=[]）
        """
        for skill_id, kws in self._keywords.items():
            hits = [kw for kw in kws if kw in question]
            if hits:
                return RouteDecision(skill_id=skill_id, matched_keywords=hits, source="keyword")
        # v1.2.1: 兜底 chit-chat（之前是 architecture）
        # source 仍然是 'keyword'（默认值），因为没用 LLM
        return RouteDecision(skill_id="chit-chat")

    def classify(self, question: str) -> str:
        """便捷壳：只要 skill_id 字符串时用这个。等价于 `self.route(question).skill_id`。"""
        return self.route(question).skill_id

    # ─── 异步慢路径（带 LLM fallback）─────────────────────────────────────

    async def route_async(self, question: str) -> RouteDecision:
        """关键词命中 → 直接返回（同 route）；
        关键词不命中 → 异步调 LLM 让它在 5 个 skill 里选 1 个。

        v1.2.1：异常 / 不合法返回都兜底到 chit-chat（之前是 architecture）。
        永不抛错（chat 链路不能 5xx）。
        """
        # 1. 先走关键词；命中就直接返回，省 LLM 钱
        keyword_decision = self.route(question)
        # `decision.matched_keywords` 非空 = 真的命中关键词；空 = 走的兜底分支
        if keyword_decision.matched_keywords:
            return keyword_decision

        # 2. 关键词没命中 + 没 LLM provider → 保持原兜底
        if self._llm is None:
            return keyword_decision

        # 3. 调 LLM
        # try/except 包住：网络错 / 限流 / 超时 / 解析错都不能让 chat 挂
        try:
            # LLM 返回的应该是单行 skill 名；用 strip() 去掉首尾空白
            raw = await self._llm.complete(system=_LLM_ROUTE_SYSTEM, user=question)
        except Exception:
            # 任何 LLM 异常都兜底；不暴露错误细节给前端
            # v1.2.1: 兜底 chit-chat（之前是 architecture）
            return RouteDecision(skill_id="chit-chat", source="llm-error")

        # 4. 解析 + 兜底
        # 取 strip 后第一个 token，避免 LLM 啰嗦多说一句
        candidate = (raw or "").strip().split()[0] if raw else ""
        # 在合法 skill 集合里 → 直接采用
        if candidate in _VALID_SKILL_IDS:
            return RouteDecision(skill_id=candidate, source="llm")
        # 不合法 → 兜底 chit-chat（之前是 architecture），但 source 标记为
        # 'llm-fallback' 便于排查"LLM 又胡说了"
        return RouteDecision(skill_id="chit-chat", source="llm-fallback")
