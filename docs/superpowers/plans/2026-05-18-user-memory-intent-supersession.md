# 用户级显式写入：意图解析 + kind 驱动取代 + 触发健壮 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修「会话A改名李龙飞→同工程新会话仍答王山河」：触发门句首/句尾健壮 + 轻量 LLM 意图解析（tier/kind/规范化/supersedes）+ kind 驱动 identity 单例硬取代。

**Architecture:** 保留 KE 廉价触发门（§6/§14 成本决策，不每轮 LLM），门控通过后对内容跑一次轻量 LLM 解析（分类/规范化/取代＝Claude Code 精髓），identity 写前归档同 user 旧 active identity（软删）。3 处协作改动 + prompt 常量，无迁移。

**Tech Stack:** Python / SQLAlchemy(duck-typed) / pytest（沿用 tests/test_auth 既有 `_FakeMemDB`/`_FakeMemLLM`/`_FakeMsg`/`_FakeDB` + 固定返回/捕获型 fake）。

**Spec（单一来源）：** `/Users/java/obsidian/01 Engineering/knowledge-engineering/记忆系统-设计.md` §22（父设计 §4.1/§6/§7）。

**Repo:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`（沿用，无 worktree）。逐任务提交（用户本会话已授权后端记忆系统逐任务 commit）。

---

## 现状基线（已核对真实代码 2026-05-18）

- `src/service/memory/service.py`
  - `_TRIGGERS = ("请记住", "记住", "记一下", "记下", "帮我记住")`（L24）
  - `detect_explicit_memory`（L27-39）：仅 `q.startswith(trig)` → `rest=q[len(trig):]`；`rest=rest.lstrip(" :：\t").strip()`；`return rest or None`。
  - `write_explicit_memory(db, *, user_id, session_id, content)`（L127-143）：裸 `db.add(QAUserMemory(user_id, kind="preference", content, source="explicit", source_session_id=session_id, status="active"))` + `await db.commit()`。无取代。
  - `recall_memory_block`（L66-124）：用户级 `select(QAUserMemory).where(user_id==, status=="active").order_by(created_at)` 全量；不改。
- `src/service/qa_router.py` `_make_memory_writer`（L531-573）：用户级分支 L555-561 `content=detect_explicit_memory(question); if content: await write_explicit_memory(db, user_id=, session_id=, content=content)`。`llm` 已是该工厂入参（L531，给 maybe_compact_session 用）。工程级分支 L546-554 不动。
- `src/service/qa_engine/prompts.py`：`_SESSION_COMPACT_SYSTEM`（L369-376，括号隐式拼接风格）在 `# ─── 记忆系统 P1` 区；前有 2 行注释 L367-368。
- 调用点：`write_explicit_memory` 仅 `qa_router.py:46`(import)/`:559`(call) + `tests/test_auth/test_memory_service.py:243`(`test_write_explicit_adds_user_memory_row`：`write_explicit_memory(db, user_id=7, session_id="s1", content="我用 Java")` → `assert len(db.added)==1`)。**故 `write_explicit_memory` 采用「追加可选参数」非破坏式**：`kind="preference", supersedes_kind=None` 默认 → 旧调用/旧测试零改动仍绿。
- 测试 fake：`tests/test_auth/test_memory_service.py` `_FakeMemDB(user_rows,session_row,msg_rows,project_rows)`（`execute` 按 `stmt.column_descriptions[0]["entity"]` 分派：QAUserMemory→user_rows / QASessionMemory→[session_row]|[] / QAProjectMemory→project_rows / else msg_rows；`.added`/`.committed`/`add`/`commit`）、`_FakeMemLLM(reply=...)`（`async complete(*,system,user,**kw)→reply` 固定）、`_FakeMsg`。`tests/test_auth/test_memory_router_hook.py` `_FakeDB(msg_rows)`（QAMessage→msg_rows 否则 `_FakeResult([])`；`.added`/`.committed`）、`_FakeLLM`（`complete`→非 JSON 串）。⚠️ fake 的 `execute` 不套用 SQL WHERE → 实现侧归档循环须 Python 再 guard `kind/status`，real SQL 下 WHERE 已限定、guard 无害。

---

## File Structure

| 文件 | 改动 |
|---|---|
| `src/service/qa_engine/prompts.py` | 新增模块级常量 `_USER_MEM_INTENT_SYSTEM`（紧随 `_SESSION_COMPACT_SYSTEM` 之后） |
| `src/service/memory/service.py` | (a) `detect_explicit_memory` 前缀**或**句尾后缀；(b) 新增 `parse_user_memory_intent(llm, content)`；(c) `write_explicit_memory` 加可选 `kind`/`supersedes_kind` + identity 单例归档取代 |
| `src/service/qa_router.py` | `_make_memory_writer` 用户级分支：detect→parse→write（skip 不写）；工程级/压缩/try-except 不动 |
| `tests/test_auth/test_qa_prompts.py` | 追加 `_USER_MEM_INTENT_SYSTEM` characterization |
| `tests/test_auth/test_memory_service.py` | 追加 detect 前后缀 / parse 正常+兜底 / write identity 取代+preference 累加 / recall 取代后只剩新值 |
| `tests/test_auth/test_memory_router_hook.py` | 追加 端到端：句尾 identity→取代写、skip 不写、解析失败兜底；既有 hook 测试不回归 |

---

## Task 1: `detect_explicit_memory` 前缀**或**句尾后缀

**Files:** Modify `src/service/memory/service.py:27-39`; Test `tests/test_auth/test_memory_service.py`

- [ ] **Step 1: 写失败测试** —— 追加到 `tests/test_auth/test_memory_service.py` 末尾：

```python
# ───────── §22.3：detect_explicit_memory 前缀或句尾后缀 ─────────

def test_detect_prefix_still_works_no_regression():
    assert detect_explicit_memory("记住我喜欢简短回答") == "我喜欢简短回答"
    assert detect_explicit_memory("请记住：我用 Java") == "我用 Java"


def test_detect_suffix_trailing_trigger():
    # 用户实测说法：触发词在句尾
    assert detect_explicit_memory("我改名叫李龙飞 请记住") == "我改名叫李龙飞"
    assert detect_explicit_memory("以后叫我李龙飞 记住") == "以后叫我李龙飞"


def test_detect_suffix_strips_trailing_punctuation():
    assert detect_explicit_memory("我改名叫李龙飞，请记住。") == "我改名叫李龙飞"
    assert detect_explicit_memory("我喜欢简短回答！记一下！") == "我喜欢简短回答"


def test_detect_prefix_priority_when_both():
    # 前缀优先：句首是触发词则按前缀剥
    assert detect_explicit_memory("记住我改名叫李龙飞 请记住") == "我改名叫李龙飞"


def test_detect_none_and_empty():
    assert detect_explicit_memory("今天天气不错") is None
    assert detect_explicit_memory("请记住") is None        # 只有触发词无内容
    assert detect_explicit_memory("请记住。") is None       # 触发词+标点无内容
    assert detect_explicit_memory("") is None
    assert detect_explicit_memory("我不需要你记住这个东西") is None  # 触发词在中间不算
```

- [ ] **Step 2: 跑，确认失败** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q -k "detect_suffix or detect_prefix_priority or detect_none_and_empty or detect_prefix_still"`
  Expected: FAIL —— `test_detect_suffix_trailing_trigger` 失败（现状仅前缀，句尾返回 None）。

- [ ] **Step 3: 实现** —— `src/service/memory/service.py`，把 L27-39 整个 `detect_explicit_memory` 函数：

```python
def detect_explicit_memory(question: str) -> str | None:
    """从用户问题里检测显式记忆意图。

    命中触发词 → 返回剥离触发词后的内容（去首尾空白与起始的中英文冒号）。
    未命中 / 内容为空 → None。
    """
    q = (question or "").strip()
    for trig in _TRIGGERS:
        if q.startswith(trig):
            rest = q[len(trig):]
            rest = rest.lstrip(" :：\t").strip()
            return rest or None
    return None
```

替换为（§22.3 精确语义，**最终版** = 对齐 commit c9d49b0：两轮 TDD/review 收尾——endswith 最长触发词优先、前缀内环 len 守卫、`_CONTENT_RSTRIP` DRY、`_TRAILING_PUNCT` 含 `；;`）：

```python
# 句尾后缀判定前先剥掉的尾部空白与中英文标点（不影响前缀分支）。含中英文分号。
_TRAILING_PUNCT = " 　\t。，、！？.!?,；;"
# 内容右侧清理集（冒号分隔约定残留 + 尾部标点）；DRY：前缀/后缀两分支复用。
_CONTENT_RSTRIP = " :：\t，、," + _TRAILING_PUNCT
# endswith 匹配必须最长触发词优先：「记住」是「请记住」「帮我记住」的后缀，
# 按 _TRIGGERS 原序匹配会把「帮我记住」误剥成「帮我」+「记住」。
# startswith 分支不受影响（5 个触发词互不为前缀，原序安全）。
_TRIGGERS_BY_LEN = tuple(sorted(_TRIGGERS, key=len, reverse=True))


def detect_explicit_memory(question: str) -> str | None:
    """从用户问题里检测显式记忆意图（§22.3：句首前缀 或 句尾后缀）。

    前缀命中：content = 触发词之后；再剥 rest 末尾多余触发词（如「记住A 请记住」→「A」），
    但内容恰为触发词本身时不剥空（len 守卫，保旧行为「请记住记住」→「记住」）。
    句尾后缀命中：q 去右侧空白与中英文标点后 endswith(触发词) → content = 其之前部分。
    endswith 匹配按触发词长度降序（_TRIGGERS_BY_LEN），避免「帮我记住」被「记住」抢匹配。
    两侧 content 再清理；空 → None。前缀优先（都命中按前缀）。
    未命中 → None。"任意位置包含"不算（"我不需要你记住" 不误判）。
    """
    q = (question or "").strip()
    if not q:
        return None
    for trig in _TRIGGERS:
        if q.startswith(trig):
            rest = q[len(trig):].lstrip(" :：\t").strip()
            # 前缀命中后剥掉 rest 自身末尾多余的触发词（如「记住A 请记住」→「A」）；
            # 内容恰为触发词本身（len 不大于触发词）则不剥（「请记住记住」→「记住」）。
            rest_tail = rest.rstrip(_TRAILING_PUNCT)
            for t2 in _TRIGGERS_BY_LEN:
                if rest_tail.endswith(t2) and len(rest_tail) > len(t2):
                    rest = rest_tail[: -len(t2)].rstrip(_CONTENT_RSTRIP).strip()
                    break
            else:
                rest = rest_tail.strip()
            return rest or None
    q_tail = q.rstrip(_TRAILING_PUNCT)
    for trig in _TRIGGERS_BY_LEN:
        if q_tail.endswith(trig) and len(q_tail) > len(trig):
            rest = q_tail[: -len(trig)].rstrip(_CONTENT_RSTRIP).strip()
            return rest or None
    return None
```

- [ ] **Step 4: 跑，确认通过** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
  Expected: 全 passed（新增 5 + 既有 detect/recall/compact/project 不回归——前缀分支逐字节等价，仅新增句尾分支）。

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/memory/service.py tests/test_auth/test_memory_service.py
git commit -m "$(cat <<'EOF'
feat(memory): §22.3 detect_explicit_memory 支持句尾触发词（修「…请记住」尾缀丢，TDD）

仅前缀→前缀或句尾后缀（去右侧空白与中文标点后 endswith），前缀优先；
"任意位置包含"不取（防「我不需要你记住」误报）。前缀分支逐字节等价。设计 §22.3。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `_USER_MEM_INTENT_SYSTEM` prompt 常量

**Files:** Modify `src/service/qa_engine/prompts.py`（紧随 `_SESSION_COMPACT_SYSTEM` L376 之后）; Test `tests/test_auth/test_qa_prompts.py`

- [ ] **Step 1: 写失败测试** —— 追加到 `tests/test_auth/test_qa_prompts.py` 末尾：

```python
# ───────── §22.4：_USER_MEM_INTENT_SYSTEM 意图解析 prompt characterization ─────────
from src.service.qa_engine.prompts import _USER_MEM_INTENT_SYSTEM


def test_user_mem_intent_system_contract():
    s = _USER_MEM_INTENT_SYSTEM
    # 四类判定
    assert "identity" in s and "preference" in s and "style_feedback" in s and "skip" in s
    # 严格 JSON 输出 + 四字段
    assert "JSON" in s
    for k in ("tier", "kind", "content", "supersedes_kind"):
        assert k in s
    # 规范化为单句陈述事实 + identity 必带取代信号 + CC「先更新不重复」同款
    assert "单句" in s or "一句" in s
    assert "supersedes_kind" in s
    assert "只输出" in s and ("不要解释" in s or "无解释" in s)
```

- [ ] **Step 2: 跑，确认失败** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_qa_prompts.py -q -k user_mem_intent`
  Expected: FAIL —— `ImportError: cannot import name '_USER_MEM_INTENT_SYSTEM'`。

- [ ] **Step 3: 实现** —— `src/service/qa_engine/prompts.py`，找到（L369-376）：

```python
_SESSION_COMPACT_SYSTEM = (
    "你是对话记忆压缩器。基于【已有会话摘要】（若有）与【新增对话】，输出一段"
    "更新后的会话摘要，忠实保留对后续有用的关键信息：用户陈述的事实与偏好"
    "及其先后/演变时间线、已确认的结论、当前状态、未决问题。"
    "不得丢弃【已有会话摘要】中的既有事实——把新信息融合进去，有变化则标注演变。"
    "不超过 300 字，中文，直接输出摘要正文，不要前缀、不要解释、不要分点编号。"
)
```

在该 `)` 之后另起空行追加：

```python


# 用户级显式记忆意图解析（轻量，仅显式记忆门控通过后调一次）。
# 设计：[[记忆系统-设计]] §22.4。CC extractMemories「不要写重复、先更新」同款指令。
_USER_MEM_INTENT_SYSTEM = (
    "你是用户记忆意图解析器。给你一段用户希望被记住的话，判定并输出严格 JSON。"
    "字段：tier（取 'user' 或 'skip'：值得长期记住关于这个用户的事 → user；"
    "无意义/临时/不该长期记 → skip）；"
    "kind（'identity'=用户身份/姓名/自我称呼/角色；'preference'=长期偏好；"
    "'style_feedback'=对回答风格的反馈）；"
    "content（把这句话规范化为一句第三人称陈述事实，如『用户的名字是李龙飞』）；"
    "supersedes_kind（若本条是身份类、会取代该用户既有身份事实 → 'identity'，否则 null）。"
    "规则：身份类（改名/我叫/称呼我）kind 必为 identity 且 supersedes_kind 必为 'identity'"
    "（先更新旧的、不要并存重复）；只输出 JSON 对象本身，不要代码块、不要解释、不要多余文字。"
    '示例输出：{"tier":"user","kind":"identity","content":"用户的名字是李龙飞","supersedes_kind":"identity"}'
)
```

- [ ] **Step 4: 跑，确认通过** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_qa_prompts.py -q`
  Expected: 全 passed（新 characterization + 既有 prompts 测试不回归——纯新增常量）。

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/prompts.py tests/test_auth/test_qa_prompts.py
git commit -m "$(cat <<'EOF'
feat(memory): §22.4 新增 _USER_MEM_INTENT_SYSTEM 用户记忆意图解析 prompt（TDD）

判 identity/preference/style_feedback/skip + 规范化单句 + identity 必带 supersedes
+ 严格 JSON 无解释（CC「不重复先更新」同款）。设计 §22.4。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `parse_user_memory_intent(llm, content)` 轻量解析 + 兜底

**Files:** Modify `src/service/memory/service.py`（新增函数 + import 常量）; Test `tests/test_auth/test_memory_service.py`

- [ ] **Step 1: 写失败测试** —— 追加到 `tests/test_auth/test_memory_service.py` 末尾：

```python
# ───────── §22.4：parse_user_memory_intent 解析 + 兜底 ─────────
from src.service.memory.service import parse_user_memory_intent


class _JsonLLM:
    def __init__(self, reply): self._reply = reply
    async def complete(self, *, system, user, **kw): return self._reply


class _RaiseLLM:
    async def complete(self, *, system, user, **kw): raise RuntimeError("llm down")


@pytest.mark.asyncio
async def test_parse_valid_json():
    llm = _JsonLLM('{"tier":"user","kind":"identity",'
                    '"content":"用户的名字是李龙飞","supersedes_kind":"identity"}')
    r = await parse_user_memory_intent(llm, "我改名叫李龙飞")
    assert r == {"tier": "user", "kind": "identity",
                 "content": "用户的名字是李龙飞", "supersedes_kind": "identity"}


@pytest.mark.asyncio
async def test_parse_strips_code_fence():
    llm = _JsonLLM('```json\n{"tier":"user","kind":"preference",'
                   '"content":"用户偏好简短回答","supersedes_kind":null}\n```')
    r = await parse_user_memory_intent(llm, "我喜欢简短回答")
    assert r["kind"] == "preference" and r["supersedes_kind"] is None


@pytest.mark.asyncio
async def test_parse_skip():
    llm = _JsonLLM('{"tier":"skip","kind":"preference","content":"x","supersedes_kind":null}')
    r = await parse_user_memory_intent(llm, "嗯嗯")
    assert r["tier"] == "skip"


@pytest.mark.asyncio
async def test_parse_invalid_json_falls_back():
    # 非 JSON → 兜底：原样 content 当 preference，不丢意图
    llm = _JsonLLM("好的我记住了")
    r = await parse_user_memory_intent(llm, "我喜欢简短回答")
    assert r == {"tier": "user", "kind": "preference",
                 "content": "我喜欢简短回答", "supersedes_kind": None}


@pytest.mark.asyncio
async def test_parse_llm_raises_falls_back():
    r = await parse_user_memory_intent(_RaiseLLM(), "我用 Java")
    assert r == {"tier": "user", "kind": "preference",
                 "content": "我用 Java", "supersedes_kind": None}


@pytest.mark.asyncio
async def test_parse_bad_enum_or_missing_keys_falls_back():
    llm = _JsonLLM('{"tier":"weird","kind":"nope"}')   # 非法枚举/缺字段
    r = await parse_user_memory_intent(llm, "我用 Java")
    assert r == {"tier": "user", "kind": "preference",
                 "content": "我用 Java", "supersedes_kind": None}
```

- [ ] **Step 2: 跑，确认失败** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q -k parse_`
  Expected: FAIL —— `ImportError: cannot import name 'parse_user_memory_intent'`。

- [ ] **Step 3: 实现** —— `src/service/memory/service.py`：① 顶部 import 块加 `import json`（避免 E402 module-import-not-at-top）；② 改 prompts import 行（L15）补常量；③ 在 `async def write_explicit_memory(` 定义之前插入 `_VALID_KINDS` 常量 + `parse_user_memory_intent` 函数。

①把顶部（L9）：

```python
import logging
from typing import Any
```

替换为：

```python
import json
import logging
from typing import Any
```

②把 L15：

```python
from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM
```

替换为：

```python
from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM, _USER_MEM_INTENT_SYSTEM
```

③在 `async def write_explicit_memory(` 定义那一行（当前 L127）之前插入（**不要**在此再写 `import json`——已在顶部）：

```python
_VALID_KINDS = ("identity", "preference", "style_feedback")


async def parse_user_memory_intent(llm: Any, content: str) -> dict:
    """轻量 LLM 解析显式记忆意图（§22.4）。返回
    {tier:'user'|'skip', kind:'identity'|'preference'|'style_feedback',
     content:str, supersedes_kind:'identity'|None}。
    任何异常/非法 JSON/字段非法 → 兜底 {user, preference, 原 content, None}（绝不丢、绝不抛）。
    """
    fallback = {
        "tier": "user", "kind": "preference",
        "content": content, "supersedes_kind": None,
    }
    try:
        raw = await llm.complete(system=_USER_MEM_INTENT_SYSTEM, user=content)
    except Exception:
        return fallback
    s = (raw or "").strip()
    if s.startswith("```"):
        # 去 ```json ... ``` 围栏
        s = s.split("```")[1] if "```" in s[3:] else s.lstrip("`")
        if s.startswith("json"):
            s = s[4:]
        s = s.strip().strip("`").strip()
    try:
        obj = json.loads(s)
    except Exception:
        return fallback
    if not isinstance(obj, dict):
        return fallback
    tier = obj.get("tier")
    kind = obj.get("kind")
    c = obj.get("content")
    sk = obj.get("supersedes_kind")
    if tier not in ("user", "skip"):
        return fallback
    if kind not in _VALID_KINDS:
        return fallback
    if not isinstance(c, str) or not c.strip():
        return fallback
    if sk not in ("identity", None):
        sk = None
    return {"tier": tier, "kind": kind, "content": c.strip(), "supersedes_kind": sk}
```

- [ ] **Step 4: 跑，确认通过** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
  Expected: 全 passed（新增 6 + 既有不回归）。

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/memory/service.py tests/test_auth/test_memory_service.py
git commit -m "$(cat <<'EOF'
feat(memory): §22.4 parse_user_memory_intent 轻量 LLM 意图解析 + 保守兜底（TDD）

调 _USER_MEM_INTENT_SYSTEM 解析严格 JSON；去 ```json 围栏；tier/kind/content
非法或 LLM 抛错/非 JSON → 兜底 {user,preference,原content,None}（不丢显式意图、不抛）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `write_explicit_memory` 加 kind/supersedes + identity 单例取代

**Files:** Modify `src/service/memory/service.py:127-143`; Test `tests/test_auth/test_memory_service.py`

- [ ] **Step 1: 写失败测试** —— 追加到 `tests/test_auth/test_memory_service.py` 末尾：

```python
# ───────── §22.5：write_explicit_memory identity 单例取代 / preference 累加 ─────────

def _um(kind, content, status="active"):
    return QAUserMemory(user_id=7, kind=kind, content=content,
                        source="explicit", source_session_id="s0", status=status)


@pytest.mark.asyncio
async def test_write_default_still_preference_no_regression():
    # 既有调用（不传 kind）行为不变：追加一条 preference
    db = _FakeMemDB()
    await write_explicit_memory(db, user_id=7, session_id="s1", content="我用 Java")
    assert len(db.added) == 1
    assert db.added[0].kind == "preference" and db.added[0].status == "active"


@pytest.mark.asyncio
async def test_write_identity_supersedes_old_identity_only():
    old_id = _um("identity", "用户的名字是王山河")
    pref = _um("preference", "用户偏好简短回答")
    db = _FakeMemDB(user_rows=[old_id, pref])
    await write_explicit_memory(
        db, user_id=7, session_id="s2", content="用户的名字是李龙飞",
        kind="identity", supersedes_kind="identity",
    )
    assert old_id.status == "archived"          # 旧 identity 归档
    assert pref.status == "active"              # preference 不受影响
    new_rows = [o for o in db.added if isinstance(o, QAUserMemory)]
    assert len(new_rows) == 1
    assert new_rows[0].kind == "identity"
    assert new_rows[0].content == "用户的名字是李龙飞"
    assert new_rows[0].status == "active"
    assert db.committed is True


@pytest.mark.asyncio
async def test_write_preference_appends_no_archive():
    old_pref = _um("preference", "用户偏好简短回答")
    db = _FakeMemDB(user_rows=[old_pref])
    await write_explicit_memory(
        db, user_id=7, session_id="s3", content="用户只看支付域",
        kind="preference", supersedes_kind=None,
    )
    assert old_pref.status == "active"          # 旧 preference 不归档（累加）
    assert len([o for o in db.added if isinstance(o, QAUserMemory)]) == 1


@pytest.mark.asyncio
async def test_recall_only_current_identity_after_supersede():
    # 取代后召回只剩李龙飞（模拟旧行已 archived）
    archived = _um("identity", "用户的名字是王山河", status="archived")
    current = _um("identity", "用户的名字是李龙飞")
    # recall 只读 status=="active" —— _FakeMemDB 不套 WHERE，故只放 active 行模拟 DB 过滤结果
    db = _FakeMemDB(user_rows=[current])
    block = await recall_memory_block(db, user_id=7, session_id="sX")
    assert "李龙飞" in block and "王山河" not in block
```

- [ ] **Step 2: 跑，确认失败** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q -k "write_identity_supersedes or write_preference_appends or write_default_still"`
  Expected: FAIL —— `test_write_identity_supersedes_old_identity_only` 失败（`write_explicit_memory` 不接受 `kind` 关键字 → TypeError）。

- [ ] **Step 3: 实现** —— `src/service/memory/service.py`，把 L127-143 整个 `write_explicit_memory`：

```python
async def write_explicit_memory(
    db: Any, *, user_id: int, session_id: str, content: str
) -> None:
    """落一条用户级显式记忆（P1：显式只进用户级）。
    注：自身不吞异常（同 recall_memory_block 契约）；Task 7 调用点已 try/except。
    """
    db.add(
        QAUserMemory(
            user_id=user_id,
            kind="preference",
            content=content,
            source="explicit",
            source_session_id=session_id,
            status="active",
        )
    )
    await db.commit()
```

替换为（追加可选 `kind`/`supersedes_kind`，非破坏；identity 写前归档同 user 旧 active identity）：

```python
async def write_explicit_memory(
    db: Any, *, user_id: int, session_id: str, content: str,
    kind: str = "preference", supersedes_kind: str | None = None,
) -> None:
    """落一条用户级显式记忆（§22.5）。

    kind=='identity' 或 supersedes_kind=='identity'：先把该 user 所有
    kind='identity' AND status='active' 行 status→'archived'（软删，宪法禁永久删），
    再 INSERT 新行 —— identity 单例，新名字取代旧名字。
    kind in ('preference','style_feedback')：仅追加（多条并存）。
    归档+插入同一次 commit。kind 默认 'preference'（旧调用零改动，不破坏）。
    自身不吞异常（同 recall_memory_block 契约）；router 调用点已 try/except。
    """
    if kind == "identity" or supersedes_kind == "identity":
        res = await db.execute(
            select(QAUserMemory).where(
                QAUserMemory.user_id == user_id,
                QAUserMemory.kind == "identity",
                QAUserMemory.status == "active",
            )
        )
        for r in res.scalars().all():
            # real SQL 已被 WHERE 限定；fake 不套 WHERE → Python 再 guard 一层
            if getattr(r, "kind", None) == "identity" and getattr(r, "status", None) == "active":
                r.status = "archived"
    db.add(
        QAUserMemory(
            user_id=user_id,
            kind=kind,
            content=content,
            source="explicit",
            source_session_id=session_id,
            status="active",
        )
    )
    await db.commit()
```

- [ ] **Step 4: 跑，确认通过** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
  Expected: 全 passed（新增 4 + 既有 `test_write_explicit_adds_user_memory_row`（不传 kind）仍绿——默认 preference 路径零行为变化、无归档查询）。

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/memory/service.py tests/test_auth/test_memory_service.py
git commit -m "$(cat <<'EOF'
feat(memory): §22.5 write_explicit_memory identity 单例取代（追加式可选参数，TDD）

加可选 kind/supersedes_kind（默认 preference，旧调用零改动）；identity 写前把同
user 旧 active identity 归档（软删）再插新 → 新名字取代旧名字；preference/style 仅
累加。归档+插入同一 commit。fake 不套 WHERE 故 Python 再 guard kind/status。设计 §22.5。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `_make_memory_writer` 用户级分支 detect→parse→write

**Files:** Modify `src/service/qa_router.py:555-561`; Test `tests/test_auth/test_memory_router_hook.py`

- [ ] **Step 1: 写失败测试** —— 追加到 `tests/test_auth/test_memory_router_hook.py` 末尾：

```python
# ───────── §22：端到端 用户级 detect→parse→write ─────────
import json as _json
from src.service.db_models_homepage import QAUserMemory as _QUM


class _SeedDB(_FakeDB):
    """在 _FakeDB 基础上让 QAUserMemory 查询返回种子行（验证 identity 归档）。"""
    def __init__(self, user_rows=None, msg_rows=None):
        super().__init__(msg_rows=msg_rows)
        self._user_rows = user_rows or []

    async def execute(self, stmt):
        ent = stmt.column_descriptions[0]["entity"]
        if ent is _QUM:
            return _FakeResult(self._user_rows)
        return await super().execute(stmt)


class _IntentLLM:
    """complete：JSON 串入参时（意图解析）返回预设；否则返回压缩用占位。"""
    def __init__(self, intent_json): self._j = intent_json
    async def complete(self, *, system, user, **kw):
        if "意图解析器" in system:
            return self._j
        return "本次目标：x"


def _seed_identity(content="用户的名字是王山河"):
    return _QUM(user_id=3, kind="identity", content=content,
                source="explicit", source_session_id="s0", status="active")


@pytest.mark.asyncio
async def test_writer_suffix_identity_supersedes_end_to_end():
    old = _seed_identity()
    db = _SeedDB(user_rows=[old])
    llm = _IntentLLM('{"tier":"user","kind":"identity",'
                     '"content":"用户的名字是李龙飞","supersedes_kind":"identity"}')
    writer = _make_memory_writer(
        db=db, llm=llm, user_id=3, session_id="s1",
        question="我改名叫李龙飞 请记住",          # 句尾触发词
    )
    await writer()
    assert old.status == "archived"
    new = [o for o in db.added if isinstance(o, _QUM)]
    assert len(new) == 1 and new[0].kind == "identity"
    assert new[0].content == "用户的名字是李龙飞" and new[0].status == "active"


@pytest.mark.asyncio
async def test_writer_skip_writes_nothing():
    db = _SeedDB()
    llm = _IntentLLM('{"tier":"skip","kind":"preference","content":"x","supersedes_kind":null}')
    writer = _make_memory_writer(
        db=db, llm=llm, user_id=3, session_id="s1", question="记住 嗯嗯",
    )
    await writer()
    assert [o for o in db.added if isinstance(o, _QUM)] == []


@pytest.mark.asyncio
async def test_writer_parse_failure_falls_back_preference():
    db = _SeedDB()
    # 意图解析返回非 JSON → parse 兜底 preference 原样写
    writer = _make_memory_writer(
        db=db, llm=_IntentLLM("好的"), user_id=3, session_id="s1",
        question="记住我喜欢简短回答",
    )
    await writer()
    rows = [o for o in db.added if isinstance(o, _QUM)]
    assert len(rows) == 1
    assert rows[0].kind == "preference" and rows[0].content == "我喜欢简短回答"
```

- [ ] **Step 2: 跑，确认失败** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_router_hook.py -q -k "suffix_identity or skip_writes_nothing or parse_failure_falls_back"`
  Expected: FAIL —— `test_writer_suffix_identity_supersedes_end_to_end`：现状用户分支直接 `write_explicit_memory(content=...)` 不解析、不取代 → `old.status` 仍 active。

- [ ] **Step 3: 实现** —— `src/service/qa_router.py`：① import 行（L46 所在 import 块）补 `parse_user_memory_intent`；② 改用户级分支 L555-561。

把 import（L46 附近，`write_explicit_memory,` 那行所在的 from 块）中的：

```python
    write_explicit_memory,
```

替换为：

```python
    write_explicit_memory,
    parse_user_memory_intent,
```

把 `_make_memory_writer` 内（L555-561）：

```python
            else:
                write_kind = "user"
                content = detect_explicit_memory(question)
                if content:
                    await write_explicit_memory(
                        db, user_id=user_id, session_id=session_id, content=content
                    )
```

替换为（detect→parse→按结果写；skip 不写；工程级/压缩/try-except 不动）：

```python
            else:
                write_kind = "user"
                content = detect_explicit_memory(question)
                if content:
                    intent = await parse_user_memory_intent(llm, content)
                    if intent.get("tier") == "user":
                        await write_explicit_memory(
                            db, user_id=user_id, session_id=session_id,
                            content=intent["content"],
                            kind=intent["kind"],
                            supersedes_kind=intent.get("supersedes_kind"),
                        )
```

- [ ] **Step 4: 跑，确认通过** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_router_hook.py -q`
  Expected: 全 passed（新增 3 + 既有 hook 测试不回归——`test_writer_persists_explicit_user_memory` 用 `_FakeLLM`（非 JSON）→ parse 兜底 preference content="我喜欢简短回答" → 仍 `len==1` 且 content 一致；project trigger 测试走工程级分支未改）。

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_router.py tests/test_auth/test_memory_router_hook.py
git commit -m "$(cat <<'EOF'
feat(memory): §22 _make_memory_writer 用户级 detect→parse→write 接线（TDD）

用户级分支：detect_explicit_memory 命中 → parse_user_memory_intent(llm) →
tier=user 才按 kind/supersedes 写（identity 取代）；skip 不写。工程级/压缩/
try-except/force_compact 一律不动。设计 §22。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: 回归 + import 自检（controller，无新文件/commit）

- [ ] **Step 1: 记忆+prompts+router 全套** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py tests/test_auth/test_memory_router_hook.py tests/test_auth/test_qa_prompts.py -q`
  Expected: 全 passed（新增 ~18；既有 detect/write/recall/compact/project/hook/prompt 不回归）。

- [ ] **Step 2: import 自检** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -c "import src.service.memory.service, src.service.qa_router, src.service.qa_engine.prompts; from src.service.memory.service import detect_explicit_memory, parse_user_memory_intent, write_explicit_memory; from src.service.qa_engine.prompts import _USER_MEM_INTENT_SYSTEM; print('OK', len(_USER_MEM_INTENT_SYSTEM))"`
  Expected: `OK <一个 >100 的整数>`

- [ ] **Step 3: QA 链路广回归** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/ -q -k "memory or qa or prompt or chitchat or sse or router" -p no:warnings`
  Expected: 0 fail（本增量纯写入路径 + prompt 文本 + 解析；召回/压缩/工程级/会话级未改）。

> 端到端（控制器 Preview/浏览器，不在 subagent 范围）：跑着的产品 —— 会话A「我改名字了 叫李龙飞 以后叫我李龙飞 请记住」→ 同工程开新会话问「我叫什么名字」应答「李龙飞」（不再是王山河）；旧 identity 行在库里 status=archived（软删可追溯）。需后端 --reload 生效 + 蓝队云 DB 隧道在。

---

## Self-Review（实施者过一遍）

- [ ] spec §22 逐条：§22.3 触发前缀/句尾（精确语义：前缀取后、后缀取前、去标点、前缀优先）✓(T1)；§22.4 _USER_MEM_INTENT_SYSTEM + parse 兜底不丢不抛 ✓(T2/T3)；§22.5 identity 单例归档取代 / preference 累加 / kind 按解析写 / 同事务 ✓(T4)；router detect→parse→write、skip 不写、工程级/压缩/try-except 不动 ✓(T5)；无迁移 ✓；§22.8 七条测试全覆盖（①句尾 T1+T5 ②解析 identity T3+T5 ③归档+新 active+preference 不动 T4 ④recall 只剩新值 T4 ⑤兜底 T3+T5 ⑥preference 累加 T4 ⑦前缀不回归 T1）✓
- [ ] 占位扫描：每 code step 完整可粘贴、命令带 Expected、无 TBD/“类似 Task”/含糊 ✓
- [ ] 类型一致：`parse_user_memory_intent(llm,content)->dict{tier,kind,content,supersedes_kind}` 在 T3 定义；T5 router 用 `intent.get("tier")/intent["content"]/intent["kind"]/intent.get("supersedes_kind")` 同名；`write_explicit_memory(...,kind="preference",supersedes_kind=None)` T4 定义、T5 调用一致；`_USER_MEM_INTENT_SYSTEM` T2 定义、T3 import 使用、T6 import 自检；`_VALID_KINDS` 仅 T3 内部用 ✓
- [ ] YAGNI：只动用户级写入路径 + 1 prompt 常量；工程级 `_PROJECT_TRIGGERS`/会话压缩/recall 排序/迁移/每轮抽取 全不碰 ✓
- [ ] 非破坏：`write_explicit_memory` 追加默认参数，旧 caller(qa_router 现状被 T5 一并改)/旧测试(`test_write_explicit_adds_user_memory_row` 不传 kind)零改动仍绿 ✓

## Phase Definition of Done

- [ ] 新增 ~18 测试全绿（detect 5 + prompt 1 + parse 6 + write 4 + router 3 — 实施时以实际为准）
- [ ] 既有 memory/router-hook/prompt 全套不回归；QA 链路 0 fail；import OK
- [ ] 5 feat commit 干净（detect / prompt / parse / write / router 接线）
- [ ] 已交付控制器端到端说明（改名跨会话取代生效、旧行软删）
