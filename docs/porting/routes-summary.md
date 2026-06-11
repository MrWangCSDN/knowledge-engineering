# 路由清单（自动生成，勿手改）

来源：tag `py-final-baseline`，共 46 个 path。
TS 移植以本表盘点覆盖率；6 段式专属路由按决策 #6 标记不迁。

| Method | Path | Summary |
|---|---|---|
| GET | `/admin/audit-logs` | List Admin Audit Logs |
| GET | `/admin/credentials` | List Credentials |
| DELETE | `/admin/credentials/{credential_id}` | Delete Credential |
| GET | `/admin/projects` | List Admin Projects |
| POST | `/admin/projects` | Create Admin Project |
| POST | `/admin/projects/test-connection` | Test Connection |
| DELETE | `/admin/projects/{project_id}` | Delete Admin Project |
| PATCH | `/admin/projects/{project_id}` | Update Admin Project |
| GET | `/admin/users` | List Users |
| POST | `/admin/users` | Create User |
| DELETE | `/admin/users/{uid}` | Delete User |
| PATCH | `/admin/users/{uid}` | Update User |
| POST | `/auth/login` | Login |
| POST | `/auth/logout` | Logout |
| GET | `/auth/me` | Me |
| PATCH | `/auth/me/model` | Update Preferred Model |
| POST | `/auth/refresh` | Refresh |
| GET | `/calls/callees` | Get Callees |
| GET | `/calls/callers` | Get Callers |
| GET | `/credentials` | List My Credentials |
| POST | `/credentials` | Create My Credential |
| DELETE | `/credentials/{cred_id}` | Delete My Credential |
| GET | `/doc/domain/{domain_id}` | Doc Domain |
| GET | `/doc/generate` | Doc Generate |
| GET | `/doc/service/{service_id}` | Doc Service |
| GET | `/groups` | List Visible Groups |
| POST | `/groups` | Create Group |
| DELETE | `/groups/{group_id}` | Delete Group |
| GET | `/groups/{group_id}` | Get Group |
| PATCH | `/groups/{group_id}` | Update Group |
| GET | `/groups/{group_id}/audit-logs` | List Group Audit Logs |
| GET | `/groups/{group_id}/members` | List Group Members |
| POST | `/groups/{group_id}/members` | Add Group Member |
| DELETE | `/groups/{group_id}/members/{user_id}` | Remove Group Member |
| PATCH | `/groups/{group_id}/members/{user_id}` | Update Member Role |
| GET | `/health` | Health |
| GET | `/impact` | Impact |
| POST | `/knowledge/load_snapshot` | Load Snapshot |
| GET | `/projects` | List Projects |
| POST | `/projects` | Create Project |
| GET | `/projects/{project_id}` | Get Project |
| GET | `/projects/{project_id}/code-snippet` | Get Code Snippet |
| POST | `/projects/{project_id}/code/resolve-symbol` | Resolve Symbol |
| GET | `/projects/{project_id}/members` | List Project Members |
| POST | `/projects/{project_id}/members` | Add Project Member |
| DELETE | `/projects/{project_id}/members/{user_id}` | Remove Project Member |
| PATCH | `/projects/{project_id}/members/{user_id}` | Update Member Role |
| POST | `/projects/{project_id}/qa/explain` | Explain |
| GET | `/projects/{project_id}/qa/sessions` | List Sessions |
| DELETE | `/projects/{project_id}/qa/sessions/{session_id}` | Delete Session |
| GET | `/projects/{project_id}/qa/sessions/{session_id}` | Get Session Detail |
| PATCH | `/projects/{project_id}/qa/sessions/{session_id}` | Rename Session |
| POST | `/projects/{project_id}/qa/sessions/{session_id}/archive` | Archive Session |
| GET | `/projects/{project_id}/qa/sessions/{session_id}/messages/{message_id}/export` | Export Message |
| POST | `/projects/{project_id}/qa/sessions/{session_id}/messages/{message_id}/feedback` | Post Feedback |
| POST | `/projects/{project_id}/qa/sessions/{session_id}/unarchive` | Unarchive Session |
| GET | `/qa` | Qa |
| GET | `/search` | Search |
| GET | `/stats` | Stats |
| GET | `/subgraph/service/{service_id}` | Subgraph Service |
| GET | `/user/archived-sessions` | List Archived Sessions |
