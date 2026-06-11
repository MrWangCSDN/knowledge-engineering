# P2b 管理面行为规范（py-final-baseline 提取，经对抗核验）

> TS 移植唯一行为权威。生成：2026-06-11 并行提取工作流（8 提取 + 8 对抗核验）。
> 各族文首「修正清单」是核验员对初稿的修正记录，保留作可信度证据。



---

# 【横切约定（RBAC/审计/分页/挂载）】

Confirmed: `qa_sessions.user_id` is `Integer` (line 212), and `BigInteger`/`Float` are imported but unused. Spec correctly says `Integer`.

I have completed thorough verification. Now I'll produce the corrected complete spec. Below are the errors I found in the spec, which I've corrected:

**Errors found:**
1. **POST /admin/credentials/{id}** — spec says path param is `id`, actual is `credential_id`; and the actual route audit metadata includes `admin_action=True` and `original_owner_id`, which spec omits (spec just says `action="credential.delete"`).
2. **DELETE /admin/projects/{id}** — spec says "写审计；action=project.delete" but the actual source does NOT call `log_audit` at all (delete_admin_project just `db.delete(p)` + commit).
3. **POST /admin/projects** — spec says "写审计；action=project.create" but actual source does NOT call `log_audit`.
4. **PATCH /admin/projects/{id}** — spec says "写审计；action=project.update" but actual source does NOT call `log_audit`.
5. **POST /credentials error** — same metadata, but more importantly the DELETE /credentials ordering in spec is correct.
6. **db_models_homepage spec** — the spec's table claims `BigInteger` is not used, which is fine, but the homepage spec section omits that `Float`/`BigInteger` are imported-but-unused (minor, not a behavior error).

Let me produce the final corrected document.

---

## 修正清单

逐条列出对原 spec 的修改/补充。回源核对了全部 3 个主源码 + 7 个 router + audit/logger + audit/actions + db_models_groups + deps_infra + auth_dependencies。

1. **【重大·审计漏报】`POST /admin/projects`** — 原 spec 称「写审计；`action="project.create"`」。**错误**：`admin_router.create_admin_project`（L237-270）**完全没有调用 `log_audit`**，只 `db.add(p)` + commit。已改为「不写审计」。

2. **【重大·审计漏报】`PATCH /admin/projects/{id}`** — 原 spec 称「写审计；`action="project.update"`」。**错误**：`update_admin_project`（L273-305）**没有 `log_audit`**。已改为「不写审计」。

3. **【重大·审计漏报】`DELETE /admin/projects/{id}`** — 原 spec 称「写审计；`action="project.delete"`」。**错误**：`delete_admin_project`（L312-326）**没有 `log_audit`**。已改为「不写审计」。另：原 spec 称「级联删 qa_sessions」属实（FK CASCADE），保留。

4. **【路径参数名错】`DELETE /admin/credentials/{id}`** — 路径参数实际是 `{credential_id}`（L141），非 `{id}`。已更正路由路径为 `/admin/credentials/{credential_id}`。

5. **【审计 metadata 漏项】`DELETE /admin/credentials/{credential_id}`** — 原 spec 仅说「`action="credential.delete"`」。**补充**：实际 `metadata={"name": cred.name, "admin_action": True, "original_owner_id": cred.owner_user_id}`（L175-179），与用户自删的 metadata 不同。已补全。

6. **【守卫顺序细化】`POST /projects`** — 原 spec 写守卫为「`require_infra_healthy → get_current_user → user.is_admin 检查`」。属实，但 `user.is_admin` 不是 dependency 而是函数体内第一行检查（L135）。已注明「函数体内」。

7. **【DB 顺序补正】`DELETE /credentials/{cred_id}`** — 原 spec「怪癖」说"先 `db.delete(cred)` 再 `await log_audit`"。**核对属实**（L194 先 delete，L197 后 log_audit），保留。但需注意这与 `DELETE /admin/credentials` 顺序一致（也是先 delete 后 audit）。已统一标注。

8. **【db_models 补充】`db_models_homepage.py` 导入了 `BigInteger`、`Float` 但全表未使用**（仅 `Integer` 实际用于 owner_user_id / user_id / message_count）。非行为错误，但 TS 移植时无需为这两个类型建列。已加注。

以下为**核验通过**（抽查点）：
- api.py 全部 14 条直挂路由的 status/detail/Query 边界（`top_k` 20/ge1/le100、`max_depth` 50/ge1/le200、`/qa` top_k 10/ge1/le50、successors/predecessors `[:5]`、`mode="semantic"` 降级条件、`service://`/`domain://` 前缀补齐、404 文案 `f"实体不存在: {entity_id}"` / `f"服务不存在: {sid}"` / `f"业务域不存在: {did}"`、503 CALLS 配置缺失长文案、`/health` 排除 `_NON_CRITICAL_DEPS`、Authorization 解析静默吞异常）—— 全部逐字一致。
- permission_deps.py：`ROLE_RANK={reporter:1,maintainer:2,owner:3}`、`require_project_role` 404→403→403 顺序与文案、`require_group_role` 的 `is_admin→"owner"` 短路在 group 存在性检查**之后**、`resolve_role` 的 is_admin override、深度上限 3、`_expand_user_groups` BFS range(3) —— 一致。
- 所有 member 路由的 last-owner 检查条件（`role=="owner" AND body.role!="owner"`）、422 文案（`"项目必须至少 1 个直接 owner"` / `"组必须至少 1 个 owner"`）、审计 action 字符串、metadata 字段 —— 一致。
- group_router 5 个 403/404/409/422 文案、`_group_depth` 的两条 500 文案、`parent_depth+1 > MAX_GROUP_DEPTH` 判定 —— 一致。
- user_router：`UserCreate` flush→audit→commit 时序、`is_admin=True` 双审计、PATCH 细粒度审计 action（set_admin/activate/deactivate/update）、删用户两条 422 插值文案、`is_active` 新值 True/False 分流 —— 一致。
- audit_router：`_validate_page_limit` clamp（不抛 422，但 Query 层 `le=200` 仍会先 422）、LEFT JOIN、ORDER BY created_at DESC、4 段 OR scope、metadata_json 解析失败返 `{}` —— 一致。
- db_models_groups：audit_logs 列、索引名、CheckConstraint 名 `ck_group_members_role`、各 FK ondelete 策略 —— 一致。

> ⚠️ 一处需 TS 移植方注意的"双层校验"：audit_router 的 `page/limit` 既有 `Query(ge=1, le=200)`（FastAPI 层，越界先返 **422**），又有 `_validate_page_limit` 的 clamp（函数体内兜底）。原 spec 只强调 clamp，未提 422。实际上 `limit>200` 或 `page<1` 会**先**被 Query 校验拦成 422，clamp 仅对"绕过 Query 的内部调用"生效。**已在对应路由的「错误」节补 422。**

---

# Crosscut 行为规范提取（修正版）

## 路由总览

| Router 文件 | prefix | require_infra_healthy |
|---|---|---|
| auth_router | /auth | 否 |
| project_router | /projects | 是 |
| project_member_router | /projects | 是 |
| group_router | /groups | 是 |
| credentials_router | /credentials | 是 |
| admin_router | /admin | 是 |
| user_router | /admin/users | 是 |
| audit_router | （无 prefix，路由自带完整路径） | 是 |
| archived_router | /user | 是 |
| qa_router | /projects/{project_id}/qa | 是 |
| code_router | /projects/{project_id} | 否（明确标注不挂） |
| api.py 直挂路由 | （无 prefix） | 否（直挂 app，仅 get_current_user） |

---

## api.py 直挂路由（挂载在 `app = FastAPI(...)` 上，无 prefix，无 require_infra_healthy）

### GET /health

- **守卫**: 无强制守卫。尝试解析 Bearer token（匿名也可访问）。
- **请求**: 无 path/query/body 参数。Authorization header 可选（仅影响响应字段）。
- **成功**: 200
  ```json
  {"healthy": bool, "ts": "ISO8601+00:00"}
  ```
  若请求方是 Instance Admin（token 有效 + DB 中 user 存在且 `is_admin=True`）：
  ```json
  {"healthy": bool, "ts": "...", "deps": {<key>: {"ok": bool, ...}}}
  ```
- **错误**: 无固定错误（本端点故意不挂 require_infra_healthy，不自我熔断）。
- **DB**: 不查业务 DB（deps ping 走各自客户端）。解析 Bearer token 时会开一个 session 查 users 表（`s.get(User, user_id)`）。
- **审计**: 不写。
- **怪癖**:
  - `healthy = all(v.get("ok") for k,v in status.items() if k not in _NON_CRITICAL_DEPS)`，`_NON_CRITICAL_DEPS = {"neo4j"}`——neo4j down 不算 unhealthy。
  - 每次调用都重新 `await check_all_deps(...)`，不读 cache，同时写回 `request.app.state.infra_status`。
  - token 解析 `decode_token` 返回 None 或 DB 异常全部 `except Exception: pass`，按匿名处理（不暴露 deps）。
  - `user_id = int(payload.get("sub", "0"))`，且仅当 `user_id > 0` 才查库。

---

### GET /search

- **守卫**: `get_current_user`（注入名为 `_user`；JWT 无效 → 401 `detail="Not authenticated"` headers `{"WWW-Authenticate": "Bearer"}`）
- **请求**: query 参数
  - `q: str`（必填，`Query(...)`）
  - `entity_type: Optional[str]`（默认 None）
  - `mode: str`（默认 `"name"`；可选 `"semantic"`）
  - `top_k: int`（默认 20，ge=1，le=100）
- **成功**: 200
  ```json
  {"query": str, "mode": str, "count": int, "results": [...]}
  ```
  `mode` 字段回显实际生效的模式（semantic 命中时为 `"semantic"`，否则 `"name"`）。
- **错误**: 503（detail=`str(RuntimeError)`，图未构建时，由 `_graph_http` 抛出）
- **DB**: 不查业务 DB，查 KnowledgeGraph 内存图。
- **审计**: 不写。
- **怪癖**: 走 semantic 分支的条件是 `mode=="semantic" and getattr(g, "_vector_store", None) and g._vector_store.size() > 0`，否则降级为 name 模式（name 模式结果 `hits[:top_k]` 截断）。

---

### GET /impact

- **守卫**: `get_current_user`
- **请求**: query
  - `entity_id: str`（必填）
  - `direction: str`（默认 `"down"`）
  - `max_depth: int`（默认 50，ge=1，le=200）
- **成功**: 200
  ```json
  {"entity_id": str, "direction": str, "count": int, "nodes": [...]}
  ```
  `count` = `len(closure)`（闭包大小，可能 ≥ nodes 数，因为 nodes 过滤掉了 `get_node` 返回 falsy 的项）。
- **错误**:
  - 503 图未构建
  - 404 `detail=f"实体不存在: {entity_id}"`（变量插值）
- **DB**: 不查业务 DB。
- **审计**: 不写。

---

### GET /subgraph/service/{service_id}

- **守卫**: `get_current_user`
- **请求**: path `service_id: str`
- **成功**: 200（直接返回 `g.subgraph_for_service(service_id)`，结构依 KnowledgeGraph 实现）
- **错误**: 503 图未构建
- **DB**: 不查业务 DB。
- **审计**: 不写。

---

### GET /stats

- **守卫**: `get_current_user`
- **请求**: 无
- **成功**: 200
  ```json
  {"nodes": int, "edges": int}
  ```
  可选字段（条件含）：`"vector_store_size": int`（当 `g._vector_store` truthy）、`"version": any`（当 `g.version` truthy）
- **错误**: 503 图未构建
- **DB**: 不查业务 DB。
- **审计**: 不写。

---

### GET /calls/callees

- **守卫**: `get_current_user`
- **请求**: query
  - `class_name: str`（必填）
  - `method_name: str`（必填）
- **成功**: 200
  ```json
  {"class_name": str, "method_name": str, "count": int, "callees": [...]}
  ```
  响应里 `class_name`/`method_name` 回显**原始未 strip** 的查询值（注意：strip 只用于 backend 查询入参，不影响回显）。
- **错误**:
  - 503 `detail="未找到配置（请先运行流水线或保证 config/project.yaml 存在），CALLS 查询需 Neo4j 配置"`（无配置时，`_get_neo4j_calls_backend` 抛出）
- **DB**: 不查业务 DB（走 Neo4j 图）。`class_name.strip()` / `method_name.strip()` 传入 `query_direct_callees`。`finally: backend.close()`。
- **审计**: 不写。

---

### GET /calls/callers

- **守卫**: `get_current_user`
- **请求**: query
  - `class_name: str`（必填）
  - `method_name: str`（必填）
- **成功**: 200
  ```json
  {"class_name": str, "method_name": str, "count": int, "callers": [...]}
  ```
- **错误**: 503（同 callees，配置缺失，逐字同文案）
- **DB**: 同 callees（走 `query_direct_callers`，入参 strip，`finally: backend.close()`）。
- **审计**: 不写。

---

### POST /knowledge/load_snapshot

- **守卫**: `get_current_user`
- **请求**: query `snapshot_dir: str`（必填）
- **成功**: 200
  ```json
  {"message": "快照已加载", "nodes": int, "edges": int, "version": any}
  ```
- **错误**:
  - 503 图未构建（`_graph_http` 在加载前先取 graph）
  - 400 `detail="快照目录无效或缺少 graph.json"`（`not path.is_dir() or not (path/"graph.json").exists()`）
- **DB**: 不查业务 DB。
- **审计**: 不写。
- **怪癖**: 守卫顺序——503（图未构建）在 400 之前，因为 `g = _graph_http(ctx)` 先于路径校验。

---

### GET /qa（旧端点，非 QA Router）

- **守卫**: `get_current_user`
- **请求**: query
  - `q: str`（必填）
  - `top_k: int`（默认 10，ge=1，le=50）
- **成功**: 200
  ```json
  {
    "question": str,
    "answer_type": "retrieval",
    "count": int,
    "results": [{"entity": {...}, "successors": [...], "predecessors": [...]}],
    "message": "基于图谱检索；可对接大模型生成自然语言回答"
  }
  ```
  `count` = `len(related)`（有效 entity 数，跳过了无 `id` 的 hit）。每个 entity 的 successors/predecessors 各最多 5 条（`succ[:5]`/`pred[:5]`，再过滤 `get_node` falsy）。
- **错误**: 503 图未构建
- **DB**: 不查业务 DB。
- **审计**: 不写。

---

### GET /doc/service/{service_id}

- **守卫**: `get_current_user`
- **请求**: path `service_id: str`
- **成功**: 200，dict：
  ```json
  {"service_id": str, "name": str, "entity_type": "Service", "summary": str,
   "business_domains": [...], "subgraph_nodes_count": int, "subgraph_edges_count": int}
  ```
  `summary` = `f"服务 {name}：共 {nodes} 个节点，{edges} 条边。"`
- **错误**:
  - 503 图未构建（`_graph_http` 先于 has_node 检查）
  - 404 `detail=f"服务不存在: {sid}"`（`sid` = 补齐 `service://` 前缀后的值）
- **DB**: 不查业务 DB。
- **审计**: 不写。
- **怪癖**: `sid = service_id if service_id.startswith("service://") else f"service://{service_id}"`，404/summary 用 `sid`，但 `subgraph_for_service(service_id)` 用**原始** service_id。

---

### GET /doc/domain/{domain_id}

- **守卫**: `get_current_user`
- **请求**: path `domain_id: str`
- **成功**: 200，dict：
  ```json
  {"domain_id": str, "name": str, "entity_type": "BusinessDomain", "summary": str,
   "capability_ids": [...], "code_entities_count": int, "services_bearing": [...]}
  ```
  `summary` = `f"业务域 {name}：{N} 个能力，{M} 个代码实体归属。"`
- **错误**:
  - 503 图未构建
  - 404 `detail=f"业务域不存在: {did}"`（补齐 `domain://` 前缀）
- **DB**: 不查业务 DB。
- **审计**: 不写。

---

### GET /doc/generate

- **守卫**: `get_current_user`
- **请求**: query `scope: str`（默认 `"all"`；含义 `"all" | "service" | "domain"`）
- **成功**: 200
  ```json
  {"scope": str, "count": int, "documents": [...]}
  ```
- **错误**: 503 图未构建
- **DB**: 不查业务 DB。
- **审计**: 不写。
- **怪癖**: 单个文档生成失败静默 `except Exception: pass`（service 段和 domain 段各一个 try/except）。`scope` 不在 `("all","service","domain")` 时返回空 documents（不报错）。

---

## /projects 路由族（prefix: `/projects`，router 级 require_infra_healthy）

### GET /projects

- **守卫**: `require_infra_healthy` → `get_current_user`
- **请求**: 无（身份从 JWT 取）
- **成功**: 200（`ProjectListResponse`）
  ```json
  {
    "projects": [
      {
        "id": str,
        "name": str,
        "status": "ready"|"indexing"|"partial"|"failed"|"configured",
        "stats": {"methods_count": int, "classes_count": int, "interpretation_progress": int},
        "pipeline_at": str|null,
        "indexing_progress": {"phase": str, "percent": int, "eta_seconds": int}|null
      }
    ]
  }
  ```
- **错误**: 503 INFRA_UNHEALTHY / INFRA_UNINITIALIZED（router 级守卫）
- **DB**: 表 `projects`；admin → `select(Project).order_by(created_at DESC)`；普通用户 → `select(Project).join(UserProjectAccess, Project.id==UserProjectAccess.project_id).where(user_id==me).order_by(created_at DESC)`。
- **审计**: 不写。
- **怪癖**:
  - `stats` 字段从 `indexing_progress` JSON 平铺取（`methods_count`/`classes_count`/`interpretation_progress`，缺省 0）。
  - `indexing_progress`（响应字段）仅当 `status=="indexing"` **且** raw JSON 含 `"phase"` key 时有值（`percent`/`eta_seconds` 缺省 0），否则 null。
  - `pipeline_at`：naive datetime → `replace(tzinfo=utc)` → `isoformat()` → `.replace("+00:00","Z")`；NULL 时为 null。

---

### GET /projects/{project_id}

- **守卫**: `require_infra_healthy` → `get_current_user`（⚠️ 注入名 `_user`，**未挂 `require_project_role`**——任意已登录用户只要知道 ID 都能查到详情）
- **请求**: path `project_id: str`
- **成功**: 200（同上单个 Project 对象）
- **错误**:
  - 404 `detail="工程不存在"`
- **DB**: `db.get(Project, project_id)` 按主键查。
- **审计**: 不写。

---

### POST /projects

- **守卫**: `require_infra_healthy` → `get_current_user`，**函数体第一行** `if not user.is_admin → 403`
- **请求**: body（`ProjectCreateRequest`）
  - `id: str`（必填；pattern=`^[a-z][a-z0-9-]{0,62}[a-z0-9]$`）
  - `name: str`（必填，min=1，max=128）
  - `repo_url: Optional[str]`（默认 None，max=512）
  - `language: str`（默认 `"java"`）
  > ⚠️ 上述 Field 约束需以 `project_models.ProjectCreateRequest` 为准（本核验未读该文件；建议 TS 移植前回查 `src/service/project_models.py` 逐字确认 pattern/min/max）。
- **成功**: 201（Project 对象，`status` 固定 `"indexing"`）
- **错误**:
  - 403 `detail="仅管理员可创建工程"`
  - 409 `detail=f"工程 ID 已存在: {body.id}"`（变量插值）
- **DB**: `db.get(Project, body.id)` 查重 → INSERT → commit → refresh。`created_by=user.username`，`status="indexing"`，`repo_url`/`language` 来自 body。
- **审计**: **不写**（`create_project` 无 `log_audit`）。

---

## /projects/{project_id}/members 路由族（prefix: `/projects`，router 级 require_infra_healthy）

### GET /projects/{project_id}/members

- **守卫**: `require_infra_healthy` → `require_project_role("reporter")`（内含 `get_current_user` + `get_db` + 工程存在性 404 + role 检查）
- **请求**: path `project_id: str`
- **成功**: 200（`ProjectMembersResponse`）
  ```json
  {
    "direct": [{"user_id": int, "project_id": str, "role": str}],
    "inherited": [{"user_id": int, "username": str, "role": str, "inherited_from_group_id": str}]
  }
  ```
- **错误**（均由 `require_project_role` dependency 抛）:
  - 404 `detail="工程不存在"`
  - 403 `detail="无权访问此工程"`
  - 403 `detail=f"需要 reporter 及以上权限（当前 {role}）"`
- **DB**:
  - direct：`select(UserProjectAccess).filter_by(project_id=?)`
  - inherited：`_list_inherited_members` —— 从 `project.group_id` 起向上遍历 parent_group_id（`depth<3`，visited 防循环），收集 ancestor_gids，再 `select(GroupMember, User).join(User, User.id==GroupMember.user_id).filter(group_id.in_(ancestor_gids))`
- **审计**: 不写。
- **怪癖**: 同一用户既 direct 又 inherited，只出现在 direct（后端 `direct_uids` set 去重）。继承成员同一用户在多 group 时取 `ROLE_RANK` 最高的那条（`inherited_from_group_id` 即贡献该最高 role 的 group）。

---

### POST /projects/{project_id}/members

- **守卫**: `require_infra_healthy` → `require_project_role("owner")`（dependency 列表）→ 函数注入 `get_current_user`（audit actor）
- **请求**: path `project_id: str`；body（`MemberAddRequest`）
  - `user_id: int`（必填）
  - `role: str`（必填；`Field(..., pattern=r"^(reporter|maintainer|owner)$")`）
- **成功**: 201（`DirectMemberResponse`）
  ```json
  {"user_id": int, "project_id": str, "role": str}
  ```
- **错误**:
  - 404 `detail="用户不存在"`（`db.get(User, body.user_id)` 为空）
  - 409 `detail="该用户已经是成员"`（`db.get(UserProjectAccess, (user_id, project_id))` 已存在）
  - 422（role pattern 非法，Pydantic 自动）
- **DB**: 检查顺序 = target user 存在性 → 重复成员检查 → INSERT `UserProjectAccess` → `log_audit`（同 session）→ commit → refresh。
- **审计**: 写；`action=actions.PROJECT_MEMBER_ADD`（`"project_member.add"`），`resource_type="project_member"`，`resource_id=project_id`，`metadata={"target_user_id": body.user_id, "role": body.role}`，`ip_address=request.client.host if request.client else None`。

---

### PATCH /projects/{project_id}/members/{user_id}

- **守卫**: `require_infra_healthy` → `require_project_role("owner")` → `get_current_user`
- **请求**: path `project_id: str`, `user_id: int`；body（`MemberRoleUpdateRequest`）
  - `role: str`（必填；pattern=`^(reporter|maintainer|owner)$`）
- **成功**: 200（`DirectMemberResponse`）
- **错误**:
  - 404 `detail="成员不存在"`（`db.get(UserProjectAccess,(user_id,project_id))` 为空）
  - 422 `detail="项目必须至少 1 个直接 owner"`（降级唯一 direct owner）
- **DB**: 查成员 → last-owner 检查 → 改 `access.role` → `log_audit` → commit → refresh。Last-owner 检查 = `_count_direct_owners`：`select(func.count(UserProjectAccess.user_id)).filter_by(project_id=?, role="owner")`，`<=1` 则拒。
- **审计**: 写；`action=actions.PROJECT_MEMBER_ROLE_CHANGE`（`"project_member.role_change"`），`resource_type="project_member"`，`resource_id=project_id`，`metadata={"target_user_id": user_id, "old_role": old_role, "new_role": body.role}`。
- **怪癖**: 仅当 `access.role == "owner" AND body.role != "owner"` 才做 last-owner 检查（owner→owner 平改跳过）。

---

### DELETE /projects/{project_id}/members/{user_id}

- **守卫**: `require_infra_healthy` → `require_project_role("owner")` → `get_current_user`
- **请求**: path `project_id: str`, `user_id: int`
- **成功**: 204（无响应体）
- **错误**:
  - 404 `detail="成员不存在"`
  - 422 `detail="项目必须至少 1 个直接 owner"`
- **DB**: 查成员 → last-owner 检查（`access.role=="owner"` 且 `_count_direct_owners<=1`）→ 保存 `deleted_role` → `log_audit`（**add 在 delete 之前**）→ `db.delete(access)` → commit。
- **审计**: 写；`action=actions.PROJECT_MEMBER_REMOVE`（`"project_member.remove"`），`resource_type="project_member"`，`resource_id=project_id`，`metadata={"target_user_id": user_id, "role": deleted_role}`（删前保存 role）。
- **怪癖**: Last-owner 保护只算 `user_project_access` 直接 owner，继承 owner 不算。审计 `log_audit` 在 `db.delete` 语句之前调用（删后 role 无法再查）。

---

## /groups 路由族（prefix: `/groups`，router 级 require_infra_healthy，MAX_GROUP_DEPTH=3）

### POST /groups

- **守卫**: `require_infra_healthy` → `get_current_user`（函数内手动做权限判断，**无 dependencies 守卫**）
- **请求**: body（`GroupCreateRequest`）
  - `id: str`（必填；min=2，max=64；pattern=`^[a-z][a-z0-9/\-]*[a-z0-9]$`）
  - `name: str`（必填；min=1，max=128）
  - `description: Optional[str]`（默认 None，max=512）
  - `parent_group_id: Optional[str]`（默认 None，None=建根 group）
- **成功**: 201（`GroupResponse`）
  ```json
  {"id": str, "name": str, "description": str|null, "parent_group_id": str|null, "created_at": "ISO8601"}
  ```
- **错误**（检查顺序如下）:
  1. `parent_group_id is None` 且非 admin → 403 `detail="仅 Instance Admin 能建根 group"`
  2. （建子组）父组不存在 → 404 `detail="父组不存在"`
  3. （建子组）普通用户 `resolve_group_role != "owner"` → 403 `detail="需要父组 Owner 权限"`
  4. （建子组）`parent_depth + 1 > MAX_GROUP_DEPTH` → 422 `detail=f"嵌套深度超过 {MAX_GROUP_DEPTH} 层"`
  5. `db.get(Group, body.id)` 已存在 → 409 `detail="组 ID 已存在"`
- **DB**: INSERT `groups` + INSERT `group_members`（creator 自动 owner）+ `log_audit`，同一事务 commit → refresh(group)。
- **审计**: 写；`action=actions.GROUP_CREATE`（`"group.create"`），`resource_type="group"`，`resource_id=group.id`，`metadata={"name": group.name, "parent_group_id": group.parent_group_id}`。
- **怪癖**:
  - admin 跳过「父组 owner 检查」（但仍走父组存在性 404 与深度 422 检查）。
  - 创建者插 `GroupMember(user_id=user.id, group_id=group.id, role="owner", added_by_user_id=user.id)`。
  - `_group_depth`：根=1，最深合法子=3。循环检测遇重复 parent 抛 500 `detail="检测到 group 循环依赖"`；`depth > MAX_GROUP_DEPTH*2`（即 >6）抛 500 `detail="嵌套异常：深度超过预期上限"`。

---

### GET /groups

- **守卫**: `require_infra_healthy` → `get_current_user`
- **请求**: 无参数
- **成功**: 200（`list[GroupResponse]`）
- **错误**: 503 INFRA_UNHEALTHY / UNINITIALIZED
- **DB**: admin → `select(Group)` 全部；普通用户 → `_expand_user_groups(user.id, db)`（BFS 向下，直接成员 group + 子孙，range(3)）→ 空则返回 `[]`，否则 `select(Group).filter(Group.id.in_(gids))`。
- **审计**: 不写。

---

### GET /groups/{group_id}

- **守卫**: `require_infra_healthy` → `require_group_role("reporter")`
- **请求**: path `group_id: str`
- **成功**: 200（`GroupResponse`）
- **错误**:
  - 404 `detail="组不存在"`（dependency 先抛；函数体内 `db.get` 再次 404 同文案——理论上 dependency 已拦）
  - 403 `detail="无权访问此组"` 或 `detail=f"需要 reporter 及以上权限（当前 {role}）"`
- **DB**: `db.get(Group, group_id)`。
- **审计**: 不写。

---

### PATCH /groups/{group_id}

- **守卫**: `require_infra_healthy` → `require_group_role("maintainer")` → `get_current_user`
- **请求**: path `group_id: str`；body（`GroupUpdateRequest`，全可选）
  - `name: Optional[str]`（默认 None，max=128）
  - `description: Optional[str]`（默认 None，max=512）
- **成功**: 200（`GroupResponse`）
- **错误**:
  - 404 `detail="组不存在"`（函数体 `db.get` 检查）
  - 403（权限不足，dependency 抛）
- **DB**: 只在「字段非 None 且值确实不同」时改 ORM 属性（无变动不产生 UPDATE 差异）；有 `changes` 时 `log_audit`；恒 commit → refresh。
- **审计**: 仅当 `changes` 非空才写；`action=actions.GROUP_UPDATE`（`"group.update"`），`resource_type="group"`，`resource_id=group_id`，`metadata={"changes": {field: {"old": v0, "new": v1}}}`。无改动跳过审计。

---

### DELETE /groups/{group_id}

- **守卫**: `require_infra_healthy` → `require_group_role("owner")` → `get_current_user`
- **请求**: path `group_id: str`
- **成功**: 204（无响应体）
- **错误**（检查顺序）:
  1. 404 `detail="组不存在"`
  2. 403 `detail="先删子组"`（`select(Group.id).filter_by(parent_group_id=group_id).limit(1)` 命中）
  3. 403 `detail="先迁移工程"`（`select(Project.id).filter_by(group_id=group_id).limit(1)` 命中）
- **DB**: 子组检查 → 工程检查 → `log_audit` → `db.delete(group)`（级联删 `group_members`，relationship `cascade="all, delete-orphan"`）→ commit。审计 add 在 delete 之前。
- **审计**: 写；`action=actions.GROUP_DELETE`（`"group.delete"`），`resource_type="group"`，`resource_id=group_id`，`metadata={"name": group.name}`（删前保存名称）。

---

### GET /groups/{group_id}/members

- **守卫**: `require_infra_healthy` → `require_group_role("reporter")`
- **请求**: path `group_id: str`
- **成功**: 200（`list[MemberResponse]`）
  ```json
  [{"user_id": int, "group_id": str, "role": str, "added_at": "ISO8601"}]
  ```
- **错误**: 404 `detail="组不存在"` / 403（dependency 抛）
- **DB**: `select(GroupMember).filter_by(group_id=?)`
- **审计**: 不写。

---

### POST /groups/{group_id}/members

- **守卫**: `require_infra_healthy` → `require_group_role("owner")` → `get_current_user`
- **请求**: path `group_id: str`；body（`MemberAddRequest`）
  - `user_id: int`（必填）
  - `role: str`（必填；pattern=`^(reporter|maintainer|owner)$`）
- **成功**: 201（`MemberResponse`）
- **错误**:
  - 404 `detail="用户不存在"`（target user）
  - 409 `detail="该用户已经是成员"`（`db.get(GroupMember,(user_id,group_id))` 命中）
- **DB**: target user 检查 → 重复检查 → INSERT `GroupMember(user_id, group_id, role, added_by_user_id=user.id)` → `log_audit` → commit → refresh。
- **审计**: 写；`action=actions.GROUP_MEMBER_ADD`（`"group_member.add"`），`resource_type="group_member"`，`resource_id=group_id`，`metadata={"target_user_id": body.user_id, "role": body.role}`。

---

### PATCH /groups/{group_id}/members/{user_id}

- **守卫**: `require_infra_healthy` → `require_group_role("owner")` → `get_current_user`
- **请求**: path `group_id: str`, `user_id: int`；body
  - `role: str`（必填；pattern=`^(reporter|maintainer|owner)$`）
- **成功**: 200（`MemberResponse`）
- **错误**:
  - 404 `detail="成员不存在"`
  - 422 `detail="组必须至少 1 个 owner"`（降级唯一 owner）
- **DB**: 查成员 → last-owner 检查（`_count_group_owners`：`select(func.count(GroupMember.user_id)).filter_by(group_id=?, role="owner")`，`<=1` 拒）→ 改 role → `log_audit` → commit → refresh。
- **审计**: 写；`action=actions.GROUP_MEMBER_ROLE_CHANGE`（`"group_member.role_change"`），`resource_type="group_member"`，`resource_id=group_id`，`metadata={"target_user_id": user_id, "old_role": old_role, "new_role": body.role}`。
- **怪癖**: 仅 `member.role == "owner" AND body.role != "owner"` 时做 last-owner 检查。

---

### DELETE /groups/{group_id}/members/{user_id}

- **守卫**: `require_infra_healthy` → `require_group_role("owner")` → `get_current_user`
- **请求**: path `group_id: str`, `user_id: int`
- **成功**: 204
- **错误**:
  - 404 `detail="成员不存在"`
  - 422 `detail="组必须至少 1 个 owner"`
- **DB**: 查成员 → last-owner 检查（`member.role=="owner"` 且 `_count_group_owners<=1`）→ 保存 `deleted_role` → `log_audit` → `db.delete(member)` → commit。审计 add 在 delete 之前。
- **审计**: 写；`action=actions.GROUP_MEMBER_REMOVE`（`"group_member.remove"`），`resource_type="group_member"`，`resource_id=group_id`，`metadata={"target_user_id": user_id, "role": deleted_role}`（删前保存 role）。

---

## /credentials 路由族（prefix: `/credentials`，router 级 require_infra_healthy）

### GET /credentials

- **守卫**: `require_infra_healthy` → `get_current_user`
- **请求**: 无
- **成功**: 200（`list[CredentialResponse]`）
  ```json
  [{"id": str, "name": str, "type": str, "token_hint": str|null, "created_at": "ISO8601"}]
  ```
- **错误**: 503 INFRA_UNHEALTHY / UNINITIALIZED
- **DB**: `select(GitCredential).filter_by(owner_user_id=user.id)`
- **审计**: 不写。
- **怪癖**: 响应永不含 `encrypted_token`；`token_hint` 可能为 null。

---

### POST /credentials

- **守卫**: `require_infra_healthy` → `get_current_user`
- **请求**: body（`CredentialCreateRequest`）
  - `name: str`（必填；min=1，max=128）
  - `token: str`（必填；min=8，max=512）
  - `type: str`（默认 `"pat"`，max=32）
- **成功**: 201（`CredentialResponse`）
- **错误**: 422（Pydantic 格式校验失败）
- **DB**: INSERT `GitCredential`，`id="cred_" + uuid.uuid4().hex[:12]`，`encrypted_token=encrypt_token(body.token)`，`token_hint=token_hint(body.token)`，`owner_user_id=user.id`，`created_by=user.username`，`type=body.type`。→ `log_audit` → commit。
- **审计**: 写；`action=actions.CREDENTIAL_CREATE`（`"credential.create"`），`resource_type="credential"`，`resource_id=cred.id`，`metadata={"name": cred.name, "type": cred.type}`。

---

### DELETE /credentials/{cred_id}

- **守卫**: `require_infra_healthy` → `get_current_user`
- **请求**: path `cred_id: str`
- **成功**: 204
- **错误**（检查顺序）:
  1. 404 `detail="凭证不存在"`（`db.get(GitCredential, cred_id)` 为空）
  2. 403 `detail="不是该凭证的所有者"`（`cred.owner_user_id != user.id`）
- **DB**: `db.delete(cred)` **先于** `log_audit`，commit 在最后，二者同一事务。
- **审计**: 写；`action=actions.CREDENTIAL_DELETE`（`"credential.delete"`），`resource_type="credential"`，`resource_id=cred.id`，`metadata={"name": cred.name}`。

---

## /admin 路由族（prefix: `/admin`，router 级 require_infra_healthy）

> admin_router 内同时定义 `require_admin` 守卫与旧仓库管理路由。

### require_admin 守卫语义（admin_router.py）

```
get_current_user → 若 user.is_admin=False → 403 detail="仅管理员可访问"（中文）
```

⚠️ 与 `get_current_admin`（auth_dependencies.py）区别：后者 403 `detail="Admin only"`（英文）。admin_router 路由用 `require_admin`；user_router、audit_router 用 `get_current_admin` / `require_admin`（见各节）。

---

### GET /admin/credentials

- **守卫**: `require_infra_healthy` → `require_admin`（注入名 `_user`）
- **请求**: 无
- **成功**: 200（`CredentialListResponse`：`{"credentials": [CredentialResponse...]}`，含所有用户凭证；该 `CredentialResponse` 来自 `admin_models`，含 `id/name/type/token_hint/created_by/created_at/last_used_at`）
- **DB**: `select(GitCredential).order_by(GitCredential.created_at.desc())`（不过滤 owner）
- **审计**: 不写。

---

### DELETE /admin/credentials/{credential_id}

- **守卫**: `require_infra_healthy` → `require_admin`（注入名 `user`，用于审计 actor）
- **请求**: path `credential_id: str`（⚠️ 注意参数名是 `credential_id`，非 `id`）
- **成功**: 204
- **错误**: 404 `detail="凭证不存在"`
- **DB**: `db.get` → `db.delete(cred)` → `log_audit` → commit（delete 在 audit 之前）。可删任意用户凭证（不受 owner 限制）。
- **审计**: 写；`action=actions.CREDENTIAL_DELETE`（`"credential.delete"`），`resource_type="credential"`，`resource_id=cred.id`，`metadata={"name": cred.name, "admin_action": True, "original_owner_id": cred.owner_user_id}`。

---

### POST /admin/projects/test-connection

- **守卫**: `require_infra_healthy` → `require_admin`（注入名 `_user`）
- **请求**: body（`TestConnectionRequest`，含 `git_url`、可选 `credential_id`）
- **成功**: 200（`TestConnectionResponse`：`{ok, default_branch, last_commit, error}`）
- **错误**:
  - 404 `detail="凭证不存在"`（`credential_id` 指定但查不到）
  - 200 `TestConnectionResponse(ok=False, error="凭证解密失败（密钥可能轮换或密文损坏）")`（解密 ValueError，注意是 200 不是错误码）
  - 其余视 `git_utils.ls_remote` 结果（封装进 200 响应的 `ok`/`error`）
- **DB**: 不写 DB（含不更新 `last_used_at`）。
- **审计**: 不写。

---

### POST /admin/projects

- **守卫**: `require_infra_healthy` → `require_admin`（注入名 `user`）
- **请求**: body（`AdminProjectCreateRequest`，含 `id/name/language/git_url/git_branch/credential_id/domain`）
- **成功**: 201（`AdminProjectResponse`，`status` 固定 `"configured"`）
- **错误**:
  - 409 `detail=f"工程 ID 已存在: {body.id}"`
  - 404 `detail="凭证不存在"`（`credential_id` 指定但查不到）
- **DB**: INSERT `projects`（`status="configured"`，`domain` 暂存进 `indexing_progress={"domain": ...}` JSON 或 None，`created_by=user.username`）→ commit → refresh。
- **审计**: **不写**（`create_admin_project` 无 `log_audit`）。

---

### GET /admin/projects

- **守卫**: `require_infra_healthy` → `require_admin`（注入名 `_user`）
- **请求**: 无
- **成功**: 200（`AdminProjectListResponse`）
- **DB**: `select(Project).order_by(created_at.desc())`。
- **审计**: 不写。

---

### PATCH /admin/projects/{project_id}

- **守卫**: `require_infra_healthy` → `require_admin`（注入名 `_user`）
- **请求**: path `project_id: str`；body（`AdminProjectUpdateRequest`，字段全可选）
- **成功**: 200（`AdminProjectResponse`）
- **错误**:
  - 404 `detail="工程不存在"`（目标 project 不存在）
  - 404 `detail="凭证不存在"`（`credential_id` 非空且查不到）
- **DB**: 仅更新非 None 字段（`name/git_url/git_branch/credential_id/domain`；`credential_id` 空串=解除关联置 None；`domain` 合并进 `indexing_progress` JSON）→ commit → refresh。
- **审计**: **不写**（`update_admin_project` 无 `log_audit`）。

---

### DELETE /admin/projects/{project_id}

- **守卫**: `require_infra_healthy` → `require_admin`（注入名 `_user`）
- **请求**: path `project_id: str`
- **成功**: 204
- **错误**: 404 `detail="工程不存在"`
- **DB**: `db.get` → `db.delete(p)`（级联删 `qa_sessions` 等，FK CASCADE；`git_credentials` 不级联，FK SET NULL）→ commit。
- **审计**: **不写**（`delete_admin_project` 无 `log_audit`）。

---

## /admin/users 路由族（prefix: `/admin/users`，router 级 require_infra_healthy）

### GET /admin/users

- **守卫**: `require_infra_healthy` → `get_current_admin`（`detail="Admin only"`，注入名 `_admin`）
- **请求**: query（全可选）
  - `username: Optional[str]`（默认 None；`User.username.contains(x)` → LIKE `%x%`）
  - `is_admin: Optional[bool]`（默认 None；精确 `==`）
  - `is_active: Optional[bool]`（默认 None；精确 `==`）
- **成功**: 200（`list[UserResponse]`）
  ```json
  [{"id": int, "email": str, "username": str, "is_active": bool, "is_admin": bool, "created_at": "ISO8601"}]
  ```
- **DB**: `select(User)` + 条件 `.where(...)` + `.order_by(User.id)`（id 升序）。
- **审计**: 不写。

---

### POST /admin/users

- **守卫**: `require_infra_healthy` → `get_current_admin`（注入名 `admin`）
- **请求**: body（`UserCreateRequest`）
  - `email: EmailStr`（必填；邮箱格式校验）
  - `username: str`（必填；min=1，max=100）
  - `password: str`（必填；min=8；写库前 `hash_password`）
  - `is_admin: bool`（默认 False）
- **成功**: 201（`UserResponse`）
- **错误**（检查顺序）:
  - 409 `detail="邮箱已被注册"`（email 查重）
  - 409 `detail="用户名已被占用"`（username 查重）
- **DB**: email 查重 → username 查重 → INSERT `User(... is_active=True, is_admin=body.is_admin, hashed_password=hash_password(...))` → `await db.flush()`（分配自增 id）→ 写审计 → commit → refresh。
- **审计**:
  - 始终写 `action=actions.USER_CREATE`（`"user.create"`），`resource_type="user"`，`resource_id=str(user.id)`，`metadata={"email": user.email, "username": user.username, "is_admin": user.is_admin}`。
  - 若 `user.is_admin` 为 True，额外写第二条 `action=actions.USER_SET_ADMIN`（`"user.set_admin"`），`metadata={"is_admin": True, "reason": "created_as_admin"}`。
- **怪癖**: 先 `flush` 分配 id 后再写审计（不是先 commit），最后统一 commit。

---

### PATCH /admin/users/{uid}

- **守卫**: `require_infra_healthy` → `get_current_admin`（注入名 `admin`）
- **请求**: path `uid: int`；body（`UserUpdateRequest`，全可选）
  - `is_admin: Optional[bool]`（默认 None）
  - `is_active: Optional[bool]`（默认 None）
  - `password: Optional[str]`（默认 None，min=8）
- **成功**: 200（`UserResponse`）
- **错误**:
  - 404 `detail="用户不存在"`
  - 422 `detail="不能降级最后一个 Instance Admin"`（`is_admin: True→False` 且 `_count_admins() <= 1`）
- **DB**: `db.get(User, uid)` → 各字段按实际变化分别处理（改 ORM 属性 + 各自 `log_audit`）→ 统一 commit → refresh。`_count_admins`：`select(func.count(User.id)).filter_by(is_admin=True)`。
- **审计**（细粒度，每类变动单独一条）:
  - `is_admin` 变化 → `action=actions.USER_SET_ADMIN`（`"user.set_admin"`），`metadata={"old_is_admin": bool, "new_is_admin": bool}`
  - `is_active` 变化且新值 True → `action=actions.USER_ACTIVATE`（`"user.activate"`），`metadata={"old_is_active": bool, "new_is_active": bool}`
  - `is_active` 变化且新值 False → `action=actions.USER_DEACTIVATE`（`"user.deactivate"`），metadata 同上
  - `password` 非 None → `action=actions.USER_UPDATE`（`"user.update"`），`metadata={"field": "password", "changed": True}`（不记录 hash）
- **怪癖**: `is_admin`/`is_active` 只在 `body.x is not None AND body.x != user.x` 时处理（相同值传入不写审计不改）。`password` 只要非 None 就处理（不比较新旧）。last-owner（admin）检查仅在 `is_admin` 降级（`not body.is_admin`）时执行。

---

### DELETE /admin/users/{uid}

- **守卫**: `require_infra_healthy` → `get_current_admin`（注入名 `admin`）
- **请求**: path `uid: int`
- **成功**: 204
- **错误**（检查顺序）:
  - 404 `detail="用户不存在"`
  - 422 `detail=f"用户是 {group_owner_count} 个组的 owner，先转让"`（`group_members WHERE user_id=? AND role='owner'` 计数 >0）
  - 422 `detail=f"用户是 {project_owner_count} 个工程的 owner，先转让"`（`user_project_access WHERE user_id=? AND role='owner'` 计数 >0）
- **DB**: 存在性 → group owner 计数 → project owner 计数 → `log_audit` → `db.delete(user)`（级联删 `group_members`、`user_project_access`，FK CASCADE；`git_credentials.owner_user_id` SET NULL）→ commit。审计 add 在 delete 之前。
- **审计**: 写；`action=actions.USER_DELETE`（`"user.delete"`），`resource_type="user"`，`resource_id=str(uid)`，`metadata={"email": user.email, "username": user.username}`（删前保存）。
- **怪癖**: 有凭证不阻止删除（FK SET NULL 自动处理）。group owner 检查在 project owner 检查之前。

---

## audit_router（无 prefix，router 级 require_infra_healthy）

### GET /admin/audit-logs

- **守卫**: `require_infra_healthy` → `require_admin`（来自 admin_router；注入名 `_admin`）
- **请求**: query（全可选，除分页）
  - `actor: Optional[str]`（默认 None；用户名模糊，`User.username.ilike("%actor%")`，**经 LEFT JOIN users 实现**）
  - `actor_user_id: Optional[int]`（默认 None；精确 `AuditLog.actor_user_id ==`）
  - `resource_type: Optional[str]`（默认 None；精确）
  - `resource_id: Optional[str]`（默认 None；精确）
  - `action_prefix: Optional[str]`（默认 None；`AuditLog.action.startswith(x)` → LIKE `x%`）
  - `from_time: Optional[datetime]`（默认 None；`created_at >= from_time`，含）
  - `to_time: Optional[datetime]`（默认 None；`created_at <= to_time`，含）
  - `page: int`（默认 1，`Query(ge=1)`）
  - `limit: int`（默认 50，`Query(ge=1, le=200)`）
- **成功**: 200（`AuditLogResponse`）
  ```json
  {
    "entries": [
      {"id": int, "actor_user_id": int|null, "actor_username": str|null,
       "action": str, "resource_type": str, "resource_id": str,
       "metadata": dict, "ip_address": str|null, "created_at": "ISO8601"}
    ],
    "total": int, "page": int, "limit": int
  }
  ```
- **错误**: 422（`page<1` 或 `limit<1` 或 `limit>200`，FastAPI Query 层先拦）；403 非 admin；503 infra。
- **DB**: `audit_logs LEFT OUTER JOIN users ON actor_user_id==users.id`，所有过滤 `and_(*conditions)`，`ORDER BY created_at DESC`，`OFFSET (page-1)*limit LIMIT limit`；独立 COUNT 子查询（`select(func.count()).select_from(join_clause.subquery())`）得 total（`or 0`）。
- **审计**: 不写（只读）。
- **怪癖**:
  - `_validate_page_limit` clamp（`page=max(1,page)`，`limit=min(max(1,limit),200)`）是函数体内兜底；但**实际请求**会先被 Query `ge=1/le=200` 拦成 422，clamp 仅对绕过 Query 的内部调用生效。
  - `metadata_json` JSON 反序列化为 dict；`None`/空串/解析失败 → `{}`。
  - `created_at` 为 falsy 时 `created_at=""`（空串）。
  - `actor` 模糊过滤通过 JOIN users 的 `ilike` 实现，非 audit_logs 本身字段。

---

### GET /groups/{group_id}/audit-logs

- **守卫**: `require_infra_healthy` → `require_group_role("owner")`（注入名 `_role`；含 `is_admin → "owner"` 短路、group 存在性 404、role 检查）
- **请求**: path `group_id: str`；query
  - `page: int`（默认 1，`Query(ge=1)`）
  - `limit: int`（默认 50，`Query(ge=1, le=200)`）
- **成功**: 200（同 `AuditLogResponse` 格式）
- **错误**: 404 `detail="组不存在"` / 403（dependency）；422 分页越界（Query 层）。
- **DB**: `_expand_group_and_descendants(group_id)`（BFS 向下含自身，range(3)）→ 查 `Project.id WHERE group_id IN (group_ids)` 得 project_ids。`or_` 范围（条件顺序）:
  1. `resource_type='group' AND resource_id IN (gid+子孙)`
  2. `resource_type='group_member' AND resource_id IN (gid+子孙)`
  3. `resource_type='project' AND resource_id IN (project_ids)`（仅 `project_ids` 非空时加）
  4. `resource_type='project_member' AND resource_id IN (project_ids)`（同上）

  `LEFT JOIN users`，`WHERE or_(*scope)`，`ORDER BY created_at DESC`，OFFSET/LIMIT，独立 COUNT 子查询。
- **审计**: 不写。
- **怪癖**: 同 admin 端，metadata_json 解析失败返 `{}`，`created_at` falsy 返空串；分页 422 同 admin 端。

---

## db_models_homepage 五张表（核对 db_models_homepage.py + db_models_groups.py）

> 注：`db_models_homepage.py` 顶部导入了 `BigInteger`、`Float`，但全文件未实际用于任何列；只有 `Integer` 用于 `owner_user_id`/`user_id`/`message_count`。TS 移植无需为这两类型建列。

### 1. `projects` 表

| Python 字段 | SQLAlchemy 类型 | 可空 | 默认值 | 说明 |
|---|---|---|---|---|
| `id` | String(64) | NOT NULL | — | PK，业务可读字符串 |
| `name` | String(128) | NOT NULL | — | 显示名 |
| `repo_url` | String(512) | NULL | — | 已废弃，保留兼容 |
| `language` | String(32) | NOT NULL | `"java"` | 主语言 |
| `status` | String(32) | NOT NULL | `"indexing"` | configured/indexing/ready/partial/failed |
| `pipeline_at` | DateTime | NULL | — | 上次 pipeline 完成时间 |
| `indexing_progress` | JSON | NULL | — | 进度 + stats 平铺 JSON |
| `created_at` | DateTime | NOT NULL | `func.now()`（server_default） | |
| `created_by` | String(64) | NULL | — | 创建人 username（非 FK） |
| `git_url` | String(512) | NULL | — | Git 仓库 URL |
| `git_branch` | String(128) | NOT NULL | `"main"` | 跟踪分支 |
| `git_credential_id` | String(64) | NULL | — | FK → git_credentials.id ON DELETE SET NULL |
| `last_synced_at` | DateTime | NULL | — | |
| `last_synced_commit` | String(40) | NULL | — | commit hash |
| `sync_schedule` | String(32) | NOT NULL | `"manual"` | manual/hourly/daily |
| `repo_local_path` | String(512) | NULL | — | 本地源码绝对路径 |
| `group_id` | String(64) | NULL | — | FK → groups.id ON DELETE SET NULL |

索引：`idx_projects_status(status)`

---

### 2. `git_credentials` 表（`GitCredential`）

| Python 字段 | SQLAlchemy 类型 | 可空 | 默认值 | 说明 |
|---|---|---|---|---|
| `id` | String(64) | NOT NULL | — | PK，如 `cred_abc123` |
| `name` | String(128) | NOT NULL | — | |
| `type` | String(32) | NOT NULL | `"pat"` | pat/ssh_key |
| `encrypted_token` | Text | NOT NULL | — | Fernet 密文 |
| `token_hint` | String(16) | NULL | — | 末 4 位掩码 |
| `created_by` | String(64) | NULL | — | 创建人 username |
| `created_at` | DateTime | NOT NULL | `func.now()`（server_default） | |
| `last_used_at` | DateTime | NULL | — | |
| `owner_user_id` | Integer | NULL | — | FK → users.id ON DELETE SET NULL |

---

### 3. `user_project_access` 表

| Python 字段 | SQLAlchemy 类型 | 可空 | 说明 |
|---|---|---|---|
| `user_id` | Integer | NOT NULL | PK(1/2)，FK → users.id CASCADE |
| `project_id` | String(64) | NOT NULL | PK(2/2)，FK → projects.id CASCADE |
| `role` | String(32) | NOT NULL（default `"reporter"`）| reporter/maintainer/owner；v1 遗留 reader/writer/admin 兼容 |

复合主键 `(user_id, project_id)`。

---

### 4. `qa_sessions` 表

| Python 字段 | SQLAlchemy 类型 | 可空 | 默认值 | 说明 |
|---|---|---|---|---|
| `id` | String(64) | NOT NULL | — | PK |
| `project_id` | String(64) | NOT NULL | — | FK → projects.id CASCADE |
| `user_id` | Integer | NOT NULL | — | 无 FK（保留已删用户历史） |
| `title` | String(255) | NULL | — | |
| `created_at` | DateTime | NOT NULL | `func.now()`（server_default） | |
| `updated_at` | DateTime | NOT NULL | `func.now()`（server_default + onupdate=`func.now()`） | |
| `message_count` | Integer | NOT NULL | 0（Python default） | 缓存值 |
| `archived_at` | DateTime | NULL | None | NULL=活动；非 NULL=已归档 |
| `title_custom` | Boolean | NOT NULL | False（server_default=`text("0")`，Python default=False）| True=用户改过，异步总结跳过 |

索引：`idx_qa_sessions_project_user(project_id, user_id, updated_at)`

---

### 5. `groups` / `group_members` / `audit_logs`（db_models_groups.py）

**groups 表**

| Python 字段 | SQLAlchemy 类型 | 可空 | 说明 |
|---|---|---|---|
| `id` | String(64) | NOT NULL | PK |
| `name` | String(128) | NOT NULL | |
| `description` | String(512) | NULL | |
| `parent_group_id` | String(64) | NULL | FK → groups.id ON DELETE RESTRICT（自引用） |
| `created_by_user_id` | Integer | NULL | FK → users.id ON DELETE SET NULL |
| `created_at` | DateTime | NOT NULL | server_default=`func.now()` |

索引：`ix_groups_parent(parent_group_id)`。relationship `members` cascade="all, delete-orphan"。

**group_members 表**

| Python 字段 | SQLAlchemy 类型 | 可空 | 说明 |
|---|---|---|---|
| `user_id` | Integer | NOT NULL | PK(1/2)，FK → users.id CASCADE |
| `group_id` | String(64) | NOT NULL | PK(2/2)，FK → groups.id CASCADE |
| `role` | String(16) | NOT NULL | CHECK `role IN ('reporter','maintainer','owner')`，约束名 `ck_group_members_role` |
| `added_by_user_id` | Integer | NULL | FK → users.id ON DELETE SET NULL |
| `added_at` | DateTime | NOT NULL | server_default=`func.now()` |

**audit_logs 表**

| Python 字段 | SQLAlchemy 类型 | 可空 | 说明 |
|---|---|---|---|
| `id` | Integer | NOT NULL | PK，autoincrement |
| `actor_user_id` | Integer | NULL | FK → users.id ON DELETE SET NULL |
| `action` | String(64) | NOT NULL | |
| `resource_type` | String(32) | NOT NULL | |
| `resource_id` | String(128) | NOT NULL | |
| `metadata_json` | Text | NULL | JSON 字符串（非 JSON 类型，跨 DB 兼容） |
| `ip_address` | String(45) | NULL | IPv4/IPv6 |
| `created_at` | DateTime | NOT NULL | server_default=`func.now()` |

索引：`ix_audit_actor_time(actor_user_id, created_at)`、`ix_audit_resource(resource_type, resource_id)`

---

## 依赖的共享设施

### `get_current_user`（auth_dependencies.py）

```
OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)
→ token 缺失（not token）→ 401 detail="Not authenticated" headers={"WWW-Authenticate": "Bearer"}
→ decode_token(token)
→ not payload or payload.get("type") != "access" → 401（同上 cred_exc）
→ payload.get("sub") 缺失 → 401
→ int(sub)；ValueError → 401
→ SELECT users WHERE id=user_id（scalar_one_or_none）→ user is None 或 not is_active → 401
→ 返回 User ORM 对象
```

### `get_current_admin`（auth_dependencies.py）

```
get_current_user → user.is_admin=False → 403 detail="Admin only"（英文）
```

### `require_admin`（admin_router.py）

```
get_current_user → user.is_admin=False → 403 detail="仅管理员可访问"（中文）
```

（⚠️ 与 `get_current_admin` detail 文案不同：英文 vs 中文）

### `require_infra_healthy`（deps_infra.py）

```
Request + get_current_user（确保已登录）
→ getattr(app.state, "infra_status", None) is None → 503 detail={"code": "INFRA_UNINITIALIZED", "message": "系统正在初始化，请稍后重试"}
→ unhealthy = [k for k,v in state.items() if k not in _NON_CRITICAL_DEPS={"neo4j"} and not v.get("ok")]
→ unhealthy 空 → return（放行）
→ 否则 503 detail={"code": "INFRA_UNHEALTHY", "message": "系统暂时不可用，请联系管理员"}
   若 user.is_admin=True → detail 额外含 "deps": app.state.infra_status
```

### `require_project_role(min_role="reporter")`（permission_deps.py）

工厂函数，启动期 `min_role not in ROLE_RANK → ValueError`（路由注册阶段爆，不等请求）。返回 checker：

```
project_id（path）+ get_current_user + get_db
→ db.get(Project, project_id) 不存在 → 404 detail="工程不存在"
→ resolve_role(user, project, db)
→ role is None → 403 detail="无权访问此工程"
→ ROLE_RANK[role] < ROLE_RANK[min_role] → 403 detail=f"需要 {min_role} 及以上权限（当前 {role}）"
→ 返回 role 字符串
```

### `require_group_role(min_role="reporter")`（permission_deps.py）

工厂函数，启动期同样 `min_role not in ROLE_RANK → ValueError`。返回 checker：

```
group_id（path）+ get_current_user + get_db
→ db.get(Group, group_id) 不存在 → 404 detail="组不存在"
→ user.is_admin → 直接返回 "owner"（在 group 存在性检查之后；跳过成员表查询）
→ resolve_group_role(user.id, group_id, db)
→ role is None → 403 detail="无权访问此组"
→ ROLE_RANK[role] < ROLE_RANK[min_role] → 403 detail=f"需要 {min_role} 及以上权限（当前 {role}）"
→ 返回 role 字符串
```

### `resolve_role(user, project, db)`（permission_deps.py）

最终 role = `_pick_higher(direct, inherited)`，其中：

```
user.is_admin → 直接返回 "owner"
direct：db.scalar(select(UserProjectAccess.role).filter_by(user_id=user.id, project_id=project.id))
group 继承链：cur_gid = project.group_id；while cur_gid and cur_gid not in visited:
    if depth >= 3: break
    visited.add(cur_gid)
    role = select(GroupMember.role).filter_by(user_id=user.id, group_id=cur_gid)
    if role: inherited = _pick_higher(inherited, role)
    cur_gid = select(Group.parent_group_id).filter_by(id=cur_gid)
    depth += 1
return _pick_higher(direct, inherited)
```

`_pick_higher(a,b)`：`a is None → b`；`b is None → a`；否则 `a if ROLE_RANK[a] >= ROLE_RANK[b] else b`。

### `resolve_group_role(user_id, group_id, db)`（permission_deps.py）

```
不含 is_admin 覆盖（由调用方处理）。
inherited=None；cur_gid=group_id；while cur_gid and cur_gid not in visited:
    if depth >= 3: break
    visited.add(cur_gid)
    role = select(GroupMember.role).filter_by(user_id=user_id, group_id=cur_gid)
    if role: inherited = _pick_higher(inherited, role)
    cur_gid = select(Group.parent_group_id).filter_by(id=cur_gid)
    depth += 1
返回 inherited（None=无权限）
```

### `log_audit(db, *, actor_user_id, action, resource_type, resource_id, metadata=None, ip_address=None)`（audit/logger.py）

- 只 `db.add(AuditLog(...))`，**不 commit**（caller 与业务事务一起 commit，原子）。
- `metadata_json = json.dumps(metadata or {}, ensure_ascii=False)`（None → `"{}"`）。
- `resource_id = str(resource_id)`（统一转字符串，兼容 int 传入）。
- 全程 `try/except`：失败仅 `logger.warning`，**不抛错**（审计失败不中断业务）。
- 全部为 keyword-only 参数（`*` 后）。

### ROLE_RANK 枚举（permission_deps.py）

```python
{"reporter": 1, "maintainer": 2, "owner": 3}
```

v1 遗留值 `reader/writer/admin` 可能存在于 `user_project_access.role`（migration `v2b_remap_role` 迁移），ROLE_RANK 不含；TS 移植仅支持三个新值（遇遗留值会 KeyError → 需移植方决定兜底）。

### `_expand_user_groups(user_id, db)`（permission_deps.py）

BFS 向下：用户直接成员 group（`select(GroupMember.group_id).filter_by(user_id=?)`）+ 子孙（`for _ in range(3)`，每层 `select(Group.id).filter(parent_group_id.in_(frontier))`，差集去重，无新增 break）。直接成员为空时返回 `set()`。用于 `list_accessible_projects` 与 `GET /groups`。

### 审计 action 常量值（audit/actions.py，逐字）

`PROJECT_MEMBER_ADD="project_member.add"`、`PROJECT_MEMBER_REMOVE="project_member.remove"`、`PROJECT_MEMBER_ROLE_CHANGE="project_member.role_change"`、`GROUP_CREATE="group.create"`、`GROUP_UPDATE="group.update"`、`GROUP_DELETE="group.delete"`、`GROUP_MEMBER_ADD="group_member.add"`、`GROUP_MEMBER_REMOVE="group_member.remove"`、`GROUP_MEMBER_ROLE_CHANGE="group_member.role_change"`、`CREDENTIAL_CREATE="credential.create"`、`CREDENTIAL_DELETE="credential.delete"`、`USER_CREATE="user.create"`、`USER_UPDATE="user.update"`、`USER_DELETE="user.delete"`、`USER_SET_ADMIN="user.set_admin"`、`USER_ACTIVATE="user.activate"`、`USER_DEACTIVATE="user.deactivate"`、`PROJECT_CREATE="project.create"`、`PROJECT_UPDATE="project.update"`、`PROJECT_DELETE="project.delete"`、`PROJECT_REINDEX_TRIGGER="project.reindex_trigger"`、`AUTH_LOGIN_SUCCESS="auth.login_success"`、`AUTH_LOGIN_FAILURE="auth.login_failure"`、`AUTH_LOGOUT="auth.logout"`、`AUTH_PASSWORD_CHANGE="auth.password_change"`、`MESSAGE_EXPORT_DOCX="message.export_docx"`。

> ⚠️ 注意：`PROJECT_CREATE`/`PROJECT_UPDATE`/`PROJECT_DELETE` 三个常量虽已定义，但 admin_router 的对应路由**实际未调用** `log_audit`（见上文修正项 1-3）——常量存在 ≠ 被使用。

---

# 【projects 族】

Confirmed: Pydantic 2.x, and `_to_pydantic` constructs `Project(status=p.status, ...)`. With a `Literal["ready","indexing","partial","failed"]`, a DB row carrying `status="configured"` would raise a `ValidationError` at response construction → FastAPI 500. This is a real edge the spec omits. I have everything needed to produce the corrected spec.

---

## 修正清单

逐条列出对原 spec 的修改/补充。**绝大多数条目核验通过**；以下是发现的问题与增补：

1. **[修正 — POST 怪癖] `created_at` 默认值写法错误**：原 spec 写 `default=sql\`(now())\``（这是 drizzle/TS 语法）。Python 源码（`db_models_homepage.py:74-76`）实际是 `created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)` —— 用的是 SQLAlchemy `server_default=func.now()`。本规范是 Python 行为权威，必须记 Python 形态。已改。

2. **[补充 — 新增怪癖｜真实坑] `status="configured"` 会导致 500**：ORM 模型 `status` 列 docstring（`db_models_homepage.py:62-66`）声明 5 种状态 `configured/indexing/ready/partial/failed`，但 Pydantic `ProjectStatus` Literal（`project_models.py:16`）只有 4 种（无 `configured`）。`_to_pydantic` 直接 `Project(status=p.status, ...)`，Pydantic 2.12.5 对 response_model 做输出校验，若 DB 行 `status="configured"`（v1.0 仓库管理新增的存量值）→ `ValidationError` → FastAPI **500**，而非 200。原 spec 三条路由都遗漏此边界。已在三条路由的「怪癖」补充。

3. **[补充 — require_project_role 默认参数]** 共享设施节原 spec 未记 `min_role` 默认值。源码 `def require_project_role(min_role: str = "reporter")`（`permission_deps.py:370`），默认 `"reporter"`。已补。

4. **[补充 — 403 detail 全角括号]** 原 spec 在共享设施节把权限不足文案写成 `"需要 {min_role} 及以上权限（当前 {role})"` —— 末尾混用了半角 `)`。源码（`permission_deps.py:446`）是**全角右括号 `）`**：`f"需要 {min_role} 及以上权限（当前 {role}）"`。逐字核对后已统一为全角。

5. **[补充 — get_current_user 401 触发条件精确化]** 原 spec 描述「sub 字段缺失或不能转为 int」基本对，但漏了 `decode_token` 返回 falsy（`not payload`）也 401，且 `sub` 用的是 `if not user_id_str`（falsy 检查，空串/0 字符串/None 都触发），不是单纯「缺失」。已精确化。

6. **[核验通过点抽查]**：
   - POST 状态码 `201`（`status.HTTP_201_CREATED`，router.py:128）✓
   - 403 文案 `"仅管理员可创建工程"` 逐字 ✓
   - 409 文案 `"工程 ID 已存在: {body.id}"`（半角冒号+空格，f-string 原样插值）✓
   - 404 文案 `"工程不存在"` ✓
   - `id` pattern `^[a-z][a-z0-9-]{0,62}[a-z0-9]$` 逐字符 ✓（OpenAPI 交叉核对一致）
   - `name` min_length=1/max_length=128、`repo_url` max_length=512、`interpretation_progress` ge=0/le=100、`percent` ge=0/le=100、`eta_seconds` ge=0 ✓（OpenAPI schema 一致）
   - 守卫顺序 `require_infra_healthy`(含 `get_current_user`) → 路由参数 `get_current_user` ✓
   - GET 详情 **无 RBAC**（`_user` 下划线、无 `require_project_role`）✓（router.py:116-125）
   - list 普通用户**只 JOIN `user_project_access`，不展开 group 继承**（未调 `list_accessible_projects`）✓
   - `_to_pydantic` 的 `indexing_progress` 仅 `status=="indexing" and "phase" in raw` 才非 null ✓（router.py:62）
   - `pipeline_at` UTC+Z 转换逻辑 ✓（router.py:69-73，含 tzinfo 判空分支）
   - 三条路由均**不写 audit_logs**、`created_by=user.username`、`db.add→commit→refresh` ✓
   - 503 三态（INFRA_UNINITIALIZED / INFRA_UNHEALTHY / +deps）文案逐字 ✓（deps_infra.py），`_NON_CRITICAL_DEPS={"neo4j"}` ✓
   - `ROLE_RANK={"reporter":1,"maintainer":2,"owner":3}`、group 继承 depth≥3 截断、`require_project_role` 工厂注册期 `ValueError` 校验 ✓
   - `log_audit` keyword-only、`metadata_json=json.dumps(metadata or {}, ensure_ascii=False)`、`resource_id=str(...)`、失败仅 warning 不抛 ✓

---

## projects 族路由行为规范

### GET /projects

- **守卫**:
  1. `require_infra_healthy` — router 级全局 dependency（`APIRouter(prefix="/projects", dependencies=[Depends(require_infra_healthy)])`，router.py:37-42）。该 dep 内部 `user: User = Depends(get_current_user)`，先完成 Bearer token 认证，再检查 `request.app.state.infra_status`；缺失或任一 critical 依赖 `ok=False` → 503。
  2. `get_current_user` — 同时作为路由参数独立注入（`user: User = Depends(get_current_user)`，router.py:90）。FastAPI 依赖缓存机制下，同一请求内 `get_current_user` 只执行一次、复用同一 `User` 实例。

  检查顺序：`require_infra_healthy`（含认证 + infra）→ 路由体（普通用户/admin 分支）。认证失败在 `require_infra_healthy` 内部即拦截（401）。

- **请求**:
  - path 参数：无
  - query 参数：无
  - body：无
  - 无分页参数（全量返回，无 limit/offset）

- **成功**: HTTP 200，`ProjectListResponse`
  ```json
  {
    "projects": [
      {
        "id": "string",
        "name": "string",
        "status": "ready|indexing|partial|failed",
        "stats": {
          "methods_count": 0,
          "classes_count": 0,
          "interpretation_progress": 0
        },
        "pipeline_at": "2024-01-01T00:00:00Z" | null,
        "indexing_progress": {
          "phase": "string",
          "percent": 0,
          "eta_seconds": 0
        } | null
      }
    ]
  }
  ```
  - `projects` 必填字段，是数组，可为空 `[]`。
  - 每个 Project 的 `required`：`id`、`name`、`stats`（`status` 有默认值 `"indexing"`，`pipeline_at`/`indexing_progress` 可 null）。OpenAPI 交叉核对一致。
  - `pipeline_at`：UTC ISO 8601 带 `Z` 后缀（如 `"2024-01-01T00:00:00Z"`），DB `pipeline_at` 为 NULL 时为 `null`。
  - `indexing_progress`：仅当 `status == "indexing"` **且** `indexing_progress` JSON 原始字段内含 `"phase"` key 时才非 `null`，其余一律 `null`。
  - `ProjectStats.required` 为空（三个子字段都有默认 0）。`IndexingProgress.required` 为 `["phase","percent"]`（`eta_seconds` 默认 0）。
  - 排序：`projects.created_at DESC`（固定，无参数可改）。

- **错误**:
  - `401 Unauthorized` — `{"detail": "Not authenticated"}` + `WWW-Authenticate: Bearer` header（token 缺失/`decode_token` 返回 falsy/`type != "access"`/`sub` falsy 或不能转 int/user 不存在/user `is_active=False`）。
  - `503 Service Unavailable` — 三种情况（detail 是 dict，非字符串）：
    - infra 未初始化（`app.state.infra_status` 为 None）：`{"code": "INFRA_UNINITIALIZED", "message": "系统正在初始化，请稍后重试"}`
    - critical 依赖 down（非 admin）：`{"code": "INFRA_UNHEALTHY", "message": "系统暂时不可用，请联系管理员"}`
    - critical 依赖 down（admin）：`{"code": "INFRA_UNHEALTHY", "message": "系统暂时不可用，请联系管理员", "deps": <infra_status dict>}`
  - `500 Internal Server Error`（隐式，新增）— 若任一返回行 DB `status="configured"`（5 态之一，但不在 4 态 Literal 内），Pydantic 输出校验 `ValidationError` → 500。详见怪癖。
  - 无 422（无 query/body 参数，不触发 Pydantic 入参验证）。

- **DB**:
  - 表：`projects`，`user_project_access`
  - admin 路径：`select(ProjectModel).order_by(ProjectModel.created_at.desc())` → `SELECT * FROM projects ORDER BY created_at DESC`
  - 普通用户路径：`select(ProjectModel).join(UserProjectAccess, ProjectModel.id == UserProjectAccess.project_id).where(UserProjectAccess.user_id == user.id).order_by(ProjectModel.created_at.desc())` → `SELECT projects.* FROM projects JOIN user_project_access ON projects.id = user_project_access.project_id WHERE user_project_access.user_id = :user_id ORDER BY projects.created_at DESC`
  - 无 limit/offset，全量返回。只读，无写、无事务/commit。

- **审计**: 不写 `audit_logs`（路由体无 `log_audit` 调用）。

- **怪癖**:
  - `indexing_progress` JSON 列同时承载两类数据：统计（`methods_count`/`classes_count`/`interpretation_progress`）+ 索引进度（`phase`/`percent`/`eta_seconds`），平铺在同一 JSON 对象（router.py:50-51 docstring）。
  - 响应 `indexing_progress` 填充条件：`p.status == "indexing" and "phase" in raw`（router.py:62）。即 status=indexing 但 JSON 无 `phase` 时，响应字段仍为 `null`。
  - `stats` 子字段缺失以 `0` 兜底（`raw.get("methods_count", 0)` 等，router.py:56-58）。
  - `pipeline_at`：先判 `if p.pipeline_at.tzinfo is None` 才 `.replace(tzinfo=timezone.utc)`（已带时区则原样），再 `.isoformat().replace("+00:00", "Z")`（router.py:72-73）。
  - 普通用户列表**仅 JOIN `user_project_access` 直接成员表，不展开 group 继承链**。`permission_deps.list_accessible_projects` 考虑了 group 继承（含 BFS 向下扩展 `_expand_user_groups`），但 `list_projects` 路由体**未调用它**，只手写 JOIN（router.py:100-110，注释标注「按 user_project_access 表过滤」，是 v2 TODO）。
  - **[新增] 存量 `status="configured"` → 500**：ORM 允许 5 态，Pydantic Literal 只认 4 态，`_to_pydantic` 透传 `p.status`，命中 `configured` 行时 Pydantic 2.12.5 输出校验抛 `ValidationError`，FastAPI 转 500（不是 200）。TS 移植需决定：放宽 enum / 映射 / 显式过滤。

---

### POST /projects

- **守卫**:
  1. `require_infra_healthy` — router 级全局 dependency（含认证），同上。
  2. `get_current_user` — 路由参数注入（router.py:132）。
  3. **路由体内 admin 检查**（非 dependency）：`if not user.is_admin: raise HTTPException(status_code=403, detail="仅管理员可创建工程")`（router.py:135-139）。

  检查顺序：`require_infra_healthy`（含认证 + infra）→ Pydantic body 校验（422）→ 函数体 `is_admin` 检查（403）→ ID 重复检查（409）。注意：FastAPI 在进入函数体**前**已完成 body 的 Pydantic 校验，所以 422 早于 403/409。

- **请求**:
  - path 参数：无
  - query 参数：无
  - body（JSON，必填，`ProjectCreateRequest`）：

  | 字段 | 类型 | 必选 | 默认 | 约束 |
  |------|------|------|------|------|
  | `id` | string | 是 | — | pattern `^[a-z][a-z0-9-]{0,62}[a-z0-9]$`（小写字母开头，2-64 字符，可含数字/连字符，不能以连字符结尾）。**无独立 min/max_length**，长度边界来自正则。 |
  | `name` | string | 是 | — | min_length=1, max_length=128 |
  | `repo_url` | string \| null | 否 | `null` | max_length=512 |
  | `language` | string | 否 | `"java"` | 无枚举约束（仅 description「主语言；目前只支持 java」） |

- **成功**: HTTP 201（`status.HTTP_201_CREATED`），`response_model=Project`
  - 响应体：与 GET /projects/{id} 相同的 `Project` 对象（经 `_to_pydantic`）。
  - 新建初始 `status="indexing"`（硬编码，router.py:153）。
  - 流程 `db.add(p)` → `await db.commit()` → `await db.refresh(p)` → `_to_pydantic(p)`（refresh 回填 DB 默认值如 `created_at`）。

- **错误**:
  - `401 Unauthorized` — `{"detail": "Not authenticated"}` + `WWW-Authenticate: Bearer`（token 问题）。
  - `403 Forbidden` — `{"detail": "仅管理员可创建工程"}`（非 admin）。
  - `409 Conflict` — `{"detail": "工程 ID 已存在: {body.id}"}`（半角冒号+空格，f-string 原样插值，如 `"工程 ID 已存在: deposit-system"`）。
  - `422 Unprocessable Entity` — Pydantic body 校验失败（id pattern 不匹配、name 空/超长、repo_url 超长、缺必填字段等）。
  - `503` — 同 GET /projects 三态。
  - `500`（隐式，新增）— 同 GET：理论上 refresh 后 status 恒为 `"indexing"`，故新建路径不会触发 configured-500；列此项仅为族内一致性提醒。

- **DB**:
  - 表：`projects`
  - 重复检查：`existing = await db.get(ProjectModel, body.id)` —— 按主键查找，存在则 409（不是 SELECT WHERE，是主键索引查找）。
  - 写入字段：`id=body.id, name=body.name, repo_url=body.repo_url, language=body.language, status="indexing", created_by=user.username`。
  - `created_at` 由 **DB server_default 填充**：`mapped_column(DateTime, server_default=func.now(), nullable=False)`（db_models_homepage.py:74-76），Python 侧不赋值。**注意：是 SQLAlchemy `server_default=func.now()`，不是 drizzle 的 `default=sql\`(now())\``**。
  - 事务：`db.add(p)` → `await db.commit()` → `await db.refresh(p)`（单条插入，提交后 refresh，无显式 `begin()` 事务块）。

- **审计**: 不写 `audit_logs`。⚠️ 路由体内**未调用** `log_audit`；`actions.PROJECT_CREATE = "project.create"` 常量存在（audit/actions.py:77）但 v1 `project_router.py` 未使用。

- **怪癖**:
  - `created_by` 存 `user.username`（字符串，人类可读），不是 `user.id`（整数）。DB 列 `created_by String(64) nullable=True`（db_models_homepage.py:78）。
  - `language` 仅 description 说明「目前只支持 java」，Pydantic 无枚举限制，传任意字符串不会 422（DB 列 `String(32)` 也不约束枚举）。
  - `id` 正则要求最短 2 字符（`^[a-z]` + `[a-z0-9-]{0,62}`（可 0 个）+ `[a-z0-9]$`），单字符 id 无法通过。
  - 409 用 `db.get`（主键查询），性能等价主键索引查找。
  - 新建 project 的 `stats` 响应全为 0（`indexing_progress` JSON 列为 NULL，`raw.get(...)` 兜底 0）；`indexing_progress` 响应字段为 `null`（status=indexing 但 DB JSON 无 `phase`）。
  - `created_at` 未在 Python 侧赋值，依赖 `db.refresh` 把 DB 生成值读回，TS 移植需保证 INSERT 后能取回 DB 生成的 `created_at`（drizzle 用 `.$default()` 或 DB `DEFAULT (now())` + returning）。

---

### GET /projects/{project_id}

- **守卫**:
  1. `require_infra_healthy` — router 级全局 dependency（含认证），同上。
  2. `get_current_user` — 路由参数注入，变量名 `_user`（下划线前缀，表示「注入但业务不用」，但仍执行认证副作用，router.py:120）。

  检查顺序：`require_infra_healthy`（含认证 + infra）→ 路由体主键查找。⚠️ **无项目级权限检查**（无 `require_project_role`）—— 任何已登录且通过 infra 检查的用户都能拿任意 project 详情，无需是项目成员（router.py:116-125）。

- **请求**:
  - path 参数：`project_id`（string，必填，无长度/格式约束）
  - query 参数：无
  - body：无

- **成功**: HTTP 200，`response_model=Project`
  ```json
  {
    "id": "string",
    "name": "string",
    "status": "ready|indexing|partial|failed",
    "stats": { "methods_count": 0, "classes_count": 0, "interpretation_progress": 0 },
    "pipeline_at": "2024-01-01T00:00:00Z" | null,
    "indexing_progress": { "phase": "string", "percent": 0, "eta_seconds": 0 } | null
  }
  ```

- **错误**:
  - `401 Unauthorized` — `{"detail": "Not authenticated"}` + `WWW-Authenticate: Bearer`。
  - `404 Not Found` — `{"detail": "工程不存在"}`（`db.get(ProjectModel, project_id)` 返回 `None`，router.py:123-124）。
  - `422 Unprocessable Entity` — FastAPI path 参数验证错误（string 类型实际极少触发）。
  - `503` — 同上三态。
  - `500`（隐式，新增）— 命中 DB `status="configured"` 行时 Pydantic 输出校验抛错 → 500。详见怪癖。

- **DB**:
  - 表：`projects`
  - 查询：`await db.get(ProjectModel, project_id)` —— SQLAlchemy `Session.get`，按主键查单条。
  - 只读，无写，无事务/commit。

- **审计**: 不写 `audit_logs`。

- **怪癖**:
  - 路由参数 `_user` 下划线前缀 —— 认证是副作用（验证令牌有效性），user 对象不参与业务。
  - **无 RBAC 过滤的不对称**：列表（GET /projects）普通用户只看自己有权限的工程，但详情（GET /projects/{id}）**无成员资格检查**，任意登录用户知道 id 即可查详情。v1 有意为之（v2 可能加权限）。
  - `indexing_progress` 的 null 逻辑、`pipeline_at` 的 UTC+Z 转换与列表路由完全相同（共用 `_to_pydantic`）。
  - **[新增] 存量 `status="configured"` → 500**：与列表同理，DB 直查的单条若 status 为 4 态外的值，Pydantic 输出校验 `ValidationError` → FastAPI 500（不是 404，也不是 200）。

---

## 依赖的共享设施

### `require_infra_healthy`（`src/service/deps_infra.py`）

- **用途**：作为 `APIRouter(dependencies=[...])` 全局 dependency，每条 project 路由执行。
- **语义**：
  1. 内部 `user: User = Depends(get_current_user)` 完成认证（认证失败在此处 401 拦截）。
  2. `state = getattr(request.app.state, "infra_status", None)`（startup 阶段写入的 dict）。
  3. `state is None` → 503 `{"code": "INFRA_UNINITIALIZED", "message": "系统正在初始化，请稍后重试"}`。
  4. 列表推导 `unhealthy = [k for k, v in state.items() if k not in _NON_CRITICAL_DEPS and not v.get("ok")]`；非空 → 503 `INFRA_UNHEALTHY`。
  5. `_NON_CRITICAL_DEPS = {"neo4j"}`（退役中，down 不致 503）。
  6. admin（`user.is_admin`）的 503 额外带 `"deps": state`（完整 infra_status dict）；普通用户只看友好文案 `"系统暂时不可用，请联系管理员"`。
  7. 全 ok → `return`（不拦截）。

### `get_current_user`（`src/service/auth_dependencies.py`）

- **用途**：从 `Authorization: Bearer <token>` 提取并验证当前 user。
- **语义**：
  1. `oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)` —— token 缺失返回 `None` 不自动报错。
  2. `if not token` → 401。
  3. `payload = decode_token(token)`；`if not payload or payload.get("type") != "access"` → 401。
  4. `user_id_str = payload.get("sub")`；`if not user_id_str` → 401（falsy 检查）；`int(user_id_str)` 失败（`ValueError`）→ 401。
  5. 按 `User.id == user_id` 查 users 表（`scalar_one_or_none`）；`user is None or not user.is_active` → 401。
  6. 所有 401 共用 `cred_exc`：`status=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"}`。
  7. 成功返回 `User` ORM 对象（含 `is_admin`/`is_active`/`username`/`id`）。

### `resolve_role`（`src/service/permission_deps.py`）

- **用途**：计算 user 在 project 的最终 role。（project 族三条路由**均未使用**，仅 `require_project_role` 工厂内部调用，供 qa/code 等路由用。）
- **语义**：
  1. `user.is_admin=True` → 直接返回 `"owner"`（不查表）。
  2. 查 `user_project_access` 取直接成员 role（`db.scalar(select(UserProjectAccess.role).filter_by(user_id=user.id, project_id=project.id))`）。
  3. 从 `project.group_id` 沿 `parent_group_id` 向上遍历 group 继承链，`visited` set 防循环，`depth >= 3` 截断（depth 0/1/2，即最多 3 层）。
  4. `_pick_higher(direct, inherited)` 取 max（按 `ROLE_RANK` 比较）。
  5. 返回 role 字符串或 `None`。

### `require_project_role` 工厂（`src/service/permission_deps.py`）

- **用途**：生成 project 级权限 dependency，用法 `Depends(require_project_role("reporter"))`。**默认参数 `min_role: str = "reporter"`**。
- **语义**：
  1. 工厂体在路由注册阶段校验 `if min_role not in ROLE_RANK: raise ValueError(...)`（早爆，快速失败；非法值如 `"admin"` 在 import/注册期即抛）。
  2. 返回 `checker` 闭包：先 `project = await db.get(Project, project_id)`（不存在 → 404 `"工程不存在"`），再 `resolve_role`（`None` → 403 `"无权访问此工程"`），权限不足 → 403 `f"需要 {min_role} 及以上权限（当前 {role}）"`（**全角括号 `（）`**）。
  3. 返回实际 `role` 字符串。
  - ⚠️ **project 族三条路由均未使用此 dependency**。

### `log_audit`（`src/service/audit/logger.py`）

- **用途**：向当前 session 添加一条 `AuditLog`（不主动 commit）。
- **语义**：
  1. keyword-only 参数（`*` 后）：`actor_user_id: int`、`action: str`、`resource_type: str`、`resource_id: str`、`metadata: Optional[dict]=None`、`ip_address: Optional[str]=None`。
  2. 写入 `AuditLog(actor_user_id=..., action=..., resource_type=..., resource_id=str(resource_id), metadata_json=json.dumps(metadata or {}, ensure_ascii=False), ip_address=...)`（`resource_id` 强制转 str；metadata None → `'{}'`；`ensure_ascii=False` 保留中文）。
  3. 只 `db.add(...)`，**不 commit**（caller 与业务事务一起原子提交）。
  4. 写入失败仅 `logger.warning(...)`，**不抛异常**（审计失败不中断业务）。
  - ⚠️ **project 族三条路由均未调用 `log_audit`**；`actions.PROJECT_CREATE = "project.create"` 常量存在但 v1 未用。

### `ROLE_RANK` 常量（`src/service/permission_deps.py`）

```python
ROLE_RANK: dict[str, int] = {"reporter": 1, "maintainer": 2, "owner": 3}
```

比较函数 `_pick_higher(a, b)`：任一为 None 返回另一个；否则 `a if ROLE_RANK[a] >= ROLE_RANK[b] else b`（相等时偏向 a）。`user_project_access.role` DB 约束限制为 `'reporter'`/`'maintainer'`/`'owner'` 三枚举。

> 补充提醒（族外路由相关，供 TS 实现者参考）：`require_group_role` 工厂同形，但 404 文案为 `"组不存在"`、403 无权文案为 `"无权访问此组"`，且 admin 分支直接 `return "owner"`（在 dependency 内处理，而 `resolve_group_role` 函数本身不处理 is_admin）。project 族不用，故不展开。

---

# 【project_members 族】

All confirmed. The spec is largely accurate. I found a small set of precise discrepancies. Here is the corrected complete specification.

---

## 修正清单

逐条列出对原 spec 的核对结果。源码核对基准：`project_member_router.py`、`permission_deps.py`、`auth_dependencies.py`、`audit/logger.py`、`audit/actions.py`、`db_models_homepage.py`、`db_models_groups.py`，并交叉 `routes-openapi.json`。

**实质修正（行为权威级，TS 实现必须按修正版）：**

1. **【修正 — 继承链遍历守卫位置错记】** GET 节「怪癖」原写「继承链防循环引用：`visited: set[str]` + `depth < 3` 双重保护」，但与 `require_project_role` 的 `resolve_role` **不是同一种写法**。`_list_inherited_members`（router 内）的循环条件是 **`while cur and cur not in visited and depth < 3:`**（三个条件全在 while 头里）。而 `resolve_role`（permission_deps.py）的写法是 `while cur_gid and cur_gid not in visited:` 内部再 `if depth >= 3: break`。两者**遍历层数等价**（都遍历 depth 0/1/2 共 3 层），但**赋值/计数顺序不同**：`_list_inherited_members` 是先 append+add 再查 parent 再 `depth += 1`；`resolve_role` 是先 `if depth>=3 break` 再 `visited.add` 再查 role 再查 parent 再 `depth+=1`。已在 GET 节与共享设施节分别精确还原两段循环，TS 不要把两段当成同一 helper。

2. **【修正 — `_count_direct_owners` 查询语义】** 原 spec 把 owner 计数写成裸 `SELECT COUNT(user_id) FROM user_project_access WHERE project_id=:pid AND role='owner'`。源码用的是 `select(func.count(UserProjectAccess.user_id)).filter_by(project_id=project_id, role="owner")`，且 **`db.scalar(...) or 0`** 兜底（空表返回 None → 0）。语义等价，但「`or 0` 兜底」是必须移植的微行为，已补。

3. **【修正 — POST/PATCH/DELETE 的 `db.get(User/UPA)` vs GET 的查询方式不一致点澄清】** 原 spec 在多处把「直接成员列表查询」与「单成员主键查询」混用描述。已逐路由钉死：GET 用 `db.scalars(select(UserProjectAccess).filter_by(project_id=...)).all()`（返回 ORM 列表）；POST/PATCH/DELETE 用 `db.get(UserProjectAccess, (user_id, project_id))`（复合主键 tuple 单查）。

4. **【补充 — `resolve_role` 中 direct 成员查询只取 role 列】** 原 spec 共享设施节伪代码 OK，但未点明源码是 `select(UserProjectAccess.role).filter_by(...)`（只 SELECT role 列，非整行），且继承链用 `_pick_higher(direct, inherited)` 合并、`is_admin` 短路返回 `"owner"` **完全不查库**。已补精确语义。

5. **【补充 — `require_project_role` 启动期校验】** 原 spec 漏记：`require_project_role(min_role)` 在工厂函数体首行就校验 `if min_role not in ROLE_RANK: raise ValueError(...)`（路由注册期早爆，非运行期）。本族传入值全合法（reporter/owner），不影响运行，但 TS 工厂若做等价封装应保留此校验。已补。

6. **【补充 — `inherited_from_group_id` 取「最近层 / 首次写入层」而非「贡献最高 role 层」的精确语义】** 原 spec 写「`inherited_from_group_id` = 贡献最高 role 的那个 group_id」——**这是对的**，源码 `by_user[user.id] = (member, user)` 仅在严格 `>`（更高 role）时覆盖，平级不覆盖，所以记录的是「第一个达到当前最高 role 的 group」。已精确化措辞（平级 role 不覆盖 → 保留先到的 group_id），消除歧义。

**核对通过、无需修正的点（抽查记录）：**

- 状态码：401 / 404 / 403 / 409 / 201 / 200 / 204 / 422 全部逐一比对源码 `raise HTTPException(status_code=...)` 与 `@router.post(status_code=201)` / `@router.delete(status_code=204)` ——全部正确，并经 `routes-openapi.json` 交叉确认（POST=201、DELETE=204、PATCH/GET=200）。
- detail 文案逐字（中文）：`工程不存在`、`无权访问此工程`、`需要 {min_role} 及以上权限（当前 {role}）`（全角括号）、`用户不存在`、`该用户已经是成员`、`成员不存在`、`项目必须至少 1 个直接 owner`、`Not authenticated`（含 `WWW-Authenticate: Bearer` header）——全部逐字命中。
- 审计 action 字符串：`project_member.add` / `project_member.role_change` / `project_member.remove`——与 `actions.py` 逐字一致。
- 审计 metadata 字段名：POST=`{target_user_id, role}`、PATCH=`{target_user_id, old_role, new_role}`、DELETE=`{target_user_id, role}`、`resource_type="project_member"`、`resource_id=project_id`——全部命中。
- 审计写入时点（commit 前、DELETE 在 `db.delete` 前先存 `deleted_role`、PATCH 在赋值前存 `old_role`）——正确。
- Last-owner 保护触发条件：PATCH `access.role=="owner" and body.role!="owner"`；DELETE 仅 `access.role=="owner"`；判定阈值 `owner_count <= 1`——全部正确。
- Pydantic：`user_id: int`（无 min/max）、`role: Field(..., pattern=r"^(reporter|maintainer|owner)$")`——正确。
- 表结构：`user_project_access` 复合主键 `(user_id Integer, project_id String(64))`、**无 `added_at` 列**（已确认；`added_at` 仅存在于 `group_members`）；`audit_logs` 列与类型（`action varchar64` / `resource_type varchar32` / `resource_id varchar128` / `metadata_json Text nullable` / `ip_address varchar45` / `actor_user_id` FK SET NULL）——全部命中。
- `log_audit`：keyword-only 签名、不自 commit、失败仅 warning、`metadata or {}` + `ensure_ascii=False`、`str(resource_id)`——全部正确。
- 无分页/无排序/无「禁止删自己」检查——确认无相关代码，spec 正确。

---

# project_members 族行为规范（修正版 · 权威）

> 来源：`/Users/java/knowledge-engineering/src/service/project_member_router.py`
> 交叉核对：`docs/porting/routes-openapi.json`、`permission_deps.py`、`auth_dependencies.py`、`audit/logger.py`、`audit/actions.py`、`db_models_homepage.py`、`db_models_groups.py`

---

## 全局约束（适用本族所有路由）

- **路由前缀**：`/projects`，tag `project-members`
- **Router 级依赖**：`Depends(require_infra_healthy)`（在 `APIRouter(... dependencies=[...])` 上），任意 critical 基础设施不可用时返回 `503 INFRA_UNHEALTHY`（在每个单路由守卫**之前**执行）
- **合法 role 枚举**：`reporter` / `maintainer` / `owner`
- **ROLE_RANK**（`permission_deps.py`）：`{ reporter: 1, maintainer: 2, owner: 3 }`
- **直接成员表**：`user_project_access`，复合主键 `(user_id INT FK→users.id ON DELETE CASCADE, project_id VARCHAR(64) FK→projects.id ON DELETE CASCADE)`，`role VARCHAR(32) DEFAULT 'reporter' NOT NULL`，**无 `added_at` 列**（`added_at` 仅存在于 `group_members`）
- **继承成员来源**：`group_members` 表，经祖先 group 链计算
- **Last-owner 保护**：仅统计 `user_project_access`（直接成员）中 `role='owner'` 的行，**不计入** group 继承 owner

---

### GET /projects/{project_id}/members

- **守卫**（顺序）：
  1. `require_infra_healthy`（Router 级）
  2. `require_project_role("reporter")`（声明为路由 `dependencies=[...]`），其内部依次：
     - `get_current_user`（注入）→ JWT Bearer 解析，失败 `401 Not authenticated`
     - `db.get(Project, project_id)` → 不存在抛 `404 "工程不存在"`
     - `resolve_role(user, project, db)` → 计算最终 role
     - `role is None` → `403 "无权访问此工程"`
     - `ROLE_RANK[role] < ROLE_RANK["reporter"]` → `403 "需要 reporter 及以上权限（当前 {role}）"`
  > 注：路由函数本身**未**通过参数接收 `require_project_role` 的返回值（它走 `dependencies=[]` 列表），所以拿不到 role 字符串；仅做守卫。

- **请求**：
  | 位置 | 字段 | 类型 | 必选 | 默认 | 约束 |
  |------|------|------|------|------|------|
  | path | `project_id` | string | 是 | — | — |

  无 query 参数，无 body。

- **成功**：`200 OK`，`response_model=ProjectMembersResponse`

  ```json
  {
    "direct": [
      { "user_id": 1, "project_id": "abc", "role": "owner" }
    ],
    "inherited": [
      { "user_id": 2, "username": "alice", "role": "maintainer", "inherited_from_group_id": "g-001" }
    ]
  }
  ```

  - `direct`：`select(UserProjectAccess).filter_by(project_id=project_id)` 的全部行 → `DirectMemberResponse{ user_id(int), project_id(str), role(str) }`
  - `inherited`：来自祖先 group 链的 `group_members`（去重后）→ `InheritedMemberResponse{ user_id(int), username(str), role(str), inherited_from_group_id(str) }`
  - 两数组均不分页、无显式排序（按 DB / dict 迭代顺序）
  - `project.group_id` 为 `null` 或无祖先 group → `inherited: []`

- **错误**：
  | status | detail 文案（逐字） |
  |--------|---------------------|
  | 401 | `Not authenticated`（header `WWW-Authenticate: Bearer`）|
  | 404 | `工程不存在` |
  | 403 | `无权访问此工程` |
  | 403 | `需要 reporter 及以上权限（当前 {role}）`（全角括号）|

- **DB**：
  - 直接成员：`db.scalars(select(UserProjectAccess).filter_by(project_id=...)).all()`
  - 继承（`_list_inherited_members`）：
    1. `project = db.get(Project, project_id)`；`if not project or not project.group_id: return []`
    2. 收集祖先 group id 链 —— **循环写法（逐字）**：
       ```python
       cur = project.group_id; visited = set(); depth = 0; ancestor_gids = []
       while cur and cur not in visited and depth < 3:
           ancestor_gids.append(cur)
           visited.add(cur)
           cur = db.scalar(select(Group.parent_group_id).filter_by(id=cur))
           depth += 1
       ```
       —— 即三条件全在 while 头，遍历 depth 0/1/2 共 **3 层**（直接 group + 父 + 爷）。
    3. `if not ancestor_gids: return []`
    4. `select(GroupMember, User).join(User, User.id==GroupMember.user_id).filter(GroupMember.group_id.in_(ancestor_gids))`
    5. 按 `user.id` 去重，仅当新行 `ROLE_RANK[member.role] > ROLE_RANK[existing.role]`（严格大于）才覆盖
  - 无写操作、无事务、无 commit

- **审计**：不写 `audit_logs`

- **怪癖**：
  - **direct 去重优先**：`direct_uids = {acc.user_id for acc in direct_records}`，`inherited` 过滤 `m.user_id not in direct_uids`。同时是直接+继承的用户**只出现在 direct**。
  - 继承链防循环：`while cur and cur not in visited and depth < 3`（visited set + depth<3 双保护，写法见上，**与 `resolve_role` 的「内部 if depth>=3 break」写法不同但层数等价**）
  - 平级 role 不覆盖：同一用户多 group 同 role 时，`inherited_from_group_id` 保留**首个达到最高 role 的 group**（严格 `>` 才换）
  - `project.group_id == None` → 直接 `inherited: []`，不报错

---

### POST /projects/{project_id}/members

- **守卫**（顺序）：
  1. `require_infra_healthy`（Router 级）
  2. `require_project_role("owner")`（路由 `dependencies=[]`）：同 GET 内部流程，role < owner → `403 "需要 owner 及以上权限（当前 {role}）"`
  3. 路由函数另注入 `user = Depends(get_current_user)`（用于 actor）、`request: Request`、`db`

- **请求**：
  | 位置 | 字段 | 类型 | 必选 | 默认 | 约束 |
  |------|------|------|------|------|------|
  | path | `project_id` | string | 是 | — | — |
  | body | `user_id` | integer | 是 | — | 无 min/max（裸 `int`）|
  | body | `role` | string | 是 | — | `Field(..., pattern=r"^(reporter\|maintainer\|owner)$")`，不符 → `422` |

  Content-Type: `application/json`（`MemberAddRequest`）

- **成功**：`201 Created`，`response_model=DirectMemberResponse`

  ```json
  { "user_id": 5, "project_id": "abc", "role": "reporter" }
  ```

  数据来自 `db.refresh(access)` 后的 ORM 值。

- **错误**：
  | status | detail 文案（逐字） |
  |--------|---------------------|
  | 401 | `Not authenticated` |
  | 404 | `工程不存在`（守卫阶段） |
  | 403 | `无权访问此工程` |
  | 403 | `需要 owner 及以上权限（当前 {role}）` |
  | 404 | `用户不存在`（目标 `user_id` 不在 `users`）|
  | 409 | `该用户已经是成员`（`(user_id, project_id)` 已在 `user_project_access`）|
  | 422 | Pydantic validation error（`role` pattern / 类型）|

- **DB**（业务体顺序）：
  1. `target_user = db.get(User, body.user_id)` → 不存在 `404 "用户不存在"`
  2. `existing = db.get(UserProjectAccess, (body.user_id, project_id))` → 存在 `409 "该用户已经是成员"`
  3. `db.add(UserProjectAccess(user_id=body.user_id, project_id=project_id, role=body.role))`
  4. `log_audit(...)`（同 session add，不 commit）
  5. `await db.commit()`（UPA + AuditLog 原子提交）
  6. `await db.refresh(access)` → 返回

- **审计**：
  - action：`actions.PROJECT_MEMBER_ADD` = `"project_member.add"`
  - `resource_type`：`"project_member"`
  - `resource_id`：`project_id`（path 字符串）
  - `metadata`：`{ "target_user_id": body.user_id, "role": body.role }`
  - `actor_user_id`：`user.id`
  - `ip_address`：`request.client.host if request.client else None`
  - 写入时机：`db.add(access)` 之后、`db.commit()` 之前

- **怪癖**：
  - 检查顺序固定：**先查目标 User 存在（404）→ 再查是否已是成员（409）**，顺序不可换
  - 重复检查用 `db.get(UPA, (user_id, project_id))` 复合主键精确查
  - `resource_id` 是 `project_id`（以 project 为资源主体），非 `user_id`
  - `log_audit` 失败仅 warning 不中断

---

### PATCH /projects/{project_id}/members/{user_id}

- **守卫**（顺序）：
  1. `require_infra_healthy`（Router 级）
  2. `require_project_role("owner")`
  3. 路由另注入 `user = Depends(get_current_user)`、`request`、`db`

- **请求**：
  | 位置 | 字段 | 类型 | 必选 | 默认 | 约束 |
  |------|------|------|------|------|------|
  | path | `project_id` | string | 是 | — | — |
  | path | `user_id` | integer | 是 | — | FastAPI 转 `int`；非整数 → `422` |
  | body | `role` | string | 是 | — | `Field(..., pattern=r"^(reporter\|maintainer\|owner)$")` |

  body schema：`MemberRoleUpdateRequest`

- **成功**：`200 OK`，`response_model=DirectMemberResponse`

  ```json
  { "user_id": 5, "project_id": "abc", "role": "maintainer" }
  ```

  返回 `db.refresh(access)` 后值。

- **错误**：
  | status | detail 文案（逐字） |
  |--------|---------------------|
  | 401 | `Not authenticated` |
  | 404 | `工程不存在` |
  | 403 | `无权访问此工程` |
  | 403 | `需要 owner 及以上权限（当前 {role}）` |
  | 404 | `成员不存在`（`(user_id, project_id)` 不在 `user_project_access`）|
  | 422 | `项目必须至少 1 个直接 owner`（Last-owner 降级保护）|
  | 422 | Pydantic validation error（role pattern）|

- **DB**（业务体顺序）：
  1. `access = db.get(UserProjectAccess, (user_id, project_id))` → 不存在 `404 "成员不存在"`
  2. **若 `access.role == "owner" and body.role != "owner"`**：`owner_count = _count_direct_owners(...)`；`if owner_count <= 1: 422`
  3. `old_role = access.role`（赋值前保存）
  4. `access.role = body.role`（ORM 属性赋值）
  5. `log_audit(...)`
  6. `await db.commit()`
  7. `await db.refresh(access)` → 返回

- **审计**：
  - action：`actions.PROJECT_MEMBER_ROLE_CHANGE` = `"project_member.role_change"`
  - `resource_type`：`"project_member"`
  - `resource_id`：`project_id`
  - `metadata`：`{ "target_user_id": user_id, "old_role": old_role, "new_role": body.role }`
  - `actor_user_id`：`user.id`
  - `ip_address`：`request.client.host if request.client else None`
  - 写入时机：`access.role = body.role` 赋值之后、`db.commit()` 之前

- **怪癖**：
  - Last-owner 保护触发（精确）：`access.role == "owner"` **且** `body.role != "owner"`，两条件都满足才查计数；同值改 owner→owner 跳过保护
  - 阈值 `owner_count <= 1`（`<=` 而非 `==`，防御性）
  - `old_role` 在赋值前保存，确保审计记录旧值

---

### DELETE /projects/{project_id}/members/{user_id}

- **守卫**（顺序）：
  1. `require_infra_healthy`（Router 级）
  2. `require_project_role("owner")`
  3. 路由另注入 `user = Depends(get_current_user)`、`request`、`db`

- **请求**：
  | 位置 | 字段 | 类型 | 必选 | 默认 | 约束 |
  |------|------|------|------|------|------|
  | path | `project_id` | string | 是 | — | — |
  | path | `user_id` | integer | 是 | — | FastAPI 转 `int`；非整数 → `422` |

  无 body，无 query。

- **成功**：`204 No Content`，无响应体（`@router.delete(status_code=204)`，无 `response_model`）

- **错误**：
  | status | detail 文案（逐字） |
  |--------|---------------------|
  | 401 | `Not authenticated` |
  | 404 | `工程不存在` |
  | 403 | `无权访问此工程` |
  | 403 | `需要 owner 及以上权限（当前 {role}）` |
  | 404 | `成员不存在` |
  | 422 | `项目必须至少 1 个直接 owner` |

- **DB**（业务体顺序）：
  1. `access = db.get(UserProjectAccess, (user_id, project_id))` → 不存在 `404 "成员不存在"`
  2. **若 `access.role == "owner"`**：`owner_count = _count_direct_owners(...)`；`if owner_count <= 1: 422`
  3. `deleted_role = access.role`（**删除前**保存）
  4. `log_audit(...)`
  5. `await db.delete(access)`（硬删，无软删标记）
  6. `await db.commit()`（AuditLog + DELETE 原子提交）

- **审计**：
  - action：`actions.PROJECT_MEMBER_REMOVE` = `"project_member.remove"`
  - `resource_type`：`"project_member"`
  - `resource_id`：`project_id`
  - `metadata`：`{ "target_user_id": user_id, "role": deleted_role }`
  - `actor_user_id`：`user.id`
  - `ip_address`：`request.client.host if request.client else None`
  - 写入时机：**`db.delete(access)` 之前**（必须先存 `deleted_role`）

- **怪癖**：
  - Last-owner 保护对 DELETE 触发条件：只要 `access.role == "owner"` 即查计数（无 body）；删任何 owner 都会检查
  - PATCH 与 DELETE 共用 `_count_direct_owners` helper
  - **无「禁止删自己」检查**：owner 可删自己，只要不是最后一个 direct owner
  - 硬删（`db.delete`），无软删

---

## 依赖的共享设施

### `require_infra_healthy`（`deps_infra.py`）

Router 级 `Depends`，任一 critical 依赖不可用时返回 `503 INFRA_UNHEALTHY`。本规范未深入其源码，TS 实现需对齐同等 health-check 中间件，且必须在单路由守卫之前执行。

### `get_current_user`（`auth_dependencies.py`）

- `oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)` → token 为 `Optional[str]`
- 统一异常 `cred_exc`：`401`，detail `"Not authenticated"`，header `{"WWW-Authenticate": "Bearer"}`
- 流程：`if not token → 401`；`payload = decode_token(token)`；`if not payload or payload.get("type") != "access" → 401`；`sub = payload.get("sub")`，`if not sub → 401`；`int(sub)` 失败（ValueError）→ `401`
- `user = (db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()`
- `if user is None or not user.is_active → 401`
- 成功返回 `User` ORM 对象

### `require_project_role(min_role="reporter")`（`permission_deps.py`）

工厂函数，**函数体首行做启动期校验**：`if min_role not in ROLE_RANK: raise ValueError(...)`（路由注册期早爆，非运行期 500）。返回 async `checker` 闭包，参数 `(project_id: str, user=Depends(get_current_user), db=Depends(get_db))`，执行顺序：

1. `project = db.get(Project, project_id)` → 不存在 `404 "工程不存在"`
2. `role = resolve_role(user, project, db)`
3. `if role is None → 403 "无权访问此工程"`
4. `if ROLE_RANK[role] < ROLE_RANK[min_role] → 403 f"需要 {min_role} 及以上权限（当前 {role}）"`（全角括号）
5. 通过：`return role`（但本族路由都用 `dependencies=[]` 形式，不接收此返回值）

**`resolve_role(user, project, db)` 语义（精确）**：

```
if user.is_admin: return "owner"          # 短路，完全不查库
direct = db.scalar(select(UserProjectAccess.role)
                   .filter_by(user_id=user.id, project_id=project.id))   # 只取 role 列
inherited = None; cur = project.group_id; visited = set(); depth = 0
while cur and cur not in visited:          # 注意：depth 在循环体内 if 检查
    if depth >= 3: break
    visited.add(cur)
    role = db.scalar(select(GroupMember.role).filter_by(user_id=user.id, group_id=cur))
    if role: inherited = _pick_higher(inherited, role)
    cur = db.scalar(select(Group.parent_group_id).filter_by(id=cur))
    depth += 1
return _pick_higher(direct, inherited)
```

- Instance Admin（`user.is_admin = true`）直接返回 `"owner"`，不查库
- 继承链深度上限 **3 层**（depth 0/1/2：direct group / 父 / 爷），第 4 层防御性截断
- `visited set` 防循环；`_pick_higher(a, b)`：任一为 None 返回另一个，否则 `a if ROLE_RANK[a] >= ROLE_RANK[b] else b`（平级取 a）
- **写法差异提示**：此处 depth 检查是「while 头只判 `cur and cur not in visited`，循环体内 `if depth>=3: break`」；而 router 内的 `_list_inherited_members` 是「`while cur and cur not in visited and depth < 3`」——遍历层数等价，但赋值/计数语句顺序不同，移植时不要合并成同一函数。

### `_count_direct_owners(project_id, db)`（`project_member_router.py` 内）

```python
count = db.scalar(
    select(func.count(UserProjectAccess.user_id))
    .filter_by(project_id=project_id, role="owner")
)
return count or 0      # 空表 None → 0 兜底
```

仅统计直接成员表的 owner，不含 group 继承。PATCH 降级 / DELETE 删 owner 时调用。

### `log_audit`（`audit/logger.py`）

- 签名：`log_audit(db, *, actor_user_id: int, action: str, resource_type: str, resource_id: str, metadata: Optional[dict]=None, ip_address: Optional[str]=None) -> None`（`*` 后全 keyword-only）
- 向 session `db.add(AuditLog(...))`，**不自己 commit**，由 caller 与业务原子提交
- `resource_id=str(resource_id)`（统一转字符串）
- `metadata_json = json.dumps(metadata or {}, ensure_ascii=False)`（None → `'{}'`，中文不转义）
- 整个 `db.add` 包在 try/except 内，失败仅 `logger.warning("audit log 写入失败 ...")`，**不抛错**，业务正常响应
- `audit_logs` 表列：`id(Integer PK autoincrement)` / `actor_user_id(Integer FK→users.id ON DELETE SET NULL, nullable)` / `action(String 64, NOT NULL)` / `resource_type(String 32, NOT NULL)` / `resource_id(String 128, NOT NULL)` / `metadata_json(Text, nullable)` / `ip_address(String 45, nullable)` / `created_at(DateTime, server_default now(), NOT NULL)`

### action 常量（`audit/actions.py`，逐字值）

| 常量名 | 字符串值 |
|--------|----------|
| `PROJECT_MEMBER_ADD` | `"project_member.add"` |
| `PROJECT_MEMBER_REMOVE` | `"project_member.remove"` |
| `PROJECT_MEMBER_ROLE_CHANGE` | `"project_member.role_change"` |

---

相关源码（绝对路径）：
- `/Users/java/knowledge-engineering/src/service/project_member_router.py`（主体，584 行）
- `/Users/java/knowledge-engineering/src/service/permission_deps.py`（`require_project_role` / `resolve_role` / `ROLE_RANK`）
- `/Users/java/knowledge-engineering/src/service/auth_dependencies.py`（`get_current_user`）
- `/Users/java/knowledge-engineering/src/service/audit/logger.py`、`/Users/java/knowledge-engineering/src/service/audit/actions.py`
- `/Users/java/knowledge-engineering/src/service/db_models_homepage.py`（`UserProjectAccess` / `Project`）、`/Users/java/knowledge-engineering/src/service/db_models_groups.py`（`Group` / `GroupMember` / `AuditLog`）

---

# 【groups 族】

I have verified everything against source. Here are the key errors I found in the spec, then the corrected full version.

Critical findings:
- **`POST /groups` 502 circular-detection detail wrong**: spec says `"嵌套异常：深度超过预期上限"` is the circular-detection message, but that string is the *depth-overrun* (`depth > 6`) message. The actual `visited` circular-detection raises 500 `"检测到 group 循环依赖"` — spec omitted this string entirely.
- **`VALID_MEMBER_ROLES` type wrong**: spec lists it as a plain set `{...}`; source defines it as a `frozenset`. Also spec claims `MemberAddRequest.role` uses `Literal`; it actually uses `pattern`.
- **Guard ordering misstated everywhere**: `require_infra_healthy` itself depends on `get_current_user`, so auth (401) runs *inside* the infra check, and the 503 vs 401 ordering is not "infra short-circuits first then auth." Also `require_infra_healthy` 503 has a structured `detail` dict, not a plain string, and has an `INFRA_UNINITIALIZED` variant.
- **`resolve_group_role` depth break wording**: spec says `depth >= 3` — source breaks at `depth >= 3` but depth counts 0/1/2, so it walks **3 levels** (self + parent + grandparent), matching, but spec's "最多走 3 层" is correct. Verified OK.
- Minor: `GROUP_MEMBER_ROLE_CHANGE` comment, `_count_group_owners` uses `func.count(GroupMember.user_id)` — confirmed.

---

## 修正清单

1. **POST /groups — 漏掉一条 500 detail 文案 + 错记另一条**：规范「怪癖」里只写了 `_group_depth` 的 `> MAX_GROUP_DEPTH * 2 = 6 → 500 "嵌套异常：深度超过预期上限"`，但**漏掉了循环检测分支**：`parent in visited` 时抛 **500 `"检测到 group 循环依赖"`**（源码第 186-189 行）。两条 500 是独立分支，都已补入。
2. **VALID_MEMBER_ROLES 类型错记**：规范写成普通 set `{...}`，源码是 **`frozenset`**（第 595 行 `frozenset({...})`）。已改。另：此常量在路由逻辑里**实际未被引用**（role 校验全靠 Pydantic pattern），仅作模块级常量存在，已注明。
3. **MemberAddRequest.role 校验机制错记**：规范常量表/请求表暗示 `Literal`，源码 docstring 提到 Literal 但**实际用 `pattern=r"^(reporter|maintainer|owner)$"`**（第 608-613 行）。MemberRoleUpdateRequest.role 同理。已确认为 pattern，非 Literal。
4. **守卫链与状态码顺序重写**：规范把 `require_infra_healthy`（503）描述为「所有路由共享、503 时短路」并列在 `get_current_user` 之前，**误导性**。源码 `deps_infra.py` 第 26-29 行：`require_infra_healthy` **自身 `Depends(get_current_user)`**，即认证（401）发生在 infra 检查**内部/之前**，未登录时先得 401 而非 503。已在每条路由「守卫」和「依赖的共享设施」节修正实际求值顺序。
5. **require_infra_healthy 503 body 形状补全**：规范说「不展开内部实现」，但移植需要：503 detail 是**结构化 dict**，有两种——`{"code":"INFRA_UNHEALTHY","message":"系统暂时不可用，请联系管理员"}`（admin 额外带 `"deps"`），以及 startup 未跑完时 `{"code":"INFRA_UNINITIALIZED","message":"系统正在初始化，请稍后重试"}`。已补。
6. **GET /groups 普通用户路径表述精确化**：规范说「BFS `range(3)` 层下展（最多 3 层子孙）」——源码 `_expand_user_groups` 第 0 层是直接成员，`range(3)` 再向下 3 层，所以**总可见层数 = 直接层 + 3 层子孙**。已保留并精确。
7. **GET/PATCH/DELETE /groups/{group_id} 的 require_group_role 内部顺序**：规范基本正确，补一处——dependency 内 `is_admin` 检查**在 group 存在性检查之后**（先 404 才轮到 admin 放行），即**不存在的 group 即便 admin 也得 404**。已在共享设施节强调顺序。
8. **PATCH /groups 审计 metadata 的 changes 值结构**：源码内部用 `changes[k] = (old, new)` 元组，写审计时才展开成 `{"old":..., "new":...}`，规范的最终 metadata 形状正确，已确认。
9. **DELETE /groups 子组/工程检查 SQL 语义**：源码用 `select(Group.id).filter_by(parent_group_id=...).limit(1)` + `db.scalar`，返回的是 **id 标量值**（真值判断），规范写 `SELECT id ... LIMIT 1` 正确。已确认。
10. **审计 action 常量表**：逐一比对 `actions.py` 第 57-73 行，6 个 group 族常量值**全部逐字正确**。已确认无误。
11. **抽查通过项**（无修正）：所有 detail 中文文案逐字（`"仅 Instance Admin 能建根 group"`/`"父组不存在"`/`"需要父组 Owner 权限"`/`f"嵌套深度超过 {MAX_GROUP_DEPTH} 层"`/`"组 ID 已存在"`/`"组不存在"`/`"无权访问此组"`/`f"需要 {min_role} 及以上权限（当前 {role}）"`/`"先删子组"`/`"先迁移工程"`/`"用户不存在"`/`"该用户已经是成员"`/`"成员不存在"`/`"组必须至少 1 个 owner"`）；Pydantic Field 边界（id `min_length=2,max_length=64,pattern`、name `1/128`、description `512`）；分页默认 `page=1,limit=50`、`ge=1,le=200`、`_validate_page_limit` clamp；status（201/200/204/403/404/409/422/503）；audit `resource_id` 均为 `group_id`（成员路由也是 group_id 非 user_id）。

---

# Groups 族行为规范（Python → TypeScript 移植权威参考）— 已核验修正版

源码：`/Users/java/knowledge-engineering/src/service/group_router.py`
审计路由：`/Users/java/knowledge-engineering/src/service/audit_router.py`（第 344-485 行）
共享依赖：`permission_deps.py`、`auth_dependencies.py`、`audit/logger.py`、`audit/actions.py`、`deps_infra.py`

---

## 常量

| 名称 | 值/类型 | 含义 |
|---|---|---|
| `MAX_GROUP_DEPTH` | `3`（int） | 嵌套最大深度（根层计为第 1 层；第 4 层被拒绝） |
| `VALID_MEMBER_ROLES` | **`frozenset({"reporter","maintainer","owner"})`** | 合法角色集合。⚠️ **实际未在路由逻辑中被引用**（role 校验全走 Pydantic pattern），仅作模块级常量存在 |
| `ROLE_RANK` | `{reporter:1, maintainer:2, owner:3}`（dict，定义在 `permission_deps.py`） | 角色等级（数字越大权限越高） |

> ⚠️ role 字段校验机制：`MemberAddRequest.role` / `MemberRoleUpdateRequest.role` 均用 **Pydantic `Field(..., pattern=r"^(reporter|maintainer|owner)$")`**，**不是 `Literal`**（源码 docstring 提到 Literal 但实际未用）。非法值 → FastAPI 自动 422。

---

## ⚠️ 全族通用守卫链（务必理解求值顺序）

Router 构造：`APIRouter(prefix="/groups", dependencies=[Depends(require_infra_healthy)])`。但 **`require_infra_healthy` 自身 `Depends(get_current_user)`**（`deps_infra.py` 第 28 行）。因此实际求值顺序为：

```
1. get_current_user（解析 JWT）—— 失败 → 401 "Not authenticated"
2. require_infra_healthy 主体（用到 user.is_admin 决定是否暴露 deps）
     - app.state.infra_status 缺失 → 503 {"code":"INFRA_UNINITIALIZED","message":"系统正在初始化，请稍后重试"}
     - 有 critical dep 不健康 → 503 {"code":"INFRA_UNHEALTHY","message":"系统暂时不可用，请联系管理员"}（admin 额外带 "deps": <infra_status dict>）
3. 路由级 require_group_role(...)（若有）—— 内部又各自 Depends(get_current_user)+get_db
4. 路由体
```

**移植要点**：未登录请求得 **401 而非 503**（因为 infra 检查依赖 auth）。503 的 `detail` 是**结构化对象**，非字符串。`_NON_CRITICAL_DEPS = {"neo4j"}`（neo4j 不健康不触发 503）。

---

### POST /groups

- **守卫**:
  1. `get_current_user`（隐含在 infra dep 内；失败 401 `"Not authenticated"`）
  2. `require_infra_healthy`（503 见上）
  3. 路由体内手动权限校验（见下）

- **请求**:
  - Body（JSON），Schema `GroupCreateRequest`：

  | 字段 | 类型 | 必选 | 约束/默认 |
  |---|---|---|---|
  | `id` | string | 是（`Field(...)`） | `min_length=2`, `max_length=64`, `pattern=r"^[a-z][a-z0-9/\-]*[a-z0-9]$"` |
  | `name` | string | 是（`Field(...)`） | `min_length=1`, `max_length=128` |
  | `description` | string\|null | 否 | 默认 `None`, `max_length=512` |
  | `parent_group_id` | string\|null | 否 | 默认 `None`（裸默认值，无 Field）；`None` = 建根 group |

- **成功**: `201 Created`，Schema `GroupResponse`
  ```json
  {
    "id": "string",
    "name": "string",
    "description": "string|null",
    "parent_group_id": "string|null",
    "created_at": "ISO8601 string (datetime.isoformat())"
  }
  ```

- **错误**（按路由体校验顺序）:

  | # | 条件 | status | detail（逐字） |
  |---|---|---|---|
  | 1 | `parent_group_id is None` 且 `user.is_admin == False` | 403 | `仅 Instance Admin 能建根 group` |
  | 2 | `parent_group_id` 非 None 且父组 `db.get(Group, parent_group_id)` 为 None | 404 | `父组不存在` |
  | 3 | `parent_group_id` 非 None，非 admin，`resolve_group_role(...) != "owner"` | 403 | `需要父组 Owner 权限` |
  | 4 | `parent_group_id` 非 None，`_group_depth(parent)+1 > 3` | 422 | `嵌套深度超过 3 层`（f-string：`f"嵌套深度超过 {MAX_GROUP_DEPTH} 层"`） |
  | 5 | `db.get(Group, body.id)` 已存在 | 409 | `组 ID 已存在` |
  | — | `_group_depth` 内部循环检测（`parent in visited`） | 500 | `检测到 group 循环依赖` |
  | — | `_group_depth` 内部深度溢出（`depth > MAX_GROUP_DEPTH * 2`，即 `> 6`） | 500 | `嵌套异常：深度超过预期上限` |

- **DB**:
  - 表：`groups`（INSERT）、`group_members`（INSERT）、`audit_logs`（INSERT）
  - 校验顺序：① 建根权限 / ②父组存在 → ③父组 role → ④深度（`_group_depth` 沿 `parent_group_id` 向上 `db.scalar(select(Group.parent_group_id).filter_by(id=cur))`）→ ⑤ ID 去重 → ⑥ INSERT
  - 三张表在**同一事务**内 `await db.commit()` 一次原子提交
  - commit 后 `await db.refresh(group)` 再 `_to_response` 序列化

- **审计**:
  - `action`: `actions.GROUP_CREATE` = `"group.create"`
  - `resource_type`: `"group"`
  - `resource_id`: `group.id`（ORM 对象 id）
  - `metadata`: `{"name": group.name, "parent_group_id": group.parent_group_id}`
  - `ip_address`: `request.client.host if request.client else None`

- **怪癖**:
  - Instance Admin 建子组跳过 role 校验（②父组存在仍查，④深度仍校验）
  - 创建者自动写 `group_members`：`role="owner"`, `added_by_user_id = user.id`（自己加自己）
  - `created_by_user_id = user.id` 写入 groups 行
  - `_group_depth`：根 group 深度=1；维护 `visited` set；两条独立 500 防御分支（见上表）

---

### GET /groups

- **守卫**:
  1. `get_current_user`（隐含在 infra dep）
  2. `require_infra_healthy`

- **请求**: 无 path/query/body 参数

- **成功**: `200 OK`，`list[GroupResponse]`
  ```json
  [ { "id": "...", "name": "...", "description": "...|null", "parent_group_id": "...|null", "created_at": "ISO8601" } ]
  ```
  （空列表 `[]` 正常返回）

- **错误**: 无业务错误（仅 401 / 503）

- **DB**:
  - `user.is_admin`：`(await db.scalars(select(Group))).all()`（无 WHERE，全量）
  - 普通用户：
    1. `_expand_user_groups(user.id, db)`（在函数体内局部 `from ... import`）→ 可见 group id 集合
    2. 集合为空 → 直接 `return []`（不查 DB）
    3. 否则 `select(Group).filter(Group.id.in_(gids))`

- **审计**: 不写

- **怪癖**:
  - `_expand_user_groups`：第 0 层=直接成员 group，再 `for _ in range(3)` 向下 BFS（即直接层 + 最多 3 层子孙），`new = children - all_accessible` 无新节点则提前 break
  - 无显式 ORDER BY，顺序不保证

---

### GET /groups/{group_id}

- **守卫**:
  1. `get_current_user` + `require_infra_healthy`
  2. `Depends(require_group_role("reporter"))`（dependencies 列表，仅副作用）。内部顺序：
     - `db.get(Group, group_id)` 不存在 → 404 `组不存在`
     - `user.is_admin` → 返回 `"owner"`（**在存在性检查之后**）
     - 否则 `resolve_group_role(user.id, group_id, db)`；None → 403 `无权访问此组`；`ROLE_RANK[role] < ROLE_RANK["reporter"]` → 403 `需要 reporter 及以上权限（当前 {role}）`

- **请求**: Path `group_id: string`

- **成功**: `200 OK`，`GroupResponse`

- **错误**:

  | 条件 | status | detail |
  |---|---|---|
  | dependency 中 group 不存在 | 404 | `组不存在` |
  | role 为 None（无成员关系且非 admin） | 403 | `无权访问此组` |
  | role rank 不足 | 403 | `需要 reporter 及以上权限（当前 {role}）` |
  | 路由体内 `db.get(Group, group_id)` 为 None ⚠️ | 404 | `组不存在` |

  ⚠️ 路由体（第 430 行）再次 `db.get(Group, group_id)`，理论上 dependency 已确认存在，但保留独立 404 分支。

- **DB**: `db.get(Group, group_id)` 共两次（dependency 1 + 路由体 1）

- **审计**: 不写

---

### PATCH /groups/{group_id}

- **守卫**:
  1. `get_current_user` + `require_infra_healthy`
  2. `Depends(require_group_role("maintainer"))`（最低 maintainer）
  3. 路由参数 `user: User = Depends(get_current_user)`（独立注入，写审计用）

- **请求**:
  - Path: `group_id: string`
  - Body（`GroupUpdateRequest`，全字段可选）：

  | 字段 | 类型 | 必选 | 约束/默认 |
  |---|---|---|---|
  | `name` | string\|null | 否 | 默认 `None`, `max_length=128` |
  | `description` | string\|null | 否 | 默认 `None`, `max_length=512` |

- **成功**: `200 OK`，更新后 `GroupResponse`

- **错误**:

  | 条件 | status | detail |
  |---|---|---|
  | group 不存在（dependency 中） | 404 | `组不存在` |
  | role 为 None | 403 | `无权访问此组` |
  | role rank 不足 | 403 | `需要 maintainer 及以上权限（当前 {role}）` |
  | group 不存在（路由体中，第 470 行） | 404 | `组不存在` |

- **DB**:
  - `db.get(Group, group_id)`（dependency + 路由体，共两次）
  - 仅当字段 `is not None` **且** 与现值不同时改 ORM 属性（无效变更不 UPDATE）
  - `await db.commit()` 后 `await db.refresh(group)`

- **审计**:
  - **仅当 `changes` dict 非空才写**
  - `action`: `actions.GROUP_UPDATE` = `"group.update"`；`resource_type`: `"group"`；`resource_id`: **`group_id`（路径参数）**
  - `metadata`: `{"changes": {字段名: {"old": 旧值, "new": 新值}, ...}}`（内部先存 `(old,new)` 元组，写审计时展开）

- **怪癖**:
  - `name == None` → 跳过；`name != None` 但等于现值 → 跳过（不写审计、不 UPDATE）；description 同理

---

### DELETE /groups/{group_id}

- **守卫**:
  1. `get_current_user` + `require_infra_healthy`
  2. `Depends(require_group_role("owner"))`
  3. 路由参数 `user: User = Depends(get_current_user)`（写审计用）

- **请求**: Path `group_id: string`

- **成功**: `204 No Content`（无响应体）

- **错误**（按校验顺序）:

  | 条件 | status | detail |
  |---|---|---|
  | group 不存在（dependency 中） | 404 | `组不存在` |
  | role 为 None / rank 不足 | 403 | `无权访问此组` / `需要 owner 及以上权限（当前 {role}）` |
  | group 不存在（路由体中，第 543 行） | 404 | `组不存在` |
  | 有子组 | 403 | `先删子组` |
  | 有关联工程 | 403 | `先迁移工程` |

- **DB**:
  - 子组检查：`db.scalar(select(Group.id).filter_by(parent_group_id=group_id).limit(1))`（取 id 标量，真值判断）
  - 工程检查：`db.scalar(select(Project.id).filter_by(group_id=group_id).limit(1))`
  - 顺序：审计写入 → `await db.delete(group)` → `await db.commit()`
  - 级联：`Group.members` `cascade="all, delete-orphan"` + DB 层 `group_members.group_id` FK `ON DELETE CASCADE`，成员记录自动删除

- **审计**:
  - `action`: `actions.GROUP_DELETE` = `"group.delete"`；`resource_type`: `"group"`；`resource_id`: `group_id`
  - `metadata`: `{"name": group.name}`（删前记录，因 delete 后取不到）

- **怪癖**: 硬删，无软删；审计在 `db.delete` 之前、`commit` 之前（同事务原子）

---

### GET /groups/{group_id}/members

- **守卫**: `get_current_user` + `require_infra_healthy`；`Depends(require_group_role("reporter"))`

- **请求**: Path `group_id: string`

- **成功**: `200 OK`，`list[MemberResponse]`
  ```json
  [ { "user_id": 1, "group_id": "...", "role": "reporter|maintainer|owner", "added_at": "ISO8601" } ]
  ```
  （空列表正常）

- **错误**:

  | 条件 | status | detail |
  |---|---|---|
  | group 不存在 | 404 | `组不存在` |
  | role 为 None / rank 不足 | 403 | `无权访问此组` / `需要 reporter 及以上权限（当前 {role}）` |

- **DB**: `(await db.scalars(select(GroupMember).filter_by(group_id=group_id))).all()`（无 ORDER BY，无分页）

- **审计**: 不写。**怪癖**: 无排序保证

---

### POST /groups/{group_id}/members

- **守卫**: `get_current_user` + `require_infra_healthy`；`Depends(require_group_role("owner"))`；路由参数 `user: User = Depends(get_current_user)`（写审计用）

- **请求**:
  - Path: `group_id: string`
  - Body（`MemberAddRequest`）：

  | 字段 | 类型 | 必选 | 约束 |
  |---|---|---|---|
  | `user_id` | integer | 是（裸 `int`） | 目标用户 DB int id |
  | `role` | string | 是（`Field(...)`） | `pattern=r"^(reporter\|maintainer\|owner)$"` |

- **成功**: `201 Created`，`MemberResponse`
  ```json
  { "user_id": 1, "group_id": "...", "role": "...", "added_at": "ISO8601" }
  ```

- **错误**（按校验顺序）:

  | 条件 | status | detail |
  |---|---|---|
  | group 不存在（dependency 中） | 404 | `组不存在` |
  | role 为 None / rank 不足 | 403 | `无权访问此组` / `需要 owner 及以上权限（当前 {role}）` |
  | `db.get(User, body.user_id)` 为 None | 404 | `用户不存在` |
  | `db.get(GroupMember, (body.user_id, group_id))` 已存在 | 409 | `该用户已经是成员` |
  | role 非法（Pydantic pattern 不过） | 422 | FastAPI 自动响应（非自定义文案） |

- **DB**:
  - 目标用户：`db.get(User, body.user_id)`
  - 重复检查：`db.get(GroupMember, (body.user_id, group_id))`（复合主键 tuple）
  - INSERT `group_members`（`added_by_user_id = user.id`=操作者）
  - `await db.commit()` 后 `await db.refresh(member)`（`added_at` 为 `server_default=func.now()`，refresh 才能读到）

- **审计**:
  - `action`: `actions.GROUP_MEMBER_ADD` = `"group_member.add"`；`resource_type`: `"group_member"`；**`resource_id`: `group_id`（路径参数，非 user_id）**
  - `metadata`: `{"target_user_id": body.user_id, "role": body.role}`

- **怪癖**: `resource_id` 是 group_id 而非 user_id（与 project_member 路由一致的设计）

---

### PATCH /groups/{group_id}/members/{user_id}

- **守卫**: `get_current_user` + `require_infra_healthy`；`Depends(require_group_role("owner"))`；路由参数 `user: User = Depends(get_current_user)`

- **请求**:
  - Path: `group_id: string`, `user_id: integer`（FastAPI 自动转 int）
  - Body（`MemberRoleUpdateRequest`）：`role: string`，`Field(..., pattern=r"^(reporter|maintainer|owner)$")`

- **成功**: `200 OK`，`MemberResponse`

- **错误**（按校验顺序）:

  | 条件 | status | detail |
  |---|---|---|
  | group 不存在（dependency 中） | 404 | `组不存在` |
  | 操作者 role 为 None / rank 不足 | 403 | `无权访问此组` / `需要 owner 及以上权限（当前 {role}）` |
  | `db.get(GroupMember, (user_id, group_id))` 为 None | 404 | `成员不存在` |
  | `member.role=="owner"` 且 `body.role!="owner"` 且 owner 数量 ≤ 1 | 422 | `组必须至少 1 个 owner` |

- **DB**:
  - 成员：`db.get(GroupMember, (user_id, group_id))`（复合主键）
  - owner 数量（条件触发）：`_count_group_owners` = `db.scalar(select(func.count(GroupMember.user_id)).filter_by(group_id=group_id, role="owner"))`，`count or 0`
  - 直接赋值 `member.role = body.role`
  - `await db.commit()` 后 `await db.refresh(member)`

- **审计**:
  - `action`: `actions.GROUP_MEMBER_ROLE_CHANGE` = `"group_member.role_change"`；`resource_type`: `"group_member"`；`resource_id`: `group_id`
  - `metadata`: `{"target_user_id": user_id, "old_role": old_role, "new_role": body.role}`（`user_id` 取路径参数）

- **怪癖**:
  - `new_role == "owner"`（平级/升级）→ 不查 owner 数量
  - 仅 `member.role=="owner"` 且 `body.role!="owner"` 才触发 owner 数量检查
  - `count or 0` 防御空 scalar
  - **无「不能改自己」保护**

---

### DELETE /groups/{group_id}/members/{user_id}

- **守卫**: `get_current_user` + `require_infra_healthy`；`Depends(require_group_role("owner"))`；路由参数 `user: User = Depends(get_current_user)`

- **请求**: Path `group_id: string`, `user_id: integer`

- **成功**: `204 No Content`（无响应体）

- **错误**（按校验顺序）:

  | 条件 | status | detail |
  |---|---|---|
  | group 不存在（dependency 中） | 404 | `组不存在` |
  | 操作者 role 为 None / rank 不足 | 403 | `无权访问此组` / `需要 owner 及以上权限（当前 {role}）` |
  | 成员不存在 | 404 | `成员不存在` |
  | `member.role=="owner"` 且 owner 数量 ≤ 1 | 422 | `组必须至少 1 个 owner` |

- **DB**:
  - 成员：`db.get(GroupMember, (user_id, group_id))`
  - owner 数量（条件触发，仅 `member.role=="owner"`）：`_count_group_owners`
  - 审计写入（在 `db.delete` 之前）→ `await db.delete(member)` → `await db.commit()`

- **审计**:
  - `action`: `actions.GROUP_MEMBER_REMOVE` = `"group_member.remove"`；`resource_type`: `"group_member"`；`resource_id`: `group_id`
  - `metadata`: `{"target_user_id": user_id, "role": deleted_role}`（删前 `deleted_role = member.role`）

- **怪癖**: 仅 owner 成员触发数量检查；非 owner 直接删；硬删；**无「不能删自己」保护**（owner 可删自己，只要非唯一 owner）

---

### GET /groups/{group_id}/audit-logs

源码：`audit_router.py` 第 344-485 行（注意：本路由在 `audit_router` 而非 group_router；`audit_router` 自身 `dependencies=[Depends(require_infra_healthy)]`）

- **守卫**:
  1. `get_current_user` + `require_infra_healthy`（Router 级）
  2. `_role: str = Depends(require_group_role("owner"))`（以参数注入而非 `dependencies=[]`；因 checker 需同名 `group_id` 路径参数被 FastAPI 匹配注入）

- **请求**:
  - Path: `group_id: string`
  - Query：

  | 参数 | 类型 | 必选 | 默认 | 约束 |
  |---|---|---|---|---|
  | `page` | integer | 否 | `1` | `Query(1, ge=1)`；`_validate_page_limit` 二次 clamp `max(1, page)` |
  | `limit` | integer | 否 | `50` | `Query(50, ge=1, le=200)`；二次 clamp `min(max(1, limit), 200)` |

- **成功**: `200 OK`，`AuditLogResponse`
  ```json
  {
    "entries": [
      {
        "id": 1,
        "actor_user_id": 1,
        "actor_username": "string|null",
        "action": "group.create",
        "resource_type": "group",
        "resource_id": "retail-bank",
        "metadata": {},
        "ip_address": "string|null",
        "created_at": "ISO8601"
      }
    ],
    "total": 42,
    "page": 1,
    "limit": 50
  }
  ```

- **错误**:

  | 条件 | status | detail |
  |---|---|---|
  | group 不存在 | 404 | `组不存在` |
  | role 为 None / rank 不足（非 owner） | 403 | `无权访问此组` / `需要 owner 及以上权限（当前 {role}）` |

- **DB**:
  1. `_expand_group_and_descendants(group_id, db)`：BFS 向下展开子孙 group id（**含自身**，初始 `{group_id}`），`for _ in range(3)`，`select(Group.id).filter(Group.parent_group_id.in_(frontier))`，`new = children - all_ids` 无新节点 break
  2. `select(Project.id).filter(Project.group_id.in_(group_ids))` → `project_ids` list
  3. OR 过滤（`or_(*scope_conditions)`，每条 `and_(resource_type==..., resource_id.in_(...))`）：
     - `(resource_type='group' AND resource_id IN <gid_list>)`
     - `(resource_type='group_member' AND resource_id IN <gid_list>)`
     - 若 `project_ids` 非空：`(resource_type='project' AND resource_id IN <project_ids>)`
     - 若 `project_ids` 非空：`(resource_type='project_member' AND resource_id IN <project_ids>)`
  4. `LEFT JOIN(outerjoin) users ON audit_logs.actor_user_id = users.id`，取 `User.username.label("actor_username")`
  5. `total = await db.scalar(select(func.count()).select_from(subquery)) or 0`
  6. 分页：`.order_by(AuditLog.created_at.desc()).offset((page-1)*limit).limit(limit)`，`.fetchall()`

- **审计**: 只读，不写

- **怪癖**:
  - `metadata`：`json.loads(row.metadata_json or "{}")`；`except (json.JSONDecodeError, TypeError)` → `{}`，不抛错
  - `created_at` 为 None → `""`（`row.created_at.isoformat() if row.created_at else ""`）
  - `actor_username`：`getattr(row, "actor_username", None)`
  - Query 层 `ge=1, le=200` 越界先返回 422；`_validate_page_limit` clamp 是函数内二次兜底（实际请求路径下 FastAPI 先拦）

---

## 依赖的共享设施

### `get_current_user`（`auth_dependencies.py`）
- `OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)`，`token` 为 `Optional`
- 无 token → 401。`decode_token(token)`；`payload` 假值或 `payload.get("type") != "access"` → 401
- `payload.get("sub")` 假值 → 401；`int(sub)` `ValueError` → 401
- `select(User).where(User.id==user_id)` `.scalar_one_or_none()`；`None` 或 `not user.is_active` → 401
- **所有失败统一 401，detail `"Not authenticated"`，header `WWW-Authenticate: Bearer`**

### `require_group_role(min_role="reporter")`（`permission_deps.py`）
工厂返回 `async checker(group_id, user=Depends(get_current_user), db=Depends(get_db)) -> str`：
1. `db.get(Group, group_id)` 不存在 → 404 `组不存在`
2. `user.is_admin` → 直接返回 `"owner"`（**在存在性检查之后**，不存在的 group 即便 admin 也先 404）
3. `resolve_group_role(user.id, group_id, db)`
4. `None` → 403 `无权访问此组`
5. `ROLE_RANK[role] < ROLE_RANK[min_role]` → 403 `需要 {min_role} 及以上权限（当前 {role}）`
6. 返回 role（string）
- 启动期：`min_role not in ROLE_RANK` → `ValueError`（路由注册阶段爆，错误文案含 `require_group_role: min_role=... 不合法，合法值：[...]`）

### `resolve_group_role(user_id, group_id, db)`（`permission_deps.py`）
- 从 `group_id` 沿 `parent_group_id` 向上遍历继承链
- 每层 `db.scalar(select(GroupMember.role).filter_by(user_id=?, group_id=cur_gid))`
- `_pick_higher` 取最高 role（`ROLE_RANK[a] >= ROLE_RANK[b]` 时取 a）
- `while cur_gid and cur_gid not in visited`，`if depth >= 3: break`（depth 0/1/2，**最多 3 层**），`visited` set 防循环
- **不处理 is_admin**（由 `require_group_role` 在调用前处理）
- 返回 `Optional[str]`，None=无成员关系

### `_expand_user_groups(user_id, db)`（`permission_deps.py`）
- 第 0 层：`select(GroupMember.group_id).filter_by(user_id=user_id)`；空 → 返回 `set()`
- BFS 向下 `for _ in range(3)`：`select(Group.id).filter(Group.parent_group_id.in_(frontier))`，`new = children - all_accessible`，无新节点 break
- 用于 `GET /groups` 可见性过滤（直接层 + 最多 3 层子孙）

### `_expand_group_and_descendants(group_id, db)`（`audit_router.py`）
- 初始 `{group_id}`（**含自身**），BFS 向下 `for _ in range(3)`，逻辑同上但不做成员关系过滤
- 用于 `GET /groups/{gid}/audit-logs`

### `log_audit(db, *, actor_user_id, action, resource_type, resource_id, metadata=None, ip_address=None)`（`audit/logger.py`）
- 关键字-only 参数（`*` 后）
- **不主动 commit**，由 caller 与业务数据原子提交
- **不抛错**：`try/except Exception` 仅 `logger.warning(...)`，业务继续
- `resource_id` 强制 `str(resource_id)`（兼容 int）
- `metadata_json = json.dumps(metadata or {}, ensure_ascii=False)`（None → `"{}"`，中文不转义）

### `require_infra_healthy(request, user=Depends(get_current_user))`（`deps_infra.py`）
- Router 级 `dependencies=[]`，**自身依赖 `get_current_user`**（故 401 在 503 之前）
- `request.app.state.infra_status` 为 None → 503 `{"code":"INFRA_UNINITIALIZED","message":"系统正在初始化，请稍后重试"}`
- 列出 `ok=False` 且不在 `_NON_CRITICAL_DEPS = {"neo4j"}` 的依赖；有不健康 → 503 `{"code":"INFRA_UNHEALTHY","message":"系统暂时不可用，请联系管理员"}`；`user.is_admin` 时 detail 追加 `"deps": <infra_status>`

### 审计 action 常量（`audit/actions.py`）— 逐字核对无误

| Python 常量 | 字符串值 |
|---|---|
| `GROUP_CREATE` | `"group.create"` |
| `GROUP_UPDATE` | `"group.update"` |
| `GROUP_DELETE` | `"group.delete"` |
| `GROUP_MEMBER_ADD` | `"group_member.add"` |
| `GROUP_MEMBER_REMOVE` | `"group_member.remove"` |
| `GROUP_MEMBER_ROLE_CHANGE` | `"group_member.role_change"` |

### DB schema 对照（`db_models_groups.py`）
- `groups`: `id String(64) PK`、`name String(128) NN`、`description String(512) NULL`、`parent_group_id String(64) FK→groups.id ON DELETE RESTRICT NULL`、`created_by_user_id Integer FK→users.id ON DELETE SET NULL NULL`、`created_at DateTime server_default=now() NN`；Index `ix_groups_parent`
- `group_members`: 复合主键 `(user_id Integer FK→users.id ON DELETE CASCADE, group_id String(64) FK→groups.id ON DELETE CASCADE)`、`role String(16) NN` + CheckConstraint `ck_group_members_role` (`role IN ('reporter','maintainer','owner')`)、`added_by_user_id Integer FK→users.id ON DELETE SET NULL NULL`、`added_at DateTime server_default=now() NN`；ORM `Group.members` `cascade="all, delete-orphan"`
- `audit_logs`: `id Integer PK autoincrement`、`actor_user_id Integer FK→users.id ON DELETE SET NULL NULL`、`action String(64) NN`、`resource_type String(32) NN`、`resource_id String(128) NN`、`metadata_json Text NULL`、`ip_address String(45) NULL`、`created_at DateTime server_default=now() NN`；Index `ix_audit_actor_time(actor_user_id, created_at)`、`ix_audit_resource(resource_type, resource_id)`

---

# 【credentials 族（用户私有）】

Everything is cross-confirmed. I have all the details needed. Note one subtle point worth verifying: the spec claims GET uses `select(GitCredential).filter_by(...)` with `.all()` — the source actually uses `db.scalars(...)` not `db.execute(...)`. Also the spec's "怪癖" about `token_hint` line 146 and `nullable=True` (String(16)) is confirmed. Let me also double-check `token_hint` column length (String(16)) since `"****" + plain[-4:]` = 8 chars, fits.

Now I have enough to produce the corrected spec. Let me compile findings.

Key discrepancies found:
1. GET query uses `db.scalars(select(...).filter_by(...))` not `db.execute`. Spec says "结果调 `.all()` 转 list" via `select(GitCredential).filter_by(...)` — the SQLAlchemy form omits `.scalars()`. Minor but the spec wrote `select(GitCredential).filter_by(owner_user_id=user.id)` and `.all()` which loses the `scalars()` step. I'll correct.
2. `token_hint` column is `String(16)`, `nullable=True` — spec said "line 146 列定义 `nullable=True`" which is correct, but didn't mention `String(16)`. Minor add.
3. Everything else (status codes, detail text, field constraints, audit actions, FK SET NULL, ip handling) matches.

Let me also confirm the 503 INFRA_UNHEALTHY admin "deps" value: spec says `<app.state.infra_status>` — source assigns `detail["deps"] = state` where `state = app.state.infra_status`. Correct.

One more: spec claims for GET "401 ... WWW-Authenticate: Bearer" header — confirmed. And the spec's claim that `require_infra_healthy` runs "先于" get_current_user but internally depends on it — FastAPI dedup confirmed conceptually. The infra check order: in source, `require_infra_healthy` itself `Depends(get_current_user)`, so 401 (bad token) fires before 503 evaluation since `get_current_user` resolves first as a sub-dependency. The spec's framing is acceptable.

## 修正清单

逐条回源码核对后，本规范**整体准确度很高**，状态码、detail 文案、pydantic 边界、审计 action、FK 级联均逐字/逐项正确。发现的问题以**轻微**为主，集中在 SQLAlchemy 查询写法和少量补充：

1. **【修正·GET DB 查询写法】** 规范写「SQLAlchemy 形式：`select(GitCredential).filter_by(owner_user_id=user.id)`，结果调 `.all()` 转 list」——漏掉了 `db.scalars(...)` 这一步。源码 line 100-102 实际为 `(await db.scalars(select(GitCredential).filter_by(owner_user_id=user.id))).all()`，用的是 **`db.scalars`**（返回 ORM 实例迭代器）而非 `db.execute`（返回 Row 元组）。TS 侧若用 drizzle，等价于 `select().from(gitCredentials).where(eq(ownerUserId, user.id))` 直接拿行对象，但务必知晓 Python 侧是 `scalars`，不是 `execute`/Row 解包。已修正。

2. **【补充·token_hint 列定义】** 规范引用了 `db_models_homepage.py` line 146 `nullable=True` 正确，但未记列类型。实际为 `mapped_column(String(16), nullable=True)`。`"****"+plain[-4:]` 最长 8 字符，`String(16)` 容得下。已补。

3. **【补充·503 与 401 的相对顺序】** 规范在 GET 节说「`require_infra_healthy`（含内部 `get_current_user`）→ 路由处理器自己的 `get_current_user`」，方向对但可更精确：因为 `require_infra_healthy` 自身 `Depends(get_current_user)`，**无效/缺失 token 会在 503 判定之前就抛 401**（get_current_user 作为子依赖先解析）。即：坏 token + infra 挂 → 返回 **401**，不是 503。已在各路由「守卫」节补明。

4. **【核验通过项（抽查）】**
   - 路径参数名 `cred_id`（非 `credential_id`）—— OpenAPI 存档与源码 line 163-165 一致，admin 路由才用 `credential_id`，规范未混淆。✅
   - 状态码：GET 200 / POST 201 / DELETE 204 / 404 / 403 / 401 / 503 —— 逐一比对 OpenAPI + 源码，全对。✅
   - detail 文案逐字：`"凭证不存在"`、`"不是该凭证的所有者"`、`"Not authenticated"`、`"系统正在初始化，请稍后重试"`、`"系统暂时不可用，请联系管理员"`、`INFRA_UNINITIALIZED`/`INFRA_UNHEALTHY` —— 全部逐字命中。✅
   - pydantic 边界：name `min_length=1/max_length=128`、token `min_length=8/max_length=512`、type `max_length=32` 无 min + 默认 `"pat"` —— 源码 line 54-56 与 OpenAPI schema 双向确认。✅
   - 审计 action：`CREDENTIAL_CREATE="credential.create"`、`CREDENTIAL_DELETE="credential.delete"` —— actions.py line 100/103 确认。✅
   - 审计字段 `metadata_json` 用 `json.dumps(metadata or {}, ensure_ascii=False)`、`resource_id` 强制 `str()`、`actor_user_id`/`resource_type`/`ip_address`、`created_at` 走 `server_default` —— logger.py 全对。✅
   - `id="cred_"+uuid.uuid4().hex[:12]`（共 17 字符）、`token_hint`=`"****"+plain[-4:]`、`encrypt_token` Fernet —— token_crypto.py + router 确认。✅
   - DELETE 内部检查顺序（查→所有权→delete→log_audit→commit）、硬删、`!=` 比较、owner=NULL 历史数据 403 —— 源码 line 183-207 + 注释确认。✅
   - `projects.git_credential_id` FK `ondelete="SET NULL"` —— db_models_homepage.py line 90 确认。✅
   - POST/DELETE 审计与业务**同一事务原子 commit**、审计失败仅 warning 不抛 —— logger.py + router 确认。✅
   - GET 无 order_by / 无分页 —— 源码确认。✅ 无审计 —— 确认。✅

---

# credentials 族行为规范（Python → TypeScript 移植权威 · 已核验修正版）

源码版本：`/Users/java/knowledge-engineering/src/service/credentials_router.py`
OpenAPI 交叉核对：`/Users/java/knowledge-engineering/docs/porting/routes-openapi.json`
DB 模型核对：`/Users/java/knowledge-engineering/src/service/db_models_homepage.py`（GitCredential）、`db_models_groups.py`（AuditLog）

路由组挂载：`APIRouter(prefix="/credentials", tags=["credentials"], dependencies=[Depends(require_infra_healthy)])`。`require_infra_healthy` 是**路由组级**依赖，三条路由共用。

---

### GET /credentials

- **守卫**:
  1. `require_infra_healthy`（路由组级）— 其本身 `Depends(get_current_user)`。
  2. `get_current_user`（处理器参数级 `Depends`）— 从 `Authorization: Bearer <token>` 解析 JWT；FastAPI 对同一 request 的同一依赖去重，实际只执行一次。
  - **相对顺序（重要）**：`get_current_user` 作为 `require_infra_healthy` 的**子依赖先被解析**。所以无效/缺失 token → 先抛 **401**（即便 infra 也挂着，也是 401 而非 503）。token 有效后，再判 infra：infra 未初始化或 critical 依赖挂 → 503。
  - 无任何项目/组角色检查，任何已登录活跃用户可调用。

- **请求**:
  - Path：无
  - Query：无
  - Body：无

- **成功**: `200 OK`，**数组**（响应 `response_model=list[CredentialResponse]`）
  ```json
  [
    {
      "id": "cred_<12位hex>",
      "name": "string",
      "type": "string",
      "token_hint": "string | null",
      "created_at": "ISO8601字符串（无时区，datetime.isoformat()）"
    }
  ]
  ```
  - 无凭证 → 空数组 `[]`。
  - 5 个字段在 OpenAPI 中**均 required**（含 `token_hint`，但其值可为 `null`：schema `anyOf [string, null]`）。
  - `token_hint` 类型 `Optional[str]`；DB 列 `String(16), nullable=True`（`db_models_homepage.py` line 146），历史数据可能为 `null` → TS 侧必须允许 `null`。
  - 绝不含 `encrypted_token`、不含 token 原文。

- **错误**:
  | Status | detail 逐字 |
  |--------|------------|
  | 401 | `"Not authenticated"`（响应头 `WWW-Authenticate: Bearer`） |
  | 503（infra 未初始化） | `{"code": "INFRA_UNINITIALIZED", "message": "系统正在初始化，请稍后重试"}` |
  | 503（infra unhealthy，普通用户） | `{"code": "INFRA_UNHEALTHY", "message": "系统暂时不可用，请联系管理员"}` |
  | 503（infra unhealthy，admin） | 上同 + `"deps": <app.state.infra_status>`（`detail["deps"] = state`，直接引用整个 infra_status dict） |

- **DB**:
  - 表：`git_credentials`
  - 查询（源码 line 100-102）：`(await db.scalars(select(GitCredential).filter_by(owner_user_id=user.id))).all()` —— 用 **`db.scalars`**（返回 ORM 实例），等价 `WHERE owner_user_id = <user.id>`，`.all()` 转 list。
  - **无 `order_by`**（返回顺序由 DB 决定）。
  - **无 limit/offset**（全量返回）。
  - 只读，无 commit。

- **审计**: 不写。

- **怪癖**:
  - `owner_user_id` DB 层 `nullable=True`（迁移期允许 NULL），但 `filter_by(owner_user_id=user.id)` 精确匹配，故历史无 owner 的凭证对普通用户**不可见**。
  - `created_at` = `cred.created_at.isoformat()`，如 `"2026-05-12T08:00:00"`，无时区、无毫秒（除非 DB 存了小数秒）。

---

### POST /credentials

- **守卫**:
  1. `require_infra_healthy`（路由组级，子依赖含 `get_current_user`）
  2. `get_current_user`
  - 顺序与 401-先于-503 规则同 GET。无项目/组权限检查，任何已登录活跃用户可创建。

- **请求**:
  - Path：无；Query：无
  - Body（JSON，`requestBody.required=true`）：

  | 字段 | 类型 | 必选 | 默认 | 约束 |
  |------|------|------|------|------|
  | `name` | string | 是 | — | `min_length=1`, `max_length=128` |
  | `token` | string | 是 | — | `min_length=8`, `max_length=512` |
  | `type` | string | 否 | `"pat"` | `max_length=32`（**无 min_length**） |

  违反 → FastAPI `422 Unprocessable Entity`（Pydantic ValidationError）。

- **成功**: `201 Created`（`status_code=status.HTTP_201_CREATED`）
  ```json
  {
    "id": "cred_<12位hex>",
    "name": "string",
    "type": "string",
    "token_hint": "string",
    "created_at": "ISO8601字符串"
  }
  ```
  - `id` = `"cred_" + uuid.uuid4().hex[:12]`（前缀 `cred_` + UUID4 hex 前 12 位，共 17 字符）。
  - `token_hint` = `"****" + plain[-4:]`（`len(plain) < 4` 或空 → `"****"`；因 `min_length=8`，实际 hint 必为 `"****"+末4位`）。

- **错误**:
  | Status | detail 逐字 |
  |--------|------------|
  | 401 | `"Not authenticated"` |
  | 422 | Pydantic 标准 ValidationError |
  | 503 | 同 GET |

  无重复名称检查（源码 line 130-160），同一用户可建多个同名凭证，不报错。

- **DB**:
  - 表：`git_credentials`（写）+ `audit_logs`（写）
  - 操作序列：`db.add(GitCredential(...))` → `await log_audit(...)`（内部 `db.add(AuditLog(...))`，不 commit）→ `await db.commit()`（**两者同一事务原子提交**）
  - 写入字段：
    | DB 列 | 来源 |
    |------|------|
    | `id` | `"cred_" + uuid.uuid4().hex[:12]` |
    | `name` | `body.name` |
    | `type` | `body.type`（默认 `"pat"`） |
    | `encrypted_token` | `encrypt_token(body.token)`（Fernet 密文，列 `Text, nullable=False`） |
    | `token_hint` | `token_hint(body.token)` |
    | `owner_user_id` | `user.id`（int） |
    | `created_by` | `user.username`（字符串） |
    | `created_at` | DB `server_default=func.now()` 自动填 |
  - `last_used_at` 不写（初始 NULL）。

- **审计**: 是
  - `action`：`"credential.create"`（`actions.CREDENTIAL_CREATE`）
  - `actor_user_id`：`user.id`（int）
  - `resource_type`：`"credential"`
  - `resource_id`：`cred.id`（logger 内强制 `str()`）
  - `metadata_json`：`json.dumps({"name": cred.name, "type": cred.type}, ensure_ascii=False)`
  - `ip_address`：`request.client.host if request.client else None`
  - 失败仅 `logger.warning`，不抛、不中断业务。

- **怪癖**:
  - Fernet 对称加密（AES-128-CBC + HMAC-SHA256），密钥来自 env `KE_TOKEN_ENC_KEY`（44 字节 base64），不入库。
  - 无唯一性约束，同名可重复创建。
  - `type` 有默认 `"pat"` 但**无枚举校验**，任何 ≤32 字符字符串均可入库。

---

### DELETE /credentials/{cred_id}

> 路径参数名是 **`cred_id`**（OpenAPI + 源码确认）。注意 admin 路由 `DELETE /admin/credentials/{credential_id}` 用的是 `credential_id`，勿混。

- **守卫**:
  1. `require_infra_healthy`（路由组级，子依赖含 `get_current_user`）
  2. `get_current_user`
  - 401-先于-503 规则同 GET。无项目/组权限检查，所有权由路由内部手动校验。

- **请求**:
  - Path：`cred_id`（string，必选，无长度约束）
  - Query：无；Body：无

- **成功**: `204 No Content`（`status_code=status.HTTP_204_NO_CONTENT`，响应体空）

- **错误**:
  | Status | detail 逐字 | 触发条件 |
  |--------|------------|---------|
  | 401 | `"Not authenticated"` | token 缺失/无效/用户不存在/inactive |
  | 404 | `"凭证不存在"` | `db.get(GitCredential, cred_id)` 返回 `None` |
  | 403 | `"不是该凭证的所有者"` | `cred.owner_user_id != user.id` |
  | 422 | Pydantic 标准 ValidationError | path 参数校验（string 几乎不触发） |
  | 503 | 同 GET | infra 未初始化 / unhealthy |

- **检查顺序（路由内部，源码 line 183-207）**:
  1. `cred = await db.get(GitCredential, cred_id)`（按主键单行）— `None` → 404 `"凭证不存在"`
  2. `cred.owner_user_id != user.id` → 403 `"不是该凭证的所有者"`
  3. `await db.delete(cred)`（标记删除）
  4. `await log_audit(...)`（**先于 commit** 写审计）
  5. `await db.commit()`（删除 + 审计**同一事务原子提交**）

- **DB**:
  - 表：`git_credentials`（**物理删除**）+ `audit_logs`（写）
  - 查询：`db.get(GitCredential, cred_id)`（主键）
  - 删除：`await db.delete(cred)` — **硬删，无软删**
  - 事务：删除 + 审计原子提交。

- **审计**: 是
  - `action`：`"credential.delete"`（`actions.CREDENTIAL_DELETE`）
  - `actor_user_id`：`user.id`（int）
  - `resource_type`：`"credential"`
  - `resource_id`：`cred.id`（删除前已取得，logger 内 `str()`）
  - `metadata_json`：`json.dumps({"name": cred.name}, ensure_ascii=False)`
  - `ip_address`：`request.client.host if request.client else None`
  - 失败仅 warning，不中断。

- **怪癖**:
  - 硬删除。`projects.git_credential_id` 列对 `git_credentials.id` 有 FK `ondelete="SET NULL"`（`db_models_homepage.py` line 90），删凭证后关联工程的 `git_credential_id` **由数据库级联置 NULL**（与本路由代码无关，但 TS 侧建表/迁移需保留此 FK 行为）。
  - 所有权用 `!=`（int 比较），不用 `is not`（源码注释明示：小整数缓存不可依赖 `is`）。
  - `cred.owner_user_id` 为 NULL（迁移期老数据）时，`None != user.id` 求值 `True` → 触发 403。即 owner=NULL 的历史凭证普通用户**无法删除**，须走 admin 路由 `/admin/credentials/{credential_id}`。

---

## 依赖的共享设施

### `get_current_user`
- 文件：`/Users/java/knowledge-engineering/src/service/auth_dependencies.py`
- `oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)` —— `auto_error=False`，不自动抛 401，由函数体手动 `if not token: raise`。
- 流程：取 token → `if not token` 失败 → `decode_token(token)` → 校验 `payload and payload.get("type") == "access"` → `payload.get("sub")` 存在 → `int(sub)`（ValueError 也算失败）→ `select(User).where(User.id == user_id)` 取 `scalar_one_or_none()` → 校验 `user is not None and user.is_active`。
- 任一步失败一律抛同一异常：`HTTPException(401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"})`。

### `require_infra_healthy`
- 文件：`/Users/java/knowledge-engineering/src/service/deps_infra.py`
- 签名：`async def require_infra_healthy(request, user=Depends(get_current_user))` —— **内含 `get_current_user` 子依赖**，故 401 在 503 之前。
- 读 `state = getattr(request.app.state, "infra_status", None)`：
  - `state is None` → 503 `{"code": "INFRA_UNINITIALIZED", "message": "系统正在初始化，请稍后重试"}`
  - 计算 `unhealthy = [k for k,v in state.items() if k not in _NON_CRITICAL_DEPS and not v.get("ok")]`，其中 `_NON_CRITICAL_DEPS = {"neo4j"}`（neo4j 退役中，挂了也不 503）。
  - `unhealthy` 为空 → 放行（return）。
  - 非空 → `detail = {"code":"INFRA_UNHEALTHY","message":"系统暂时不可用，请联系管理员"}`；若 `user.is_admin` → 追加 `detail["deps"] = state`（整个 infra_status dict）；抛 503。

### `log_audit`
- 文件：`/Users/java/knowledge-engineering/src/service/audit/logger.py`
- 签名：`log_audit(db, *, actor_user_id: int, action: str, resource_type: str, resource_id: str, metadata: Optional[dict]=None, ip_address: Optional[str]=None) -> None`（`*` 后全 keyword-only）。
- 向当前 `AsyncSession` `db.add(AuditLog(...))`，**不 commit**（caller 负责与业务一起原子提交）。
- 失败策略：`try/except Exception` 仅 `logger.warning(...)`，**不 raise**。
- 写入 `audit_logs` 列：`actor_user_id`、`action`、`resource_type`、`resource_id`（`str(resource_id)`）、`metadata_json`（`json.dumps(metadata or {}, ensure_ascii=False)`，None → `"{}"`）、`ip_address`。`created_at` 由 DB `server_default=func.now()` 填。
- AuditLog 列约束（`db_models_groups.py`）：`action String(64)`、`resource_type String(32)`、`resource_id String(128)`、`metadata_json Text nullable`、`ip_address String(45) nullable`、`actor_user_id` nullable（FK 用户删除时 SET NULL）。

### `actions` 常量（credential 族）
- 文件：`/Users/java/knowledge-engineering/src/service/audit/actions.py`
- `CREDENTIAL_CREATE = "credential.create"`
- `CREDENTIAL_DELETE = "credential.delete"`

### `encrypt_token` / `token_hint`
- 文件：`/Users/java/knowledge-engineering/src/service/token_crypto.py`
- `encrypt_token(plain) -> str`：`if not plain: raise ValueError("token 不能为空")`；否则 Fernet 加密，密钥单例（`@lru_cache`）来自 env `KE_TOKEN_ENC_KEY`（缺失 → RuntimeError）；返回 base64 密文。
- `token_hint(plain) -> str`：`if not plain: return "****"`；`if len(plain) < 4: return "****"`；否则 `f"****{plain[-4:]}"`。
- （`decrypt_token` 本族未用：密文损坏/密钥不匹配统一抛 `ValueError("密文损坏或密钥不匹配")`。）

---

# 【admin 族】

All cross-checks confirmed. The drizzle schema matches: `git_credentials.token_hint` varchar(16), `owner_user_id`, `metadata_json` text, `resource_type` varchar(32), `git_credential_id` with `onDelete: "set null"`.

I now have everything needed. Here is the corrected, complete specification.

---

## 修正清单

1. **DELETE /admin/projects — 级联表名错记（事实错误）**。规范写"级联删：`sessions`、`messages`、`feedback`"。回源码核对：项目内对 `projects.id` 设了 `ON DELETE CASCADE` 的表**只有两张** —— `qa_sessions` 和 `user_project_access`（`db_models_homepage.py` 第 184、207 行）。`messages` / `feedback` **根本不存在为 DB 表**（`db_models_homepage.py` 顶部 docstring 明确：qa_messages / qa_feedback 已于 S6/S7 删除，消息与反馈改为文件式存储）。已改为：级联删 `qa_sessions`、`user_project_access`；文件式存储的消息/反馈不在 DB 层 CASCADE。

2. **POST /admin/projects test-connection 等的 `credential_id` 触发条件用词不准（真值 vs 非 null）**。规范多处写"`credential_id` 非 null 时查询/校验"。源码用的是 **Python 真值判断** `if body.credential_id:`（第 196、250 行），空字符串 `""` 同样跳过查询。已统一改为"`credential_id` 为真值（非 null 且非空字符串）时"。这对 TS 移植是 load-bearing 区别（TS 里要写 `if (body.credential_id)` 而非 `!= null`）。

3. **`require_admin` 在 dependency 链中的"顺序"措辞收紧**。规范把守卫顺序描述为"401 JWT → 503 infra → 403 非 admin"，暗示是确定性串行顺序。FastAPI 对**路由级 dependency**（`require_admin`）与**router 级 dependency**（`require_infra_healthy`）的求解顺序并无文档化的强保证；二者都依赖被缓存的 `get_current_user`。实务上 router 级 dep 通常先于路由签名 dep 解析，但移植时应以"401 一定最先（无 user 无法判断 infra/admin），403 与 503 的相对先后不要当成契约"为准。已在公共守卫节注明这点。

4. **凭证解密错误文案：澄清不是来自 `decrypt_token`**。`token_crypto.decrypt_token` 抛的 `ValueError` 文案是 `"密文损坏或密钥不匹配"`（`token_crypto.py` 第 72 行），但 test-connection 路由**捕获后返回自己的字符串** `"凭证解密失败（密钥可能轮换或密文损坏）"`（第 204 行）。规范文案本身正确，已补注"此文案由路由硬编码，非异常原文"，避免移植时误用底层异常文案。

5. **`git_credentials` ORM 无 `*` 全列别名**。规范把 GET /admin/credentials 的查询写成 `SELECT * FROM git_credentials ORDER BY created_at DESC`。源码是 `select(GitCredential).order_by(GitCredential.created_at.desc())`（ORM 形式，等价 SELECT 全列）。语义等价，已保留但改为标注"ORM 全列 select，非字面 `*`"，并补上**响应映射顺序**（`_credential_to_pydantic` 字段顺序）。

6. **补充 OpenAPI 交叉核对结论**。已逐一对 `routes-openapi.json` 核对 6 个 schema 的 required/默认/边界，全部与 `admin_models.py` 一致（含 `AdminProjectResponse.git_url` 为 nullable、`git_branch` 非 null 等）。无矛盾。

7. **明确本族 7 条路由的范围正确**。OpenAPI 存档另含 `GET /admin/audit-logs`（在 `audit_router.py`）、`GET/POST/PATCH/DELETE /admin/users/*`（在 `user_router.py`），它们**不属于** `admin_router.py`，规范未收录是**正确**的。已加一行说明，防止下游误以为漏路由。

**抽查并核验通过（无修正）的点**：所有 detail 文案逐字（`"凭证不存在"`/`"工程不存在"`/`"工程 ID 已存在: {body.id}"`/`"仅管理员可访问"`/`"Not authenticated"`）；状态码（204/201/200/409/404/403/401/503）；pydantic 边界（`git_url` min4/max512、`name` min1/max128、`domain` max64、`id` pattern `^[a-z][a-z0-9-]{0,62}[a-z0-9]$`、`git_branch` 默认 `"main"`/min1/max128、`language` 默认 `"java"`）；审计仅 `DELETE /admin/credentials` 写、`action="credential.delete"`、metadata 三字段、`log_audit` 不自 commit；POST/PATCH/DELETE projects **均不写审计**；409 先于 404 的顺序；`status="configured"` 硬编码；`created_by=user.username`；`_iso()` 行为；PATCH 空串解绑语义；`branch` 字段在路由体未传给 `ls_remote`（已核 `ls_remote` 签名仅收 `git_url`+`token`）。

---

# Admin 族路由行为规范（修正版）

> 源码：`/Users/java/knowledge-engineering/src/service/admin_router.py`
> Pydantic schemas：`/Users/java/knowledge-engineering/src/service/admin_models.py`
> 守卫依赖：`/Users/java/knowledge-engineering/src/service/auth_dependencies.py`、`deps_infra.py`
> 审计：`/Users/java/knowledge-engineering/src/service/audit/logger.py` + `actions.py`
> ORM：`/Users/java/knowledge-engineering/src/service/db_models_homepage.py`（projects / git_credentials / qa_sessions / user_project_access）、`db_models_groups.py`（audit_logs）

**本族范围 = `admin_router.py` 内的 7 条路由**。注意 OpenAPI 存档里还有 `GET /admin/audit-logs`（来自 `audit_router.py`）和 `GET/POST/PATCH/DELETE /admin/users/*`（来自 `user_router.py`，前缀 `/admin/users`）——它们**不在本族**，不要在本规范里移植。

---

## 路由器级公共守卫（所有路由共享）

`APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_infra_healthy)])`。每条路由的函数签名里**额外**声明 `Depends(require_admin)`。

**有效守卫集合（两层）**：
1. `require_infra_healthy`（router 级）—— 内部 `Depends(get_current_user)`
2. `require_admin`（路由级）—— 内部 `Depends(get_current_user)`

**`get_current_user`（最先且必然先执行）**：
- 无 Bearer token / `decode_token` 失败 / `payload["type"] != "access"` / `sub` 缺失或非 int / 用户不存在 / `is_active=False` → **401 `"Not authenticated"`** + header `WWW-Authenticate: Bearer`

**`require_infra_healthy`**（读 `request.app.state.infra_status`）：
- `state is None` → **503** `{"code": "INFRA_UNINITIALIZED", "message": "系统正在初始化，请稍后重试"}`
- 存在非 `"neo4j"` 的 critical 依赖 `ok=False`（`_NON_CRITICAL_DEPS = {"neo4j"}`）→ **503** `{"code": "INFRA_UNHEALTHY", "message": "系统暂时不可用，请联系管理员"}`；**且 `user.is_admin` 为 True 时**额外附 `"deps": <完整 infra_status dict>`
- 全部 ok → 返回 None 放行

**`require_admin`**：
```python
async def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    return user
```

**顺序契约（移植须知）**：401（无 user 时无法判断 infra/admin）**必然最先**。403 与 503 之间的相对先后**不是文档化契约**——二者都依赖被 FastAPI 缓存的同一个 `get_current_user`（同 request 内只执行一次）。实务上 router 级 `require_infra_healthy` 一般先于路由签名级 `require_admin` 解析（即 infra 503 先于 admin 403），但 TS 端不要把这当成必须复刻的强保证；只需保证 401 最先、且非 admin 与 infra 不健康都会被拦。

---

### GET /admin/credentials

- **守卫**: `get_current_user`（401）→ `require_infra_healthy`（503）→ `require_admin`（403）。路由签名参数名 `_user`（下划线：仅触发守卫，函数体不用）。
- **请求**: 无 path / query / body。
- **成功**: `200 OK`，`response_model=CredentialListResponse`
  ```json
  {
    "credentials": [
      {
        "id": "string",
        "name": "string",
        "type": "string",
        "token_hint": "string | null",
        "created_by": "string | null",
        "created_at": "string",         // 非空字符串；_iso() 结果，None 兜底为 ""
        "last_used_at": "string | null" // _iso() 结果，可 null
      }
    ]
  }
  ```
  映射函数 `_credential_to_pydantic` 字段顺序：id, name, type, token_hint, created_by, created_at(`_iso(c.created_at) or ""`), last_used_at(`_iso(c.last_used_at)`)。
- **错误**: 无业务错误（空表 → 空数组）。
- **DB**:
  - 表：`git_credentials`
  - 查询：`select(GitCredential).order_by(GitCredential.created_at.desc())`（ORM 全列 select，非字面 `*`；全量，无分页，无过滤；`created_at` 降序）
  - 只读，无 commit
- **审计**: 不写。
- **怪癖**:
  - 响应**永不含** `encrypted_token`，仅 `token_hint`（DB 列 `git_credentials.token_hint` varchar(16)，存储时已截取末位）
  - `created_at` DB 为 None 时 → `""`；`last_used_at` DB 为 None 时 → `null`

---

### DELETE /admin/credentials/{credential_id}

- **守卫**: 同上，但路由签名参数名为 `user`（非下划线，函数体写审计 actor）。另注入 `request: Request`。
- **请求**: path `credential_id` (string, 必选)。
- **成功**: `204 No Content`（空响应体）。
- **错误**:

  | status | detail（逐字） | 触发 |
  |--------|---------------|------|
  | 404 | `凭证不存在` | `db.get(GitCredential, credential_id) is None` |
- **DB**:
  - 表：`git_credentials`
  - 查询：`db.get(GitCredential, credential_id)`（按主键）
  - 操作：`db.delete(cred)` → `log_audit(...)` → `db.commit()`（删除与审计同一事务原子提交）
  - FK：`projects.git_credential_id` 为 `ON DELETE SET NULL`，关联工程的该列置 NULL，工程不删
- **审计**: **写** `audit_logs`
  - `action`: `actions.CREDENTIAL_DELETE` = `"credential.delete"`
  - `actor_user_id`: `user.id`
  - `resource_type`: `"credential"`
  - `resource_id`: `cred.id`（`log_audit` 内 `str()` 转换）
  - `metadata`（JSON 序列化进 `metadata_json` 列，`ensure_ascii=False`）：
    ```json
    {"name": "<cred.name>", "admin_action": true, "original_owner_id": "<cred.owner_user_id>"}
    ```
    `original_owner_id` 取自 `cred.owner_user_id`（int 或 None，原样进 dict，由 json.dumps 序列化）
  - `ip_address`: `request.client.host if request.client else None`
  - 提交时机：与 `db.delete` 同一 `db.commit()`
- **怪癖**:
  - admin 可删任意 owner 的凭证（不受 `owner_user_id` 限制）
  - `admin_action=True` 区分 admin 强删 vs 用户自删
  - `log_audit` 自身不 commit、失败仅 warning 不抛错——审计写失败不影响 204 删除成功

---

### POST /admin/projects/test-connection

- **守卫**: 同公共守卫，路由签名参数名 `_user`。
- **请求** (JSON body, `TestConnectionRequest`):

  | 字段 | 类型 | 必选 | 默认 | 约束 |
  |------|------|------|------|------|
  | `git_url` | string | 是 | — | min_length=4, max_length=512 |
  | `credential_id` | string\|null | 否 | `null` | 无长度约束 |
  | `branch` | string\|null | 否 | `null` | 无长度约束（schema 有此字段，路由体未用，见怪癖）|
- **成功**: `200 OK`，`response_model=TestConnectionResponse`
  ```json
  { "ok": true, "default_branch": "string | null", "last_commit": "string | null", "error": "string | null" }
  ```
  `ok=false` 也是 200，用 `ok` 区分。`default_branch`/`last_commit`/`error` 默认 None。
- **错误**:

  | status | detail（逐字） | 触发 |
  |--------|---------------|------|
  | 404 | `凭证不存在` | `credential_id` 为真值，但 `db.get(GitCredential, ...)` 返回 None |

  凭证解密失败**不是** HTTP 错误：返回 `200` + `{"ok": false, "error": "凭证解密失败（密钥可能轮换或密文损坏）"}`（此文案由路由硬编码，非底层 `decrypt_token` 的 `ValueError` 原文 `"密文损坏或密钥不匹配"`）。
- **DB**:
  - 表：`git_credentials`（**仅当 `credential_id` 为真值时**查）
  - 查询：`db.get(GitCredential, body.credential_id)`
  - **不写库**，不更新 `last_used_at`（docstring 明确）；无 commit
- **审计**: 不写。
- **怪癖**:
  - `credential_id` 为 falsy（`None` **或空字符串 `""`**）→ **完全跳过 DB 查询**，以 `plain_token=None` 调 `ls_remote`（公开仓）。判断条件是 Python 真值 `if body.credential_id:`，TS 移植须用 `if (body.credential_id)` 而非 `!= null`。
  - `decrypt_token` 抛 `ValueError` 时不抛 HTTP，软返回 `ok=False`
  - `branch` 字段**路由体未使用**：第 207 行 `result = await git_utils.ls_remote(body.git_url, token=plain_token)`，`body.branch` 未传入（已核 `ls_remote` 签名只接受 `git_url`+`token`，无 branch 形参）

---

### GET /admin/projects

- **守卫**: 同公共守卫，参数名 `_user`。
- **请求**: 无 path / query / body（无分页、无过滤、无排序参数）。
- **成功**: `200 OK`，`response_model=AdminProjectListResponse`
  ```json
  {
    "projects": [
      {
        "id": "string",
        "name": "string",
        "domain": "string | null",
        "status": "string",
        "git_url": "string | null",
        "git_branch": "string",
        "credential_id": "string | null",
        "last_synced_at": "string | null",
        "last_synced_commit": "string | null",
        "sync_schedule": "string",
        "created_at": "string",
        "created_by": "string | null"
      }
    ]
  }
  ```
  映射 `_project_to_admin_pydantic`：`domain = (p.indexing_progress or {}).get("domain")`；`credential_id = p.git_credential_id`；`created_at = _iso(p.created_at) or ""`；`last_synced_at = _iso(p.last_synced_at)`。
- **错误**: 无业务错误（空 → 空数组）。
- **DB**:
  - 表：`projects`
  - 查询：`select(ProjectModel).order_by(ProjectModel.created_at.desc())`（全量，无分页，`created_at` 降序）
  - 无 commit
- **审计**: 不写。
- **怪癖**:
  - `domain` 从 `projects.indexing_progress` JSON 列的 `"domain"` key 读（v1.0 临时；注释标 W7 加正式列）
  - `credential_id` 映射 DB 列 `projects.git_credential_id`
  - `created_at` DB None → `""`

---

### POST /admin/projects

- **守卫**: 同公共守卫，参数名 `user`（函数体写 `created_by`）。
- **请求** (JSON body, `AdminProjectCreateRequest`):

  | 字段 | 类型 | 必选 | 默认 | 约束 |
  |------|------|------|------|------|
  | `id` | string | 是 | — | pattern `^[a-z][a-z0-9-]{0,62}[a-z0-9]$` |
  | `name` | string | 是 | — | min_length=1, max_length=128 |
  | `domain` | string\|null | 否 | `null` | max_length=64 |
  | `git_url` | string | 是 | — | min_length=4, max_length=512 |
  | `git_branch` | string | 否 | `"main"` | min_length=1, max_length=128 |
  | `credential_id` | string\|null | 否 | `null` | 无长度约束 |
  | `language` | string | 否 | `"java"` | 无约束 |
- **成功**: `201 Created`，`response_model=AdminProjectResponse`（字段同 GET /admin/projects 单项；`refresh` 后映射）。
- **错误**:

  | status | detail（逐字） | 触发 | 顺序 |
  |--------|---------------|------|------|
  | 409 | `工程 ID 已存在: {body.id}` | `db.get(ProjectModel, body.id)` 非 None | **先** |
  | 404 | `凭证不存在` | `credential_id` 为真值但 DB 找不到 | **后** |

  插值格式：f-string `f"工程 ID 已存在: {body.id}"`（冒号后一个空格 + id 原样）。
- **DB**:
  - 表：`projects`（写）+ `git_credentials`（`credential_id` 为真值时校验查）
  - 写入字段：`id`、`name`、`language`、`status="configured"`（硬编码）、`git_url`、`git_branch`、`git_credential_id=body.credential_id`、`indexing_progress={"domain": body.domain} if body.domain else None`、`created_by=user.username`（username，非 user.id）
  - `db.add(p)` → `await db.commit()` → `await db.refresh(p)` → 映射返回
- **审计**: **不写**（路由体内无 `log_audit`）。
- **怪癖**:
  - `domain` 存进 `indexing_progress` JSON，非独立列
  - `status` 固定 `"configured"`（请求 schema 无 status 字段）
  - `body.domain` 为 falsy（`None` 或 `""`）→ `indexing_progress` 写 `None`（不是 `{"domain": ""}`）

---

### PATCH /admin/projects/{project_id}

- **守卫**: 同公共守卫，参数名 `_user`。
- **请求**:
  - path: `project_id` (string, 必选)
  - body (JSON, `AdminProjectUpdateRequest`，全可选，`null`/不传 = 不更新)：

    | 字段 | 类型 | 默认 | 约束 |
    |------|------|------|------|
    | `name` | string\|null | `null` | min_length=1, max_length=128 |
    | `domain` | string\|null | `null` | max_length=64 |
    | `git_url` | string\|null | `null` | min_length=4, max_length=512 |
    | `git_branch` | string\|null | `null` | min_length=1, max_length=128 |
    | `credential_id` | string\|null | `null` | 无长度约束 |
- **成功**: `200 OK`，`response_model=AdminProjectResponse`。
- **错误**:

  | status | detail（逐字） | 触发 |
  |--------|---------------|------|
  | 404 | `工程不存在` | `db.get(ProjectModel, project_id) is None` |
  | 404 | `凭证不存在` | `credential_id is not None` 且为真值，但 `db.get(GitCredential, ...)` 找不到 |
- **DB**: 表 `projects`（主查）+ `git_credentials`（条件查）。更新逻辑（源码实际顺序）：
  1. `if body.credential_id is not None:` → `if body.credential_id and not await db.get(GitCredential, body.credential_id): raise 404`；然后 `p.git_credential_id = body.credential_id or None`（空串 → None 解绑）
  2. `if body.name is not None:` → `p.name = body.name`
  3. `if body.git_url is not None:` → `p.git_url = body.git_url`
  4. `if body.git_branch is not None:` → `p.git_branch = body.git_branch`
  5. `if body.domain is not None:` → `progress = dict(p.indexing_progress or {}); progress["domain"] = body.domain; p.indexing_progress = progress`
  - `await db.commit()` → `await db.refresh(p)`
- **审计**: **不写**。
- **怪癖**:
  - `credential_id` 字段用**两层判断**：`is not None`（是否更新）+ 真值（空串 `""` → `git_credential_id = None` 解绑，且**跳过**凭证存在校验）。`null` = 不更新该字段，与空串语义不同。
  - `domain` 更新只合并 `"domain"` key，保留 `indexing_progress` 其他 key
  - `body.domain` 可传空串（schema 无 min_length，仅 max 64），此时 `progress["domain"] = ""`（写空串，不删 key；因为判断是 `is not None`，空串过得了）

---

### DELETE /admin/projects/{project_id}

- **守卫**: 同公共守卫，参数名 `_user`。
- **请求**: path `project_id` (string, 必选)。
- **成功**: `204 No Content`（空响应体）。
- **错误**:

  | status | detail（逐字） | 触发 |
  |--------|---------------|------|
  | 404 | `工程不存在` | `db.get(ProjectModel, project_id) is None` |
- **DB**:
  - 表：`projects`（主删）
  - **DB 层 FK CASCADE 实际只波及两张表**：`qa_sessions`（`ForeignKey("projects.id", ondelete="CASCADE")`）和 `user_project_access`（同）。源码 docstring 写的"sessions / messages / feedback"中，**`messages` / `feedback` 不存在为 DB 表**（消息/反馈已改文件式存储，无 `projects.id` FK）——移植时只需级联 `qa_sessions` + `user_project_access`，不要为 messages/feedback 建联表删除。
  - 查询：`db.get(ProjectModel, project_id)`
  - 操作：`db.delete(p)` → `await db.commit()`
  - `git_credentials` 不级联（`projects.git_credential_id` 是 `ON DELETE SET NULL`，方向相反，且删工程不动凭证）
- **审计**: **不写**。
- **怪癖**: 硬删（物理删），无软删标记。

---

## _iso() 工具函数行为规范

```python
def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    fixed = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return fixed.isoformat().replace("+00:00", "Z")
```
- `None` → `None`
- naive（无 tzinfo）→ 强制附 UTC，输出形如 `"2024-01-01T00:00:00Z"`
- aware → 按原 tz 序列化，把 `+00:00` 替换为 `Z`（非 UTC 偏移如 `+08:00` 保留不变）
- 调用方对 `created_at` 用 `_iso(...) or ""`（DB None 兜底空串），其余 datetime 字段直接用，保留 `Optional[str]`

---

## 依赖的共享设施

### `get_current_user`（`auth_dependencies.py`）
- 从 `Authorization: Bearer <token>` 取 JWT；`oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)`
- `decode_token`，校验 `payload["type"] == "access"`；`sub` → int user_id；查 `users` 表
- 无 token / 解析失败 / 类型不对 / sub 缺失 / sub 非 int / 用户不存在 / `is_active=False` → 全部 **401 `"Not authenticated"`** + `WWW-Authenticate: Bearer`

### `require_admin`（`admin_router.py` 内部定义，本族专用）
- 依赖 `get_current_user`；`user.is_admin == False` → **403 `"仅管理员可访问"`**；成功返回 `User`
- 与 `auth_dependencies.py` 的 `get_current_admin`（detail = `"Admin only"`）是**两个不同函数**，admin 路由族用前者（中文文案）

### `require_infra_healthy`（`deps_infra.py`）
- 依赖 `get_current_user`（需先认证以区分 admin/普通用户）
- 读 `request.app.state.infra_status`（startup 写入的 dict）
- 非致命依赖集合 `_NON_CRITICAL_DEPS = {"neo4j"}`（neo4j down 不触发 503）
- 503 两种：`INFRA_UNINITIALIZED`（state None）、`INFRA_UNHEALTHY`（有 critical dep `ok=False`）
- admin 的 `INFRA_UNHEALTHY` 响应额外附 `"deps": <完整 infra_status>`

### `log_audit`（`audit/logger.py`）
- 签名：`log_audit(db, *, actor_user_id: int, action: str, resource_type: str, resource_id: str, metadata: Optional[dict]=None, ip_address: Optional[str]=None) -> None`（`*` 后全 keyword-only）
- **不主动 commit**，调用方自行 `await db.commit()`
- 写入 `audit_logs` 表，ORM 字段：`actor_user_id`、`action`、`resource_type`、`resource_id=str(resource_id)`、`metadata_json=json.dumps(metadata or {}, ensure_ascii=False)`（None → `"{}"`）、`ip_address`
- 失败仅 `logger.warning`，**不抛错**（审计失败不中断业务）
- DB 列约束（drizzle 已对齐）：`resource_type` varchar(32)、`resource_id` varchar(128)、`metadata_json` text、`ip_address` varchar(45)、`actor_user_id` nullable（用户注销后历史保留）

### `actions`（`audit/actions.py`）
admin 族实际使用：

| 常量 | 值 | 路由 |
|------|----|------|
| `CREDENTIAL_DELETE` | `"credential.delete"` | DELETE /admin/credentials/{id} |

其余路由（projects CRUD、test-connection）**均未写审计**，无对应 action。

### DB 模型要点（`db_models_homepage.py` / `db_models_groups.py`）
- `git_credentials`：`token_hint` varchar(16) nullable、`encrypted_token` Text（永不出 API）、`owner_user_id` int FK→users `ON DELETE SET NULL`、`created_at` server_default now
- `projects`：PK `id` varchar(64)（业务字符串）、`status` 默认 `"indexing"`（但 admin 创建硬编码 `"configured"`）、`git_branch` 默认 `"main"`、`sync_schedule` 默认 `"manual"`、`git_credential_id` FK→git_credentials `ON DELETE SET NULL`、`indexing_progress` JSON nullable
- 对 `projects.id` 设 `ON DELETE CASCADE` 的表仅 `qa_sessions`、`user_project_access`

---

# 【users 族（/admin/users）】

Important finding: the **Python ORM** (`db_models_homepage.py` line 164) DOES define `owner_user_id` with `ondelete="SET NULL"`. But the **drizzle introspected schema** (`schema.ts` line 39) shows `owner_user_id: int().references(() => users.id)` with NO `onDelete` — meaning the live MySQL DB has `RESTRICT`/`NO ACTION`, not `SET NULL`. This is a Python-ORM-vs-live-DB drift.

The spec is verifying against Python source as the authority. The Python ORM says SET NULL, so the spec's claim aligns with the Python ORM. But the spec cites schema.ts as cross-reference, and schema.ts disagrees. This is worth flagging prominently for the TS port (since the TS port uses the drizzle schema, which would RESTRICT delete). Let me note this nuance precisely.

Now I have all I need. Let me produce the corrected complete specification.

## 修正清单

逐条核对源码后，发现以下错误/需补充项（按严重度排序）：

1. **【关键·FK 行为冲突】DELETE 路由 `git_credentials.owner_user_id` 的 `onDelete`**：spec 断言「`onDelete: set null`」并把它列在 DB 自动处理里。核对发现 **Python ORM 与 live DB drizzle schema 不一致**：
   - Python ORM `db_models_homepage.py` 第 162~166 行：`ForeignKey("users.id", ondelete="SET NULL")` ✅（spec 描述与 Python 源码一致）
   - 但 drizzle introspect `schema.ts` 第 39 行：`owner_user_id: int().references(() => users.id)` —— **没有任何 `onDelete`**，即 live MySQL 上是 `RESTRICT`/`NO ACTION`。
   - 影响 TS 移植：若按真库行为，删除「拥有凭证的用户」会被 **MySQL FK 约束直接拒绝（1451 错误）**，而不是静默置 NULL。已在下方 DELETE 节用醒目方框标注此 drift，TS 实现者必须二选一对齐（建议补 migration 把 FK 改成 SET NULL 以匹配 Python 语义）。

2. **【表述矛盾·已澄清】PATCH 全 null 时的 commit 行为**：spec 原文「全部为 null 时...也不执行 commit（⚠️ 实际会调用 await db.commit()...）」自相矛盾。核对第 465 行：**无条件 `await db.commit()`**。已改写为单一明确结论：全 null 也照常 commit（空事务，无 audit、无 mutate）。

3. **【补充·infra 503 触发链】**：spec 把 `require_infra_healthy` 的 401 也归到「infra 守卫」。核对 `deps_infra.py`：401（`Not authenticated`）实际由其内部依赖 `get_current_user` 抛出，发生在 infra 状态检查**之前**。即使 infra 未初始化，未登录请求也是先 401 再轮不到 503。已在守卫节明确这一顺序（token 校验 → infra 状态 → admin 校验）。

4. **【补充·DELETE count 字段】**：spec 的 DELETE DB 流程写 `COUNT(group_id)` / `COUNT(project_id)`，核对第 515、531 行属实（`func.count(GroupMember.group_id)` / `func.count(UserProjectAccess.project_id)`）。已保留并标注 `or 0` 在 `db.scalar` 之后（第 519、535 行）。

5. **【补充·PATCH password audit 公共字段 resource_id】**：spec 写 `resource_id: str(uid)`，核对第 458 行属实（用 path 参数 `uid` 而非 `user.id`，二者相等但来源不同——TS 移植照抄 `String(uid)`）。已显式标注来源。

**抽查并核验通过（无修正）的点**：

- 所有 4 条路由的 method/path（GET/POST `/admin/users`、PATCH/DELETE `/admin/users/{uid}`）—— 与 OpenAPI 存档逐一吻合，无漏路由。
- 全部 detail 文案逐字：`"Not authenticated"`、`"Admin only"`、`"邮箱已被注册"`、`"用户名已被占用"`、`"用户不存在"`、`"不能降级最后一个 Instance Admin"`、`f"用户是 {N} 个组的 owner，先转让"`、`f"用户是 {N} 个工程的 owner，先转让"`、infra 的 `"系统正在初始化，请稍后重试"` / `"系统暂时不可用，请联系管理员"` —— 全部逐字正确。
- 状态码：201（POST）、204（DELETE）、404、409、422、403、401、503 —— 全部正确。
- pydantic 边界：`username min_length=1/max_length=100`、`password min_length=8`、`is_admin default=False` —— 与 OpenAPI schema 完全一致。
- 审计 action 字符串：`user.create` / `user.set_admin` / `user.activate` / `user.deactivate` / `user.update` / `user.delete` —— 与 `actions.py` 逐字吻合。
- POST 双审计顺序（先 CREATE 后 SET_ADMIN）、PATCH 处理顺序（is_admin→is_active→password）、`_count_admins` 的 `count <= 1`（第 401 行）、`flush` 在 `commit` 前 —— 全部属实。
- email 检查先于 username（第 275/285 行）、group ownership 检查先于 project（第 514/530 行）、审计写在 `db.delete` 之前（第 550/567 行）—— 顺序正确。
- DELETE 无「最后一个 admin」保护、无自删除 guard、PATCH 无自操作 guard —— 核对属实。
- `audit_logs` 表结构（schema.ts 第 14~28 行）、`group_members.user_id`/`user_project_access.user_id` 的 `onDelete: cascade`、`audit_logs.actor_user_id` 的 `set null` —— 全部正确。
- `log_audit` 签名、`metadata or {}`+`ensure_ascii=False`、吞异常不 raise —— 正确。

---

# 「users」族移植行为规范（核验修正版）

> 权威来源：`/Users/java/knowledge-engineering/src/service/user_router.py`（共 4 路由，无遗漏）。
> 路由前缀 `/admin/users`，`tags=["admin-users"]`，router 级 `dependencies=[Depends(require_infra_healthy)]`。

---

## GET /admin/users

### 守卫

执行顺序（从外到内，FastAPI 依赖求值顺序）：

1. **`require_infra_healthy`**（router 级 `dependencies`，先于路由级 `Depends` 执行）
   其内部依赖链：
   1. `get_current_user`（JWT Bearer → User）。**token 缺失/无效/过期，或解出的用户 `is_active=False` → 401 `"Not authenticated"`**（带 header `WWW-Authenticate: Bearer`）。此步在 infra 状态检查之前——未登录请求永远先拿 401，轮不到 503。
   2. 读 `app.state.infra_status`：
      - 为 `None` → 503 `{"code":"INFRA_UNINITIALIZED","message":"系统正在初始化，请稍后重试"}`
      - 存在 critical dep（排除 `_NON_CRITICAL_DEPS = {"neo4j"}`）`ok=False` → 503 `{"code":"INFRA_UNHEALTHY","message":"系统暂时不可用，请联系管理员"}`；**当前用户 `is_admin=True` 时额外附 `"deps": <state dict>`**。
2. **`get_current_admin`**（路由级 `Depends`，参数名 `_admin`）
   - 内部复用 `get_current_user`（FastAPI 同请求内缓存同一 dependency 结果）。
   - `user.is_admin == False` → 403 `"Admin only"`。

### 请求

| 位置 | 字段 | 类型 | 必选 | 默认 | 约束 |
|---|---|---|---|---|---|
| query | `username` | string \| null | 否 | `None` | 无 |
| query | `is_admin` | boolean \| null | 否 | `None` | 无 |
| query | `is_active` | boolean \| null | 否 | `None` | 无 |

无请求体。三个参数均为 `Query(None, ...)`。

### 成功

- **status**: 200
- **响应**: `list[UserResponse]`，可为空数组 `[]`。

`UserResponse` 字段（6 个，`required` 全部必出）：

```json
{
  "id": 1,
  "email": "user@example.com",
  "username": "alice",
  "is_active": true,
  "is_admin": false,
  "created_at": "2026-05-12T08:00:00"
}
```

- `created_at`: `user.created_at.isoformat()` 字符串（无时区后缀，取决于 DB 存值）。
- **不含** `hashed_password`、`failed_attempts`、`locked_until`、`updated_at`、`preferred_model`（这些 users 表列被 `_to_response` 显式排除）。

### 错误

| status | detail 逐字 | 触发条件 |
|---|---|---|
| 401 | `"Not authenticated"` | Bearer token 缺失/无效/过期，或用户 `is_active=False`（由 `get_current_user` 抛，最先） |
| 403 | `"Admin only"` | 已登录但 `is_admin=False` |
| 503 | `{"code":"INFRA_UNINITIALIZED","message":"系统正在初始化，请稍后重试"}` | `app.state.infra_status` 为 None |
| 503 | `{"code":"INFRA_UNHEALTHY","message":"系统暂时不可用，请联系管理员"}` | critical dep 不可用（admin 额外附 `"deps"` 字段） |

### DB

- **表**: `users`（只读）。
- **查询**: `select(User)`，按传参动态附加：
  - `username is not None` → `.where(User.username.contains(username))` → SQL `LIKE '%username%'`
  - `is_admin is not None` → `.where(User.is_admin == is_admin)`
  - `is_active is not None` → `.where(User.is_active == is_active)`
- **排序**: `.order_by(User.id)`（**第 229 行，按 `id` 升序，非 `created_at`**；注释称「id 自增是创建时间升序的近似」）。
- 取数: `(await db.scalars(stmt)).all()`，一次全量，**无分页 / 无 limit / 无 offset**。
- 无 commit（纯读）。

### 审计

不写 `audit_logs`。

### 怪癖

- 排序键是 `User.id` 而非 `created_at`。
- 无分页，返回全量，结果集可能为 `[]`。
- 三个 filter 均用 `is not None` 判断：传空字符串 `username=""` 也会触发 `LIKE '%%'`（匹配所有）。`is_admin=false` / `is_active=false` 是合法过滤值（`False is not None` 为真），不会被当成「不过滤」。
- 大小写敏感性取决于 DB 字符集（MySQL 默认 `utf8mb4_general_ci` 不区分大小写）。
- 权限参数名 `_admin`（前缀下划线 = 仅做权限校验，函数体内不使用其值）。

---

## POST /admin/users

### 守卫

1. `require_infra_healthy`（router 级，含 401/503，见 GET 节）。
2. `get_current_admin`（路由级，参数名 `admin`）：`is_admin=False` → 403 `"Admin only"`。

### 请求

**请求体**（`UserCreateRequest`，JSON，必须）：

| 字段 | 类型 | 必选 | 默认 | 约束 |
|---|---|---|---|---|
| `email` | EmailStr | 是 | — | Pydantic 邮箱格式校验（需 `email-validator`） |
| `username` | string | 是 | — | `min_length=1`, `max_length=100` |
| `password` | string | 是 | — | `min_length=8` |
| `is_admin` | boolean | 否 | `false` | 无 |

校验失败 → FastAPI 自动 422（`HTTPValidationError` 标准结构）。

### 成功

- **status**: 201 Created
- **响应**: `UserResponse`（单对象，格式同上）。
- `is_active` **硬编码为 `True`**（创建时不可由请求体控制）。
- `created_at` 来自 DB（users 表 `created_at DEFAULT CURRENT_TIMESTAMP`），经 `db.refresh(user)` 读回后 `.isoformat()` 序列化。

### 错误

| status | detail 逐字 | 触发条件 |
|---|---|---|
| 401 | `"Not authenticated"` | token 无效 |
| 403 | `"Admin only"` | 非 admin |
| 409 | `"邮箱已被注册"` | `email` 已存在 |
| 409 | `"用户名已被占用"` | `username` 已存在 |
| 422 | FastAPI `HTTPValidationError` | Pydantic 字段校验失败 |
| 503 | （同 infra） | — |

### DB

- **表**: `users`（INSERT）、`audit_logs`（INSERT 1~2 条）。
- **检查/写入顺序**（严格）：
  1. `db.scalar(select(User.id).filter_by(email=body.email))` → 非 None → 409 `"邮箱已被注册"`
  2. `db.scalar(select(User.id).filter_by(username=body.username))` → 非 None → 409 `"用户名已被占用"`
  3. 构造 `User(email, username, hashed_password=hash_password(password), is_active=True, is_admin=body.is_admin)` → `db.add(user)`
  4. `await db.flush()`（分配自增 `user.id`，不提交）
  5. `log_audit(... USER_CREATE ...)`
  6. 若 `user.is_admin` 为真：`log_audit(... USER_SET_ADMIN ...)`
  7. `await db.commit()`（user 行 + audit 行原子提交）
  8. `await db.refresh(user)` 读回 DB 生成字段
- **事务**: 单一事务，user + audit 原子提交。
- `flush()` 必须在 audit 之前——审计 `resource_id=str(user.id)` 依赖已分配的自增 id。

### 审计

| 条件 | action | metadata |
|---|---|---|
| 始终 | `"user.create"` | `{"email": user.email, "username": user.username, "is_admin": user.is_admin}` |
| 仅 `is_admin=True` | `"user.set_admin"` | `{"is_admin": true, "reason": "created_as_admin"}` |

两条公共字段：

- `actor_user_id`: `admin.id`（操作人）
- `resource_type`: `"user"`
- `resource_id`: `str(user.id)`
- `ip_address`: `request.client.host if request.client else None`

### 怪癖

- email 唯一性检查先于 username；先命中的 409 立即返回，后者不再查。
- `is_active` 硬编码 `True`。
- `is_admin=True` 写**两条** audit（先 CREATE 后 SET_ADMIN）。
- `flush()` 后、`commit()` 前写审计；审计内部 try/except 吞异常，不阻塞 commit（`log_audit` 不 raise）。
- `request.client` 可能为 None（反代场景），三元安全处理（第 323、335 行）。
- 唯一性靠应用层 SELECT 预检；users 表上 `email`/`username` 各有 UNIQUE 约束（schema.ts 第 147~148 行）作为兜底，但代码不依赖捕获 IntegrityError——并发下仍有竞态窗口（TS 移植照抄此行为，不要擅自加锁）。

---

## PATCH /admin/users/{uid}

### 守卫

1. `require_infra_healthy`（router 级）。
2. `get_current_admin`（路由级，参数名 `admin`）。

### 请求

**路径参数**：

| 字段 | 类型 | 必选 |
|---|---|---|
| `uid` | integer | 是 |

**请求体**（`UserUpdateRequest`，JSON，必须）：

| 字段 | 类型 | 必选 | 默认 | 约束 | 语义 |
|---|---|---|---|---|---|
| `is_admin` | boolean \| null | 否 | `None` | 无 | `null` = 不改 |
| `is_active` | boolean \| null | 否 | `None` | 无 | `null` = 不改 |
| `password` | string \| null | 否 | `None` | `min_length=8`（仅非 null 时校验） | `null` = 不改密码 |

三字段全 null 时请求仍 200。

### 成功

- **status**: 200
- **响应**: 更新后的 `UserResponse`。

### 错误

| status | detail 逐字 | 触发条件 |
|---|---|---|
| 401 | `"Not authenticated"` | token 无效 |
| 403 | `"Admin only"` | 非 admin |
| 404 | `"用户不存在"` | `uid` 不存在 |
| 422 | `"不能降级最后一个 Instance Admin"` | `is_admin=False` 且与当前值不同，且全局 admin 总数 `<= 1` |
| 422 | FastAPI `HTTPValidationError` | Pydantic 校验失败（如 `password` 长度 < 8） |
| 503 | （同 infra） | — |

### DB

- **表**: `users`（UPDATE）、`audit_logs`（INSERT 0~3 条）。
- **流程**：
  1. `await db.get(User, uid)` → 若 falsy（None）→ 404 `"用户不存在"`
  2. `ip = request.client.host if request.client else None`
  3. **is_admin 分支**：`if body.is_admin is not None and body.is_admin != user.is_admin:`
     - 若 `not body.is_admin`（降级）：`admin_count = await _count_admins(db)`；`if admin_count <= 1:` → 422 `"不能降级最后一个 Instance Admin"`
     - 否则 mutate `user.is_admin`，写 `USER_SET_ADMIN`
  4. **is_active 分支**：`if body.is_active is not None and body.is_active != user.is_active:` → mutate，写 `USER_ACTIVATE`（新值 True）或 `USER_DEACTIVATE`（新值 False）
  5. **password 分支**：`if body.password is not None:` → `user.hashed_password = hash_password(body.password)`，写 `USER_UPDATE`
  6. `await db.commit()`（**无条件执行**，即使三字段全 null / 无任何 mutate）
  7. `await db.refresh(user)`
- **事务**: 所有字段变更 + 所有 audit 同一事务原子提交。

### 审计

仅在字段**实际变更**时写（is_admin / is_active 含「新值 != 旧值」短路；password 不含）：

| 触发条件 | action | metadata |
|---|---|---|
| `is_admin != null` 且 `!= user.is_admin` | `"user.set_admin"` | `{"old_is_admin": <旧 bool>, "new_is_admin": <新 bool>}` |
| `is_active != null` 且 `!= user.is_active` 且新值 `True` | `"user.activate"` | `{"old_is_active": <旧 bool>, "new_is_active": true}` |
| `is_active != null` 且 `!= user.is_active` 且新值 `False` | `"user.deactivate"` | `{"old_is_active": <旧 bool>, "new_is_active": false}` |
| `password != null` | `"user.update"` | `{"field": "password", "changed": true}` |

公共字段：

- `actor_user_id`: `admin.id`
- `resource_type`: `"user"`
- `resource_id`: `str(uid)`（用 **path 参数 uid**，第 418/439/458 行；与 `user.id` 数值相等但来源是路径）
- `ip_address`: `ip`（= `request.client.host if request.client else None`）

### 怪癖

- 「最后一个 Instance Admin」保护**仅在降级（is_admin=False）时**触发；检查全局 admin count（含操作人自己），`_count_admins` 用 `count <= 1`（第 401 行）——等于 1 也拦截，即操作人自降级最后一个 admin 同样被拒 422。
- is_admin / is_active 的判断含「值未变」短路（`body.x != user.x`）：值相同则**不 mutate、不写 audit**。
- password **无**「是否变更」判断：只要 `body.password is not None` 就 hash + 写 audit（传入与当前相同的明文也会触发，且每次产生不同 bcrypt hash）。
- **无 self-operation guard**：管理员可对自己执行 PATCH。
- 三字段全 null 时仍 `await db.commit()`（空事务提交，无 audit、无 mutate，第 465 行）。
- 处理顺序固定：`is_admin` → `is_active` → `password`（多字段一次提交，可写出多达 3 条 audit）。

---

## DELETE /admin/users/{uid}

### 守卫

1. `require_infra_healthy`（router 级）。
2. `get_current_admin`（路由级，参数名 `admin`）。

### 请求

| 位置 | 字段 | 类型 | 必选 |
|---|---|---|---|
| path | `uid` | integer | 是 |

无请求体，无 query。

### 成功

- **status**: 204 No Content
- **响应**: 无响应体（函数返回 `None`）。

### 错误

| status | detail 逐字 | 触发条件 |
|---|---|---|
| 401 | `"Not authenticated"` | token 无效 |
| 403 | `"Admin only"` | 非 admin |
| 404 | `"用户不存在"` | `uid` 不存在 |
| 422 | `f"用户是 {group_owner_count} 个组的 owner，先转让"` | 在 `group_members` 有 `role='owner'` 记录（`> 0`）；N 为整数插值（第 525 行） |
| 422 | `f"用户是 {project_owner_count} 个工程的 owner，先转让"` | 在 `user_project_access` 有 `role='owner'` 记录（`> 0`）；N 为整数插值（第 541 行） |
| 503 | （同 infra） | — |

### DB

- **涉及表**: `users`（DELETE）、`group_members`（COUNT）、`user_project_access`（COUNT）、`audit_logs`（INSERT）；外加 FK 级联涉及 `git_credentials`（见下方 drift 警告）。
- **流程**：
  1. `await db.get(User, uid)` → falsy → 404 `"用户不存在"`
  2. `db.scalar(select(func.count(GroupMember.group_id)).filter_by(user_id=uid, role="owner")) or 0` → `> 0` → 422（组）
  3. `db.scalar(select(func.count(UserProjectAccess.project_id)).filter_by(user_id=uid, role="owner")) or 0` → `> 0` → 422（工程）
  4. `log_audit(... USER_DELETE ...)`（**物理删除前**写，保留 email/username 快照）
  5. `await db.delete(user)`（标记删除）
  6. `await db.commit()`（审计 + 删除原子提交）

> ⚠️ **【FK 行为·Python ORM 与 live DB 不一致，TS 移植必读】**
>
> | FK | Python ORM（`db_models_*`） | drizzle introspect（`schema.ts`，= live MySQL） |
> |---|---|---|
> | `group_members.user_id` | `ondelete="CASCADE"` | `onDelete: "cascade"` ✅ 一致 |
> | `user_project_access.user_id` | `ondelete="CASCADE"` | `onDelete: "cascade"` ✅ 一致 |
> | `audit_logs.actor_user_id` | `ondelete="SET NULL"` | `onDelete: "set null"` ✅ 一致 |
> | **`git_credentials.owner_user_id`** | **`ondelete="SET NULL"`**（`db_models_homepage.py` 第 164 行） | **无 `onDelete`**（`schema.ts` 第 39 行 `int().references(() => users.id)`）= MySQL `RESTRICT`/`NO ACTION` ❌ **不一致** |
>
> 后果：Python 源码意图是「删用户时凭证 `owner_user_id` 置 NULL、凭证保留」；但 **live DB 上该 FK 没有 SET NULL**，删除一个仍持有 `git_credentials` 的用户会被 MySQL **直接拒绝（errno 1451 外键约束）**，commit 抛错回滚。TS 移植前必须二选一对齐：(a) 补一条 migration 把 `git_credentials.owner_user_id` FK 改为 `ON DELETE SET NULL`（推荐，对齐 Python 语义）；或 (b) 在 TS DELETE 逻辑里显式处理凭证（先置 NULL 再删用户）。DELETE 路由**没有**对凭证的任何应用层检查或处理。

### 审计

| 时机 | action | metadata |
|---|---|---|
| `db.delete(user)` **之前** | `"user.delete"` | `{"email": user.email, "username": user.username}` |

- `actor_user_id`: `admin.id`
- `resource_type`: `"user"`
- `resource_id`: `str(uid)`
- `ip_address`: `request.client.host if request.client else None`

### 怪癖

- 审计写在物理删除之前（第 550~561 行），捕获 email/username 快照；commit 失败则业务删除 + 审计一起回滚（原子性）。
- group ownership 检查先于 project ownership；任一 `> 0` 即返回 422，后者不再查（短路）。
- **不检查「最后一个 Instance Admin」**：DELETE 无 admin count 保护（仅 PATCH 有）——可删掉最后一个 admin，源码无相关代码。
- **无自删除 guard**：第 506~571 行无 `uid == admin.id` 检查，admin 可删自己。
- `count or 0` 模式：`db.scalar` 空结果返回 None，`or 0` 保证 int（第 519、535 行）。
- 凭证处理：Python 注释声称完全委托 DB FK（第 544~547 行「不需要任何额外操作」）——但见上方 drift 警告，live DB 并不会自动 SET NULL。

---

## 依赖的共享设施

### `get_current_user`（`src/service/auth_dependencies.py`）

- async FastAPI dependency，从 `Authorization: Bearer <token>` 解析 JWT → `User` ORM 对象。
- `cred_exc` = `HTTPException(401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"})`。
- 流程：
  1. `oauth2_scheme`（`OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)`）取 token；缺失为 `None`。
  2. `if not token` → raise `cred_exc`。
  3. `decode_token(token)`；payload 为 falsy **或** `payload.get("type") != "access"` → `cred_exc`。
  4. `sub = payload.get("sub")`；`if not sub` → `cred_exc`；`int(sub)` 失败（`ValueError`）→ `cred_exc`。
  5. `select(User).where(User.id == user_id)` → `scalar_one_or_none()`；`user is None or not user.is_active` → `cred_exc`。
- 返回活跃 `User`。

### `get_current_admin`（`src/service/auth_dependencies.py`）

- async dependency，`Depends(get_current_user)`。
- `if not user.is_admin:` → 403 `"Admin only"`。
- 返回 admin `User`。

### `require_infra_healthy`（`src/service/deps_infra.py`）

- async dependency，声明在 `APIRouter(dependencies=[...])`（**早于路由级 `Depends` 求值**）。
- 内部 `Depends(get_current_user)`：**未登录先 401**（早于 infra 状态判断）。
- `state = getattr(request.app.state, "infra_status", None)`：
  - `None` → 503 `{"code":"INFRA_UNINITIALIZED","message":"系统正在初始化，请稍后重试"}`
  - `unhealthy = [k for k,v in state.items() if k not in _NON_CRITICAL_DEPS and not v.get("ok")]`；非空 → 503 `{"code":"INFRA_UNHEALTHY","message":"系统暂时不可用，请联系管理员"}`，**`user.is_admin` 时追加 `detail["deps"] = state`**。
  - 空 → `return`（放行）。
- `_NON_CRITICAL_DEPS = {"neo4j"}`（neo4j 退役中，其 down 不触发 503）。

### `log_audit`（`src/service/audit/logger.py`）

- 签名：`async def log_audit(db, *, actor_user_id, action, resource_type, resource_id, metadata=None, ip_address=None) -> None`（`*` 之后全部 keyword-only）。
- 行为：
  - 构造 `AuditLog(...)` 并 `db.add()`（**不 commit**）。
  - `resource_id=str(resource_id)`（统一转 str）。
  - `metadata_json = json.dumps(metadata or {}, ensure_ascii=False)`（metadata 为 None → 写 `"{}"`；中文不转义为 `\uXXXX`）。
  - 任何异常被 try/except 捕获 → `logger.warning(...)`，**不 raise**，不影响业务。
- 调用方负责最终 `await db.commit()` 保证业务 + 审计原子性。

### `actions` 常量（`src/service/audit/actions.py`）

| 常量 | 值 |
|---|---|
| `USER_CREATE` | `"user.create"` |
| `USER_UPDATE` | `"user.update"` |
| `USER_DELETE` | `"user.delete"` |
| `USER_SET_ADMIN` | `"user.set_admin"` |
| `USER_ACTIVATE` | `"user.activate"` |
| `USER_DEACTIVATE` | `"user.deactivate"` |

### `_count_admins`（`user_router.py` 第 156~175 行，内部工具）

- `await db.scalar(select(func.count(User.id)).filter_by(is_admin=True))`。
- `return count or 0`（None → 0）。
- 仅被 PATCH 的 `is_admin=False` 降级分支调用。

### `AuditLog` 表结构（`audit_logs`；ORM `db_models_groups.py` 第 202~267 行 / drizzle `schema.ts` 第 14~28 行，二者一致）

| 列 | 类型 | 约束 |
|---|---|---|
| `id` | int autoincrement | PK |
| `actor_user_id` | int | FK → `users.id` ON DELETE SET NULL，nullable |
| `action` | varchar(64) | NOT NULL |
| `resource_type` | varchar(32) | NOT NULL |
| `resource_id` | varchar(128) | NOT NULL |
| `metadata_json` | text | nullable |
| `ip_address` | varchar(45) | nullable |
| `created_at` | datetime | DEFAULT now() NOT NULL |

索引：`ix_audit_actor_time(actor_user_id, created_at)`、`ix_audit_resource(resource_type, resource_id)`。

### 相关 users 表（`schema.ts` 第 130~149 行，供 `_to_response` 排除字段参考）

`users` 列：`id`(PK autoinc)、`email`(varchar255, UNIQUE)、`username`(varchar100, UNIQUE)、`hashed_password`(varchar255)、`is_active`(tinyint default 1)、`is_admin`(tinyint default 0)、`failed_attempts`(int default 0)、`locked_until`(datetime null)、`created_at`(datetime default CURRENT_TIMESTAMP)、`updated_at`(datetime default CURRENT_TIMESTAMP)、`preferred_model`(varchar64 null)。`UserResponse` 只输出 `id/email/username/is_active/is_admin/created_at` 六列，其余一律不出 API。

---

相关文件路径：
- 主源码：`/Users/java/knowledge-engineering/src/service/user_router.py`
- 守卫：`/Users/java/knowledge-engineering/src/service/auth_dependencies.py`、`/Users/java/knowledge-engineering/src/service/deps_infra.py`
- 审计：`/Users/java/knowledge-engineering/src/service/audit/logger.py`、`/Users/java/knowledge-engineering/src/service/audit/actions.py`
- ORM：`/Users/java/knowledge-engineering/src/service/db_models_groups.py`（AuditLog/GroupMember）、`/Users/java/knowledge-engineering/src/service/db_models_homepage.py`（GitCredential 第 162 行、UserProjectAccess）
- live DB schema：`/Users/java/ke-server/packages/store/src/schema/schema.ts`（注意第 39 行 `git_credentials.owner_user_id` 缺 onDelete）
- OpenAPI：`/Users/java/knowledge-engineering/docs/porting/routes-openapi.json`（4 路由确认）

---

# 【audit 族】

I have everything needed. The spec is very thorough. Let me now do the adversarial pass, focusing on the specific checkpoints. I've found several discrepancies. Let me verify one subtle point about `get_current_user` — the `payload.get("type") != "access"` check, and the `is_active` 401 behavior, both of which the spec touches.

I have all the source confirmed. Here is my verified, corrected spec.

---

## 修正清单

逐条对照源码核对结果。规范整体质量高，绝大部分逐字、边界、顺序正确。发现以下需要修正/补充的点：

1. **【补充 — get_current_user 的 401 触发条件不完整】** 规范只说"无/无效 Bearer token"。源码 `auth_dependencies.py` L33-47 还有两个隐藏 401 分支：(a) `payload.get("type") != "access"`（token 类型不是 access，例如拿 refresh token 来用）→ 401；(b) `sub` 缺失或 `int(sub)` 抛 `ValueError` → 401；(c) `user.is_active == False` → 401。规范在「共享设施」节提到了 `is_active==False → 401`，但漏了 `type != "access"` 这条。已补全。

2. **【修正 — `_NON_CRITICAL_DEPS` 判定语义措辞】** 规范写"不在 `_NON_CRITICAL_DEPS = {"neo4j"}` 中 `ok=False` → 503"。源码 L56-59 的列表推导式条件是 `k not in _NON_CRITICAL_DEPS and not v.get("ok")`。注意是 `not v.get("ok")`——当某 dep 的 dict 里**没有 `ok` 键**时 `v.get("ok")` 返回 `None`，`not None == True`，也会被判为 unhealthy。即"缺 ok 字段"等同于"ok=False"。已在怪癖中点明（TS 实现别只判 `ok === false`，要判 falsy）。

3. **【修正 — 503 INFRA_UNHEALTHY 的 admin deps 字段描述不精确】** 规范在 admin 分支写 `"deps": <infra_status dict>`，正确。但规范把它和"非 admin"并列在同一行用括号区分，易误读为两种 detail。源码 L67-74：base detail 是 `{"code","message"}`，**仅当** `user.is_admin` 为 True 时才 `detail["deps"] = state`。已拆成两行明确。注意 `require_infra_healthy` 先于 `require_admin` 执行，此处 `user.is_admin` 来自 `require_infra_healthy` 自己注入的 `get_current_user`，与是否通过 admin 守卫无关——**普通登录用户但 is_admin=True 也会拿到 deps**（虽然 audit 路由后面还有 admin 守卫，但 infra 503 在 admin 守卫之前就抛了）。已点明。

4. **【确认无误 — 守卫执行顺序】** 规范声称 `require_infra_healthy`（router 级）先于 `require_admin`/`require_group_role`（路由级）。核对：router 级 dependencies 确实先于路由参数级 Depends 求值，FastAPI 行为属实。且 `get_current_user` 在同一 request 内被 FastAPI 依赖缓存，只执行一次。规范描述准确。保留。

5. **【确认无误 — `_validate_page_limit` clamp 公式】** `page = max(1, page)`；`limit = min(max(1, limit), 200)`，逐字对（L150-152）。保留。

6. **【确认无误 — 全部 25 个 action 常量字符串】** 逐字比对 `actions.py` L25-107，规范表格 26 行（含 MESSAGE_EXPORT_DOCX）全部正确。保留。

7. **【确认无误 — log_audit 签名与落库列】** 逐字对 `logger.py` L50-105：keyword-only、`resource_id=str(resource_id)`、`metadata_json=json.dumps(metadata or {}, ensure_ascii=False)`、不 commit、失败仅 `logger.warning`。落库列类型与 `db_models_groups.py` AuditLog 及 ke-server drizzle schema 三方一致（action varchar(64)、resource_type varchar(32)、resource_id varchar(128)、ip_address varchar(45)、metadata_json text、created_at datetime default now()）。保留。

8. **【确认无误 — group 端 BFS / scope OR 四组条件 / project_ids 空列表跳过】** 逐字对 `audit_router.py` L156-200、L402-440。`range(3)`、含自身、`group`/`group_member` 始终加、`project`/`project_member` 仅 `project_ids` 非空时加。保留。

9. **【抽查确认 — 422 触发面】** OpenAPI 存档确认 page `minimum:1`、limit `minimum:1 maximum:200`、from_time/to_time `format:date-time`，与规范 422 条件一致。保留。

10. **【补充 — `metadata` 响应字段的 Pydantic 别名风险（移植提示）】** 源码响应 schema `AuditLogEntry.metadata`（L80）字段名就叫 `metadata`。注意 Pydantic v2 中 `metadata` 不是保留名（BaseModel 无冲突），所以 JSON 输出键就是 `"metadata"`，不是 `"metadata_json"`。规范的响应 JSON 形状用 `"metadata"` 正确，已确认。补一句移植注意：DB 列叫 `metadata_json`，但 API 出参叫 `metadata`，TS 端勿混。

11. **【措辞修正 — `actor_username` 的 getattr】** 规范说"用户注销（FK SET NULL）后返回 None"。更精确：LEFT JOIN 下 `actor_user_id` 为 NULL（或 JOIN 不到 user）时 `User.username` 列为 NULL，`row.actor_username` 即 `None`；`getattr(row, "actor_username", None)` 的 default 分支实际几乎不触发（该列恒在 SELECT 里），是纯防御。已澄清。

其余所有 detail 文案（`"Not authenticated"`、`"仅管理员可访问"`、`"组不存在"`、`"无权访问此组"`、`"需要 {min_role} 及以上权限（当前 {role}）"`、两条 503 message）逐字核对**全部通过**。`ROLE_RANK = {"reporter":1,"maintainer":2,"owner":3}` 确认。

---

# audit 族行为规范（修正后完整版）

> 源码基准：`/Users/java/knowledge-engineering/src/service/audit_router.py`
> 辅助：`permission_deps.py`、`auth_dependencies.py`、`deps_infra.py`、`admin_router.py`、`audit/logger.py`、`audit/actions.py`、`db_models_groups.py`（AuditLog ORM）、ke-server `schema.ts`、`docs/porting/routes-openapi.json`

---

### GET /admin/audit-logs

- **守卫**（执行顺序，FastAPI 依赖缓存使 `get_current_user` 全 request 仅执行一次）：
  1. `require_infra_healthy`（router 级，`APIRouter(dependencies=[Depends(require_infra_healthy)])`，对本 router 所有路由生效）
     - 内部先 `Depends(get_current_user)`：无/无效 token → 401；token `type != "access"` → 401；`sub` 缺失或非整数 → 401；`user.is_active == False` → 401
     - 取 `request.app.state.infra_status`：`None` → 503 `INFRA_UNINITIALIZED`；有 critical dep（不在 `{"neo4j"}` 中且 `ok` 为 falsy）→ 503 `INFRA_UNHEALTHY`
  2. `require_admin`（路由参数级 `_admin: User = Depends(require_admin)`）
     - 内部再次 `Depends(get_current_user)`（命中缓存，不重复执行）
     - `user.is_admin == False` → 403 `"仅管理员可访问"`

- **请求**：

  | 位置 | 名称 | 类型 | 必选 | 默认 | 约束 |
  |------|------|------|------|------|------|
  | query | `actor` | `string \| null` | 否 | `null` | 无 |
  | query | `actor_user_id` | `integer \| null` | 否 | `null` | 无 |
  | query | `resource_type` | `string \| null` | 否 | `null` | 无 |
  | query | `resource_id` | `string \| null` | 否 | `null` | 无 |
  | query | `action_prefix` | `string \| null` | 否 | `null` | 无 |
  | query | `from_time` | `datetime \| null`（ISO8601 / date-time） | 否 | `null` | 无 |
  | query | `to_time` | `datetime \| null`（ISO8601 / date-time） | 否 | `null` | 无 |
  | query | `page` | `integer` | 否 | `1` | `ge=1`（Query 层校验；违反 → 422） |
  | query | `limit` | `integer` | 否 | `50` | `ge=1, le=200`（Query 层；违反 → 422） |

  无 request body。

- **成功**：`HTTP 200`，`AuditLogResponse`：

  ```json
  {
    "entries": [
      {
        "id": 123,
        "actor_user_id": 1,          // int | null
        "actor_username": "alice",   // string | null
        "action": "project.create",
        "resource_type": "project",
        "resource_id": "petclinic",
        "metadata": {},              // dict（DB 列名为 metadata_json，出参键为 metadata）
        "ip_address": "127.0.0.1",   // string | null
        "created_at": "2026-05-12T08:00:00"  // ISO8601 字符串（.isoformat()，无时区后缀；created_at 为 None 时为 ""）
      }
    ],
    "total": 999,   // 满足过滤条件总行数（COUNT 子查询；scalar 为 None → 0）
    "page": 1,      // 经 _validate_page_limit clamp 后的实际页码
    "limit": 50     // 经 clamp 后的实际 limit
  }
  ```

  ⚠️ 移植注意：DB 列叫 `metadata_json`，API 出参键叫 `metadata`，勿混。

- **错误**：

  | Status | detail 逐字 | 触发条件 |
  |--------|-------------|----------|
  | 401 | `"Not authenticated"`（Header `WWW-Authenticate: Bearer`） | 无/无效 token、`type != "access"`、`sub` 缺失/非整数、`is_active == False` |
  | 403 | `"仅管理员可访问"` | `user.is_admin == False` |
  | 422 | FastAPI 标准 ValidationError body | `page < 1` / `limit < 1` / `limit > 200` / `from_time`/`to_time` 格式非法 |
  | 503 | `{"code": "INFRA_UNINITIALIZED", "message": "系统正在初始化，请稍后重试"}` | `app.state.infra_status` 为 `None` |
  | 503 | base：`{"code": "INFRA_UNHEALTHY", "message": "系统暂时不可用，请联系管理员"}`；当请求者 `is_admin` 为 True 时额外追加 `"deps": <infra_status dict>` | 存在 critical dep `ok` falsy |

- **DB**：
  - 表：`audit_logs` LEFT OUTER JOIN `users`（`AuditLog.actor_user_id == User.id`）
  - SELECT 列：`id, actor_user_id, action, resource_type, resource_id, metadata_json, ip_address, created_at, users.username AS actor_username`
  - 过滤条件（全部 AND，仅非 None 才加）：
    - `actor` → `users.username ILIKE '%{actor}%'`（大小写不敏感）
    - `actor_user_id` → `audit_logs.actor_user_id = {actor_user_id}`（精确）
    - `resource_type` → `audit_logs.resource_type = {resource_type}`（精确）
    - `resource_id` → `audit_logs.resource_id = {resource_id}`（精确）
    - `action_prefix` → `audit_logs.action.startswith(...)`，即 `LIKE '{action_prefix}%'`（前缀）
    - `from_time` → `audit_logs.created_at >= {from_time}`（含起点）
    - `to_time` → `audit_logs.created_at <= {to_time}`（含终点）
  - 查询序：① 构造 join+where → ② `SELECT COUNT(*) FROM (join_clause.subquery())` 取 `total` → ③ 同 query 追加 `ORDER BY audit_logs.created_at DESC OFFSET (page-1)*limit LIMIT limit` `.fetchall()`
  - 无事务，只读，无 commit

- **审计**：不写 audit_logs（只读端点）

- **怪癖**：
  - `_validate_page_limit` 二次 clamp：`page=max(1,page)`、`limit=min(max(1,limit),200)`；Query 层 ge/le 已拦截，clamp 是双重防护，正常不生效。
  - `_row_to_entry`：`metadata_json` 为 None/空串 → 回落 `"{}"` 再 `json.loads`；`json.loads` 抛 `JSONDecodeError`/`TypeError` → 回落空 dict `{}`（不崩）。
  - `actor_username`：`getattr(row, "actor_username", None)`，LEFT JOIN 未命中 user 时为 `None`（该列恒在 SELECT，default 分支纯防御）。
  - `created_at` 为 None → 返回 `""`（实际 DB NOT NULL + DEFAULT NOW()，不应发生）。
  - `total`：`db.scalar(...) or 0`，空表/None → 0。
  - COUNT 用 `join_clause.subquery()` 包裹，与数据查询共享同一 WHERE，不重复构造。
  - infra 不健康的 503 在 admin 守卫**之前**抛出；其 `deps` 字段取决于请求者本人的 `is_admin`，不取决于是否通过 audit 的 admin 守卫。

---

### GET /groups/{group_id}/audit-logs

- **守卫**（执行顺序）：
  1. `require_infra_healthy`（router 级，同上；含全部 401 子分支与 503 逻辑）
  2. `require_group_role("owner")`（路由参数级 `_role: str = Depends(require_group_role("owner"))`）
     - 注意：必须注入到参数（而非放 `dependencies=[]`），因 `group_id` 路径参数需被 `checker` 同名捕获
     - 工厂启动期：`min_role` 不在 `ROLE_RANK` → 模块加载/路由注册期 `ValueError`（`"owner"` 合法）
     - checker 检查顺序：
       1. `await db.get(Group, group_id)` 为 `None` → 404 `"组不存在"`
       2. `user.is_admin == True` → 直接返回 `"owner"`（跳过成员表查询）
       3. 否则 `resolve_group_role(user.id, group_id, db)` → 沿 `parent_group_id` 向上 BFS（`depth >= 3` 截断，visited 防环）取最高 role
       4. `role is None` → 403 `"无权访问此组"`
       5. `ROLE_RANK[role] < ROLE_RANK["owner"]` → 403 `"需要 owner 及以上权限（当前 {role}）"`

- **请求**：

  | 位置 | 名称 | 类型 | 必选 | 默认 | 约束 |
  |------|------|------|------|------|------|
  | path | `group_id` | `string` | 是 | — | 无 |
  | query | `page` | `integer` | 否 | `1` | `ge=1` |
  | query | `limit` | `integer` | 否 | `50` | `ge=1, le=200` |

  无过滤参数（仅分页，不支持 actor/resource_type/action_prefix 等）。无 request body。

- **成功**：`HTTP 200`，形状与 `/admin/audit-logs` 完全相同（`AuditLogResponse`）。

- **错误**：

  | Status | detail 逐字 | 触发条件 |
  |--------|-------------|----------|
  | 401 | `"Not authenticated"`（Header `WWW-Authenticate: Bearer`） | 同 admin 端全部 401 子分支 |
  | 404 | `"组不存在"` | `group_id` 无对应 Group |
  | 403 | `"无权访问此组"` | 继承链上无任何 role |
  | 403 | `"需要 owner 及以上权限（当前 {role}）"` | role 为 `reporter` 或 `maintainer` |
  | 422 | FastAPI ValidationError | `page < 1`、`limit` 越界 |
  | 503 | 同 admin 端两条 503 | infra 未初始化 / 不健康 |

- **DB**：
  - **步骤 1**：`_expand_group_and_descendants(group_id, db)` —— BFS 向下展开子孙 group：`SELECT id FROM groups WHERE parent_group_id IN (frontier)`，`range(3)` 最多 3 层，frontier 空或无 new 提前 break，初始 `all_ids = {group_id}`（含自身），返回 `set[str]`
  - **步骤 2**：`SELECT id FROM projects WHERE group_id IN (group_ids)` → `project_ids: list[str]`（可能为空）
  - **步骤 3**：`gid_list = list(group_ids)`；scope OR 条件（OR 连接，每组内 AND）：
    - `resource_type='group' AND resource_id IN (gid_list)`（始终加）
    - `resource_type='group_member' AND resource_id IN (gid_list)`（始终加）
    - `resource_type='project' AND resource_id IN (project_ids)`（仅 `project_ids` 非空时加）
    - `resource_type='project_member' AND resource_id IN (project_ids)`（仅 `project_ids` 非空时加）
  - **步骤 4**：`SELECT ...(同 admin 列) FROM audit_logs LEFT OUTER JOIN users ON ... WHERE or_(*scope_conditions)`
  - **步骤 5**：`SELECT COUNT(*) FROM (base_query.subquery())` → `total`（`or 0`）
  - **步骤 6**：`ORDER BY audit_logs.created_at DESC OFFSET (page-1)*limit LIMIT limit` `.fetchall()`
  - 无事务，只读，无 commit

- **审计**：不写 audit_logs

- **怪癖**：
  - `project_ids` 为空时，`project` 与 `project_member` 的 OR 条件**不加入**（避免 `IN ()` 空列表 SQL 错误）。
  - scope 仅含 `group` / `group_member` / `project` / `project_member` 四类；**不**含 `user` / `auth` / `credential` / `message`。
  - `group_member` 的 `resource_id` 取的是 group_id（与 group 同用 `gid_list`）。
  - 分页 clamp 复用同一 `_validate_page_limit`，逻辑与 admin 端完全相同。
  - `group_ids` 转 `list` 后传 `.in_()`。

---

## 审计写入端（`audit/logger.py`）数据形状

`log_audit` 签名（`db` 为位置参数，其余 keyword-only）：

```
log_audit(
    db: AsyncSession,
    *,
    actor_user_id: int,                 # 必填
    action: str,                        # 必填
    resource_type: str,                 # 必填
    resource_id: str,                   # 必填，落库前 str(resource_id) 强转
    metadata: Optional[dict] = None,    # None → 写 "{}"
    ip_address: Optional[str] = None,   # 可选
) -> None
```

落库列（`audit_logs`，三方校对：Python ORM / Python service / ke-server drizzle 一致）：

| 列名 | DB 类型 | 写入值 |
|------|---------|--------|
| `actor_user_id` | int（FK users.id, ON DELETE SET NULL, nullable） | `actor_user_id` 参数 |
| `action` | varchar(64) NOT NULL | `action` 参数 |
| `resource_type` | varchar(32) NOT NULL | `resource_type` 参数 |
| `resource_id` | varchar(128) NOT NULL | `str(resource_id)` |
| `metadata_json` | text（nullable） | `json.dumps(metadata or {}, ensure_ascii=False)` |
| `ip_address` | varchar(45)（nullable） | `ip_address` 参数（可 NULL） |
| `created_at` | datetime NOT NULL, DEFAULT NOW() | 不由应用填，DB 默认 |

索引：`ix_audit_actor_time (actor_user_id, created_at)`、`ix_audit_resource (resource_type, resource_id)`。

**不主动 commit**：仅 `db.add(AuditLog(...))`，由 caller 与业务事务一起 commit（原子）。`db.add` 抛异常仅 `logger.warning("audit log 写入失败 ...")`，不 re-raise（审计失败不中断业务）。

全部 action 常量（`audit/actions.py`，逐字核对全部通过）：

| 常量名 | 字符串值 |
|--------|---------|
| `AUTH_LOGIN_SUCCESS` | `"auth.login_success"` |
| `AUTH_LOGIN_FAILURE` | `"auth.login_failure"` |
| `AUTH_LOGOUT` | `"auth.logout"` |
| `AUTH_PASSWORD_CHANGE` | `"auth.password_change"` |
| `USER_CREATE` | `"user.create"` |
| `USER_UPDATE` | `"user.update"` |
| `USER_DELETE` | `"user.delete"` |
| `USER_SET_ADMIN` | `"user.set_admin"` |
| `USER_ACTIVATE` | `"user.activate"` |
| `USER_DEACTIVATE` | `"user.deactivate"` |
| `GROUP_CREATE` | `"group.create"` |
| `GROUP_UPDATE` | `"group.update"` |
| `GROUP_DELETE` | `"group.delete"` |
| `GROUP_MEMBER_ADD` | `"group_member.add"` |
| `GROUP_MEMBER_REMOVE` | `"group_member.remove"` |
| `GROUP_MEMBER_ROLE_CHANGE` | `"group_member.role_change"` |
| `PROJECT_CREATE` | `"project.create"` |
| `PROJECT_UPDATE` | `"project.update"` |
| `PROJECT_DELETE` | `"project.delete"` |
| `PROJECT_REINDEX_TRIGGER` | `"project.reindex_trigger"` |
| `PROJECT_MEMBER_ADD` | `"project_member.add"` |
| `PROJECT_MEMBER_REMOVE` | `"project_member.remove"` |
| `PROJECT_MEMBER_ROLE_CHANGE` | `"project_member.role_change"` |
| `CREDENTIAL_CREATE` | `"credential.create"` |
| `CREDENTIAL_DELETE` | `"credential.delete"` |
| `MESSAGE_EXPORT_DOCX` | `"message.export_docx"` |

---

## 依赖的共享设施

### `require_infra_healthy`（`deps_infra.py`）
- 挂 router 级 `dependencies=[Depends(require_infra_healthy)]`，对 audit router 所有路由生效。
- 先调 `get_current_user`（故所有路由隐含 401 守卫）。
- 取 `request.app.state.infra_status`（dict）。
- `None` → 503 `{"code":"INFRA_UNINITIALIZED","message":"系统正在初始化，请稍后重试"}`。
- unhealthy 判定：`[k for k,v in state.items() if k not in {"neo4j"} and not v.get("ok")]`。注意 `not v.get("ok")`——**ok 字段缺失（None）也算 unhealthy**，TS 端勿仅判 `=== false`。非空 → 503 `{"code":"INFRA_UNHEALTHY","message":"系统暂时不可用，请联系管理员"}`；`if user.is_admin` 再追加 `detail["deps"] = state`。

### `get_current_user`（`auth_dependencies.py`）
- `OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)` → token。
- 401（detail 逐字 `"Not authenticated"`，Header `WWW-Authenticate: Bearer`）触发：token 缺失；`decode_token` 失败或 `payload.get("type") != "access"`；`sub` 缺失；`int(sub)` 抛 `ValueError`；查无 user 或 `user.is_active == False`。
- 返回 `User`。

### `require_admin`（`admin_router.py` L61-65）
- `Depends(get_current_user)` → `user.is_admin == False` → 403 detail 逐字 `"仅管理员可访问"`，否则返回 `User`。
- audit_router 声明为 `_admin`，仅守卫不取值。
- ⚠️ 与 `auth_dependencies.get_current_admin`（detail `"Admin only"`）是**两个不同函数**，audit 用的是 admin_router 的 `require_admin`（中文文案）。

### `require_group_role(min_role)`（`permission_deps.py`）
- 工厂；`min_role` 不在 `ROLE_RANK={"reporter":1,"maintainer":2,"owner":3}` → 启动期 `ValueError`。
- checker 注入 `group_id`(path) + `get_current_user` + `get_db`，顺序：
  1. `db.get(Group, group_id)` None → 404 `"组不存在"`
  2. `user.is_admin` → 返回 `"owner"`
  3. `resolve_group_role(user.id, group_id, db)`
  4. `role is None` → 403 `"无权访问此组"`
  5. `ROLE_RANK[role] < ROLE_RANK[min_role]` → 403 `"需要 {min_role} 及以上权限（当前 {role}）"`
- 成功返回实际 role 字符串。

### `resolve_group_role(user_id, group_id, db)`（`permission_deps.py`）
- 不处理 is_admin（职责分离，is_admin 在 require_group_role 里处理）。
- 沿 `Group.parent_group_id` 向上 BFS，`while cur_gid and cur_gid not in visited`，`depth >= 3` break，每层 `select(GroupMember.role).filter_by(user_id, group_id=cur_gid)`，用 `_pick_higher` 取链上最高。返回最高 role 或 None。

### `log_audit`（`audit/logger.py`）
- 仅 `db.add()`，不 commit；caller 负责事务。
- 失败 `logger.warning(...)`，不抛错。
- `metadata` 序列化 `json.dumps(metadata or {}, ensure_ascii=False)`；`resource_id` 强转 `str()`。

### `_expand_group_and_descendants`（`audit_router.py` L156-200，内部）
- BFS 向下，`range(3)` 最多 3 层，含自身，返回 `set[str]`。
- 与 `permission_deps._expand_user_groups` 类似，但不过滤用户成员关系，纯展开 group 树。

### `_validate_page_limit`（`audit_router.py` L138-153，内部）
- `page = max(1, page)`；`limit = min(max(1, limit), 200)`，静默 clamp 不抛错（Query 层 ge/le 已校验，双重防护）。

---

相关源码文件（绝对路径）：
- `/Users/java/knowledge-engineering/src/service/audit_router.py`
- `/Users/java/knowledge-engineering/src/service/auth_dependencies.py`（L33-47 完整 401 分支）
- `/Users/java/knowledge-engineering/src/service/deps_infra.py`（L56-74 unhealthy 判定 + admin deps）
- `/Users/java/knowledge-engineering/src/service/admin_router.py`（L61-65 require_admin）
- `/Users/java/knowledge-engineering/src/service/permission_deps.py`
- `/Users/java/knowledge-engineering/src/service/audit/logger.py`、`/Users/java/knowledge-engineering/src/service/audit/actions.py`
- `/Users/java/knowledge-engineering/src/service/db_models_groups.py`（L202-267 AuditLog ORM）
- `/Users/java/ke-server/packages/store/src/schema/schema.ts`（L14-28 audit_logs drizzle）