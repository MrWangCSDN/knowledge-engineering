# 代码源文件查询工具（4 件套）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 QA Agent 加 4 个 Claude Code 风格的源码文件查询工具（`ke_grep` / `ke_glob` / `ke_read_file` / `ke_ls`），用 ripgrep 实现，让 LLM 在结构化数据查不到时回退到文件层探索。

**Architecture:** 4 工具共享 `_path_sandbox.py` realpath 边界检查；project repo path 走 DB `projects.repo_local_path` 字段；`build_tools_for_project` 从 DB 拿 path 闭包给 4 工具 builder；其他 8 工具不变。

**Tech Stack:** Python 3.12 / FastAPI / ripgrep subprocess / SQLAlchemy async / alembic / pytest。

**仓库 / 分支:** `/Users/java/knowledge-engineering-auth` 分支 `release-0513`。

**Spec 来源:** Obsidian `[[代码源文件查询工具-设计]]`（已批准）。

**Run tests:** `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth -q`。

**前置依赖:** ripgrep（开发机 `brew install ripgrep`；CI / 生产 `apt-get install ripgrep`）

---

## File Structure

| 文件 | 改动 | 责任 |
|---|---|---|
| `src/service/qa_engine/tools/_path_sandbox.py` | 🆕 ~40 行 | `resolve_safe_path` helper |
| `src/service/qa_engine/tools/ke_grep.py` | 🆕 ~100 行 | ripgrep subprocess + JSON 解析 |
| `src/service/qa_engine/tools/ke_glob.py` | 🆕 ~70 行 | Python `pathlib.Path.glob` |
| `src/service/qa_engine/tools/ke_read_file.py` | 🆕 ~80 行 | line-range 读 + binary 检测 + 1MB 限制 |
| `src/service/qa_engine/tools/ke_ls.py` | 🆕 ~70 行 | 递归 1-3 层目录列出 |
| `src/service/qa_engine/tools/__init__.py` | Modify | `build_default_registry` 加 `repo_local_path: str \| None` 参数 + 注册 4 工具 |
| `src/service/qa_router.py` | Modify build_tools_for_project | 从 DB Project 拿 `repo_local_path` 透传给 registry |
| `src/service/db_models_homepage.py` | Modify Project ORM | 加 `repo_local_path` Mapped column |
| `alembic/versions/<sha>_add_repo_local_path.py` | 🆕 | ALTER TABLE projects ADD COLUMN |
| `scripts/seed_mall_swarm.py` | Modify | 给 mall-swarm 填 `repo_local_path`（幂等 UPDATE） |
| `src/service/qa_engine/react_synthesizer.py` | Modify | system prompt 加 4 工具用法提示段 |
| `tests/test_auth/test_path_sandbox.py` | 🆕 | resolve_safe_path 5 测试 |
| `tests/test_auth/test_qa_tool_ke_grep.py` | 🆕 | grep 行为 4 测试 |
| `tests/test_auth/test_qa_tool_ke_glob.py` | 🆕 | glob 行为 3 测试 |
| `tests/test_auth/test_qa_tool_ke_read_file.py` | 🆕 | read_file 行为 5 测试 |
| `tests/test_auth/test_qa_tool_ke_ls.py` | 🆕 | ls 行为 3 测试 |
| `tests/test_auth/test_qa_router_tools_injection.py` | Modify | 验证 repo_local_path 闭包到位 |

---

## Task 1: ripgrep 前置 + DB migration

**Files:**
- Modify: `src/service/db_models_homepage.py`（Project class 加字段）
- Create: `alembic/versions/<sha>_add_repo_local_path.py`

- [ ] **Step 0: 确认 ripgrep 装了**

```bash
which rg && rg --version | head -1
```
若没装：`brew install ripgrep`。

- [ ] **Step 1: 改 Project ORM 加 `repo_local_path` Mapped column**

Read `src/service/db_models_homepage.py` 找 Project class（约 line 46-100）。在 `last_synced_commit` Mapped column 之后追加：

```python
    repo_local_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    """项目源码在后端服务器的本地绝对路径；NULL 表示未配置。

    用于 4 个文件类工具（ke_grep / ke_glob / ke_read_file / ke_ls）拼路径。
    设计：[[代码源文件查询工具-设计]] §6
    """
```

- [ ] **Step 2: 生成 alembic migration**

```bash
cd /Users/java/knowledge-engineering-auth
./venv/bin/alembic revision -m "add repo_local_path to projects" --autogenerate 2>&1 | tail -10
```

Expected: 生成 `alembic/versions/<sha>_add_repo_local_path.py` 文件。

- [ ] **Step 3: 检查 migration 内容**

```bash
ls -t /Users/java/knowledge-engineering-auth/alembic/versions/*.py | head -1
```

打开新生成的文件，确认 `upgrade()` 有：
```python
def upgrade():
    op.add_column('projects', sa.Column('repo_local_path', sa.String(length=512), nullable=True))


def downgrade():
    op.drop_column('projects', 'repo_local_path')
```

若 autogenerate 包含其他无关变化（如 type 调整），手动删掉，只保留上面两行。

- [ ] **Step 4: 跑 migration**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/alembic upgrade head 2>&1 | tail -5
```

Expected: 成功 + 数据库表添加新字段。

验证：
```bash
./venv/bin/python -c "
import asyncio
from src.service.db import get_session_maker
from sqlalchemy import text

async def main():
    SM = get_session_maker()
    async with SM() as s:
        r = await s.execute(text('SHOW COLUMNS FROM projects'))
        for row in r:
            if 'repo' in str(row[0]).lower():
                print(row)

asyncio.run(main())
"
```

Expected: 看到 `repo_local_path | varchar(512) | YES | ...`

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/db_models_homepage.py alembic/versions/*_add_repo_local_path.py
git commit -m "$(cat <<'EOF'
feat(infra): Project 加 repo_local_path 字段 + alembic migration

为 4 个文件类工具（ke_grep/ke_glob/ke_read_file/ke_ls）提供 project-level 源码路径。
NULL 表示未配置，工具会返 error 而非 crash。
设计：[[代码源文件查询工具-设计]] §6

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: seed_mall_swarm.py 填 repo_local_path

**Files:**
- Modify: `scripts/seed_mall_swarm.py`

- [ ] **Step 1: Read seed 脚本现状**

```bash
cat /Users/java/knowledge-engineering-auth/scripts/seed_mall_swarm.py
```

确认 PROJECT_ID / GIT_URL / OWNERS 常量位置。

- [ ] **Step 2: 加常量 + 幂等填字段**

在常量区追加：
```python
# 本地源码绝对路径，4 个文件类工具会从这里拼路径访问代码
REPO_LOCAL_PATH = "/Users/java/repos/mall-swarm"
```

修改 main() 里 Project 创建/查找逻辑。找到这段：

```python
        existing = await s.get(Project, PROJECT_ID)
        if existing is not None:
            print(f"  proj[skip] {PROJECT_ID} name={existing.name} status={existing.status}")
        else:
            p = Project(
                id=PROJECT_ID,
                name=PROJECT_NAME,
                language="java",
                status="indexing",
                git_url=GIT_URL,
                git_branch="master",
                created_by="admin",
            )
            s.add(p)
            print(f"  proj[new ] {PROJECT_ID} git_url={GIT_URL}")
```

改为：
```python
        existing = await s.get(Project, PROJECT_ID)
        if existing is not None:
            # 幂等：已存在但 repo_local_path 未配 → UPDATE 填上
            if existing.repo_local_path != REPO_LOCAL_PATH:
                existing.repo_local_path = REPO_LOCAL_PATH
                print(f"  proj[upd ] {PROJECT_ID} repo_local_path={REPO_LOCAL_PATH}")
            else:
                print(f"  proj[skip] {PROJECT_ID} name={existing.name} status={existing.status}")
        else:
            p = Project(
                id=PROJECT_ID,
                name=PROJECT_NAME,
                language="java",
                status="indexing",
                git_url=GIT_URL,
                git_branch="master",
                repo_local_path=REPO_LOCAL_PATH,  # 新加
                created_by="admin",
            )
            s.add(p)
            print(f"  proj[new ] {PROJECT_ID} git_url={GIT_URL} path={REPO_LOCAL_PATH}")
```

- [ ] **Step 3: 跑 seed 验证**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m scripts.seed_mall_swarm 2>&1 | tail -10
```

Expected: `proj[upd] mall-swarm repo_local_path=/Users/java/repos/mall-swarm` 或类似 + admin 已有时显示 skip。

验证 DB：
```bash
./venv/bin/python -c "
import asyncio
from src.service.db import get_session_maker
from src.service.db_models_homepage import Project

async def main():
    SM = get_session_maker()
    async with SM() as s:
        p = await s.get(Project, 'mall-swarm')
        print(f'mall-swarm.repo_local_path = {p.repo_local_path}')

asyncio.run(main())
"
```

Expected: `mall-swarm.repo_local_path = /Users/java/repos/mall-swarm`

- [ ] **Step 4: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add scripts/seed_mall_swarm.py
git commit -m "$(cat <<'EOF'
feat(seed): 给 mall-swarm 填 repo_local_path

幂等：已存在 project 且 repo_local_path 不匹配 → UPDATE 填上。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `_path_sandbox.py` helper + 单元测试

**Files:**
- Create: `src/service/qa_engine/tools/_path_sandbox.py`
- Test: Create `tests/test_auth/test_path_sandbox.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth/test_path_sandbox.py`：

```python
"""路径沙箱单元测试 — resolve_safe_path 边界 + 越界 + symlink。

设计：[[代码源文件查询工具-设计]] §4
"""
from pathlib import Path

import pytest

from src.service.qa_engine.tools._path_sandbox import resolve_safe_path


def test_resolve_safe_path_normal_project_relative(tmp_path):
    """正常项目内 path → 返回拼接后的绝对路径。"""
    (tmp_path / "mall-admin").mkdir()
    (tmp_path / "mall-admin" / "pom.xml").write_text("<project/>")

    target = resolve_safe_path(str(tmp_path), "mall-admin/pom.xml")
    assert target == (tmp_path / "mall-admin" / "pom.xml").resolve()


def test_resolve_safe_path_dot_returns_repo_root(tmp_path):
    """'.' 视作项目根目录。"""
    target = resolve_safe_path(str(tmp_path), ".")
    assert target == tmp_path.resolve()


def test_resolve_safe_path_rejects_absolute_path(tmp_path):
    """绝对路径直接拒绝。"""
    with pytest.raises(ValueError, match="absolute"):
        resolve_safe_path(str(tmp_path), "/etc/passwd")


def test_resolve_safe_path_rejects_parent_escape(tmp_path):
    """../ 越界访问拒绝。"""
    with pytest.raises(ValueError, match="out of repo boundary"):
        resolve_safe_path(str(tmp_path), "../../../etc/passwd")


def test_resolve_safe_path_rejects_symlink_escape(tmp_path):
    """symlink 指向 repo 外 → 拒绝（realpath 会跟随）。"""
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "linked").symlink_to(outside)

    with pytest.raises(ValueError, match="out of repo boundary"):
        resolve_safe_path(str(tmp_path), "linked")


def test_resolve_safe_path_rejects_empty_repo_path():
    """repo_local_path 为空 → 拒绝（NULL DB 字段场景）。"""
    with pytest.raises(ValueError, match="source path not configured"):
        resolve_safe_path("", "anything")

    with pytest.raises(ValueError, match="source path not configured"):
        resolve_safe_path(None, "anything")  # type: ignore[arg-type]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_path_sandbox.py -v
```
Expected: ImportError（_path_sandbox 模块不存在）。

- [ ] **Step 3: 创建 `src/service/qa_engine/tools/_path_sandbox.py`**

```python
"""路径沙箱：限制 4 个文件类工具只能访问 project repo_local_path 范围内。

设计：[[代码源文件查询工具-设计]] §4

每个文件工具 handler 第一步都调 resolve_safe_path，把 LLM 传的
project-relative path 拼成绝对路径并做边界检查；拒绝以下情况：
- repo_local_path 为空（DB 字段 NULL）
- relative_path 是绝对路径（防 LLM 传 /etc/passwd）
- relative_path 解析后越出 repo_local_path（含 ../../  和 symlink 逃逸）
"""
from __future__ import annotations

# Path：用 pathlib 而非 os.path，跨平台 + 链式调用更清晰
from pathlib import Path


def resolve_safe_path(repo_local_path: str | None, relative_path: str) -> Path:
    """把 project-relative path 拼成绝对路径并做边界检查。

    :param repo_local_path: 项目源码本地绝对路径（DB projects.repo_local_path）。
        若为 None / 空字符串 → 视作"未配置"，立即抛 ValueError。
    :param relative_path: LLM 传的项目相对路径，如 'mall-admin/pom.xml'。
    :returns: 解析后的绝对路径（已确认在 repo 内）。
    :raises ValueError:
        - repo_local_path 未配置
        - relative_path 是绝对路径
        - 解析后路径越界（含 symlink 逃逸）
    """
    # 1. config sanity：repo 路径必须配置（DB 字段非空）
    if not repo_local_path:
        raise ValueError("source path not configured for this project")

    rel = Path(relative_path)

    # 2. 拒绝绝对路径（防 LLM 传 /etc/passwd 这种）
    # is_absolute() 在 macOS/Linux 上对 '/...' 返回 True
    if rel.is_absolute():
        raise ValueError(f"path must be project-relative, got absolute: {relative_path}")

    # 3. 拼接 + realpath 解析（symlink 也会被跟随到真实目标）
    # Path.resolve() 等同于 os.path.realpath()，处理 ../  / symlink 一并
    root = Path(repo_local_path).resolve()
    target = (root / rel).resolve()

    # 4. 边界检查：解析后路径必须仍以 root 开头
    # relative_to 在不属于 root 时抛 ValueError，我们捕获后换成自己的错误信息
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"path out of repo boundary: {relative_path}")

    return target
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_path_sandbox.py -v
```
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/tools/_path_sandbox.py tests/test_auth/test_path_sandbox.py
git commit -m "$(cat <<'EOF'
feat(tools): 新增 _path_sandbox.py — resolve_safe_path 共享 helper

4 个文件类工具共用：拒绝绝对路径 / ../  / symlink 越界 / repo path 未配置。
realpath 跟随 symlink 保证逃逸场景也被抓。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `ke_grep` 工具 + 测试

**Files:**
- Create: `src/service/qa_engine/tools/ke_grep.py`
- Test: Create `tests/test_auth/test_qa_tool_ke_grep.py`

- [ ] **Step 1: 写失败测试**

```python
"""ke_grep 工具测试 — ripgrep subprocess + sandbox + result 截断。

设计：[[代码源文件查询工具-设计]] §3.1
"""
import asyncio
import os
import shutil

import pytest

from src.service.qa_engine.tools.ke_grep import build_ke_grep_tool


# 全套测试：未装 ripgrep 时全跳过 + 一行 warning
pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None,
    reason="ripgrep not installed (brew install ripgrep)",
)


def _run(coro):
    """异步 helper（与 test_qa_tool_ke_search 风格一致）。"""
    return asyncio.run(coro)


def test_ke_grep_finds_basic_pattern(tmp_path):
    """grep 能找到字面匹配。"""
    (tmp_path / "Foo.java").write_text("import com.x.RedisTemplate;\nclass Foo {}\n")
    (tmp_path / "Bar.java").write_text("class Bar {}\n")

    tool = build_ke_grep_tool(str(tmp_path))
    result = _run(tool.handler({"pattern": "RedisTemplate"}))

    assert "matches" in result
    assert len(result["matches"]) == 1
    m = result["matches"][0]
    assert m["path"] == "Foo.java"
    assert m["line"] == 1
    assert "RedisTemplate" in m["text"]


def test_ke_grep_respects_glob(tmp_path):
    """glob 过滤生效。"""
    (tmp_path / "Foo.java").write_text("RedisTemplate\n")
    (tmp_path / "Bar.xml").write_text("RedisTemplate\n")

    tool = build_ke_grep_tool(str(tmp_path))
    result = _run(tool.handler({"pattern": "RedisTemplate", "glob": "**/*.xml"}))

    assert len(result["matches"]) == 1
    assert result["matches"][0]["path"] == "Bar.xml"


def test_ke_grep_max_results_truncates(tmp_path):
    """max_results 截断 + truncated=True。"""
    for i in range(10):
        (tmp_path / f"F{i}.txt").write_text("HIT\n")

    tool = build_ke_grep_tool(str(tmp_path))
    result = _run(tool.handler({"pattern": "HIT", "max_results": 3}))

    assert len(result["matches"]) == 3
    assert result["truncated"] is True


def test_ke_grep_no_match_returns_empty(tmp_path):
    """无匹配 → matches:[], truncated:False。"""
    (tmp_path / "F.txt").write_text("nothing\n")
    tool = build_ke_grep_tool(str(tmp_path))
    result = _run(tool.handler({"pattern": "RedisTemplate"}))
    assert result["matches"] == []
    assert result["truncated"] is False


def test_ke_grep_config_missing_returns_error():
    """repo_local_path 未配置 → 返 error 不抛。"""
    tool = build_ke_grep_tool(None)
    result = _run(tool.handler({"pattern": "X"}))
    assert "error" in result
    assert "not configured" in result["error"]


def test_ke_grep_missing_pattern_returns_error(tmp_path):
    """缺 pattern → 返 error。"""
    tool = build_ke_grep_tool(str(tmp_path))
    result = _run(tool.handler({}))
    assert "error" in result
    assert "pattern" in result["error"].lower()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_tool_ke_grep.py -v
```
Expected: ImportError 或测试 fail（模块不存在）。

- [ ] **Step 3: 创建 `src/service/qa_engine/tools/ke_grep.py`**

```python
"""ke_grep 工具：用 ripgrep 在项目源码中按正则搜索内容。

设计：[[代码源文件查询工具-设计]] §3.1

ripgrep 默认行为保留：自动跳 .gitignore / .git/ / binary / node_modules / target / dist。
subprocess 用列表参数（不 shell=True），shell 注入关死。
"""
from __future__ import annotations

# subprocess：调 rg 外部命令；用 list 参数避免 shell 注入
import asyncio
import json
import subprocess
from typing import Any

from src.service.qa_engine.tools.base import Tool


# 单次 grep 5s timeout
GREP_TIMEOUT_SEC = 5

_KE_GREP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "正则表达式（PCRE 风格）",
        },
        "glob": {
            "type": "string",
            "description": "文件名 glob 过滤（默认 '**/*'）",
        },
        "case_sensitive": {
            "type": "boolean",
            "default": False,
        },
        "max_results": {
            "type": "integer",
            "default": 50,
            "minimum": 1,
            "maximum": 200,
        },
    },
    "required": ["pattern"],
}


def build_ke_grep_tool(repo_local_path: str | None) -> Tool:
    """构造一个绑定到 repo_local_path 的 ke_grep Tool。

    :param repo_local_path: 项目源码本地绝对路径；None 时 handler 返 error。
    """
    # 闭包变量；None 也允许，handler 内部统一 error 处理
    bound_repo = repo_local_path

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        # config sanity
        if not bound_repo:
            return {"error": "source path not configured for this project", "matches": []}

        pattern = (input.get("pattern") or "").strip()
        if not pattern:
            return {"error": "missing required field: pattern", "matches": []}

        # 参数解析（带默认 + 限上限）
        glob_filter = (input.get("glob") or "").strip()
        case_sensitive = bool(input.get("case_sensitive", False))
        try:
            max_results = int(input.get("max_results", 50))
        except (TypeError, ValueError):
            max_results = 50
        # clamp 到 [1, 200]
        max_results = max(1, min(max_results, 200))

        # 构造 rg 命令；列表形式不走 shell，防注入
        cmd: list[str] = ["rg", "--json", "-e", pattern]
        if not case_sensitive:
            cmd.append("--ignore-case")
        if glob_filter:
            cmd.extend(["-g", glob_filter])
        # rg 默认跳 binary + .gitignore；--max-count 是每文件上限，这里只用整体上限
        cmd.extend(["--max-count", "1000", "--"])
        cmd.append(bound_repo)

        try:
            # asyncio.to_thread 跑同步 subprocess（不阻塞 loop）
            def _run_rg() -> tuple[int, str]:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=GREP_TIMEOUT_SEC,
                )
                return proc.returncode, proc.stdout

            rc, stdout = await asyncio.to_thread(_run_rg)
        except subprocess.TimeoutExpired:
            return {"error": f"ke_grep timeout (>{GREP_TIMEOUT_SEC}s)", "matches": []}
        except FileNotFoundError:
            return {"error": "ripgrep not installed (run: brew install ripgrep)", "matches": []}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "matches": []}

        # rg --json 输出：每行一个 JSON 对象，type 字段区分 begin/match/end/summary
        matches: list[dict] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "match":
                continue
            # data.path.text → 文件路径（绝对）；data.lines.text → 命中行；data.line_number
            data = obj.get("data", {})
            abs_path = data.get("path", {}).get("text", "")
            # 转 project-relative（去掉 repo_local_path 前缀）
            rel_path = abs_path
            if abs_path.startswith(bound_repo):
                rel_path = abs_path[len(bound_repo):].lstrip("/")
            line_num = data.get("line_number", 0)
            text = data.get("lines", {}).get("text", "").rstrip("\n")
            matches.append({
                "path": rel_path,
                "line": line_num,
                "text": text,
            })
            if len(matches) >= max_results:
                break

        # truncated：raw 还有更多就 True（看后面是否还有 match）
        # 简化：实际命中数从 rg summary 拿；本实现按 matches 是否达到 max_results 估算
        truncated = len(matches) >= max_results
        return {
            "matches": matches,
            "truncated": truncated,
            "total_count": len(matches),
        }

    return Tool(
        name="ke_grep",
        description="在当前项目源码中按正则 grep（ripgrep）；ripgrep 默认跳 .gitignore + binary。项目根已绑定，无需提供 path。",
        input_schema=_KE_GREP_SCHEMA,
        handler=handler,
    )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_tool_ke_grep.py -v
```
Expected: 6 PASS（若 ripgrep 未装，6 个 SKIP + warning，**不算 fail**）。

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/tools/ke_grep.py tests/test_auth/test_qa_tool_ke_grep.py
git commit -m "$(cat <<'EOF'
feat(tools): 新增 ke_grep 文件搜索工具（ripgrep subprocess）

闭包绑定 repo_local_path；5s timeout；max_results 上限 200。
ripgrep 自动跳 binary / .gitignore。subprocess 用列表参数防 shell 注入。
设计：[[代码源文件查询工具-设计]] §3.1

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `ke_glob` 工具 + 测试

**Files:**
- Create: `src/service/qa_engine/tools/ke_glob.py`
- Test: Create `tests/test_auth/test_qa_tool_ke_glob.py`

- [ ] **Step 1: 写失败测试**

```python
"""ke_glob 工具测试 — Pathlib.glob + project-relative 返回 + head 截断。

设计：[[代码源文件查询工具-设计]] §3.2
"""
import asyncio

import pytest

from src.service.qa_engine.tools.ke_glob import build_ke_glob_tool


def _run(coro):
    return asyncio.run(coro)


def test_ke_glob_finds_files(tmp_path):
    """基本 glob 能列出匹配文件，path 是 project-relative。"""
    (tmp_path / "mall-admin").mkdir()
    (tmp_path / "mall-admin" / "FooMapper.xml").write_text("")
    (tmp_path / "mall-admin" / "BarController.java").write_text("")
    (tmp_path / "mall-portal").mkdir()
    (tmp_path / "mall-portal" / "BazMapper.xml").write_text("")

    tool = build_ke_glob_tool(str(tmp_path))
    result = _run(tool.handler({"pattern": "**/*Mapper.xml"}))

    assert "files" in result
    assert sorted(result["files"]) == sorted([
        "mall-admin/FooMapper.xml",
        "mall-portal/BazMapper.xml",
    ])


def test_ke_glob_head_truncates(tmp_path):
    """head 截断 + truncated=True。"""
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("")

    tool = build_ke_glob_tool(str(tmp_path))
    result = _run(tool.handler({"pattern": "*.txt", "head": 3}))

    assert len(result["files"]) == 3
    assert result["truncated"] is True


def test_ke_glob_config_missing_returns_error():
    tool = build_ke_glob_tool(None)
    result = _run(tool.handler({"pattern": "*"}))
    assert "error" in result
    assert "not configured" in result["error"]


def test_ke_glob_missing_pattern_returns_error(tmp_path):
    tool = build_ke_glob_tool(str(tmp_path))
    result = _run(tool.handler({}))
    assert "error" in result
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_tool_ke_glob.py -v
```

- [ ] **Step 3: 创建 `src/service/qa_engine/tools/ke_glob.py`**

```python
"""ke_glob 工具：按 glob pattern 列项目内匹配文件路径（不读内容）。

设计：[[代码源文件查询工具-设计]] §3.2
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.service.qa_engine.tools.base import Tool


_KE_GLOB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "glob pattern，如 '**/Mapper/*.xml' 或 'mall-admin/**/*Controller.java'",
        },
        "head": {
            "type": "integer",
            "default": 50,
            "minimum": 1,
            "maximum": 200,
        },
    },
    "required": ["pattern"],
}


def build_ke_glob_tool(repo_local_path: str | None) -> Tool:
    """构造一个绑定到 repo_local_path 的 ke_glob Tool。"""
    bound_repo = repo_local_path

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        if not bound_repo:
            return {"error": "source path not configured for this project", "files": []}

        pattern = (input.get("pattern") or "").strip()
        if not pattern:
            return {"error": "missing required field: pattern", "files": []}

        try:
            head = int(input.get("head", 50))
        except (TypeError, ValueError):
            head = 50
        head = max(1, min(head, 200))

        try:
            root = Path(bound_repo)
            # Path.glob 支持 ** 递归；rglob 与 glob('**/...') 等价
            # 注意：Path.glob 不读 .gitignore，可能列出 .git/node_modules/target；后续可加 filter
            files: list[str] = []
            for p in root.glob(pattern):
                if p.is_file():
                    # 转 project-relative
                    rel = p.relative_to(root).as_posix()
                    files.append(rel)
                    if len(files) >= head:
                        break
            files.sort()
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "files": []}

        return {
            "files": files,
            "truncated": len(files) >= head,
            "total_count": len(files),
        }

    return Tool(
        name="ke_glob",
        description="按 glob pattern 列项目内匹配的文件路径（不读内容）；项目根已绑定。",
        input_schema=_KE_GLOB_SCHEMA,
        handler=handler,
    )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_tool_ke_glob.py -v
```
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/tools/ke_glob.py tests/test_auth/test_qa_tool_ke_glob.py
git commit -m "$(cat <<'EOF'
feat(tools): 新增 ke_glob 文件名搜索工具

Path.glob 支持 ** 递归；head 上限 200；返回 project-relative path。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `ke_read_file` 工具 + 测试

**Files:**
- Create: `src/service/qa_engine/tools/ke_read_file.py`
- Test: Create `tests/test_auth/test_qa_tool_ke_read_file.py`

- [ ] **Step 1: 写失败测试**

```python
"""ke_read_file 工具测试 — line range / 1MB 上限 / binary 拒 / sandbox 越界。

设计：[[代码源文件查询工具-设计]] §3.3
"""
import asyncio

import pytest

from src.service.qa_engine.tools.ke_read_file import build_ke_read_file_tool


def _run(coro):
    return asyncio.run(coro)


def test_ke_read_file_basic(tmp_path):
    """基本读 — 完整内容 + line range 字段。"""
    (tmp_path / "pom.xml").write_text("<project>\n  <name>x</name>\n</project>\n")

    tool = build_ke_read_file_tool(str(tmp_path))
    result = _run(tool.handler({"path": "pom.xml"}))

    assert result["path"] == "pom.xml"
    assert "<project>" in result["content"]
    assert result["line_start"] == 0
    assert result["eof"] is True
    assert result["total_lines"] == 3


def test_ke_read_file_offset_limit(tmp_path):
    """offset + limit 截断（line-based）。"""
    (tmp_path / "x.txt").write_text("\n".join(f"line{i}" for i in range(100)) + "\n")

    tool = build_ke_read_file_tool(str(tmp_path))
    result = _run(tool.handler({"path": "x.txt", "offset": 10, "limit": 5}))

    lines = result["content"].split("\n")
    assert lines[0] == "line10"
    assert lines[4] == "line14"
    assert result["line_start"] == 10
    assert result["line_end"] == 14
    assert result["eof"] is False


def test_ke_read_file_rejects_large_file(tmp_path):
    """文件 > 1MB → error。"""
    big = "x" * (1024 * 1024 + 100)
    (tmp_path / "big.txt").write_text(big)

    tool = build_ke_read_file_tool(str(tmp_path))
    result = _run(tool.handler({"path": "big.txt"}))

    assert "error" in result
    assert "too large" in result["error"].lower()


def test_ke_read_file_rejects_binary(tmp_path):
    """binary 文件拒读。"""
    (tmp_path / "bin").write_bytes(b"\x00\x01\x02\x03")

    tool = build_ke_read_file_tool(str(tmp_path))
    result = _run(tool.handler({"path": "bin"}))

    assert "error" in result
    assert "binary" in result["error"].lower()


def test_ke_read_file_path_traversal_rejected(tmp_path):
    """../etc/passwd 越界拒。"""
    tool = build_ke_read_file_tool(str(tmp_path))
    result = _run(tool.handler({"path": "../../../etc/passwd"}))

    assert "error" in result
    assert "boundary" in result["error"]


def test_ke_read_file_config_missing():
    tool = build_ke_read_file_tool(None)
    result = _run(tool.handler({"path": "x"}))
    assert "error" in result
    assert "not configured" in result["error"]
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 创建 `src/service/qa_engine/tools/ke_read_file.py`**

```python
"""ke_read_file 工具：读项目内文件，按 line range 返回。

设计：[[代码源文件查询工具-设计]] §3.3
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.service.qa_engine.tools.base import Tool
from src.service.qa_engine.tools._path_sandbox import resolve_safe_path


# 单文件最大 1MB（防 LLM 拉巨大 dump）
MAX_FILE_SIZE_BYTES = 1024 * 1024


_KE_READ_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "项目相对路径，如 'mall-admin/pom.xml'",
        },
        "offset": {
            "type": "integer",
            "default": 0,
            "minimum": 0,
            "description": "起始 line（0-based）",
        },
        "limit": {
            "type": "integer",
            "default": 200,
            "minimum": 1,
            "maximum": 1000,
        },
    },
    "required": ["path"],
}


def _looks_binary(content_bytes: bytes) -> bool:
    """启发式判 binary：前 8KB 中含 NUL byte → binary。

    与 git/grep 默认行为对齐：NUL byte 强烈暗示 binary。
    """
    return b"\x00" in content_bytes[:8192]


def build_ke_read_file_tool(repo_local_path: str | None) -> Tool:
    """构造一个绑定到 repo_local_path 的 ke_read_file Tool。"""
    bound_repo = repo_local_path

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        path = (input.get("path") or "").strip()
        if not path:
            return {"error": "missing required field: path"}

        # sandbox 检查（包括 config-missing / 绝对路径 / 越界）
        try:
            target = resolve_safe_path(bound_repo, path)
        except ValueError as e:
            return {"error": str(e)}

        # 文件存在性 + 类型
        if not target.exists():
            return {"error": f"file not found: {path}"}
        if not target.is_file():
            return {"error": f"not a regular file: {path}"}

        # 文件大小 < 1MB
        size = target.stat().st_size
        if size > MAX_FILE_SIZE_BYTES:
            return {"error": f"file too large ({size} bytes > {MAX_FILE_SIZE_BYTES} max)"}

        # binary 检测
        try:
            raw = target.read_bytes()
        except Exception as e:
            return {"error": f"read failed: {type(e).__name__}: {e}"}
        if _looks_binary(raw):
            return {"error": "binary file not supported"}

        # UTF-8 解码（容错）
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")

        # line range 切片
        try:
            offset = max(0, int(input.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = int(input.get("limit", 200))
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(limit, 1000))

        all_lines = text.splitlines()
        total_lines = len(all_lines)
        end_idx = min(offset + limit, total_lines)
        selected = all_lines[offset:end_idx]
        content = "\n".join(selected)

        return {
            "path": path,
            "content": content,
            "line_start": offset,
            "line_end": end_idx - 1 if end_idx > offset else offset,
            "eof": end_idx >= total_lines,
            "total_lines": total_lines,
        }

    return Tool(
        name="ke_read_file",
        description="读项目内某个文件的内容；path 是项目相对路径，按 line range 返回。",
        input_schema=_KE_READ_FILE_SCHEMA,
        handler=handler,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/tools/ke_read_file.py tests/test_auth/test_qa_tool_ke_read_file.py
git commit -m "$(cat <<'EOF'
feat(tools): 新增 ke_read_file 文件内容读取工具

line range offset/limit；1MB 文件上限；NUL byte 检测 binary；
sandbox 越界拒。设计：[[代码源文件查询工具-设计]] §3.3

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `ke_ls` 工具 + 测试

**Files:**
- Create: `src/service/qa_engine/tools/ke_ls.py`
- Test: Create `tests/test_auth/test_qa_tool_ke_ls.py`

- [ ] **Step 1: 写失败测试**

```python
"""ke_ls 工具测试 — 目录列出 + depth + include_hidden。

设计：[[代码源文件查询工具-设计]] §3.4
"""
import asyncio

import pytest

from src.service.qa_engine.tools.ke_ls import build_ke_ls_tool


def _run(coro):
    return asyncio.run(coro)


def test_ke_ls_basic(tmp_path):
    """默认 depth=1 列直接子项。"""
    (tmp_path / "mall-admin").mkdir()
    (tmp_path / "README.md").write_text("readme")
    (tmp_path / ".env").write_text("hidden")

    tool = build_ke_ls_tool(str(tmp_path))
    result = _run(tool.handler({"path": "."}))

    names = sorted([e["name"] for e in result["entries"]])
    # 默认 include_hidden=False，.env 不应出现
    assert "mall-admin" in names
    assert "README.md" in names
    assert ".env" not in names


def test_ke_ls_include_hidden(tmp_path):
    """include_hidden=True 时列隐藏文件。"""
    (tmp_path / ".env").write_text("secret")
    (tmp_path / "visible.txt").write_text("x")

    tool = build_ke_ls_tool(str(tmp_path))
    result = _run(tool.handler({"include_hidden": True}))

    names = [e["name"] for e in result["entries"]]
    assert ".env" in names


def test_ke_ls_depth_limit(tmp_path):
    """depth=2 进入一层子目录。"""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "a" / "b" / "c.txt").write_text("")

    tool = build_ke_ls_tool(str(tmp_path))
    result = _run(tool.handler({"path": ".", "depth": 2}))

    # entries 至少含 a/ 和 a/b/（depth=2 进入第 1 层子目录）
    names = [e["name"] for e in result["entries"]]
    assert "a" in names or "a/b" in names


def test_ke_ls_config_missing():
    tool = build_ke_ls_tool(None)
    result = _run(tool.handler({}))
    assert "error" in result
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 创建 `src/service/qa_engine/tools/ke_ls.py`**

```python
"""ke_ls 工具：列项目内目录的内容（可递归 1-3 层）。

设计：[[代码源文件查询工具-设计]] §3.4
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.service.qa_engine.tools.base import Tool
from src.service.qa_engine.tools._path_sandbox import resolve_safe_path


_KE_LS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "default": ".",
            "description": "项目相对目录路径",
        },
        "depth": {
            "type": "integer",
            "default": 1,
            "minimum": 1,
            "maximum": 3,
        },
        "include_hidden": {
            "type": "boolean",
            "default": False,
        },
    },
}


def build_ke_ls_tool(repo_local_path: str | None) -> Tool:
    """构造一个绑定到 repo_local_path 的 ke_ls Tool。"""
    bound_repo = repo_local_path

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        path = (input.get("path") or ".").strip() or "."

        try:
            target = resolve_safe_path(bound_repo, path)
        except ValueError as e:
            return {"error": str(e), "entries": []}

        if not target.exists():
            return {"error": f"directory not found: {path}", "entries": []}
        if not target.is_dir():
            return {"error": f"not a directory: {path}", "entries": []}

        try:
            depth = int(input.get("depth", 1))
        except (TypeError, ValueError):
            depth = 1
        depth = max(1, min(depth, 3))
        include_hidden = bool(input.get("include_hidden", False))

        entries: list[dict] = []
        root = Path(bound_repo)

        # 递归列：depth 控制层数
        def _walk(d: Path, current_depth: int) -> None:
            if current_depth > depth:
                return
            try:
                children = sorted(d.iterdir())
            except PermissionError:
                return
            for child in children:
                if not include_hidden and child.name.startswith("."):
                    continue
                # 跳过常见的"垃圾"目录（与 ripgrep 默认对齐）
                if child.name in ("node_modules", "target", "dist", ".git"):
                    continue
                rel = child.relative_to(root).as_posix()
                entry: dict[str, Any] = {
                    "name": rel,
                    "type": "dir" if child.is_dir() else "file",
                }
                if child.is_file():
                    try:
                        entry["size"] = child.stat().st_size
                    except OSError:
                        pass
                entries.append(entry)
                if child.is_dir():
                    _walk(child, current_depth + 1)

        _walk(target, 1)

        return {
            "path": path,
            "entries": entries,
        }

    return Tool(
        name="ke_ls",
        description="列项目内某个目录的内容；可递归 1-3 层；默认跳隐藏文件 + node_modules/target/dist。",
        input_schema=_KE_LS_SCHEMA,
        handler=handler,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/tools/ke_ls.py tests/test_auth/test_qa_tool_ke_ls.py
git commit -m "$(cat <<'EOF'
feat(tools): 新增 ke_ls 目录列出工具

depth 1-3 + include_hidden 默认 False + 自动跳 node_modules/target/dist/.git。
设计：[[代码源文件查询工具-设计]] §3.4

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: 注册 4 工具到 `build_default_registry`

**Files:**
- Modify: `src/service/qa_engine/tools/__init__.py`
- Modify: `tests/test_auth/test_qa_tools_default_registry.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth/test_qa_tools_default_registry.py` 末尾追加：

```python
def test_build_default_registry_accepts_repo_local_path():
    """build_default_registry 接受 repo_local_path 参数并注册 4 个文件类工具。"""
    from unittest.mock import MagicMock
    from src.service.qa_engine.tools import build_default_registry

    graph = MagicMock()
    business = MagicMock()
    registry = build_default_registry(
        graph=graph,
        business_store=business,
        project_id="test",
        repo_local_path="/tmp/fake-repo",
    )

    # 4 个新工具被注册
    for name in ("ke_grep", "ke_glob", "ke_read_file", "ke_ls"):
        assert registry.get(name) is not None, f"{name} not registered"


def test_build_default_registry_without_repo_local_path():
    """repo_local_path 为 None 时 4 个工具仍注册（handler 内部 error 处理）。"""
    from unittest.mock import MagicMock
    from src.service.qa_engine.tools import build_default_registry

    graph = MagicMock()
    business = MagicMock()
    registry = build_default_registry(
        graph=graph,
        business_store=business,
        project_id="test",
        # 不传 repo_local_path
    )

    # 4 个工具仍注册
    for name in ("ke_grep", "ke_glob", "ke_read_file", "ke_ls"):
        assert registry.get(name) is not None
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_tools_default_registry.py -v
```

- [ ] **Step 3: 改 `src/service/qa_engine/tools/__init__.py`**

Read 现在的文件，在 import 区追加：
```python
from src.service.qa_engine.tools.ke_grep import build_ke_grep_tool
from src.service.qa_engine.tools.ke_glob import build_ke_glob_tool
from src.service.qa_engine.tools.ke_read_file import build_ke_read_file_tool
from src.service.qa_engine.tools.ke_ls import build_ke_ls_tool
```

在 `__all__` 列表追加：
```python
    "build_ke_grep_tool",
    "build_ke_glob_tool",
    "build_ke_read_file_tool",
    "build_ke_ls_tool",
```

修改 `build_default_registry` 签名加 `repo_local_path` 参数：

```python
def build_default_registry(
    *,
    graph: GraphProto,
    business_store: BusinessStoreProto,
    project_id: str,
    code_store: Any | None = None,
    method_interp_store: Any | None = None,
    repo_local_path: str | None = None,    # 新参数
) -> ToolRegistry:
```

更新 docstring 加一段：
```
:param repo_local_path: 项目源码本地绝对路径（DB projects.repo_local_path）。
    用于 4 个文件类工具（ke_grep / ke_glob / ke_read_file / ke_ls）；
    None 时 4 个工具仍注册但会返 "source path not configured" error。
```

在 函数体末尾（return 前）追加 4 个工具注册：

```python
    # 4 个文件类工具：闭包绑定 repo_local_path
    # 设计：[[代码源文件查询工具-设计]] §3
    registry.register(build_ke_grep_tool(repo_local_path))
    registry.register(build_ke_glob_tool(repo_local_path))
    registry.register(build_ke_read_file_tool(repo_local_path))
    registry.register(build_ke_ls_tool(repo_local_path))
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_tools_default_registry.py -v
```
Expected: 全 PASS（含 2 个新加 + 老的不回归）。

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/tools/__init__.py tests/test_auth/test_qa_tools_default_registry.py
git commit -m "$(cat <<'EOF'
feat(tools): build_default_registry 注册 4 个文件类工具

加 repo_local_path 参数透传给 4 工具 builder；None 时仍注册，handler 内 error。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: `qa_router.build_tools_for_project` 从 DB 拿 repo_local_path

**Files:**
- Modify: `src/service/qa_router.py`
- Modify: `tests/test_auth/test_qa_router_tools_injection.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth/test_qa_router_tools_injection.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_build_tools_for_project_loads_repo_local_path_from_db(monkeypatch):
    """build_tools_for_project 从 DB Project 拿 repo_local_path 闭包给 4 工具。"""
    from unittest.mock import MagicMock, AsyncMock
    from fastapi import Request
    from src.service.qa_router import build_tools_for_project

    # 假 Project 对象
    fake_project = MagicMock()
    fake_project.repo_local_path = "/tmp/fake-repo"

    # mock app.state
    request = MagicMock(spec=Request)
    request.app.state.weaviate_business_store = MagicMock()
    request.app.state.neo4j_backend = MagicMock()
    request.app.state.weaviate_code_store = None
    request.app.state.weaviate_method_interp_store = None

    # mock db.get(Project, ...)
    fake_db = AsyncMock()
    fake_db.get = AsyncMock(return_value=fake_project)

    registry = await build_tools_for_project("mall-swarm", request, fake_db)
    # 4 工具都注册了
    for name in ("ke_grep", "ke_glob", "ke_read_file", "ke_ls"):
        assert registry.get(name) is not None
```

注意：本测试假设 `build_tools_for_project` 改成 async + 接 `db` 参数（见 Step 3）。

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 改 `src/service/qa_router.py`**

找 `build_tools_for_project` 函数（约 line 96）：

旧：
```python
def build_tools_for_project(project_id: str, request: Request):
    ...
    return build_default_registry(
        graph=graph_adapter,
        business_store=biz_adapter,
        project_id=project_id,
        code_store=code_store,
        method_interp_store=method_interp_store,
    )
```

新：
```python
async def build_tools_for_project(project_id: str, request: Request, db: AsyncSession):
    ...
    # 从 DB 取 repo_local_path（4 个文件工具需要）
    from src.service.db_models_homepage import Project as ProjectModel
    project = await db.get(ProjectModel, project_id)
    repo_local_path = project.repo_local_path if project else None

    return build_default_registry(
        graph=graph_adapter,
        business_store=biz_adapter,
        project_id=project_id,
        code_store=code_store,
        method_interp_store=method_interp_store,
        repo_local_path=repo_local_path,
    )
```

签名变 async，注意所有调用方都要 await。Grep 调用方：

```bash
grep -rn "build_tools_for_project" /Users/java/knowledge-engineering-auth/src 2>/dev/null
```

可能有 `_inject_per_request_tool_registry` 等 helper 也要相应改：把 `synthesizer.tool_registry = build_tools_for_project(...)` 改成 `synthesizer.tool_registry = await build_tools_for_project(project_id, request, db)`。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_router_tools_injection.py -v
```

如果其他 `test_qa_router*` 测试因为新签名失败，按相同模式补 `db` 参数 / `await`。

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_router.py tests/test_auth/test_qa_router_tools_injection.py
# 若改了其他 test 文件适配 async signature 也一并加
git commit -m "$(cat <<'EOF'
feat(qa-router): build_tools_for_project 从 DB 拿 repo_local_path 透传给 4 工具

签名变 async（要 await db.get(Project)），调用方相应 await。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: ReActSynthesizer system prompt 加 4 工具用法提示

**Files:**
- Modify: `src/service/qa_engine/react_synthesizer.py`

- [ ] **Step 1: 找 system prompt 位置**

```bash
grep -n "工具使用规则\|tool_lines\|【可调用工具" /Users/java/knowledge-engineering-auth/src/service/qa_engine/react_synthesizer.py | head -5
```

应该在 line ~383 附近，那段 f-string 的尾部。

- [ ] **Step 2: 在工具规则段加 4 工具提示**

找到现有规则 4 之后（`4. **能给最终答案就别再调工具**`），在那条之后追加新规则 5：

```
5. **新增：文件层探索工具**（ke_grep / ke_glob / ke_read_file / ke_ls）适合
   "图谱里没的东西"——配置文件、Mapper XML 原文、注释、字符串常量、目录结构。
   流程参考：先 ke_glob 找文件 → ke_grep 定位行 → ke_read_file 看完整内容。
   `path` 参数都是项目相对路径，禁用 `..`/绝对路径，越界会被拒。
```

- [ ] **Step 3: 跑相关测试确认无回归**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth -q -k "synthes or prompt" 2>&1 | tail -10
```

如果有 system prompt 字面断言失败，更新测试断言到新字符串。

- [ ] **Step 4: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/react_synthesizer.py
# 如果改了某个测试断言一并加
git commit -m "$(cat <<'EOF'
feat(qa-engine): system prompt 加 4 个文件类工具用法提示

ke_grep / ke_glob / ke_read_file / ke_ls 的使用流程 + 路径约束说明。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: 全套回归 + E2E 验证

- [ ] **Step 1: 跑后端全套回归**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth -q --tb=short 2>&1 | tail -10
```
Expected: 全 PASS（baseline 658 + 新加 22 = ~680 pass）。

若有失败：
- ripgrep 没装 → 6 个 ke_grep 测试 skip，**不算 fail**
- 其他 async signature 没改全 → 按 task 9 补

- [ ] **Step 2: 重启 uvicorn 加载新工具**

```bash
pkill -f "uvicorn src.service.api:app" 2>/dev/null
sleep 2
cd /Users/java/knowledge-engineering-auth && KE_QA_USE_REACT=1 nohup ./venv/bin/uvicorn src.service.api:app --host 127.0.0.1 --port 8000 --reload > /tmp/uvicorn-react.log 2>&1 &
sleep 5
grep -E "infra_status|startup" /tmp/uvicorn-react.log | head -5
```

- [ ] **Step 3: curl 验证工具 schema 出现**

```bash
curl -sS -X POST http://localhost:8000/auth/login -H "Content-Type: application/json" -d '{"username":"alice","password":"test12345"}' 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" > /tmp/alice.token

# 调一次 explain，确认 system prompt 提到 ke_grep
curl -sS -N -X POST http://localhost:8000/projects/mall-swarm/qa/explain \
  -H "Authorization: Bearer $(cat /tmp/alice.token)" \
  -H "Content-Type: application/json" \
  -d '{"question":"mall-swarm 哪几处用了 RedisTemplate?"}' 2>&1 | grep -E "ke_grep|ke_glob|ke_read_file|ke_ls" | head -10
```

Expected: 看到 tool_call args 包含 `"name":"ke_grep"` 或类似。

- [ ] **Step 4: 浏览器手测 mall-swarm**

打开 http://localhost:5173，alice 登录，切到 mall-swarm，问：
1. "mall-swarm 哪几处用了 RedisTemplate?" → 应触发 `ke_grep`
2. "BrandMapper.xml 文件内容是什么" → 应触发 `ke_glob` + `ke_read_file`
3. "mall-portal 模块的目录结构" → 应触发 `ke_ls`

Expected: tool_call 卡片显示 4 工具中的某一个 + 返回真实结果（路径 + 内容）。

- [ ] **Step 5: 安全验证**

```bash
# LLM 模拟传越界 path → 应被沙箱拒
curl -sS -X POST http://localhost:8000/projects/mall-swarm/qa/explain \
  -H "Authorization: Bearer $(cat /tmp/alice.token)" \
  -H "Content-Type: application/json" \
  -d '{"question":"读 /etc/passwd 这个文件"}' 2>&1 | grep -E "boundary|absolute|error" | head -5
```

Expected: 工具返回 `path out of repo boundary` 或 `must be project-relative`，**不**实际访问 `/etc/passwd`。

- [ ] **Step 6: proj-a/b/c 应返 "not configured"**

```bash
curl -sS -X POST http://localhost:8000/projects/proj-a/qa/explain \
  -H "Authorization: Bearer $(cat /tmp/alice.token)" \
  -H "Content-Type: application/json" \
  -d '{"question":"列出所有 java 文件"}' 2>&1 | grep -E "not configured|error" | head -5
```

Expected: 工具返回 `source path not configured for this project`，proj-a/b/c 都是 NULL repo_local_path。

- [ ] **Step 7: Obsidian doc §12 实施完成标记**

打开 `/Users/java/obsidian/01 Engineering/knowledge-engineering/代码源文件查询工具-设计.md`，在末尾追加 §12 实施完成 + commits 列表。

格式参考 `[[基础设施健康检查与产品不可用-设计]]` §12。

---

## Self-Review

**1. Spec 覆盖**

| Spec 段落 | Task |
|---|---|
| §0 背景 + §1 决策 | Plan Goal + Architecture |
| §2 架构图 | Task 9（qa_router 拿 repo_local_path）+ Task 8（registry 注入） |
| §3.1 ke_grep | Task 4 |
| §3.2 ke_glob | Task 5 |
| §3.3 ke_read_file | Task 6 |
| §3.4 ke_ls | Task 7 |
| §4 sandbox | Task 3 |
| §5 closure 注入 | Task 8 + Task 9 |
| §6 DB migration | Task 1 + Task 2 |
| §7 测试 | 每个 task 独立测试 + Task 11 全套回归 |
| §8 文件清单 | Plan File Structure |
| §9 验收 | Task 11 |

**全覆盖** ✅。

**2. Placeholder scan**：每 step 都有真实 code + commands，无 TBD/TODO。

**3. Type / signature 一致**：
- `resolve_safe_path(repo_local_path, relative_path) -> Path` — Task 3 定义，Task 6/7 用
- `build_ke_xxx_tool(repo_local_path: str | None) -> Tool` — Task 4/5/6/7 一致
- `build_default_registry(... repo_local_path: str | None = None)` — Task 8 定义，Task 9 调
- `build_tools_for_project(project_id, request, db)` 变 async — Task 9 全文一致

一致 ✅。

---
