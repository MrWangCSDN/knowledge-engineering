# P6 后端「自助 create+bind」端点 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `POST /scm/connections/{connection_id}/projects`，让仓库管理员（SCM can_bind）自助从一个连接的某仓创建 KE 工程并入队首次索引，无需 admin 先建工程。

**Architecture:** 在现有 `create_scm_binding_routes`（`scm_binding_router.py`）里加一个路由——它已注入 `authorize_scm`/`get_current_user`/`get_db` 且已在 `api.py` 挂载。先核验后写（连接归属 → SCM can_bind 门 → project_id 冲突 → 建 Project + UserProjectAccess(owner) + enqueue full_index）。复用 `authorize_scm`(P4b-1)、`enqueue_index_job`、`Project`/`UserProjectAccess` ORM。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x async / pytest + pytest-asyncio + TestClient（内存 SQLite）。分支：off `release-0513`。

**安全基线（与 spec §4 的细化）**：本端点是**全新端点**，SCM can_bind 门**始终生效**，**不接 `KE_SCM_BIND_AUTHZ` 的 off 旁路**（避免造出一条"无鉴权也能建工程"的新路径）。PAT 连接无 SCM role，以**连接归属**为授权门（步骤 1 已校验）。

---

## File Structure

- **Modify** `src/service/scm_binding_router.py`：
  - 顶部 import 加 `UserProjectAccess`（来自 `db_models_homepage`）。
  - 加 `CreateProjectBindRequest(BaseModel)`。
  - 在 `create_scm_binding_routes` 内加 `@router.post("/scm/connections/{connection_id}/projects")` 处理函数。
- **Create** `tests/test_auth/test_scm_create_project.py`：端点全行为单测（照搬 `test_scm_binding_rbac.py` 的 session_maker + client 范式，注入 fake `authorize_scm`）。
- `api.py` **无需改**（`create_scm_binding_routes` 已挂载且已传 `authorize_scm=_authorize_scm`）。

参考签名（已核对）：
- `authorize_scm(db, *, user, conn, repo_full_name, repo_external_id, need_bind=True) -> ScmRole`（`ScmRole.CAN_BIND/CAN_QUERY/NOT_VISIBLE`）。
- `enqueue_index_job(session, *, project_id, type_, trigger, dedup_key=None, commit_sha=None) -> IndexJob`（`.id`）。
- `ScmConnection`：`id`/`user_id`/`provider`/`auth_type`（`"github_app"` | `"pat"`）。
- `Project`：`id`(str pk)/`name`(必填)/`status`(默认 indexing)/`scm_connection_id`/`repo_external_id`(BigInteger)/`repo_full_name`/`ref`/`ref_type`/`subpath`/`created_by`(可空)。
- `UserProjectAccess(user_id:int, project_id:str, role:str)`，role∈{reporter,maintainer,owner}。

---

## Task 1：请求模型 + 路由骨架 + 连接归属 404

**Files:**
- Modify: `src/service/scm_binding_router.py`
- Test: `tests/test_auth/test_scm_create_project.py`

- [ ] **Step 1: 写失败测试**（照搬 `tests/test_auth/test_scm_binding_rbac.py` 的 `session_maker`+`client`+`_login` fixture；额外 seed 一个 alice 拥有的 `ScmConnection(id="c1", user_id=<alice.id>, provider="github", auth_type="github_app")` 和一个 bob 拥有的 `ScmConnection(id="c2", user_id=<bob.id>, ...)`；client 注入 `authorize_scm` = 返回 `ScmRole.CAN_BIND` 的 async fake）

```python
def test_create_project_unknown_connection_404(client):
    """连接不存在 → 404。"""
    tok = _login(client, "alice", "12345678")
    r = client.post("/scm/connections/nope/projects",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"project_id": "newp", "name": "New", "repo_external_id": 1,
                          "repo_full_name": "o/r", "ref": "main"})
    assert r.status_code == 404

def test_create_project_connection_not_owned_404(client):
    """连接属于他人（c2 是 bob 的）→ alice 调用 404，不泄露存在性。"""
    tok = _login(client, "alice", "12345678")
    r = client.post("/scm/connections/c2/projects",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"project_id": "newp", "name": "New", "repo_external_id": 1,
                          "repo_full_name": "o/r", "ref": "main"})
    assert r.status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `KE_JWT_SECRET=$(python3 -c "print('x'*32)") KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=$(./venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())") ./venv/bin/python -m pytest tests/test_auth/test_scm_create_project.py -q`
Expected: FAIL（404 路由不存在 → 实际 404 但因路由缺失，TestClient 返回 404 Not Found 路由级；用 `assert "detail" in r.json()` 或先让它因 KeyError/路由缺失失败。更稳：断言 `r.json()["detail"] == "连接不存在"` → 路由没建时拿不到该 detail → FAIL）

- [ ] **Step 3: 加模型 + 路由骨架 + 归属校验**

在 `scm_binding_router.py` 顶部 import 增加 `UserProjectAccess`：
```python
from src.service.db_models_homepage import Project, IndexJob, ScmConnection, UserProjectAccess
```
加请求模型（放 `BindRequest` 下面）：
```python
class CreateProjectBindRequest(BaseModel):
    """自助连接：从某 SCM 连接的某仓创建工程并绑定。"""
    project_id: str
    name: str
    repo_external_id: int
    repo_full_name: str
    ref: str
    ref_type: str = "branch"
    subpath: Optional[str] = None
```
在 `create_scm_binding_routes` 内（与 bind 同级）加：
```python
    @router.post("/scm/connections/{connection_id}/projects")
    async def create_project_from_connection(
        connection_id: str, body: CreateProjectBindRequest,
        user=Depends(get_current_user), db=Depends(get_db),
    ) -> dict:
        # 1) 连接归属：不存在 / 非本人 → 404（不泄露他人连接存在性）
        conn = await db.get(ScmConnection, connection_id)
        if conn is None or conn.user_id != user.id:
            raise HTTPException(status_code=404, detail="连接不存在")
        # 占位：后续任务补 SCM 门 / 冲突 / 建工程
        raise HTTPException(status_code=501, detail="未实现")
```

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2 命令
Expected: 两个 404 测试 PASS（501 占位不影响这两条）。

- [ ] **Step 5: 提交**

```bash
git add src/service/scm_binding_router.py tests/test_auth/test_scm_create_project.py
git commit -m "feat(scm): create+bind 端点骨架 + 连接归属 404"
```

---

## Task 2：SCM can_bind 门（App 强制；PAT 以连接归属为门）

**Files:**
- Modify: `src/service/scm_binding_router.py`
- Test: `tests/test_auth/test_scm_create_project.py`

- [ ] **Step 1: 写失败测试**（client fixture 支持按测试切换注入的 `authorize_scm` 返回值；建议做成可参数化的 fake：默认返 `CAN_BIND`，单测里改 `fake.role = ScmRole.CAN_QUERY`）

```python
def test_create_project_not_can_bind_403(client_with_role):
    """authorize_scm 返回 CAN_QUERY（非 can_bind）→ 403。"""
    client = client_with_role(ScmRole.CAN_QUERY)
    tok = _login(client, "alice", "12345678")
    r = client.post("/scm/connections/c1/projects",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"project_id": "newp", "name": "New", "repo_external_id": 1,
                          "repo_full_name": "o/r", "ref": "main"})
    assert r.status_code == 403

def test_create_project_pat_skips_scm_gate(client_with_pat_conn):
    """PAT 连接（auth_type='pat'）→ 跳过 SCM 门，即使 authorize_scm 会拒也不调用。"""
    client = client_with_pat_conn(deny=True)  # authorize_scm 返回 CAN_QUERY，但 PAT 不应调用它
    tok = _login(client, "alice", "12345678")
    r = client.post("/scm/connections/cpat/projects",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"project_id": "patp", "name": "Pat", "repo_external_id": 9,
                          "repo_full_name": "o/p", "ref": "main"})
    assert r.status_code != 403   # PAT 连接归属已过 → 不被 SCM 门拦（最终 501/200，本任务后是 501）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `... -m pytest tests/test_auth/test_scm_create_project.py -q`
Expected: `test_create_project_not_can_bind_403` FAIL（当前骨架对所有 conn 走到 501，没 403）。

- [ ] **Step 3: 加 SCM 门**（替换 Task1 的"占位 raise 501"前面那段）

```python
        # 2) SCM 授权门：App 连接必须 can_bind；PAT 以连接归属为门（步骤1已校验）。
        #    本端点始终强制 can_bind，不接 KE_SCM_BIND_AUTHZ off 旁路（避免无鉴权新建路径）。
        if conn.auth_type != "pat":
            if authorize_scm is None:                       # 装配缺失：安全失败而非放行
                raise HTTPException(status_code=503, detail="SCM 授权未装配")
            role = await authorize_scm(
                db, user=user, conn=conn,
                repo_full_name=body.repo_full_name,
                repo_external_id=body.repo_external_id,
                need_bind=True,
            )
            if role != ScmRole.CAN_BIND:
                raise HTTPException(status_code=403, detail="无该仓 maintainer/admin 权限，不能连接")
        # 占位：后续任务补 冲突 / 建工程
        raise HTTPException(status_code=501, detail="未实现")
```

- [ ] **Step 4: 跑测试确认通过**

Run: 同上
Expected: 403 测试 + PAT 测试 PASS。

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat(scm): create+bind SCM can_bind 门（App 强制/PAT 连接归属）"
```

---

## Task 3：project_id 冲突 → 409

**Files:**
- Modify: `src/service/scm_binding_router.py`
- Test: `tests/test_auth/test_scm_create_project.py`

- [ ] **Step 1: 写失败测试**（session_maker 预 seed 一个 `Project(id="taken", name="X")`）

```python
def test_create_project_id_collision_409(client):
    """project_id 已存在 → 409，不覆盖既有工程。"""
    tok = _login(client, "alice", "12345678")
    r = client.post("/scm/connections/c1/projects",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"project_id": "taken", "name": "Dup", "repo_external_id": 1,
                          "repo_full_name": "o/r", "ref": "main"})
    assert r.status_code == 409
```

- [ ] **Step 2: 跑测试确认失败**

Expected: FAIL（当前到 501）。

- [ ] **Step 3: 加冲突检查**（替换"占位 raise 501"）

```python
        # 3) project_id 冲突 → 409（不覆盖既有工程）
        if await db.get(Project, body.project_id) is not None:
            raise HTTPException(status_code=409, detail="工程 ID 已存在")
        # 占位：下一任务补建工程
        raise HTTPException(status_code=501, detail="未实现")
```

- [ ] **Step 4: 跑测试确认通过** → 409 PASS。

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat(scm): create+bind project_id 冲突 409"
```

---

## Task 4：建工程 + owner 授权 + 入队 full_index（happy path）

**Files:**
- Modify: `src/service/scm_binding_router.py`
- Test: `tests/test_auth/test_scm_create_project.py`

- [ ] **Step 1: 写失败测试**

```python
def test_create_project_happy_path(client, session_maker):
    """成功：建 Project（绑定列全填）+ UserProjectAccess(owner) + 入队 full_index，返回 {project_id, job_id}。"""
    import asyncio
    from sqlalchemy import select
    from src.service.db_models_homepage import Project, UserProjectAccess, IndexJob
    tok = _login(client, "alice", "12345678")
    r = client.post("/scm/connections/c1/projects",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"project_id": "newp", "name": "New Proj", "repo_external_id": 42,
                          "repo_full_name": "org/repo", "ref": "master", "subpath": "mall-portal"})
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == "newp" and body["job_id"]

    async def _check():
        async with session_maker() as s:
            p = await s.get(Project, "newp")
            assert p is not None and p.status == "indexing"
            assert p.scm_connection_id == "c1" and p.repo_external_id == 42
            assert p.repo_full_name == "org/repo" and p.ref == "master"
            assert p.ref_type == "branch" and p.subpath == "mall-portal"
            # owner 授权写入
            acc = (await s.execute(select(UserProjectAccess).where(
                UserProjectAccess.project_id == "newp"))).scalars().all()
            assert len(acc) == 1 and acc[0].role == "owner"
            # full_index 作业入队
            jobs = (await s.execute(select(IndexJob).where(
                IndexJob.project_id == "newp"))).scalars().all()
            assert len(jobs) == 1 and jobs[0].type == "full_index"
    asyncio.run(_check())
```

- [ ] **Step 2: 跑测试确认失败** → FAIL（当前到 501）。

- [ ] **Step 3: 实现 happy path**（替换"占位 raise 501"为真正建工程）

```python
        # 4) 建工程 + 创建者 owner + 入队首次全量索引
        p = Project(
            id=body.project_id, name=body.name, status="indexing",
            scm_connection_id=connection_id,
            repo_external_id=body.repo_external_id,
            repo_full_name=body.repo_full_name,
            ref=body.ref, ref_type=body.ref_type, subpath=body.subpath,
            created_by=str(user.id),
        )
        db.add(p)
        db.add(UserProjectAccess(user_id=user.id, project_id=body.project_id, role="owner"))
        # flush：先把 Project 落库，确保 enqueue 的 IndexJob 外键 project_id 有效（同事务内）
        await db.flush()
        job = await enqueue_index_job(
            db, project_id=body.project_id, type_="full_index", trigger="manual")
        await db.commit()
        return {"project_id": body.project_id, "job_id": job.id}
```
（注意：`body` 此处指请求体变量 `body: CreateProjectBindRequest`；不要与测试里的 `r.json()` 混淆。）

- [ ] **Step 4: 跑测试确认通过** → happy path PASS；本文件全绿。

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat(scm): create+bind 建工程+owner+入队 full_index"
```

---

## Task 5：全量回归 + 对抗评审

**Files:** 无新增（验证 + 评审）

- [ ] **Step 1: 跑全量 test_auth 回归**

Run: `KE_JWT_SECRET=$(python3 -c "print('x'*32)") KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=$(./venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())") ./venv/bin/python -m pytest tests/test_auth -q`
Expected: 全绿（新增 6 测试 + 既有全过）。

- [ ] **Step 2: 对抗评审（同 P4b-2 规格）**

派一个评审 agent 按 4 维度核验：① 鉴权（App 必 can_bind、PAT 连接归属、authorize_scm=None→503 不放行）；② 先核验后写（403/404/409 路径**零写入** DB——加一条断言"被拒后 Project/UserProjectAccess/IndexJob 均未新增");③ 事务原子性（flush 顺序、commit 失败回滚不留半条);④ 越权（他人连接 404、project_id 冲突不覆盖）。修掉发现项后回到 Step 1。

- [ ] **Step 3: 终态提交**

```bash
git add -A && git commit -m "test(scm): create+bind 全量回归 + 对抗评审修复"
```

---

## Self-Review（已过）

- **Spec 覆盖**：spec §4（端点/逻辑 1-8/PAT/安全规格）→ Task1-5 全覆盖。
- **占位扫描**：无 TBD；每步有真实测试+实现代码。
- **类型一致**：`CreateProjectBindRequest` 字段、`authorize_scm`/`enqueue_index_job` 签名、`UserProjectAccess(user_id,project_id,role)`、`Project` 字段均与现有代码核对一致。
- **安全细化**：相对 spec §4 把"复用 KE_SCM_BIND_AUTHZ"细化为"新端点始终强制 can_bind、不接 off 旁路"——更安全，已在评审维度①固化。
