from __future__ import annotations

import json
import math
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from src.app.views.scene_template_room.scene_context import SceneTemplateContext


def _trim_text(s: str, max_len: int) -> str:
    v = (s or "").strip()
    if len(v) <= max_len:
        return v
    return v[: max_len - 3] + "..."


def _build_method_label(node: dict[str, Any], *, max_sig_len: int = 120) -> str:
    # 需求：方法签名 + 所在类名（两行）
    sig = (node.get("signature") or node.get("name") or "").strip()
    sig = sig.replace("\\n", "\n")
    cls = (node.get("class_name") or "").strip()
    sig = _trim_text(sig, max_sig_len) if sig else ""
    cls = _trim_text(cls, 70) if cls else ""
    if cls:
        return f"{sig}\n{cls}"
    return sig


def render_method_call_relation_graph(
    *,
    ctx: SceneTemplateContext,
    start_method_id: str,
    seen_dist: dict[str, int],
    node_ids: list[str],
    edge_source_to_targets: dict[str, list[str]],
) -> None:
    backend = ctx.get_graph_backend_memory_first()
    if backend is None:
        st.caption("当前未加载图谱后端：无法渲染方法调用关系图。")
        return

    # node_ids 已包含可达节点（不一定包含 start；这里确保包含）
    node_set = set(node_ids) | {start_method_id}

    # 按距离分圈
    # dist=0 -> center，dist>=1 -> ring1/ring2...
    ring_map: dict[int, list[str]] = {}
    for nid in node_set:
        dist = int(seen_dist.get(nid, 1))
        if dist <= 0:
            ring_idx = 0
        else:
            ring_idx = min(dist, 3)  # 超过 3 距离的节点并到 ring3，避免图太大
        ring_map.setdefault(ring_idx, []).append(nid)

    # 计算每个 ring 的位置（极坐标均分）
    # 保持“中心化两到三圈”的观感
    ring_radii = {0: 0.0, 1: 190.0, 2: 330.0, 3: 470.0}

    ring_positions: dict[str, dict[str, float]] = {}
    for ring_idx, nids in ring_map.items():
        if ring_idx == 0:
            ring_positions[start_method_id] = {"x": 0.0, "y": 0.0}
            continue

        # 稳定排序，保证刷新时位置尽量一致
        nids_sorted = sorted(nids, key=lambda x: ctx.get_node_name(x))
        radius = float(ring_radii.get(ring_idx, 330.0))
        for i, nid in enumerate(nids_sorted):
            angle = (2 * math.pi * i) / max(1, len(nids_sorted))
            # 旋转一点点，避免总是在同一条水平线
            angle -= math.pi / 2
            ring_positions[nid] = {
                "x": radius * math.cos(angle),
                "y": radius * math.sin(angle),
            }

    nodes: list[dict[str, Any]] = []
    for nid in node_set:
        node = ctx.get_node(nid) or {"id": nid}
        dist = int(seen_dist.get(nid, 0))
        label = _build_method_label(node)
        # 中心方法突出：黑底白字；其余方法尽量“接近白色”，避免底色块太黑
        if nid == start_method_id:
            # 中心节点突出：浅蓝底 + 黑字（更接近你要的“浅蓝色背景色+黑色文字”）
            bg = "#cfe5ff"
            font_color = "#0f172a"
        else:
            bg = "#f3f4f6" if dist == 1 else "#ffffff"  # 浅灰/白底
            font_color = "#0f172a"  # 黑色字
        tooltip = (
            f"ID: {nid}<br/>"
            f"类: {(node.get('class_name') or '').strip()}<br/>"
            f"方法: {(node.get('name') or '').strip()}<br/>"
            f"签名: {_trim_text((node.get('signature') or '').strip(), 180)}"
        )
        nodes.append(
            {
                "id": nid,
                "entity_type": "method",
                "label": label or nid,
                "bg": bg,
                "fontColor": font_color,
                "tooltip": tooltip,
            }
        )

    edges: list[dict[str, Any]] = []
    edge_seen: set[str] = set()
    # 只要 nodes 之间存在 calls 出边（s -> t），就画出来
    for s, ts in edge_source_to_targets.items():
        if s not in node_set:
            continue
        for t in ts:
            if t not in node_set:
                continue
            if s == t:
                continue
            eid = f"{s}->{t}"
            if eid in edge_seen:
                continue
            edge_seen.add(eid)
            edges.append({"id": eid, "source": s, "target": t, "rel_type": "calls"})

    elements: list[dict[str, Any]] = []
    for n in nodes:
        pos = ring_positions.get(n["id"], {"x": 0.0, "y": 0.0})
        elements.append(
            {
                "data": {
                    "id": n["id"],
                    # 真实换行符：避免显示字面 "\n"
                    "label": n["label"],
                    "bg": n["bg"],
                    "tooltip": n["tooltip"],
                },
                "position": pos,
            }
        )

    def _edge_short(rel_type: str) -> str:
        r = (rel_type or "").upper()
        if r == "CALLS":
            return "calls"
        if r == "CALLS".lower():
            return "calls"
        if r == "CALLS" or r == "CALLS()":
            return "calls"
        return rel_type or ""

    for e in edges:
        rel_type = e.get("rel_type") or ""
        elements.append(
            {
                "data": {
                    "id": e["id"],
                    "source": e["source"],
                    "target": e["target"],
                    "rel_type": rel_type,
                    "edgeLabel": "calls",
                }
            }
        )

    selected_id_js = start_method_id
    nodes_json = json.dumps(elements, ensure_ascii=False)

    html = f"""
<div style="height:560px; width:100%; border:1px solid rgba(0,0,0,0.06); border-radius:10px; overflow:hidden;">
  <div style="padding:10px 12px; display:flex; gap:12px; align-items:center; border-bottom:1px solid rgba(0,0,0,0.06); background:rgba(250,251,252,1);">
    <div style="font-size:12px; color:#334155;">
      <span style="padding:2px 8px; border-radius:999px; border:1px solid rgba(0,0,0,0.08); background:white;">中心方法</span>
      <span style="padding:2px 8px; border-radius:999px; border:1px solid rgba(0,0,0,0.08); background:#cbd5e1; margin-left:8px;">近邻方法</span>
    </div>
    <div style="margin-left:auto; font-size:12px; color:#334155;">
      <span style="padding:2px 8px; border-radius:999px; border:1px solid rgba(0,0,0,0.08); background:white;">标注：签名（第1行）+ 类名（第2行）</span>
    </div>
  </div>
  <div id="mcr_cy" style="height:500px; width:100%;"></div>
</div>
<script src="https://unpkg.com/cytoscape@3.26.0/dist/cytoscape.min.js"></script>
<script>
  const elements = {nodes_json};
  const selectedId = {json.dumps(selected_id_js, ensure_ascii=False)};

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
  document.getElementById('mcr_cy').parentElement.appendChild(tooltip);

  let cy = null;
  try {{
    cy = cytoscape({{
      container: document.getElementById('mcr_cy'),
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
            'font-weight': 800,
            'color': 'data(fontColor)',
            'background-color': 'data(bg)',
            'border-width': 1,
            'border-color': 'rgba(15,23,42,0.35)',
            'width': 220,
            'height': 78,
            'padding': 8,
            'text-wrap': 'wrap',
            'text-max-width': 165,
            'text-justification': 'center',
            'text-margin-y': 2,
          }}
        }},
        {{
          selector: 'edge',
          style: {{
            'label': 'data(edgeLabel)',
            'font-size': 8,
            'color': '#334155',
            'line-color': '#94a3b8',
            'target-arrow-color': '#94a3b8',
            'curve-style': 'bezier',
            'target-arrow-shape': 'triangle',
            'width': 2.0,
            'arrow-scale': 0.6
          }}
        }}
      ],
      layout: {{
        name: 'preset'
      }}
    }});
  }} catch (err) {{
    const el = document.getElementById('mcr_cy');
    if (el) {{
      el.innerHTML = '<div style="padding:16px; color:#ef4444; font-size:14px;">Cytoscape 渲染失败</div>';
    }}
  }}

  if (cy) {{
    try {{
      cy.fit();
      cy.zoom(1.02);
    }} catch (e) {{}}
    if (selectedId) {{
      const sel = cy.getElementById(selectedId);
      sel.style({{'border-width': 3, 'border-color': '#111827'}});
    }}
  }}

  if (cy) {{
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
  }}
</script>
"""
    components.html(html, height=580, scrolling=True)

