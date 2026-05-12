"""审计动作常量。

命名规范：resource_type.verb（全小写，下划线连接）。
  - resource_type 与 AuditLog.resource_type 字段值一致
  - 常量名大写（Python 惯例：模块级常量用 UPPER_CASE 命名）

分类：
  - auth.*        : 认证相关（登录/登出/密码修改）
  - user.*        : 用户管理（创建/更新/删除/激活/停用）
  - group.*       : Group（租户/团队）管理
  - group_member.* : Group 成员管理
  - project.*     : 工程管理
  - project_member.* : 工程成员管理
  - credential.*  : Git 凭证管理
  - message.*     : 消息/对话相关（导出等）

使用方式：
    from src.service.audit import actions

    action_str = actions.PROJECT_CREATE   # → "project.create"
"""

# ─── 认证（auth.*） ────────────────────────────────────────────────────────────
# 登录成功：记录 IP、时间等；可用于异常登录检测
AUTH_LOGIN_SUCCESS = "auth.login_success"

# 登录失败：记录失败原因（密码错/账号不存在），可用于暴力破解检测
AUTH_LOGIN_FAILURE = "auth.login_failure"

# 登出：用户主动登出
AUTH_LOGOUT = "auth.logout"

# 密码修改：安全敏感操作，必须审计
AUTH_PASSWORD_CHANGE = "auth.password_change"

# ─── 用户（user.*） ────────────────────────────────────────────────────────────
# 创建用户（管理员操作）
USER_CREATE = "user.create"

# 更新用户信息（邮箱、用户名等）
USER_UPDATE = "user.update"

# 删除用户
USER_DELETE = "user.delete"

# 设置管理员权限（授予 is_admin=True）
USER_SET_ADMIN = "user.set_admin"

# 激活用户账号（is_active=True）
USER_ACTIVATE = "user.activate"

# 停用用户账号（is_active=False，不删除）
USER_DEACTIVATE = "user.deactivate"

# ─── Group（group.*） ─────────────────────────────────────────────────────────
# 创建 Group（租户/团队）
GROUP_CREATE = "group.create"

# 更新 Group 信息（名称/描述等）
GROUP_UPDATE = "group.update"

# 删除 Group
GROUP_DELETE = "group.delete"

# ─── Group 成员（group_member.*） ─────────────────────────────────────────────
# 添加 Group 成员
GROUP_MEMBER_ADD = "group_member.add"

# 移除 Group 成员
GROUP_MEMBER_REMOVE = "group_member.remove"

# 变更 Group 成员角色（如 member → admin）
GROUP_MEMBER_ROLE_CHANGE = "group_member.role_change"

# ─── 工程（project.*） ────────────────────────────────────────────────────────
# 创建工程
PROJECT_CREATE = "project.create"

# 更新工程信息
PROJECT_UPDATE = "project.update"

# 删除工程
PROJECT_DELETE = "project.delete"

# 触发重建索引（Reindex）操作
PROJECT_REINDEX_TRIGGER = "project.reindex_trigger"

# ─── 工程成员（project_member.*） ─────────────────────────────────────────────
# 添加工程成员
PROJECT_MEMBER_ADD = "project_member.add"

# 移除工程成员
PROJECT_MEMBER_REMOVE = "project_member.remove"

# 变更工程成员角色（如 reporter → editor）
PROJECT_MEMBER_ROLE_CHANGE = "project_member.role_change"

# ─── 凭证（credential.*） ─────────────────────────────────────────────────────
# 创建 Git 凭证
CREDENTIAL_CREATE = "credential.create"

# 删除 Git 凭证
CREDENTIAL_DELETE = "credential.delete"

# ─── 消息/导出（message.*） ───────────────────────────────────────────────────
# 将对话/消息导出为 Word（.docx）文件
MESSAGE_EXPORT_DOCX = "message.export_docx"
