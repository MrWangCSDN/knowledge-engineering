# TS 重构 Phase 0：基线整理与冻结 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `release-0513`（线上生产代码）确立为唯一移植基线：合回 main 打 tag `py-final-baseline`，产出 TS 移植所需的四件对照物（路由清单 / agent 工具 schema / SSE 协议文档 / eval 基线），终验不移植清单，并对「候选组装收尾 + 最终一次 Python 部署」做用户定夺。

**Architecture:** 不写任何新功能代码。全部工作 = git 分支操作 + 从现有代码/文档**提取**对照物到 `docs/porting/`（移植过程产物，存项目仓）+ Obsidian 状态同步。spec 见 Obsidian `01 Engineering/knowledge-engineering/TS重构-总体路线-设计.md`（status: approved）。

**Tech Stack:** git / Python 3.12 venv（现有）/ pytest / jq。无新依赖。

**执行环境（必读）：**
- 主工作目录：`/Users/java/knowledge-engineering`（main 分支 clone，本计划在此执行）
- 另有 `/Users/java/knowledge-engineering-auth`（同 origin 的另一个 clone，停在 release-0513，是过去的开发目录）——本计划**不在那边改动**，只在收尾时 fetch
- 关键事实：`main` 落后 `release-0513` 424 commits；`release-0513..main` 仅 3 个 6 段式 call_chain commit（决策 #6：不带入基线）；生产部署点为 `a2e168e`（2026-06-08），release-0513 tip `459c58c` 还有 ~16 个未部署 commit（候选树 Task1-6 + 画图修复）

---

### Task 1: 合并 release-0513 → main（main 树 := release-0513 树）

**Files:**
- 无文件改动（纯 git 操作）

策略说明：用「`-s ours` 反向合并」让 main 的树**逐字节等于** release-0513 的树。main 独有的 3 个 call_chain commit（6 段式路径，决策 #6 不迁）保留在历史里但内容不进基线——直接正向 merge 会在 synthesizer/prompts 上撞大量无意义冲突（这些文件在 release-0513 已重构 424 commits）。

⚠️ 副作用（已安排恢复）：本计划文件本身（commit `4071c39`，只在 main 树上）也会被合并覆盖掉——这是预期的，Task 2 Step 4 会在打完 tag 后从 `4071c39` 恢复它再继续执行。执行器在 Task 1-2 期间请以本文件的当前内存副本/transcript 为准。

- [ ] **Step 1: 核验分支差异恰为预期的 3 个 commit**

```bash
cd /Users/java/knowledge-engineering
git fetch origin
git log release-0513..main --oneline
```

预期输出（恰 3 行，全部是 6 段式 call_chain 工作）：
```
daef206 feat(qa): call_chain JSON schema validate + LLM 1-shot 自修
f80c16f feat(qa): call_chain 段升级为 JSON 调用图 + 承载所有 nodes-edges 图
a21d9d4 fix(qa): json-repair 兜底 + GFM 表格 cell 多行 → <br>
```

若出现**第 4 个及以上** commit（例如 `chore: 剔除 Streamlit UI + OWL` 类清理 commit），记下 hash——Step 5 要逐个判断是否需要 cherry-pick 回来。

- [ ] **Step 2: 确认工作区干净 + 本地 release-0513 与远端一致**

```bash
git status --porcelain          # 预期：空输出
git rev-parse release-0513 origin/release-0513   # 预期：两行相同 hash（459c58c…）
```

不一致则先 `git checkout release-0513 && git pull --ff-only && git checkout main`。

- [ ] **Step 3: 执行反向合并（树取 release-0513 全量）**

```bash
git checkout -b baseline-merge release-0513
git merge -s ours main -m "merge: main → py 基线（树=release-0513 生产代码；main 独有 3 个 6段式 call_chain commit 按 TS重构决策#6 不带入内容，仅留历史）

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git checkout main
git merge --ff-only baseline-merge
git branch -d baseline-merge
```

预期：最后一步输出 `Fast-forward`，无冲突（`-s ours` 永不冲突）。

- [ ] **Step 4: 验证 main 树与 release-0513 逐字节一致**

```bash
git diff main release-0513 --stat
```

预期：**空输出**（树完全一致）。非空 = 合并方式错了，`git reset --hard origin/main` 回滚重来。

- [ ] **Step 5: 验证已删除的死代码没有复活**

```bash
git ls-tree -r main --name-only | grep -iE "streamlit|ontology|owl_" ; echo "exit=$?"
```

预期：`exit=1`（无匹配）。若有匹配：说明 Streamlit/OWL 剔除 commit 只在 main 独有侧，执行 `git cherry-pick <Step 1 记下的剔除 commit hash>` 后重跑本步验证。

---

### Task 2: 合并后全量回归 + 打 tag + 冻结声明

**Files:**
- Modify: `README.md`（顶部插入冻结横幅）

- [ ] **Step 1: 同步 venv 依赖（424 commits 间 deps 有增量：minimax/dashscope/docx 等）**

```bash
cd /Users/java/knowledge-engineering
venv/bin/pip install -e ".[dev,auth,neo4j,vector,llm]" -q
test -f requirements.txt && venv/bin/pip install -r requirements.txt -q
venv/bin/python -c "import fastapi, sqlalchemy, weaviate; print('deps ok')"
```

预期：`deps ok`。

- [ ] **Step 2: 跑全量后端测试（这同时就是候选组装老计划的 Task 7「全量回归」）**

```bash
venv/bin/python -m pytest -q 2>&1 | tail -5
```

预期：`0 failed`，passed 数 **≥ 838**（2026-06-05 reactflow 上线时为 838 passed，此后 tip 又加了测试）。e2e 标记默认跳过（pyproject `addopts = "-m 'not e2e'"`），无需真实 LLM/Weaviate。
若有失败：先判断是否环境性失败（缺服务/缺 env）；逻辑性失败必须修复后才能打 tag（修复也提交到 main）。

- [ ] **Step 3: 打基线 tag 并推送**

```bash
git tag -a py-final-baseline -m "Python 后端最终基线（TS 重构移植对照物锚点）：树=release-0513 生产代码。自此 Python 冻结只修 bug，新功能在 ke-server (TS) 实现。spec: Obsidian 01 Engineering/knowledge-engineering/TS重构-总体路线-设计.md"
git push origin main py-final-baseline
```

预期：push 成功，`git tag -l py-final-baseline` 有输出。注意：tag 必须打在恢复计划文件**之前**——保证 tag 树 == release-0513 树逐字节一致。

- [ ] **Step 4: 恢复被合并覆盖的 Phase 0 计划文件**

```bash
git checkout 4071c39 -- docs/superpowers/plans/2026-06-11-ts-rewrite-phase0-baseline-freeze.md
git commit -m "docs(plan): 恢复 Phase 0 计划文件（Task 1 合并取 release-0513 树时被覆盖，预期内）

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: README 顶部加冻结横幅**

在 `README.md` 第一个标题**之后**插入：

```markdown
> [!IMPORTANT]
> **🧊 本仓库已功能冻结（2026-06-11）**：只接受线上 bug 修复，不再新增功能。
> 后端正在重写为 TypeScript（新仓 `ke-server`），新功能一律在 TS 侧实现。
> 移植基线 = tag `py-final-baseline`（树 = release-0513 生产代码）；
> 移植对照物见 `docs/porting/`；总体路线 spec 见 Obsidian
> `01 Engineering/knowledge-engineering/TS重构-总体路线-设计.md`。
```

- [ ] **Step 6: Commit + push**

```bash
git add README.md
git commit -m "docs: 功能冻结横幅 — Python 只修 bug，新功能转 ke-server (TS)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 3: 对照物① — 全路由清单（openapi.json + 摘要表）

**Files:**
- Create: `scripts/export_porting_routes.py`
- Create: `docs/porting/routes-openapi.json`（脚本产物）
- Create: `docs/porting/routes-summary.md`（脚本产物）

- [ ] **Step 1: 写导出脚本**

```python
"""导出 FastAPI 全量 OpenAPI schema + 人读路由摘要表 — TS 移植对照物①。

跑法（KE_DB_URL 只需占位，import 期不连库）：
    KE_DB_URL='mysql+asyncmy://x:x@127.0.0.1:3306/x' venv/bin/python scripts/export_porting_routes.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# import app 前兜底 env：api.py 自带 dotenv 加载，但裸环境跑脚本时 KE_DB_URL 可能为空
os.environ.setdefault("KE_DB_URL", "mysql+asyncmy://x:x@127.0.0.1:3306/x")

from src.service.api import app  # noqa: E402


def main() -> None:
    out_dir = Path("docs/porting")
    out_dir.mkdir(parents=True, exist_ok=True)

    schema = app.openapi()
    (out_dir / "routes-openapi.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 人读摘要表：method / path / summary / 是否需要鉴权（粗判：security 字段）
    lines = [
        "# 路由清单（自动生成，勿手改）",
        "",
        f"来源：tag `py-final-baseline`，共 {len(schema['paths'])} 个 path。",
        "TS 移植以本表盘点覆盖率；6 段式专属路由按决策 #6 标记不迁。",
        "",
        "| Method | Path | Summary |",
        "|---|---|---|",
    ]
    for path, methods in sorted(schema["paths"].items()):
        for method, op in sorted(methods.items()):
            summary = (op.get("summary") or "").replace("|", "\\|")
            lines.append(f"| {method.upper()} | `{path}` | {summary} |")
    (out_dir / "routes-summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"✅ {len(schema['paths'])} paths → docs/porting/routes-openapi.json + routes-summary.md")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 跑脚本并验证产物**

```bash
cd /Users/java/knowledge-engineering
venv/bin/python scripts/export_porting_routes.py
jq '.paths | length' docs/porting/routes-openapi.json
```

预期：打印 `✅ N paths …`；jq 输出 N ≥ 30（api.py 挂了 12+ 个 router：auth/admin/archived/credentials/group/project_member/user/project/qa/code/audit/…）。
若 import 失败：贴出报错——多半是某 router 在模块级读了缺失 env，按报错补 `os.environ.setdefault(...)` 到脚本（与 KE_DB_URL 同位置），不改 src/。

- [ ] **Step 3: Commit**

```bash
git add scripts/export_porting_routes.py docs/porting/routes-openapi.json docs/porting/routes-summary.md
git commit -m "docs(porting): 对照物① 全路由清单 — openapi.json + 摘要表

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: 对照物② — 13 个 agent 工具 I/O schema

**Files:**
- Create: `scripts/export_tool_schemas.py`
- Create: `docs/porting/agent-tools-schema.json`（脚本产物）

注意：`build_default_registry()` 只装 **12** 个工具（5 核心 ke_* + todo_write + 2 可选 + 4 文件类）；`render_call_graph` 是在 SSE 链路里带 `summary_lookup` 单独构建的（commit `69b2b65`），脚本要单独补装。

- [ ] **Step 1: 写导出脚本**

```python
"""导出 13 个 agent 工具的 name/description/input_schema — TS 移植对照物②。

跑法：
    venv/bin/python scripts/export_tool_schemas.py

原理：工具 = 元数据 + handler；导出只读元数据，所以所有后端依赖
（graph/store）一律用 MagicMock 占位，handler 永远不会被调用。
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock

from src.service.qa_engine.tools import build_default_registry
from src.service.qa_engine.tools.render_call_graph import build_render_call_graph_tool


def main() -> None:
    reg = build_default_registry(
        graph=MagicMock(name="graph"),
        interpretation_store=MagicMock(name="interpretation_store"),
        project_id="porting-export",
        code_store=MagicMock(name="code_store"),            # 非 None → ke_read_entity 注册
        method_interp_store=MagicMock(name="method_interp_store"),  # 非 None → ke_method_interp 注册
        repo_local_path=".",                                # 非 None → 4 个文件类工具注册
    )
    tools = list(reg.list_tools())

    # render_call_graph 不在 default registry 里：按真实签名用 MagicMock 补齐必填参数后单独构建
    if "render_call_graph" not in {t.name for t in tools}:
        sig = inspect.signature(build_render_call_graph_tool)
        kwargs = {
            p.name: MagicMock(name=p.name)
            for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        }
        tools.append(build_render_call_graph_tool(**kwargs))

    payload = sorted(
        (
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ),
        key=lambda d: d["name"],
    )
    names = [d["name"] for d in payload]
    assert len(payload) == 13, f"期望 13 个工具，实际 {len(payload)}: {names}"

    out = Path("docs/porting/agent-tools-schema.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 13 tools → {out}\n{names}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 跑脚本并核对 13 个名字**

```bash
venv/bin/python scripts/export_tool_schemas.py
```

预期打印 13 个名字，集合 =：`ke_callees, ke_callers, ke_glob, ke_grep, ke_impact, ke_ls, ke_method_interp, ke_read_entity, ke_read_file, ke_search, ke_table_access, render_call_graph, todo_write`。
若 `list_tools()` 方法名对不上（base.py 实际可能叫别名），先 `grep -n "def " src/service/qa_engine/tools/base.py` 按实际 API 调整脚本。
若 render 构建仍报错（builder 内部访问了 mock 的具体属性）：按报错把对应 MagicMock 换成 `MagicMock(return_value=...)` 配置，目标只有一个——拿到 Tool 元数据。

- [ ] **Step 3: Commit**

```bash
git add scripts/export_tool_schemas.py docs/porting/agent-tools-schema.json
git commit -m "docs(porting): 对照物② 13 个 agent 工具 I/O schema

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: 对照物③ — SSE 事件协议文档

**Files:**
- Create: `docs/porting/sse-protocol.md`
- 只读参考: `src/service/qa_engine/sse_emitter.py`、`src/service/qa_engine/react_synthesizer.py`、`src/service/qa_router.py`、前端消费侧 `/Users/java/knowledge-engineering-web/src/`（字段名以前端实际读取为准）

这是 TS 重构三门禁之首（「SSE 协议逐字段兼容 → 前端零改动」）的依据文档，必须从**两端**提取：后端发了什么 + 前端读了什么。

- [ ] **Step 1: 提取后端事件产生点**

```bash
cd /Users/java/knowledge-engineering
grep -nE "def emit|yield|\"event\"|'event'|event_type|data:" src/service/qa_engine/sse_emitter.py | head -60
grep -nE "emit|_event|section|fold" src/service/qa_engine/react_synthesizer.py | grep -vE "^\s*#" | head -40
grep -nE "EventSourceResponse|StreamingResponse|media_type" src/service/qa_router.py
```

通读三个文件（重点 `sse_emitter.py` 全文 + `fold_render_sections` 实现），记录：每种 event 的名字、payload 字段、出现顺序、终止条件。

- [ ] **Step 2: 提取前端消费点交叉验证**

```bash
grep -rnE "addEventListener|onmessage|event\.|EventSource|fetchEventSource" /Users/java/knowledge-engineering-web/src --include="*.ts" --include="*.vue" -l | head
# 对找到的文件逐个看事件名/字段解构：
grep -rnE "case '|\.event|JSON\.parse\(.*data" /Users/java/knowledge-engineering-web/src --include="*.ts" --include="*.vue" | grep -iE "sse|stream|event" | head -30
```

前端读取的每个字段都进协议文档；**前端没读的字段标注「前端未消费（TS 版仍须保留，防其它消费方）」**。

- [ ] **Step 3: 写 `docs/porting/sse-protocol.md`**，骨架（每节都必须填实测内容，不留空节）：

```markdown
# QA SSE 事件协议（py-final-baseline 提取）

> TS 重构门禁①：TS 版 emitter 输出必须与本文档逐字段一致，前端零改动。

## 1. 传输层
- 端点：POST /api/projects/{pid}/qa/explain（实际以 routes-summary.md 为准）
- Content-Type / 心跳 / 重连语义 / 终止信号

## 2. 事件类型总表
| event | 触发时机 | payload 字段（名/类型/必选） | 顺序约束 | 前端消费点（文件:行） |
|---|---|---|---|---|
（逐事件填：thinking/灰字、section、tool 轮、render_call_graph 图事件、todo、citation、done、error …以实测为准）

## 3. section 与 fold 语义
- sections 结构、at 偏移含义、fold_render_sections 把图按 at 折叠进 sections 的规则
- reopen（会话重开）时持久化 sections 的回放语义

## 4. 边界行为
- LLM 中途失败 / 工具超时（单工具超时值）/ 8 轮护栏触发 / 75s 总护栏触发时各发什么事件
- 客户端断开时后端行为

## 5. 持久化映射
- SSE 流结束后落库的字段（sessions 表 sections JSON 等）与流内事件的对应关系
```

- [ ] **Step 4: 自检 — 用一次真实调用对照（可选但强烈建议，若本机能连 staging）**

```bash
# 若 .env 可用且服务能本地起：起服务后抓一次真实 SSE 流存档
# venv/bin/uvicorn src.service.api:app --port 8001 &
# curl -N -X POST http://127.0.0.1:8001/api/... > docs/porting/sse-sample.txt
# 起不了就跳过本步，文档以代码+前端双向提取为准，并在文档头注明「未抓真实流」
```

- [ ] **Step 5: Commit**

```bash
git add docs/porting/sse-protocol.md
git commit -m "docs(porting): 对照物③ SSE 事件协议（后端产生点+前端消费点双向提取）

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: 对照物④ — eval 基线归档（51 题 + 30 题）

**Files:**
- Create: `docs/porting/eval/qa-51-questions.json`
- Create: `docs/porting/eval/diagram-30-questions.json`
- Create: `docs/porting/eval/baseline.md`

背景：两套 eval 都是历史 Claude 会话编排执行的（**仓库里没有 eval 脚本**）。51 题 = 2026-06-04 P1 gate（7 个并行子代理读真实源码逐题判准）；30 题 = 2026-06-05 reactflow 出图率 eval。题集原文要从 Obsidian 报告 + 历史会话 transcript 里抢救出来。

- [ ] **Step 1: 从 Obsidian 文档找题集**

```bash
grep -nE "^\| ?[0-9]+|^[0-9]+[.、]" "/Users/java/obsidian/01 Engineering/knowledge-engineering/mall-swarm-QA评测报告-2026-06-01.md" | head -25
grep -nE "^\| ?[0-9]+|^[0-9]+[.、]" "/Users/java/obsidian/01 Engineering/knowledge-engineering/mall-swarm-QA评测报告-第二轮-2026-06-01.md" | head -25
grep -nE "题|question" "/Users/java/obsidian/01 Engineering/knowledge-engineering/业务问答-源码优先接地-P1设计.md" | grep -nE "#[0-9]+|清空|收藏" | head
```

- [ ] **Step 2: 从历史会话 transcript 找题集全文（eval 在 -auth 项目目录跑的）**

```bash
ls /Users/wangshanhe/.claude/projects/ | grep knowledge
grep -l "51 题\|出图率" /Users/wangshanhe/.claude/projects/-Users-java-knowledge-engineering-auth/*.jsonl 2>/dev/null | head
# 命中的 transcript 用 grep -o 提取题目串（题目多为中文问句，按 "question" 字段或题号列表上下文人工摘出）
```

- [ ] **Step 3: 组装两个题集 JSON**（统一 schema，TS 版 Phase 3 直接复用）：

```json
{
  "version": "py-final-baseline",
  "source": "P1 eval gate 2026-06-04（恢复自 <具体来源：transcript 文件名或 Obsidian 文档名>）",
  "scoring": "子代理读真实源码逐题判准：准确 / 部分 / 错误 三档",
  "questions": [
    {"id": 1, "domain": "订单", "question": "<题目原文>"}
  ]
}
```

- [ ] **Step 4: 写 `docs/porting/eval/baseline.md` 固化基线数字与判准方法**（数字已核实，直接抄）：

```markdown
# eval 基线（TS 重构门禁②：Phase 3 TS 版重跑须 ≥ 本基线）

## 51 题 QA eval（2026-06-04 P1 gate，KE_QA_USE_REACT=1，agent 自由输出路径）
| 指标 | pre-P1 baseline | P1（= py-final-baseline 行为） |
|---|---|---|
| 准确 | 15 (29%) | **24 (47%)** |
| 部分 | — | 26 (51%) |
| 错误 | 9 | **1 (2%)** |

判准方法：7 个并行子代理按业务域分工，逐题读 mall-swarm 真实源码核对答案。
已知唯一残留错误：#51 互动域 Mongo 被坐实成 SQL（Spring-Data 防臆造未做，TS 版 Phase 6 处理）。

## 30 题出图 eval（2026-06-05 reactflow 御用画图工具上线 eval）
| 指标 | 基线 |
|---|---|
| 出图率 | 30/30 = 100% |
| 折叠进 sections | 30/30 |
| 模式 B 使用次数 | 4 |
| 异常 | 0 |
| 已知残留 | 3/30 agent 多手画 mermaid（前端已 strip 不显示） |

## 题集完整性
- 51 题：<完整恢复 / 部分恢复 N 题（缺失部分见下）>
- 30 题：<同上>
- 缺失处置：<若未能完整恢复，记录用户决策（见计划 Task 6 Step 5）>
```

- [ ] **Step 5: 题集若无法完整恢复 → 问用户**（AskUserQuestion）：
  - 选项 A：以恢复出的部分题集 + 第二轮评测报告 20 题合并去重，作为 Phase 3 门禁题集（基线数字按可恢复子集重新标）
  - 选项 B：Phase 3 前由用户重新出一套固定题集，本次只归档基线数字
  - 把决策写进 `baseline.md`「题集完整性」节

- [ ] **Step 6: Commit**

```bash
git add docs/porting/eval/
git commit -m "docs(porting): 对照物④ eval 基线归档 — 51题/30题题集 + 基线数字 + 判准方法

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: IR 层终验 + 不移植清单

**Files:**
- Create: `docs/porting/no-port-list.md`
- Modify（Obsidian）: `01 Engineering/knowledge-engineering/TS重构-总体路线-设计.md`（§八去掉 IR 的「待终验」括注）

- [ ] **Step 1: 全仓终验 IR 运行时引用**

```bash
cd /Users/java/knowledge-engineering
git grep -nE "from src\.ir|import src\.ir|from src import ir" py-final-baseline -- ':!src/ir' ':!tests'
echo "runtime refs exit=$?"
git grep -lE "from src\.ir" py-final-baseline -- 'tests' | head   # 测试引用单列（不算运行时）
```

预期：第一条命令空输出、`exit=1`。**若有命中**：逐个看是真运行时依赖还是死 import——真依赖则 IR 必须移植，更新 spec §八并停下来向用户说明（这改变 Phase 2 范围）。

- [ ] **Step 2: 写 `docs/porting/no-port-list.md`**

```markdown
# 不移植清单（py-final-baseline → ke-server）

> 终态依据：spec §八。本文档记录 Phase 0 验证结论与证据。

| 项 | 验证结论 | 证据 |
|---|---|---|
| `src/ir/`（整层） | 运行时零引用（service/pipeline/knowledge/scripts 均无 import；仅 tests 引用 N 处） | 本计划 Task 7 Step 1 命令输出 |
| 6 段式 synthesizer 全链路 | 决策 #6 agent-only；main 独有 3 commit（daef206/f80c16f/a21d9d4）内容未进基线 | Task 1 合并记录 |
| `stub_retriever.py` | dev 桩，TS 侧按需重建 | — |
| Streamlit UI / OWL 推理 | 基线树中已不存在 | Task 1 Step 5 验证输出 |
| KE_QA_USE_REACT 开关 | TS 版恒为 agent 路径，开关消失 | 决策 #6 |
```

（表中「N 处」「验证输出」填 Step 1 实测结果，不许留模板字样。）

- [ ] **Step 3: 更新 Obsidian spec §八 IR 行**

把 `**src/ir/**（待 Phase 0 终验无运行时引用）` 改为 `**src/ir/**（Phase 0 已终验：运行时零引用，证据见老仓 docs/porting/no-port-list.md）`。

- [ ] **Step 4: Commit（两个仓）**

```bash
git add docs/porting/no-port-list.md
git commit -m "docs(porting): 不移植清单 — IR 终验运行时零引用 + 6段式/桩/死代码

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
cd /Users/java/obsidian && git add "01 Engineering/knowledge-engineering/TS重构-总体路线-设计.md" && git commit -m "docs(ke): TS重构spec §八 IR 终验完成" && git push && cd /Users/java/knowledge-engineering
```

---

### Task 8: 候选组装收尾 + 最终一次 Python 部署 — 用户定夺

**Files:**
- Modify（Obsidian）: `01 Engineering/knowledge-engineering/候选按调用顺序组装-设计.md`（状态行）
- Modify（Obsidian）: `01 Engineering/knowledge-engineering/_overview.md`（候选组装条目状态）

事实先摆齐（执行时原样呈现给用户）：
- 候选组装老计划 10 任务：**Task 1-6（全部代码+测试）已 commit**（`3baad5e`…`cdd3c7f`）；Task 7 全量回归已由本计划 Task 2 Step 2 覆盖；剩 Task 8 部署+10 题手测（需授权）、Task 9 可选 30 题重跑、Task 10 Obsidian 标记
- 生产部署点 `a2e168e`（2026-06-08）；基线 tip 含 **~16 个未部署 commit** = 候选树 Task1-6 + render_call_graph 系列修复（freeform 边校验、入口剥 scheme、防 narrate 退化等）
- 矛盾点：不部署 → 生产 ≠ `py-final-baseline`，TS「对齐生产」与「对齐基线」出现尾差；热回滚兜底回切的也是旧码

- [ ] **Step 1: AskUserQuestion 定夺**
  - **选项 A（推荐）：冻结版做最后一次 Python 部署** —— 按 `docs/deploy/runbook-2026-05-21-release-0513.md` + Obsidian [[生产部署-蓝队云]] 流程把 `py-final-baseline` 部署上线（重启 ke-api），10 题手测核验。生产 = 基线，TS 对齐无歧义；候选树 + 画图修复顺带上线；P1 源码接地与 `6bf143a` tenant 修复也随重启生效（Obsidian 开放问题②落账）。**部署操作单独排期执行，须用户在场授权，不在本计划内自动做。**
  - 选项 B：不部署 —— 生产停在 `a2e168e`，`baseline.md` 与 spec 记录尾差清单（16 commit 列表），TS Phase 3 对齐基线而非生产。
- [ ] **Step 2: 按决策更新 Obsidian**
  - 候选组装设计文档状态行：A → `已实施（Task1-6 commit 至基线）+ 随冻结版部署待执行`；B → `已实施进基线，未部署（生产尾差记录见老仓 docs/porting/）`
  - `_overview.md` 设计文档目录对应条目同步；开放问题里 P1 重启上线条目按决策更新
- [ ] **Step 3: Obsidian commit + push**

```bash
cd /Users/java/obsidian && git add -A "01 Engineering/knowledge-engineering/" && git commit -m "docs(ke): 候选组装状态落账 + 最终 Python 部署决策（Phase 0 Task 8）" && git push && cd /Users/java/knowledge-engineering
```

---

### Task 9: Phase 0 收口

**Files:**
- Modify（Obsidian）: `01 Engineering/knowledge-engineering/_overview.md`（开放问题首条更新为 Phase 0 完成）

- [ ] **Step 1: 退出标准自检（全部勾上才算完）**

```bash
cd /Users/java/knowledge-engineering
git tag -l py-final-baseline                  # ① tag 在
ls docs/porting/                              # ② 四件套在：routes-*.{json,md} / agent-tools-schema.json / sse-protocol.md / eval/ / no-port-list.md
git diff py-final-baseline main --name-only   # ③ 只允许出现 Phase 0 产物：README.md / docs/porting/* / docs/superpowers/plans/2026-06-11-* / scripts/export_*（以及 Task 2 Step 2 若做过回归修复的文件）
git log origin/main..main --oneline | head -1 # ④ 空 = 全部已推送
```

- [ ] **Step 2: -auth 目录同步元数据（不切分支不改文件）**

```bash
cd /Users/java/knowledge-engineering-auth && git fetch origin && cd /Users/java/knowledge-engineering
```

- [ ] **Step 3: 更新 Obsidian `_overview.md` 开放问题首条**：「Phase 0 已完成（tag `py-final-baseline` + 四件套 + 不移植终验 + 候选组装落账）。下一步 = Phase 1 新仓 `ke-server` 脚手架（届时走 writing-plans）」；commit + push

- [ ] **Step 4: 向用户汇报**：tag、四件套路径、回归结果、Task 6/8 两个决策结论、Phase 1 入口提示

---

## 自审记录（写计划时已跑）

1. **Spec 覆盖**：spec Phase 0 五项 — 合分支打 tag（Task 1-2）、冻结声明（Task 2）、四件对照物（Task 3-6）、IR 终验（Task 7）、候选组装定夺（Task 8）— 全覆盖，另补了 spec 漏的「生产尾差 16 commit」决策（并入 Task 8）。
2. **占位符**：Task 5/6/7 的文档骨架均标注「填实测内容，不许留模板字样」；无 TBD。
3. **一致性**：tag 名 `py-final-baseline` 全文统一；工具数 13 = registry 12 + render 单装，与 Task 4 脚本断言一致；路径统一 `docs/porting/`。
4. **已知不确定点（执行时按步内 fallback 处理）**：① `release-0513..main` 是否恰 3 commit（Task 1 Step 1 显式核验）；② `list_tools()` 实际方法名（Task 4 Step 2 fallback）；③ 51 题题集能否完整恢复（Task 6 Step 5 用户决策）。
5. **时序坑（已修复进计划）**：本计划文件 commit 在合并**之前**的 main 上，`-s ours` 合并会把它从树上冲掉 → Task 2 Step 4 在打完 tag 后从 `4071c39` 恢复；tag 因此保持与 release-0513 树逐字节一致；Task 9 ③ 验收命令相应改为「diff 基线只允许 Phase 0 产物」。
