"""Static self-contained Graph Export (HTML + JSON, zero CDN) with Conflux Pivot-Chain (ADR-034).

- All reads go through ``UnitOfWork`` and respect Space ACLs (ADR-025).
- Exports visible memories, active structural edges (highlighting the Conflux ``PARENT`` tree
  and heaviest-subtree Pivot Chain), active semantic relations, and Conflux derived epochs /
  total order.
- Never echoes full raw memory content: node labels use the version summary or a 24-char
  truncated snippet.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from ame.application.access import resolve
from ame.application.phase4_service import _scopes, compute_consensus_in
from ame.application.schemas import Actor
from ame.domain.enums import MemoryStatus
from ame.domain.phase4_scoring import SpineWeights
from ame.errors import NotFoundError, ValidationFailedError
from ame.infrastructure.unit_of_work import UnitOfWork

_ALL_STATUSES = tuple(s.value for s in MemoryStatus)

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>AME Conflux Tree-Graph Export</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 16px; background: #fafafa; color: #222; }
  h1 { font-size: 16px; margin: 0 0 8px 0; }
  svg { background: #fff; border: 1px solid #ddd; border-radius: 4px; }
  .legend {
    font-size: 12px; margin: 8px 0; display: flex; gap: 16px;
    align-items: center; flex-wrap: wrap;
  }
  .node circle { stroke: #333; stroke-width: 1.5px; }
  .node-pivot circle { stroke: #d97706; stroke-width: 3px; }
  .edge { stroke: #999; stroke-width: 1.2px; }
  .edge-parent { stroke: #4b5563; stroke-width: 1.8px; }
  .edge-pivot { stroke: #d97706; stroke-width: 3px; }
  .rel { stroke: #c2410c; stroke-dasharray: 4 3; stroke-width: 1.2px; }
  text { font-size: 10px; fill: #111; }
  label { cursor: pointer; user-select: none; }
</style>
</head>
<body>
<h1>AME Conflux Tree-Graph Export (static snapshot, zero external resources)</h1>
<div class="legend">
  <label><input type="checkbox" id="showRel" checked> 显示语义关系 (虚线)</label>
  <label><input type="checkbox" id="showPivot" checked> 高亮 Conflux 主链 (Pivot Chain)</label>
  <span>L1 灰 / L2 蓝 / L3 绿 / L4 金；空心 = 非 active；金粗框 = Pivot Chain</span>
</div>
<svg id="g" width="1120" height="640"></svg>
<script>
const data = __DATA__;
const svg = document.getElementById('g');
const LAYER_Y = { L1: 520, L2: 380, L3: 220, L4: 80 };
const nodes = {};
data.nodes.forEach((n, i) => {
  nodes[n.memory_id] = n;
  const r = n.total_order_rank;
  const col = (r !== null && r !== undefined) ? r : i;
  n._x = 70 + (col % 8) * 130;
  n._y = LAYER_Y[n.layer] || 300;
});
function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function draw() {
  const showRel = document.getElementById('showRel').checked;
  const showPivot = document.getElementById('showPivot').checked;
  let out = '';
  data.edges.forEach(e => {
    const a = nodes[e.from_memory_id], b = nodes[e.to_memory_id];
    if (!a || !b) return;
    let cls = 'edge';
    if (showPivot && e.on_pivot_chain) cls = 'edge-pivot';
    else if (e.edge_type === 'PARENT') cls = 'edge-parent';
    const et = `${esc(e.edge_type)}: ${esc(e.from_memory_id)} -&gt; ${esc(e.to_memory_id)}`;
    out += `<line class="${cls}" x1="${a._x}" y1="${a._y}" x2="${b._x}" y2="${b._y}">`
      + `<title>${et}</title></line>`;
  });
  if (showRel) data.relations.forEach(r => {
    const a = nodes[r.from_memory_id], b = nodes[r.to_memory_id];
    if (!a || !b) return;
    const rt = `${esc(r.relation_type)}: ${esc(r.from_memory_id)} &lt;-&gt; ${esc(r.to_memory_id)}`;
    out += `<line class="rel" x1="${a._x}" y1="${a._y}" x2="${b._x}" y2="${b._y}">`
      + `<title>${rt}</title></line>`;
  });
  const fill = { L1: '#cbd5e1', L2: '#93c5fd', L3: '#86efac', L4: '#fcd34d' };
  data.nodes.forEach(n => {
    const hollow = n.status !== 'active';
    const gcls = (showPivot && n.on_pivot_chain) ? 'node node-pivot' : 'node';
    const r = n.total_order_rank;
    const rankStr = (r !== null && r !== undefined) ? `#${r + 1}` : '-';
    const f = hollow ? 'none' : (fill[n.layer] || '#ddd');
    const tip = `${esc(n.memory_id)}\\n${esc(n.label)}\\n${esc(n.layer)}/${esc(n.status)} `
      + `| epoch=${esc(n.epoch_index)} | order=${rankStr} | subW=${esc(n.subtree_weight)}`;
    out += `<g class="${gcls}"><circle cx="${n._x}" cy="${n._y}" r="10" fill="${f}">`
      + `<title>${tip}</title></circle>`
      + `<text x="${n._x + 13}" y="${n._y + 4}">${esc(n.label)}</text></g>`;
  });
  svg.innerHTML = out;
}
document.getElementById('showRel').addEventListener('change', draw);
document.getElementById('showPivot').addEventListener('change', draw);
draw();
</script>
</body>
</html>
"""


def _label(summary: str | None, content: str | None) -> str:
    raw = (summary or content or "").strip().replace("\n", " ")
    return raw[:24] + ("…" if len(raw) > 24 else "")


class GraphExportService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        weights: SpineWeights | None = None,
        half_life_days: float = 90.0,
    ) -> None:
        self._sf = session_factory
        self._w = weights or SpineWeights()
        self._half = half_life_days

    def export_data(
        self,
        scope_id: str | None = None,
        actor: Actor | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        with UnitOfWork(self._sf, read_only=True) as uow:
            acc = resolve(uow, actor)
            if scope_id is not None:
                if uow.spaces.get(scope_id) is None or not acc.can_read(scope_id):
                    raise NotFoundError("Space not found", {"space_id": scope_id})
            scopes = _scopes(acc, scope_id)
            mems = [] if scopes == [] else uow.p4.memories(scopes, _ALL_STATUSES)
            ids = [m.memory_id for m in mems]
            _scored, _nsrc, consensus = compute_consensus_in(uow, mems, self._w, self._half, now)
            pivot_set = set(consensus.pivot_chain)
            pivot_pairs = {
                (consensus.pivot_chain[i], consensus.pivot_chain[i + 1])
                for i in range(len(consensus.pivot_chain) - 1)
            }
            edges = uow.p4.active_structural_edges(ids)
            relations = uow.p4.active_relations(ids)

            nodes_out = []
            for m in mems:
                v = uow.versions.get(m.memory_id, m.current_version)
                nodes_out.append(
                    {
                        "memory_id": m.memory_id,
                        "label": _label(v.summary if v else None, v.content if v else None),
                        "layer": m.layer,
                        "status": m.status,
                        "scope_id": m.scope_id,
                        "parent_memory_id": consensus.parent_of.get(m.memory_id),
                        "subtree_weight": consensus.subtree_weights.get(m.memory_id, 0.0),
                        "on_pivot_chain": m.memory_id in pivot_set,
                        "epoch_index": consensus.epoch_of.get(m.memory_id),
                        "total_order_rank": consensus.rank_of.get(m.memory_id),
                    }
                )
            edges_out = [
                {
                    "from_memory_id": frm,
                    "to_memory_id": to,
                    "edge_type": etype,
                    "on_pivot_chain": etype == "PARENT" and (frm, to) in pivot_pairs,
                }
                for frm, to, etype in edges
            ]
            relations_out = [
                {
                    "relation_id": rid,
                    "from_memory_id": frm,
                    "to_memory_id": to,
                    "relation_type": rtype,
                }
                for rid, frm, to, rtype in relations
            ]
            return {
                "nodes": nodes_out,
                "edges": edges_out,
                "relations": relations_out,
                "pivot_chain": list(consensus.pivot_chain),
                "derived_epochs": [
                    {
                        "index": ep.index,
                        "pivot_memory_id": ep.pivot_memory_id,
                        "memory_ids": list(ep.memory_ids),
                    }
                    for ep in consensus.epochs
                ],
                "total_order": list(consensus.total_order),
            }

    @staticmethod
    def render_html(data: dict[str, Any]) -> str:
        # Escape '</' inside JSON so a label containing '</script>' cannot break the script tag.
        safe_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        return _HTML_TEMPLATE.replace("__DATA__", safe_json)

    def export_file(
        self,
        out_path: Path,
        scope_id: str | None = None,
        fmt: str = "html",
        actor: Actor | None = None,
    ) -> dict[str, Any]:
        if fmt not in ("html", "json"):
            raise ValidationFailedError("format must be 'html' or 'json'", {"format": fmt})
        data = self.export_data(scope_id=scope_id, actor=actor)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            out_path.write_text(self.render_html(data), encoding="utf-8")
        return data
