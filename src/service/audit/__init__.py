"""v2.0 audit 子包。

本包提供审计日志的写入入口和动作常量，供各路由 / service 层调用。

子模块：
  - actions  : 审计动作字符串常量（如 'project.create'）
  - logger   : 统一写入入口函数 log_audit()

使用示例：
    from src.service.audit.logger import log_audit
    from src.service.audit import actions

    await log_audit(db, actor_user_id=user.id, action=actions.PROJECT_CREATE, ...)
"""
# 子包 __init__.py 可为空，但必须存在才能让 Python 识别此目录为包（package）
# Python 包规则：每个目录下有 __init__.py → 该目录可作为 import 的模块来源
