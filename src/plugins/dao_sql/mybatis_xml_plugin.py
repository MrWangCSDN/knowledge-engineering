"""MyBatis XML Mapper 插件 — 解析 *Mapper.xml / *Dao.xml 中的 SQL。

将 MyBatis 动态 SQL 标签转为带中文注释的纯 SQL：
  <if test="name!=null"> AND name=#{name} </if>
  → -- [条件] 当 name!=null 时:
       AND name=#{name}
"""
from __future__ import annotations

import glob
import logging
import os
import re
import xml.etree.ElementTree as ET
from typing import Any

from .protocol import DaoSqlPlugin, DaoSqlResult

_LOG = logging.getLogger(__name__)

# 表名提取正则
_TABLE_PATTERN = re.compile(
    r'\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+[`]?(\w+)[`]?',
    re.IGNORECASE,
)


class MyBatisXmlPlugin:
    """MyBatis XML Mapper 插件。"""

    @property
    def name(self) -> str:
        return "mybatis_xml"

    def detect(self, repo_path: str) -> bool:
        """检测是否有 MyBatis XML 文件。"""
        for pattern in ["**/*Mapper.xml", "**/*Dao.xml"]:
            matches = glob.glob(os.path.join(repo_path, pattern), recursive=True)
            # 排除 target/build 目录
            matches = [m for m in matches if "/target/" not in m and "/build/" not in m]
            if matches:
                return True
        return False

    def extract_all(self, repo_path: str, config: dict[str, Any]) -> dict[str, DaoSqlResult]:
        """解析所有 MyBatis XML，提取 SQL 并转为带注释格式。"""
        results: dict[str, DaoSqlResult] = {}

        # 收集所有 XML 文件
        globs = []
        mapper_glob = config.get("mapper_glob", "**/mapper/*Mapper.xml")
        dao_glob = config.get("dao_glob", "**/dao/*Dao.xml")
        globs.append(mapper_glob)
        globs.append(dao_glob)

        xml_files: set[str] = set()
        for pattern in globs:
            matches = glob.glob(os.path.join(repo_path, pattern), recursive=True)
            for m in matches:
                if "/target/" not in m and "/build/" not in m:
                    xml_files.add(m)

        _LOG.info("MyBatis XML: 找到 %d 个文件", len(xml_files))

        for xml_path in sorted(xml_files):
            try:
                file_results = self._parse_xml_file(xml_path, repo_path)
                results.update(file_results)
            except Exception as e:
                _LOG.debug("解析 %s 失败: %s", xml_path, e)

        return results

    def _parse_xml_file(self, xml_path: str, repo_path: str) -> dict[str, DaoSqlResult]:
        """解析单个 MyBatis XML 文件。"""
        results: dict[str, DaoSqlResult] = {}

        tree = ET.parse(xml_path)
        root = tree.getroot()
        namespace = root.get("namespace", "")
        rel_path = os.path.relpath(xml_path, repo_path)

        for tag in ("select", "insert", "update", "delete"):
            for elem in root.iter(tag):
                stmt_id = elem.get("id", "")
                if not stmt_id:
                    continue

                # 转为带注释的 SQL
                annotated_sql = self._element_to_annotated_sql(elem)

                # 原始 XML
                raw_xml = ET.tostring(elem, encoding="unicode", method="xml").strip()

                # 提取表名
                plain_text = "".join(elem.itertext())
                tables = list(set(_TABLE_PATTERN.findall(plain_text)))

                full_key = f"{namespace}.{stmt_id}" if namespace else stmt_id
                results[full_key] = DaoSqlResult(
                    method_name=stmt_id,
                    namespace=namespace,
                    sql_type=tag.upper(),
                    annotated_sql=annotated_sql,
                    tables=tables,
                    raw_xml=raw_xml,
                    source_file=rel_path,
                )

        return results

    def _element_to_annotated_sql(self, elem: ET.Element, indent: int = 0) -> str:
        """将 MyBatis XML 元素递归转为带注释的 SQL。

        转换规则:
          <if test="x!=null">         → -- [条件] 当 x!=null 时:
          <foreach collection="list"> → -- [循环] 遍历 list:
          <where>                     → WHERE
          <set>                       → SET
          <choose>                    → -- [分支选择]
          <when test="x">             → -- [当 x]:
          <otherwise>                 → -- [否则]:
          <trim>                      → (透传内容)
          <include>                   → -- [引用] refid
        """
        parts: list[str] = []
        prefix = "  " * indent

        # 元素自身的文本
        if elem.text and elem.text.strip():
            for line in elem.text.strip().split("\n"):
                line = line.strip()
                if line:
                    parts.append(f"{prefix}{line}")

        # 子元素
        for child in elem:
            tag = child.tag
            if tag == "if":
                test = child.get("test", "")
                parts.append(f"{prefix}-- [条件] 当 {test} 时:")
                child_sql = self._element_to_annotated_sql(child, indent + 1)
                if child_sql.strip():
                    parts.append(child_sql)

            elif tag == "foreach":
                collection = child.get("collection", "?")
                item = child.get("item", "item")
                separator = child.get("separator", "")
                open_str = child.get("open", "")
                close_str = child.get("close", "")
                desc = f"遍历 {collection} 中的每个 {item}"
                if separator:
                    desc += f"，以 '{separator}' 分隔"
                if open_str or close_str:
                    desc += f"，包裹在 {open_str}...{close_str} 中"
                parts.append(f"{prefix}-- [循环] {desc}:")
                child_sql = self._element_to_annotated_sql(child, indent + 1)
                if child_sql.strip():
                    parts.append(child_sql)

            elif tag == "where":
                parts.append(f"{prefix}WHERE")
                child_sql = self._element_to_annotated_sql(child, indent + 1)
                if child_sql.strip():
                    parts.append(child_sql)

            elif tag == "set":
                parts.append(f"{prefix}SET")
                child_sql = self._element_to_annotated_sql(child, indent + 1)
                if child_sql.strip():
                    parts.append(child_sql)

            elif tag == "choose":
                parts.append(f"{prefix}-- [分支选择]:")
                child_sql = self._element_to_annotated_sql(child, indent + 1)
                if child_sql.strip():
                    parts.append(child_sql)

            elif tag == "when":
                test = child.get("test", "")
                parts.append(f"{prefix}-- [当 {test}]:")
                child_sql = self._element_to_annotated_sql(child, indent + 1)
                if child_sql.strip():
                    parts.append(child_sql)

            elif tag == "otherwise":
                parts.append(f"{prefix}-- [否则]:")
                child_sql = self._element_to_annotated_sql(child, indent + 1)
                if child_sql.strip():
                    parts.append(child_sql)

            elif tag == "trim":
                # trim 只是格式化容器，透传内容
                prefix_attr = child.get("prefix", "")
                suffix_attr = child.get("suffix", "")
                if prefix_attr:
                    parts.append(f"{prefix}{prefix_attr}")
                child_sql = self._element_to_annotated_sql(child, indent)
                if child_sql.strip():
                    parts.append(child_sql)
                if suffix_attr:
                    parts.append(f"{prefix}{suffix_attr}")

            elif tag == "include":
                refid = child.get("refid", "?")
                parts.append(f"{prefix}-- [引用 SQL 片段] {refid}")

            elif tag == "selectKey":
                key_prop = child.get("keyProperty", "")
                parts.append(f"{prefix}-- [主键生成] {key_prop}")
                child_sql = self._element_to_annotated_sql(child, indent + 1)
                if child_sql.strip():
                    parts.append(child_sql)

            elif tag == "bind":
                name = child.get("name", "")
                value = child.get("value", "")
                parts.append(f"{prefix}-- [变量绑定] {name} = {value}")

            else:
                # 未知标签：提取文本内容
                if child.text and child.text.strip():
                    parts.append(f"{prefix}{child.text.strip()}")

            # 尾部文本（XML 中的 tail text）
            if child.tail and child.tail.strip():
                for line in child.tail.strip().split("\n"):
                    line = line.strip()
                    if line:
                        parts.append(f"{prefix}{line}")

        return "\n".join(p for p in parts if p.strip())
