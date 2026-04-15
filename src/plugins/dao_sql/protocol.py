"""DAO SQL 提取插件协议 — 所有 DAO 框架插件必须实现此接口。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class DaoSqlResult:
    """一个 DAO 方法对应的 SQL 提取结果。"""
    method_name: str                    # XML 中的 id，如 "getList"
    namespace: str                      # 完整命名空间，如 "com.macro.mall.dao.OmsOrderDao"
    sql_type: str                       # SELECT / INSERT / UPDATE / DELETE
    annotated_sql: str                  # 带注释的 SQL（动态条件转中文注释）
    tables: list[str] = field(default_factory=list)   # 涉及的表名
    raw_xml: str = ""                   # 原始 XML 片段（备用）
    source_file: str = ""               # XML 文件路径

    @property
    def full_key(self) -> str:
        """完整方法键：namespace.methodName"""
        return f"{self.namespace}.{self.method_name}" if self.namespace else self.method_name

    @property
    def class_simple_name(self) -> str:
        """从 namespace 提取简单类名：com.macro.mall.dao.OmsOrderDao → OmsOrderDao"""
        if not self.namespace:
            return ""
        return self.namespace.rsplit(".", 1)[-1]


@runtime_checkable
class DaoSqlPlugin(Protocol):
    """DAO SQL 提取插件接口。

    每种 DAO 框架实现一个插件：
    - mybatis_xml: 解析 *Mapper.xml / *Dao.xml
    - mybatis_annotation: 解析 @Select/@Insert 注解
    - jpa: 解析 @Query + 方法名推导
    - spring_jdbc: 解析 JdbcTemplate 调用
    """

    @property
    def name(self) -> str:
        """插件名称，用于注册和配置匹配。"""
        ...

    def detect(self, repo_path: str) -> bool:
        """自动检测项目是否使用此 DAO 框架。

        Args:
            repo_path: 代码仓库根路径

        Returns:
            True 如果检测到该框架的特征文件
        """
        ...

    def extract_all(self, repo_path: str, config: dict[str, Any]) -> dict[str, DaoSqlResult]:
        """提取所有 SQL。

        Args:
            repo_path: 代码仓库根路径
            config: 来自 project.yaml 的 schema 配置段

        Returns:
            {full_method_key → DaoSqlResult} 映射
            key 格式: "com.macro.mall.dao.OmsOrderDao.getList"
        """
        ...
