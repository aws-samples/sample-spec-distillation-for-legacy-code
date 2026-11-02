#!/usr/bin/env python3
"""
Generate HTML Viewer (v3) — Paginated fragment-based viewer.

Solves v2's size problem (50MB index, 317MB graph, 16-36MB per-still pages)
by splitting formatted still content into paginated HTML fragments loaded
on demand via fetch().

Produces a viewer/ directory with:
  - index.html (shell: tab bar, sidebar nav, iframe container, chat panel)
  - pages/executive_summary.html
  - pages/master_blend.html
  - pages/stills_{still_name}.html (paginated loader per still)
  - pages/knowledge_graph.html (stats + subset visualization)
  - fragments/{still}/{file_stem}/page_N.html (pre-rendered concept pages)
  - fragments/{still}/{file_stem}/manifest.json
  - assets/cytoscape.min.js

Each still page fetches fragment pages on demand (infinite scroll).
All v2 UI features are preserved: tooltips, evidence links, validate buttons,
View As... translation, chat panel, deep-link navigation, knowledge graph.

Usage:
    python generate-viewer-v3.py <distillation_output_dir> <target_name>
"""

import sys
import re
import json
import argparse
import shutil
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import viewer_security  # noqa: E402

try:
    import markdown
    from markdown.extensions.tables import TableExtension
    from markdown.extensions.fenced_code import FencedCodeExtension
    from markdown.extensions.toc import TocExtension
except ImportError:
    print("ERROR: 'markdown' package required. Install with: pip install markdown")
    sys.exit(1)


# ── Constants ────────────────────────────────────────────────────────────────

STILLS_ORDER = [
    "domain_language", "data", "workflow", "rule", "permission",
    "state", "exception", "test", "interface",
]

STILL_DISPLAY_NAMES = {
    "domain_language": "Domain Language", "data": "Data", "workflow": "Workflow",
    "rule": "Rule", "permission": "Permission", "state": "State",
    "exception": "Exception", "test": "Test", "interface": "Interface",
}

STILL_COLORS = {
    "domain_language": "#48bb78", "data": "#4299e1", "workflow": "#ed8936",
    "rule": "#e53e3e", "permission": "#9f7aea", "state": "#38b2ac",
    "exception": "#f56565", "test": "#667eea", "interface": "#ed64a6",
    # history and runtime were absent, so their concepts fell through to the grey fallback
    # and were indistinguishable from unattributed ones (20 of 200 on the AP run).
    "history": "#d69e2e", "runtime": "#319795",
}


# ── md_to_html — ported from v2 exactly ──────────────────────────────────────

def md_to_html(md_content: str, source_repo_url: str = "") -> str:
    """Convert markdown to HTML with v2 enhancements (tooltips, evidence links)."""
    md_content = re.sub(r'<(?=[a-zA-Z/])', '&lt;', md_content)

    def _convert_indented_fences(text):
        lines = text.split('\n')
        result = []
        in_fence = False
        fence_indent = 0
        fence_lang = ""
        code_lines = []
        for line in lines:
            if not in_fence:
                m = re.match(r'^(\s+)```\s*(\w*)\s*$', line)
                if m:
                    in_fence = True
                    fence_indent = len(m.group(1))
                    fence_lang = m.group(2)
                    code_lines = []
                    continue
            else:
                if re.match(r'^\s*```\s*$', line):
                    code_text = '\n'.join(code_lines)
                    code_text = code_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    if fence_lang == "formal":
                        result.append(f'{" " * fence_indent}<pre><code class="language-formal">{code_text}</code></pre>')
                    else:
                        result.append(f'{" " * fence_indent}<pre><code>{code_text}</code></pre>')
                    in_fence = False
                    code_lines = []
                    fence_lang = ""
                    continue
                else:
                    stripped = line[fence_indent:] if len(line) > fence_indent else line.lstrip()
                    code_lines.append(stripped)
                    continue
            result.append(line)
        if code_lines:
            code_text = '\n'.join(code_lines)
            code_text = code_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            if fence_lang == "formal":
                result.append(f'<pre><code class="language-formal">{code_text}</code></pre>')
            else:
                result.append(f'<pre><code>{code_text}</code></pre>')
        return '\n'.join(result)

    md_content = _convert_indented_fences(md_content)

    extensions = [TableExtension(), FencedCodeExtension(), TocExtension(permalink=False)]
    try:
        html_out = markdown.markdown(md_content, extensions=extensions + ['codehilite'])
    except Exception:
        html_out = markdown.markdown(md_content, extensions=extensions)

    # Linkify evidence citations
    def _linkify_evidence(match):
        filepath = match.group(1)
        line_ref = match.group(2)
        if source_repo_url:
            url = f"{source_repo_url.rstrip('/')}/{filepath}{line_ref}"
        else:
            url = f"#source:{filepath}{line_ref}"
        display = f"{filepath}{line_ref}"
        return f'<a href="{url}" target="_blank" class="evidence-link" data-path="{filepath}" data-lines="{line_ref}">{display}</a>'

    html_out = re.sub(
        r'([\w][\w./\-]+\.(?:py|ts|tsx|js|jsx|rs|rb|java|sql|yaml|yml|json|toml|sh|md|txt|cfg|ini|env|html|css|vue|svelte|vb|cs|bas|cls|frm|vbs|go|kt|cpp|php|xml|config))(#L\d+(?:-L\d+)?)',
        _linkify_evidence,
        html_out
    )

    # Score tooltips — Confidence
    score_legend = (
        '1 = Weak signal, likely noise\n'
        '2 = Plausible but thin evidence\n'
        '3 = Reasonable candidate requiring review\n'
        '4 = Strong signal, multiple artifacts\n'
        '5 = Durable business truth, strong convergence'
    )
    html_out = re.sub(
        r'(Confidence:\s*)(\d)',
        lambda m: f'{m.group(1)}{m.group(2)} <span class="score-info" data-legend="{score_legend}">ⓘ</span>',
        html_out
    )

    # Score tooltips — Business Relevance
    relevance_legend = (
        '1 = Technical artifact, no business meaning\n'
        '2 = Weak business connection\n'
        '3 = Moderate business relevance\n'
        '4 = Clear business significance\n'
        '5 = Core business truth'
    )
    html_out = re.sub(
        r'(Business Relevance:\s*)(\d)',
        lambda m: f'{m.group(1)}{m.group(2)} <span class="score-info" data-legend="{relevance_legend}">ⓘ</span>',
        html_out
    )

    # Score tooltips — Evidence Diversity
    evidence_legend = (
        '1 = Single source only\n'
        '2 = Two corroborating sources\n'
        '3 = Three or more sources\n'
        '4 = Multiple source types (code + tests + docs)\n'
        '5 = Pervasive across codebase'
    )
    html_out = re.sub(
        r'(Evidence Diversity:\s*)(\d)',
        lambda m: f'{m.group(1)}{m.group(2)} <span class="score-info" data-legend="{evidence_legend}">ⓘ</span>',
        html_out
    )

    # Transform formal code blocks into enhanced widget HTML
    def _formal_widget(match):
        code_content = match.group(1)
        # Syntax highlight formal keywords — single pass to avoid span-in-span
        lines = code_content.split('\n')
        formatted_lines = []
        for i, line in enumerate(lines, 1):
            # Tokenize: split into quoted strings and everything else
            # Process non-string parts for keyword highlighting
            parts = re.split(r'(&quot;[^&]*?&quot;|"[^"]*?")', line)
            highlighted_parts = []
            for part in parts:
                if part.startswith('&quot;') or part.startswith('"'):
                    highlighted_parts.append('<span class="val">' + part + '</span>')
                else:
                    # Highlight keywords in non-string parts
                    part = re.sub(
                        r'\b(REQUIRE|PERMIT|PROHIBIT|DENY|FORBID|WARN|DEFAULT|ENABLE|REJECT|FLAG|DERIVE)\b',
                        r'<span class="kw">\1</span>', part)
                    part = re.sub(
                        r'\b(AND|OR|NOT|WHERE|IF|THEN|ELSE|WHEN|FOR|ACROSS|WITHIN|EVERY|IN|IS|IMPLIES|INCLUDING)\b',
                        r'<span class="op">\1</span>', part)
                    highlighted_parts.append(part)
            hl = ''.join(highlighted_parts)
            num_span = '<span class="line-num">{:d}</span>'.format(i)
            formatted_lines.append(num_span + hl)
        content_html = '\n'.join(formatted_lines)
        return (
            '<div class="formal-block-wrap">'
            '<pre class="formal-block">'
            '<div class="formal-header">'
            '<span class="formal-badge">Formal Spec</span>'
            '<div class="formal-actions">'
            '<button class="copy-btn" onclick="copyFormal(this)">Copy</button>'
            '</div></div>'
            '<div class="formal-content">' + content_html + '</div>'
            '</pre></div>'
        )

    html_out = re.sub(
        r'<pre><code class="language-formal">(.*?)</code></pre>',
        _formal_widget,
        html_out,
        flags=re.DOTALL
    )

    return html_out


# ── Helpers ──────────────────────────────────────────────────────────────────

def extract_h2_toc(html_content: str) -> list:
    """Extract H2 and H3 headings with their IDs for sidebar navigation."""
    entries = []
    for m in re.finditer(r'<h([23])[^>]*id="([^"]*)"[^>]*>([^<]+)</h\1>', html_content):
        level, h_id, h_text = int(m.group(1)), m.group(2), m.group(3).strip()
        if "table of contents" in h_text.lower():
            continue
        entries.append({"id": h_id, "title": h_text, "level": level})
    return entries


def split_into_sections(md_content: str) -> tuple:
    """Split markdown on ## headers. Each section = one H2 + all its child ### concepts.

    Returns:
        (preamble, sections) where:
          - preamble = everything before the first ## header (file title, intro text)
          - sections = list of (title, body) tuples; each body includes the ## line
            and all content until the next ##
    """
    lines = md_content.split("\n")
    preamble_lines = []
    sections = []
    current_section_lines = []
    in_section = False

    for line in lines:
        if line.startswith("## ") and not line.startswith("### "):
            if in_section and current_section_lines:
                sections.append("\n".join(current_section_lines))
            current_section_lines = [line]
            in_section = True
        elif in_section:
            current_section_lines.append(line)
        else:
            preamble_lines.append(line)

    # Last section
    if in_section and current_section_lines:
        sections.append("\n".join(current_section_lines))

    preamble = "\n".join(preamble_lines)
    return preamble, sections


def read_md_files(directory: Path, skip_readme: bool = False) -> str:
    """Read and concatenate all .md files in a directory."""
    files = sorted(directory.glob("*.md"))
    parts = []
    for f in files:
        if skip_readme and f.name == "README.md":
            continue
        parts.append(f.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


# ── Page CSS (shared across all content pages) ───────────────────────────────

PAGE_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;line-height:1.6;color:#1a1a2e;background:#fff;margin:0;padding:24px 32px}
h1{font-size:1.75rem;font-weight:700;color:#1a202c;margin:0 0 16px}
h2{font-size:1.4rem;font-weight:600;color:#fff;background:#1e293b;margin:32px -32px 16px -32px;scroll-margin-top:64px;padding:12px 32px;border-radius:0}
h3{font-size:1.15rem;font-weight:600;color:#4a5568;margin:24px -32px 8px -32px;padding:20px 32px 0 32px;scroll-margin-top:64px;border-top:1px solid #e2e8f0}
h2 + h3{border-top:none;margin-top:16px;padding-top:0}
.toc-section{scroll-margin-top:64px}
h4,h5,h6{font-size:1rem;font-weight:600;color:#4a5568;margin:16px 0 8px}
p{margin:0 0 12px;color:#2d3748}
ul,ol{margin:0 0 12px;padding-left:24px}
li{margin-bottom:4px}
table{width:100%;border-collapse:collapse;margin:16px 0;font-size:14px}
th,td{border:1px solid #e2e8f0;padding:8px 12px;text-align:left}
th{background:#f7fafc;font-weight:600;color:#2d3748}
tr:nth-child(even){background:#f8fafc}
code{background:#f1f5f9;padding:2px 6px;border-radius:3px;font-size:.9em;font-family:"SF Mono",Menlo,monospace}
pre{background:#1e293b;color:#e2e8f0;padding:16px 20px;border-radius:6px;overflow-x:auto;margin:16px 0;font-size:15px;line-height:1.5}
pre code{background:none;padding:0;color:inherit;font-size:15px}
pre.formal-block{background:#0f172a;border:1px solid #334155;border-radius:10px;padding:0;margin:20px 0;overflow:visible}
.formal-block-wrap{position:relative;overflow:visible}
.formal-header{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;background:#1e293b;border-bottom:1px solid #334155;border-radius:10px 10px 0 0;position:relative;z-index:20}
.formal-header .formal-badge{font-size:11px;font-weight:600;color:#fff;padding:2px 0;text-transform:uppercase;letter-spacing:0.5px}
.formal-header .formal-actions{display:flex;gap:6px;align-items:center}
.formal-header .copy-btn{padding:4px 10px;font-size:11px;background:#334155;color:#94a3b8;border:none;border-radius:4px;cursor:pointer}
.formal-header .copy-btn:hover{background:#475569;color:#fff}
.formal-content{padding:16px 20px;font-family:"SF Mono",Menlo,Consolas,monospace;font-size:15px;line-height:1.7;color:#e2e8f0;overflow-x:auto}
.formal-content .line-num{display:inline-block;width:32px;color:#475569;text-align:right;margin-right:16px;user-select:none;font-size:13px}
.formal-content .kw{color:#f59e0b;font-weight:600}
.formal-content .op{color:#94a3b8}
.formal-content .val{color:#34d399}
.formal-translation{display:none;border-top:1px solid #334155}
.formal-translation.open{display:block}
.formal-translation .xlate-title{padding:8px 20px;font-size:11px;font-weight:600;color:#f59e0b;text-transform:uppercase;letter-spacing:0.5px;background:#1e293b;display:flex;align-items:center;justify-content:space-between}
.formal-translation .xlate-copy{padding:3px 10px;font-size:11px;background:#334155;color:#94a3b8;border:none;border-radius:4px;cursor:pointer;text-transform:none;letter-spacing:0}
.formal-translation .xlate-copy:hover{background:#475569;color:#fff}
.formal-translation .xlate-body pre{margin:0;padding:16px 20px;background:#0f172a;color:#e2e8f0;font-size:14px;line-height:1.6;overflow-x:auto;border-radius:0 0 10px 10px}
.formal-translation .xlate-loading{padding:16px 20px;color:#64748b;font-style:italic;font-family:-apple-system,sans-serif}
blockquote{border-left:4px solid #ff9900;padding:8px 16px;margin:16px 0;background:#fffbf0;color:#4a5568}
a.evidence-link{color:#2563eb;text-decoration:none;font-family:"SF Mono",Menlo,monospace;font-size:.85em;background:#f0f4ff;padding:1px 4px;border-radius:3px}
a.evidence-link:hover{text-decoration:underline;background:#dbeafe}
.validate-btn{display:inline-block;padding:2px 8px;font-size:11px;background:#f0fdf4;color:#166534;border:1px solid #86efac;border-radius:4px;cursor:pointer;font-weight:500;opacity:0.7;transition:opacity 0.2s;margin-left:10px;vertical-align:middle}
.validate-btn:hover{opacity:1;background:#dcfce7}
.deps-btn{display:inline-block;padding:2px 8px;font-size:11px;background:#eff6ff;color:#1e40af;border:1px solid #93c5fd;border-radius:4px;cursor:pointer;font-weight:500;opacity:0.7;transition:opacity 0.2s;margin-left:10px;vertical-align:middle}
.deps-btn:hover{opacity:1;background:#dbeafe}
hr{border:none;border-top:1px solid #e2e8f0;margin:24px 0}
strong{font-weight:600;color:#1a202c}
.score-info{display:inline-block;cursor:default;font-size:16px;color:#6b7280;margin-left:4px;opacity:0.7;transition:opacity 0.2s;vertical-align:middle;position:relative}
.score-info:hover{opacity:1;color:#f59e0b}
.score-info:hover::after{content:attr(data-legend);position:absolute;left:50%;bottom:calc(100% + 6px);transform:translateX(-50%);background:#1a202c;color:#e2e8f0;padding:10px 14px;border-radius:6px;font-size:13px;line-height:1.6;white-space:pre-line;z-index:999;min-width:260px;max-width:320px;box-shadow:0 4px 12px rgba(0,0,0,0.3);pointer-events:none}
.score-info:hover::before{content:'';position:absolute;left:50%;bottom:calc(100% + 2px);transform:translateX(-50%);border:5px solid transparent;border-top-color:#1a202c;z-index:999}
.view-as-wrap{position:absolute;top:0;right:0;z-index:10}
.view-as-btn{padding:4px 10px;font-size:11px;background:rgba(248,250,252,0.95);color:#475569;border:none;border-top-right-radius:6px;border-bottom-left-radius:4px;cursor:pointer;font-weight:500}
.view-as-btn:hover{background:#e2e8f0}
.view-as-menu{display:none;position:absolute;right:0;top:calc(100% + 4px);background:#fff;border:1px solid #e2e8f0;border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,0.15);z-index:1000;min-width:120px;padding:4px 0}
.view-as-menu.open{display:block}
.view-as-menu a{display:block;padding:6px 14px;font-size:12px;color:#1e293b;text-decoration:none;cursor:pointer}
.view-as-menu a:hover{background:#f1f5f9}
pre.has-view-as{padding-top:32px}
.translate-modal{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:9999;align-items:center;justify-content:center}
.translate-modal.open{display:flex}
.translate-modal-content{background:#fff;border-radius:10px;width:90%;max-width:700px;max-height:80vh;overflow-y:auto;padding:24px;position:relative;box-shadow:0 8px 32px rgba(0,0,0,0.2)}
.translate-modal-close{position:absolute;top:12px;right:16px;font-size:22px;color:#64748b;cursor:pointer;border:none;background:none;line-height:1}
.translate-modal-close:hover{color:#1e293b}
.translate-modal pre{background:#1e293b;color:#e2e8f0;padding:16px;border-radius:8px;overflow-x:auto;font-size:13px;line-height:1.5}
.translate-modal h3{margin:0 0 12px;font-size:15px;color:#1e293b}
.translate-modal .loading{color:#64748b;font-style:italic}
.file-tabs{display:flex;gap:4px;padding:8px 16px;background:#f8fafc;border-bottom:1px solid #e2e8f0;flex-wrap:wrap;position:sticky;top:0;z-index:100;margin:-24px -32px 16px -32px}
.file-tabs button{padding:4px 12px;font-size:12px;background:#fff;border:1px solid #e2e8f0;border-radius:4px;cursor:pointer;color:#4a5568}
.file-tabs button:hover{background:#edf2f7}
.file-tabs button.active{background:#ff9900;color:#fff;border-color:#ff9900}
.pagination{display:flex;gap:8px;align-items:center;padding:16px;justify-content:center;border-top:1px solid #e2e8f0;margin-top:24px}
.pagination button{padding:6px 16px;background:#ff9900;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:13px;font-weight:500}
.pagination button:hover{background:#e08800}
.pagination button:disabled{background:#d1d5db;cursor:not-allowed}
.pagination .info{font-size:12px;color:#6b7280}
#content-area{min-height:200px}
.loading-indicator{text-align:center;padding:24px;color:#6b7280;font-style:italic}
"""


# ── Fragment generation ──────────────────────────────────────────────────────

def generate_still_fragments(still_name: str, formatted_dir: Path,
                             fragments_dir: Path, source_repo_url: str) -> dict:
    """Generate H2-section-aligned HTML fragments for a single still.

    Each fragment = one complete H2 section wrapped in a <div class="toc-section">.
    This ensures TOC tracking works regardless of scroll position — the
    IntersectionObserver watches the entire section container.

    Returns manifest dict: {files: [{stem, pages, concepts, toc}]}
    """
    still_fragments_dir = fragments_dir / still_name
    still_fragments_dir.mkdir(parents=True, exist_ok=True)

    manifest = {"still": still_name, "files": []}
    concept_index = []  # [{title, stem, page}] — lazy-loaded for cross-ref nav

    md_files = sorted(formatted_dir.glob("*.md"))
    md_files = [f for f in md_files if f.name != "README.md"]

    for md_file in md_files:
        stem = md_file.stem
        file_dir = still_fragments_dir / stem
        file_dir.mkdir(parents=True, exist_ok=True)

        md_content = md_file.read_text(encoding="utf-8")
        preamble, sections = split_into_sections(md_content)

        # Count total ### concepts across all sections
        total_concepts = sum(
            s.count("\n### ") + (1 if s.lstrip().startswith("### ") else 0)
            for s in sections
        )

        # Page 0 = preamble (file title, intro text) if non-trivial
        file_toc = []  # [{id, title, page}] — H2 sections
        page_num = 0

        if preamble.strip():
            preamble_html = md_to_html(preamble, source_repo_url)
            frag_path = file_dir / f"page_{page_num}.html"
            frag_path.write_text(preamble_html, encoding="utf-8")
            page_num += 1

        # Each subsequent page = one H2 section, wrapped in a tracking div
        for section_md in sections:
            section_html = md_to_html(section_md, source_repo_url)

            # Collect concept-level index entries (### headings) for this page
            for cm in re.finditer(r'<h3[^>]*>(.+?)</h3>', section_html, re.DOTALL):
                raw = re.sub(r'<[^>]+>', '', cm.group(1)).strip()
                if raw:
                    concept_index.append({"title": raw, "stem": stem, "page": page_num})

            # Extract the H2 id and title from rendered HTML
            h2_match = re.search(r'<h2[^>]*id="([^"]*)"[^>]*>([^<]+)</h2>', section_html)
            if h2_match:
                h2_id = h2_match.group(1)
                h2_title = h2_match.group(2).strip()
                # Wrap in tracking div (move id from h2 to div)
                section_html = re.sub(
                    r'<h2([^>]*) id="[^"]*"([^>]*)>',
                    r'<h2\1\2>',
                    section_html,
                    count=1,
                )
                section_html = (
                    f'<div class="toc-section" id="{h2_id}">\n'
                    f'{section_html}\n'
                    f'</div>'
                )
                file_toc.append({"id": h2_id, "title": h2_title, "page": page_num})

            frag_path = file_dir / f"page_{page_num}.html"
            frag_path.write_text(section_html, encoding="utf-8")
            page_num += 1

        total_pages = page_num
        file_manifest = {
            "stem": stem,
            "pages": total_pages,
            "concepts": total_concepts,
            "toc": file_toc,
        }
        manifest["files"].append(file_manifest)

        print(f"    {stem}: {total_concepts} concepts, {len(file_toc)} sections, {total_pages} pages")

    # Write manifest (lean — H2 sections only, loaded on init)
    manifest_path = still_fragments_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Write concept index (loaded lazily only for cross-ref navigation)
    concepts_path = still_fragments_dir / "concepts.json"
    concepts_path.write_text(json.dumps(concept_index), encoding="utf-8")

    return manifest


# ── Still page generation (paginated loader) ─────────────────────────────────

STILL_PAGE_JS = """
// ── Lazy-loading section viewer (no eviction) ──
// Each page = one H2 section wrapped in a .toc-section div.
// Loads first 5 sections on init, loads more on scroll (sentinel at bottom).
// TOC click loads target section + neighbors if not already present.

let currentFile = null;
let manifest = null;
let loadedPages = new Set(); // page numbers currently in DOM
let loading = new Set();
let totalPages = 0;
let highestLoaded = -1;

async function init() {
  const resp = await fetch('../fragments/' + STILL_NAME + '/manifest.json');
  manifest = await resp.json();
  if (manifest.files.length > 0) {
    const tabsEl = document.getElementById('file-tabs');
    manifest.files.forEach((f, i) => {
      const btn = document.createElement('button');
      btn.textContent = f.stem.replace(/_/g, ' ');
      btn.onclick = () => switchFile(i);
      if (i === 0) btn.classList.add('active');
      tabsEl.appendChild(btn);
    });
    switchFile(0);
  }
}

function switchFile(index) {
  const f = manifest.files[index];
  currentFile = f;
  totalPages = f.pages;
  highestLoaded = -1;
  loadedPages.clear();
  loading.clear();
  document.getElementById('content-area').innerHTML = '';
  document.querySelectorAll('#file-tabs button').forEach((btn, i) => {
    btn.classList.toggle('active', i === index);
  });
  // Send full TOC to parent
  window.parent.postMessage({type: 'toc-update', toc: f.toc || []}, '*');
  // Load initial batch (first 5 sections)
  loadNextBatch(5);
  updateStatus();
}

async function loadNextBatch(count) {
  const start = highestLoaded + 1;
  const end = Math.min(start + count - 1, totalPages - 1);
  for (let p = start; p <= end; p++) {
    await loadPage(p);
  }
  setupSentinel();
  updateStatus();
}

async function loadPage(pageNum) {
  if (pageNum < 0 || pageNum >= totalPages || loadedPages.has(pageNum) || loading.has(pageNum)) return;
  loading.add(pageNum);
  const url = '../fragments/' + STILL_NAME + '/' + currentFile.stem + '/page_' + pageNum + '.html';
  try {
    const resp = await fetch(url);
    const html = await resp.text();
    const wrapper = document.createElement('div');
    wrapper.className = 'page-chunk';
    wrapper.dataset.page = pageNum;
    wrapper.innerHTML = html;
    // Insert in order
    const area = document.getElementById('content-area');
    const existing = Array.from(area.querySelectorAll('.page-chunk'));
    let inserted = false;
    for (const ex of existing) {
      if (parseInt(ex.dataset.page) > pageNum) {
        area.insertBefore(wrapper, ex);
        inserted = true;
        break;
      }
    }
    if (!inserted) area.appendChild(wrapper);
    loadedPages.add(pageNum);
    if (pageNum > highestLoaded) highestLoaded = pageNum;
    // Activate interactive features
    activateValidateButtons(wrapper);
    activateViewAsButtons(wrapper);
    // Re-observe sections for TOC highlighting
    observeCurrentSections();
  } catch (e) {
    console.error('Failed to load page ' + pageNum, e);
  }
  loading.delete(pageNum);
}

// ── Load a range of pages (for TOC jump) ──
async function loadRange(startPage, endPage) {
  for (let p = startPage; p <= endPage; p++) {
    await loadPage(p);
  }
  setupSentinel();
  updateStatus();
}

// ── Bottom sentinel for infinite scroll ──
let sentinel = null;
let sentinelObserver = null;

function setupSentinel() {
  if (sentinel) sentinel.remove();
  const area = document.getElementById('content-area');
  if (highestLoaded < totalPages - 1) {
    sentinel = document.createElement('div');
    sentinel.className = 'sentinel';
    sentinel.style.height = '1px';
    area.appendChild(sentinel);
    if (!sentinelObserver) {
      sentinelObserver = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            loadNextBatch(3);
          }
        }
      }, {root: null, rootMargin: '400px', threshold: 0});
    }
    sentinelObserver.observe(sentinel);
  }
}

function updateStatus() {
  const info = document.getElementById('page-info');
  info.textContent = loadedPages.size + ' of ' + totalPages + ' sections loaded (' + currentFile.concepts + ' concepts total)';
}

// ── Cross-reference: jump to a specific concept (### heading) ──
let conceptIndex = null;

function normTitle(t) {
  // Strip leading [XX-NNNN] id prefix, lowercase, collapse whitespace
  return t.replace(/^\\[[A-Z]+-\\d+\\]\\s*/, '').replace(/\\s+/g, ' ').trim().toLowerCase();
}

async function scrollToConcept(title, _retries) {
  _retries = _retries || 0;
  // Wait for manifest to be ready (still page may still be initializing)
  if (manifest === null) {
    if (_retries < 20) { setTimeout(() => scrollToConcept(title, _retries + 1), 150); }
    return;
  }
  if (conceptIndex === null) {
    try {
      const resp = await fetch('../fragments/' + STILL_NAME + '/concepts.json');
      conceptIndex = await resp.json();
    } catch (e) { conceptIndex = []; }
  }
  const target = normTitle(title);
  // Exact match first, then startswith fallback
  let hit = conceptIndex.find(c => normTitle(c.title) === target);
  if (!hit) hit = conceptIndex.find(c => normTitle(c.title).startsWith(target) || target.startsWith(normTitle(c.title)));
  if (!hit) return;

  // Switch to the right file tab if needed
  const fileIdx = manifest.files.findIndex(f => f.stem === hit.stem);
  if (fileIdx >= 0 && (!currentFile || currentFile.stem !== hit.stem)) {
    switchFile(fileIdx);
  }
  const start = Math.max(0, hit.page - 1);
  const end = Math.min(totalPages - 1, hit.page + 1);
  await loadRange(start, end);
  // Find the heading element by matching text and scroll to it
  setTimeout(() => {
    const heads = document.querySelectorAll('#content-area h3');
    for (const h of heads) {
      if (normTitle(h.textContent) === target || normTitle(h.textContent).startsWith(target)) {
        h.scrollIntoView({behavior: 'smooth', block: 'start'});
        h.style.background = '#fef3c7';
        setTimeout(() => h.style.background = '', 3000);
        break;
      }
    }
  }, 120);
}

// ── TOC click: jump to target page ──
window.addEventListener('message', (event) => {
  if (!event.data) return;
  if (event.data.type === 'scrollToConcept') {
    scrollToConcept(event.data.title);
    return;
  }
  if (event.data.type === 'scrollTo') {
    const id = event.data.id;
    const targetPage = event.data.page;
    if (targetPage !== undefined && targetPage !== null) {
      // Load target + neighbors, then scroll
      const start = Math.max(0, targetPage - 1);
      const end = Math.min(totalPages - 1, targetPage + 1);
      loadRange(start, end).then(() => {
        setTimeout(() => {
          const el = document.getElementById(id);
          if (el) {
            el.scrollIntoView({behavior: 'smooth', block: 'start'});
            el.style.background = '#fef3c7';
            setTimeout(() => el.style.background = '', 3000);
          }
        }, 50);
      });
    } else {
      const el = document.getElementById(id);
      if (el) {
        el.scrollIntoView({behavior: 'smooth', block: 'start'});
        el.style.background = '#fef3c7';
        setTimeout(() => el.style.background = '', 3000);
      }
    }
  }
});

// ── IntersectionObserver for TOC highlighting ──
const visibleHeadings = new Map();
let sectionObserver = null;

function observeCurrentSections() {
  if (sectionObserver) sectionObserver.disconnect();
  sectionObserver = new IntersectionObserver((entries) => {
    entries.forEach(e => {
      if (e.isIntersecting) visibleHeadings.set(e.target.id, e.intersectionRatio);
      else visibleHeadings.delete(e.target.id);
    });
    window.parent.postMessage({type: 'toc-visibility', visible: Object.fromEntries(visibleHeadings)}, '*');
  }, {threshold: [0, 0.05, 0.1, 0.25]});
  document.querySelectorAll('#content-area .toc-section[id]').forEach(s => sectionObserver.observe(s));
}

// ── Interactive features ──
function activateValidateButtons(container) {
  container.querySelectorAll('h3').forEach(h3 => {
    const title = h3.childNodes[0] ? h3.childNodes[0].textContent.trim() : h3.textContent.trim();
    h3.style.position = 'relative';

    // Dependencies button — available on every concept
    const depBtn = document.createElement('button');
    depBtn.textContent = 'Dependencies';
    depBtn.className = 'deps-btn';
    depBtn.onclick = () => {
      window.parent.postMessage({type: 'deps-block', title: title, still: STILL_NAME}, '*');
    };
    h3.appendChild(depBtn);

    // Validate button — only when the concept has evidence
    const nextUl = h3.nextElementSibling?.tagName === 'UL' ? h3.nextElementSibling :
                   h3.nextElementSibling?.nextElementSibling?.tagName === 'UL' ? h3.nextElementSibling.nextElementSibling : null;
    if (!nextUl) return;
    const evidenceLi = Array.from(nextUl.querySelectorAll('li')).find(li => li.textContent.startsWith('Evidence:'));
    if (!evidenceLi) return;
    const btn = document.createElement('button');
    btn.textContent = 'Validate';
    btn.className = 'validate-btn';
    btn.onclick = () => {
      let blockText = title + '\\n';
      let sibling = h3.nextElementSibling;
      while (sibling && !sibling.matches('h2,h3')) {
        blockText += sibling.textContent + '\\n';
        sibling = sibling.nextElementSibling;
      }
      const evidenceText = evidenceLi.textContent.replace('Evidence:', '').trim();
      window.parent.postMessage({type: 'validate-block', title: title, block: blockText.trim(), evidence: evidenceText}, '*');
    };
    h3.appendChild(btn);
  });
}

function activateViewAsButtons(container) {
  const LANGS = ['Python', 'Node.js', 'C#', 'Java', 'Go', 'Ruby', 'C'];
  container.querySelectorAll('.formal-block-wrap').forEach(wrap => {
    const contentEl = wrap.querySelector('.formal-content');
    const block = wrap.querySelector('.formal-block');
    if (!contentEl || !block) return;
    const rawText = contentEl.textContent.replace(/^\\d+\\s*/gm, '').trim();
    const actions = wrap.querySelector('.formal-actions');
    if (!actions) return;

    // Inline translation output area appended to the bottom of the formal block
    let xlate = block.querySelector('.formal-translation');
    if (!xlate) {
      xlate = document.createElement('div');
      xlate.className = 'formal-translation';
      xlate.innerHTML = '<div class="xlate-title"><span class="xlate-lang"></span><button class="xlate-copy" onclick="copyTranslation(this)">Copy</button></div><div class="xlate-body"></div>';
      block.appendChild(xlate);
    }

    // "View as..." button + language menu
    const btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.textContent = 'View as\u2026';
    btn.style.background = '#475569';
    const menu = document.createElement('div');
    menu.className = 'view-as-menu';
    menu.style.position = 'absolute';
    menu.style.right = '0';
    menu.style.top = '100%';
    LANGS.forEach(lang => {
      const a = document.createElement('a');
      a.textContent = lang;
      a.onclick = () => { menu.classList.remove('open'); translateInline(xlate, rawText, lang); };
      menu.appendChild(a);
    });
    btn.onclick = (e) => { e.stopPropagation(); menu.classList.toggle('open'); };
    document.addEventListener('click', () => menu.classList.remove('open'));
    const menuWrap = document.createElement('div');
    menuWrap.style.position = 'relative';
    menuWrap.appendChild(btn);
    menuWrap.appendChild(menu);
    actions.appendChild(menuWrap);
  });
}

// Inline translation: populate the block's translation area and reveal it
async function translateInline(xlate, code, lang) {
  const langEl = xlate.querySelector('.xlate-lang');
  const bodyEl = xlate.querySelector('.xlate-body');
  langEl.textContent = lang;
  bodyEl.innerHTML = '<div class="xlate-loading">Translating\\u2026</div>';
  xlate.classList.add('open');
  try {
    const r = await fetch(STILL_API_BASE + '/api/translate', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code: code, language: lang})});
    const d = await r.json();
    if (d.error) {
      bodyEl.innerHTML = '<div class="xlate-loading" style="color:#f87171"></div>';
      bodyEl.firstChild.textContent = d.error;
    } else {
      const clean = d.code.replace(/^```\\w*\\n/, '').replace(/\\n```\\s*$/, '');
      bodyEl.innerHTML = '<pre><code></code></pre>';
      bodyEl.querySelector('code').textContent = clean;
    }
  } catch(e) {
    bodyEl.innerHTML = '<div class="xlate-loading" style="color:#f87171">Translation unavailable. Is the server running?</div>';
  }
}

// Copy translated code to clipboard
window.copyTranslation = function(btn) {
  const xlate = btn.closest('.formal-translation');
  const code = xlate ? xlate.querySelector('.xlate-body code') : null;
  if (code) {
    navigator.clipboard.writeText(code.textContent);
    btn.textContent = '\\u2713 Copied';
    setTimeout(() => btn.textContent = 'Copy', 1500);
  }
};

// Copy formal block content
window.copyFormal = function(btn) {
  const wrap = btn.closest('.formal-block');
  const content = wrap.querySelector('.formal-content');
  if (content) {
    const text = content.textContent.replace(/^\\d+\\s*/gm, '').trim();
    navigator.clipboard.writeText(text);
    btn.textContent = '\u2713';
    setTimeout(() => btn.textContent = 'Copy', 1500);
  }
};

init();
"""


def generate_still_page(still_name: str, still_manifest: dict,
                        pages_dir: Path, api_base: str = ""):
    """Generate the paginated still page that loads fragments on demand."""
    display_name = STILL_DISPLAY_NAMES.get(still_name, still_name.replace('_', ' ').title())

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{display_name} Still</title>
<style>{PAGE_CSS}
.sentinel{{height:1px;width:100%}}
.page-chunk{{border-bottom:1px solid #f0f0f0}}
.page-chunk + .page-chunk{{margin-top:24px}}
.toc-section h2{{position:sticky;top:40px;z-index:50;margin-top:0}}
#status-bar{{position:sticky;bottom:0;background:#f8fafc;border-top:1px solid #e2e8f0;padding:8px 16px;font-size:12px;color:#6b7280;text-align:center}}
</style>
</head>
<body>
<div id="file-tabs" class="file-tabs"></div>
<div id="content-area"></div>
<div id="status-bar"><span id="page-info"></span></div>
<script>
const STILL_NAME = '{still_name}';
const STILL_API_BASE = '{api_base}';
{STILL_PAGE_JS}
</script>
</body>
</html>"""
    path = pages_dir / f"stills_{still_name}.html"
    path.write_text(page_html, encoding="utf-8")


# ── Standalone content page (executive summary, master blend) ────────────────

def write_content_page(path: Path, title: str, html_body: str):
    """Write a standalone HTML content page (loaded in iframe)."""
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>{PAGE_CSS}</style>
</head>
<body>
{html_body}
<script>
(function(){{
  const headings=document.querySelectorAll('h2[id],h3[id],h4[id],h5[id]');
  if(!headings.length)return;
  // Publish this page's own contents to the shell sidebar. Only the fragment-based still
  // pages used to do this, so the sidebar sat on "Select a tab to see sections" for Master
  // Blend, Executive Summary and Knowledge Graph — the tabs a reader opens first, and the
  // largest document of the set.
  //
  // Sections and subsections only (h2/h3). The blend also carries ~780 h4 entries, one per
  // concept; listing those turns the sidebar into a wall of near-identical titles and makes
  // the filter box useless. Scroll tracking below still observes every level, so the
  // highlight follows the reader into h4 content even though it is not listed.
  window.parent.postMessage({{type:'toc-update',toc:Array.from(headings)
    .filter(h=>h.tagName==='H2'||h.tagName==='H3')
    .map(h=>({{
      id:h.id,
      title:(h.textContent||'').trim(),
      level:parseInt(h.tagName.substring(1),10)
    }}))}},'*');
  const visible=new Map();
  function postState(){{
    const msg={{type:'toc-visibility',visible:Object.fromEntries(visible)}};
    window.parent.postMessage(msg,'*');
  }}
  const observer=new IntersectionObserver((entries)=>{{
    entries.forEach(e=>{{
      if(e.isIntersecting){{visible.set(e.target.id,e.intersectionRatio)}}
      else{{visible.delete(e.target.id)}}
    }});
    postState();
  }},{{threshold:[0,0.25,0.5,0.75,1.0]}});
  headings.forEach(h=>observer.observe(h));
  let scrollTimer=null;
  window.addEventListener('scroll',()=>{{
    if(scrollTimer)clearTimeout(scrollTimer);
    scrollTimer=setTimeout(postState,100);
  }},true);
  // Honour the shell's sidebar clicks. write_doc_page has always listened for this;
  // write_content_page did not, so sidebar entries on the pages it produces (Evidence &
  // Audit Trail, Executive Summary) published a table of contents that did nothing when
  // clicked — the same links worked on Business Spec, which uses the other writer.
  window.addEventListener('message',(e)=>{{
    if(e.data&&e.data.type==='scrollTo'){{
      const el=document.getElementById(e.data.id);
      if(el)el.scrollIntoView({{behavior:'smooth',block:'start'}});
    }}
  }});
  if(location.hash){{
    const el=document.getElementById(location.hash.slice(1));
    if(el)el.scrollIntoView();
  }}
  // Validate buttons
  document.querySelectorAll('h3').forEach(h3=>{{
    const nextUl=h3.nextElementSibling?.tagName==='UL'?h3.nextElementSibling:
                 h3.nextElementSibling?.nextElementSibling?.tagName==='UL'?h3.nextElementSibling.nextElementSibling:null;
    if(!nextUl)return;
    const evidenceLi=Array.from(nextUl.querySelectorAll('li')).find(li=>li.textContent.startsWith('Evidence:'));
    if(!evidenceLi)return;
    const btn=document.createElement('button');
    btn.textContent='Validate';
    btn.className='validate-btn';
    btn.onclick=()=>{{
      const title=h3.childNodes[0]?h3.childNodes[0].textContent.trim():h3.textContent.trim();
      let blockText=title+'\\n';
      let sibling=h3.nextElementSibling;
      while(sibling&&!sibling.matches('h2,h3')){{
        blockText+=sibling.textContent+'\\n';
        sibling=sibling.nextElementSibling;
      }}
      const evidenceText=evidenceLi.textContent.replace('Evidence:','').trim();
      window.parent.postMessage({{
        type:'validate-block',
        title:title,
        block:blockText.trim(),
        evidence:evidenceText
      }},'*');
    }};
    h3.style.position='relative';
    h3.appendChild(btn);
  }});
}})();
</script>
</body>
</html>"""
    path.write_text(page, encoding="utf-8")


# ── Issues & Insights page + bottled business spec page ──────────────────────

# Shared script for standalone document pages: publishes the heading TOC to the shell sidebar
# (toc-update) and honours the shell's scrollTo requests.
DOC_PAGE_TOC_JS = """
<script>
(function(){
  const toc=JSON.parse(document.getElementById('toc-data').textContent||'[]');
  window.parent.postMessage({type:'toc-update',toc:toc},'*');
  window.addEventListener('message',(e)=>{
    if(e.data&&e.data.type==='scrollTo'){
      const el=document.getElementById(e.data.id);
      if(el){el.scrollIntoView({behavior:'smooth',block:'start'});}
    }
  });
})();
</script>"""


def _json_for_script(obj) -> str:
    """JSON safe to embed inside <script type="application/json">."""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def write_doc_page(path: Path, title: str, html_body: str, toc: list, extra_head: str = "",
                   extra_js: str = ""):
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>{PAGE_CSS}</style>
{extra_head}
</head>
<body>
{html_body}
<script type="application/json" id="toc-data">{_json_for_script(toc)}</script>
{DOC_PAGE_TOC_JS}
{extra_js}
</body>
</html>"""
    path.write_text(page, encoding="utf-8")


def generate_bottled_spec_page(base_dir: Path, pages_dir: Path, source_repo_url: str = "") -> bool:
    """Render the bottled business specification itself (not just its README)."""
    for cand in (base_dir / "bottled_spec_final" / "bottled_spec.md",
                 base_dir / "bottled_spec" / "bottled_spec.md"):
        if cand.is_file() and cand.stat().st_size > 0:
            md = cand.read_text(encoding="utf-8", errors="replace")
            # The bottled spec inherits its H1 from the blend it was culled from, so this
            # customer-facing page has been titled "<target> — Master Blend Specification":
            # internal vocabulary, and it names the wrong document. Retitle at render time
            # rather than waiting on a re-bottle, since the heading is purely presentational.
            md = re.sub(r"(?m)\A\s*#\s+(.*?)\s*—\s*Master Blend Specification\s*$",
                        r"# \1 — Business Specification", md, count=1)
            md = re.sub(r"(?m)\A\s*#\s+.*Master Blend.*$",
                        "# Business Specification", md, count=1)
            html_body = md_to_html(md, source_repo_url)
            write_doc_page(pages_dir / "bottled_spec.html", "Business Specification", html_body,
                           extract_h2_toc(html_body))
            print(f"  Business Spec: {len(html_body) // 1024} KB")
            return True
    return False


def _metric_caption(text: str, headline: str) -> str:
    """Make a metric's supporting text readable under its number.

    The raw strings carry three habits that fight a small tile, all handled below and all
    falling back to the original text when the pattern does not match:
      "21 (registry) + 18 (objects) = 39 distinct entities"  the arithmetic is noise once
                                                             the total is the headline
      "(invariants: 15; catalog: 42; deps: 7)"               a parenthetical breakdown
      "(a, b, c, d, e, f, g, h, i)"                          a nine-item list in brackets
    """
    t = text.strip()

    # Drop a leading sum once its total is already the headline: keep what the total counts.
    m = re.match(r"^[\d,]+\s*\(.*?=\s*" + re.escape(headline) + r"\b\s*(.*)$", t, re.S)
    if m:
        tail = m.group(1).strip()
        if tail:
            t = tail
        else:
            # Nothing follows the total ("27 (role registry) + 1 (boundary spec) = 30"), so
            # there is no description to keep — but the addends are the breakdown. Restate
            # them as "27 role registry · 1 boundary spec" rather than leaving arithmetic
            # that just re-derives the number already shown above it.
            pairs = re.findall(r"([\d,]+)\s*\(([^)]+)\)", t)
            if len(pairs) > 1:
                return " · ".join(f"{n} {lbl.strip()}" for n, lbl in pairs)

    # Unwrap a parenthetical that is the whole caption.
    m = re.match(r"^\((.*)\)$", t, re.S)
    if m:
        t = m.group(1).strip()

    # Semicolon breakdown -> middots, which read as separate facts rather than one clause.
    if ";" in t:
        parts = [p.strip(" .") for p in t.split(";") if p.strip(" .")]
        if len(parts) > 1:
            return " · ".join(parts)

    # A long enumeration cannot be read in a tile; show the first few and count the rest.
    items = [p.strip(" .") for p in re.split(r",(?![^(]*\))", t) if p.strip(" .")]
    if len(items) > 5 and all(len(i) < 40 for i in items):
        shown = ", ".join(items[:4])
        return f"{shown} + {len(items) - 4} more"

    return t


def _sentence_case(item: str) -> str:
    """Capitalise the first letter of a list item, and only that.

    Splitting a sentence into a list exposes its original casing: the first clause was
    sentence-initial, the rest were mid-sentence, so a list reads "Accounts Payable invoice
    entry / approval authority / retainage management".

    Only the leading character changes, and only when the first word is plain lowercase
    prose. A word carrying its own capitalisation (`APInvHdr`, `usp_APInvPost`, `GL month`)
    is left exactly as written — code identifiers and acronyms must survive verbatim, since
    altering them would misname real things in the source.
    """
    m = re.match(r"(\s*(?:<[^>]+>\s*)*)([^\s<]+)(.*)", item, re.S)
    if not m:
        return item
    lead, first, rest = m.groups()
    if not re.fullmatch(r"[a-z][a-z/\-']*", first):
        return item          # has internal capitals, digits, or is an identifier
    return f"{lead}{first[0].upper()}{first[1:]}{rest}"


def _exec_restyle(html: str, target_name: str = "") -> str:
    """Turn the executive summary's bold-label bullet lists into a scannable layout.

    The markdown is uniform — "## Section" then "- **Label:** value" — so the structure is
    recoverable without the model's help: System Classification becomes definition cards,
    Key Metrics becomes stat tiles with the count pulled out front, and the narrative
    sections get an accent colour by role. Purely presentational; no text is invented,
    dropped or reordered.
    """
    def _items(ul_html: str):
        out = []
        for li in re.findall(r"<li>(.*?)</li>", ul_html, re.S):
            m = re.match(r"\s*<strong>(.*?)</strong>:?\s*(.*)", li, re.S)
            if m:
                out.append((re.sub(r"<[^>]+>", "", m.group(1)).strip().rstrip(":"),
                            m.group(2).strip()))
            else:
                out.append(("", li.strip()))
        return out

    def _section(name):
        """The <ul> immediately following an <h2> whose text matches `name`."""
        return re.search(
            r"<h2[^>]*>\s*" + re.escape(name) + r"\s*</h2>\s*(<ul>.*?</ul>)", html, re.S | re.I)

    # ── hero ──
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    cls = _section("System Classification")
    if h1:
        title = re.sub(r"<[^>]+>", "", h1.group(1))
        title = re.sub(r"\s*—\s*Specification Summary\s*$", "", title).strip() or target_name
        meta = dict(_items(cls.group(1))) if cls else {}
        date = re.sub(r"<[^>]+>", "", meta.get("Distillation date", ""))
        sub = "Business specification recovered from the existing implementation"
        if date:
            sub += f" &middot; {date}"
        html = html.replace(h1.group(0),
                            f'<div class="ex-hero"><h1>{title}</h1>'
                            f'<div class="sub">{sub}</div></div>', 1)

    # ── System Classification -> cards ──
    # These three values have different shapes, so each gets the treatment that fits. Every
    # transform below is best-effort over MODEL-GENERATED prose: phrasing will drift between
    # runs, so each one falls back to the original paragraph when its pattern does not match.
    # Never mangle, never drop — a plain paragraph is an acceptable outcome, missing text is not.
    def _as_list(val: str) -> str:
        """Comma/semicolon-separated enumeration -> list. Prose -> unchanged."""
        plain = re.sub(r"<[^>]+>", "", val)
        if re.search(r"[.!?]\s+[A-Z]", plain):
            return ""          # more than one sentence: real prose, leave it alone
        parts = [p.strip(" .") for p in re.split(r",(?![^(]*\))|;", val) if p.strip(" .")]
        if len(parts) < 3:
            return ""          # not enough items to be worth a list
        parts[-1] = re.sub(r"^(?:and|&)\s+", "", parts[-1], flags=re.I)
        return ('<ul class="ex-list">'
                + "".join(f"<li>{_sentence_case(p)}</li>" for p in parts) + "</ul>")

    def _as_label_caption(val: str) -> str:
        """"Label — explanation" -> prominent label with the explanation beneath."""
        m = re.match(r"\s*([^—–-]{2,40}?)\s*[—–]\s*(.+)", val, re.S)
        return (f'<div class="ex-lead">{m.group(1).strip()}</div>'
                f'<div class="ex-note">{m.group(2).strip()}</div>') if m else ""

    def _as_stack(val: str) -> str:
        """"X (a, b) plus Y (c, d)" -> one row per technology layer with its artefacts."""
        layers = re.split(r"\s+(?:plus|and)\s+(?=[A-Z])", val)
        rows = []
        for layer in layers:
            m = re.match(r"\s*(.+?)\s*\(([^)]*)\)\s*$", layer.strip(" ."))
            if not m:
                return ""      # not the shape we expected; keep the paragraph
            rows.append(f'<div class="ex-row"><span class="ex-tech">{m.group(1).strip()}</span>'
                        f'<span class="ex-parts">{m.group(2).strip()}</span></div>')
        return "".join(rows) if len(rows) >= 2 else ""

    if cls:
        shape = {"archetype": _as_label_caption,
                 "primary domains": _as_list,
                 "source system size": _as_stack}
        cards = []
        for k, v in _items(cls.group(1)):
            if not k or k.lower() == "distillation date":
                continue       # already in the hero
            body = shape.get(k.lower(), lambda _v: "")(v) or f'<div class="v">{v}</div>'
            cards.append(f'<div class="ex-card"><div class="k">{k}</div>{body}</div>')
        if cards:
            html = html.replace(cls.group(1), f'<div class="ex-grid">{"".join(cards)}</div>', 1)

    # ── Key Metrics -> stat tiles ──
    km = _section("Key Metrics")
    if km:
        tiles = []
        for k, v in _items(km.group(1)):
            if not k:
                continue
            plain = re.sub(r"<[^>]+>", "", v).strip()
            # A metric may be a single count ("23 lifecycle entries") or a sum that states
            # its own total ("21 (registry) + 18 (objects) = 39 ..."). Taking the FIRST
            # integer is wrong for the second shape — it headlines an addend instead of the
            # total, so this card read "21" when the stated answer was 39. Prefer an
            # explicit "= N" when the text gives one.
            total = re.findall(r"=\s*(\d[\d,]*)", plain)
            first = re.search(r"\d[\d,]*", plain)
            if not (total or first):
                # No count at all: it is not really a metric. Show the text rather than
                # invent a number for it.
                tiles.append(f'<div class="ex-tile"><div class="l">{k}</div>'
                             f'<div class="d">{plain}</div></div>')
                continue
            if total:
                headline = total[-1]
                caption = plain
            else:
                headline = first.group(0)
                caption = plain[first.end():].strip(" :—-")
            caption = _metric_caption(caption, headline)
            tiles.append(f'<div class="ex-tile"><div class="n">{headline}</div>'
                         f'<div class="l">{k}</div>'
                         + (f'<div class="d">{caption}</div>' if caption else "")
                         + "</div>")
        if tiles:
            html = html.replace(km.group(1), f'<div class="ex-tiles">{"".join(tiles)}</div>', 1)

    # ── accent the narrative headings by role ──
    # Every h2 gets the same treatment, differing only in accent colour. Previously only
    # three were restyled, so the page carried two heading styles at once: heavy solid dark
    # banners for System Classification and Key Metrics, light accented rules for the rest.
    for name, kind in (("System Classification", "info"), ("Key Metrics", "info"),
                       ("Critical Findings", "crit"), ("Known Gaps", "gap"),
                       ("Confidence Overview", "conf")):
        # Keep the original attributes (\1) — the id= is what the sidebar and anchor links
        # use — and close the opening tag. An earlier version dropped both, emitting
        # `<h2 class="..."Critical Findings</h2>`, which is unparseable: the three narrative
        # headings disappeared from the page and from the table of contents.
        html = re.sub(r"<h2([^>]*)>(\s*" + re.escape(name) + r"[^<]*)</h2>",
                      r'<h2\1 class="ex-accent ' + kind + r'">\2</h2>',
                      html, count=1, flags=re.I)
    return html


EXEC_CSS = """<style>
/* Executive summary presentation. This is the first page a customer sees, and as plain
   markdown it was a wall of bold-label bullets — the counts, which are the scannable part,
   were buried mid-sentence. Typography and colour only, no icons: this is a deliverable
   for a finance/construction audience and emoji reads as unserious there. */
.ex-hero{border-bottom:2px solid #232f3e;padding-bottom:14px;margin-bottom:22px}
.ex-hero h1{margin:0 0 6px;font-size:26px;letter-spacing:-0.3px}
.ex-hero .sub{font-size:13px;color:#718096}
.ex-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin:0 0 24px}
.ex-card{border:1px solid #e2e8f0;border-radius:6px;padding:12px 14px;background:#fff}
.ex-card .k{font-size:10.5px;font-weight:700;color:#718096;text-transform:uppercase;
  letter-spacing:0.6px;margin-bottom:5px}
.ex-card .v{font-size:13.5px;color:#2d3748;line-height:1.5}
/* Archetype: the classification is the answer, the definition is support. */
.ex-card .ex-lead{font-size:16px;font-weight:700;color:#232f3e;line-height:1.3}
.ex-card .ex-note{font-size:12.5px;color:#718096;line-height:1.5;margin-top:4px}
/* Primary domains: an enumeration, so set it as one. */
.ex-card .ex-list{margin:0;padding:0;list-style:none}
.ex-card .ex-list li{font-size:13px;color:#2d3748;line-height:1.5;padding-left:12px;
  position:relative}
.ex-card .ex-list li:before{content:'';position:absolute;left:0;top:8px;width:4px;height:4px;
  border-radius:50%;background:#cbd5e0}
/* Source stack: one row per technology layer, artefact kinds beneath it. */
.ex-card .ex-row{margin-bottom:8px}
.ex-card .ex-row:last-child{margin-bottom:0}
.ex-card .ex-tech{display:block;font-size:13px;font-weight:600;color:#232f3e}
.ex-card .ex-parts{display:block;font-size:12px;color:#718096;line-height:1.45}
.ex-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:10px;margin:0 0 24px}
.ex-tile{border:1px solid #e2e8f0;border-top:3px solid #ff9900;border-radius:6px;
  padding:12px 14px;background:#fff}
.ex-tile .n{font-size:26px;font-weight:700;color:#232f3e;line-height:1.1}
.ex-tile .l{font-size:11.5px;color:#4a5568;margin-top:3px;font-weight:600}
.ex-tile .d{font-size:11px;color:#a0aec0;margin-top:4px;line-height:1.4}
/* Findings and gaps carry different weight; the accent says which without a label. */
/* !important overrides the shared PAGE_CSS h2 rule, which paints a solid dark banner. */
h2.ex-accent{border-left:4px solid #718096;padding:2px 0 2px 12px !important;
  background:none !important;color:#1a202c !important;font-size:19px !important;
  margin:30px 0 14px !important;border-radius:0 !important}
h2.ex-accent:first-of-type{margin-top:4px !important}
h2.ex-accent.info{border-left-color:#232f3e}
h2.ex-accent.crit{border-left-color:#c53030}
h2.ex-accent.gap{border-left-color:#dd6b20}
h2.ex-accent.conf{border-left-color:#2f855a}
</style>"""


ISSUES_CSS = """<style>
.iss-bar{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0 18px;align-items:center}
.iss-chip{border:1px solid #cbd5e0;background:#fff;border-radius:14px;padding:4px 12px;font-size:12px;cursor:pointer;color:#2d3748}
.iss-chip.active{background:#232f3e;color:#fff;border-color:#232f3e}
.iss-search{padding:5px 10px;border:1px solid #cbd5e0;border-radius:4px;font-size:13px;min-width:220px;margin-left:auto}
.iss-card{border:1px solid #e2e8f0;border-left:4px solid #a0aec0;border-radius:6px;padding:12px 16px;margin:0 0 14px;background:#fff}
.iss-card.sev-High{border-left-color:#e53e3e}.iss-card.sev-Medium{border-left-color:#dd6b20}.iss-card.sev-Low{border-left-color:#3182ce}
.iss-card h3{margin:0 0 6px;font-size:15px}
.iss-badge{display:inline-block;font-size:11px;padding:1px 8px;border-radius:10px;background:#edf2f7;color:#2d3748;margin-right:6px}
.iss-card p{margin:6px 0;font-size:13.5px;line-height:1.5}
.iss-card .lbl{font-weight:600;color:#4a5568}
.iss-loc{display:inline-block;margin:2px 8px 2px 0;font-family:"SF Mono",Menlo,monospace;font-size:12px;color:#2563eb;background:#f0f4ff;padding:1px 6px;border-radius:3px;cursor:pointer;text-decoration:none}
.iss-src{background:#1a202c;color:#e2e8f0;font-family:"SF Mono",Menlo,monospace;font-size:12px;padding:8px 0;border-radius:4px;overflow-x:auto;margin:6px 0;display:none}
.iss-src.open{display:block}
.iss-src div{white-space:pre;padding:0 10px}
.iss-src div.hit{background:#744210}
.iss-src .ln{color:#718096;display:inline-block;width:46px;text-align:right;margin-right:10px;user-select:none}
.iss-table td,.iss-table th{font-size:12.5px}
.iss-note{font-size:12.5px;color:#718096}
</style>"""

ISSUES_JS = """
<script>
(function(){
  const data=JSON.parse(document.getElementById('issues-data').textContent);
  const repo=document.getElementById('issues-data').dataset.repo||'';
  const issues=data.issues||[];
  const cats=[...new Set(issues.map(i=>i.category))];
  let cat='All', q='';
  const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const bar=document.getElementById('iss-bar');
  function chips(){
    const counts={};issues.forEach(i=>counts[i.category]=(counts[i.category]||0)+1);
    bar.innerHTML=['All',...cats].map(c=>'<button class="iss-chip'+(c===cat?' active':'')+'" data-cat="'+esc(c)+'">'+esc(c)+' ('+(c==='All'?issues.length:counts[c])+')</button>').join('')
      +'<input class="iss-search" id="iss-q" placeholder="Search issues..." value="'+esc(q)+'">';
    bar.querySelectorAll('.iss-chip').forEach(b=>b.onclick=()=>{cat=b.dataset.cat;render();});
    const qi=document.getElementById('iss-q');qi.oninput=()=>{q=qi.value.toLowerCase();renderList();};
  }
  function srcBlock(loc){
    const rows=(loc.excerpt||[]).map(([n,t])=>'<div class="'+(n>=loc.start&&n<=loc.end?'hit':'')+'"><span class="ln">'+n+'</span>'+esc(t)+'</div>').join('');
    return '<div class="iss-src" data-ref="'+esc(loc.ref)+'">'+(rows||'<div>(source excerpt unavailable)</div>')+'</div>';
  }
  function card(i){
    const locs=i.locations.map((l,k)=>{
      const repoLink=repo?' <a href="'+esc(repo.replace(/\\/$/,'')+'/'+l.file+'#L'+l.start+'-L'+l.end)+'" target="_blank" class="iss-note">repo</a>':'';
      return '<a class="iss-loc evidence-link" data-path="'+esc(l.file)+'" data-lines="#L'+l.start+'-L'+l.end+'" href="#'+esc(i.id)+'-loc'+k+'" data-target="'+esc(i.id)+'-src'+k+'">'+esc(l.ref)+'</a>'+repoLink;
    }).join('');
    const srcs=i.locations.map((l,k)=>srcBlock(l).replace('class="iss-src"','class="iss-src" id="'+esc(i.id)+'-src'+k+'"')).join('');
    return '<div class="iss-card sev-'+esc(i.severity)+'" id="'+esc(i.id)+'" data-cat="'+esc(i.category)+'">'
      +'<h3>'+esc(i.id)+' — '+esc(i.title)+'</h3>'
      +'<span class="iss-badge">'+esc(i.category)+'</span><span class="iss-badge">Severity: '+esc(i.severity)+'</span><span class="iss-badge">Evidence: '+esc(i.evidence_class)+'</span>'
      +'<p><span class="lbl">What we found:</span> '+esc(i.description)+'</p>'
      +'<p><span class="lbl">Business impact:</span> '+esc(i.business_impact)+'</p>'
      +(i.sme_question?'<p><span class="lbl">Question for an SME:</span> '+esc(i.sme_question)+'</p>':'')
      +'<p><span class="lbl">Locations:</span> '+locs+'</p>'+srcs+'</div>';
  }
  function visible(){return issues.filter(i=>(cat==='All'||i.category===cat)&&(!q||(i.title+' '+i.description+' '+i.locations.map(l=>l.ref).join(' ')).toLowerCase().includes(q)));}
  function renderList(){
    const list=document.getElementById('iss-list');const v=visible();
    list.innerHTML=v.length?v.map(card).join(''):'<p class="iss-note">No issues match this filter.</p>';
    list.querySelectorAll('.iss-loc').forEach(a=>a.onclick=(e)=>{e.preventDefault();const el=document.getElementById(a.dataset.target);if(el)el.classList.toggle('open');});
    window.parent.postMessage({type:'toc-update',toc:v.map(i=>({id:i.id,title:i.id+' '+i.title,level:3}))},'*');
  }
  function render(){chips();renderList();}
  render();
  window.addEventListener('message',(e)=>{if(e.data&&e.data.type==='scrollTo'){const el=document.getElementById(e.data.id);if(el)el.scrollIntoView({behavior:'smooth',block:'start'});}});
  if(location.hash){const el=document.getElementById(location.hash.slice(1));if(el)el.scrollIntoView();}
})();
</script>"""


def load_issues(base_dir: Path) -> dict | None:
    p = base_dir / "issues" / "issues.json"
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("issues"), list) else None


def _h(s) -> str:
    return (str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def generate_issues_page(base_dir: Path, pages_dir: Path, source_repo_url: str = "") -> bool:
    """Issues & Insights: summary table + filterable issue cards with inline cited source lines."""
    doc = load_issues(base_dir)
    if doc is None or doc.get("status", "ok") != "ok":
        return False
    issues = doc["issues"]
    meta = doc.get("meta", {})
    rows = "".join(
        f'<tr><td><a href="#{_h(i.get("id"))}">{_h(i.get("id"))}</a></td><td>{_h(i.get("category"))}</td>'
        f'<td>{_h(i.get("title"))}</td><td>{_h(i.get("severity"))}</td><td>{_h(i.get("evidence_class"))}</td>'
        f'<td>{len(i.get("locations", []))}</td></tr>' for i in issues)
    body = (f'<h1>Issues &amp; Insights</h1>'
            f'<p class="iss-note">Prototype output for SME review: each item is a question to settle before '
            f'modernization, not a confirmed defect. {len(issues)} issues from {_h(meta.get("candidates", 0))} '
            f'scanned candidates; every location was checked against the source pages. '
            f'Model: {_h(meta.get("model") or "none (deterministic only)")}.</p>'
            f'<h2 id="summary">Summary</h2>'
            f'<table class="iss-table"><thead><tr><th>#</th><th>Category</th><th>Title</th><th>Severity</th>'
            f'<th>Evidence</th><th>Locations</th></tr></thead><tbody>{rows}</tbody></table>'
            f'<h2 id="issues">Issues</h2><div class="iss-bar" id="iss-bar"></div><div id="iss-list"></div>'
            f'<script type="application/json" id="issues-data" data-repo="{_h(source_repo_url)}">'
            f'{_json_for_script({"issues": issues})}</script>')
    write_doc_page(pages_dir / "issues.html", "Issues & Insights", body, [], extra_head=ISSUES_CSS,
                   extra_js=ISSUES_JS)
    print(f"  Issues & Insights: {len(issues)} issues")
    return True


# ── Knowledge Graph page (stats + optional subset) ───────────────────────────

def generate_graph_page(stills_dir: Path, pages_dir: Path, target_name: str):
    """Generate lightweight graph page with stats and top-200 subset."""
    unified_graph = stills_dir / "knowledge_graph.json"
    if not unified_graph.exists():
        write_content_page(pages_dir / "knowledge_graph.html",
                           "Knowledge Graph", "<p>No knowledge graph available.</p>")
        return

    try:
        data = json.loads(unified_graph.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError):
        write_content_page(pages_dir / "knowledge_graph.html",
                           "Knowledge Graph", "<p>Error loading knowledge graph.</p>")
        return

    nodes = data.get("nodes", [])
    edges = data.get("edges", data.get("links", []))

    # Compute stats
    uc_count = sum(1 for n in nodes if n.get("node_type") == "unified_concept")
    obs_count = len(nodes) - uc_count
    still_counts = {}
    for n in nodes:
        still = n.get("source_still", n.get("still", "unknown"))
        if n.get("node_type") == "unified_concept":
            stills_list = n.get("source_stills", [still])
            still = stills_list[0] if stills_list else still
        still_counts[still] = still_counts.get(still, 0) + 1

    # Build subset: top 200 unified concepts by confidence
    uc_nodes = [n for n in nodes if n.get("node_type") == "unified_concept"]
    uc_nodes.sort(key=lambda n: n.get("confidence", 0), reverse=True)
    subset_nodes = uc_nodes[:200]
    subset_ids = {n["id"] for n in subset_nodes}

    # ── Project block-level edges up onto unified concepts ───────────────────
    # The graph has two tiers and EVERY edge lives on the lower one:
    #   observation block --CONTRIBUTES_TO--> unified concept
    #   observation block --references------> observation block   (cross-still agreement)
    #   observation block --same_as---------> observation block   (within-still duplicate)
    # No edge ever has a unified concept as an endpoint. The previous filter kept edges
    # whose BOTH ends were in the concept subset, so it matched nothing — every run drew
    # 339 nodes and 0 edges, which is why this view rendered as a grid of dots rather than
    # a graph.
    #
    # So walk the contribution map: if block A belongs to concept X, block B to concept Y,
    # and A references B, then X and Y were independently corroborated — draw X—Y. Several
    # block pairs usually collapse onto the same concept pair, and that count IS the
    # strength of the agreement, so keep it as a weight for edge thickness.
    block_to_concept = {}
    for e in edges:
        if e.get("edge_type") == "CONTRIBUTES_TO":
            block_to_concept[e.get("source", "")] = e.get("target", "")

    pair_weight: dict = {}
    pair_kind: dict = {}
    for e in edges:
        et = e.get("edge_type", "")
        if et not in ("references", "same_as"):
            continue
        a = block_to_concept.get(e.get("source", ""))
        b = block_to_concept.get(e.get("target", ""))
        if not a or not b or a == b:
            continue  # unmapped block, or both blocks merged into the same concept
        if a not in subset_ids or b not in subset_ids:
            continue
        key = (a, b) if a < b else (b, a)
        pair_weight[key] = pair_weight.get(key, 0) + 1
        # "references" (cross-still) is the stronger claim; keep it if either pair had one.
        if pair_kind.get(key) != "references":
            pair_kind[key] = et

    subset_edges = [
        {"source": a, "target": b, "edge_type": pair_kind[(a, b)], "weight": w}
        for (a, b), w in sorted(pair_weight.items(), key=lambda kv: -kv[1])
    ]

    # Build cytoscape elements
    cy_nodes = []
    for n in subset_nodes:
        still = n.get("source_still", n.get("still", "unknown"))
        if n.get("node_type") == "unified_concept":
            stills_list = n.get("source_stills", [still])
            still = stills_list[0] if stills_list else still
        cy_nodes.append({"data": {
            "id": n["id"],
            "label": n.get("title", "")[:60],
            "title": n.get("title", ""),
            "still": still,
            "confidence": n.get("confidence", 3),
            "color": STILL_COLORS.get(still, "#a0aec0"),
            "contributor_count": n.get("contributor_count", 1),
        }})

    cy_edges = []
    for e in subset_edges:
        cy_edges.append({"data": {
            "id": f"{e['source']}-{e['target']}",
            "source": e["source"],
            "target": e["target"],
            "edge_type": e.get("edge_type", "references"),
            # How many independent block-level agreements collapsed onto this concept
            # pair. Drives edge thickness, so the strongest corroboration reads loudest.
            "weight": e.get("weight", 1),
        }})

    # Size nodes by how many agreement links they carry, not by confidence: the subset is
    # "top 200 BY confidence", so every node in it scores 5 and a confidence mapping renders
    # 200 identical circles while the legend claims size is meaningful. Degree varies and is
    # the more useful signal — a densely corroborated concept should look like one.
    degree: dict = {}
    for e in cy_edges:
        degree[e["data"]["source"]] = degree.get(e["data"]["source"], 0) + 1
        degree[e["data"]["target"]] = degree.get(e["data"]["target"], 0) + 1
    for n in cy_nodes:
        n["data"]["degree"] = degree.get(n["data"]["id"], 0)

    graph_subset = {"nodes": cy_nodes, "edges": cy_edges}
    stats = {
        "total_nodes": len(nodes),
        "unified_concepts": uc_count,
        "observation_blocks": obs_count,
        "total_edges": len(edges),
        "stills": still_counts,
        "subset_shown": len(cy_nodes),
        # Edges actually drawn, after projecting block-level agreement onto concepts.
        # The old stats line reported total_edges (1317) while drawing none, which
        # told a reader the view was complete when it was empty.
        "edges_shown": len(cy_edges),
    }

    graph_json = json.dumps(graph_subset)
    stats_json = json.dumps(stats)

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Knowledge Graph \u2014 {target_name}</title>
<style>
body{{margin:0;padding:0;font-family:-apple-system,sans-serif;background:#1a202c;color:#e2e8f0}}
#top{{padding:12px 16px;display:flex;gap:12px;align-items:center;background:#2d3748;flex-wrap:wrap}}
#search{{flex:1;min-width:200px;padding:8px 14px;border:1px solid #4a5568;border-radius:6px;font-size:14px;background:#1a202c;color:#e2e8f0;outline:none}}
#search:focus{{border-color:#ff9900}}
#search-btn{{padding:8px 16px;background:#ff9900;color:#fff;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer}}
#search-btn:hover{{background:#e08800}}
#stats-bar{{font-size:12px;color:#a0aec0;padding:4px 16px}}
/* Four things are encoded visually here — node colour, node size, line thickness and line
   style — and without a key none of them mean anything to a reader. */
#legend{{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;padding:6px 16px 8px;
  font-size:11px;color:#a0aec0;border-bottom:1px solid #2d3748}}
#legend .lg{{display:flex;align-items:center;gap:5px;white-space:nowrap}}
#legend .sw{{width:11px;height:11px;border-radius:50%;flex:0 0 11px;
  border:1px solid rgba(255,255,255,0.25)}}
#legend .ttl{{color:#718096;font-weight:600;text-transform:uppercase;letter-spacing:0.4px;
  margin-right:2px}}
#legend .sep{{width:1px;height:14px;background:#2d3748;margin:0 4px}}
#legend .ln{{display:inline-block;width:22px;background:#63b3ed;flex:0 0 22px}}
#cy{{width:100%;height:calc(100vh - 152px)}}
#detail{{position:fixed;right:16px;top:70px;width:340px;max-height:calc(100vh - 90px);overflow-y:auto;background:#fff;border-radius:8px;padding:16px;box-shadow:0 4px 12px rgba(0,0,0,.3);display:none;z-index:200;color:#1a202c}}
#detail h3{{font-size:14px;margin:0 0 8px;padding-right:24px}}
#detail .meta{{font-size:12px;color:#718096;margin-bottom:8px}}
#close{{position:absolute;top:8px;right:12px;background:none;border:none;font-size:18px;cursor:pointer;color:#a0aec0}}
</style>
</head>
<body>
<div id="top">
<input id="search" type="text" placeholder="Search a concept to see it and what agrees with it (e.g. retainage, hold, approval)..." />
<button id="search-btn">Search</button>
</div>
<div id="stats-bar"></div>
<div id="legend"></div>
<div id="cy"></div>
<div id="detail"><button id="close">&times;</button><h3 id="dt"></h3><div class="meta" id="dm"></div></div>
<script src="../assets/cytoscape.min.js"></script>
<script>
const GRAPH={graph_json};
const STATS={stats_json};
const NAMES={json.dumps(STILL_DISPLAY_NAMES)};
const COLORS={json.dumps(STILL_COLORS)};
// Stats bar
const sb=document.getElementById('stats-bar');
// Keep only what the legend does not already say, and the one caveat that matters:
// this is a subset, so an absent concept is not necessarily a missing one.
sb.textContent='Showing the '+STATS.subset_shown+' best-evidenced of '+STATS.unified_concepts+' concepts, with '+STATS.edges_shown+' agreement links between them.';
// Init cytoscape
let cy=cytoscape({{container:document.getElementById('cy'),elements:[...GRAPH.nodes,...GRAPH.edges],
style:[
{{selector:'node',style:{{'label':'data(label)','font-size':'10px','color':'#e2e8f0','text-valign':'bottom','text-halign':'center','background-color':'data(color)','width':'mapData(degree,0,12,14,38)','height':'mapData(degree,0,12,14,38)','border-width':1,'border-color':'rgba(255,255,255,0.3)','text-outline-width':2,'text-outline-color':'#1a202c','text-max-width':'80px','text-wrap':'ellipsis'}}}},
// Thickness = how many independent agreements collapsed onto this concept pair, so the
// strongest corroboration is the most visible thing on screen.
{{selector:'edge',style:{{'line-color':'#63b3ed','width':'mapData(weight,1,8,1,6)','curve-style':'bezier','opacity':0.45}}}},
// A within-still duplicate is a weaker claim than cross-still agreement: dim and dash it.
{{selector:'edge[edge_type="same_as"]',style:{{'line-color':'#a0aec0','line-style':'dashed','opacity':0.3}}}},
{{selector:'node:selected',style:{{'border-width':3,'border-color':'#ff9900'}}}}
],
layout:{{name:'cose',idealEdgeLength:120,nodeRepulsion:6000,gravity:0.25,numIter:200,animate:false}},
minZoom:0.2,maxZoom:4,wheelSensitivity:0.3}});
cy.on('tap','node',e=>{{const d=e.target.data();document.getElementById('dt').textContent=d.title;document.getElementById('dm').textContent=(NAMES[d.still]||d.still)+' \u00b7 confidence: '+d.confidence+(d.contributor_count>1?' \u00b7 '+d.contributor_count+' contributors':'');document.getElementById('detail').style.display='block'}});
cy.on('tap',e=>{{if(e.target===cy)document.getElementById('detail').style.display='none'}});
document.getElementById('close').onclick=()=>document.getElementById('detail').style.display='none';
// ── Legend ───────────────────────────────────────────────────────────────────
// Built from the same COLORS/NAMES data the nodes are drawn from, so it cannot drift from
// what is on screen, and only lists passes that actually appear in this run.
(function(){{
  const present=new Set(GRAPH.nodes.map(n=>n.data.still));
  const parts=['<span class="ttl">Found by</span>'];
  Object.keys(NAMES).forEach(k=>{{
    if(!present.has(k))return;
    parts.push('<span class="lg"><span class="sw" style="background:'+(COLORS[k]||'#a0aec0')
      +'"></span>'+NAMES[k]+'</span>');
  }});
  if(present.has('unknown')||present.has(undefined)){{
    parts.push('<span class="lg"><span class="sw" style="background:#a0aec0"></span>Unattributed</span>');
  }}
  parts.push('<span class="sep"></span>');
  parts.push('<span class="lg"><span class="sw" style="width:8px;height:8px;flex:0 0 8px;'
    +'background:#718096"></span><span class="sw" style="background:#718096"></span>'
    +'Bigger = more agreement links</span>');
  parts.push('<span class="sep"></span>');
  parts.push('<span class="lg"><span class="ln" style="height:1px"></span>'
    +'<span class="ln" style="height:4px"></span>Thicker = more passes agreed</span>');
  parts.push('<span class="lg"><span class="ln" style="height:0;border-top:1px dashed #a0aec0">'
    +'</span>Duplicate within one pass</span>');
  document.getElementById('legend').innerHTML=parts.join('');
}})();

// ── Search = neighbourhood query, not a text filter ──────────────────────────
// Matching nodes alone is the wrong answer on a graph: the reason to draw one is to see
// what a concept CONNECTS to. So a match pulls in its directly-linked neighbours, and
// everything else — edges included — fades back. Dimming only nodes (the previous
// behaviour) left all 469 lines at full opacity, so the view stayed just as busy.
const searchEl=document.getElementById('search');
const statsEl=document.getElementById('stats-bar');
const STATS_BASE=statsEl.textContent;

function applySearch(){{
  const q=searchEl.value.toLowerCase().trim();
  if(!q){{
    cy.nodes().style('opacity',1);
    cy.edges().style('opacity',0.45);
    statsEl.textContent=STATS_BASE;
    return;
  }}
  const hits=cy.nodes().filter(n=>(n.data('title')||'').toLowerCase().includes(q));
  if(hits.length===0){{
    // Say so explicitly: an all-dim graph is indistinguishable from a typo.
    cy.nodes().style('opacity',0.08);
    cy.edges().style('opacity',0.04);
    statsEl.textContent='No concept matches "'+q+'". Clear the box to show everything again.';
    return;
  }}
  const neighbourhood=hits.closedNeighborhood();   // matches + their links + linked nodes
  cy.nodes().style('opacity',0.08);
  cy.edges().style('opacity',0.04);
  neighbourhood.nodes().style('opacity',1);
  neighbourhood.edges().style('opacity',0.75);
  hits.style('opacity',1);
  const linked=neighbourhood.nodes().length-hits.length;
  statsEl.textContent='"'+q+'": '+hits.length+' matching concept'+(hits.length===1?'':'s')
    +', '+linked+' connected by agreement, '+neighbourhood.edges().length+' links shown.'
    +' Press Escape to clear.';
}}

document.getElementById('search-btn').onclick=applySearch;
searchEl.addEventListener('input',applySearch);   // live, no button press needed
searchEl.addEventListener('keydown',e=>{{
  if(e.key==='Enter')applySearch();
  if(e.key==='Escape'){{searchEl.value='';applySearch();searchEl.blur();}}
}});
</script>
</body>
</html>"""
    (pages_dir / "knowledge_graph.html").write_text(page, encoding="utf-8")
    print(f"  Knowledge Graph: {uc_count} unified concepts, {obs_count} obs blocks, showing top {len(cy_nodes)}")


# ── Index page generation ────────────────────────────────────────────────────

def generate_index(output_dir: Path, target_name: str, stills_info: dict,
                   has_exec_summary: bool, has_master_blend: bool,
                   has_graph: bool, disabled_tabs: set | None = None,
                   has_issues: bool = False, has_bottled: bool = False,
                   api_base: str = ""):
    """Generate the index.html shell — nearly identical to v2."""

    if disabled_tabs is None:
        disabled_tabs = set()

    # Auto-disable tabs whose content is absent
    if not has_exec_summary:
        disabled_tabs.add("executive_summary")
    if not has_master_blend:
        disabled_tabs.add("master_blend")
    if not has_graph:
        disabled_tabs.add("knowledge_graph")
    if not has_issues:
        disabled_tabs.add("issues")
    if not has_bottled:
        disabled_tabs.add("bottled_spec")

    # Generate the "not implemented" placeholder page
    not_impl_html = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f8f9fa;color:#4a5568}
.box{text-align:center;padding:48px;border:2px dashed #cbd5e0;border-radius:12px;max-width:480px}
.box h2{font-size:20px;margin-bottom:12px;color:#2d3748}
.box p{font-size:14px;line-height:1.6;color:#718096}</style></head>
<body><div class="box"><h2>Feature Not Implemented</h2><p>This capability is planned but not yet available in the current build.</p></div></body></html>"""
    (output_dir / "pages" / "not_implemented.html").write_text(not_impl_html, encoding="utf-8")

    stills_items = []
    for still_name in STILLS_ORDER:
        if still_name in stills_info:
            stills_items.append({
                "label": STILL_DISPLAY_NAMES.get(still_name, still_name),
                "page": f"stills_{still_name}",
            })
    # Catch stills not in STILLS_ORDER
    for still_name in stills_info:
        if still_name not in STILLS_ORDER:
            label = STILL_DISPLAY_NAMES.get(still_name, still_name.replace('_', ' ').title())
            stills_items.append({"label": label, "page": f"stills_{still_name}"})

    stills_dropdown_html = "\n".join(
        f'  <a href="#" data-page="pages/{item["page"]}.html" '
        f'onclick="loadDropdownPage(this, \'stills-dropdown\')">{item["label"]}</a>'
        for item in stills_items
    )

    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Spec Distillation \u2014 {target_name}</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f8f9fa;overflow:hidden;height:100vh}}
.top-bar{{height:48px;background:#232f3e;color:#fff;display:flex;align-items:center;padding:0 24px;font-size:15px;font-weight:500;box-shadow:0 2px 8px rgba(0,0,0,.15)}}
.top-bar .brand{{color:#ff9900;font-weight:700;margin-right:8px}}
.top-bar .target{{color:#a0aec0;font-weight:400}}
.tab-bar{{height:40px;background:#37475a;display:flex;align-items:stretch;padding:0 12px;border-bottom:1px solid #2d3a4a;position:relative;z-index:200}}
.tab-bar .tab-btn{{background:none;border:none;color:#d5dbdb;font-size:13px;font-weight:500;padding:0 14px;cursor:pointer;border-bottom:3px solid transparent;white-space:nowrap;position:relative}}
.tab-bar .tab-btn:hover{{color:#fff;background:rgba(255,255,255,.05)}}
.tab-bar .tab-btn.active{{color:#ff9900;border-bottom-color:#ff9900}}
.tab-bar .tab-btn[data-disabled]{{opacity:.5}}
.tab-bar .tab-btn[data-disabled].active{{color:#a0aec0;border-bottom-color:#a0aec0}}
.dropdown a[data-disabled]{{opacity:.5}}
/* CSS escapes are \\HHHH with no "u" — "\\u25be" is JS/Python syntax and renders as the
   literal text "u25be" next to the tab label. */
.tab-bar .tab-btn.has-dropdown::after{{content:' \\25be';font-size:10px;opacity:0.7}}
.tab-bar .sep{{width:1px;background:#4a5568;margin:8px 4px;align-self:stretch}}
.dropdown{{position:absolute;top:100%;left:0;background:#2d3748;border:1px solid #4a5568;border-radius:0 0 6px 6px;box-shadow:0 8px 24px rgba(0,0,0,.4);min-width:200px;max-height:70vh;overflow-y:auto;display:none;z-index:300}}
.dropdown.open{{display:block}}
.dropdown a{{display:block;padding:8px 16px;color:#d5dbdb;text-decoration:none;font-size:13px;border-bottom:1px solid #3d4a5c}}
.dropdown a:hover{{background:#3d4a5c;color:#fff}}
.dropdown a:last-child{{border-bottom:none}}
.dropdown a.active{{color:#ff9900;background:#1a2332}}
.layout{{display:flex;height:calc(100vh - 88px)}}
.sidebar{{width:260px;background:#fff;border-right:1px solid #e2e8f0;overflow-y:auto;padding:12px 0;flex-shrink:0}}
.sidebar a{{display:block;padding:4px 16px;font-size:13px;color:#4a5568;text-decoration:none;border-radius:4px;line-height:1.4;transition:background 0.15s}}
.sidebar a:hover{{background:#edf2f7;color:#1a202c}}
.sidebar a.active{{background:#fff7e6;color:#c05621;font-weight:500}}
.sidebar a.visible{{background:rgba(0,0,0,0.04)}}
.sidebar a.primary{{background:rgba(0,0,0,0.08);font-weight:500}}
.sidebar .label{{font-size:11px;font-weight:600;color:#718096;padding:8px 16px 4px;text-transform:uppercase;letter-spacing:0.5px}}
.toc-match{{background:#fef08a;color:#1a202c;font-weight:600;border-radius:2px;padding:0 1px}}
/* Collapsible sections: an H2 is a group header, its H3s are children hidden until
   opened. A flat list of every subsection is a wall a reader has to scan rather than
   navigate, so the default is collapsed and the top level reads as a table of contents. */
.toc-group{{margin-bottom:2px}}
.toc-head{{display:flex;align-items:center;gap:6px;padding:5px 10px 5px 8px;cursor:pointer;
  border-radius:4px;color:#2d3748;font-size:13px;font-weight:600;user-select:none}}
.toc-head:hover{{background:#edf2f7}}
.toc-head.open{{color:#c05621}}
/* Reader is inside this section but it is collapsed — show where they are without opening it. */
.toc-head.current{{background:rgba(0,0,0,0.06)}}
.toc-caret{{flex:0 0 10px;font-size:9px;color:#a0aec0;transition:transform 0.15s;line-height:1}}
.toc-head.open .toc-caret{{transform:rotate(90deg);color:#c05621}}
.toc-head .toc-title{{flex:1 1 auto}}
.toc-count{{flex:0 0 auto;font-size:10px;color:#a0aec0;font-weight:500}}
.toc-children{{display:none}}
.toc-children.open{{display:block}}
.toc-children a{{padding-left:28px;font-size:12px}}
.filter-box{{padding:8px 12px;border-bottom:1px solid #e2e8f0;display:flex;gap:4px}}
.filter-box input{{flex:1;padding:6px 10px;border:1px solid #e2e8f0;border-radius:4px;font-size:13px;outline:none}}
.filter-box input:focus{{border-color:#ff9900}}
.filter-box button{{background:none;border:none;font-size:16px;color:#a0aec0;cursor:pointer;padding:0 4px}}
.filter-box button:hover{{color:#e53e3e}}
.content{{flex:1;overflow:hidden}}
.content iframe{{width:100%;height:100%;border:none}}
</style>
</head>
<body>
<div class="top-bar"><svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg" fill="none" style="height:20px;width:auto;margin-right:10px;vertical-align:middle"><path fill="#ffffff" d="M4.51 7.687c0 .197.02.357.058.475.042.117.096.245.17.384a.233.233 0 01.037.123c0 .053-.032.107-.1.16l-.336.224a.255.255 0 01-.138.048c-.054 0-.107-.026-.16-.074a1.652 1.652 0 01-.192-.251 4.137 4.137 0 01-.165-.315c-.415.491-.936.737-1.564.737-.447 0-.804-.129-1.064-.385-.261-.256-.394-.598-.394-1.025 0-.454.16-.822.484-1.1.325-.278.756-.416 1.304-.416.18 0 .367.016.564.042.197.027.4.07.612.118v-.39c0-.406-.085-.689-.25-.854-.17-.166-.458-.246-.868-.246-.186 0-.377.022-.574.07a4.23 4.23 0 00-.575.181 1.525 1.525 0 01-.186.07.326.326 0 01-.085.016c-.075 0-.112-.054-.112-.166v-.262c0-.085.01-.15.037-.186a.399.399 0 01.15-.113c.185-.096.409-.176.67-.24.26-.07.537-.101.83-.101.633 0 1.096.144 1.394.432.293.288.442.726.442 1.314v1.73h.01zm-2.161.811c.175 0 .356-.032.548-.096.191-.064.362-.182.505-.342a.848.848 0 00.181-.341c.032-.129.054-.283.054-.465V7.03a4.43 4.43 0 00-.49-.09 3.996 3.996 0 00-.5-.033c-.357 0-.618.07-.793.214-.176.144-.26.347-.26.614 0 .25.063.437.196.566.128.133.314.197.559.197zm4.273.577c-.096 0-.16-.016-.202-.054-.043-.032-.08-.106-.112-.208l-1.25-4.127a.938.938 0 01-.049-.214c0-.085.043-.133.128-.133h.522c.1 0 .17.016.207.053.043.032.075.107.107.208l.894 3.535.83-3.535c.026-.106.058-.176.1-.208a.365.365 0 01.214-.053h.425c.102 0 .17.016.213.053.043.032.08.107.101.208l.841 3.578.92-3.578a.458.458 0 01.107-.208.346.346 0 01.208-.053h.495c.085 0 .133.043.133.133 0 .027-.006.054-.01.086a.76.76 0 01-.038.133l-1.283 4.127c-.032.107-.069.177-.111.209a.34.34 0 01-.203.053h-.457c-.101 0-.17-.016-.213-.053-.043-.038-.08-.107-.101-.214L8.213 5.37l-.82 3.439c-.026.107-.058.176-.1.213-.043.038-.118.054-.213.054h-.458zm6.838.144a3.51 3.51 0 01-.82-.096c-.266-.064-.473-.134-.612-.214-.085-.048-.143-.101-.165-.15a.378.378 0 01-.031-.149v-.272c0-.112.042-.166.122-.166a.3.3 0 01.096.016c.032.011.08.032.133.054.18.08.378.144.585.187.213.042.42.064.633.064.336 0 .596-.059.777-.176a.575.575 0 00.277-.508.52.52 0 00-.144-.373c-.095-.102-.276-.193-.537-.278l-.772-.24c-.388-.123-.676-.305-.851-.545a1.275 1.275 0 01-.266-.774c0-.224.048-.422.143-.593.096-.17.224-.32.384-.438.16-.122.34-.213.553-.277.213-.064.436-.091.67-.091.118 0 .24.005.357.021.122.016.234.038.346.06.106.026.208.052.303.085.096.032.17.064.224.096a.46.46 0 01.16.133.289.289 0 01.047.176v.251c0 .112-.042.171-.122.171a.552.552 0 01-.202-.064 2.427 2.427 0 00-1.022-.208c-.303 0-.543.048-.708.15-.165.1-.25.256-.25.475 0 .149.053.277.16.379.106.101.303.202.585.293l.756.24c.383.123.66.294.825.513.165.219.244.47.244.748 0 .23-.047.437-.138.619a1.436 1.436 0 01-.388.47c-.165.133-.362.23-.591.299-.24.075-.49.112-.761.112z"/><g fill="#F90" fill-rule="evenodd" clip-rule="evenodd"><path d="M14.465 11.813c-1.75 1.297-4.294 1.986-6.481 1.986-3.065 0-5.827-1.137-7.913-3.027-.165-.15-.016-.353.18-.235 2.257 1.313 5.04 2.109 7.92 2.109 1.941 0 4.075-.406 6.039-1.239.293-.133.543.192.255.406z"/><path d="M15.194 10.98c-.223-.287-1.479-.138-2.048-.069-.17.022-.197-.128-.043-.24 1-.705 2.645-.502 2.836-.267.192.24-.053 1.89-.99 2.68-.143.123-.281.06-.218-.1.213-.53.687-1.72.463-2.003z"/></g></svg><span class="brand">Spec Distillation</span><span class="target">&mdash; {target_name}</span></div>
"""

    def _dis(tab_id: str) -> str:
        return ' data-disabled="true"' if tab_id in disabled_tabs else ''

    exec_disabled = "executive_summary" in disabled_tabs
    stills_disabled = "stills" in disabled_tabs
    # Initial view: executive summary, or its placeholder if disabled
    # Changes every generation so iframe URLs change too — see framePage() below.
    build_stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    initial_src = ("pages/not_implemented.html" if exec_disabled
                   else "pages/executive_summary.html") + f"?v={build_stamp}"

    index_html += f"""<div class="tab-bar" id="tabs">
  <button class="tab-btn active" data-page="pages/executive_summary.html"{_dis("executive_summary")} onclick="loadPage(this)">Executive Summary</button>
  <button class="tab-btn" data-page="pages/issues.html"{_dis("issues")} onclick="loadPage(this)">Issues &amp; Insights</button>
  <button class="tab-btn" data-page="pages/bottled_spec.html"{_dis("bottled_spec")} onclick="loadPage(this)">Business Spec</button>
  <span class="sep"></span>
  <button class="tab-btn" data-page="pages/master_blend.html"{_dis("master_blend")} onclick="loadPage(this)" title="The same specification with its evidence shown: confidence scores, how many independent passes found each item, and which extraction pass it came from. Use it to check where a claim in the Business Spec came from.">Evidence &amp; Audit Trail</button>
  <button class="tab-btn has-dropdown" data-dropdown="stills-dropdown"{_dis("stills")}>Stills</button>
  <span class="sep"></span>
  <button class="tab-btn" data-page="pages/knowledge_graph.html"{_dis("knowledge_graph")} onclick="loadPage(this)">Knowledge Graph</button>
  <span class="sep"></span>
  <button class="tab-btn" data-page="pages/evaluation.html"{_dis("evaluation")} onclick="loadPage(this)" title="How the outputs were checked: exact (deterministic) checks next to LLM-judged estimates with 95% CIs. Read from the evaluation API when the page opens.">Evaluation</button>
</div>

<div class="dropdown" id="stills-dropdown">
{stills_dropdown_html}
</div>

<div class="layout">
<nav class="sidebar" id="sidebar"><div class="filter-box"><input type="text" id="toc-filter" placeholder="Filter sections..." /><button id="toc-clear">&times;</button></div><div id="toc-list"></div></nav>
<main class="content"><iframe id="frame" src="{initial_src}"></iframe></main>
</div>
"""

    # Chat panel HTML
    index_html += """<div class="chat-panel" id="chat-panel" style="position:fixed;bottom:0;right:0;width:420px;background:#fff;border:1px solid #e2e8f0;border-radius:12px 12px 0 0;box-shadow:0 -4px 12px rgba(0,0,0,.1);z-index:500;display:flex;flex-direction:column;max-height:50vh;transition:all 0.2s ease">
<div class="chat-header" style="padding:10px 16px;background:#232f3e;color:#fff;font-size:13px;font-weight:600;cursor:pointer;border-radius:12px 12px 0 0;display:flex;justify-content:space-between;align-items:center;gap:8px"><span>Ask the Spec</span><span class="actions" style="display:flex;gap:6px;align-items:center"><button id="chat-dock" title="Dock to side" style="background:none;border:none;color:#a0aec0;cursor:pointer;font-size:14px;padding:2px 4px">&#x2B12;</button><button id="chat-toggle" style="background:none;border:none;color:#a0aec0;cursor:pointer;font-size:14px;padding:2px 4px">&#x25B2;</button></span></div>
<div class="chat-body collapsed" id="chat-body" style="display:none;flex-direction:column;flex:1;overflow:hidden">
<div class="chat-messages" id="chat-messages" style="flex:1;overflow-y:auto;padding:12px;font-size:13px;min-height:150px"></div>
<div style="display:flex;padding:8px 12px;border-top:1px solid #e2e8f0;gap:8px"><input type="text" id="chat-input" placeholder="Ask a question about the spec..." style="flex:1;padding:8px 12px;border:1px solid #e2e8f0;border-radius:6px;font-size:13px;outline:none" /><button id="chat-send" style="padding:8px 16px;background:#ff9900;color:#fff;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer">Send</button></div>
</div>
</div>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js" integrity="sha384-/TQbtLCAerC3jgaim+N78RZSDYV7ryeoBCVqTuzRrFec2akfBkHS7ACQ3PQhvMVi" crossorigin="anonymous"></script>
"""

    # Base for the shell's /api/* fetches. Kept in a SEPARATE f-string from the main JS block
    # below (which is deliberately a plain string so "{...}" placeholders ship literally) so the
    # one templated value here cannot collide with that hazard. '' = same-origin for a standalone
    # serve_viewer.py; '/api/runs/<id>' when the launcher serves the viewer.
    index_html += f"""<script>window.API_BASE={api_base!r}||"";</script>"""
    # Deep link: index.html#evaluation opens the Evaluation tab (the launcher links to it).
    index_html += """<script>window.addEventListener('load',function(){if(location.hash==='#evaluation'){var b=document.querySelector('[data-page="pages/evaluation.html"]');if(b&&typeof loadPage==='function')loadPage(b);}});</script>"""

    # Main JS for index
    index_html += """<script>
const frame=document.getElementById('frame');
// Read the generation stamp back off the initial iframe src rather than templating it in:
// this script block is not an f-string, so a "{{build_stamp}}" placeholder would ship
// literally. Falling back to Date.now() still busts the cache if the stamp is ever absent.
const BUILD_STAMP=(function(){
  const m=(frame.getAttribute('src')||'').match(/[?&]v=([0-9]+)/);
  return m?m[1]:String(Date.now());
})();
let openDropdown=null;
let currentTocEntries=[];

// Every viewer page is loaded into an iframe, and a browser will happily reuse a cached
// iframe across a shell reload — so regenerating the viewer looks like it did nothing and
// you read a stale spec believing it is current. BUILD_STAMP changes every generation, so
// the URL changes too and the cache can never win. (serve_viewer.py also sends no-store,
// but that only helps browsers that have not already cached the old copy.)
function framePage(p){
  return p + (p.indexOf('?')<0?'?':'&') + 'v=' + BUILD_STAMP;
}

// Dropdown toggle
document.querySelectorAll('.tab-btn.has-dropdown').forEach(btn=>{
  btn.addEventListener('click', (e)=>{
    e.stopPropagation();
    // Disabled dropdown tab: keep clickable, but show placeholder instead of opening
    if(btn.hasAttribute('data-disabled')){
      if(openDropdown){openDropdown.classList.remove('open');openDropdown=null}
      frame.src=framePage('pages/not_implemented.html');
      document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
      currentTocEntries=[];renderToc('');
      return;
    }
    const ddId=btn.dataset.dropdown;
    const dd=document.getElementById(ddId);
    if(openDropdown&&openDropdown!==dd){openDropdown.classList.remove('open')}
    dd.classList.toggle('open');
    openDropdown=dd.classList.contains('open')?dd:null;
    const rect=btn.getBoundingClientRect();
    dd.style.left=rect.left+'px';
    dd.style.top=rect.bottom+'px';
  });
});
document.addEventListener('click',()=>{if(openDropdown){openDropdown.classList.remove('open');openDropdown=null}});
document.querySelectorAll('.dropdown').forEach(dd=>dd.addEventListener('click',e=>e.stopPropagation()));

function loadPage(btn){
  // Disabled tabs stay clickable but show the "not implemented" placeholder
  const page=btn.hasAttribute('data-disabled')?'pages/not_implemented.html':btn.dataset.page;
  frame.src=framePage(page);
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  if(openDropdown){openDropdown.classList.remove('open');openDropdown=null}
  currentTocEntries=[];
  renderToc('');
}

function loadDropdownPage(a, ddId){
  const page=a.dataset.page;
  frame.src=framePage(page);
  const ddEl=document.getElementById(ddId);
  const parentBtn=document.querySelector('[data-dropdown="'+ddId+'"]');
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  parentBtn.classList.add('active');
  ddEl.querySelectorAll('a').forEach(x=>x.classList.remove('active'));
  a.classList.add('active');
  ddEl.classList.remove('open');
  openDropdown=null;
  currentTocEntries=[];
  renderToc('');
  return false;
}

// Sidebar TOC
const tocFilter=document.getElementById('toc-filter');
const tocClear=document.getElementById('toc-clear');
const tocList=document.getElementById('toc-list');

tocFilter.addEventListener('input',()=>renderToc(tocFilter.value));
tocClear.onclick=()=>{tocFilter.value='';renderToc('')};

// Which top-level sections are expanded. Kept outside renderToc so opening a section
// survives a re-render (filtering, scroll-highlight updates, returning to a tab).
let tocOpen=new Set();

function tocHighlight(title,f){
  const t=title.substring(0,80);
  if(!f)return escH(t);
  const idx=t.toLowerCase().indexOf(f);
  if(idx<0)return escH(t);
  return escH(t.substring(0,idx))+'<span class="toc-match">'+escH(t.substring(idx,idx+f.length))
         +'</span>'+escH(t.substring(idx+f.length));
}

function tocLink(e,f){
  return '<a href="#" data-section-id="'+escH(e.id)+'" data-page="'+(Number(e.page)||0)+'"'
    +' onclick="navToSection(this.dataset.sectionId,Number(this.dataset.page));return false">'
    +tocHighlight(e.title,f)+'</a>';
}

function renderToc(filter){
  const f=(filter||'').toLowerCase();

  // Group each H2 with the H3+ entries that follow it. Anything appearing before the
  // first H2 (or a page that only emits one level) stays a plain top-level link, so a
  // flat page still renders sensibly.
  const groups=[];
  let orphans=[];
  currentTocEntries.forEach(e=>{
    if(e.level<=2){
      // Merge a repeated section title into the group above it. Upstream phases can emit
      // the same "## Title" twice (an empty one followed by the real content), which would
      // otherwise show as a duplicate sidebar row — one of them empty. Keep the first
      // occurrence as the anchor so navigation lands at the top of the section.
      const prev=groups[groups.length-1];
      if(prev&&prev.head.title.trim()===e.title.trim())return;
      groups.push({head:e,children:[]});
    }
    else if(groups.length){groups[groups.length-1].children.push(e);}
    else{orphans.push(e);}
  });

  let html=orphans.filter(e=>!f||e.title.toLowerCase().includes(f)).map(e=>tocLink(e,f)).join('');
  let shown=0;

  groups.forEach((g,i)=>{
    const headHit=g.head.title.toLowerCase().includes(f);
    const kids=g.children.filter(c=>!f||c.title.toLowerCase().includes(f));
    // While filtering, hide groups with no match and auto-open those that have one —
    // otherwise a search appears to return nothing because the hits are inside a
    // collapsed section.
    if(f&&!headHit&&!kids.length)return;
    const open=f?true:tocOpen.has(g.head.id);
    shown++;
    const kidList=(f?kids:g.children);
    html+='<div class="toc-group">'
      +'<div class="toc-head'+(open?' open':'')+'" data-toc-group="'+escH(g.head.id)+'" data-page="'+(Number(g.head.page)||0)+'"'
      +' onclick="toggleTocGroup(this.dataset.tocGroup,Number(this.dataset.page))">'
      +'<span class="toc-caret">▶</span>'
      +'<span class="toc-title">'+tocHighlight(g.head.title,f)+'</span>'
      +(g.children.length?'<span class="toc-count">'+g.children.length+'</span>':'')
      +'</div>'
      +'<div class="toc-children'+(open?' open':'')+'" data-toc-children="'+escH(g.head.id)+'">'
      +kidList.map(c=>tocLink(c,f)).join('')
      +'</div></div>';
  });

  if(!html){
    html='<div class="label">'+(filter?'No matches':'Select a tab to see sections')+'</div>';
  }
  tocList.innerHTML=html;
}

// Clicking a section header both navigates to it and toggles its children: a reader who
// clicks a heading wants to go there, and also wants to see what is under it.
function toggleTocGroup(id,pageNum){
  if(tocOpen.has(id))tocOpen.delete(id); else tocOpen.add(id);
  const head=tocList.querySelector('[data-toc-group="'+CSS.escape(id)+'"]');
  const kids=tocList.querySelector('[data-toc-children="'+CSS.escape(id)+'"]');
  if(head)head.classList.toggle('open',tocOpen.has(id));
  if(kids)kids.classList.toggle('open',tocOpen.has(id));
  navToSection(id,pageNum);
}

function navToSection(id, pageNum){
  try{
    frame.contentWindow.postMessage({type:'scrollTo', id:id, page:pageNum},'*');
  }catch(e){}
}

// Listen for TOC updates from still pages (fragment-based pages send toc-update)
window.addEventListener('message',(event)=>{
  if(event.source!==frame.contentWindow||!event.data)return;
  if(event.data.type==='toc-update'){
    currentTocEntries=event.data.toc||[];
    // New page, new section ids: carrying expansion state over would leave groups open
    // or closed based on headings that no longer exist.
    tocOpen=new Set();
    renderToc(tocFilter.value);
  }
  if(event.data.type==='toc-visibility'){
    const visible=event.data.visible;
    let maxId='';let maxRatio=0;
    for(const[id,ratio]of Object.entries(visible)){
      if(ratio>maxRatio){maxRatio=ratio;maxId=id}
    }
    // Scrolling NEVER changes which groups are expanded. An earlier version opened the
    // group containing the visible section, but a smooth scroll passes through every
    // section on the way, so each one fired and the whole sidebar unfolded after a single
    // click. Expansion is the reader's choice; scrolling only moves the highlight.
    //
    // When the visible section sits inside a collapsed group, the highlight goes to that
    // group's header instead, so "you are here" still reads correctly without anything
    // springing open.
    tocList.querySelectorAll('.toc-head').forEach(h=>h.classList.remove('current'));
    if(maxId){
      const owner=tocList.querySelector('[data-toc-children] a[data-section-id="'+CSS.escape(maxId)+'"]');
      const box=owner?owner.closest('[data-toc-children]'):null;
      if(box&&!box.classList.contains('open')){
        const gid=box.getAttribute('data-toc-children');
        const head=tocList.querySelector('[data-toc-group="'+CSS.escape(gid)+'"]');
        if(head)head.classList.add('current');
      }
    }
    const links=tocList.querySelectorAll('a[data-section-id]');
    links.forEach(a=>{
      const sid=a.dataset.sectionId;
      a.classList.remove('visible','primary');
      if(sid in visible){
        if(sid===maxId){
          a.classList.add('primary');
          const sidebar=document.getElementById('sidebar');
          const sidebarRect=sidebar.getBoundingClientRect();
          const aRect=a.getBoundingClientRect();
          const offset=aRect.top-sidebarRect.top-(sidebarRect.height/2)+(aRect.height/2);
          sidebar.scrollBy({top:offset,behavior:'smooth'});
        }else{a.classList.add('visible')}
      }
    });
  }
});

// Chat panel
const chatPanel=document.getElementById('chat-panel');
const chatBody=document.getElementById('chat-body');
const chatMessages=document.getElementById('chat-messages');
const chatInput=document.getElementById('chat-input');
const chatSend=document.getElementById('chat-send');
const chatToggle=document.getElementById('chat-toggle');
const chatDock=document.getElementById('chat-dock');
let chatCollapsed=true;let chatDocked=false;

function toggleChat(){chatCollapsed=!chatCollapsed;chatBody.style.display=chatCollapsed?'none':'flex';chatToggle.innerHTML=chatCollapsed?'&#x25B2;':'&#x25BC;'}
function dockChat(){chatDocked=!chatDocked;
if(chatDocked){chatCollapsed=false;chatBody.style.display='flex';chatToggle.style.display='none';chatDock.innerHTML='&#x2B13;';chatDock.title='Undock';chatPanel.style.position='fixed';chatPanel.style.top='88px';chatPanel.style.right='0';chatPanel.style.bottom='0';chatPanel.style.width='380px';chatPanel.style.maxHeight='none';chatPanel.style.borderRadius='0';document.querySelector('.content').style.marginRight='380px'}
else{chatToggle.style.display='';chatDock.innerHTML='&#x2B12;';chatDock.title='Dock to side';chatPanel.style.position='fixed';chatPanel.style.top='';chatPanel.style.bottom='0';chatPanel.style.width='420px';chatPanel.style.maxHeight='50vh';chatPanel.style.borderRadius='12px 12px 0 0';document.querySelector('.content').style.marginRight='0'}}
chatToggle.onclick=toggleChat;chatDock.onclick=dockChat;

function escH(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function safeLinks(h){const t=document.createElement('template');t.innerHTML=h;t.content.querySelectorAll('[href]').forEach(a=>{const u=(a.getAttribute('href')||'').replace(/[\\x00-\\x20]/g,'');if(!/^(https?:|mailto:|#|\\/|\\.{1,2}\\/|[^:]*$)/i.test(u))a.setAttribute('href','#')});return t.innerHTML}
function mdSafe(s){const e=escH(s);return safeLinks(typeof marked!=='undefined'?marked.parse(e):e.replace(/\\n/g,'<br>'))}
async function sendChat(){const q=chatInput.value.trim();if(!q)return;
chatInput.value='';chatSend.disabled=true;
chatMessages.innerHTML+='<div style="margin-bottom:12px;padding:8px 12px;border-radius:8px;background:#edf2f7;color:#2d3748">'+escH(q)+'</div>';chatMessages.scrollTop=chatMessages.scrollHeight;
chatMessages.innerHTML+='<div style="margin-bottom:12px;padding:8px 12px;border-radius:8px;background:#fffbf0;color:#718096;font-style:italic" id="thinking">Thinking...</div>';chatMessages.scrollTop=chatMessages.scrollHeight;
try{const r=await fetch((window.API_BASE||'')+'/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q,top_k:15})});
const d=await r.json();document.getElementById('thinking')?.remove();if(d.error){chatMessages.innerHTML+='<div style="margin-bottom:12px;padding:8px 12px;border-radius:8px;background:#fffbf0;border:1px solid #fbd38d">Error: '+escH(d.error)+'</div>'}else{
let src='';if(d.sources&&d.sources.length){// Show the titles only. The still name ('state/', 'permission/') is the extraction
// pass that found it \u2014 internal vocabulary that means nothing to a reader \u2014 and the
// answer already cites blocks inline. The pass COUNT is worth keeping: several
// independent passes agreeing is the trust signal.
const passes=new Set(d.sources.map(s=>s.still).filter(Boolean)).size;const agree=passes>1?' \u00b7 corroborated by '+passes+' independent analysis passes':'';src='<div style="font-size:11px;color:#718096;margin-top:6px">Based on: '+escH(d.sources.slice(0,4).map(s=>s.title).join('; '))+agree+'</div>'}
chatMessages.innerHTML+='<div style="margin-bottom:12px;padding:8px 12px;border-radius:8px;background:#fffbf0;border:1px solid #fbd38d">'+mdSafe(d.answer)+src+'</div>'}}
catch(e){document.getElementById('thinking')?.remove();chatMessages.innerHTML+='<div style="margin-bottom:12px;padding:8px 12px;border-radius:8px;background:#fffbf0;border:1px solid #fbd38d">The chat request failed: '+escH(e&&e.message?e.message:e)+'.<br><span style="font-size:11px;color:#718096">If the rest of this page loaded, the server is running \u2014 check its terminal output for the cause (commonly expired AWS credentials).</span></div>'}
chatSend.disabled=false;chatMessages.scrollTop=chatMessages.scrollHeight}
chatSend.onclick=sendChat;chatInput.onkeydown=e=>{if(e.key==='Enter')sendChat()};

// Validate-block messages from content iframes
window.addEventListener('message',(event)=>{
  if(event.source!==frame.contentWindow||!event.data||event.data.type!=='validate-block')return;
  const d=event.data;
  if(chatCollapsed)toggleChat();
  chatMessages.innerHTML+='<div style="margin-bottom:12px;padding:8px 12px;border-radius:8px;background:#edf2f7">Validate: <strong>'+escH(d.title)+'</strong><br><small>Evidence: '+escH(d.evidence)+'</small></div>';
  chatMessages.innerHTML+='<div style="margin-bottom:12px;padding:8px 12px;border-radius:8px;background:#fffbf0;color:#718096;font-style:italic" id="validating">Fetching source & validating...</div>';
  chatMessages.scrollTop=chatMessages.scrollHeight;
  fetch((window.API_BASE||'')+'/api/validate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:d.title,block:d.block,evidence:d.evidence})})
  .then(r=>r.json())
  .then(result=>{
    document.getElementById('validating')?.remove();
    if(result.error){chatMessages.innerHTML+='<div style="margin-bottom:12px;padding:8px 12px;border-radius:8px;background:#fffbf0;border:1px solid #fbd38d">Error: '+escH(result.error)+'</div>';}
    else{
      let html=mdSafe(result.answer);
      if(result.cross_refs&&result.cross_refs.length){
        html+='<div style="margin-top:12px;padding-top:8px;border-top:1px solid #e2e8f0;font-size:12px"><strong>See also:</strong><ul style="margin:4px 0 0 16px">';
        result.cross_refs.forEach(ref=>{
          const page='pages/stills_'+ref.still+'.html';
          html+='<li><a href="#" data-page="'+escH(page)+'" data-title="'+escH(ref.title)+'" onclick="navToConcept(this.dataset.page,encodeURIComponent(this.dataset.title));return false" style="color:#f59e0b">'+escH(ref.title)+'</a> <span style="color:#8b98a5">('+escH(ref.still)+')</span></li>';
        });
        html+='</ul></div>';
      }
      chatMessages.innerHTML+='<div style="margin-bottom:12px;padding:8px 12px;border-radius:8px;background:#fffbf0;border:1px solid #fbd38d">'+html+'</div>';
    }
    chatMessages.scrollTop=chatMessages.scrollHeight;
  })
  .catch(e=>{document.getElementById('validating')?.remove();chatMessages.innerHTML+='<div style="margin-bottom:12px;padding:8px 12px;border-radius:8px;background:#fffbf0;border:1px solid #fbd38d">Validation unavailable. Start the server.</div>';chatMessages.scrollTop=chatMessages.scrollHeight;});
});

// Dependencies: render a list of code-derived dependency items with concept links
function renderDepList(items){
  if(!items||!items.length)return '<div style="color:#8b98a5;font-size:12px">none found</div>';
  let h='<ul style="margin:4px 0 0 16px;padding:0">';
  items.slice(0,25).forEach(it=>{
    const dir=it.direction?('<span style="color:#2563eb">'+escH(it.direction)+'</span> '):'';
    const label=it.business_name?(escH(it.business_name)+' <span style="color:#8b98a5">('+escH(it.object)+')</span>'):escH(it.object);
    let concepts='';
    if(it.concepts&&it.concepts.length){
      concepts='<br><span style="font-size:11px;color:#8b98a5">→ '+it.concepts.slice(0,4).map(k=>{
        const parts=k.split('::');const st=parts[0];const ti=parts.slice(1).join('::');
        const page='pages/stills_'+st+'.html';
        return '<a href="#" data-page="'+escH(page)+'" data-title="'+escH(ti)+'" onclick="navToConcept(this.dataset.page,encodeURIComponent(this.dataset.title));return false" style="color:#f59e0b">'+escH(ti.replace(/^\\[[A-Z]+-\\d+\\]\\s*/,'').slice(0,60))+'</a>';
      }).join(' · ')+'</span>';
    }
    h+='<li style="margin-bottom:6px;font-size:12px">'+dir+label+' <span style="color:#a0aec0">'+escH(it.confidence||'')+'</span>'+concepts+'</li>';
  });
  h+='</ul>';
  return h;
}

window.addEventListener('message',(event)=>{
  if(event.source!==frame.contentWindow||!event.data||event.data.type!=='deps-block')return;
  const d=event.data;
  if(chatCollapsed)toggleChat();
  chatMessages.innerHTML+='<div style="margin-bottom:12px;padding:8px 12px;border-radius:8px;background:#edf2f7">Dependencies: <strong>'+escH(d.title)+'</strong></div>';
  chatMessages.innerHTML+='<div style="margin-bottom:12px;padding:8px 12px;border-radius:8px;background:#eff6ff;color:#718096;font-style:italic" id="deploading">Tracing code dependencies…</div>';
  chatMessages.scrollTop=chatMessages.scrollHeight;
  fetch((window.API_BASE||'')+'/api/dependencies',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:d.title,still:d.still})})
  .then(r=>r.json())
  .then(result=>{
    document.getElementById('deploading')?.remove();
    if(result.error){chatMessages.innerHTML+='<div style="margin-bottom:12px;padding:8px 12px;border-radius:8px;background:#eff6ff;border:1px solid #93c5fd">'+escH(result.error)+'</div>';}
    else{
      let html='';
      if(result.anchors&&result.anchors.length){
        html+='<div style="font-size:11px;color:#8b98a5;margin-bottom:6px">Anchored to code objects: '+escH(result.anchors.slice(0,6).join(', '))+'</div>';
      }else{
        html+='<div style="font-size:12px;color:#8b98a5">'+escH(result.note||'No code-level footprint found for this concept.')+'</div>';
      }
      html+='<div style="margin-top:6px"><strong style="font-size:12px">Requires (calls / reads / writes):</strong>'+renderDepList(result.requires)+'</div>';
      html+='<div style="margin-top:10px"><strong style="font-size:12px">Required by (depended on by):</strong>'+renderDepList(result.required_by)+'</div>';
      chatMessages.innerHTML+='<div style="margin-bottom:12px;padding:8px 12px;border-radius:8px;background:#eff6ff;border:1px solid #93c5fd">'+html+'</div>';
    }
    chatMessages.scrollTop=chatMessages.scrollHeight;
  })
  .catch(e=>{document.getElementById('deploading')?.remove();chatMessages.innerHTML+='<div style="margin-bottom:12px;padding:8px 12px;border-radius:8px;background:#eff6ff;border:1px solid #93c5fd">Dependency service unavailable.</div>';chatMessages.scrollTop=chatMessages.scrollHeight;});
});

// Cross-reference deep links → navigate to still page + scroll to concept
let pendingConcept=null;
function navToConcept(page, encodedTitle){
  const title=decodeURIComponent(encodedTitle);
  const currentSrc=frame.getAttribute('src')||'';
  if(currentSrc.split('#')[0]===page){
    // Already on the target still page — scroll immediately
    frame.contentWindow.postMessage({type:'scrollToConcept',title:title},'*');
  }else{
    pendingConcept=title;
    frame.src=framePage(page);
    // Mark the target still's tab/dropdown active
    const parentBtn=document.querySelector('[data-dropdown="stills-dropdown"]');
    document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
    if(parentBtn)parentBtn.classList.add('active');
    const ddEl=document.getElementById('stills-dropdown');
    if(ddEl){ddEl.querySelectorAll('a').forEach(x=>{x.classList.toggle('active',x.dataset.page===page)});}
  }
  return false;
}
frame.addEventListener('load',()=>{
  if(pendingConcept){
    const title=pendingConcept;pendingConcept=null;
    // Give the still page a moment to init (fetch manifest) before scrolling
    setTimeout(()=>frame.contentWindow.postMessage({type:'scrollToConcept',title:title},'*'),300);
  }
});

renderToc('');
</script>
</body>
</html>"""
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")


# ── Main orchestrator ────────────────────────────────────────────────────────

EVALUATION_TEMPLATE = Path(__file__).parent / "viewer_evaluation.html"


def write_evaluation_page(pages_dir: Path, api_base: str = "") -> bool:
    """pages/evaluation.html: the scorecard page. It holds no numbers itself: it fetches
    ``<api_base>/api/eval`` when opened (the launcher's per-run route, or serve_viewer.py's
    /api/eval), so an evaluation finished after generation shows up without regenerating."""
    if not EVALUATION_TEMPLATE.is_file():
        return False
    html = EVALUATION_TEMPLATE.read_text(encoding="utf-8")
    (pages_dir / "evaluation.html").write_text(
        html.replace('"__EVAL_API_BASE__"', json.dumps(api_base or "")), encoding="utf-8")
    return True


def generate_viewer(base_dir: Path, target_name: str, output_dir: Path,
                    source_repo_url: str = "", disabled_tabs: set | None = None,
                    api_base: str = ""):
    """Generate the full v3 paginated viewer."""
    pages_dir = output_dir / "pages"
    fragments_dir = output_dir / "fragments"
    assets_dir = output_dir / "assets"
    pages_dir.mkdir(parents=True, exist_ok=True)
    fragments_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    stills_dir = base_dir / "stills"
    stills_info = {}

    # ── Executive Summary ──
    readme_candidates = [
        base_dir / "bottled_spec_final" / "README.md",
        base_dir / "bottled_spec" / "README.md",
    ]
    exec_md = ""
    for rc in readme_candidates:
        if rc.exists():
            exec_md = rc.read_text(encoding="utf-8")
            break
    # If no readme, try stills README
    if not exec_md and stills_dir.exists():
        for sd in sorted(stills_dir.iterdir()):
            readme_path = sd / "final" / "README.md"
            if readme_path.exists():
                exec_md = readme_path.read_text(encoding="utf-8")
                break
    exec_html = md_to_html(exec_md, source_repo_url) if exec_md else "<p>No executive summary available.</p>"
    if exec_md:
        exec_html = EXEC_CSS + _exec_restyle(exec_html, target_name)
    write_content_page(pages_dir / "executive_summary.html", "Executive Summary", exec_html)
    print(f"  Executive Summary: {len(exec_html) // 1024} KB")

    # ── Master Blend ──
    blend_candidates = [
        base_dir / "master_blend_final" / "master_blend.md",
        base_dir / "master_blend" / "master_blend.md",
    ]
    blend_md = ""
    for bc in blend_candidates:
        if bc.exists():
            blend_md = bc.read_text(encoding="utf-8")
            break
    if not blend_md:
        for bd in [base_dir / "master_blend_final", base_dir / "master_blend"]:
            if bd.exists():
                blend_md = read_md_files(bd)
                break
    blend_html = md_to_html(blend_md, source_repo_url) if blend_md else "<p>No evidence view available.</p>"
    # State up front what this view is for. It carries the same sections as the Business Spec,
    # so without a note a reader cannot tell which of the two is authoritative — they differ
    # only in that this one keeps the evidence (confidence, cross-pass agreement, which
    # extraction pass found each item) that the Business Spec strips out.
    if blend_md:
        blend_html = (
            '<p style="background:#fff7e6;border-left:3px solid #c05621;padding:10px 14px;'
            'margin:0 0 18px;font-size:13px;color:#4a5568;border-radius:3px">'
            '<strong>This is the audit view.</strong> Same content as the Business Spec, with '
            'the supporting evidence left in: how confident each finding is, how many '
            'independent analysis passes agreed, and which pass it came from. Read the '
            '<strong>Business Spec</strong> tab for the specification itself; come here to '
            'check where something came from.</p>' + blend_html)
    write_content_page(pages_dir / "master_blend.html", "Evidence & Audit Trail", blend_html)
    print(f"  Evidence & Audit Trail: {len(blend_html) // 1024} KB")

    # ── Issues & Insights + bottled business spec ──
    has_issues = generate_issues_page(base_dir, pages_dir, source_repo_url)
    has_bottled = generate_bottled_spec_page(base_dir, pages_dir, source_repo_url)

    # ── Stills (paginated fragments) ──
    if stills_dir.exists():
        for still_dir in sorted(stills_dir.iterdir()):
            if not still_dir.is_dir():
                continue
            still_name = still_dir.name

            # Find formatted directory — prefer signal/ (bifurcated) over raw formatted/
            signal_dir = still_dir / "final" / "formatted" / "signal"
            formatted_dir = still_dir / "final" / "formatted"
            if signal_dir.exists() and any(signal_dir.glob("*.md")):
                formatted_dir = signal_dir
            elif not formatted_dir.exists() or not any(formatted_dir.glob("*.md")):
                # Fallback: try final/ directly
                formatted_dir = still_dir / "final"
                if not formatted_dir.exists() or not any(formatted_dir.glob("*.md")):
                    continue

            print(f"  Still [{still_name}]:")
            manifest = generate_still_fragments(
                still_name, formatted_dir, fragments_dir, source_repo_url
            )
            generate_still_page(still_name, manifest, pages_dir, api_base=api_base)
            stills_info[still_name] = manifest

            if still_name not in STILL_DISPLAY_NAMES:
                STILL_DISPLAY_NAMES[still_name] = still_name.replace("_", " ").title()

    # ── Knowledge Graph ──
    if stills_dir.exists():
        # Copy cytoscape.js asset
        cytoscape_src = Path(__file__).parent / "cytoscape.min.js"
        if cytoscape_src.exists():
            shutil.copy2(cytoscape_src, assets_dir / "cytoscape.min.js")
        generate_graph_page(stills_dir, pages_dir, target_name)

    # ── Evaluation (read at view time from <api_base>/api/eval; see viewer_evaluation.html) ──
    write_evaluation_page(pages_dir, api_base)

    # ── Index ──
    generate_index(
        output_dir, target_name, stills_info,
        has_exec_summary=bool(exec_md),
        has_master_blend=bool(blend_md),
        has_graph=(stills_dir / "knowledge_graph.json").exists() if stills_dir.exists() else False,
        disabled_tabs=disabled_tabs,
        has_issues=has_issues,
        has_bottled=has_bottled,
        api_base=api_base,
    )

    # ── Hardening: safe links and a per-file Content Security Policy (viewer_security.py) ──
    for html_file in output_dir.rglob("*.html"):
        html_file.write_text(viewer_security.harden(html_file.read_text(encoding="utf-8")), encoding="utf-8")

    # Summary
    total_size = sum(f.stat().st_size for f in output_dir.rglob("*") if f.is_file())
    index_size = (output_dir / "index.html").stat().st_size
    print(f"\n  Viewer v3: {output_dir}")
    print(f"  Total size: {total_size / 1024 / 1024:.1f} MB")
    print(f"  Index: {index_size / 1024:.0f} KB")
    print(f"  Stills: {len(stills_info)} with paginated fragments")


# ── CLI entry point ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate paginated HTML viewer (v3)")
    parser.add_argument("output_dir", help="Path to distillation output directory")
    parser.add_argument("target_name", help="Display name of the target system")
    parser.add_argument("--viewer-dir", "-o", default=None,
                        help="Output viewer directory (default: <output_dir>/viewer)")
    parser.add_argument("--source-repo-url", "-r", default="",
                        help="Base URL for source file links")
    parser.add_argument("--api-base", default="",
                        help="Prefix for the viewer's /api/* fetches (e.g. /api/runs/<id> when "
                             "served by the launcher). Default '' = same-origin /api/* for a "
                             "standalone serve_viewer.py.")
    parser.add_argument("--disable-tabs", default="",
                        help="Comma-separated tab IDs to force-disable (kept clickable, "
                             "show a 'not implemented' placeholder). Valid IDs: "
                             "executive_summary, issues, bottled_spec, master_blend, stills, knowledge_graph")
    args = parser.parse_args()

    base_dir = Path(args.output_dir).resolve()
    target_name = args.target_name
    viewer_dir = Path(args.viewer_dir) if args.viewer_dir else base_dir / "viewer"

    if not base_dir.exists():
        print(f"ERROR: Directory not found: {base_dir}")
        sys.exit(1)

    print(f"Generating HTML viewer (v3 — paginated) for: {target_name}")
    print(f"  Source: {base_dir}")
    print(f"  Viewer: {viewer_dir}")
    print()

    disabled_tabs = {t.strip() for t in args.disable_tabs.split(",") if t.strip()}
    if disabled_tabs:
        print(f"  Disabled tabs (feature-flagged off): {', '.join(sorted(disabled_tabs))}")

    generate_viewer(base_dir, target_name, viewer_dir,
                    source_repo_url=args.source_repo_url, disabled_tabs=disabled_tabs,
                    api_base=args.api_base)


if __name__ == "__main__":
    main()
