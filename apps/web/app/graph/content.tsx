"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { BookOpen, Network, RefreshCcw, Search } from "lucide-react";
import { PageHeading } from "../../components/shell";
import { EmptyState, Panel } from "../../components/ui";
import { HyperNeighborData, getKbEntityNames, getKbEntities, getKbRelationships, getKbVertexNeighbor, listKbs } from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";

const ENTITY_TYPE_COLORS: Record<string, string> = {
  PERSON: "#00C9C9", CONCEPT: "#a68fff", ORGANIZATION: "#F08F56", LOCATION: "#16f69c",
  EVENT: "#004ac9", PRODUCT: "#f056d1",
};
const BUBBLE_COLORS = ["#F6BD16", "#00C9C9", "#F08F56", "#D580FF", "#FF3D00", "#16f69c", "#004ac9", "#f056d1"];

function HypergraphCanvas({ data, selectedVertex }: { data: HyperNeighborData; selectedVertex: string }) {
  const nodes = useMemo(() => Object.keys(data.vertices).map((id) => ({ id, ...data.vertices[id] })), [data]);
  const positions = useMemo(() => {
    const result: Record<string, { x: number; y: number }> = {};
    nodes.forEach((node, index) => {
      const angle = (index / nodes.length) * Math.PI * 2;
      result[node.id] = { x: 280 + Math.cos(angle) * (130 + (index % 3) * 30), y: 200 + Math.sin(angle) * (110 + (index % 3) * 30) };
    });
    return result;
  }, [nodes]);
  const edgeEntries = Object.entries(data.edges);

  return (
    <svg viewBox="0 0 560 400" style={{ width: "100%", height: "auto", background: "var(--surface-muted)", borderRadius: 10, border: "1px solid var(--border)" }} data-testid="hypergraph-canvas">
      {edgeEntries.map(([key], i) => {
        const ids = key.split("|#|").filter((id) => positions[id]);
        if (ids.length < 2) return null;
        const cx = ids.reduce((sum, id) => sum + positions[id].x, 0) / ids.length;
        const cy = ids.reduce((sum, id) => sum + positions[id].y, 0) / ids.length;
        const rx = Math.max(...ids.map((id) => Math.abs(positions[id].x - cx) + 40), 50);
        const ry = Math.max(...ids.map((id) => Math.abs(positions[id].y - cy) + 30), 40);
        return <ellipse key={key} cx={cx} cy={cy} rx={rx} ry={ry} fill={BUBBLE_COLORS[i % BUBBLE_COLORS.length]} fillOpacity={0.08} stroke={BUBBLE_COLORS[i % BUBBLE_COLORS.length]} strokeOpacity={0.3} strokeWidth={1.5} />;
      })}
      {nodes.map((node) => {
        const pos = positions[node.id];
        const isCenter = node.id === selectedVertex;
        const fill = isCenter ? "#1a1a1a" : ENTITY_TYPE_COLORS[String(node.entity_type || "")] || "#8566CC";
        return (
          <g key={node.id}>
            <circle cx={pos.x} cy={pos.y} r={isCenter ? 12 : 8} fill={fill} stroke="white" strokeWidth={1.5} />
            <text x={pos.x} y={pos.y - 14} textAnchor="middle" fontSize={10} fill="var(--text-soft)" fontWeight={isCenter ? 600 : 400}>{node.id.slice(0, 12)}</text>
          </g>
        );
      })}
    </svg>
  );
}

export default function GraphPage() {
  const { notify } = useWorkspace();
  const [kbs, setKbs] = useState<{ id: string; name: string }[]>([]);
  const [kbId, setKbId] = useState("");
  const [tab, setTab] = useState<"graph" | "entities" | "relations">("graph");
  const [entityNames, setEntityNames] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [selectedEntity, setSelectedEntity] = useState<string | undefined>();
  const [neighborData, setNeighborData] = useState<HyperNeighborData | null>(null);
  const [entities, setEntities] = useState<{ entities: { id: string; entity_name: string; entity_type: string; description: string }[]; total: number } | null>(null);
  const [relations, setRelations] = useState<{ relationships: { id: string; entity_set: string; keywords: string; summary: string }[]; total: number } | null>(null);
  const [loading, setLoading] = useState(false);

  const loadKbs = useCallback(async () => {
    try { const list = await listKbs(); setKbs(list); if (list.length > 0 && !kbId) setKbId(list[0].id); }
    catch { notify("知识库列表读取失败"); }
  }, [notify, kbId]);

  const loadGraph = useCallback(async (id: string) => {
    if (!id) return;
    setLoading(true);
    try {
      const names = await getKbEntityNames(id);
      setEntityNames(names.names || []);
      if (names.names?.length > 0 && !selectedEntity) setSelectedEntity(names.names[0]);
    } catch { setEntityNames([]); }
    finally { setLoading(false); }
  }, [selectedEntity]);

  const loadNeighbor = useCallback(async (id: string, vertex: string) => {
    if (!id || !vertex) return;
    setLoading(true);
    try { setNeighborData(await getKbVertexNeighbor(id, vertex)); }
    catch { setNeighborData(null); }
    finally { setLoading(false); }
  }, []);

  const loadEntities = useCallback(async (id: string) => {
    if (!id) return;
    try { setEntities(await getKbEntities(id)); } catch { setEntities(null); }
  }, []);

  const loadRelations = useCallback(async (id: string) => {
    if (!id) return;
    try { setRelations(await getKbRelationships(id)); } catch { setRelations(null); }
  }, []);

  useEffect(() => { void loadKbs(); }, [loadKbs]);
  useEffect(() => { void loadGraph(kbId); }, [kbId, loadGraph]);
  useEffect(() => { if (kbId && selectedEntity) void loadNeighbor(kbId, selectedEntity); }, [kbId, selectedEntity, loadNeighbor]);
  useEffect(() => { if (kbId && tab === "entities") void loadEntities(kbId); }, [kbId, tab, loadEntities]);
  useEffect(() => { if (kbId && tab === "relations") void loadRelations(kbId); }, [kbId, tab, loadRelations]);

  const filteredNames = entityNames.filter((name) => !search || name.toLowerCase().includes(search.toLowerCase()));
  const selectedVertex = neighborData?.vertices?.[selectedEntity ?? ""];

  return (
    <div className="page-content" id="graph">
      <PageHeading
        hint="图谱可视化：选择实体展开一阶邻居子图；实体与关系为只读浏览（写入 capability 未开放）"
        actions={
          <>
            <select value={kbId} onChange={(event) => { setKbId(event.target.value); setSelectedEntity(undefined); setNeighborData(null); }} style={{ width: "auto", minWidth: 160 }}>
              <option value="">选择知识库…</option>
              {kbs.map((kb) => <option key={kb.id} value={kb.id}>{kb.name}</option>)}
            </select>
            <button className="button button-secondary" onClick={() => { void loadGraph(kbId); if (kbId && selectedEntity) void loadNeighbor(kbId, selectedEntity); }} disabled={loading}>
              {loading ? <RefreshCcw size={15} className="spin" /> : <Network size={15} />} 刷新
            </button>
          </>
        }
      />

      <div className="tabs">
        <button className={`tab ${tab === "graph" ? "tab-active" : ""}`} onClick={() => setTab("graph")} data-testid="graph-tab-explore">图谱探索</button>
        <button className={`tab ${tab === "entities" ? "tab-active" : ""}`} onClick={() => setTab("entities")} data-testid="graph-tab-entities">实体浏览</button>
        <button className={`tab ${tab === "relations" ? "tab-active" : ""}`} onClick={() => setTab("relations")} data-testid="graph-tab-relations">关系浏览</button>
      </div>

      {tab === "graph" && (
        <section className="grid grid-main-side">
          <Panel title="实体搜索" subtitle={`${entityNames.length} 个实体`} testId="entity-search">
            <div className="form-row">
              <input placeholder="搜索实体…" value={search} onChange={(event) => setSearch(event.target.value)} data-testid="entity-search-input" />
            </div>
            <div className="list" style={{ maxHeight: 420, overflowY: "auto" }}>
              {filteredNames.slice(0, 100).map((name) => (
                <button key={name} type="button" className={`list-item list-item-button ${selectedEntity === name ? "list-item-active" : ""}`.trim()} onClick={() => setSelectedEntity(name)}>
                  <span className="icon-tile"><BookOpen size={14} /></span>
                  <span className="item-copy"><strong>{name}</strong></span>
                </button>
              ))}
              {!filteredNames.length && <EmptyState>{loading ? "加载中…" : "没有实体（需先在知识库页触发索引）"}</EmptyState>}
            </div>
          </Panel>

          <Panel title={selectedEntity ?? "邻居子图"} subtitle={neighborData ? `${Object.keys(neighborData.vertices).length} 个顶点 · ${Object.keys(neighborData.edges).length} 条超边` : "选择实体后展开一阶邻居"} testId="neighbor-subgraph">
            {neighborData ? (
              <>
                {/* 手机上画布保持 560px 最小宽度靠横向滚动看：缩到 328px 时节点标签只有约 6px 高，等于不可读 */}
                <div className="graph-canvas">
                  <HypergraphCanvas data={neighborData} selectedVertex={selectedEntity ?? ""} />
                </div>
                {selectedVertex && (
                  <div className="document-comment-item">
                    <strong>{String(selectedVertex.entity_name || selectedEntity)}</strong>
                    <p>{String(selectedVertex.description || "").split("<SEP>").slice(0, 2).join("；").slice(0, 200)}</p>
                    <small>{String(selectedVertex.entity_type || "未知类型")}</small>
                  </div>
                )}
              </>
            ) : <EmptyState><Network size={16} /> {loading ? "加载中…" : "未选择实体或知识库尚未索引"}</EmptyState>}
          </Panel>
        </section>
      )}

      {tab === "entities" && (
        <Panel title="实体浏览" subtitle={entities ? `${entities.total} 个实体` : "加载中"} testId="entity-list">
          {entities?.entities.length ? (
            <div className="table">
              <div className="table-head"><span>实体</span><span>类型</span><span>描述</span><span /></div>
              {entities.entities.map((entity) => (
                <div className="table-row" key={entity.id}>
                  <div className="table-title"><strong>{entity.entity_name}</strong></div>
                  <span className="chip" data-label="类型">{entity.entity_type || "—"}</span>
                  <span className="hint" data-label="描述">{entity.description?.slice(0, 80) || "—"}</span>
                  <button className="text-button" onClick={() => { setSelectedEntity(entity.entity_name); setTab("graph"); }}>查看子图</button>
                </div>
              ))}
            </div>
          ) : <EmptyState>没有实体数据</EmptyState>}
        </Panel>
      )}

      {tab === "relations" && (
        <Panel title="关系浏览" subtitle={relations ? `${relations.total} 条超边` : "加载中"} testId="relation-list">
          {relations?.relationships.length ? (
            <div className="list">
              {relations.relationships.map((relation) => (
                <div className="list-item list-item-static" key={relation.id}>
                  <span className="icon-tile icon-tile-amber"><Network size={15} /></span>
                  <div className="item-copy">
                    <strong>{relation.entity_set}</strong>
                    <small>{relation.keywords?.replace(/<SEP>/g, ", ")}</small>
                    <small>{relation.summary?.slice(0, 120)}</small>
                  </div>
                </div>
              ))}
            </div>
          ) : <EmptyState>没有关系数据</EmptyState>}
        </Panel>
      )}
    </div>
  );
}