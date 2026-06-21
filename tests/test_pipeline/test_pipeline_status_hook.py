"""集成测试：pipeline 编排里挂的 status 回写 hook。

验证 execute_full_pipeline_table 在段循环中按 seg_id 回写工程 status：
- semantic 段跑完 → partial
- finalize 段返回结果 → ready（带统计富化）
- project_id 为空 → 全程跳过（纯 CLI 无副作用）
- 段抛异常 → failed 后重新抛出

本测试用 monkeypatch 拦截真实 writer / Weaviate 进度查询，
并用轻量 stub scope（只带 hook 读取的 project_id / config_path 两个属性），
避免构造完整 FullPipelineScope（其 __post_init__ 需要真实 ProjectConfig）。
"""
# 导入被测编排模块，用别名 orch 方便 monkeypatch 模块级符号
from src.pipeline import full_pipeline_orchestrator as orch

# pytest：测试框架；这里用它的 raises 断言异常
import pytest


class _StubScope:
    """轻量 scope 替身：hook 只读 project_id / config_path 两个属性。

    用普通类而非 FullPipelineScope，是因为后者的 __post_init__ 会访问
    self.config.knowledge 等真实配置，stub 段 runner 场景下不需要也无法提供。
    """

    def __init__(self, project_id, config_path="dummy.yaml"):
        # 工程 ID：None 时 hook 应完全跳过回写
        self.project_id = project_id
        # config_path：finalize 段查 Weaviate 进度时需要（本测试 monkeypatch 掉）
        self.config_path = config_path


def test_hook_writes_partial_after_semantic_and_ready_after_finalize(monkeypatch):
    """semantic 后写 partial、finalize 后写 ready。"""
    # calls 收集每次 writer 调用的 (project_id, status)，用于断言
    calls = []

    # 拦截真实 DB 写入：换成只追加到 calls 的 lambda
    # sm=session_maker, pid=project_id, status=新状态, **kw=统计富化参数
    monkeypatch.setattr(
        orch,
        "update_project_status_sync",
        lambda sm, pid, status, **kw: calls.append((pid, status)),
    )
    # session maker 工厂也拦截：避免真实建库连接，返回一个占位对象即可
    monkeypatch.setattr(orch, "get_session_maker", lambda: object())
    # finalize 段的状态富化会查 Weaviate，拦截返回假计数（避免触达真实 Weaviate）
    # done==total（1/1=100%）→ finalize 判 ready，下方 ready 断言成立
    monkeypatch.setattr(
        orch,
        "get_interpretation_progress_from_weaviate",
        lambda config_path, *a, **k: {"tech": {"done": 1, "total": 1}},
    )
    # 用 stub 段表替换真实段表：每段 runner 是返回固定值的 lambda
    # finalize 返回非 None dict → 触发 ready 回写并结束循环
    monkeypatch.setattr(
        orch,
        "FULL_PIPELINE_SEGMENT_RUNNERS",
        [
            ("structure", lambda scope: None),
            ("semantic", lambda scope: None),
            ("knowledge", lambda scope: None),
            ("interpretation", lambda scope: None),
            ("finalize", lambda scope: {"ok": True}),
        ],
    )

    # 构造带 project_id 的 stub scope 并执行编排
    scope = _StubScope(project_id="mall-swarm")
    out = orch.execute_full_pipeline_table(scope)

    # finalize 的返回值应原样透出（不破坏现有返回行为）
    assert out == {"ok": True}
    # semantic 后写过 partial
    assert ("mall-swarm", "partial") in calls
    # finalize 后写过 ready
    assert ("mall-swarm", "ready") in calls


def test_finalize_partial_coverage_writes_partial_not_ready(monkeypatch):
    """finalize 时解读覆盖率未满 100% → 写 partial 不写 ready。

    解读可能被门控跳过/部分完成（done<total），此时结构+语义虽完成，
    但解读未满，finalize 应判 partial 而非硬编码 ready，
    与 backfill 的 decide_status（同样判 partial）保持一致。
    """
    # calls 收集每次 writer 调用的 (project_id, status)
    calls = []
    # 拦截 DB 写入：换成只追加到 calls 的 lambda
    monkeypatch.setattr(
        orch,
        "update_project_status_sync",
        lambda sm, pid, status, **kw: calls.append((pid, status)),
    )
    # 拦截 session maker，避免真实建库连接
    monkeypatch.setattr(orch, "get_session_maker", lambda: object())
    # 解读进度 80/100=80% 未满 → finalize 应判 partial
    monkeypatch.setattr(
        orch,
        "get_interpretation_progress_from_weaviate",
        lambda *a, **k: {"tech": {"done": 80, "total": 100}},
    )
    # stub 段表：semantic 先写 partial，finalize 返回非 None 触发状态置位
    monkeypatch.setattr(
        orch,
        "FULL_PIPELINE_SEGMENT_RUNNERS",
        [
            ("semantic", lambda scope: None),
            ("finalize", lambda scope: {"ok": True}),
        ],
    )

    # 构造带 project_id 的 stub scope 并执行编排
    scope = _StubScope(project_id="mall-swarm")
    orch.execute_full_pipeline_table(scope)

    # finalize 80% 未满 → 写 partial
    assert ("mall-swarm", "partial") in calls
    # 未满 100% → 不写 ready
    assert ("mall-swarm", "ready") not in calls


def test_hook_skipped_when_no_project_id(monkeypatch):
    """project_id 为空 → 不发生任何 status 回写。"""
    # 同样拦截 writer，断言它一次都没被调用
    calls = []
    monkeypatch.setattr(
        orch,
        "update_project_status_sync",
        lambda sm, pid, status, **kw: calls.append((pid, status)),
    )
    # get_session_maker 不应被调用（pid 为空时跳过），但仍拦截以防意外触达 DB
    monkeypatch.setattr(orch, "get_session_maker", lambda: object())
    monkeypatch.setattr(
        orch,
        "get_interpretation_progress_from_weaviate",
        lambda config_path, *a, **k: {"tech": {"done": 1, "total": 1}},
    )
    monkeypatch.setattr(
        orch,
        "FULL_PIPELINE_SEGMENT_RUNNERS",
        [
            ("semantic", lambda scope: None),
            ("finalize", lambda scope: {"ok": True}),
        ],
    )

    # project_id=None → hook 应全程跳过
    scope = _StubScope(project_id=None)
    out = orch.execute_full_pipeline_table(scope)

    # 返回值仍正常透出
    assert out == {"ok": True}
    # 没有任何 status 回写
    assert calls == []


def test_hook_writes_failed_on_exception(monkeypatch):
    """段抛异常 → 写 failed 后重新抛出（不吞异常）。"""
    calls = []
    monkeypatch.setattr(
        orch,
        "update_project_status_sync",
        lambda sm, pid, status, **kw: calls.append((pid, status)),
    )
    monkeypatch.setattr(orch, "get_session_maker", lambda: object())
    monkeypatch.setattr(
        orch,
        "get_interpretation_progress_from_weaviate",
        lambda config_path, *a, **k: {"tech": {"done": 1, "total": 1}},
    )

    # boom：一个会抛 RuntimeError 的段 runner
    def boom(scope):
        raise RuntimeError("段炸了")

    monkeypatch.setattr(
        orch,
        "FULL_PIPELINE_SEGMENT_RUNNERS",
        [
            ("structure", boom),
        ],
    )

    scope = _StubScope(project_id="mall-swarm")
    # pytest.raises：断言代码块抛出指定异常（hook 写 failed 后必须重新抛出）
    with pytest.raises(RuntimeError):
        orch.execute_full_pipeline_table(scope)

    # 异常前写过 failed
    assert ("mall-swarm", "failed") in calls


def test_semantic_early_return_writes_partial_not_ready(monkeypatch):
    """until='semantic' 路径：semantic 段返回非 None → 写 partial 但不写 ready。

    验证编排循环在 semantic 段提前结束时：
    - partial 已由 seg_id=='semantic' 守卫写出（骨架就绪）
    - 循环因 runner 返回非 None 而 break，finalize 段未执行 → 不写 ready
    """
    # calls 收集每次 writer 调用的 (project_id, status)
    calls = []

    # 拦截 DB 写入：换成只追加到 calls 的 lambda
    monkeypatch.setattr(
        orch,
        "update_project_status_sync",
        lambda sm, pid, status, **kw: calls.append((pid, status)),
    )
    # 拦截 session maker，避免真实建库连接
    monkeypatch.setattr(orch, "get_session_maker", lambda: object())
    # 拦截 Weaviate 进度查询（finalize 路径才用到，这里不应到达；仍拦截避免意外触达）
    monkeypatch.setattr(
        orch,
        "get_interpretation_progress_from_weaviate",
        lambda *a, **k: {"tech": {"done": 1, "total": 1}},
    )
    # stub 段表：semantic 返回非 None，模拟 until="semantic" 提前退出
    # finalize lambda 不应被调用（loop 在 semantic 处 break）
    monkeypatch.setattr(
        orch,
        "FULL_PIPELINE_SEGMENT_RUNNERS",
        [
            ("structure", lambda scope: None),
            ("semantic", lambda scope: {"stage": "semantic"}),  # 提前返回 → loop break
            ("finalize", lambda scope: {"ok": True}),           # 不应到达
        ],
    )

    # 构造带 project_id 的 stub scope 并执行编排
    scope = _StubScope(project_id="mall-swarm")
    orch.execute_full_pipeline_table(scope)

    # semantic 段守卫已写 partial（骨架就绪，状态有效）
    assert ("mall-swarm", "partial") in calls
    # finalize 未执行 → 不应写 ready
    assert ("mall-swarm", "ready") not in calls
