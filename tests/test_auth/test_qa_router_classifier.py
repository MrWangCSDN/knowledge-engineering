"""验证 SkillRouter.classify：把用户问题映射到 1 个 skill_id。

Skill 列表（v1.1）：
  - business      业务规则 / 约束 / 校验
  - dependency    调用 / 依赖 / 谁调了谁
  - data-flow     数据流 / 表 / 持久化
  - architecture  整体架构 / 怎么实现 / 是什么（默认）

TDD 策略：
  - 关键词命中 → 直接返回该 skill
  - 没命中 → 返回 architecture（最通用，承担兜底）
  - 复杂场景（多 skill 候选）→ v1.5 再上 LLM fallback
"""
# pytest：Python 主流测试框架；用 `pytest tests/` 跑
import pytest
# AsyncMock：标准库 unittest.mock 的异步版；用来模拟 awaitable 接口（这里模拟 LLM provider）
from unittest.mock import AsyncMock

# 待实现的 SkillRouter；现在 import 会失败 → 测试自然 RED
# 这种"先 import 不存在的类"是 TDD 标准做法（让 import error 当第一个 fail 信号）
from src.service.qa_engine.router import SkillRouter, RouteDecision


# ───────── RED 1: dependency 关键词 ─────────

# `@pytest` 装饰器把下面的函数标记成测试用例；命名必须 test_ 开头
def test_classify_dependency_when_question_asks_who_calls() -> None:
    """问题含『调用』、『依赖』等关键词 → 归为 dependency skill。"""
    # 实例化 router；构造函数 v1 不需要任何参数（关键词词典写在类里）
    router = SkillRouter()

    # 三个典型的 dependency 问题；都应该返回 "dependency"
    # 用 `==` 不用 `is`，因为字符串比的是值不是身份
    assert router.classify("OwnerController 调用了哪些方法？") == "dependency"
    assert router.classify("谁调用了 findOwners？") == "dependency"
    assert router.classify("Pet 类的依赖关系是什么？") == "dependency"


# ───────── RED 2: data-flow 关键词 ─────────


def test_classify_data_flow_when_question_asks_about_tables() -> None:
    """问题含『表』、『数据流』、『写入』等关键词 → 归为 data-flow skill。"""
    router = SkillRouter()
    # 用户问『哪里写表』『数据怎么流的』时，重点不是调用链，而是 db_ops
    assert router.classify("Owner 写到哪些表？") == "data-flow"
    assert router.classify("数据是怎么流的？") == "data-flow"
    assert router.classify("查询接口涉及哪些数据库表？") == "data-flow"


# ───────── RED 3: business 关键词 ─────────


def test_classify_business_when_question_asks_about_rules() -> None:
    """问题含『业务规则』、『校验』、『约束』等关键词 → 归为 business skill。"""
    router = SkillRouter()
    # 用户关心规则 / 限制 / 校验时，应该聚焦在 rules 段
    assert router.classify("注册 Owner 有什么业务规则？") == "business"
    assert router.classify("Pet 类型有哪些约束？") == "business"
    assert router.classify("提交表单时做了哪些校验？") == "business"


# ───────── RED 4: 默认兜底到 architecture ─────────


# 注：这条测试在落地时立即 pass —— 它属于"契约 / 回归测试"性质
# （RED 1-3 已经隐式确立了 architecture 是默认值）。保留它是为了：
#   - 锁住"默认必须是 architecture，不能是 None 或抛错"这个 API 契约
#   - 防止有人为加新 skill 把默认改成别的导致回归
# 不算严格意义上的 TDD-driven 新代码；坦白承认。
def test_classify_defaults_to_architecture_for_ambiguous_questions() -> None:
    """问题不含任何已知关键词 → 兜底到 architecture（最通用的"是什么 / 怎么实现"视角）。

    设计上 architecture 同时承担：
      - 已知意图（"OwnerController 怎么实现"）
      - 兜底（看不出意图的笼统问题）
    这样路由器不会"识别失败"。
    """
    router = SkillRouter()
    # 显式 architecture 类问题（含"怎么实现"语义）
    assert router.classify("OwnerController 怎么实现的？") == "architecture"
    # 完全笼统、问什么也不像
    assert router.classify("这是什么？") == "architecture"
    # 业务名词但没明确意图
    assert router.classify("Owner") == "architecture"


# ───────── RED 5: 升级返回值为 RouteDecision（含可解释字段）─────────


def test_route_returns_decision_with_matched_keywords() -> None:
    """`route(question)` 返回 RouteDecision，含 skill_id + matched_keywords。

    设计动机：
      - SSE 第一个事件可以告诉前端"这题被识别成 xxx 类，匹配到关键词 [...]"
      - 调试时知道命中规则，方便迭代关键词词典
      - 用户能在 UI 上看到"切换到 xxx 视角"，建立信任感
    """
    router = SkillRouter()

    # 命中 dependency 关键词"调用"
    d = router.route("OwnerController 调用了哪些方法？")
    # `isinstance` 是 Python 内置类型检查；新手要知道这是动态语言里的"类型自省"
    assert isinstance(d, RouteDecision)
    assert d.skill_id == "dependency"
    assert "调用" in d.matched_keywords

    # 命中 business 关键词"业务规则"
    d2 = router.route("注册有什么业务规则？")
    assert d2.skill_id == "business"
    assert "业务规则" in d2.matched_keywords

    # 默认兜底：matched_keywords 是空列表（不是 None）
    d3 = router.route("Owner")
    assert d3.skill_id == "architecture"
    # `== []` 显式断言空列表；`is None` 会失败（因为返回的是 list 不是 None）
    assert d3.matched_keywords == []


def test_classify_remains_str_shortcut() -> None:
    """旧 API `classify` 仍然返回 str（向后兼容）。

    `classify` 是 `route(...).skill_id` 的便捷壳，保留它让 router 用户两种风格都能用。
    """
    router = SkillRouter()
    assert router.classify("Owner 调用了什么？") == "dependency"


# ───────── RED 6: LLM fallback（关键词不命中 → 异步调 LLM 兜底）─────────


# 装饰器组合：先 `@pytest.mark.asyncio` 把测试声明为协程，pytest-asyncio 才会跑它
@pytest.mark.asyncio
async def test_route_async_falls_back_to_llm_when_no_keyword_match() -> None:
    """当关键词都不命中、问题又比较具体（不属于"Owner"这种笼统名词）时，
    异步调 LLM 让它在 4 个 skill 里选一个，避免硬兜底成 architecture 漏判。

    设计原则：
      - 同步 route() / classify() 维持纯关键词，零成本
      - 异步 route_async() 才会触发 LLM 调用（成本 + 延迟由调用方决定要不要付）
      - LLM provider 注入式，便于单测 mock（不打真模型）
    """
    # mock 一个 LLM provider：它的 .complete(system=, user=) 是 async 函数，返回 'dependency'
    # AsyncMock 自动处理 await；调用 mock.complete(...) 返回的是 awaitable
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value="dependency")

    # 把 mock 通过构造函数注入；router 自己不去 import DashScopeProvider
    router = SkillRouter(llm_provider=llm)

    # 一个不命中任何关键词的具体问题（"实现细节"不在 dependency/data-flow/business 关键词里）
    decision = await router.route_async("这个接口的实现细节是怎样的？")

    # LLM 给的是 'dependency'，router 接受
    assert decision.skill_id == "dependency"
    # 命中来源是 LLM，不是关键词，matched_keywords 应该是空 + 有个标识
    assert decision.matched_keywords == []
    assert decision.source == "llm"
    # 验证 LLM 被调用了一次（防止"不小心走了关键词路径"）
    assert llm.complete.await_count == 1


@pytest.mark.asyncio
async def test_route_async_keeps_keyword_decision_when_keyword_hits() -> None:
    """关键词命中时，route_async 应该直接返回，不调 LLM（省成本）。"""
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value="business")  # 即便 LLM 想说 business 也不应被调

    router = SkillRouter(llm_provider=llm)
    decision = await router.route_async("OwnerController 调用了什么？")

    # 走关键词路径
    assert decision.skill_id == "dependency"
    assert decision.source == "keyword"
    # 关键：LLM 没被调用
    assert llm.complete.await_count == 0


@pytest.mark.asyncio
async def test_route_async_falls_back_to_architecture_when_llm_returns_garbage() -> None:
    """LLM 返回不在 4 个 skill 范围内 → 兜底 architecture，不抛错。"""
    llm = AsyncMock()
    # LLM 偶尔会返回胡说八道的字符串；router 要稳健，不能让前端 5xx
    llm.complete = AsyncMock(return_value="🤷 我不知道选哪个")

    router = SkillRouter(llm_provider=llm)
    decision = await router.route_async("一个无法分类的问题")

    assert decision.skill_id == "architecture"
    assert decision.source == "llm-fallback"


@pytest.mark.asyncio
async def test_route_async_falls_back_to_architecture_when_llm_raises() -> None:
    """LLM 网络错 / 超时 / 限流 → 兜底 architecture，不抛错。"""
    llm = AsyncMock()
    # `side_effect` 是 mock 的"调用时引发异常"机制
    llm.complete = AsyncMock(side_effect=RuntimeError("LLM 超时"))

    router = SkillRouter(llm_provider=llm)
    decision = await router.route_async("一个无法分类的问题")

    assert decision.skill_id == "architecture"
    assert decision.source == "llm-error"
