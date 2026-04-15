"""DAO SQL 提取插件 — 从各类 DAO 框架中解析 SQL 并关联到方法实体。

支持的框架（插件化）：
- mybatis_xml: MyBatis XML Mapper（已实现）
- mybatis_annotation: MyBatis @Select/@Insert 注解（待实现）
- jpa: JPA/Hibernate @Query（待实现）
- spring_jdbc: Spring JdbcTemplate（待实现）
"""
from .protocol import DaoSqlPlugin, DaoSqlResult
from .registry import (
    register_dao_sql_plugin,
    get_dao_sql_plugin,
    auto_detect_plugin,
    registered_dao_sql_plugins,
    load_dao_sql_for_repo,
)

__all__ = [
    "DaoSqlPlugin",
    "DaoSqlResult",
    "register_dao_sql_plugin",
    "get_dao_sql_plugin",
    "auto_detect_plugin",
    "registered_dao_sql_plugins",
    "load_dao_sql_for_repo",
]
