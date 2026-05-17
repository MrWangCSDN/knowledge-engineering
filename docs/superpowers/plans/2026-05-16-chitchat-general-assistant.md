# chit-chat 通用助手化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重写 `_CHIT_CHAT_SYSTEM` prompt，让没路由到 4 个具体 skill 的通用编程/技术问题（如"用 java 实现排序"）得到直接、有用的回答，而非死板套话；问候/产品问询行为保持。

**Architecture:** 纯 prompt 重写（`src/service/qa_engine/prompts.py` 单个常量）。路由逻辑、synthesizer chit-chat 路径均不变（已确认 synthesizer 无 token/长度硬限制，唯一长度约束在 prompt 文字内，本次删除）。

**Tech Stack:** Python / pytest

**Spec:** `/Users/java/obsidian/01 Engineering/knowledge-engineering/chit-chat-闲聊路径-设计.md`

**Repo:** `/Users/java/knowledge-engineering-auth`（运行中后端 uvicorn pid 61190 --reload）

---

## File Structure

| 文件 | 改动 |
|---|---|
| `src/service/qa_engine/prompts.py` | 重写 `_CHIT_CHAT_SYSTEM` 常量（旧规则删，新规则写） |
| `tests/test_auth/test_chit_chat_prompt.py` | 🆕 characterization 回归测试（旧死板规则不得在；新通用助手指令须在） |

---

## Task 1: 重写 _CHIT_CHAT_SYSTEM（TDD）

**Files:**
- Create: `tests/test_auth/test_chit_chat_prompt.py`
- Modify: `src/service/qa_engine/prompts.py`

- [ ] **Step 1: 写 characterization 回归测试（先写，TDD）**

新建 `tests/test_auth/test_chit_chat_prompt.py`：

```python
"""_CHIT_CHAT_SYSTEM prompt 行为契约 characterization 测试。

设计：[[chit-chat-闲聊路径-设计]] §2.3, §5
v1.3：chit-chat 从"死板引导回 4 能力"扩为"通用编程助手直接答"。
prompt 内容无法做行为单测（要真调 LLM），用文本契约兜底防回归：
  - 旧死板规则文字不得残留
  - 新"直接回答通用技术问题"指令须存在
  - 产品问询仍保留 4 个 KG 能力介绍
"""
from src.service.qa_engine.prompts import _CHIT_CHAT_SYSTEM


def test_old_rigid_deflection_rule_removed():
    # v1.2.1 的死板规则原文，v1.3 必须删除
    assert "不要回答与代码工程无关的问题" not in _CHIT_CHAT_SYSTEM
    # 旧的"1-3 句"硬长度限制也删除（通用问题答案需要长度+代码块）
    assert "1-3 句" not in _CHIT_CHAT_SYSTEM


def test_general_tech_questions_answered_directly():
    # 新行为：必须明确指示"直接/完整回答通用编程/技术问题"
    assert "通用编程" in _CHIT_CHAT_SYSTEM
    # 必须允许代码块/不限长度（任一关键词出现即可）
    assert ("代码块" in _CHIT_CHAT_SYSTEM) or ("Markdown" in _CHIT_CHAT_SYSTEM)


def test_product_query_still_introduces_4_abilities():
    # 产品问询行为保留：4 个 KG 能力仍在 prompt 里
    for kw in ["业务规则", "调用链路", "数据流", "架构"]:
        assert kw in _CHIT_CHAT_SYSTEM


def test_greeting_behavior_kept():
    # 问候仍要求简短友好（关键词存在即可）
    assert "问候" in _CHIT_CHAT_SYSTEM
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_chit_chat_prompt.py -q`
Expected: FAIL —— `test_old_rigid_deflection_rule_removed` 失败（旧 prompt 仍含"不要回答与代码工程无关的问题"和"1-3 句"）；`test_general_tech_questions_answered_directly` 失败（旧 prompt 无"通用编程"）

- [ ] **Step 3: 重写 _CHIT_CHAT_SYSTEM**

在 `src/service/qa_engine/prompts.py` 中，将整个 `_CHIT_CHAT_SYSTEM = """..."""` 常量**完全替换**为：

```python
_CHIT_CHAT_SYSTEM = """你是 KE（代码知识工程）的对话助手，同时也是一个有用的编程/技术助手。

回复原则：
1. 问候 / 道谢 / 告别 → 简短、友好、自然（一两句即可）
2. 产品问询（「你是谁 / 能做什么 / KE 是什么」）→ 介绍你的 4 个核心能力（针对用户已接入的代码库）：
   - 业务规则（约束 / 校验 / 限制）
   - 调用链路（谁调了谁 / 依赖）
   - 数据流（写到哪些表 / 数据如何流转）
   - 整体架构（系统是什么 / 怎么实现）
3. 通用编程 / 技术问题（如「用 Java 写个排序」「解释下快排」「什么是闭包」）→ 像专业编程助手一样**直接、完整地回答**：可用 Markdown、代码块，长度按需要来，不必刻意简短，也不要推脱或搪塞
4. 与技术完全无关的问题（天气 / 八卦 / 闲聊故事）→ 简短自然地回应，可以顺口提一句你更擅长分析代码库，但语气轻松，别生硬地背诵能力清单

整体语气：自然、专业、有帮助。绝不用「我专注于代码知识查询，比如业务规则、调用链路、数据流和架构」这种死板套话去搪塞具体的技术问题。

示例：
- 用户「你好」→「你好！有什么编程或代码方面的问题都可以问我。」
- 用户「你能做什么」→「我是 KE 代码知识工程助手。除了回答通用编程问题，我更擅长分析你已接入的代码库——业务规则、调用链路、数据流转、整体架构都能问我。」
- 用户「用 Java 写个冒泡排序」→（直接给出带注释的完整 Java 代码 + 简要说明，不要推脱）
- 用户「今天天气怎么样」→「天气我看不了，不过编程或者你代码库的问题都可以找我聊。」
"""
```

⚠️ 实施者：确认替换后文件其余部分（其他 prompt 常量、import）不受影响；`_CHIT_CHAT_SYSTEM` 仍是模块级 `str` 常量，被 `synthesizer.py` import 使用，名字不变。

- [ ] **Step 4: 跑测试，确认全过**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_chit_chat_prompt.py -q`
Expected: 4 passed

- [ ] **Step 5: 语法自检（venv 内 import 验证）**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -c "from src.service.qa_engine.prompts import _CHIT_CHAT_SYSTEM; print('len=', len(_CHIT_CHAT_SYSTEM))"`
Expected: 打印 `len= <一个 > 200 的整数>`

- [ ] **Step 6: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/prompts.py tests/test_auth/test_chit_chat_prompt.py
git commit -m "feat(qa): chit-chat v1.3 — 通用编程问题直接答，不再死板套话（TDD 4 测试）"
```

---

## Task 2: 路由回归 + 后端健康

**Files:** 无新文件

- [ ] **Step 1: 跑 chit-chat 路由相关测试，确认路由逻辑没回归**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_qa_router_classifier.py -q`
Expected: 全 passed（本次只改 prompt 文本，路由分类逻辑未动，不应回归）

⚠️ 实施者：若该文件名不存在，用 `ls tests/test_auth/ | grep -i "classif\|router\|chit"` 找到 chit-chat 路由测试文件名后再跑。

- [ ] **Step 2: 确认运行中后端 --reload 已加载（pid 61190）**

Run: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/openapi.json`
Expected: `200`

- [ ] **Step 3: 无新 commit（验证用）**

---

## Task 3: Preview MCP E2E 验证（controller 执行）

由控制器（主 agent）用 Claude Preview MCP 跑（subagent 无 MCP 上下文，不在 subagent 范围）：

- [ ] 启动 ke-web preview，登录 admin/admin12345
- [ ] 新对话问「用 Java 写个冒泡排序」→ **应得到带注释的真实 Java 代码**（不是"我专注于代码知识查询…"套话）
- [ ] 新对话问「你好」→ 仍简短友好（不长篇大论）
- [ ] 新对话问「你能做什么」→ 仍介绍 4 个 KG 能力
- [ ] 新对话问「今天天气怎么样」→ 简短自然 + 轻松提一句擅长代码库（不死板背能力清单）
- [ ] 截图留证

---

## Self-Review 检查项（实施者跑完过一遍）

- [ ] 设计 §2.3 行为表（问候/产品问询/通用技术/无关）→ Task 1 新 prompt 4 条规则 ✓
- [ ] 设计 §2.3 删除旧规则（"1-3 句"、"不要回答…引导回能力"）→ Task 1 测试 `test_old_rigid_deflection_rule_removed` 守住 ✓
- [ ] 设计 §3 范围（仅 prompt，不动路由/synthesizer）→ Task 1 只改 prompts.py；Task 2 验证路由不回归 ✓
- [ ] 设计 §5 测试策略（路由不变 / prompt 不变量 / E2E）→ Task 1+2+3 ✓
- [ ] 无 placeholder：新 prompt 全文已写死在 Task 1 Step 3 ✓

## Phase Definition of Done

- [ ] `test_chit_chat_prompt.py` 4 测试全 pass
- [ ] chit-chat 路由测试无回归
- [ ] 后端 --reload 健康（openapi 200）
- [ ] Preview MCP：「用 Java 写冒泡排序」得到真实代码、「你好」仍简短、「能做什么」仍介绍 4 能力
- [ ] 单 commit 干净（prompt + 测试一起）
