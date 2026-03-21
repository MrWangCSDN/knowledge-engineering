"""业务域中心图（Business Domain Center Graph）。

以 BusinessDomain 为中心，展示两圈：
1) Ring1：BusinessCapability（来自图中的 CONTAINS_CAPABILITY）
2) Ring2：代码本体类/方法/接口（来自图中的 IN_DOMAIN 反向边）

点击节点打开「节点详情」面板，可关闭返回。
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st
import streamlit.components.v1 as components
import json

from src.app.components.interpretation_panel import InterpretationPanel
from src.core.domain_enums import INTERP_PANEL_ENTITY_TYPES
from src.app.ui.streamlit_keys import SessionKeys


def _safe_int(x: Any, default: int) -> int:
    try:
        return int(x)
    except Exception:
        return default


class BusinessDomainCenterGraphView:
    def __init__(
        self,
        *,
        graph_backend: Any,
        neo4j_backend: Any,
        services: Any,
    ) -> None:
        self._backend = graph_backend or neo4j_backend
        self._neo4j = neo4j_backend
        self._services = services

    def _list_business_domains(self) -> list[dict[str, str]]:
        """返回 [{nid,name,id}]，优先使用内存图 iter_nodes；否则用 neo4j_backend 的 list_nodes_by_entity_type。"""
        b = self._backend
        if b is None:
            return []

        out: list[dict[str, str]] = []
        # 内存图 KnowledgeGraph：有 iter_nodes
        if hasattr(b, "iter_nodes"):
            for nid, attrs in b.iter_nodes():  # type: ignore[attr-defined]
                et = (attrs or {}).get("entity_type") or ""
                if str(et).lower() != "businessdomain":
                    continue
                name = (attrs or {}).get("name") or nid
                did = str(nid).replace("domain://", "", 1)
                out.append({"nid": nid, "id": did, "name": str(name)})
        # Neo4jGraphBackend：有 list_nodes_by_entity_type
        elif hasattr(b, "list_nodes_by_entity_type"):
            try:
                rows = b.list_nodes_by_entity_type("BusinessDomain", limit=5000, skip=0)  # type: ignore[attr-defined]
                for r in rows or []:
                    nid = r.get("id") or ""
                    if not nid:
                        continue
                    did = str(nid).replace("domain://", "", 1)
                    out.append({"nid": nid, "id": did, "name": r.get("name") or did})
            except Exception:
                return []
        out.sort(key=lambda x: (x.get("name") or ""))
        return out

    def _get_ring1_capabilities(self, domain_nid: str, *, limit: int) -> list[dict[str, Any]]:
        b = self._backend
        if b is None:
            return []
        caps = b.successors(domain_nid, rel_type="CONTAINS_CAPABILITY")  # type: ignore[call-arg]
        # 取 name 展示
        rows: list[dict[str, Any]] = []
        for cid in caps[: max(0, limit)]:
            node = b.get_node(cid) if hasattr(b, "get_node") else None
            name = (node or {}).get("name") or cid
            rows.append({"id": cid, "name": name, "entity_type": (node or {}).get("entity_type") or ""})
        return rows

    def _get_ring2_code_entities(
        self,
        domain_nid: str,
        *,
        limit_per_type: int,
    ) -> tuple[dict[str, list[dict[str, Any]]], bool]:
        """
        返回：
          ({class: [...], interface: [...], method: [...]} , derived_fallback)

        首选从 predecessors(domain, IN_DOMAIN) 取 code 节点；
        若图中未落下 IN_DOMAIN（或查询为空），则回退到：
          service -> domain (BELONGS_TO_DOMAIN) + code.module_id 属于这些 service/module
        来“推导式”构建 Ring2，保证业务域与代码本体关联在图上可见。
        """
        b = self._backend
        if b is None:
            return {}, False

        grouped: dict[str, list[dict[str, Any]]] = {k: [] for k in INTERP_PANEL_ENTITY_TYPES}
        try:
            preds = b.predecessors(domain_nid, rel_type="IN_DOMAIN")  # type: ignore[call-arg]
        except Exception:
            preds = []

        for nid in preds:
            node = b.get_node(nid) if hasattr(b, "get_node") else None
            et = ((node or {}).get("entity_type") or "").lower()
            if et not in grouped:
                continue
            grouped[et].append(
                {"id": nid, "name": (node or {}).get("name") or nid, "entity_type": et}
            )

        # 若首选为空，则做推导式回退
        has_any = any(len(v) > 0 for v in grouped.values())
        if has_any:
            for et, arr in grouped.items():
                grouped[et] = arr[: max(0, limit_per_type)]
            return grouped, False

        # 推导回退
        derived_fallback = True
        service_nids: list[str] = []
        try:
            service_nids = b.predecessors(domain_nid, rel_type="BELONGS_TO_DOMAIN")  # type: ignore[call-arg]
        except Exception:
            service_nids = []

        module_ids: list[str] = []
        for sid in service_nids:
            if str(sid).startswith("service://"):
                module_ids.append(str(sid).replace("service://", "", 1))
            else:
                # 兜底：用 name 字段作为 module id
                node = b.get_node(sid) if hasattr(b, "get_node") else None
                if node and node.get("name"):
                    module_ids.append(str(node.get("name") or ""))

        module_ids = [m for m in dict.fromkeys(module_ids) if m]
        if not module_ids:
            return grouped, True

        # 优先使用 Neo4j 提供的 list_nodes_by_entity_type_and_module；没有的话就只能遍历 iter_nodes
        neo = self._neo4j
        seen: set[str] = set()

        for et in ("class", "interface", "method"):
            collected: list[dict[str, Any]] = []
            if neo is not None and hasattr(neo, "list_nodes_by_entity_type_and_module"):
                try:
                    for mid in module_ids:
                        rows = neo.list_nodes_by_entity_type_and_module(  # type: ignore[attr-defined]
                            et, mid, limit=int(limit_per_type), skip=0
                        )
                        for row in rows or []:
                            nid = row.get("id") or ""
                            if not nid or nid in seen:
                                continue
                            collected.append(
                                {
                                    "id": nid,
                                    "name": row.get("name") or nid,
                                    "entity_type": et,
                                }
                            )
                            seen.add(nid)
                            if len(collected) >= int(limit_per_type):
                                break
                        if len(collected) >= int(limit_per_type):
                            break
                except Exception:
                    collected = []
            elif hasattr(b, "iter_nodes"):
                # KnowledgeGraph 有 iter_nodes（nid, attrs）
                try:
                    for nid, attrs in b.iter_nodes():  # type: ignore[attr-defined]
                        if (attrs or {}).get("entity_type") != et:
                            continue
                        mid = (attrs or {}).get("module_id") or ""
                        if mid and mid in module_ids:
                            if nid in seen:
                                continue
                            collected.append(
                                {
                                    "id": nid,
                                    "name": (attrs or {}).get("name") or nid,
                                    "entity_type": et,
                                }
                            )
                            seen.add(nid)
                            if len(collected) >= int(limit_per_type):
                                break
                except Exception:
                    collected = []

            grouped[et] = collected[: max(0, limit_per_type)]

        return grouped, derived_fallback

    def _render_node_detail(self, *, nid: str, entity_type: str) -> None:
        b = self._backend
        if b is None:
            st.info("当前无图谱后端，无法展示详情。")
            return

        node = b.get_node(nid) if hasattr(b, "get_node") else None
        # 用 expander 提供“边框容器”效果，并支持关闭（通过不再渲染本面板实现）
        with st.expander("节点详情", expanded=True):
            if st.button("关闭详情", type="secondary", key=f"{SessionKeys.NS}_bd_close_{nid}"):
                st.session_state.pop(SessionKeys.BD_CENTER_SELECTED_NODE, None)
                st.session_state.pop(SessionKeys.BD_CENTER_SELECTED_NODE_TYPE, None)
                st.rerun()

            st.caption(f"节点：{nid}")
            st.json(node or {"id": nid})

            # 展示简单邻居（MVP，不依赖 get_node_relations）
            try:
                outgoing = b.successors(nid, rel_type=None)  # type: ignore[call-arg]
                incoming = b.predecessors(nid, rel_type=None)  # type: ignore[call-arg]
                if outgoing:
                    st.markdown("**出边邻居（前 20）**")
                    st.write(outgoing[:20])
                if incoming:
                    st.markdown("**入边邻居（前 20）**")
                    st.write(incoming[:20])
            except Exception:
                pass

            # 方法/类/接口复用你已有的解读面板（源码/技术/业务解读）
            etype = (entity_type or "").lower()
            if etype in INTERP_PANEL_ENTITY_TYPES:
                InterpretationPanel.render(
                    nid,
                    etype,
                    node,
                    self._services.weaviate_data_svc,
                    wrap_in_expander=False,
                )

    @staticmethod
    def _graph_node_style(type_text: str) -> dict[str, str]:
        t = (type_text or "").lower()
        if t == "businessdomain" or t == "domain":
            return {"bg": "#e5e7eb", "text": "#0f172a"}  # neutral light gray
        if "capability" in t:
            return {"bg": "#f3f4f6", "text": "#0f172a"}  # neutral very light
        if t == "method":
            return {"bg": "#d1d5db", "text": "#0f172a"}  # neutral gray
        if t in ("class", "interface"):
            return {"bg": "#cbd5e1", "text": "#0f172a"}  # neutral slate
        return {"bg": "#e5e7eb", "text": "#0f172a"}  # neutral gray

    def _render_cytoscape_graph(
        self,
        *,
        center_id: str,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        selected_id: Optional[str],
    ) -> None:
        """使用 Cytoscape 渲染子图（仅 Ring1/Ring2 的关键边），点击节点不回传；详情通过下方按钮/下拉选择打开。"""

        # 组装 cytoscape elements
        # 为避免布局算法导致“节点一字排开/不居中”，这里使用 preset + 手工极坐标，
        # 让：
        # - 中心域在 (0,0)
        # - capability 在 ring1 半径上做圆周分布
        # - class/interface/method 在 ring2 半径上做圆周分布
        import math

        center_id = str(center_id or "")
        ring0_ids = {center_id} if center_id else set()
        ring1_nodes: list[dict[str, Any]] = []
        ring2_nodes: list[dict[str, Any]] = []
        for n in nodes:
            nid = str(n.get("id") or "")
            if not nid:
                continue
            if nid in ring0_ids:
                continue
            et = (n.get("entity_type") or "").lower()
            if "capability" in et:
                ring1_nodes.append(n)
            elif et in ("class", "interface", "method"):
                ring2_nodes.append(n)

        def _polar_position(i: int, total: int, radius: float) -> dict[str, float]:
            if total <= 0:
                return {"x": 0.0, "y": 0.0}
            # 从正上方开始，顺时针分布
            ang = (-math.pi / 2) + (2 * math.pi * i / total)
            return {"x": float(radius * math.cos(ang)), "y": float(radius * math.sin(ang))}

        radius1 = 190.0
        radius2 = 330.0
        ring1_positions = {
            str(n.get("id")): _polar_position(i, len(ring1_nodes), radius1)
            for i, n in enumerate(ring1_nodes)
        }
        ring2_positions = {
            str(n.get("id")): _polar_position(i, len(ring2_nodes), radius2)
            for i, n in enumerate(ring2_nodes)
        }

        elements: list[dict[str, Any]] = []
        def _type_short(t: str) -> str:
            tt = (t or "").lower()
            if tt in ("businessdomain", "domain"):
                return "域"
            if "capability" in tt:
                return "能力"
            if tt == "method":
                return "方法"
            if tt == "class":
                return "类"
            if tt == "interface":
                return "接口"
            return tt or "节点"

        def _edge_short(rel: str) -> str:
            rr = (rel or "").upper()
            if rr == "CONTAINS_CAPABILITY":
                return "包含"
            if rr == "IN_DOMAIN":
                return "所属"
            return rel or ""

        for n in nodes:
            et = n.get("entity_type") or ""
            style = self._graph_node_style(et)
            label = n.get("label") or n["id"]
            short_t = _type_short(et)
            nid = str(n.get("id") or "")
            if nid in ring0_ids:
                pos = {"x": 0.0, "y": 0.0}
            else:
                pos = ring1_positions.get(nid) or ring2_positions.get(nid) or {"x": 0.0, "y": 0.0}
            elements.append(
                {
                    "data": {
                        "id": n["id"],
                        # Cytoscape 支持在 label 中使用真实换行符；避免显示字面 "\n"
                        "label": f"{short_t}\n{label}",
                        "type": et,
                        "bg": style.get("bg") or "#64748b",
                        "tooltip": f"{short_t}: {label}<br/>ID: {n['id']}<br/>Type: {et}",
                    }
                    ,
                    "position": pos,
                }
            )
        for e in edges:
            rel_type = e.get("rel_type") or e.get("label") or ""
            elements.append(
                {
                    "data": {
                        "id": e["id"],
                        "source": e["source"],
                        "target": e["target"],
                        "rel_type": rel_type,
                        "edgeLabel": _edge_short(rel_type),
                    }
                }
            )

        selected_id_js = selected_id or ""
        html = f"""
<div style="height:560px; width:100%; border:1px solid rgba(0,0,0,0.06); border-radius:10px; overflow:hidden;">
    <div style="padding:10px 12px; display:flex; gap:12px; align-items:center; border-bottom:1px solid rgba(0,0,0,0.06); background:rgba(250,251,252,1);">
    <div style="display:flex; gap:10px; align-items:center; font-size:12px; color:#334155;">
      <span style="display:inline-flex; align-items:center; gap:6px;">
        <i style="width:10px; height:10px; border-radius:2px; background:#e5e7eb; border:1px solid rgba(0,0,0,0.08); display:inline-block;"></i>中心域
      </span>
      <span style="display:inline-flex; align-items:center; gap:6px;">
        <i style="width:10px; height:10px; border-radius:2px; background:#f3f4f6; border:1px solid rgba(0,0,0,0.08); display:inline-block;"></i>能力
      </span>
      <span style="display:inline-flex; align-items:center; gap:6px;">
        <i style="width:10px; height:10px; border-radius:2px; background:#cbd5e1; border:1px solid rgba(0,0,0,0.08); display:inline-block;"></i>类/接口
      </span>
      <span style="display:inline-flex; align-items:center; gap:6px;">
        <i style="width:10px; height:10px; border-radius:2px; background:#d1d5db; border:1px solid rgba(0,0,0,0.08); display:inline-block;"></i>方法
      </span>
    </div>
    <div style="margin-left:auto; font-size:12px; color:#334155;">
      <span style="padding:2px 8px; border-radius:999px; border:1px solid rgba(0,0,0,0.08); background:white;">{_edge_short("CONTAINS_CAPABILITY")} = 灰边</span>
      <span style="padding:2px 8px; border-radius:999px; border:1px solid rgba(0,0,0,0.08); background:white; margin-left:8px;">{_edge_short("IN_DOMAIN")} = 深灰边</span>
    </div>
  </div>
  <div id="bd_cy" style="height:500px; width:100%;"></div>
</div>
<script src="https://unpkg.com/cytoscape@3.26.0/dist/cytoscape.min.js"></script>
<script>
  const elements = {json.dumps(elements, ensure_ascii=False)};
  const centerId = {json.dumps(center_id, ensure_ascii=False)};
  const selectedId = {json.dumps(selected_id_js, ensure_ascii=False)};

  function edgeColor(relType) {{
    const r = (relType || '').toUpperCase();
    if (r === 'CONTAINS_CAPABILITY') return '#9ca3af'; // gray
    if (r === 'IN_DOMAIN') return '#6b7280'; // dark gray
    return '#94a3b8';
  }}

  // 简易 tooltip
  const tooltip = document.createElement('div');
  tooltip.style.position = 'absolute';
  tooltip.style.zIndex = 9999;
  tooltip.style.pointerEvents = 'none';
  tooltip.style.padding = '8px 10px';
  tooltip.style.fontSize = '12px';
  tooltip.style.color = '#0f172a';
  tooltip.style.background = 'rgba(255,255,255,0.95)';
  tooltip.style.border = '1px solid rgba(0,0,0,0.08)';
  tooltip.style.borderRadius = '10px';
  tooltip.style.boxShadow = '0 6px 18px rgba(0,0,0,0.12)';
  tooltip.style.display = 'none';
  document.getElementById('bd_cy').parentElement.appendChild(tooltip);

  let cy = null;
  try {{
    cy = cytoscape({{
    container: document.getElementById('bd_cy'),
    elements,
    style: [
      {{
        selector: 'node',
        style: {{
          'label': 'data(label)',
          'shape': 'round-rectangle',
          'text-valign': 'center',
          'text-halign': 'center',
          'font-size': 10,
          'color': '#0f172a',
          'background-color': 'data(bg)',
          'border-width': 1,
          'border-color': 'rgba(15,23,42,0.35)',
          'width': 175,
          'height': 52,
          'padding': 7,
          'text-wrap': 'wrap',
          'text-max-width': 125,
          'text-justification': 'center',
          'text-margin-y': 3,
        }}
      }},
      {{
        selector: 'edge',
        style: {{
          'label': 'data(edgeLabel)',
          'font-size': 9,
          'color': '#334155',
          'line-color': 'data(rel_type)',
          'target-arrow-color': 'data(rel_type)',
          'text-background-color': '#ffffff',
          'text-background-opacity': 1,
          'text-background-padding': 2,
          'curve-style': 'bezier',
          'target-arrow-shape': 'triangle',
          'width': 1.5,
          'arrow-scale': 0.6
        }}
      }},
    ],
    layout: {{
      // 使用 preset：position 已在 Python 侧手工给出，保证居中两圈布局。
      name: 'preset'
    }}
  }});
  }} catch (err) {{
    const el = document.getElementById('bd_cy');
    if (el) {{
      el.innerHTML = '<div style="padding:16px; color:#ef4444; font-size:14px;">Cytoscape 渲染失败：' + (err && err.message ? err.message : err) + '</div>';
    }}
    cy = null;
  }}

  if (cy) {{
    // 自动缩放与居中，避免节点“跑出视口”
    try {{
      cy.fit();
      cy.zoom(1.05);
    }} catch (e) {{}}

  // 边颜色：基于 rel_type
  cy.edges().forEach(function(e) {{
    const rel = e.data('rel_type');
    const c = edgeColor(rel);
    e.style('line-color', c);
    e.style('target-arrow-color', c);
    e.style('label', e.data('edgeLabel'));
    if (rel === 'IN_DOMAIN') {{
      e.style('width', 2.1);
    }} else if (rel === 'CONTAINS_CAPABILITY') {{
      e.style('width', 2.0);
    }}
  }});

  // tooltip & highlight selected
  if (selectedId && cy.getElementById(selectedId).length > 0) {{
    const sel = cy.getElementById(selectedId);
    cy.nodes().forEach(function(n) {{
      if (n.id() === selectedId) {{
        n.style({{'border-width': 3, 'border-color': '#111827'}});
      }}
    }});

    // incident edges 高亮，其它边淡化
    const incident = new Set();
    cy.edges().forEach(function(ed) {{
      const s = ed.data('source');
      const t = ed.data('target');
      if (s === selectedId || t === selectedId) {{
        incident.add(ed.id());
      }}
    }});
    cy.edges().forEach(function(ed) {{
      if (incident.has(ed.id())) {{
        ed.style({{'opacity': 1, 'width': 3.0}});
      }} else {{
        ed.style({{'opacity': 0.14, 'width': 1.2}});
      }}
    }});
  }}

  cy.nodes().on('mouseover', function(evt) {{
    const n = evt.target;
    tooltip.innerHTML = n.data('tooltip') || '';
    tooltip.style.display = 'block';
    tooltip.style.left = (evt.originalEvent.pageX + 8) + 'px';
    tooltip.style.top = (evt.originalEvent.pageY + 8) + 'px';
  }});
  cy.nodes().on('mousemove', function(evt) {{
    tooltip.style.left = (evt.originalEvent.pageX + 8) + 'px';
    tooltip.style.top = (evt.originalEvent.pageY + 8) + 'px';
  }});
    cy.nodes().on('mouseout', function() {{
      tooltip.style.display = 'none';
    }});
  }} // end if(cy)
</script>
"""
        components.html(html, height=560, scrolling=True)

    def render(self) -> None:
        if self._backend is None:
            st.info("请先运行流水线或配置 Neo4j 后再查看业务域中心图。")
            return

        # 详情面板：存在就优先展示
        sel_nid = st.session_state.get(SessionKeys.BD_CENTER_SELECTED_NODE)
        sel_type = st.session_state.get(SessionKeys.BD_CENTER_SELECTED_NODE_TYPE) or ""
        if sel_nid:
            self._render_node_detail(nid=str(sel_nid), entity_type=str(sel_type))
            return

        st.markdown("##### 业务域中心图（Business Domain Center Graph）")
        domains = self._list_business_domains()
        if not domains:
            st.info("图谱中暂无 BusinessDomain 节点。")
            return

        # 默认选第一个
        default_domain_nid = domains[0]["nid"]
        if not st.session_state.get(SessionKeys.BD_CENTER_SELECTED_DOMAIN):
            st.session_state[SessionKeys.BD_CENTER_SELECTED_DOMAIN] = default_domain_nid

        domain_nid = st.selectbox(
            "选择业务域（中心节点）",
            options=[d["nid"] for d in domains],
            format_func=lambda x: next((d["name"] for d in domains if d["nid"] == x), x),
            key=SessionKeys.BD_CENTER_SELECTED_DOMAIN,
        )

        cap_limit = st.slider(
            "内环：最多展示能力（capability）节点数",
            min_value=1,
            max_value=60,
            value=_safe_int(st.session_state.get(SessionKeys.BD_CENTER_CAP_LIMIT, 20), 20),
            step=1,
            key=SessionKeys.BD_CENTER_CAP_LIMIT,
        )
        ring2_limit = st.slider(
            "外环：每类代码实体最多展示数量",
            min_value=5,
            max_value=200,
            value=_safe_int(st.session_state.get(SessionKeys.BD_CENTER_CODE_LIMIT, 40), 40),
            step=5,
            key=SessionKeys.BD_CENTER_CODE_LIMIT,
        )

        b = self._backend
        domain_node = b.get_node(domain_nid) if hasattr(b, "get_node") else None
        domain_name = (domain_node or {}).get("name") or domain_nid

        st.markdown("**中心节点**")
        st.info(f"{domain_name}（{domain_nid}）")

        # Ring1 / Ring2 数据（由 Neo4j Cypher 查询；graph_backend 的 successors/predecessors/get_node 也会走对应后端实现）
        ring1 = self._get_ring1_capabilities(domain_nid, limit=cap_limit)
        grouped, derived_fallback = self._get_ring2_code_entities(domain_nid, limit_per_type=ring2_limit)

        # 构建子图：中心 domain + capability + 代码实体 + 关键边（连接到中心域）
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        node_type_map: dict[str, str] = {}

        nodes.append({"id": domain_nid, "label": domain_name, "entity_type": "businessdomain"})
        node_type_map[domain_nid] = "businessdomain"

        for cap in ring1:
            cid = cap.get("id")
            if not cid:
                continue
            nodes.append({"id": cid, "label": cap.get("name") or cid, "entity_type": cap.get("entity_type") or "capability"})
            node_type_map[cid] = (cap.get("entity_type") or "capability") or "capability"
            edges.append(
                {
                    "id": f"e_dom_cap_{cid}",
                    "source": domain_nid,
                    "target": cid,
                    "rel_type": "CONTAINS_CAPABILITY",
                    "label": "CONTAINS_CAPABILITY",
                }
            )

        for et in ("class", "interface", "method"):
            arr = grouped.get(et) or []
            for node in arr[: max(1, ring2_limit)]:
                nid = node.get("id")
                if not nid:
                    continue
                nodes.append({"id": nid, "label": node.get("name") or nid, "entity_type": et})
                node_type_map[nid] = et
                # code entity -> domain
                edges.append(
                    {
                        "id": f"e_code_dom_{nid}",
                        "source": nid,
                        "target": domain_nid,
                        "rel_type": "IN_DOMAIN",
                        "label": "IN_DOMAIN",
                    }
                )

        # 图例（简洁）
        colL, colR = st.columns(2)
        with colL:
            st.caption("颜色含义：中心域=绿色，能力=紫色，类/接口=蓝色，方法=深蓝")
        with colR:
            ring2_total = sum(len(grouped.get(t) or []) for t in ("class", "interface", "method"))
            st.caption(f"内环（能力）节点数：{len(ring1)}｜外环（类/接口/方法）节点数：{ring2_total}")
            if derived_fallback:
                st.caption(
                    "提示：外环节点使用了推导式回退（基于 service/module 与 BELONGS_TO_DOMAIN），确保图上可见关联。"
                )
        if len(ring1) <= 2:
            st.caption(
                "提示：当前中心业务域下能力（capability）节点较少；请检查 `config/project.yaml` 中 "
                "`domain.capabilities` 是否完整定义了 `capability_ids`。"
            )

        # 图形渲染
        pick_key = f"{SessionKeys.NS}_bd_pick_node"
        # 默认把中心节点作为初始选择
        options = [(domain_nid, domain_name, "businessdomain")]
        for nid, ntype in node_type_map.items():
            if nid == domain_nid:
                continue
            # 从 nodes 列表查 name（避免再查图谱）
            nm = next((x["label"] for x in nodes if x["id"] == nid), nid)
            options.append((nid, str(nm), ntype))

        option_ids = [o[0] for o in options]
        option_labels = [f"{o[2]}：{o[1]}" for o in options]
        selected_pick_nid = st.selectbox(
            "选择节点查看详情（强可视化图旁边不支持直接回传点击）",
            options=option_ids,
            format_func=lambda nid: next((l for (i, l) in zip(option_ids, option_labels) if i == nid), nid),
            key=pick_key,
        )

        # 在中心图中高亮选中节点（不打开详情）
        self._render_cytoscape_graph(
            center_id=domain_nid,
            nodes=nodes,
            edges=edges,
            selected_id=selected_pick_nid,
        )

        # 打开详情
        if st.button("打开所选节点详情", type="primary"):
            st.session_state[SessionKeys.BD_CENTER_SELECTED_NODE] = selected_pick_nid
            st.session_state[SessionKeys.BD_CENTER_SELECTED_NODE_TYPE] = node_type_map.get(selected_pick_nid) or ""
            st.rerun()

