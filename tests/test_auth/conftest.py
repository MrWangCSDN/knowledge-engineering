"""tests/test_auth/conftest.py — 测试夹具（fixtures）。

autouse fixture：
  - _stub_infra_status：所有测试默认 infra 全 ok，避免 require_infra_healthy 拦截。
    测试如果需要验证 infra 挂时的行为，可以在测试函数体内覆盖 app.state.infra_status。

Python 知识点：
  - conftest.py：pytest 的"魔法配置文件"，同目录及子目录的测试会自动加载它里面的 fixture。
  - @pytest.fixture：把一个函数声明为 fixture（可被测试函数注入的依赖）。
  - autouse=True：不需要在测试函数参数里声明，pytest 会自动对所有测试使用它。
"""
import pytest

# 从 src.service.api 导入 FastAPI app 实例（ASGI application 对象）
from src.service.api import app


@pytest.fixture(autouse=True)
def _stub_infra_status():
    """所有测试默认 infra 全 ok，避免 require_infra_healthy 拦截测试流程。

    设计意图：
      - Task 7 给 8 个 router 附了 require_infra_healthy 依赖。
      - 测试夹具（fixture）在 startup 之前跑，app.state.infra_status 还没被 startup 设置。
      - 若不提前设置，测试里所有打到受保护路由的请求都会 503 INFRA_UNINITIALIZED。
      - 本 fixture 把 infra_status 预设为"全 ok"，让已有测试不受影响。

    如需测试 infra 挂时的行为：
      在测试函数体内 **直接覆盖** app.state.infra_status = {...}，
      本 fixture 设置的值会被覆盖（Python 赋值是就地替换，不需要特殊处理）。
    """
    # 把 infra_status 设置为所有依赖全 ok 的状态
    # dict 字面量：{key: value} 语法，每个 key 对应一个依赖名，value 是状态 dict
    app.state.infra_status = {
        "mysql": {"ok": True},       # MySQL 数据库
        "neo4j": {"ok": True},       # Neo4j 图数据库
        "weaviate": {"ok": True},    # Weaviate 向量数据库
        "dashscope": {"ok": True},   # 阿里云 DashScope LLM + embedding API
    }
    # yield 关键字：让 fixture 在测试运行前设置，测试结束后继续运行（这里什么也不做）
    # 相当于：setup → yield → teardown（teardown 部分在 yield 后写）
    yield
    # 测试结束后清理（可选）：把 infra_status 恢复为 None，避免测试间互相干扰
    # 不强制 —— 下一个测试会被本 fixture 重新覆盖，但清理是好习惯
    app.state.infra_status = None
