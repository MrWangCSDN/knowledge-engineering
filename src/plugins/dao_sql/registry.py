"""DAO SQL 插件注册中心 — 遵循 LLM factory 的注册模式。"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .protocol import DaoSqlPlugin, DaoSqlResult

_LOG = logging.getLogger(__name__)

# 插件注册表
_DAO_SQL_PLUGINS: dict[str, DaoSqlPlugin] = {}


def register_dao_sql_plugin(name: str, plugin: DaoSqlPlugin) -> None:
    """注册一个 DAO SQL 插件。"""
    key = (name or "").strip().lower()
    _DAO_SQL_PLUGINS[key] = plugin
    _LOG.debug("注册 DAO SQL 插件: %s", key)


def get_dao_sql_plugin(name: str) -> Optional[DaoSqlPlugin]:
    """按名称获取插件。"""
    return _DAO_SQL_PLUGINS.get((name or "").strip().lower())


def auto_detect_plugin(repo_path: str) -> Optional[DaoSqlPlugin]:
    """自动检测项目使用的 DAO 框架，返回匹配的插件。"""
    for plugin in _DAO_SQL_PLUGINS.values():
        try:
            if plugin.detect(repo_path):
                _LOG.info("自动检测到 DAO 框架: %s", plugin.name)
                return plugin
        except Exception as e:
            _LOG.debug("插件 %s detect 失败: %s", plugin.name, e)
    return None


def registered_dao_sql_plugins() -> list[str]:
    """列出所有已注册的插件名。"""
    return list(_DAO_SQL_PLUGINS.keys())


def load_dao_sql_for_repo(
    repo_path: str,
    config: dict[str, Any],
) -> dict[str, DaoSqlResult]:
    """为项目加载 DAO SQL。

    自动选择插件：
    1. 如果配置了 dao_framework → 使用指定插件
    2. 否则 auto detect → 使用第一个匹配的
    3. 都没有 → 返回空

    Args:
        repo_path: 仓库根路径
        config: project.yaml 的 schema 段

    Returns:
        {full_method_key → DaoSqlResult}
    """
    framework = (config.get("dao_framework") or "auto").strip().lower()

    if framework == "none":
        return {}

    plugin: Optional[DaoSqlPlugin] = None
    if framework != "auto":
        plugin = get_dao_sql_plugin(framework)
        if not plugin:
            _LOG.warning("未找到 DAO 插件 '%s'，可用: %s", framework, registered_dao_sql_plugins())
    else:
        plugin = auto_detect_plugin(repo_path)

    if not plugin:
        _LOG.info("未检测到 DAO 框架，跳过 SQL 提取")
        return {}

    _LOG.info("使用 DAO 插件: %s", plugin.name)
    try:
        results = plugin.extract_all(repo_path, config)
        _LOG.info("DAO SQL 提取完成: %d 条", len(results))
        return results
    except Exception as e:
        _LOG.warning("DAO SQL 提取失败: %s", e)
        return {}


# ===== 默认插件注册 =====
def _install_default_plugins():
    """注册内置插件。延迟导入避免循环依赖。"""
    try:
        from .mybatis_xml_plugin import MyBatisXmlPlugin
        register_dao_sql_plugin("mybatis_xml", MyBatisXmlPlugin())
    except ImportError:
        pass

_install_default_plugins()
