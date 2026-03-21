"""知识层图存储：Neo4j 后端。与 KnowledgeGraph 的图操作接口对齐，供 build_from 与查询使用。"""
from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence

# Neo4j 关系类型：仅允许字母数字下划线，将小写与特殊字符转为大写
def _rel_type(rel: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", rel or "")
    return (s or "REL").upper()


class Neo4jGraphBackend:
    """Neo4j 图后端：节点标签 Entity，属性 id/entity_type/name/location/module_id；关系类型为 rel_type 值。"""

    LABEL = "Entity"

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j"):
        self._uri = uri
        self._user = user
        self._password = password
        self._database = database
        self._driver = None
        self._ensure_driver()

    def _ensure_driver(self) -> None:
        import neo4j
        self._driver = neo4j.GraphDatabase.driver(self._uri, auth=(self._user, self._password))

    def close(self) -> None:
        if self._driver:
            try:
                self._driver.close()
            except Exception:
                pass
            self._driver = None

    def clear(self) -> None:
        with self._driver.session(database=self._database) as session:
            session.run("MATCH (n:" + self.LABEL + ") DETACH DELETE n")

    def add_node(self, nid: str, **attrs: Any) -> None:
        safe_id = nid.replace("'", "\\'")
        props = ", ".join(f"n.{k} = ${k}" for k in attrs if attrs[k] is not None)
        params = {"id": nid, **attrs}
        with self._driver.session(database=self._database) as session:
            session.run(
                f"MERGE (n:{self.LABEL} {{id: $id}}) SET n += $attrs",
                {"id": nid, "attrs": {k: v for k, v in attrs.items() if v is not None}},
            )

    def add_edge(self, source_id: str, target_id: str, rel_type: str, **attrs: Any) -> None:
        rtype = _rel_type(rel_type)
        with self._driver.session(database=self._database) as session:
            session.run(
                f"""
                MERGE (a:{self.LABEL} {{id: $sid}})
                MERGE (b:{self.LABEL} {{id: $tid}})
                CREATE (a)-[r:{rtype}]->(b)
                SET r += $attrs
                """,
                sid=source_id,
                tid=target_id,
                attrs={"rel_type": rel_type, **{k: v for k, v in attrs.items() if v is not None}},
            )

    def has_node(self, nid: str) -> bool:
        with self._driver.session(database=self._database) as session:
            r = session.run(f"MATCH (n:{self.LABEL} {{id: $id}}) RETURN n", id=nid)
            return r.single() is not None

    def get_node(self, nid: str) -> Optional[dict]:
        with self._driver.session(database=self._database) as session:
            r = session.run(f"MATCH (n:{self.LABEL} {{id: $id}}) RETURN n", id=nid)
            rec = r.single()
            if rec is None:
                return None
            n = rec["n"]
            out = dict(n) if hasattr(n, "keys") else {}
            out["id"] = nid
            return out

    def successors(self, nid: str, rel_type: Optional[str] = None) -> list[str]:
        if rel_type:
            # 兼容：① 属性 r.rel_type 与结构层一致（小写 calls 等）；
            # ② 仅存在 Neo4j 关系「类型」CREATE ...-[r:CALLS]->... 而未写入 r.rel_type 的旧数据。
            rlabel = _rel_type(rel_type)
            q = (
                f"MATCH (a:{self.LABEL} {{id: $id}})-[r]->(b:{self.LABEL}) "
                f"WHERE r.rel_type = $rel_type OR type(r) = '{rlabel}' "
                f"RETURN b.id AS bid"
            )
        else:
            q = f"MATCH (a:{self.LABEL} {{id: $id}})-[r]->(b:{self.LABEL}) RETURN b.id AS bid"
        with self._driver.session(database=self._database) as session:
            r = session.run(q, id=nid, rel_type=rel_type)
            return [rec["bid"] for rec in r if rec.get("bid")]

    def successors_excluding_rel_types(
        self, nid: str, exclude_rel_types: Sequence[str]
    ) -> list[str]:
        raw = [str(x).strip() for x in exclude_rel_types if str(x).strip()]
        ex_lower = [x.lower() for x in raw]
        if not ex_lower:
            return self.successors(nid, rel_type=None)
        neo_types = list(dict.fromkeys(_rel_type(x) for x in raw))
        q = (
            f"MATCH (a:{self.LABEL} {{id: $id}})-[r]->(b:{self.LABEL}) "
            f"WHERE NOT (toLower(trim(toString(coalesce(r.rel_type, '')))) IN $ex_lower) "
            f"AND NOT (type(r) IN $neo_types) "
            f"RETURN DISTINCT b.id AS bid"
        )
        with self._driver.session(database=self._database) as session:
            r = session.run(q, id=nid, ex_lower=ex_lower, neo_types=neo_types)
            return [rec["bid"] for rec in r if rec.get("bid")]

    def predecessors(self, nid: str, rel_type: Optional[str] = None) -> list[str]:
        if rel_type:
            rlabel = _rel_type(rel_type)
            q = (
                f"MATCH (a:{self.LABEL})-[r]->(b:{self.LABEL} {{id: $id}}) "
                f"WHERE r.rel_type = $rel_type OR type(r) = '{rlabel}' "
                f"RETURN a.id AS aid"
            )
        else:
            q = f"MATCH (a:{self.LABEL})-[r]->(b:{self.LABEL} {{id: $id}}) RETURN a.id AS aid"
        with self._driver.session(database=self._database) as session:
            r = session.run(q, id=nid, rel_type=rel_type)
            return [rec["aid"] for rec in r if rec.get("aid")]

    def predecessors_excluding_rel_types(
        self, nid: str, exclude_rel_types: Sequence[str]
    ) -> list[str]:
        raw = [str(x).strip() for x in exclude_rel_types if str(x).strip()]
        ex_lower = [x.lower() for x in raw]
        if not ex_lower:
            return self.predecessors(nid, rel_type=None)
        neo_types = list(dict.fromkeys(_rel_type(x) for x in raw))
        q = (
            f"MATCH (a:{self.LABEL})-[r]->(b:{self.LABEL} {{id: $id}}) "
            f"WHERE NOT (toLower(trim(toString(coalesce(r.rel_type, '')))) IN $ex_lower) "
            f"AND NOT (type(r) IN $neo_types) "
            f"RETURN DISTINCT a.id AS aid"
        )
        with self._driver.session(database=self._database) as session:
            r = session.run(q, id=nid, ex_lower=ex_lower, neo_types=neo_types)
            return [rec["aid"] for rec in r if rec.get("aid")]

    def node_count(self) -> int:
        with self._driver.session(database=self._database) as session:
            r = session.run(f"MATCH (n:{self.LABEL}) RETURN count(n) AS c")
            rec = r.single()
            return rec["c"] or 0

    def edge_count(self) -> int:
        with self._driver.session(database=self._database) as session:
            r = session.run(f"MATCH (a:{self.LABEL})-[r]->(b:{self.LABEL}) RETURN count(r) AS c")
            rec = r.single()
            return rec["c"] or 0

    def all_node_ids(self) -> list[str]:
        with self._driver.session(database=self._database) as session:
            r = session.run(f"MATCH (n:{self.LABEL}) RETURN n.id AS id")
            return [rec["id"] for rec in r if rec.get("id")]

    def impact_closure(self, start_id: str, direction: str = "down", max_depth: int = 50) -> set[str]:
        """沿**全部关系类型**的有向边扩展（successors/predecessors 不限制 rel_type），含 IMPLEMENTS 等。"""
        seen: set[str] = set()
        stack = [start_id]
        depth = 0
        while stack and depth < max_depth:
            depth += 1
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            next_ids = self.successors(nid) if direction == "down" else self.predecessors(nid)
            for k in next_ids:
                if k not in seen:
                    stack.append(k)
        return seen

    def out_edges_with_rel(self, nid: str) -> list[tuple[str, dict]]:
        with self._driver.session(database=self._database) as session:
            r = session.run(
                f"MATCH (a:{self.LABEL} {{id: $id}})-[r]->(b:{self.LABEL}) RETURN b.id AS bid, r.rel_type AS rel_type",
                id=nid,
            )
            return [(rec["bid"], {"rel_type": rec.get("rel_type") or ""}) for rec in r if rec.get("bid")]

    def query_direct_callees(self, class_name: str, method_name: str) -> list[dict]:
        """
        给定类名+方法名，在 Neo4j 中查找该方法直接调用的其他方法列表。
        返回 [{"class_name": str, "method_name": str}, ...]。
        """
        with self._driver.session(database=self._database) as session:
            r = session.run(
                f"""
                MATCH (a:{self.LABEL})
                WHERE toLower(coalesce(a.entity_type, '')) = 'method' AND a.class_name = $class_name AND a.name = $method_name
                MATCH (a)-[r:CALLS]->(b:{self.LABEL})
                RETURN DISTINCT b.class_name AS class_name, b.name AS method_name
                """,
                class_name=class_name,
                method_name=method_name,
            )
            out = []
            seen: set[tuple[str, str]] = set()
            for rec in r:
                cname = (rec.get("class_name") or "").strip()
                mname = (rec.get("method_name") or "").strip()
                if (cname, mname) in seen:
                    continue
                seen.add((cname, mname))
                out.append({"class_name": cname, "method_name": mname})
            return out

    def query_direct_callers(self, class_name: str, method_name: str) -> list[dict]:
        """
        给定类名+方法名，在 Neo4j 中查找所有直接调用该方法的其他方法列表。
        返回 [{"class_name": str, "method_name": str}, ...]。
        """
        with self._driver.session(database=self._database) as session:
            r = session.run(
                f"""
                MATCH (b:{self.LABEL})
                WHERE toLower(coalesce(b.entity_type, '')) = 'method' AND b.class_name = $class_name AND b.name = $method_name
                MATCH (a:{self.LABEL})-[r:CALLS]->(b)
                RETURN DISTINCT a.class_name AS class_name, a.name AS method_name
                """,
                class_name=class_name,
                method_name=method_name,
            )
            out = []
            seen: set[tuple[str, str]] = set()
            for rec in r:
                cname = (rec.get("class_name") or "").strip()
                mname = (rec.get("method_name") or "").strip()
                if (cname, mname) in seen:
                    continue
                seen.add((cname, mname))
                out.append({"class_name": cname, "method_name": mname})
            return out

    def count_nodes_by_entity_type(self, entity_type: str) -> int:
        """按实体类型统计节点总数（用于分页）。"""
        with self._driver.session(database=self._database) as session:
            r = session.run(
                f"""
                MATCH (n:{self.LABEL})
                WHERE toLower(coalesce(n.entity_type, '')) = toLower($entity_type)
                RETURN count(n) AS c
                """,
                entity_type=entity_type or "",
            )
            rec = r.single()
            return rec["c"] or 0

    def count_nodes_by_entity_type_and_prefix(
        self,
        entity_type: str,
        prefix: str,
        *,
        exclude_methods_on_interface: bool = False,
    ) -> int:
        """
        按实体类型 + 名称首字母分区统计数量。prefix 为 'a'..'z' 或 'other'（非 a-z 首字母）。
        首字母取自 name，若无则取 id；按首字母小写比较。
        exclude_methods_on_interface：为 True 时排除通过 BELONGS_TO 挂在 interface 上的 method（仅 Java 方法查表等场景）。
        """
        lb = self.LABEL
        iface_tail = f"""
                    OPTIONAL MATCH (n)-[:BELONGS_TO]->(decl:{lb})
                    WITH n, collect(decl.entity_type) AS declTypes
                    WHERE none(t IN declTypes WHERE toLower(coalesce(t, '')) = 'interface')
                    """
        with self._driver.session(database=self._database) as session:
            if (prefix or "").lower() == "other":
                q = f"""
                    MATCH (n:{self.LABEL})
                    WHERE toLower(coalesce(n.entity_type, '')) = toLower($entity_type)
                    WITH n, toLower(substring(coalesce(n.name, n.id), 0, 1)) AS first
                    WHERE size(coalesce(n.name, n.id)) >= 1 AND (first < 'a' OR first > 'z')
                    {iface_tail if exclude_methods_on_interface else ""}
                    RETURN count(n) AS c
                    """
                r = session.run(q, entity_type=entity_type or "")
            else:
                p = (prefix or "a").lower()
                if len(p) != 1 or p < "a" or p > "z":
                    return 0
                q = f"""
                    MATCH (n:{self.LABEL})
                    WHERE toLower(coalesce(n.entity_type, '')) = toLower($entity_type)
                      AND toLower(substring(coalesce(n.name, n.id), 0, 1)) = $prefix
                    {iface_tail if exclude_methods_on_interface else ""}
                    RETURN count(n) AS c
                    """
                r = session.run(q, entity_type=entity_type or "", prefix=p)
            rec = r.single()
            return rec["c"] or 0

    def list_nodes_by_entity_type_and_prefix(
        self,
        entity_type: str,
        prefix: str,
        limit: int = 500,
        skip: int = 0,
        *,
        exclude_methods_on_interface: bool = False,
    ) -> list[dict]:
        """
        按实体类型 + 名称首字母分区分页查询。prefix 为 'a'..'z' 或 'other'。
        exclude_methods_on_interface：同 count_nodes_by_entity_type_and_prefix。
        """
        lb = self.LABEL
        iface_tail_other = f"""
                    OPTIONAL MATCH (n)-[:BELONGS_TO]->(decl:{lb})
                    WITH n, sortKey, collect(decl.entity_type) AS declTypes
                    WHERE none(t IN declTypes WHERE toLower(coalesce(t, '')) = 'interface')
                    """
        iface_tail_az = f"""
                    OPTIONAL MATCH (n)-[:BELONGS_TO]->(decl:{lb})
                    WITH n, collect(decl.entity_type) AS declTypes
                    WHERE none(t IN declTypes WHERE toLower(coalesce(t, '')) = 'interface')
                    """
        with self._driver.session(database=self._database) as session:
            if (prefix or "").lower() == "other":
                ex = exclude_methods_on_interface
                q = f"""
                    MATCH (n:{self.LABEL})
                    WHERE toLower(coalesce(n.entity_type, '')) = toLower($entity_type)
                    WITH n, coalesce(n.name, n.id) AS sortKey
                    WHERE size(sortKey) >= 1 AND (toLower(substring(sortKey, 0, 1)) < 'a' OR toLower(substring(sortKey, 0, 1)) > 'z')
                    {iface_tail_other if ex else ""}
                    RETURN n
                    ORDER BY sortKey
                    SKIP $skip LIMIT $limit
                """
                params = {"entity_type": entity_type or "", "skip": skip, "limit": limit}
            else:
                p = (prefix or "a").lower()
                if len(p) != 1 or p < "a" or p > "z":
                    return []
                ex = exclude_methods_on_interface
                q = f"""
                    MATCH (n:{self.LABEL})
                    WHERE toLower(coalesce(n.entity_type, '')) = toLower($entity_type)
                      AND toLower(substring(coalesce(n.name, n.id), 0, 1)) = $prefix
                    {iface_tail_az if ex else ""}
                    RETURN n
                    ORDER BY coalesce(n.name, n.id)
                    SKIP $skip LIMIT $limit
                """
                params = {"entity_type": entity_type or "", "prefix": p, "skip": skip, "limit": limit}
            r = session.run(q, **params)
            out = []
            for rec in r:
                n = rec["n"]
                if n is None:
                    continue
                d = dict(n) if hasattr(n, "keys") else {}
                if "id" not in d and hasattr(n, "get"):
                    d["id"] = n.get("id")
                out.append(d)
            return out

    def list_distinct_module_ids_for_entity_type(self, entity_type: str, limit: int = 200) -> List[str]:
        """
        返回指定实体类型下存在过的所有不同 module_id（用于 package 等按模块分组）。
        """
        with self._driver.session(database=self._database) as session:
            r = session.run(
                f"""
                MATCH (n:{self.LABEL})
                WHERE toLower(coalesce(n.entity_type, '')) = toLower($entity_type)
                  AND n.module_id IS NOT NULL AND n.module_id <> ''
                RETURN DISTINCT n.module_id AS mid
                ORDER BY mid
                LIMIT $limit
                """,
                entity_type=entity_type or "",
                limit=limit,
            )
            return [rec["mid"] for rec in r if rec.get("mid")]

    def list_nodes_by_entity_type_and_module(
        self, entity_type: str, module_id: str, limit: int = 500, skip: int = 0
    ) -> list[dict]:
        """
        按实体类型 + 模块分页查询节点（如某模块下的 package 列表）。
        """
        with self._driver.session(database=self._database) as session:
            r = session.run(
                f"""
                MATCH (n:{self.LABEL})
                WHERE toLower(coalesce(n.entity_type, '')) = toLower($entity_type)
                  AND n.module_id = $module_id
                RETURN n
                ORDER BY coalesce(n.name, n.id)
                SKIP $skip LIMIT $limit
                """,
                entity_type=entity_type or "",
                module_id=module_id or "",
                skip=skip,
                limit=limit,
            )
            out = []
            for rec in r:
                n = rec["n"]
                if n is None:
                    continue
                d = dict(n) if hasattr(n, "keys") else {}
                if "id" not in d and hasattr(n, "get"):
                    d["id"] = n.get("id")
                out.append(d)
            return out

    def list_nodes_by_entity_type(self, entity_type: str, limit: int = 500, skip: int = 0) -> list[dict]:
        """
        按实体类型从 Neo4j 分页查询节点列表。
        返回 [{"id", "name", "entity_type", ...}, ...]。
        """
        with self._driver.session(database=self._database) as session:
            r = session.run(
                f"""
                MATCH (n:{self.LABEL})
                WHERE toLower(coalesce(n.entity_type, '')) = toLower($entity_type)
                RETURN n
                ORDER BY coalesce(n.name, n.id)
                SKIP $skip LIMIT $limit
                """,
                entity_type=entity_type or "",
                skip=skip,
                limit=limit,
            )
            out = []
            for rec in r:
                n = rec["n"]
                if n is None:
                    continue
                d = dict(n) if hasattr(n, "keys") else {}
                if "id" not in d and hasattr(n, "get"):
                    d["id"] = n.get("id")
                out.append(d)
            return out

    def get_node_relations(self, nid: str) -> dict[str, list[dict]]:
        """
        查询某节点的所有入边与出边及对端节点信息。
        返回 {"outgoing": [{"rel_type", "target_id", "target_name", "target_type"}, ...],
              "incoming": [{"rel_type", "source_id", "source_name", "source_type"}, ...]}。
        """
        outgoing: list[dict] = []
        incoming: list[dict] = []
        with self._driver.session(database=self._database) as session:
            # 出边: (self)-[r]->(other)
            r_out = session.run(
                f"""
                MATCH (a:{self.LABEL} {{id: $id}})-[r]->(b:{self.LABEL})
                RETURN type(r) AS rel_type, b.id AS other_id, b.name AS other_name, b.entity_type AS other_type
                """,
                id=nid,
            )
            for rec in r_out:
                outgoing.append({
                    "rel_type": rec.get("rel_type") or "",
                    "target_id": rec.get("other_id") or "",
                    "target_name": rec.get("other_name") or rec.get("other_id") or "",
                    "target_type": rec.get("other_type") or "",
                })
            # 入边: (other)-[r]->(self)
            r_in = session.run(
                f"""
                MATCH (a:{self.LABEL})-[r]->(b:{self.LABEL} {{id: $id}})
                RETURN type(r) AS rel_type, a.id AS other_id, a.name AS other_name, a.entity_type AS other_type
                """,
                id=nid,
            )
            for rec in r_in:
                incoming.append({
                    "rel_type": rec.get("rel_type") or "",
                    "source_id": rec.get("other_id") or "",
                    "source_name": rec.get("other_name") or rec.get("other_id") or "",
                    "source_type": rec.get("other_type") or "",
                })
        return {"outgoing": outgoing, "incoming": incoming}

    def search_by_name(
        self,
        name_substring: str,
        entity_types: Optional[List[str]] = None,
        limit: int = 100,
    ) -> List[dict]:
        """
        按名称模糊检索节点（与内存图 search_by_name 语义一致，供 graph 为 None 时使用）。
        返回 [{"id", "name", "entity_type", ...}, ...]。
        """
        with self._driver.session(database=self._database) as session:
            q = (name_substring or "").strip().lower()
            if not q:
                return []
            # 按 name 或 id 包含关键词，不区分大小写
            if entity_types:
                types_lower = [str(t).lower() for t in entity_types]
                r = session.run(
                    f"""
                    MATCH (n:{self.LABEL})
                    WHERE (toLower(coalesce(n.name, '')) CONTAINS $q OR toLower(coalesce(n.id, '')) CONTAINS $q)
                      AND toLower(coalesce(n.entity_type, '')) IN $types_lower
                    RETURN n
                    ORDER BY n.name
                    LIMIT $limit
                    """,
                    q=q,
                    types_lower=types_lower,
                    limit=limit,
                )
            else:
                r = session.run(
                    f"""
                    MATCH (n:{self.LABEL})
                    WHERE toLower(coalesce(n.name, '')) CONTAINS $q OR toLower(coalesce(n.id, '')) CONTAINS $q
                    RETURN n
                    ORDER BY n.name
                    LIMIT $limit
                    """,
                    q=q,
                    limit=limit,
                )
            out = []
            for rec in r:
                n = rec["n"]
                if n is None:
                    continue
                d = dict(n) if hasattr(n, "keys") else {}
                if "id" not in d and hasattr(n, "get"):
                    d["id"] = n.get("id")
                out.append(d)
            return out

    def subgraph_for_service(self, service_id: str) -> dict:
        """
        按服务/模块返回子图。若存在 Service 节点则从其出发做影响闭包；否则按 module_id 取子图。
        返回 {"nodes": [...], "edges": [{"source", "target", "rel_type", ...}, ...]}。
        """
        sid = service_id if (service_id or "").startswith("service://") else f"service://{service_id}"
        node_ids = set()
        if self.has_node(sid):
            node_ids = self.impact_closure(sid, direction="down", max_depth=10)
            node_ids.add(sid)
        if not node_ids:
            # 无 Service 节点时按 module_id 兜底：sid 形如 service://mall-admin -> module_id = mall-admin
            module_id = sid.replace("service://", "").strip() if sid else ""
            if not module_id:
                return {"nodes": [], "edges": []}
            with self._driver.session(database=self._database) as session:
                r = session.run(
                    f"MATCH (n:{self.LABEL}) WHERE n.module_id = $mid RETURN n.id AS id",
                    mid=module_id,
                )
                node_ids = {rec["id"] for rec in r if rec.get("id")}
        if not node_ids:
            return {"nodes": [], "edges": []}
        ids_list = list(node_ids)
        with self._driver.session(database=self._database) as session:
            rn = session.run(
                f"MATCH (n:{self.LABEL}) WHERE n.id IN $ids RETURN n",
                ids=ids_list,
            )
            nodes = []
            for rec in rn:
                n = rec["n"]
                if n is None:
                    continue
                d = dict(n) if hasattr(n, "keys") else {}
                if "id" not in d and hasattr(n, "get"):
                    d["id"] = n.get("id")
                nodes.append(d)
            re = session.run(
                f"""
                MATCH (a:{self.LABEL})-[r]->(b:{self.LABEL})
                WHERE a.id IN $ids AND b.id IN $ids
                RETURN a.id AS src, b.id AS tgt, type(r) AS rel_type
                """,
                ids=ids_list,
            )
            edges = []
            for rec in re:
                edges.append({
                    "source": rec.get("src"),
                    "target": rec.get("tgt"),
                    "rel_type": rec.get("rel_type") or "",
                })
        return {"nodes": nodes, "edges": edges}

    def list_distinct_module_ids(self, limit: int = 200) -> List[str]:
        """
        返回图中出现过的所有不同的 module_id（用于无 Service 节点时兜底展示服务/模块列表）。
        """
        with self._driver.session(database=self._database) as session:
            r = session.run(
                f"""
                MATCH (n:{self.LABEL})
                WHERE n.module_id IS NOT NULL AND n.module_id <> ''
                RETURN DISTINCT n.module_id AS mid
                ORDER BY mid
                LIMIT $limit
                """,
                limit=limit,
            )
            return [rec["mid"] for rec in r if rec.get("mid")]

    def iter_nodes(self):
        """
        迭代所有节点，产出 (node_id, attrs_dict)。与 KnowledgeGraph.iter_nodes() 语义一致，供 OWL 导出与推理使用。
        """
        with self._driver.session(database=self._database) as session:
            r = session.run(f"MATCH (n:{self.LABEL}) RETURN n")
            for rec in r:
                n = rec.get("n")
                if n is None:
                    continue
                attrs = dict(n) if hasattr(n, "keys") else {}
                nid = attrs.get("id")
                if nid is None and hasattr(n, "get"):
                    nid = n.get("id")
                if nid is not None:
                    yield str(nid), attrs

    def iter_edges(self):
        """
        迭代所有边，产出 (source_id, target_id, rel_type, attrs_dict)。
        使用关系的 rel_type 属性（与写入时一致）；若无则用 type(r) 规范化为与 KnowledgeGraph 一致的 rel_type。
        """
        # Neo4j 关系类型 -> 图中 rel_type（小写为结构层枚举值，大写为知识层字面量）
        _type_to_rel = {
            "CALLS": "calls", "EXTENDS": "extends", "IMPLEMENTS": "implements",
            "DEPENDS_ON": "depends_on", "BELONGS_TO": "belongs_to", "SERVICE_CALLS": "service_calls",
            "CONTAINS": "contains", "RELATES_TO": "relates_to", "ANNOTATED_BY": "annotated_by",
            "SERVICE_EXPOSES": "service_exposes", "BINDS_TO_SERVICE": "binds_to_service",
            "BELONGS_TO_DOMAIN": "BELONGS_TO_DOMAIN", "CONTAINS_CAPABILITY": "CONTAINS_CAPABILITY",
            "IMPLEMENTED_BY": "IMPLEMENTED_BY", "RELATED_TO": "RELATED_TO", "IN_DOMAIN": "IN_DOMAIN",
        }
        with self._driver.session(database=self._database) as session:
            r = session.run(
                f"""
                MATCH (a:{self.LABEL})-[r]->(b:{self.LABEL})
                RETURN a.id AS sid, b.id AS tid, type(r) AS type_r, r.rel_type AS rel_type_prop
                """
            )
            for rec in r:
                sid = rec.get("sid")
                tid = rec.get("tid")
                if sid is None or tid is None:
                    continue
                rel_type = rec.get("rel_type_prop")
                if rel_type is None or (isinstance(rel_type, str) and rel_type.strip() == ""):
                    type_r = (rec.get("type_r") or "").strip()
                    rel_type = _type_to_rel.get(type_r, type_r if type_r else "RELATED")
                attrs = {"rel_type": rel_type}
                yield str(sid), str(tid), rel_type, attrs

    def add_inferred_edge(self, source_id: str, target_id: str, rel_type: str, **attrs: Any) -> None:
        """
        添加一条推理得到的边（带 inferred=True）。使用 MERGE 避免重复边。
        """
        rtype = _rel_type(rel_type)
        with self._driver.session(database=self._database) as session:
            session.run(
                f"""
                MERGE (a:{self.LABEL} {{id: $sid}})
                MERGE (b:{self.LABEL} {{id: $tid}})
                MERGE (a)-[r:{rtype}]->(b)
                SET r.rel_type = $rel_type, r.inferred = true
                """,
                sid=source_id,
                tid=target_id,
                rel_type=rel_type,
            )

    def list_inferred_edges(self, limit: int = 500) -> List[dict]:
        """
        返回图中标记为 inferred 的边列表，每项 {"source": id, "target": id, "rel_type": str}。
        用于 OWL 推理 Tab 展示「当前图中的推断边」。
        """
        with self._driver.session(database=self._database) as session:
            # 关系上可能有 inferred 属性；Neo4j 无全局关系属性查询，需按已知关系类型查
            r = session.run(
                f"""
                MATCH (a:{self.LABEL})-[r]->(b:{self.LABEL})
                WHERE r.inferred = true
                RETURN a.id AS sid, b.id AS tid, r.rel_type AS rel_type
                LIMIT $limit
                """,
                limit=limit,
            )
            return [
                {"source": rec.get("sid"), "target": rec.get("tid"), "rel_type": rec.get("rel_type") or ""}
                for rec in r if rec.get("sid") and rec.get("tid")
            ]
