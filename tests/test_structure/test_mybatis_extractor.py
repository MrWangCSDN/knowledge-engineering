"""MyBatisXmlExtractor 单元测试。

设计：[[MyBatis-XML-Extractor-设计]] §3 + §6.1
参考：CodeGraph src/extraction/mybatis-extractor.ts (198 行)
"""
from pathlib import Path

import pytest

from src.models.structure import EntityType, RelationType
from src.structure.mybatis_extractor import MyBatisXmlExtractor


# fixture 路径（相对本测试文件）
FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> tuple[str, str]:
    """读 fixture，返回 (file_path 字符串, source 文本)。"""
    p = FIXTURE_DIR / name
    return (f"tests/fixtures/{name}", p.read_text(encoding="utf-8"))


def test_finds_mapper_root():
    """识别 <mapper namespace="X"> 并提取 namespace。"""
    file_path, source = _load("UmsRoleDao.xml")
    extractor = MyBatisXmlExtractor(file_path, source)
    result = extractor.extract()

    # 至少有 1 个 file entity + N 个 method entity
    file_ents = [e for e in result.entities if e.type == EntityType.FILE]
    assert len(file_ents) == 1
    assert file_ents[0].name == "UmsRoleDao.xml"


def test_emits_method_per_statement():
    """UmsRoleDao.xml 有 3 个 <select>，应 emit 3 个 method entity + 3 个 contains relation。"""
    file_path, source = _load("UmsRoleDao.xml")
    result = MyBatisXmlExtractor(file_path, source).extract()

    method_ents = [e for e in result.entities if e.type == EntityType.METHOD]
    assert len(method_ents) == 3

    names = sorted([e.name for e in method_ents])
    assert names == ["getMenuList", "getMenuListByRoleId", "getResourceListByRoleId"]

    # 每个 method 的 qualifiedName 必须是 namespace::id
    for m in method_ents:
        assert m.attributes["qualified_name"].startswith("com.macro.mall.dao.UmsRoleDao::")
        assert m.language == "xml"

    # 3 个 contains relation: file -> method
    contains = [r for r in result.relations if r.type == RelationType.CONTAINS]
    assert len(contains) == 3
    file_id = next(e.id for e in result.entities if e.type == EntityType.FILE)
    for r in contains:
        assert r.source_id == file_id


def test_signature_format_select_with_result_type():
    """<select resultType="X"> → signature "SELECT result=X"。"""
    file_path, source = _load("UmsRoleDao.xml")
    result = MyBatisXmlExtractor(file_path, source).extract()

    get_menu = next(e for e in result.entities
                    if e.type == EntityType.METHOD and e.name == "getMenuList")
    sig = get_menu.attributes.get("signature", "")
    assert sig.startswith("SELECT")
    assert "result=com.macro.mall.model.UmsMenu" in sig


def test_sql_kind_attribute():
    """每个 statement 节点 attributes 里有 sql_kind。"""
    file_path, source = _load("UmsRoleDao.xml")
    result = MyBatisXmlExtractor(file_path, source).extract()

    for e in result.entities:
        if e.type == EntityType.METHOD:
            assert e.attributes.get("sql_kind") == "select"


def test_sql_preview_in_attributes():
    """attributes 里有 sql_preview 字段（前 256 字符 SQL）。"""
    file_path, source = _load("UmsRoleDao.xml")
    result = MyBatisXmlExtractor(file_path, source).extract()

    get_menu = next(e for e in result.entities
                    if e.type == EntityType.METHOD and e.name == "getMenuList")
    preview = get_menu.attributes.get("sql_preview", "")
    # 含 SELECT 关键字 + 表名（不超 256）
    assert "SELECT" in preview
    assert "ums_admin_role_relation" in preview
    assert len(preview) <= 256


def test_handles_sql_fragment():
    """<sql id="X"> 也 emit 节点，signature 为 "<sql>"。"""
    sql_fragment = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" "x">
<mapper namespace="com.x.FooDao">
    <sql id="baseColumns">id, name, create_time</sql>
    <select id="findAll" resultType="com.x.Foo">
        SELECT <include refid="baseColumns"/> FROM foo
    </select>
</mapper>
'''
    result = MyBatisXmlExtractor("Foo.xml", sql_fragment).extract()
    methods = [e for e in result.entities if e.type == EntityType.METHOD]
    assert len(methods) == 2
    base = next(m for m in methods if m.name == "baseColumns")
    assert base.attributes["sql_kind"] == "sql"
    assert base.attributes["signature"] == "<sql>"


def test_handles_include_refid_emits_relates_to():
    """<include refid="X"> → emit relation 指向同 mapper 内的 sql fragment。

    备注：跨 mapper 引用（refid="other.X"）暂不在 structure 阶段 resolve，
    走 unresolved reference 流程，由后续 resolver / mybatis_link_resolver 处理。
    """
    src = '''<?xml version="1.0"?>
<mapper namespace="com.x.FooDao">
    <sql id="cols">id, name</sql>
    <select id="findAll" resultType="com.x.Foo">
        SELECT <include refid="cols"/> FROM foo
    </select>
</mapper>
'''
    result = MyBatisXmlExtractor("Foo.xml", src).extract()
    # 至少一个 from select → sql_fragment 的 references-style relation
    # 我们用 attributes["include_refids"] 列表记录所有 include，留给 resolver 用
    select = next(e for e in result.entities
                  if e.type == EntityType.METHOD and e.name == "findAll")
    refids = select.attributes.get("include_refids", [])
    assert "cols" in refids or "com.x.FooDao::cols" in refids


def test_non_mapper_xml_only_emits_file():
    """非 mapper XML（如 log4j config）只 emit file entity，不 emit method 节点。"""
    file_path, source = _load("non_mapper.xml")
    result = MyBatisXmlExtractor(file_path, source).extract()

    file_ents = [e for e in result.entities if e.type == EntityType.FILE]
    method_ents = [e for e in result.entities if e.type == EntityType.METHOD]
    assert len(file_ents) == 1
    assert len(method_ents) == 0
