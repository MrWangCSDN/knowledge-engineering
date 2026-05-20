# 文件式记忆重构 S7 — 3 项 broken/漏洞 cleanup 实现计划（roadmap 终章）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修 3 项已知 broken/漏洞 — ① archive/ 路径不应进 Weaviate 召回索引（S4 holistic 真 bug）② delete_session 后 fs message 文件不应成 orphan（S6 §7.11 #2）③ post_feedback endpoint 不应永远返 404（S6 §7.11 #1）。

**Architecture:** 3 个独立小 task：T1 在 `MemoryRecaller.index_changed` 入口加一行 archive 过滤；T2 在 `qa_router.delete_session` DB delete 之后加 `fs.rm(recursive=True)` with try/except；T3 在 `session.py` 加 `_FsFeedback` dataclass + `write_feedback_to_fs` / `read_feedback_for_message` helpers + `_feedback_uri` URI helper，`qa_router.post_feedback` 从 404 stub 改写 fs，`read_messages_for_session` 加 `.feedback.md` 过滤。无新依赖。

**Tech Stack:** Python 3.12.13 / pytest / pytest-asyncio / S1 MemoryFS / S2 _split_frontmatter+_render_frontmatter / S4 _now_iso_z / S5+S6 session.py 既有模式（dataclass + free function + 时区归一）/ FastAPI HTTPException

**Spec source:** Obsidian `/Users/java/obsidian/01 Engineering/knowledge-engineering/文件式记忆重构-设计.md` §8（§8.0–§8.9）。

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/service/memory/recall.py` | Modify | `MemoryRecaller.index_changed` 入口加 `/archive/` 过滤（T1） |
| `src/service/qa_router.py` | Modify | `delete_session` 加 fs.rm 级联（T2）；`post_feedback` 从 404 stub → fs.write（T3） |
| `src/service/memory/session.py` | Modify | 加 `_FsFeedback` + `_feedback_uri` + `write_feedback_to_fs` + `read_feedback_for_message`（T3 helpers）；`read_messages_for_session` 加 `.feedback.md` 过滤（T3） |
| `tests/test_auth/test_memory_recall.py` | Modify | 加 `test_index_changed_skips_archived_uri`（T1） |
| `tests/test_auth/test_qa_session_router.py` | Modify | 加 delete_session fs 级联 2 测试（T2）；改 post_feedback 3 既有测试从期望 404 → 期望 204 + fs 验证（T3） |
| `tests/test_auth/test_memory_session.py` | Modify | 加 T3 helpers 单元测试 4 个 + read_messages_for_session feedback 过滤回归 1 个（T3） |

---

## Task 1: archive/ 召回过滤

**Files:**
- Modify: `src/service/memory/recall.py:143-151`（`MemoryRecaller.index_changed` for-ordered loop 内加 archive 过滤）
- Modify: `tests/test_auth/test_memory_recall.py`（追加 1 测试）

**Why this task:** S4 holistic concern 真 bug — identity-supersede 把旧 `.md` + `.abstract.md` mv 到 `archive/` 子目录后，当前 `MemoryRecaller.index_changed` 仍处理 archived `.abstract.md`，archived vector 进 Weaviate → 召回时可能命中旧 identity（如「王山河」改名后仍被命中）污染 context。修复 = 写入侧绝源过滤。

### Step 1: 加 archive 过滤逻辑

- [ ] **Inner Step 1: Read 现有 `index_changed` 实现**

定位 `src/service/memory/recall.py:126-151`。当前 for-ordered loop 内部已有 `if not uri.endswith(_ABSTRACT_SUFFIX): _log.debug(...); continue` 过滤非 abstract。S7 在此之前加 archive 过滤。

- [ ] **Inner Step 2: 修改 for-ordered loop**

替换 `src/service/memory/recall.py:143-151` 的 for-ordered loop 体：

```python
        for uri in ordered:
            try:
                # S7: archive/ 路径绝源过滤 — archived identity (S4 supersede 归档的旧版本)
                # 不应参与召回，否则改名后旧名仍被命中污染 context（"王山河→李龙飞" 类 bug）。
                # 字符串匹配 "/archive/" 精确捕获 archive/ 子目录（S4 把旧 .md + .abstract.md
                # 一并 mv 到此），不会误伤其他业务路径（无业务路径含 archive 段）。
                if "/archive/" in uri:
                    _log.debug("index_changed: skip archived uri %r", uri)
                    continue
                # 非 .abstract.md 后缀 → 跳过（debug log，便于追问题）
                if not uri.endswith(_ABSTRACT_SUFFIX):
                    _log.debug("index_changed: skip non-abstract uri %r", uri)
                    continue
                await self._index_one(fs, uri)
            except Exception as exc:           # noqa: BLE001 单条目隔离（与 S2 §3.5 一致）
                _log.debug("index_changed: index failed %r: %r", uri, exc)
```

**改动要点**：
- 新增 archive 过滤分支（带详细注释引用 S4 bug）
- 既有 non-abstract 跳过逻辑保留位置不变
- archive 过滤在 abstract 检查之前 — 优先级更高（archived `.abstract.md` 也要跳）

- [ ] **Inner Step 3: 验证 import 自检**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.memory.recall import MemoryRecaller; print('OK')"`
Expected: `OK`

- [ ] **Inner Step 4: Commit (不带测试，下一 step 加测试)**

不 commit 此 step（与 step 2 加测试一起 commit — 保证 src + test 同一 commit 形成完整 unit）。

### Step 2: 加 archive 过滤回归测试

- [ ] **Inner Step 1: Read 既有 test_memory_recall.py 结构**

Run: `grep -n "^async def test_index_changed\|^def test_index_changed\|class _FakeWeaviateClient" tests/test_auth/test_memory_recall.py | head -10`

了解既有 test 命名 + `_FakeWeaviateClient` 位置（已有 fake stack 可复用）。

- [ ] **Inner Step 2: 追加测试**

追加到 `tests/test_auth/test_memory_recall.py` 末尾（在文件最后一个 test 函数之后）：

```python
@pytest.mark.asyncio
async def test_index_changed_skips_archived_uri(tmp_path):
    """index_changed 跳过 /archive/ URI 不进 Weaviate（§8.2 S4 holistic bug 修复）。

    S4 identity-supersede 把旧 .md + .abstract.md mv 到 archive/ 子目录后，
    archived 路径不应被 vector 索引（否则改名后旧名仍被召回命中污染 context）。
    """
    fs = MemoryFS(root=str(tmp_path))
    # 准备 fake fs 内容：archive/ 路径 + 正常路径各一
    # 正常路径必须真实存在以走 _index_one 路径
    await fs.write(
        "ke://u/1/global/identity/abc.abstract.md",
        "---\nsrc_hash: h1\n---\n用户叫李龙飞\n",
    )
    await fs.write(
        "ke://u/1/global/identity/archive/old.abstract.md",
        "---\nsrc_hash: h2\n---\n用户叫王山河（archived）\n",
    )
    # 构造 fake Weaviate + recaller
    fake = _FakeWeaviateClient()
    embedder = _FakeEmbedder()
    recaller = MemoryRecaller(embedder=embedder, weaviate_client=fake)

    # index_changed 传入两个 URI（archive + 正常）
    await recaller.index_changed(fs, [
        "ke://u/1/global/identity/abc.abstract.md",
        "ke://u/1/global/identity/archive/old.abstract.md",
    ])

    # 验证：fake Weaviate 仅含正常 URI 的 vector（archive 跳过）
    # _FakeWeaviateClient 的具体 inspect 接口看既有测试用法（如 fake._store / fake._tenants）
    # 关键不变量：archived URI 不应触发 _index_one 调用
    # 一种通用断言：non-archive URI 应进入 tenant=1 的 store；archive URI 不应
    # 注：测试该不变量的精确手段取决于 _FakeWeaviateClient API；
    #     实施时 grep 既有测试看 inspect 方式（如 fake.tenant_store 或 fake._tenants）
    assert fake.collection_view("memory_l0").with_tenant("1").objects_count() == 1
```

注：`_FakeWeaviateClient` 内部 inspect API 实施时 grep `tests/test_auth/test_memory_recall.py` 找既有 `fake._store` / `fake._tenants` / `objects_count` 等使用方式，照搬。如果既有 test 没有 `objects_count` 之类，改为遍历 tenant store 数 entries：

```python
    # 备选断言：直接 inspect tenant store dict
    tenant_1_store = fake._collections["memory_l0"]._tenant_stores.get("1", {})
    assert len(tenant_1_store) == 1
```

实施时按 `_FakeWeaviateClient` 真实 API 调整断言形式。

- [ ] **Inner Step 3: Run test to verify PASS**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_recall.py::test_index_changed_skips_archived_uri -v`
Expected: PASS（src 已改完，test 跑即过）

- [ ] **Inner Step 4: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/memory/recall.py tests/test_auth/test_memory_recall.py && git commit -m "$(cat <<'EOF'
feat(memory-s7): MemoryRecaller archive/ 召回过滤（S4 holistic 真 bug 修复）

§8.2：index_changed 入口加 if "/archive/" in uri: continue — archived identity
（S4 supersede 把旧版本 mv 到 archive/）不应进 Weaviate 索引，否则改名后
旧名仍被命中污染 context（"王山河→李龙飞" 类 bug）。

加 test_index_changed_skips_archived_uri 回归：传 archive + non-archive URI
各一 → 仅 non-archive 进 vector store。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 3: T1 单元回归

- [ ] **Inner Step 1: 跑 test_memory_recall.py 全部**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_recall.py -v 2>&1 | tail -5`
Expected: 既有测试全过 + 1 新测试通过

T1 出口：2 commits（src+test 合一 + 回归无 commit）；新 1 测试通过；既有 recall 测试无回归。

---

## Task 2: delete_session fs 级联清理

**Files:**
- Modify: `src/service/qa_router.py:508-525`（`delete_session` endpoint 加 fs.rm）
- Modify: `tests/test_auth/test_qa_session_router.py`（追加 2 测试 / 改 1 既有 docstring）

**Why this task:** S6 §7.11 #2 — `delete_session` endpoint 当前只删 DB qa_sessions row；fs `ke://u/{uid}/session/{sid}/` 整目录（含 summary.md + messages/{msg_id}.md + messages/{msg_id}.feedback.md，S7 后）成 orphan，磁盘累积。

### Step 1: 改 delete_session endpoint

- [ ] **Inner Step 1: Read 当前 delete_session 实现**

定位 `src/service/qa_router.py:508-525`。当前实现：

```python
async def delete_session(
    project_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """删除会话：从 DB 删 qa_sessions 行；fs 端 message 文件保留 orphan（S7 清理）。

    S6 注：qa_messages / qa_feedback 表已删（§7.6），DB 不再级联消息/反馈；
    fs 端 ke://u/{uid}/session/{sid}/messages/ 文件在 session 被删后成 orphan，
    暂留待 S7 引入 fs 端级联清理（同 §7.11 #N 注记）。
    """
    sess = await db.get(QASession, session_id)
    if not sess or sess.project_id != project_id or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")

    await db.delete(sess)
    await db.commit()
```

- [ ] **Inner Step 2: 替换 delete_session 整段（含 docstring）**

替换为：

```python
async def delete_session(
    project_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """删除会话：从 DB 删 qa_sessions 行 + fs 级联清理 session 目录。

    设计：[[文件式记忆重构-设计]] §8.3（S7 修 S6 §7.11 #2 orphan 累积）。
    DB delete 先（业务核心 / 事务一致性）；fs.rm recursive 后（best-effort，
    失败 debug 静默，不影响 DB delete 主业务，与 §6.5/§7.7 三层防御一致）。
    清理范围：ke://u/{uid}/session/{sid}/ 整目录（含 summary.md / messages/）。
    """
    sess = await db.get(QASession, session_id)
    if not sess or sess.project_id != project_id or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")

    await db.delete(sess)
    await db.commit()

    # S7: fs 级联清理（best-effort，失败不抛）
    try:
        from src.service.memory.vfs import MemoryFS as _MemFS, MemoryNotFound
        fs = _MemFS()
        await fs.rm(f"ke://u/{user.id}/session/{session_id}", recursive=True)
    except MemoryNotFound:
        # session 目录还没创建（首压前 / 仅 DB row 无 fs）— 正常路径，静默继续
        pass
    except Exception:
        # 其他异常 → 中层失败语义（§8.5）：debug + 静默；DB 主业务已成功
        _log.debug(
            "delete_session fs cleanup failed for session %s, silently ignored",
            session_id, exc_info=True,
        )
```

**改动要点**：
- docstring 从"暂留待 S7"改为"S7 修 S6 §7.11 #2"事实陈述
- DB delete + commit 不动（业务核心）
- 加 fs.rm with try/except + MemoryNotFound catch + 其他异常 debug 静默
- 局部 import 同 S6 模式（保模块顶部 import 轻量）

- [ ] **Inner Step 3: 验证 import 自检**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service import qa_router; print('OK')"`
Expected: `OK`

- [ ] **Inner Step 4: 不 commit（与 step 2 测试一起 commit）**

### Step 2: 加 fs 级联回归测试

- [ ] **Inner Step 1: Read 既有 delete_session 测试**

Run: `grep -B 1 -A 20 "def test_delete_session" tests/test_auth/test_qa_session_router.py | head -50`

了解既有 `test_delete_session_cascades_messages`（line 186）+ `test_delete_session_404`（line 208）测试结构 + 用到的 fixture（`seeded_session` / `client` / `session_maker`）。

- [ ] **Inner Step 2: 追加 2 个新测试**

追加到 `tests/test_auth/test_qa_session_router.py`（在 `test_delete_session_404` 之后，或紧跟 `test_delete_session_cascades_messages` 之后视既有顺序）：

```python
@pytest.mark.asyncio
async def test_delete_session_cascades_fs_cleanup(client, seeded_session, session_maker, tmp_path, monkeypatch):
    """delete_session 调 fs.rm 级联清理 session 目录（§8.3 S7）。

    先 fs.write session 目录下若干文件（summary + messages），调 DELETE endpoint，
    断言 fs 目录消失。MemoryFS root 指向 tmp_path 避污染仓库 .ke-memory/。
    """
    # MemoryFS 默认 root 由 KE_MEM_ROOT env 派生；测试期指向 tmp_path
    monkeypatch.setenv("KE_MEM_ROOT", str(tmp_path))

    # 准备 fs：先写 session 目录下若干文件
    from src.service.memory.vfs import MemoryFS
    fs = MemoryFS()
    user_id, session_id = seeded_session  # fixture 应返 (user_id, session_id) — 实施时核对真返值
    await fs.write(
        f"ke://u/{user_id}/session/{session_id}/summary.md",
        "---\nturn_count: 2\n---\n会话摘要\n",
    )
    await fs.write(
        f"ke://u/{user_id}/session/{session_id}/messages/msg_test_a.md",
        "---\nrole: user\ncreated_at: \"2026-05-22T10:00:00Z\"\n---\nuser msg\n",
    )

    # 调 DELETE
    r = client.delete(f"/projects/test-project/qa/sessions/{session_id}")
    assert r.status_code == 204

    # 验证 fs 目录消失
    assert not await fs.exists(f"ke://u/{user_id}/session/{session_id}/summary.md")
    assert not await fs.exists(f"ke://u/{user_id}/session/{session_id}/messages/msg_test_a.md")


@pytest.mark.asyncio
async def test_delete_session_fs_not_exists_ok(client, seeded_session, monkeypatch, tmp_path):
    """delete_session 在 fs 目录不存在时不抛（首压前 session / 仅 DB row 无 fs，§8.3）。

    seeded_session fixture 仅 seed DB 不 seed fs；调 DELETE endpoint 应 204
    （MemoryNotFound 被中层 catch + continue）。
    """
    monkeypatch.setenv("KE_MEM_ROOT", str(tmp_path))
    user_id, session_id = seeded_session

    # fs 目录从未创建（fixture 仅 DB seed）
    from src.service.memory.vfs import MemoryFS
    fs = MemoryFS()
    assert not await fs.exists(f"ke://u/{user_id}/session/{session_id}")

    r = client.delete(f"/projects/test-project/qa/sessions/{session_id}")
    assert r.status_code == 204
```

注：`seeded_session` fixture 返值结构（是否为 tuple `(user_id, session_id)`）— 实施时 grep `def seeded_session` 看 fixture 真实返回，按需调整 unpack 方式（可能是 dict 或 SimpleNamespace）。

- [ ] **Inner Step 3: 改既有 `test_delete_session_cascades_messages` docstring**

Search test_qa_session_router.py 既有 `test_delete_session_cascades_messages` (line 186)，docstring 提到 "fs orphan" 或类似的注释（S6 m-4 fix 加过）。改为反映 S7 已修：

定位 docstring 含 "fs orphan" 或 "S7 清理" 文字，替换为：

```python
"""delete_session 删 DB row 后 fs 目录级联清理（§8.3 S7 修）。

S6 → S7 路径：S6 后 fs message 文件成 orphan；S7 加 fs.rm recursive 级联
清理整目录。本测试保留 DB delete 主断言；fs 级联断言由
test_delete_session_cascades_fs_cleanup 专项覆盖。
"""
```

（按既有 docstring 结构调整 — 仅改 fs orphan 提及部分，保 DB delete 主断言不动）

- [ ] **Inner Step 4: Run tests to verify pass**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_session_router.py::test_delete_session_cascades_fs_cleanup tests/test_auth/test_qa_session_router.py::test_delete_session_fs_not_exists_ok -v 2>&1 | tail -10`
Expected: 2 passed

- [ ] **Inner Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/qa_router.py tests/test_auth/test_qa_session_router.py && git commit -m "$(cat <<'EOF'
feat(memory-s7): delete_session fs 级联清理（修 S6 §7.11 #2 orphan）

§8.3：delete_session endpoint DB delete + commit 后加 fs.rm recursive 清理
ke://u/{uid}/session/{sid}/ 整目录（含 summary.md / messages/）。
MemoryNotFound 是正常路径（fs 目录从未创建 — 首压前 / 仅 DB row）→ catch continue；
其他异常 debug 静默（§8.5 三层防御）— 不影响 DB delete 主业务。

加 2 回归测试：
- test_delete_session_cascades_fs_cleanup（fs.write seed + DELETE → 验目录消失）
- test_delete_session_fs_not_exists_ok（fs 未 seed + DELETE → 仍 204）
更新 test_delete_session_cascades_messages docstring 引 §8.3。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 3: T2 单元回归

- [ ] **Inner Step 1: 跑 test_qa_session_router.py 全部**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_session_router.py -v 2>&1 | tail -10`
Expected: 既有测试无回归 + 2 新测试通过

T2 出口：2 commits（src+test 合一 + 回归无 commit）；2 新测试 + 1 docstring 更新。

---

## Task 3: post_feedback fs 迁移（最大）

**Files:**
- Modify: `src/service/memory/session.py`（追加 `_FsFeedback` + `_feedback_uri` + `write_feedback_to_fs` + `read_feedback_for_message`；改 `read_messages_for_session` 过滤）
- Modify: `src/service/qa_router.py:776-790`（`post_feedback` 从 404 stub → fs.write）
- Modify: `tests/test_auth/test_memory_session.py`（追加 5 测试：4 helpers + 1 read 过滤回归）
- Modify: `tests/test_auth/test_qa_session_router.py`（改 3 既有 post_feedback 测试从期望 404 → 期望 204 + fs 验证）

**Why this task:** S6 §7.11 #1 — `qa_feedback` 表已删（S6 §7.6），endpoint 暂返 404 stub。S7 恢复完整 upvote/downvote 契约，走 per-message sibling file `{msg_id}.feedback.md`。

### Step 1: 加 `_FsFeedback` dataclass + URI helper

- [ ] **Inner Step 1: 追加到 `src/service/memory/session.py` 末尾**

在文件最后一个 helper（应为 `read_messages_for_session`）之后追加：

```python
# ─── S7: fs-back feedback helpers（§8.4）─────────────────────────────────────


@dataclass
class _FsFeedback:
    """fs-back feedback 鸭子类型（duck-type 等价于 S6 已删的 QAFeedback ORM）。

    设计：[[文件式记忆重构-设计]] §8.4（S7 修 S6 §7.11 #1 broken stub）。
    一条 assistant message 对应 0 或 1 feedback（覆盖式更新；用户可改投票 / 取消）。

    字段：
    - vote: "up" / "down" / None（取消反馈）
    - user_id: 反馈人 user.id
    - comment: optional 文字反馈
    - created_at: UTC-aware datetime（write 端归一化保证）
    """
    vote: str | None
    user_id: int
    comment: str | None
    created_at: datetime


def _feedback_uri(user_id: int, session_id: str, msg_id: str) -> str:
    """URI 派生：ke://u/{uid}/session/{sid}/messages/{msg_id}.feedback.md（§8.4）。

    与 message 同目录，`.feedback.md` 后缀隐含与 {msg_id}.md 关联；
    delete_session recursive rm（§8.3）时一同被清；
    需配套 read_messages_for_session 加 `.feedback.md` 过滤避误读为 message。
    """
    return f"ke://u/{user_id}/session/{session_id}/messages/{msg_id}.feedback.md"
```

- [ ] **Inner Step 2: 验证 import 自检**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.memory.session import _FsFeedback, _feedback_uri; print('OK')"`
Expected: `OK`

- [ ] **Inner Step 3: 不 commit（与 write/read helpers 一起 commit）**

### Step 2: 加 `write_feedback_to_fs` + `read_feedback_for_message`

- [ ] **Inner Step 1: 追加到 session.py（紧跟 _feedback_uri 之后）**

```python
async def write_feedback_to_fs(
    fs: MemoryFS, *, user_id: int, session_id: str, msg_id: str,
    vote: str | None, comment: str | None = None,
    created_at: datetime | None = None,
) -> None:
    """写一条 feedback 到 fs（覆盖式更新；新 feedback 覆盖旧文件）。

    设计：[[文件式记忆重构-设计]] §8.4。
    qa_router post_feedback endpoint 用此 helper 替换 S6 后的 404 stub。

    Args:
        fs: S1 文件存储层
        user_id / session_id / msg_id: 路径派生
        vote: "up" / "down" / None（取消反馈）— None 序列化为 YAML null
        comment: optional 文字反馈（写入 body）
        created_at: 不传则用当前 UTC；naive datetime 视作 UTC；非 UTC tz-aware 转 UTC
                    （同 S6 write_message_to_fs I1 fix 模式，保证 frontmatter Z 字符串正确）
    """
    # 时区归一为 UTC-aware（同 write_message_to_fs 模式）
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    elif created_at.tzinfo is None:
        # naive 视作 UTC（与 S4 _now_iso_z / S6 I1 fix 同模式）
        created_at = created_at.replace(tzinfo=timezone.utc)
    else:
        # 非 UTC tz-aware → 归一为 UTC（防错标 Z）
        created_at = created_at.astimezone(timezone.utc)

    fm: dict = {
        "vote": vote,            # 显式 None 允许（取消反馈）— YAML 序列化为 null
        "user_id": user_id,
        "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # body 末尾换行（S2 _render_frontmatter 约定）；comment 可能 None
    body = (comment or "") + "\n"
    raw = _render_frontmatter(fm, body)
    uri = _feedback_uri(user_id, session_id, msg_id)
    # fs.write 原子（S1 os.replace POSIX rename）；覆盖式自然达成（同路径 → 替换）
    await fs.write(uri, raw)


async def read_feedback_for_message(
    fs: MemoryFS, *, user_id: int, session_id: str, msg_id: str,
) -> _FsFeedback | None:
    """读单条 message 的 feedback；不存在返 None（首次访问，正常路径）。

    设计：[[文件式记忆重构-设计]] §8.4。
    get_session_detail / export_message 可选用此 helper attach feedback to response
    （§8.6 极简版决策：默认不 attach，前端按需单独 GET）。

    单文件损坏（缺字段 / fromisoformat 抛）→ debug 返 None（§8.5 中层失败语义）。
    """
    uri = _feedback_uri(user_id, session_id, msg_id)
    try:
        raw = await fs.read(uri)
        fm, body = _split_frontmatter(raw)
        # vote 字段：允许 None / str；其他类型容错为 None
        vote = fm.get("vote")
        if vote is not None and not isinstance(vote, str):
            vote = None
        # user_id 必须 int — 损坏文件直接返 None
        uid_val = fm.get("user_id")
        if not isinstance(uid_val, int):
            return None
        # created_at 必须可解析 ISO — 损坏 / 缺失返 None
        created_at_str = fm.get("created_at")
        if not isinstance(created_at_str, str):
            return None
        created_at = datetime.fromisoformat(created_at_str)
        # body strip 后为 "" 时视作无 comment（None）；非 "" 才入 comment
        comment = body.strip() if isinstance(body, str) and body.strip() else None
        return _FsFeedback(
            vote=vote,
            user_id=uid_val,
            comment=comment,
            created_at=created_at,
        )
    except MemoryNotFound:
        # 该 message 还没 feedback（首次 GET / 用户未投票）— 正常路径
        return None
    except Exception as exc:
        # 其他异常（fromisoformat ValueError / fs 权限 / 损坏 YAML 等）— 中层兜底
        _log.debug("read_feedback_for_message failed: %r", exc)
        return None
```

- [ ] **Inner Step 2: 验证 import 自检**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.memory.session import write_feedback_to_fs, read_feedback_for_message; print('OK')"`
Expected: `OK`

- [ ] **Inner Step 3: Commit step 1 + step 2 合一**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/memory/session.py && git commit -m "$(cat <<'EOF'
feat(memory-s7): session.py 加 _FsFeedback + write/read feedback helpers

§8.4：S7 修 S6 §7.11 #1 broken stub — 新增：
- _FsFeedback private dataclass (vote: str|None, user_id, comment, created_at)
- _feedback_uri(uid, sid, msg_id) → ke://u/{uid}/session/{sid}/messages/{msg_id}.feedback.md
- write_feedback_to_fs：覆盖式 fs.write；tz 归一化（naive→UTC, non-UTC→astimezone）
  与 S6 write_message_to_fs I1 fix 同模式保证 ISO Z 字符串正确
- read_feedback_for_message：MemoryNotFound → None（首次访问正常路径）；
  其他异常 debug 静默返 None；vote/user_id/created_at 字段类型容错

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 3: 改 `read_messages_for_session` 过滤 `.feedback.md`

- [ ] **Inner Step 1: 定位 + 修改 filter 行**

Run: `grep -n "endswith.*\\.md" src/service/memory/session.py`

定位 `read_messages_for_session` 内的 filename filter（约 line 302）：

```python
        if not fname.endswith(".md"):
            continue
```

改为：

```python
        # S7 加 .feedback.md 过滤：feedback sibling 文件（msg_xyz.feedback.md，§8.4）
        # 文件名也 endswith ".md" 但不是 message — 否则误读为 message 导致 _FsMessage
        # 解析失败（缺 role 字段被 inner skip path 兜底，但走错路径有性能浪费）
        if not fname.endswith(".md") or fname.endswith(".feedback.md"):
            continue
```

- [ ] **Inner Step 2: 验证既有 message tests 仍通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_session.py -v 2>&1 | tail -5`
Expected: 既有 29 测试全过（filter 加入应不影响仅 message 文件的现有测试）

- [ ] **Inner Step 3: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/memory/session.py && git commit -m "$(cat <<'EOF'
fix(memory-s7): read_messages_for_session 过滤 .feedback.md 后缀

§8.4 配套：feedback sibling file 命名 {msg_id}.feedback.md 也 endswith ".md"，
不过滤会被 read_messages_for_session 误读为 message → _FsMessage 构造时缺
role/created_at 字段走 inner skip path（功能正确但路径错走）。加显式
endswith(".feedback.md") 过滤避免。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 4: 加 helpers 单元测试（4 个）

- [ ] **Inner Step 1: 追加到 `tests/test_auth/test_memory_session.py` 末尾**

```python
# ─── S7 T3: fs-back feedback helpers 单元测试（§8.7） ────────────────────────

from src.service.memory.session import (
    _FsFeedback, write_feedback_to_fs, read_feedback_for_message,
    _feedback_uri,
)


@pytest.mark.asyncio
async def test_write_feedback_to_fs_basic(tmp_path):
    """write_feedback_to_fs 写 up vote + comment：frontmatter.vote=up + user_id + body=comment（§8.7 场景 1）。"""
    fs = MemoryFS(root=str(tmp_path))
    ts = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)
    await write_feedback_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_abc",
        vote="up", comment="回答很赞", created_at=ts,
    )
    raw = await fs.read("ke://u/7/session/sess_x/messages/msg_abc.feedback.md")
    from src.service.memory.memgen import _split_frontmatter
    fm, body = _split_frontmatter(raw)
    assert fm["vote"] == "up"
    assert fm["user_id"] == 7
    assert fm["created_at"] == "2026-05-22T10:00:00Z"
    assert body.strip() == "回答很赞"


@pytest.mark.asyncio
async def test_write_feedback_to_fs_overwrite(tmp_path):
    """write_feedback_to_fs 第二次写覆盖第一次（fs.write atomic rename，§8.7 场景 2）。"""
    fs = MemoryFS(root=str(tmp_path))
    ts1 = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)
    ts2 = datetime(2026, 5, 22, 11, 0, 0, tzinfo=timezone.utc)
    # 写 up
    await write_feedback_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_abc",
        vote="up", created_at=ts1,
    )
    # 覆盖为 down（取消之前的 up）
    await write_feedback_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_abc",
        vote="down", comment="改主意了", created_at=ts2,
    )
    # 验最终态
    raw = await fs.read("ke://u/7/session/sess_x/messages/msg_abc.feedback.md")
    from src.service.memory.memgen import _split_frontmatter
    fm, body = _split_frontmatter(raw)
    assert fm["vote"] == "down"
    assert fm["created_at"] == "2026-05-22T11:00:00Z"
    assert body.strip() == "改主意了"


@pytest.mark.asyncio
async def test_write_feedback_to_fs_null_vote(tmp_path):
    """write_feedback_to_fs vote=None（取消反馈）frontmatter 序列化为 YAML null（§8.7）。"""
    fs = MemoryFS(root=str(tmp_path))
    ts = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)
    await write_feedback_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_abc",
        vote=None, created_at=ts,
    )
    # read 回来：vote 应为 None
    fb = await read_feedback_for_message(
        fs, user_id=7, session_id="sess_x", msg_id="msg_abc",
    )
    assert fb is not None
    assert fb.vote is None
    assert fb.user_id == 7
    assert fb.comment is None  # 无 comment


@pytest.mark.asyncio
async def test_read_feedback_for_message_not_exists_returns_none(tmp_path):
    """read_feedback_for_message 文件不存在 → 返 None（首次访问 / 用户未投票，§8.7）。"""
    fs = MemoryFS(root=str(tmp_path))
    result = await read_feedback_for_message(
        fs, user_id=7, session_id="sess_x", msg_id="msg_no_feedback",
    )
    assert result is None


@pytest.mark.asyncio
async def test_read_messages_for_session_filters_feedback_suffix(tmp_path):
    """read_messages_for_session 过滤 .feedback.md 不误读为 message（§8.4 配套）。"""
    fs = MemoryFS(root=str(tmp_path))
    base = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)
    # 写一条 message + 一条 feedback（同 msg_id 不同后缀）
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_abc",
        role="user", content="正常消息", created_at=base,
    )
    await write_feedback_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_abc",
        vote="up", created_at=base,
    )
    # read_messages 只应返 1 条 (msg_abc.md)，不误读 msg_abc.feedback.md
    result = await read_messages_for_session(fs, user_id=7, session_id="sess_x")
    assert len(result) == 1
    assert result[0].content == "正常消息"
```

- [ ] **Inner Step 2: Run tests to verify**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_session.py -k "test_write_feedback or test_read_feedback or test_read_messages_for_session_filters_feedback" -v 2>&1 | tail -10`
Expected: 5 passed

- [ ] **Inner Step 3: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add tests/test_auth/test_memory_session.py && git commit -m "$(cat <<'EOF'
test(memory-s7): fs feedback helpers 5 单元测试（§8.7 T3 helpers + filter 回归）

- test_write_feedback_to_fs_basic: up vote + comment → frontmatter + body
- test_write_feedback_to_fs_overwrite: 第二次写覆盖第一次（fs.write atomic）
- test_write_feedback_to_fs_null_vote: vote=None 序列化 YAML null + read 回 None
- test_read_feedback_for_message_not_exists_returns_none: 文件不存在返 None
- test_read_messages_for_session_filters_feedback_suffix: .feedback.md 不误读
  为 message（§8.4 read_messages_for_session 过滤回归）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 5: 改 `qa_router.py:post_feedback` endpoint

- [ ] **Inner Step 1: Read 既有 post_feedback 实现**

定位 `src/service/qa_router.py:776-790`。当前是 404 stub。

- [ ] **Inner Step 2: 替换整段**

替换 `post_feedback` 函数体：

```python
async def post_feedback(
    project_id: str,
    session_id: str,
    message_id: str,
    body: FeedbackRequest,
    user: User = Depends(get_current_user),
) -> None:
    """对一条 assistant 消息打反馈（覆盖式 upsert）。S7 改造：fs sibling file。

    设计：[[文件式记忆重构-设计]] §8.4（修 S6 §7.11 #1 broken stub）。
    路径：ke://u/{uid}/session/{sid}/messages/{msg_id}.feedback.md（与 message 同目录）。
    覆盖式更新（同 msg_id 后续 POST 覆盖前次）；vote=None 表示取消反馈。

    失败语义（§8.5）：fs.write 抛 → 抛 500（不静默 — 用户主动投票需明确反馈，
    与 compact/recall 那种 best-effort 元数据不同）。
    """
    try:
        from src.service.memory.session import write_feedback_to_fs
        from src.service.memory.vfs import MemoryFS as _MemFS
        fs = _MemFS()
        await write_feedback_to_fs(
            fs, user_id=user.id, session_id=session_id, msg_id=message_id,
            vote=body.vote, comment=body.comment,
        )
    except Exception:
        # 中层失败语义（§8.5）：fs 写失败 → 500 让前端知反馈未保存
        _log.debug(
            "post_feedback fs write failed for msg %s, returning 500",
            message_id, exc_info=True,
        )
        raise HTTPException(status_code=500, detail="反馈保存失败")
```

注：`FeedbackRequest` body 模型既有定义不动；`body.vote` / `body.comment` 沿用既有字段名。

- [ ] **Inner Step 3: 验证 import 自检**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service import qa_router; print('OK')"`
Expected: `OK`

- [ ] **Inner Step 4: 不 commit（与 step 6 改既有 endpoint 测试一起 commit）**

### Step 6: 改既有 post_feedback 测试从 404 → 204 + fs 验证

- [ ] **Inner Step 1: Read 既有 post_feedback 4 测试**

定位 `tests/test_auth/test_qa_session_router.py`：
- `test_post_feedback_success` (line 233)
- `test_post_feedback_overwrites_existing` (line 245)
- `test_post_feedback_404_on_unknown_message` (line 254)
- `test_post_feedback_validates_vote` (line 265) — 保留 422 检查不动

S6 改这 3 测试 (success / overwrites / 404) 为期望 404；S7 改回 204 + fs 验证。

- [ ] **Inner Step 2: 改 test_post_feedback_success 期望 204 + fs 验证**

替换 test_post_feedback_success 函数体：

```python
@pytest.mark.asyncio
async def test_post_feedback_success(client, seeded_session, monkeypatch, tmp_path):
    """post_feedback 写 fs feedback file 成功（§8.4 S7 修 S6 broken stub）。

    S6 后是 404 stub；S7 恢复完整契约：POST upvote → fs.read 看 frontmatter.vote=up。
    """
    monkeypatch.setenv("KE_MEM_ROOT", str(tmp_path))
    user_id, session_id = seeded_session  # 实施时按 fixture 真返值 unpack
    message_id = "msg_test_assistant"

    r = client.post(
        f"/projects/test-project/qa/sessions/{session_id}/messages/{message_id}/feedback",
        json={"vote": "up", "comment": "答得好"},
    )
    assert r.status_code == 204

    # 验 fs 写入
    from src.service.memory.vfs import MemoryFS
    from src.service.memory.memgen import _split_frontmatter
    fs = MemoryFS()
    raw = await fs.read(
        f"ke://u/{user_id}/session/{session_id}/messages/{message_id}.feedback.md"
    )
    fm, body = _split_frontmatter(raw)
    assert fm["vote"] == "up"
    assert fm["user_id"] == user_id
    assert body.strip() == "答得好"
```

- [ ] **Inner Step 3: 改 test_post_feedback_overwrites_existing**

替换函数体：

```python
@pytest.mark.asyncio
async def test_post_feedback_overwrites_existing(client, seeded_session, monkeypatch, tmp_path):
    """post_feedback 同 msg_id 第二次覆盖第一次（fs.write 原子覆盖，§8.4）。"""
    monkeypatch.setenv("KE_MEM_ROOT", str(tmp_path))
    user_id, session_id = seeded_session
    message_id = "msg_test_overwrite"

    # 第一次 up
    r1 = client.post(
        f"/projects/test-project/qa/sessions/{session_id}/messages/{message_id}/feedback",
        json={"vote": "up"},
    )
    assert r1.status_code == 204
    # 第二次 down（覆盖）
    r2 = client.post(
        f"/projects/test-project/qa/sessions/{session_id}/messages/{message_id}/feedback",
        json={"vote": "down", "comment": "改主意了"},
    )
    assert r2.status_code == 204

    # 验最终态 = down
    from src.service.memory.vfs import MemoryFS
    from src.service.memory.memgen import _split_frontmatter
    fs = MemoryFS()
    raw = await fs.read(
        f"ke://u/{user_id}/session/{session_id}/messages/{message_id}.feedback.md"
    )
    fm, body = _split_frontmatter(raw)
    assert fm["vote"] == "down"
    assert body.strip() == "改主意了"
```

- [ ] **Inner Step 4: 删 / 改 test_post_feedback_404_on_unknown_message**

S7 后 endpoint 不再 check message_id 在 fs 真实存在（只写 feedback file，message_id 是 path segment）。对未知 message_id POST 仍返 204 — 这是设计选择：

```python
@pytest.mark.asyncio
async def test_post_feedback_unknown_message_writes_orphan_feedback(client, seeded_session, monkeypatch, tmp_path):
    """post_feedback 对未知 message_id 仍返 204 写 orphan feedback file（§8.4 设计选择）。

    S7 endpoint 不校验 message 在 fs 真实存在 — 写 feedback file 是独立操作。
    Orphan feedback file 在 delete_session 时一同被 fs.rm recursive 清理。
    """
    monkeypatch.setenv("KE_MEM_ROOT", str(tmp_path))
    user_id, session_id = seeded_session
    message_id = "msg_does_not_exist"

    r = client.post(
        f"/projects/test-project/qa/sessions/{session_id}/messages/{message_id}/feedback",
        json={"vote": "up"},
    )
    assert r.status_code == 204

    # orphan feedback file 已写
    from src.service.memory.vfs import MemoryFS
    fs = MemoryFS()
    assert await fs.exists(
        f"ke://u/{user_id}/session/{session_id}/messages/{message_id}.feedback.md"
    )
```

- [ ] **Inner Step 5: test_post_feedback_validates_vote 保留不动**

定位该测试，确认 docstring 已含 S6 m-3 fix 的 Pydantic-layer 说明。如未含，加：

```python
"""S7 注：Pydantic body validation 422 与 S7 endpoint 200/500 行为正交 —
Pydantic 先于 endpoint 体执行；vote="maybe" 仍 422，不取决于 fs 状态。
"""
```

- [ ] **Inner Step 6: Run tests + verify**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_session_router.py -k "test_post_feedback" -v 2>&1 | tail -10`
Expected: 4 passed（success / overwrites_existing / unknown_message_writes_orphan / validates_vote）

- [ ] **Inner Step 7: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/qa_router.py tests/test_auth/test_qa_session_router.py && git commit -m "$(cat <<'EOF'
feat(memory-s7): post_feedback fs 迁移（修 S6 §7.11 #1 broken 404 stub）

§8.4：endpoint 从 raise HTTPException(404) → await write_feedback_to_fs(...)。
路径 ke://u/{uid}/session/{sid}/messages/{msg_id}.feedback.md（覆盖式 upsert）。
fs.write 抛 → raise HTTPException(500, "反馈保存失败")（§8.5：用户主动投票
需明确反馈，不静默 — 与 compact/recall best-effort 不同）。

改 3 既有测试从 S6 期望 404 → S7 期望 204 + fs.read 验证：
- test_post_feedback_success: POST up → frontmatter.vote=up + body=comment
- test_post_feedback_overwrites_existing: 第二次 down 覆盖第一次 up
- test_post_feedback_unknown_message_writes_orphan_feedback（改名 from 404_on_unknown）:
  endpoint 不校验 message 真实存在；orphan feedback 由 delete_session §8.3 级联清
- test_post_feedback_validates_vote: 保 422（Pydantic-layer 独立于 endpoint 体）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 7: T3 单元 + 广回归

- [ ] **Inner Step 1: T3 单元跑**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_session.py tests/test_auth/test_qa_session_router.py -v 2>&1 | tail -10`
Expected: 全过

- [ ] **Inner Step 2: 全套广回归**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth -q 2>&1 | tail -5`
Expected: **0 failed**（S6 baseline 560 + S7 净 +5-8 → 预期 ~568 passed）

- [ ] **Inner Step 3: import 自检 + grep 残留**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "
from src.service import qa_router
from src.service.memory.session import (
    SessionCompactor, read_session_summary, _summary_uri,
    _FsMessage, write_message_to_fs, read_messages_for_session,
    _FsFeedback, write_feedback_to_fs, read_feedback_for_message, _feedback_uri,
)
from src.service.memory.recall import MemoryRecaller
print('all OK')
"
```
Expected: `all OK`

- [ ] **Inner Step 4: Commit 广回归 + S7 finishing 文档**

```bash
cd /Users/java/knowledge-engineering-auth && git add -A && git commit --allow-empty -m "$(cat <<'EOF'
test(memory-s7): 广回归 0 failed + S7 roadmap 终章收尾

S7 部署最终验证：
- tests/test_auth -q 全套 passed（S6 baseline 560 + S7 加 5-8 新 = ~568 passed）
- session.py 公开 contract 演进：S7 加 _FsFeedback / write_feedback_to_fs /
  read_feedback_for_message / _feedback_uri 与 _FsMessage 系列并列
- archive/ 召回过滤生效（MemoryRecaller.index_changed 不索引 archived URI）
- delete_session fs 级联清理生效（fs.rm recursive 整目录）
- post_feedback fs 迁移生效（从 404 stub → 204 + fs.write feedback sibling file）

Roadmap 终章 S1-S7 全部完成 — file-based memory 整套替换 DB 三层
（qa_user_memory + qa_project_memory + qa_session_memory + qa_messages
+ qa_feedback 5 表全迁文件 / 删 ORM / Alembic drop）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

T3 出口：4 commits（helpers + read filter + helpers 测试 + endpoint+测试改）+ 1 收尾 commit；广回归 0 failed；roadmap 终章 closed。

---

## Self-Review

### 1. Spec coverage check

| §8 spec 节 | 实现 task |
|---|---|
| §8.0 brainstorm forks | T1+T2+T3 各任务承载相应决策（极简版 3 forks 拍板） |
| §8.1 架构总览（3 项独立 task） | T1 / T2 / T3 各自独立实现 |
| §8.2 T1 archive 过滤 | T1 Step 1+2（src 改 + 测试） |
| §8.3 T2 delete_session fs 清理 | T2 Step 1+2（src 改 + 2 测试 + 1 docstring 更新） |
| §8.4 T3 post_feedback fs 迁移 | T3 Step 1-6（helpers + read filter + helpers tests + endpoint + 既有 tests 改） |
| §8.5 失败语义（三层防御） | T1 _log.debug skip + T2 try/except MemoryNotFound + T3 write 端透传 + endpoint raise 500 |
| §8.6 范围边界 | 全 plan 严守极简版 |
| §8.7 测试策略 | T1 1 test + T2 2 tests + T3 5 tests + 3 既有改写 = 11 测试触点 |
| §8.8 决策日志 | commit messages 引用各决策 |
| §8.9 roadmap 终章 | T3 Step 7 收尾 commit 含 roadmap 完成声明 |

✅ 全覆盖。

### 2. Placeholder scan

Plan 自身扫 `TBD|FIXME|fill in|implement later|add appropriate|see Task [0-9]`：

- 部分 fixture 返值（如 `seeded_session` 返 tuple vs dict 视 fixture 实际定义）+ `_FakeWeaviateClient` inspect API 形态等明示"实施时按真实状态调整" — 这是 implementer flexibility 不是占位（plan 给的是行为约束 + 具体可选断言形式，由 implementer 按真实 API 选）

预期 0 实际 placeholder（只有 implementer flexibility 注释）。

### 3. Type consistency

| 类型 / 签名 | T3 Step 1-3 定义 | T3 Step 4-6 使用 |
|---|---|---|
| `_FsFeedback(vote: str|None, user_id: int, comment: str|None, created_at: datetime)` | Step 1 dataclass | Step 4 测试构造；Step 6 endpoint 间接（通过 helpers） |
| `_feedback_uri(user_id, session_id, msg_id) -> str` | Step 1 | Step 2/4 内部用 |
| `write_feedback_to_fs(fs, *, user_id, session_id, msg_id, vote, comment=None, created_at=None) -> None` | Step 2 | Step 4 测试调；Step 5 endpoint 调（无 created_at — 端点用默认） |
| `read_feedback_for_message(fs, *, user_id, session_id, msg_id) -> _FsFeedback | None` | Step 2 | Step 4 测试调；Step 6 既有 endpoint 测试间接 |
| `read_messages_for_session` filter | Step 3（加 `.feedback.md` 过滤） | Step 4 测试 `test_read_messages_for_session_filters_feedback_suffix` |

✅ 类型一致。

### 4. T1/T2 边界

- T1 仅改 `recall.py:143-151` 内 for-ordered loop 体（加 archive 过滤分支）+ 加 1 测试 — 边界清晰
- T2 仅改 `qa_router.py:508-525` `delete_session` 函数（加 fs.rm with try/except）+ 加 2 测试 + 改 1 docstring — 边界清晰
- T3 改 `session.py`（追加 helpers + 改 read 过滤）+ `qa_router.py:776-790` 改 endpoint + 改既有测试 — 边界清晰，5 步分解合理

无 inter-task dependency（T1/T2/T3 各自独立可并行实现；本 plan 选择顺序仅出于 review 流水线）。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-22-file-memory-s7-cleanup.md`. Two execution options:

**1. Subagent-Driven (recommended)** - 同 S1-S6 模式：每 task 派 fresh subagent，两阶段 review（spec 合规 + 代码质量），修正闭环，末尾整体 holistic review。

**2. Inline Execution** - 本会话内顺序执行 3 tasks，检查点 review。

Which approach?
