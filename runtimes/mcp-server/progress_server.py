#!/usr/bin/env python3
"""
progress_server.py — Real-time distillation progress dashboard.

Polls the output directory and serves a live HTML dashboard showing:
- Current phase (page build / map / reduce / knowledge / format)
- Per-still progress (pages completed / total)
- Elapsed time
- File sizes and block counts

Usage:
    python3 progress_server.py <output_dir> [--port 8001] [--poll-interval 5]

Non-invasive: reads filesystem only, never writes. Safe to run alongside the pipeline.
"""

import argparse
import json
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse


def get_progress(output_dir: str) -> dict:
    """Scan the output directory and return current pipeline progress."""
    output = Path(output_dir)
    
    progress = {
        "timestamp": time.strftime("%H:%M:%S"),
        "output_dir": output_dir,
        "phases": {},
        "stills": {},
        "source": {},
    }
    
    # Source mash info (from enumeration)
    enum_dir = output / ".enumeration"
    candidates_file = enum_dir / "candidates.txt"
    if candidates_file.exists():
        with open(candidates_file) as f:
            file_list = [l.strip() for l in f if l.strip()]
        total_files = len(file_list)
        # Count by extension
        extensions = {}
        for fp in file_list:
            ext = fp.rsplit('.', 1)[-1].lower() if '.' in fp else 'other'
            extensions[ext] = extensions.get(ext, 0) + 1
        top_exts = sorted(extensions.items(), key=lambda x: -x[1])[:5]
        progress["source"] = {
            "files": total_files,
            "top_types": ", ".join(f".{e}({c})" for e, c in top_exts),
        }
    else:
        progress["source"] = {"files": 0, "top_types": ""}
    
    # Phase 0/1: Pages
    pages_dir = output / ".pages"
    if pages_dir.exists():
        page_dirs = [d for d in pages_dir.iterdir() if d.is_dir() and d.name.startswith("page_")]
        progress["phases"]["pages"] = {
            "status": "complete",
            "count": len(page_dirs),
        }
    else:
        progress["phases"]["pages"] = {"status": "not_started", "count": 0}
    
    total_pages = progress["phases"]["pages"]["count"]
    
    # Phase 2+: Per-still progress
    stills_dir = output / "stills"
    if stills_dir.exists():
        for still_dir in sorted(stills_dir.iterdir()):
            if not still_dir.is_dir():
                continue
            if still_dir.name.endswith("_backup") or "backup" in still_dir.name:
                continue
            if still_dir.name.startswith("."):
                continue
            
            still_name = still_dir.name
            still_info = {"name": still_name}
            
            # Check partials (map progress)
            partials_dir = still_dir / "partials"
            if not partials_dir.exists():
                # Some runs put page dirs directly in the still directory
                partials_dir = still_dir
            
            extracts = list(partials_dir.rglob(f"{still_name}_extract.md"))
            still_info["map_completed"] = len(extracts)
            still_info["map_total"] = total_pages
            still_info["map_pct"] = round(len(extracts) / max(total_pages, 1) * 100, 1) if total_pages > 0 else 0
            
            # Check if still has been started (even if no extracts yet)
            started_marker = still_dir / ".started"
            still_info["started"] = started_marker.exists()

            # Authoritative failure marker (written by run_distillation.sh: the still-centric
            # worker on nonzero exit, or the Phase 2->2.5 barrier for a missing raw_concat). This
            # is ground truth, not staleness-inference — a crashed still must not render as a
            # forever-"running" amber cell. The marker is cleared on a successful (re-)run.
            failed_marker = still_dir / ".failed"
            if failed_marker.exists():
                still_info["failed"] = True
                try:
                    fm = json.loads(failed_marker.read_text())
                    still_info["failed_phase"] = fm.get("phase", "")
                    still_info["failed_rc"] = fm.get("rc")
                    still_info["failed_reason"] = fm.get("reason", "")
                    still_info["failed_at"] = fm.get("failed_at", "")
                except (json.JSONDecodeError, IOError):
                    # marker present but unreadable — still a failure signal, don't swallow it
                    still_info["failed_phase"] = "unknown (unreadable .failed marker)"
            else:
                still_info["failed"] = False
            
            # Check raw_concat (reduce)
            raw_concat = still_dir / f"{still_name}_raw_concat.md"
            if raw_concat.exists():
                still_info["reduce"] = "complete"
                still_info["raw_concat_kb"] = round(raw_concat.stat().st_size / 1024)
            else:
                # Honest reduce state: only two on-disk facts are knowable without a reduce-job
                # heartbeat — map-not-done (pending) and raw_concat-present (complete). "map done,
                # no raw_concat yet" is NOT provably "running": in page-centric ALL stills finish
                # mapping before ANY reduce starts, so a mapped-but-unreduced still is QUEUED, not
                # running. Do not synthesize 'in_progress' from map completion (that was the
                # inference bug — a mapper that finished first showed a false amber reduce).
                still_info["reduce"] = "pending" if still_info["map_pct"] < 100 else "queued"
            
            # Check dedup index
            dedup = still_dir / f"{still_name}_dedup_index.json"
            if dedup.exists():
                still_info["dedup"] = "complete"
                try:
                    data = json.loads(dedup.read_text())
                    still_info["dedup_groups"] = data.get("duplicate_groups", 0)
                    still_info["total_findings"] = data.get("total_findings", 0)
                except:
                    pass
            else:
                still_info["dedup"] = "pending"
            
            # Check knowledge graph — only trust the final/.complete marker
            knowledge_complete_marker = still_dir / "final" / ".complete"
            if knowledge_complete_marker.exists():
                still_info["graph"] = "complete"
                try:
                    km = json.loads(knowledge_complete_marker.read_text())
                    still_info["graph_concepts"] = km.get("unified_concepts", 0)
                except:
                    pass
            else:
                still_info["graph"] = "pending"
            
            # Check synthesis — per-still .synthesis_complete marker
            synthesis_marker = still_dir / ".synthesis_complete"
            synthesis_progress = still_dir / ".synthesis_progress"
            if synthesis_marker.exists():
                still_info["synthesis"] = "complete"
            elif synthesis_progress.exists():
                still_info["synthesis"] = "running"
                try:
                    prog = json.loads(synthesis_progress.read_text())
                    still_info["synthesis_processed"] = prog.get("processed", 0)
                    still_info["synthesis_total"] = prog.get("total", 0)
                    still_info["synthesis_failed"] = prog.get("failed", 0)
                except (json.JSONDecodeError, IOError):
                    pass
            else:
                still_info["synthesis"] = "pending"
            
            # Check final deliverables
            final_dir = still_dir / "final"
            if final_dir.exists():
                final_files = [f for f in final_dir.glob("*.md") if f.name != "README.md"]
                still_info["final_files"] = len(final_files)
                still_info["final_kb"] = sum(f.stat().st_size for f in final_files) // 1024
            else:
                still_info["final_files"] = 0
            
            # Check formatted
            formatted_dir = still_dir / "final" / "formatted"
            format_complete_marker = formatted_dir / ".complete" if formatted_dir.exists() else None
            if format_complete_marker and format_complete_marker.exists():
                still_info["format_complete"] = True
                fmt_files = list(formatted_dir.glob("*.md"))
                still_info["formatted_files"] = len(fmt_files)
                still_info["formatted_kb"] = sum(f.stat().st_size for f in fmt_files) // 1024
            elif formatted_dir.exists():
                still_info["format_complete"] = False
                fmt_files = [f for f in formatted_dir.glob("*.md") if not f.name.startswith("._")]
                still_info["formatted_files"] = len(fmt_files)
                still_info["formatted_kb"] = sum(f.stat().st_size for f in fmt_files) // 1024
                # Check for in-progress format trace
                progress_json = formatted_dir / ".format_progress.json"
                progress_jsonl = formatted_dir / ".format_progress.jsonl"
                if progress_json.exists():
                    try:
                        prog = json.loads(progress_json.read_text())
                        still_info["format_current_file"] = prog.get("current_file", "")
                        still_info["format_files_done"] = prog.get("processed", 0)
                        still_info["format_files_total"] = prog.get("total_files", 0)
                        still_info["format_running"] = True
                    except (json.JSONDecodeError, IOError):
                        pass
                if progress_jsonl.exists():
                    try:
                        lines = progress_jsonl.read_text().strip().split("\n")
                        if lines and lines[-1]:
                            last = json.loads(lines[-1])
                            still_info["format_current_file"] = last.get("file", "")
                            still_info["format_chunks_done"] = last.get("completed", 0)
                            still_info["format_chunks_total"] = last.get("total_chunks", 0)
                            still_info["format_running"] = True
                    except:
                        pass
            else:
                still_info["format_complete"] = False
                still_info["formatted_files"] = 0
            
            # Determine overall phase.
            # FAILED takes precedence over any in-progress phase — but a completed reduce
            # (raw_concat present) is stronger truth than a stale marker, so a still that
            # actually produced its output is never shown failed even if a marker lingered.
            if still_info.get("failed") and still_info.get("reduce") != "complete":
                still_info["phase"] = "failed"
            elif still_info.get("format_complete"):
                still_info["phase"] = "formatted"
            elif still_info.get("format_current_file") or still_info.get("formatted_files", 0) > 0:
                still_info["phase"] = "formatting"
            elif still_info.get("graph") == "complete":
                still_info["phase"] = "knowledge_complete"
            elif still_info.get("reduce") == "complete":
                still_info["phase"] = "reduce_complete"
            elif still_info["map_pct"] >= 100:
                # Map done, reduce not yet complete, no proof a reduce is actively running.
                # Honest label is "map_complete" (awaiting/queued for reduce) — NOT "reducing",
                # which falsely implies active reduce work (page-centric reduces all stills only
                # after every map finishes).
                still_info["phase"] = "map_complete"
            elif still_info["map_pct"] > 0:
                still_info["phase"] = "mapping"
            else:
                still_info["phase"] = "queued"
            
            progress["stills"][still_name] = still_info
    
    # Post-still phases
    recon = output / "reconciliation"
    progress["phases"]["reconciliation"] = "complete" if recon.exists() and any(recon.glob("*.md")) else "pending"
    
    blend = output / "master_blend"
    if blend.exists() and any(blend.glob("*.md")):
        progress["phases"]["master_blend"] = "complete"
    elif (blend / ".master_blend_progress").exists():
        progress["phases"]["master_blend"] = "running"
        try:
            prog = json.loads((blend / ".master_blend_progress").read_text())
            progress["phases"]["master_blend_processed"] = prog.get("processed", 0)
            progress["phases"]["master_blend_total"] = prog.get("total_sections", 0)
        except (json.JSONDecodeError, IOError):
            pass
    else:
        progress["phases"]["master_blend"] = "pending"
    
    bottled = output / "bottled_spec"
    progress["phases"]["bottled"] = "complete" if bottled.exists() and any(bottled.glob("*.md")) else "pending"
    
    viewer = output / "viewer"
    progress["phases"]["viewer"] = "complete" if (viewer / "index.html").exists() else "pending"
    
    return progress


DASHBOARD_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Distillation Progress</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,sans-serif;background:#0f1419;color:#e7e9ea;padding:24px}
h1{font-size:20px;color:#ff9900;margin-bottom:4px}
.meta{font-size:12px;color:#8b98a5;margin-bottom:24px}
.dag-container{position:relative;width:100%;overflow-x:auto;margin-bottom:32px;display:flex;gap:16px;align-items:flex-start}
.dag-legend{min-width:150px;padding:12px;background:#1a1f26;border:1px solid #2f3336;border-radius:8px;font-size:12px;line-height:2}
.dag-legend .legend-title{font-size:11px;text-transform:uppercase;color:#8b98a5;letter-spacing:0.5px;margin-bottom:8px;font-weight:600}
.dag-legend .legend-item{color:#e7e9ea;padding:2px 4px;border-radius:3px;transition:background 0.2s}
.dag-legend .legend-item.highlighted{background:#3a2a1a;color:#ff9900}
.dag-legend .legend-item span{color:#ff9900;font-weight:600;display:inline-block;width:28px}
svg.dag{flex:1;min-width:900px;height:auto}
.dag-node{cursor:default}
.dag-node rect{rx:6;ry:6;stroke-width:1.5}
.dag-node text{font-size:12px;fill:#e7e9ea;text-anchor:middle;dominant-baseline:middle}
.dag-node .label{font-weight:600;font-size:13px}
.dag-node .sub{font-size:11px;fill:#8b98a5}
.dag-edge line{stroke:#3d4349;stroke-width:1.5;marker-end:url(#arrow)}
.status-queued rect{fill:#1a1f26;stroke:#3d4349}
.status-running rect{fill:#3a2a1a;stroke:#f59e0b;animation:pulse 1.5s ease-in-out infinite}
.status-complete rect{fill:#0f2e1a;stroke:#22c55e}
.status-failed rect{fill:#2e0f0f;stroke:#ef4444}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.6}}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px;margin-top:16px}
.card{background:#1a1f26;border:1px solid #2f3336;border-radius:8px;padding:16px}
.card h3{font-size:14px;margin-bottom:8px;color:#f7f9f9}
.card .phase{font-size:11px;padding:2px 8px;border-radius:10px;display:inline-block;margin-bottom:8px}
.phase-mapping{background:#1d3a5c;color:#8ecdf8}
.phase-map_complete{background:#26303a;color:#9fb4c4}
.phase-reducing{background:#3a2a1a;color:#fbbf24}
.phase-reduce_complete{background:#1a3a2a;color:#6ee7b7}
.phase-knowledge_graph{background:#2d1b4e;color:#c084fc}
.phase-knowledge_complete{background:#2d1b4e;color:#c084fc}
.phase-formatted{background:#166534;color:#86efac}
.phase-queued{background:#2f3336;color:#8b98a5}
.progress-bar{height:6px;background:#2f3336;border-radius:3px;margin:8px 0;overflow:hidden}
.progress-fill{height:100%;background:#ff9900;border-radius:3px;transition:width 0.5s}
.stats{font-size:12px;color:#8b98a5;margin-top:8px}
.stats span{margin-right:12px}
</style>
</head><body>
<h1>Specification Distillation — Pipeline Progress</h1>
<div class="meta" id="meta">Loading...</div>
<div class="dag-container">
<div class="dag-legend" id="legend"></div>
<svg class="dag" id="dag" viewBox="0 0 1200 600"></svg>
</div>
<h2 style="font-size:16px;margin-bottom:12px;color:#8b98a5">Per-Still Detail</h2>
<div class="grid" id="grid"></div>
<h2 style="font-size:16px;margin:24px 0 12px;color:#8b98a5">Tokens Per Minute <span style="font-size:11px;font-weight:normal;color:#687078">(<span style="color:#ff9900">■</span> input <span style="color:#22c55e">■</span> output <span style="color:#ef4444">■</span> over limit)</span></h2>
<div id="tpm-chart" style="background:#1a1f26;border:1px solid #2f3336;border-radius:8px;padding:16px;min-height:80px">
<div id="tpm-summary" style="font-size:12px;color:#8b98a5;margin-bottom:8px"></div>
<div id="tpm-bars" style="display:flex;align-items:flex-end;gap:2px;height:60px;overflow-x:auto"></div>
</div>
<h2 style="font-size:16px;margin:24px 0 12px;color:#8b98a5">Preflight — File-Type Classification &amp; Rig Coverage</h2>
<div id="preflight-panel" style="background:#1a1f26;border:1px solid #2f3336;border-radius:8px;padding:16px;min-height:60px">
<div id="preflight-body" style="font-size:12px;color:#8b98a5">Loading…</div>
</div>
<script>
const STILL_NAMES=['data','domain_language','exception','history','interface','permission','rule','runtime','state','test','workflow'];
const STILL_SHORT={'data':'DT','domain_language':'DL','exception':'EX','history':'HI','interface':'IF','permission':'PM','rule':'RL','runtime':'RT','state':'ST','test':'TS','workflow':'WF'};
const STILL_FULL={'DT':'Data','DL':'Domain Language','EX':'Exception','HI':'History','IF':'Interface','PM':'Permission','RL':'Rule','RT':'Runtime','ST':'State','TS':'Test','WF':'Workflow'};

function buildDAG(data){
  const svg=document.getElementById('dag');
  const stills=STILL_NAMES;
  const cols=Math.max(stills.length,1);
  const W=1200,rowH=80,nodeW=90,nodeH=42;
  const startY=20;
  
  // Rows: Source, Pages, Map, Reduce, Knowledge, Synthesis, Format, Reconcile, Blend, Bottled, Viewer
  const rows=[
    [{id:'source',label:'Source (Mash)',status:'complete',sub:data.source?.files?(data.source.files+' files'):'',tooltip:data.source?.top_types||''}],
    [{id:'pages',label:'Pages',status:data.phases.pages?.status||'pending',sub:data.phases.pages?.count?data.phases.pages.count+' pages':''}],
    stills.map(s=>({id:'map_'+s,label:STILL_SHORT[s],icon:'map',status:getMapStatus(data.stills[s]),sub:(data.stills[s]?.map_pct??0)+'%',tooltip:(STILL_FULL[STILL_SHORT[s]]||s)+': '+(data.stills[s]?.map_completed||0)+'/'+(data.stills[s]?.map_total||'?')+' pages'})),
    stills.map(s=>({id:'red_'+s,label:STILL_SHORT[s],icon:'reduce',status:getReduceStatus(data.stills[s]),sub:data.stills[s]?.raw_concat_kb?data.stills[s].raw_concat_kb+'KB':'—',tooltip:(STILL_FULL[STILL_SHORT[s]]||s)+': reduce '+(data.stills[s]?.reduce||'pending')})),
    stills.map(s=>({id:'kg_'+s,label:STILL_SHORT[s],icon:'knowledge',status:getKnowledgeStatus(data.stills[s]),sub:data.stills[s]?.final_files?data.stills[s].final_files+' files':'—',tooltip:(STILL_FULL[STILL_SHORT[s]]||s)+': knowledge graph '+(data.stills[s]?.graph||'pending')})),
    stills.map(s=>({id:'syn_'+s,label:STILL_SHORT[s],icon:'synth',status:getSynthesisStatus(data.stills[s]),sub:getSynthesisSub(data.stills[s]),tooltip:(STILL_FULL[STILL_SHORT[s]]||s)+': synthesis '+(data.stills[s]?.synthesis||'pending')})),
    stills.map(s=>({id:'fmt_'+s,label:STILL_SHORT[s],icon:'format',status:getFormatStatus(data.stills[s]),sub:getFormatSub(data.stills[s]),tooltip:(STILL_FULL[STILL_SHORT[s]]||s)+': '+(data.stills[s]?.formatted_kb?data.stills[s].formatted_kb+'KB':'pending')})),
    [{id:'recon',label:'Reconcile',status:data.phases.reconciliation==='complete'?'complete':'queued',sub:''}],
    [{id:'blend',label:'Master Blend',status:getBlendStatus(data.phases),sub:getBlendSub(data.phases)}],
    [{id:'bottled',label:'Bottled Spec',status:data.phases.bottled==='complete'?'complete':'queued',sub:''}],
    [{id:'viewer',label:'Viewer',status:data.phases.viewer==='complete'?'complete':'queued',sub:''}],
  ];
  
  const totalRows=rows.length;
  const svgH=totalRows*rowH+40;
  svg.setAttribute('viewBox',`0 0 ${W} ${svgH}`);
  
  let html='<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="#3d4349"/></marker><symbol id="icon-map" viewBox="0 0 24 24"><path d="M12 4v6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M12 10L6 15v5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M12 10l6 5v5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M12 10v10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></symbol><symbol id="icon-reduce" viewBox="0 0 24 24"><path d="M6 4v5l6 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M18 4v5l-6 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M12 4v10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M12 14v6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M9 17l3 3 3-3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></symbol><symbol id="icon-knowledge" viewBox="0 0 24 24"><path d="M12 2C8.5 2 5.5 4.5 5.5 7.5c0 2 1.2 3.7 3 4.5v2a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1v-2c1.8-.8 3-2.5 3-4.5C18.5 4.5 15.5 2 12 2z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M9 17v1a3 3 0 0 0 6 0v-1" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M12 2v3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M8 8h8" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" opacity="0.5"/><path d="M8.5 10.5h7" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" opacity="0.5"/></symbol><symbol id="icon-format" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-1.42 3.42 2 2 0 0 1-1.42-.58l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-3.42-1.42 2 2 0 0 1 .58-1.42l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1.08 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.84-2.84l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.84 2.84l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></symbol><symbol id="icon-synth" viewBox="0 0 24 24"><path d="M5 4v7c0 1.1.9 2 2 2h3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M19 4v7c0 1.1-.9 2-2 2h-3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M12 13v7" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="12" cy="13" r="2" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M9 18l3 3 3-3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></symbol></defs>';
  
  // Draw nodes and edges
  let prevRowCenters=[];
  rows.forEach((row,ri)=>{
    const y=startY+ri*rowH;
    const spacing=W/(row.length+1);
    const centers=[];
    
    row.forEach((node,ci)=>{
      const x=spacing*(ci+1);
      centers.push({x,y:y+nodeH/2});
      const w=row.length===1?120:nodeW;
      const stillCode=node.label;
      html+=`<g class="dag-node status-${node.status}" data-still="${stillCode}" transform="translate(${x-w/2},${y})" onmouseenter="highlightLegend('${stillCode}')" onmouseleave="highlightLegend('')">`;
      html+=`<title>${esc(node.tooltip||node.label)}</title>`;
      html+=`<rect width="${w}" height="${nodeH}"/>`;
      if(node.icon){
        html+=`<use href="#icon-${node.icon}" x="6" y="4" width="16" height="16" style="color:#e7e9ea"/>`;
        html+=`<text class="label" x="${w/2+10}" y="${nodeH/2-7}">${esc(node.label)}</text>`;
      }else{
        html+=`<text class="label" x="${w/2}" y="${nodeH/2-7}">${esc(node.label)}</text>`;
      }
      html+=`<text class="sub" x="${w/2}" y="${nodeH/2+9}">${node.sub}</text>`;
      html+=`</g>`;
    });
    
    // Edges from previous row
    if(ri>0&&prevRowCenters.length){
      if(prevRowCenters.length===1&&centers.length>1){
        // Fan out
        centers.forEach(c=>{
          html+=`<g class="dag-edge"><line x1="${prevRowCenters[0].x}" y1="${prevRowCenters[0].y+nodeH/2}" x2="${c.x}" y2="${c.y-nodeH/2}"/></g>`;
        });
      }else if(prevRowCenters.length>1&&centers.length===1){
        // Fan in
        prevRowCenters.forEach(p=>{
          html+=`<g class="dag-edge"><line x1="${p.x}" y1="${p.y+nodeH/2}" x2="${centers[0].x}" y2="${centers[0].y-nodeH/2}"/></g>`;
        });
      }else{
        // 1:1 mapping
        const n=Math.min(prevRowCenters.length,centers.length);
        for(let i=0;i<n;i++){
          html+=`<g class="dag-edge"><line x1="${prevRowCenters[i].x}" y1="${prevRowCenters[i].y+nodeH/2}" x2="${centers[i].x}" y2="${centers[i].y-nodeH/2}"/></g>`;
        }
      }
    }
    prevRowCenters=centers;
  });
  
  svg.innerHTML=html;
}

function getMapStatus(s){
  if(!s)return'queued';
  if(s.map_pct>=100)return'complete';
  // failed (started-but-crashed) beats 'running' so a dead still isn't a forever-amber cell
  if(s.failed&&s.reduce!=='complete')return'failed';
  if(s.map_pct>0)return'running';
  if(s.started)return'running';
  return'queued';
}
function getReduceStatus(s){
  if(!s)return'queued';
  if(s.reduce==='complete')return'complete';
  if(s.failed)return'failed';
  if(s.reduce==='in_progress')return'running';  // only if a reduce job ever reports it (ground truth)
  // NB: do NOT infer 'running' from map_pct>=100 — map-done-no-raw_concat is QUEUED, not running
  // (page-centric reduces only after all maps finish; a mapper that finished first is waiting).
  return'queued';
}
function getKnowledgeStatus(s){
  if(!s)return'queued';
  if(s.graph==='complete')return'complete';
  if(s.reduce==='complete')return'queued';
  return'queued';
}
function getSynthesisStatus(s){
  if(!s)return'queued';
  if(s.synthesis==='complete')return'complete';
  if(s.synthesis==='running')return'running';
  if(s.graph==='complete')return'queued';
  return'queued';
}
function getSynthesisSub(s){
  if(!s)return'—';
  if(s.synthesis==='complete')return'✓';
  if(s.synthesis==='running'&&s.synthesis_total>0)return s.synthesis_processed+'/'+s.synthesis_total;
  return'—';
}
function getFormatStatus(s){
  if(!s)return'queued';
  if(s.format_complete)return'complete';
  if(s.format_running||s.formatted_files>0)return'running';
  if(s.graph==='complete')return'queued';
  return'queued';
}
function getFormatSub(s){
  if(!s)return'—';
  if(s.format_complete)return(s.formatted_files||0)+' files';
  if(s.format_files_total>0)return(s.format_files_done||0)+'/'+(s.format_files_total)+' files';
  if(s.format_chunks_total>0)return s.format_chunks_done+'/'+s.format_chunks_total;
  if(s.formatted_files>0)return s.formatted_files+' files';
  return'—';
}
function getBlendStatus(phases){
  if(!phases)return'queued';
  if(phases.master_blend==='complete')return'complete';
  if(phases.master_blend==='running')return'running';
  return'queued';
}
function getBlendSub(phases){
  if(!phases)return'';
  if(phases.master_blend==='complete')return'✓';
  if(phases.master_blend==='running'&&phases.master_blend_total>0)return phases.master_blend_processed+'/'+phases.master_blend_total;
  return'';
}
function getOverallPct(s){
  // Weighted by approximate wall-clock time per phase
  // Map ~90s (15%), Reduce ~30s (5%), Knowledge ~15s (5%), Format ~15min (75%)
  if(!s)return 0;
  let pct=0;
  // Map: 0-15%
  pct+=Math.min(s.map_pct||0,100)*0.15;
  // Reduce: 15-20%
  if(s.reduce==='complete')pct+=5;
  // Knowledge: 20-25%
  if(s.graph==='complete')pct+=5;
  // Format: 25-100%
  if(s.format_complete)pct+=75;
  else if(s.formatted_files>0&&s.final_files>0)pct+=(s.formatted_files/s.final_files)*75;
  else if(s.formatted_files>0)pct+=10;
  return Math.round(pct);
}

function highlightLegend(code){
  document.querySelectorAll('.legend-item').forEach(el=>{
    if(code&&el.dataset.legend===code){el.classList.add('highlighted')}
    else{el.classList.remove('highlighted')}
  });
}

async function refresh(){
  const r=await fetch('/api/progress');
  const d=await r.json();
  document.getElementById('meta').textContent=`Last update: ${d.timestamp} | Output: ${d.output_dir}`;
  
  buildDAG(d);
  
  // Legend
  const legend=document.getElementById('legend');
  legend.innerHTML='<div class="legend-title">Still Key</div>'+
    Object.entries(STILL_FULL).map(([k,v])=>`<div class="legend-item" data-legend="${k}"><span>${k}</span>${v}</div>`).join('');
  
  // Still cards
  const grid=document.getElementById('grid');
  // Still cards — always show all stills, even if no data yet
  const allStillData=STILL_NAMES.map(s=>d.stills[s]||{name:s,map_completed:0,map_total:d.phases?.pages?.count||0,map_pct:0,reduce:'pending',phase:'queued',started:false});
  grid.innerHTML=allStillData.map(s=>{
    const phasePct=getOverallPct(s);
    const barColor=s.format_complete?'#22c55e':s.phase==='formatting'||s.phase==='knowledge_complete'?'#f59e0b':'#ff9900';
    return `
    <div class="card">
      <span class="phase phase-${esc(s.phase)}">${esc(s.phase)}</span>
      <h3>${esc(s.name)}</h3>
      <div class="progress-bar"><div class="progress-fill" style="width:${phasePct}%;background:${barColor}"></div></div>
      <div class="stats">
        <span>Map: ${s.map_completed||0}/${s.map_total||0}</span>
        <span>Reduce: ${esc(s.reduce||'pending')}</span>
        ${s.raw_concat_kb?`<span>Concat: ${s.raw_concat_kb}KB</span>`:''}
        ${s.dedup_groups?`<span>Dedup: ${s.dedup_groups} groups</span>`:''}
        ${s.formatted_files?`<span>Formatted: ${s.formatted_files} files (${s.formatted_kb}KB)</span>`:''}
        ${s.format_current_file?`<span>Formatting: ${esc(s.format_current_file)} (${s.format_chunks_done}/${s.format_chunks_total} chunks)</span>`:''}
      </div>
    </div>
  `}).join('');
}
async function refreshTpm(){
  try{
    const r=await fetch('/api/tpm');
    const t=await r.json();
    const sum=document.getElementById('tpm-summary');
    const bars=document.getElementById('tpm-bars');
    if(!t.timeseries||t.timeseries.length===0){sum.textContent='No telemetry data yet';bars.innerHTML='';return;}
    const costInput=((t.total_input_tokens||0)/1e6*3).toFixed(3);
    const costOutput=((t.total_output_tokens||0)/1e6*15).toFixed(3);
    const totalCost=(parseFloat(costInput)+parseFloat(costOutput)).toFixed(2);
    sum.textContent=`Input: peak ${(t.peak_input_tpm/1000).toFixed(0)}K/min (${t.input_violations} violations) | Output: peak ${(t.peak_output_tpm/1000).toFixed(0)}K/min (${t.output_violations} violations) | Calls: ${t.total_calls} | Cost: $${costInput} in + $${costOutput} out = $${totalCost}`;
    const maxT=Math.max(...t.timeseries.map(x=>Math.max(x.input_tokens,x.output_tokens)),1);
    bars.innerHTML=t.timeseries.map(x=>{
      const hIn=Math.max(1,Math.round(x.input_tokens/maxT*56));
      const hOut=Math.max(1,Math.round(x.output_tokens/maxT*56));
      const colorIn=x.input_tokens>1000000?'#ef4444':'#ff9900';
      const colorOut=x.output_tokens>1000000?'#ef4444':'#22c55e';
      return `<div style="display:flex;flex-direction:column;align-items:center;gap:1px" title="${esc(x.minute)}&#10;In: ${(x.input_tokens/1000).toFixed(0)}K&#10;Out: ${(x.output_tokens/1000).toFixed(0)}K"><div style="width:3px;height:${hIn}px;background:${colorIn};border-radius:1px"></div><div style="width:3px;height:${hOut}px;background:${colorOut};border-radius:1px"></div></div>`;
    }).join('');
  }catch(e){}
}
const PF_SEV_COLOR={CRITICAL:'#ef4444',HIGH:'#f59e0b',MEDIUM:'#8ecdf8',LOW:'#8b98a5'};
const PF_VERDICT_COLOR={CRITICAL:'#ef4444',HIGH:'#f59e0b',MEDIUM:'#8ecdf8',LOW:'#22c55e','GO':'#22c55e'};
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
async function refreshPreflight(){
  const el=document.getElementById('preflight-body');
  try{
    const r=await fetch('/api/preflight');
    const d=await r.json();
    if(!d.present){el.innerHTML='<span style="color:#687078">Preflight has not run yet for this run.</span>';return;}
    if(d.error){el.innerHTML='<span style="color:#ef4444">Preflight report unreadable: '+esc(d.error)+'</span>';return;}
    const v=d.verdict||'?';const vc=PF_VERDICT_COLOR[v]||'#8b98a5';
    const blk=d.blocking?'<span style="color:#ef4444;font-weight:600"> [BLOCKING]</span>':'<span style="color:#687078"> [advisory]</span>';
    let h='<div style="margin-bottom:6px"><span style="font-weight:600;color:'+vc+'">VERDICT: '+esc(v)+'</span>'+blk;
    if(d.verdict_meaning)h+=' <span style="color:#687078">— '+esc(d.verdict_meaning)+'</span>';
    h+='</div>';
    if(d.recommended_action)h+='<div style="margin-bottom:10px;padding:6px 8px;background:#12161c;border-left:2px solid '+vc+';color:#c4ccd4">→ <b>Recommended:</b> '+esc(d.recommended_action)+'</div>';
    const c=d.coverage||{};
    h+='<div style="margin-bottom:8px;color:#8b98a5">Survey: '+esc(c.total_source_files)+' files · '
       +esc(c.structural_parse_pct)+'% structurally parsed by code-graph '
       +'<span style="color:#687078">(NOT distillation coverage)</span> · '
       +esc(c.booked_untapped)+' booked-untapped · conserved='+esc(c.conserved)+'</div>';
    const m=d.materialized;
    if(m&&m.present)h+='<div style="margin-bottom:8px;color:#8b98a5">Materialized: '+esc(m.raw)+' raw · '+esc(m.inferred)+' inferred · '+esc(m.referential)+' referential</div>';
    // per-modality census table
    const pm=c.per_modality||{};
    if(Object.keys(pm).length){
      h+='<table style="width:100%;border-collapse:collapse;font-size:11px;margin:8px 0">'
        +'<tr style="color:#687078;text-align:left"><th style="padding:3px 8px">Modality</th><th style="padding:3px 8px">Reserves</th><th style="padding:3px 8px">Struct. rig</th><th style="padding:3px 8px">Extensions</th></tr>';
      for(const [name,mm] of Object.entries(pm)){
        const rig=mm.has_structural_rig?'<span style="color:#22c55e">yes</span>':'<span style="color:#f59e0b">no</span>';
        h+='<tr style="border-top:1px solid #2f3336"><td style="padding:3px 8px;color:#e7e9ea">'+esc(name)+'</td>'
          +'<td style="padding:3px 8px">'+esc(mm.reserves)+'</td>'
          +'<td style="padding:3px 8px">'+rig+'</td>'
          +'<td style="padding:3px 8px;color:#8b98a5">'+esc(mm.extensions_label||(mm.extensions||[]).join(', '))+'</td></tr>';
      }
      h+='</table>';
    }
    // findings
    const f=d.findings||[];
    if(f.length){
      h+='<div style="margin-top:8px">';
      for(const x of f){
        const sc=PF_SEV_COLOR[x.severity]||'#8b98a5';
        const xblk=x.blocking?'<span style="color:#ef4444">BLOCKING</span>':'<span style="color:#687078">advisory</span>';
        h+='<div style="margin:8px 0;padding-left:8px;border-left:2px solid '+sc+'">'
          +'<div><span style="color:'+sc+';font-weight:600">['+esc(x.severity)+'/</span>'+xblk+'<span style="color:'+sc+';font-weight:600">]</span> '
          +'<span style="color:#e7e9ea">'+esc(x.kind)+'</span> '
          +'<span style="color:#8b98a5">'+esc(x.why||'')+'</span></div>';
        const w=x.where||{};
        if(w.example_paths&&w.example_paths.length){
          h+='<div style="color:#687078;font-size:11px;margin-top:2px"><b>where</b> ('+esc(w.total_files)+' files) e.g. '+esc(w.example_paths.slice(0,5).join(', '))+'</div>';
        }
        if(x.action)h+='<div style="color:#9fb4c4;font-size:11px;margin-top:2px"><b>action</b> '+esc(x.action)+'</div>';
        if(x.why_it_matters)h+='<div style="color:#8b98a5;font-size:11px;margin-top:2px"><b>impact</b> '+esc(x.why_it_matters)+'</div>';
        h+='</div>';
      }
      h+='</div>';
    }
    el.innerHTML=h;
  }catch(e){el.innerHTML='<span style="color:#ef4444">preflight fetch failed</span>';}
}
refresh();
refreshTpm();
refreshPreflight();
setInterval(refresh,5000);
setInterval(refreshTpm,10000);
setInterval(refreshPreflight,15000);
</script>
</body></html>"""


class ProgressHandler(BaseHTTPRequestHandler):
    output_dir = ""
    
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/progress":
            data = get_progress(self.output_dir)
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/tpm":
            import telemetry
            tel_dir = str(Path(self.output_dir) / "telemetry")
            data = telemetry.aggregate_tpm(tel_dir)
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/preflight":
            # Surface the preflight census (written by run_distillation.sh Phase 1.5 to
            # preflight_report.json). Read-only; absent = preflight hasn't run yet (honest
            # "present": false, not a fabricated verdict). Malformed → reported as an error,
            # never swallowed into a fake all-clear.
            pf_path = Path(self.output_dir) / "preflight_report.json"
            if not pf_path.exists():
                data = {"present": False}
            else:
                try:
                    data = json.loads(pf_path.read_text())
                    data["present"] = True
                except (json.JSONDecodeError, IOError) as e:
                    data = {"present": True, "error": f"{type(e).__name__}: {e}"}
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/", "/index.html"):
            body = DASHBOARD_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)
    
    def log_message(self, format, *args):
        pass  # Quiet


def main():
    parser = argparse.ArgumentParser(description="Distillation progress dashboard")
    parser.add_argument("output_dir", help="Path to distillation output directory")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    
    ProgressHandler.output_dir = str(Path(args.output_dir).resolve())
    
    server = HTTPServer(("localhost", args.port), ProgressHandler)
    print(f"Progress dashboard at http://localhost:{args.port}")
    print(f"  Watching: {args.output_dir}")
    print(f"  Refreshes every 5s")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
