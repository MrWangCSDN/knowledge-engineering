"""验证 CLI 与 run_pipeline 对 --repo-path / --project-id 的透传（多工程索引 Phase1-T5）。

场景：SCM worker 索引克隆下来的仓库时，需要把"仓库路径"和"租户 project_id"
一路透传到 pipeline，否则会永远索引 config 里写死的 mall-swarm。
"""
# 导入 sys：用于临时替换 sys.argv，模拟命令行参数
import sys

# 导入 pytest：测试框架（这里用到 monkeypatch fixture）
import pytest


def test_cli_passes_repo_path_and_project_id_to_run_pipeline(monkeypatch):
    """
    cli.main 解析 --repo-path / --project-id 后，应原样透传给 run_pipeline。

    做法：用 monkeypatch 把 run_pipeline 替换成一个"间谍"函数，
    捕获它收到的关键字参数，再断言 repo_path / project_id 正确。
    """
    # captured 是一个普通 dict，用闭包在 fake_run_pipeline 里写入捕获到的 kwargs
    captured = {}

    # 定义假的 run_pipeline，签名用 **kwargs 兜住所有关键字参数
    # cli.py 是 `from src.pipeline.run import run_pipeline`，再以关键字方式调用，
    # 所以这里只需接受 **kwargs 即可捕获 repo_path / project_id
    def fake_run_pipeline(**kwargs):
        # 把收到的关键字参数整体存下来供断言
        captured.update(kwargs)
        # run_pipeline 真实返回的是 dict（含 stage 等），这里返回最小可用结构
        return {"stage": "knowledge"}

    # cli.main 内部是 `from src.pipeline.run import run_pipeline` 局部导入，
    # 因此必须打补丁到源模块 src.pipeline.run.run_pipeline 上（导入时会取到替换后的）
    monkeypatch.setattr("src.pipeline.run.run_pipeline", fake_run_pipeline)

    # 构造命令行参数：第 0 个是程序名（占位），其余为真实 flag
    fake_argv = [
        "cli",
        "--repo-path", "/x",
        "--project-id", "px",
        "--output-dir", "/o",
        "--with-interpretation",
    ]
    # monkeypatch.setattr 临时替换 sys.argv，测试结束自动还原
    monkeypatch.setattr(sys, "argv", fake_argv)

    # 局部导入 main，确保拿到的是当前模块状态（避免顶层导入缓存问题）
    from src.pipeline.cli import main

    # 调用入口；它内部会调用被打补丁的 fake_run_pipeline
    main()

    # 断言：repo_path / project_id 被正确透传
    assert captured.get("repo_path") == "/x"
    assert captured.get("project_id") == "px"


def test_run_pipeline_overrides_config_repo_path(monkeypatch):
    """
    run_pipeline(repo_path=...) 应覆盖 config.repo.path，
    且 run_pipeline(project_id=...) 应让 effective_project_id 等于该值。

    做法：mock 掉 load_config（返回可控的 ProjectConfig）和
    execute_full_pipeline_table（捕获最终构造的 scope），
    然后断言 scope.config.repo.path / scope.project_id。
    """
    # 导入真实的 ProjectConfig / RepoConfig，构造一个"原始 config"
    from src.config.models import ProjectConfig, RepoConfig

    # 原始 config.repo.path 指向 mall-swarm（模拟写死的默认值）
    base_config = ProjectConfig(repo=RepoConfig(path="/opt/mall-swarm", project_id="mall-swarm"))

    # captured_scope 用来接住 execute_full_pipeline_table 收到的 scope 对象
    captured_scope = {}

    # 假的 load_config：忽略路径，直接返回我们构造的 base_config
    def fake_load_config(config_path):
        return base_config

    # 假的 execute_full_pipeline_table：捕获 scope 后返回最小 dict
    def fake_execute(scope):
        captured_scope["scope"] = scope
        return {"stage": "knowledge"}

    # run.py 里 `from src.pipeline.config_bootstrap import ... load_config`，
    # 已绑定到 src.pipeline.run.load_config，故打补丁到该名字上
    monkeypatch.setattr("src.pipeline.run.load_config", fake_load_config)
    # execute_full_pipeline_table 是函数内 `from ... import` 局部导入，
    # 必须打补丁到源模块 full_pipeline_orchestrator 上
    monkeypatch.setattr(
        "src.pipeline.full_pipeline_orchestrator.execute_full_pipeline_table",
        fake_execute,
    )

    # 局部导入 run_pipeline，确保用到打过补丁的依赖
    from src.pipeline.run import run_pipeline

    # 传入 repo_path 与 project_id；config_path 任意（已被 fake_load_config 忽略）
    run_pipeline(
        config_path="config/project.yaml",
        repo_path="/some/clone",
        project_id="px",
    )

    # 取出被捕获的 scope
    scope = captured_scope["scope"]
    # 断言 repo.path 被 repo_path 覆盖为克隆仓路径
    assert scope.config.repo.path == "/some/clone"
    # 断言 effective_project_id（scope.project_id）等于传入的 px（参数优先级最高）
    assert scope.project_id == "px"


def test_run_pipeline_keeps_config_repo_path_when_no_override(monkeypatch):
    """repo_path 未传（None）时，不应改动 config.repo.path（保持向后兼容）。"""
    from src.config.models import ProjectConfig, RepoConfig

    base_config = ProjectConfig(repo=RepoConfig(path="/opt/mall-swarm", project_id="mall-swarm"))
    captured_scope = {}

    def fake_load_config(config_path):
        return base_config

    def fake_execute(scope):
        captured_scope["scope"] = scope
        return {"stage": "knowledge"}

    monkeypatch.setattr("src.pipeline.run.load_config", fake_load_config)
    monkeypatch.setattr(
        "src.pipeline.full_pipeline_orchestrator.execute_full_pipeline_table",
        fake_execute,
    )

    from src.pipeline.run import run_pipeline

    # 不传 repo_path：保持原状
    run_pipeline(config_path="config/project.yaml")

    scope = captured_scope["scope"]
    # path 仍是原始的 mall-swarm 路径
    assert scope.config.repo.path == "/opt/mall-swarm"
