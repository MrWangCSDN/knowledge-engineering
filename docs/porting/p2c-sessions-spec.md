# P2c sessions 族行为规范（py-final-baseline 提取，经对抗核验）

> TS 移植权威。2026-06-12 提取+核验工作流产出。



---

# 【sessions 族（8 路由 + docx）】

`format` query: `required=False, default="docx"` — matches spec. Now I have everything verified. Let me compile the corrected specification.

Key findings:
1. **docx main title**: spec says `代码知识工程问答记录`, source line 91 says `代码知识问答记录` (no 工程). **Material error.**
2. **`message_count` DB default**: spec says `INT NOT NULL DEFAULT 0`, introspected drizzle has `int().notNull()` with NO default. **Error.**
3. **Section attribute access notation**: spec writes `section.title`/`section.type`/`section.references`, code uses dict `.get()`. Minor — sections are dicts. Will flag.
4. **`build_docx` generated_at default**: spec says header timestamp; the export endpoint doesn't pass `generated_at`, so it defaults to `datetime.now()` (local, not UTC). Minor nuance worth noting.

Everything else verified accurate. Here is the corrected complete specification.

---

## 修正清单

逐条回源核验 8 路由 + 共享设施。发现 **2 处实质错误 + 3 处需澄清/补强**，其余抽查点全部与源码一致。

| # | 严重度 | 位置 | 同事 spec | 源码真相 | 证据 |
|---|--------|------|-----------|----------|------|
| C1 | **高（逐字错）** | §8 docx 主标题 | `代码知识工程问答记录`（Heading 0） | `代码知识问答记录`（**无"工程"二字**） | `docx_exporter.py:91` `doc.add_heading("代码知识问答记录", level=0)` |
| C2 | **中（数据模型错）** | 末节 qa_sessions 表 | `message_count INT NOT NULL DEFAULT 0` | introspect 后 **无 DEFAULT**：`int().notNull()` | `schema.ts:109` `message_count: int().notNull()`（无 `.default()`） |
| C3 | 低（命名歧义） | §8 Section 结构 | 用 `section.title` / `section.type` / `section.references`（属性访问） | 实为 **dict**，源码用 `section.get("title")` 等；TS 端是普通对象/JSON | `docx_exporter.py:214-237, 284-305`；export 处 `sections` 来自 `fm.get("sections")` |
| C4 | 低（语义补强） | §8 docx generated_at | 笼统说"页眉 `{YYYY-MM-DD HH:MM}`" | export 端 **不传** `generated_at` → `build_docx` 内默认 `datetime.now()`（**本地时区**，非 UTC，且页眉用 `%Y-%m-%d %H:%M`） | `qa_router.py:1091-1095` 不传 generated_at；`docx_exporter.py:85` `or datetime.now()` |
| C5 | 低（页眉文案精度） | §8 页眉 | `{project_name}\t\t代码知识工程 · {时间}` | 逐字正确（双 `\t`），但 project_name 实际是 `project.name or project.id` | `docx_exporter.py:263`；`qa_router.py:1088,1094` |

**抽查通过（无修正）的关键点**：

- Router prefix `/projects/{project_id}/qa`、Router 级 `dependencies=[Depends(require_infra_healthy)]` ✓（`qa_router.py:57-61`）
- 8 条路由全部 `Depends(require_project_role("reporter"))` ✓（行 553/592/650/828/872/903/938/987）
- 所有 detail 文案逐字（全角括号 `（）`、`需要 reporter 及以上权限（当前 {role}）`、`unsupported export format: {format!r}; only 'docx' is supported in v1.5`）✓
- `get_current_user` 401 `"Not authenticated"` + `WWW-Authenticate: Bearer`、`type!=access` / sub 非数字 / `is_active` 检查 ✓（`auth_dependencies.py:25-47`）
- `_iso()` None→`""`、naive→UTC、`+00:00`→`Z` ✓（行 539-544）
- list_sessions 过滤 `archived_at IS NULL` + owner + `ORDER BY updated_at DESC` ✓（行 561-569）
- 第二道门 `not sess or project_id 不符 or user_id 不符 → 404 会话不存在` ✓（4 处）
- rename 守卫顺序：先 title 校验（空→400 / >100→400）→ 查会话（404）→ 归档检查（409）✓（行 841-851）
- `message.id` 恒为 `None`、`session_id` 用 path 值 ✓（行 632-633）
- 消息排序 `(created_at, user=0/assistant=1)` ✓（`session.py:343`）
- feedback fs 失败 → 500 `"反馈保存失败"`（唯一不静默）✓（行 956-970）
- export 守卫顺序 a-g 逐条 ✓（行 1007-1069）；`short_id = message_id.replace("msg_", "")[:8]` ✓（行 1099）
- 8 路由均无 `log_audit`；`MESSAGE_EXPORT_DOCX = "message.export_docx"` 定义于 `audit/actions.py:107` 但未被引用 ✓
- `_SECTION_TITLES` / `_SECTION_EMOJIS` 7 项映射逐字 ✓（`docx_exporter.py:37-56`）
- `qa_sessions` 列、两个 index、`user_id` 无 FK、`project_id` ON DELETE CASCADE、`title_custom tinyint default 0` ✓（`schema.ts:102-117`）

---

# Sessions 族路由行为规范（8 条）— 修正版

路由前缀：`/projects/{project_id}/qa`（`APIRouter(prefix="/projects/{project_id}/qa")`，`qa_router.py:57-58`）

**Router 级公共守卫**（所有 8 条路由先过此层，再到路由自身守卫）：

1. `require_infra_healthy`（Router 级 `dependencies`，`qa_router.py:61`）。注意其内部先 `Depends(get_current_user)`（`deps_infra.py:28`），所以**鉴权实际先于健康判断求值**（FastAPI 会先解析子依赖 user）：
   - `app.state.infra_status` 未初始化（None）→ **503**，detail `{"code":"INFRA_UNINITIALIZED","message":"系统正在初始化，请稍后重试"}`（`deps_infra.py:45-52`）
   - 任一 critical 依赖 `ok==False`（排除 `_NON_CRITICAL_DEPS={"neo4j"}`）→ **503**，detail `{"code":"INFRA_UNHEALTHY","message":"系统暂时不可用，请联系管理员"}`；`user.is_admin` 额外附 `"deps": state`（`deps_infra.py:56-77`）
2. `get_current_user`（Bearer，`auth_dependencies.py:20-49`）：无 token / `decode_token` 失败 / `payload.type != "access"` / `sub` 缺失或非数字 / user 不存在 / `is_active` 假 → **401**，detail `"Not authenticated"`，header `WWW-Authenticate: Bearer`

> TS 移植注意：FastAPI 依赖求值顺序使 401 与 503 的先后取决于 user 解析；但 infra 守卫 detail 是 dict（结构化 body），不是字符串。

---

## 1. GET /sessions — list_sessions

### 守卫（顺序）

1. Router 级公共守卫
2. `require_project_role("reporter")`（`permission_deps.py:370-451`）：
   - project 不存在（`db.get(Project, project_id) is None`）→ **404**，detail `"工程不存在"`
   - `resolve_role` 返回 None（无任何成员关系）→ **403**，detail `"无权访问此工程"`
   - `ROLE_RANK[role] < ROLE_RANK["reporter"]`（reporter=1，不可能低于此）→ 403，detail `"需要 reporter 及以上权限（当前 {role}）"`（理论分支；reporter 为最低级故实际不触发，但 detail 文案保留）

### 请求

| 位置 | 字段 | 类型 | 必选 | 默认 |
|------|------|------|------|------|
| path | `project_id` | str | Y | — |

无 query / body 参数。

### 成功响应 — 200 OK

```json
{ "sessions": [ { "id": "...", "project_id": "...", "title": "查询转账记录", "created_at": "2026-01-01T00:00:00Z", "updated_at": "...", "message_count": 4 } ] }
```

- `sessions`：数组，可为 `[]`
- `id`：string
- `project_id`：string
- `title`：string | null（DB `varchar(255)` 可空）
- `created_at` / `updated_at`：string，`_iso()` 输出（带 `Z`；naive 视作 UTC）
- `message_count`：integer

### 错误

| status | detail |
|--------|--------|
| 401 | `"Not authenticated"` |
| 403 | `"无权访问此工程"` |
| 403 | `"需要 reporter 及以上权限（当前 {role}）"` |
| 404 | `"工程不存在"` |
| 503 | `{"code":"INFRA_UNHEALTHY","message":"系统暂时不可用，请联系管理员"}` / `{"code":"INFRA_UNINITIALIZED","message":"系统正在初始化，请稍后重试"}` |

### DB

- 表 `qa_sessions`
- `select(QASession).where(project_id==, user_id==user.id, archived_at IS NULL).order_by(updated_at DESC)`（行 561-569）

### 软删语义

`archived_at IS NULL` 为活动；已归档不出现在此列表。

### 审计

不写。

### 怪癖

- 只返回**当前用户自己**的会话（`user_id == user.id`），不跨用户
- 默认排除已归档

---

## 2. GET /sessions/{session_id} — get_session_detail

### 守卫（顺序）

1. Router 级公共守卫
2. `require_project_role("reporter")`
3. 内部第二道门：`not sess or sess.project_id != project_id or sess.user_id != user.id` → **404**，detail `"会话不存在"`（不暴露存在性，行 604-606）

### 请求

| 位置 | 字段 | 类型 | 必选 |
|------|------|------|------|
| path | `project_id` | str | Y |
| path | `session_id` | str | Y |

### 成功响应 — 200 OK

```json
{
  "session": { "id": "...", "project_id": "...", "title": "...", "created_at": "...", "updated_at": "...", "message_count": 4 },
  "messages": [
    { "id": null, "session_id": "<path session_id>", "role": "user", "content": "...", "sections": null, "metadata": null, "created_at": "..." },
    { "id": null, "session_id": "<path session_id>", "role": "assistant", "content": null, "sections": [...], "metadata": {...}, "created_at": "..." }
  ]
}
```

字段（行 621-642）：
- `session`：同 list 单条格式（含 `message_count`）
- `messages[]`：
  - `id`：**恒 `null`**（fs 存储无 DB row id）
  - `session_id`：path 中的 `session_id`（来自闭包变量，非消息自身字段）
  - `role`：`"user"` / `"assistant"`（来自 `m.role`）
  - `content`：`m.content`（fs body `.strip()`；user 有内容，assistant 通常为 `""` 或 None — body 为空时 `"".strip()==""`，故可能是空串而非 null）
  - `sections`：`m.sections`（null | list；非 list 在读取层归一为 None）
  - `metadata`：`m.msg_metadata`（null | dict；非 dict 归一为 None）
  - `created_at`：`_iso(m.created_at)`

### 错误

| status | detail |
|--------|--------|
| 401 | `"Not authenticated"` |
| 403 | `"无权访问此工程"` |
| 404 | `"会话不存在"` |
| 503 | （同上） |

### DB / 消息来源

- `qa_sessions`：`db.get(QASession, session_id)`（主键直查）
- 消息：**不查 DB**，`read_messages_for_session(MemoryFS(), user_id=user.id, session_id=session_id)`（行 611-619）
- fs 路径：`ke://u/{user_id}/session/{session_id}/messages/*.md`，过滤 `endswith(".md") and not endswith(".feedback.md")`（`session.py:305`）
- frontmatter：`role`（必需，非 str/空则跳过）、`created_at`（必需 str，否则跳过）、可选 `sections`、可选 `msg_metadata`；body `.strip()` → content
- 失败降级：`read_messages_for_session` 抛任何异常（含 import 失败）→ `msgs = []`，`_log.debug`，**不抛错**

### 审计

不写。

### 怪癖

- 已归档会话**也能**访问详情（内部只检查 owner，不检查 `archived_at`）
- 排序：`(created_at, 0 if role=="user" else 1)` 升序（`session.py:343`）；同秒 user 在前
- `messages[n].id` 永远 `null`

---

## 3. DELETE /sessions/{session_id} — delete_session

### 守卫（顺序）

1. Router 级公共守卫
2. `require_project_role("reporter")`
3. 内部：`not sess or project_id 不符 or user_id 不符` → **404** `"会话不存在"`（行 665-667）

### 请求

| 位置 | 字段 | 类型 | 必选 |
|------|------|------|------|
| path | `project_id` | str | Y |
| path | `session_id` | str | Y |

### 成功响应 — 204 No Content

无 body（`status_code=status.HTTP_204_NO_CONTENT`，行 647）。

### 错误

| status | detail |
|--------|--------|
| 401 | `"Not authenticated"` |
| 403 | `"无权访问此工程"` |
| 404 | `"会话不存在"` |
| 503 | （同上） |

### DB（事务时点）

- `db.get(QASession, session_id)` → `db.delete(sess)` → `db.commit()`（先 DB，业务核心）（行 669-670）
- 再 fs 清理（best-effort）

### fs 清理（S7）

- `fs.rm("ke://u/{user.id}/session/{session_id}", recursive=True)`（行 676）
- `MemoryNotFound` → 静默 `pass`（目录未创建，正常路径）
- 其他异常 → `_log.debug` 静默，不影响已 commit 的 DB 主业务（行 680-685）

### 审计

不写。

---

## 4. PATCH /sessions/{session_id} — rename_session

### 守卫（顺序）

1. Router 级公共守卫
2. `require_project_role("reporter")`
3. 内部（**严格顺序**，行 841-851）：
   a. `(body.title or "").strip()` 为空 → **400** `"标题不能为空"`
   b. `len(title) > 100` → **400** `"标题不能超过 100 字"`
   c. `not sess or project_id 不符 or user_id 不符` → **404** `"会话不存在"`
   d. `sess.archived_at is not None` → **409** `"已归档会话不可重命名"`

### 请求

| 位置 | 字段 | 类型 | 必选 | 规则 |
|------|------|------|------|------|
| path | `project_id` | str | Y | |
| path | `session_id` | str | Y | |
| body | `title` | str | Y | strip 后非空，长度 ≤ 100 |

Body model `RenameSessionBody`（`BaseModel`，仅 `title: str`，无 pydantic Field 约束 — 长度/空校验为**手写**，故越界返 400 而非 422）（行 820-822, 841-845）。

### 成功响应 — 200 OK

```json
{ "id": "...", "title": "新标题", "title_custom": true }
```

- `title`：strip 后已落库值
- `title_custom`：**恒 `true`**（`sess.title_custom = True`，行 854；返回 `sess.title_custom`）

### 错误

| status | detail |
|--------|--------|
| 400 | `"标题不能为空"` |
| 400 | `"标题不能超过 100 字"` |
| 401 | `"Not authenticated"` |
| 403 | `"无权访问此工程"` |
| 404 | `"会话不存在"` |
| 409 | `"已归档会话不可重命名"` |
| 503 | （同上） |

### DB

`sess.title = title`，`sess.title_custom = True`，`db.commit()`（行 853-855）。返回值用内存中的 `sess.id / sess.title / sess.title_custom`（无 refresh）。

### 审计

不写。

### 怪癖

- `title_custom=True` 一旦置位，异步 LLM 标题总结（`_make_title_generator._gen`，行 705）在 `sess.title_custom` 为真时返 None，**永不覆盖**用户手动标题
- 纯空格 title → strip 后空 → 400
- 验证顺序：先 title 有效性 → 再会话存在性 → 再归档状态

---

## 5. POST /sessions/{session_id}/archive — archive_session

### 守卫（顺序）

1. Router 级公共守卫
2. `require_project_role("reporter")`
3. 内部：`not sess or project_id 不符 or user_id 不符` → **404** `"会话不存在"`（行 883-885）

### 请求

| 位置 | 字段 | 类型 | 必选 |
|------|------|------|------|
| path | `project_id` | str | Y |
| path | `session_id` | str | Y |

无 body。

### 成功响应 — 200 OK（pydantic `ArchiveResponse`）

```json
{ "id": "...", "archived_at": "2026-06-12T10:00:00Z" }
```

- `ArchiveResponse`：`id: str`，`archived_at: Optional[str] = None`（行 863-866）
- `archived_at`：`_iso(sess.archived_at) if sess.archived_at else None`（行 896）

### 错误

| status | detail |
|--------|--------|
| 401 | `"Not authenticated"` |
| 403 | `"无权访问此工程"` |
| 404 | `"会话不存在"` |
| 503 | （同上） |

### DB（幂等）

- 若 `sess.archived_at is None`：`sess.archived_at = datetime.now(timezone.utc).replace(tzinfo=None)`（**naive UTC** 落库），`commit()` + `refresh(sess)`（行 888-892）
- 若已归档：**不更新时间戳**，直接返回现有值

### 审计

不写。

### 怪癖

- 落库为 naive datetime（`replace(tzinfo=None)`），响应经 `_iso()` 重新视作 UTC 输出带 `Z`
- 幂等：重复 POST 不刷新时间戳

---

## 6. POST /sessions/{session_id}/unarchive — unarchive_session

### 守卫（顺序）

1. Router 级公共守卫
2. `require_project_role("reporter")`
3. 内部：同上 → **404** `"会话不存在"`（行 913-915）

### 请求

| 位置 | 字段 | 类型 | 必选 |
|------|------|------|------|
| path | `project_id` | str | Y |
| path | `session_id` | str | Y |

无 body。

### 成功响应 — 200 OK（`ArchiveResponse`）

```json
{ "id": "...", "archived_at": null }
```

- `archived_at`：**恒 `null`**（`ArchiveResponse(id=sess.id, archived_at=None)`，行 922）

### 错误

同 archive（401/403/404/503）。

### DB（幂等）

- 若 `sess.archived_at is not None`：置 None，`commit()`（行 918-920）
- 若本已活动：**不写 DB**，直接返回

### 审计

不写。

---

## 7. POST /sessions/{session_id}/messages/{message_id}/feedback — post_feedback

### 守卫（顺序）

1. Router 级公共守卫
2. `require_project_role("reporter")`
3. 内部：**无** session/message 存在性 DB 校验（纯 fs 写）。函数签名**不注入 `db`**（仅 `user: User`，行 940-946）

### 请求

| 位置 | 字段 | 类型 | 必选 | 规则 |
|------|------|------|------|------|
| path | `project_id` | str | Y | |
| path | `session_id` | str | Y | |
| path | `message_id` | str | Y | |
| body | `vote` | `Literal["up","down"]` | Y | 仅 `"up"` / `"down"` |
| body | `comment` | str \| null | N | `Field(None, max_length=2000)` |

Body model `FeedbackRequest`（`vote: Literal["up","down"]`，`comment: Optional[str] = Field(None, max_length=2000)`，行 927-930）。

### 成功响应 — 204 No Content

无 body（`status_code=status.HTTP_204_NO_CONTENT`，行 935）。

### 错误

| status | detail |
|--------|--------|
| 422 | pydantic 校验错（vote 非 up/down、comment > 2000）— FastAPI 返 **422**，非 400 |
| 401 | `"Not authenticated"` |
| 403 | `"无权访问此工程"` |
| 500 | `"反馈保存失败"` |
| 503 | （同上） |

### DB

无。

### fs 写

- `write_feedback_to_fs(MemoryFS(), user_id=user.id, session_id, msg_id=message_id, vote=body.vote, comment=body.comment)`（行 960-963）
- 路径 `ke://u/{user_id}/session/{session_id}/messages/{message_id}.feedback.md`（`session.py:376`）
- frontmatter（顺序）：`vote`（None 序列化为 YAML null）、`user_id`、`created_at`（`%Y-%m-%dT%H:%M:%SZ`）；body `= (comment or "") + "\n"`（`session.py:407-413`）
- 覆盖式（`fs.write` 原子 `os.replace`）
- 失败：任何异常 → `_log.debug` + `raise HTTPException(500, "反馈保存失败")`（**不静默**，行 964-970）

### 审计

不写。

### 怪癖

- sessions 族中**唯一**失败返 500 而非静默的端点
- 不注入 db，不校验 session/message 存在；fs 写成功即 204
- vote 非法 → 422（FastAPI Pydantic），不是路由内 400

---

## 8. GET /sessions/{session_id}/messages/{message_id}/export — export_message

### 守卫（顺序）

1. Router 级公共守卫
2. `require_project_role("reporter")`
3. 内部（**早爆顺序**，行 1007-1069）：
   a. `format != "docx"` → **400** `"unsupported export format: {format!r}; only 'docx' is supported in v1.5"`
   b. `db.get(ProjectModel, project_id) is None` → **404** `"工程不存在"`
   c. `sess is None or sess.project_id != project_id` → **404** `"会话不存在"`
   d. `sess.user_id != user.id` → **404** `"会话不存在"`（非 owner 不暴露存在性）
   e. fs/import 初始化失败 → **404** `"消息不存在"`
   f. 读/解析目标消息文件失败 → **404** `"消息不存在"`
   g. `target_msg.role != "assistant"` → **400** `"只能导出 assistant 消息，user 消息没有结构化答案"`

### 请求

| 位置 | 字段 | 类型 | 必选 | 默认 |
|------|------|------|------|------|
| path | `project_id` | str | Y | — |
| path | `session_id` | str | Y | — |
| path | `message_id` | str | Y | — |
| query | `format` | str | N | `"docx"`（`Query("docx", ...)`，手动校验非 Literal） |

### 成功响应 — 200 OK

- `Content-Type`：`application/vnd.openxmlformats-officedocument.wordprocessingml.document`
- `Content-Disposition`：`attachment; filename="qa-{project_id}-{short_id}.docx"`
  - `short_id = message_id.replace("msg_", "")[:8]`（行 1099）
- body：docx 二进制 bytes（`Response(content=..., media_type=_DOCX_MIME, headers=...)`）

### 错误

| status | detail |
|--------|--------|
| 400 | `"unsupported export format: {format!r}; only 'docx' is supported in v1.5"` |
| 400 | `"只能导出 assistant 消息，user 消息没有结构化答案"` |
| 401 | `"Not authenticated"` |
| 403 | `"无权访问此工程"` |
| 404 | `"工程不存在"` |
| 404 | `"会话不存在"` |
| 404 | `"消息不存在"` |
| 503 | （同上） |

### DB

- `projects`：`db.get(ProjectModel, project_id)`，取 `project.name`（fallback `project.id`）
- `qa_sessions`：`db.get(QASession, session_id)`，校验 owner

### fs 读

1. `read_messages_for_session(fs, user_id=user.id, session_id)` → 全部消息（找问题原文；异常 → `all_msgs=[]`，行 1038-1041）
2. `fs.read(_message_uri(user.id, session_id, message_id))` → 目标消息，`_split_frontmatter` 解析，取 `fm.get("sections") or []`（行 1046-1057）

**问题原文**：`all_msgs` 中所有 `role=="user"` 的**最后一条** `.content`；若无 user 消息 → `"(未知问题)"`（行 1072-1073）

### docx 构造

模式选择（行 1082-1095）：`KE_DOCX_TEMPLATE_PATH` 非空且 `os.path.isfile()` 为真 → 模板模式；否则内置模式。两者均传 `project_name=project.name or project.id`，**均不传 `generated_at`**（故 docx 内部默认 `datetime.now()`，本地时区）。

**内置模式** `build_docx(question, sections, project_name)`（`docx_exporter.py:59-111`）：
1. 页边距：左右 `Cm(2.5)`，上下 `Cm(2.0)`
2. 页眉（9pt）：`{project_name}\t\t代码知识工程 · {generated_at:%Y-%m-%d %H:%M}`（双 `\t`）
3. 页脚（8pt，居中）：`本文档由代码知识工程自动生成 · 仅供参考`
4. **主标题（Heading 0，居中）：`代码知识问答记录`**（⚠️ 修正：无"工程"二字）
5. 问题段：Bold run `"问题："` + 问题原文
6. 空行段
7. 各 section

Section 渲染（`_write_section`，行 279-305；sections 为 **dict**，用 `.get()`）：
- 标题（Heading 2）：`f"{emoji} {title}".strip()`，`title = section.get("title") or _SECTION_TITLES.get(type, type)`，`emoji = _SECTION_EMOJIS.get(type, "")`
- 内容：`content = section.get("content","") or ""`；
  - 非 call_chain：按 `\n` 逐行 `add_paragraph(line)`
  - call_chain：扫 ` ```mermaid\n...\n``` ` fence → `render_mermaid_to_png`（`mmdc` 转 PNG，宽 `Inches(6)`），失败 fallback `_add_code_block`（Courier New 9pt）；fence 外文字逐行写（跳空行）
- 引用：`refs = section.get("references") or []`；italic run `"引用："` + 每项 `List Bullet`：`f"{display_text}  [{kind}]"`（`display_text = r.get("display_text") or r.get("entity_id","?")`，`kind = r.get("kind","")`，两空格分隔）

Section type → 默认标题 / emoji（`docx_exporter.py:37-56`，逐字）：

| type | 默认中文标题 | emoji |
|------|------------|-------|
| `overview` | 业务概述 | 📋 |
| `entry_point` | 入口方法 | 🚪 |
| `call_chain` | 调用链路 | 🔀 |
| `db_ops` | 数据库操作 | 💾 |
| `rules` | 关键约束与业务规则 | ⚠️ |
| `sources` | 引用来源 | 🔗 |
| `chit-chat` | 对话回复 | 💬 |

> 未命中映射的 type：`title` fallback 用 type 字符串本身，`emoji` 用 `""`。

**模板模式** `build_docx_from_template(template_path, question, sections, project_name)`（行 117-191）：
- 占位符：`{{QUESTION}}`、`{{PROJECT_NAME}}`、`{{GENERATED_AT}}`（`%Y-%m-%d %H:%M`）做段内字符串 replace（清空 runs 后写入 `runs[0]`）；`{{SECTIONS}}` → 找到首个匹配段，原位插入多段 paragraphs 后删除占位段（`break`，只替首个）
- section 插入逻辑同内置（标题 Heading 2 + 内容逐行 + 引用 List Bullet），但**不做 mermaid PNG**（模板模式仅纯文本拆行）

### 审计

不写。`audit/actions.py:107` 有 `MESSAGE_EXPORT_DOCX = "message.export_docx"` 常量，但 `export_message` handler **未调用** `log_audit`（全局 grep 无引用）。

### 怪癖

- 文件名 `short_id = message_id.replace("msg_", "")[:8]`（如 `msg_a1b2c3d4e5f6` → `a1b2c3d4` → `qa-{project_id}-a1b2c3d4.docx`）；注意 `replace` 替换**所有** `msg_` 子串（非仅前缀）
- 仅导出 `role=="assistant"`；user 消息 → 400
- 非 owner 返 **404**（不暴露存在性），非 403
- docx 主标题与 spec 同事版本不同：实为 `代码知识问答记录`

---

## 依赖的共享设施 / 数据模型

### `_iso(dt)`（`qa_router.py:539-544`）

```python
def _iso(dt) -> str:
    if dt is None:
        return ""                                              # None → 空串，非 null
    fixed = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return fixed.isoformat().replace("+00:00", "Z")
```

- None → `""`（不是 `null`）
- naive → 视作 UTC
- `+00:00` → `Z`
- TS 等价：naive datetime 须当作 UTC 解析后 `.toISOString()`（注意 Python `isoformat()` 在含微秒时输出 `...123456Z`；但 fs/DB 路径多为整秒）

### `require_infra_healthy`（`deps_infra.py`）

- Router 级；`app.state.infra_status` None → 503 `INFRA_UNINITIALIZED`
- critical 依赖（排除 `_NON_CRITICAL_DEPS={"neo4j"}`）`ok==False` → 503 `INFRA_UNHEALTHY`
- `user.is_admin` 时 detail 追加 `"deps": state`
- 内部依赖 `get_current_user`，故 user 解析先发生

### `require_project_role(min_role)`（`permission_deps.py`）

- `ROLE_RANK = {reporter:1, maintainer:2, owner:3}`；非法 min_role 在工厂调用期 `raise ValueError`（注册期早爆）
- `is_admin` → 视作 `owner`，跳过查询（`resolve_role` 行 130）
- `resolve_role = max(直接成员 role, Group 继承链最高 role)`；Group 链向上遍历 `parent_group_id`，`depth>=3` 截断 + `visited` 防环
- 顺序：project 不存在 → 404 `"工程不存在"`；role None → 403 `"无权访问此工程"`；不足 → 403 `"需要 {min_role} 及以上权限（当前 {role}）"`
- checker 返回实际 role 字符串（本 8 路由未在函数体内消费该返回值）

### `get_current_user`（`auth_dependencies.py`）

- `OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)`
- 失败一律 401 `"Not authenticated"` + `WWW-Authenticate: Bearer`
- 检查链：token 存在 → `decode_token` → `type=="access"` → `sub` 存在且 `int()` 可解析 → user 存在 → `is_active` 真

### `qa_sessions` 表（drizzle introspect，`schema.ts:102-117`）

```
qa_sessions (
  id            varchar(64)  PK            NOT NULL
  project_id    varchar(64)  NOT NULL  FK → projects.id ON DELETE CASCADE
  user_id       int          NOT NULL  （无 FK — 保留已删用户历史）
  title         varchar(255) NULL
  created_at    datetime     NOT NULL  DEFAULT (now())
  updated_at    datetime     NOT NULL  DEFAULT (now())
  message_count int          NOT NULL  （⚠️ 修正：无 DEFAULT）
  archived_at   datetime     NULL      （NULL=活动，非 NULL=已归档）
  title_custom  tinyint      NOT NULL  DEFAULT 0
)
INDEX idx_qa_sessions_project_user (project_id, user_id, updated_at)
INDEX idx_qa_sessions_user_archived (user_id, archived_at)
```

软删语义：`archived_at IS NULL` 为活动；list 默认过滤、rename 拒绝、archive/unarchive 操作此字段。

### `projects` 表（export 用，`schema.ts:78-99`）

```
projects ( id varchar(64) PK, name varchar(128) NOT NULL, ... )
```
export 取 `project.name or project.id`。

### 消息文件系统（fs，S6/S7）

- 消息真相源：`ke://u/{user_id}/session/{session_id}/messages/{msg_id}.md`
- message frontmatter 顺序：`role`、`created_at`（`%Y-%m-%dT%H:%M:%SZ`）、可选 `sections`、可选 `msg_metadata`；body = `content + "\n"`（`session.py:263-273`）
- 反馈：`ke://u/{user_id}/session/{session_id}/messages/{msg_id}.feedback.md`；frontmatter 顺序 `vote`（可 null）、`user_id`、`created_at`；body = `comment + "\n"`（`session.py:407-413`）
- `read_messages_for_session` 过滤：`endswith(".md") and not endswith(".feedback.md")`；缺 `role`/`created_at` 跳过；损坏文件 `_log.debug` 跳过；目录不存在（`MemoryNotFound`）→ `[]`
- 排序键：`(created_at, 0 if role=="user" else 1)` 升序
- `_FsMessage` 字段：`role / content / msg_metadata / created_at / sections`（无 `id`、无 `session_id`）

### `audit_logs` 表 / actions

```
audit_logs ( id int AUTO_INCREMENT PK, actor_user_id int FK→users ON DELETE SET NULL,
             action varchar(64) NOT NULL, resource_type varchar(32) NOT NULL,
             resource_id varchar(128) NOT NULL, metadata_json text,
             ip_address varchar(45), created_at datetime DEFAULT now() )
```

sessions 族 8 条路由**均不调用** `log_audit`。`audit/actions.py:107` 定义 `MESSAGE_EXPORT_DOCX = "message.export_docx"` 但全仓无引用（死常量）。

---

**TS 移植关键提醒（高优先级）**：
1. docx 主标题字符串务必用 `代码知识问答记录`（**不要**加"工程"）。
2. `qa_sessions.message_count` 在 TS schema/迁移里**不要**加 `DEFAULT 0`（introspect 真相为无默认，插入时必须显式给值，否则 MySQL 报错或行为依赖 sql_mode）。
3. feedback 校验非法值返 **422**（FastAPI Pydantic 形态），rename title 越界返 **400**（手写校验）——两者错误形态不同，移植时勿统一。
4. export 非 owner 返 **404** 而非 403；feedback 端点不接 db、不校验存在性。
5. `sections` 是普通 dict/JSON 对象（用键访问），非带方法的对象。

---

# 【archived 族】

All evidence gathered. The OpenAPI archive only declares a `200` response (FastAPI default — error responses from guards aren't auto-documented), `title` is the only nullable field, no params, and there's no soft-delete column. Every material spec claim checks out against source. Now I'll produce the corrected full spec.

## 修正清单

逐条回源核验后，**该 spec 的实质性行为断言全部准确**——状态码、detail 文案（全角逗号、中文原样）、守卫顺序、JWT 失败语义、`_iso()` 怪癖、排序两级（sessions `archived_at DESC` / groups `project_name ASC`）、孤儿 project 回退、无审计、无分页、字段集（`title` 唯一 nullable）、`archived_at IS NOT NULL` 过滤、无软删字段——均与 `archived_router.py`、`deps_infra.py`、`auth_dependencies.py`、`db_models_homepage.py`、OpenAPI 存档一致。

仅有 **3 处需要修正/澄清的细节**，均不改变对外行为，但会误导 TS 移植者：

| # | 严重度 | 位置 | spec 原文 | 实情 | 影响 |
|---|--------|------|-----------|------|------|
| **M1** | 中 | DB 节「步骤 1 索引」 | 称查询「走 `idx_qa_sessions_project_user(project_id, user_id, updated_at)`……走部分扫描」 | 该索引**前导列是 `project_id`**，而本查询 WHERE 只有 `user_id`（无 `project_id`），ORDER BY 是 `archived_at`。该复合索引对本查询**前导列不可用**（leftmost-prefix 不匹配），优化器更可能全表扫 + filesort，而非「部分扫描」。源码注释 line 67 自称「复合索引 user_id + archived_at 覆盖」也是**错的**（不存在该索引）。spec 沿用了源码注释的错误。 | 移植到 TS/drizzle 时若照搬「走该索引」的假设，会错误判断性能特征；实际无可用索引，与「全量无分页」叠加是真实性能隐患。 |
| **M2** | 低 | 成功响应「Content-Type」、错误节 | spec 列出 `401`/`503` 为本路由错误响应 | 行为正确，但需注明：**OpenAPI 存档仅声明 `200`**（`responses` 只有 `200` 键），`401/503` 来自守卫 `raise HTTPException`，FastAPI 不自动写入该路由的 OpenAPI `responses`。这是 FastAPI 默认行为，不是 spec 错误，但 TS 端若以 OpenAPI 为唯一契约会漏掉错误形状。 | 提醒移植者：错误响应形状权威来自守卫源码，不是 OpenAPI。 |
| **M3** | 低 | 「与 sessions 族 list 的关系」表 | 断言 sessions 族「按 `updated_at DESC`」「`require_project_role("reporter")`」「有分页 `skip`/`limit`」 | 这些是**对 sessions 族（`qa_router`）的断言，不在本次核验源码范围内**（本任务只给了 `archived_router.py`）。无法在本次回源中证实，标注为「未核验，需以 qa_router 源码为准」。 | 防止把未经核验的对照表当成权威。 |

下面是修正后的完整规范。

---

## `GET /user/archived-sessions` — 跨工程归档会话汇总

模块：`src/service/archived_router.py`；router `prefix="/user"`，`tags=["user-archived"]`；线上经 vite 代理 / nginx 加 `/api` 前缀，完整路径 `/api/user/archived-sessions`。

### 守卫（顺序）

| 顺序 | 守卫 | 来源 | 失败响应 |
|------|------|------|---------|
| 1 | `require_infra_healthy` | Router 级 `dependencies=[Depends(require_infra_healthy)]`（line 34） | 见错误节 |
| 2 | `get_current_user` | `require_infra_healthy` 的依赖参数 `user: User = Depends(get_current_user)`（deps_infra line 28），FastAPI 先解析子依赖 → JWT 校验先于基础设施判断 | `401 Not authenticated` |
| 3 | 基础设施健康检查 | `require_infra_healthy` 函数体（deps_infra line 42-77） | `503 INFRA_UNINITIALIZED` / `INFRA_UNHEALTHY` |
| 4 | `get_current_user`（再次） | 路由函数签名 `user: User = Depends(get_current_user)`（line 63） | 同 401；FastAPI 依赖缓存，同一请求内只实际执行一次 |

**顺序细节（已回源确认）**：`require_infra_healthy(request: Request, user: User = Depends(get_current_user))`。FastAPI 在执行函数体（读 `app.state.infra_status`）前必须先解析全部子依赖，因此：
- 无 / 无效 token → `401`，**不进入**基础设施检查；
- token 有效但 critical 依赖 down → `503`。

本路由**无** `require_project_role` / `require_group_role`，不做任何工程级 RBAC。数据隔离仅靠查询条件 `user_id == user.id`（line 70）。

---

### 请求

- **Path 参数**：无
- **Query 参数**：无（无 `skip`/`limit`/`cursor`/排序/过滤——全量返回，见怪癖 1）。OpenAPI 存档 `parameters: None` 已确认。
- **Body**：无（`GET`）
- **Headers**：`Authorization: Bearer <access_token>`（必选）。OpenAPI `security: [{OAuth2PasswordBearer: []}]`。

---

### 成功响应

**Status**：`200 OK`　**Content-Type**：`application/json`

响应模型 `ArchivedListResponse`（line 55-57），形状：

```json
{
  "by_project": [
    {
      "project_id": "<string>",
      "project_name": "<string>",
      "sessions": [
        {
          "id": "<string>",
          "title": "<string | null>",
          "archived_at": "<ISO 8601, Z 结尾>",
          "created_at": "<ISO 8601, Z 结尾>",
          "updated_at": "<ISO 8601, Z 结尾>",
          "message_count": "<integer>"
        }
      ]
    }
  ]
}
```

**逐字段**（pydantic + OpenAPI required 已交叉确认）：

| 字段路径 | 类型 | required | nullable | 说明 |
|---------|------|----------|----------|------|
| `by_project` | `array` | ✅（OpenAPI required） | 否 | `_ProjectGroupDTO[]`；空时为 `[]` |
| `by_project[].project_id` | `string` | ✅ | 否 | `projects.id`（业务可读串，如 `deposit-system`） |
| `by_project[].project_name` | `string` | ✅ | 否 | `projects.name`；孤儿 session（project 已删）回退为 `project_id` 串本身（line 100：`projects.get(pid).name if pid in projects else pid`） |
| `by_project[].sessions` | `array` | ✅ | 否 | `_ArchivedSessionDTO[]`，永不为 null |
| `sessions[].id` | `string` | ✅ | 否 | `qa_sessions.id`（`VARCHAR(64)` PK，如 `sess_abc123`） |
| `sessions[].title` | `string` | ❌（OpenAPI 非 required，`anyOf [string,null]`，pydantic 默认 `None`） | ✅ | `qa_sessions.title`，未生成标题时 `null` |
| `sessions[].archived_at` | `string` | ✅ | 否 | `_iso(archived_at)`；naive → 附加 UTC，`+00:00`→`Z`（如 `2026-06-11T08:30:00Z`） |
| `sessions[].created_at` | `string` | ✅ | 否 | 同格式 |
| `sessions[].updated_at` | `string` | ✅ | 否 | 同格式 |
| `sessions[].message_count` | `integer` | ✅ | 否 | `qa_sessions.message_count` 缓存值；`0`=无消息 |

> 注：DTO 中 `title` 类型注解为 `Optional[str] = None`，故 OpenAPI 把它列为**非必填**；其余 5 个字段（含 `archived_at`/`created_at`/`updated_at`/`message_count`/`id`）均 required。TS 端类型应为 `title?: string | null`，其余非可选。

**排序（两级，已回源）**：
1. `sessions` 内部：SQL `ORDER BY archived_at DESC`（line 71），最新归档排最前；
2. `by_project` 数组：Python `sorted(..., key=lambda g: g.project_name or "")`（line 96/105），按 `project_name` 升序，`None` 折叠为 `""`。

---

### 错误响应

| Status | detail（逐字，全角逗号原样） | 触发条件 |
|--------|------------------------------|---------|
| `401` | `"Not authenticated"`（响应头 `WWW-Authenticate: Bearer`） | token 缺失 / `decode_token` 返回 None（签名错或过期，见 auth_security line 88-93）/ `payload.type != "access"` / `sub` 缺失 / `sub` 非整数（`int()` 抛 ValueError）/ user 不存在 / `is_active == False` |
| `503` | `{"code": "INFRA_UNINITIALIZED", "message": "系统正在初始化，请稍后重试"}` | `app.state.infra_status` 为 None（startup 未完成 / 被跳过） |
| `503` | `{"code": "INFRA_UNHEALTHY", "message": "系统暂时不可用，请联系管理员"}` | 至少一个 critical 依赖 `v.get("ok")` 假值；普通用户 detail 仅这两字段 |
| `503` | `{"code": "INFRA_UNHEALTHY", "message": "系统暂时不可用，请联系管理员", "deps": {<infra_status 全量 map>}}` | 同上但 `user.is_admin == True`，追加 `deps`（直接引用 `state`，非拷贝，deps_infra line 74） |

约束：
- `neo4j ∈ _NON_CRITICAL_DEPS`，其 down **不**触发 503（deps_infra line 23、56-59）。
- **M2**：OpenAPI 存档仅声明本路由 `200`；`401`/`503` 由守卫 `raise HTTPException` 产生，FastAPI 默认不写入该路由 OpenAPI `responses`。**错误形状以守卫源码为权威，勿以 OpenAPI 为唯一契约。**
- detail 文案逐字：`"系统正在初始化，请稍后重试"`、`"系统暂时不可用，请联系管理员"`——均含**全角逗号 `，`**，无句末标点。

---

### DB

无显式事务，同一 `AsyncSession` 内顺序执行两条 `SELECT`（自动提交语义）。

| 顺序 | 表 | 操作 | 条件 | 备注 |
|------|----|------|------|------|
| 1 | `qa_sessions` | `SELECT QASession` | `user_id = :uid AND archived_at IS NOT NULL`，`ORDER BY archived_at DESC`（line 68-72） | **M1 修正**：唯一索引 `idx_qa_sessions_project_user(project_id, user_id, updated_at)` 前导列为 `project_id`，本查询 WHERE 无 `project_id`，**leftmost-prefix 不匹配 → 该索引不可用**；实际多为全表扫 + filesort。源码 line 67 注释「复合索引 user_id + archived_at 覆盖」**描述了一个并不存在的索引**，勿照搬。 |
| 2 | `projects` | `SELECT Project` | `id IN (:project_ids)`（line 78） | 仅当 step 1 结果非空才执行（line 77）；`project_ids = sorted({r.project_id for r in rows})`（line 76，去重 + 字典序排序） |

读取列：`qa_sessions` 全行映射对象（实际用到 `id/title/archived_at/created_at/updated_at/message_count/project_id`）；`projects` 实际只用 `id`（dict key）和 `name`（line 100）。

---

### 审计

**不写审计。** `archived_router.py` 无任何 `audit_log` / audit 设施 import 或调用（grep 确认）。纯只读路由，不产生审计事件。

---

### 怪癖（已逐条回源）

1. **全量无分页**：无 `skip`/`limit`/`cursor`。叠加 **M1**（无可用索引），用户归档量大时是真实大响应 + 慢查询隐患。
2. **孤儿 project 回退**：step 2 若某 `project_id` 在 `projects` 已删（`qa_sessions` 因 `user_id` 不加 FK、`project_id` 虽 CASCADE 但理论边界仍可能残留），`pid not in projects`，`project_name` 回退为 `pid` 串本身（line 100），不抛错。
3. **`_iso()` 时区修正**：naive datetime（`tzinfo is None`）强制 `replace(tzinfo=timezone.utc)`，`isoformat()` 后 `+00:00`→`Z`（line 20-26）。`dt is None` 时返回 `""`；但本路由 `archived_at` 已被 `IS NOT NULL` 过滤、`created_at`/`updated_at` 列 `NOT NULL`，故实际三个字段都不会出现 `""`。
4. **`project_ids` 排序无对外效果**：`sorted({...})`（line 76）仅决定 `by_pid` dict 插入顺序；最终 groups 顺序由 line 96 的 `project_name` 排序覆盖，`project_id` 字典序不影响输出。
5. **`title` vs `title_custom`**：DTO 只暴露 `title`（line 41）；`title_custom`（标记用户是否手动重命名，`Boolean server_default text("0")`）**不出现在响应**。
6. **无软删过滤**：`QASession` 与 `Project` 均无 `deleted_at`/软删列（grep 确认）；本路由不需软删过滤。归档语义靠 `archived_at` 非空表达，**不是**软删。
7. **依赖缓存导致 `get_current_user` 只执行一次**：守卫与路由函数都声明 `Depends(get_current_user)`，FastAPI 同请求内缓存，JWT 仅解析一次。

---

### 与 sessions 族 list 的关系（**M3：未核验，仅供参考**）

> 本次核验范围仅 `archived_router.py`。下表对 sessions 族（`qa_router`）的断言**未回源验证**，移植时须以 `qa_router` 源码为准。

| 维度 | `GET /user/archived-sessions`（已核验） | sessions 族（**未核验**） |
|------|----------------------------------------|---------------------------|
| 范围 | 跨工程，当前用户全部 | 单工程（path `project_id`）|
| 过滤 | `archived_at IS NOT NULL` | 待核验（推测过滤活动会话）|
| 权限 | 仅 JWT + 基础设施，无 RBAC | 待核验（推测 `require_project_role`）|
| 分组 | 按 project `by_project[]` | 待核验 |
| 分页 | 无 | 待核验 |
| 排序 | sessions `archived_at DESC` / groups `project_name ASC` | 待核验 |

---

## 依赖的共享设施 / 数据模型

### `get_current_user`（`src/service/auth_dependencies.py:20-49`）
- `oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)`——`auto_error=False`，故 token 缺失时由函数体自己 raise（line 30-31），而非 FastAPI 自动 403。
- `decode_token(token)`（`auth_security.py:88-93`）：`jwt.decode(token, secret, algorithms=[alg])`，**校验签名与过期**，`JWTError` → 返回 `None`。
- 校验链（任一失败 → `401 "Not authenticated"` + `WWW-Authenticate: Bearer`）：`payload.type == "access"` → `sub` 存在 → `int(sub)` 可解析 → `users.id` 查到 → `is_active == True`。

### `require_infra_healthy`（`src/service/deps_infra.py:26-77`）
- 读 `request.app.state.infra_status`（startup 写入）。
- `_NON_CRITICAL_DEPS = {"neo4j"}`（line 23），neo4j down 不触发 503。
- `unhealthy = [k for k,v in state.items() if k not in _NON_CRITICAL_DEPS and not v.get("ok")]`（line 56-59）。
- admin（`user.is_admin`）503 detail 追加 `deps`（line 73-74，引用整张 `state` map）。

### `QASession`（`src/service/db_models_homepage.py:198-249`，表 `qa_sessions`）

| 列 | 类型 | nullable | 说明 |
|----|------|----------|------|
| `id` | `String(64)` PK | 否 | 如 `sess_abc123` |
| `project_id` | `String(64)` FK→`projects.id` `ondelete=CASCADE` | 否 | 删工程级联删会话 |
| `user_id` | `Integer` | 否 | 对应 `users.id`，**不加 FK**（保留已删用户历史）|
| `title` | `String(255)` | 是 | |
| `created_at` | `DateTime` `server_default=func.now()` | 否 | naive，存 UTC |
| `updated_at` | `DateTime` `server_default=func.now()` `onupdate=func.now()` | 否 | naive，存 UTC |
| `message_count` | `Integer` `default=0` | 否 | 缓存字段 |
| `archived_at` | `DateTime` `default=None` | 是 | NULL=活动；非NULL=归档时刻 |
| `title_custom` | `Boolean` `server_default=text("0")` `default=False` | 否 | 不出现在本路由响应 |

索引：唯一一条 `idx_qa_sessions_project_user("project_id","user_id","updated_at")`（line 245-248）。**注意（M1）**：前导列 `project_id`，本路由查询不带 `project_id`，该索引对本查询不可用；亦不含 `archived_at`。**无任何以 `user_id` 或 `archived_at` 为前导列的索引。**

### `Project`（`src/service/db_models_homepage.py:46`，表 `projects`）
- `id`：`String(64)` PK（业务可读串）；`name`：`String(128) NOT NULL`。本路由仅读这两列。
- 无软删列。

### `_iso()`（`archived_router.py:20-26`，模块私有）
签名 `_iso(dt: datetime) -> str`：`dt is None`→`""`；naive→附加 `timezone.utc`；`isoformat()` 后 `+00:00`→`Z`。本路由三个时间字段均不触发 `""` 分支。