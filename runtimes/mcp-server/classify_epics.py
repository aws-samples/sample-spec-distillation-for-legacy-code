#!/usr/bin/env python3
"""
classify_epics.py — REPORT pass for the structural classification of the epic tier
(SYSTEM_SPEC §7.2 two-axis model). Measurement only: labels each epic on Axis-1 structural role
and reports the distribution + spot-checks. NO re-cut, NO commit, NO LLM spend — prove the axis
separates on real data before deciding re-cut vs. re-organize.

Axis-1 role (what this pass can decide WITHOUT the dependency graph):
  - VERTICAL   : concentrated in one business module (dominant-module share high, few modules).
  - HORIZONTAL : business-meaningful concern spanning many modules (high scatter) AND its
                 concepts carry business/compliance consequence (business-value present).
  - SYSTEM?    : high scatter AND low business-value AND/OR a technical "module" (SQL/UI/WCF…)
                 → CANDIDATE system/plumbing. The system-wide-business-rule vs. true-plumbing
                 split is deferred (needs served-vertical from the dependency graph — Phase B),
                 so this pass only FLAGS candidates, never calls true-plumbing.

Axis-2 materiality is reported (recovered), never used to lower a role — orthogonality check.

Business-value signal (local, generalizing — not a hardcoded module list): fraction of an
epic's concepts whose recovered "What breaks if violated" / "Business rationale" carries
business/compliance/financial consequence vs. purely technical. (Regexes are lenses over the
recovered field, not fabrication; the split is measured, thresholds are diagnostic.)
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import story_module as SM

_BIZ = re.compile(r"complian|legal|audit|tax|financ|billing|invoice|payment|contract|regulat|"
                  r"disput|owed|remit|liabilit|payroll|customer|vendor|statut|GL\b|revenue|"
                  r"reconcil|withhold|deduction|approval", re.I)
_TECH = re.compile(r"\bUI\b|refresh|render|flicker|stale data|crash|null reference|repaint|"
                   r"form load|screen freeze|latency|http|\bsql error|grid|control\b", re.I)
# tokens that are technical/plumbing rather than business modules (used only as a WEAK hint,
# alongside the measured business-value ratio — never the sole decider)
_TECH_MODULES = {"SQL", "UI", "UX", "API", "DB", "WCF", "WF", "ELSE", "ID"}


def _consequence_class(body: str) -> str:
    m = re.search(r"What breaks if violated:\s*(.*)", body or "")
    line = (m.group(1)[:400] if m else "")
    if not line:
        m2 = re.search(r"Business rationale:\s*(.*)", body or "")
        line = (m2.group(1)[:400] if m2 else "")
    hb, ht = bool(_BIZ.search(line)), bool(_TECH.search(line))
    if hb and not ht:
        return "business"
    if ht and not hb:
        return "technical"
    if hb and ht:
        return "mixed"
    return "neither"


_THEME_SYS = (
    "You classify the STRUCTURAL ROLE of a software capability grouping in a specification. "
    "Decide which ONE role the grouping's THEME represents:\n"
    "- VERTICAL: a business-DOMAIN capability — a value stream a domain owner owns (e.g. billing, "
    "payroll, purchasing, invoicing). The theme is a business NOUN/domain.\n"
    "- HORIZONTAL: a business-meaningful concern that CUTS ACROSS many domains (e.g. "
    "data-integrity/immutability, audit logging, format validation, approval sequencing). The "
    "theme is a cross-cutting PATTERN with business consequence, applied everywhere.\n"
    "- SYSTEM: a platform/infrastructure/framework concern (e.g. UI state, service identity, "
    "menu navigation, document rendering, dependency versioning). Technical plumbing.\n"
    "Judge the THEME, using the provided structural signals as evidence (module scatter, "
    "business-value ratio) but decide on meaning. Emit STRICT JSON: "
    "{\"role\":\"VERTICAL|HORIZONTAL|SYSTEM\",\"confidence\":1-5,\"why\":\"<=12 words\"}. No prose."
)


def semantic_role(llm, model, epic_row, sample_titles):
    import bedrock_client
    import json_extract
    ev = (f"Epic theme: {epic_row['name']}\n"
          f"Structural signals: dominant_module={epic_row['dom_module']} "
          f"dom_share={epic_row['dom_share']} distinct_modules={epic_row['distinct_modules']} "
          f"business_value_ratio={epic_row['biz_ratio']}\n"
          f"Sample member capabilities:\n" + "\n".join(f"- {t[:70]}" for t in sample_titles[:12]))
    for attempt in range(3):
        try:
            res = bedrock_client.call_llm(llm, model, _THEME_SYS,
                                             ev + "\n\nClassify the theme's role.",
                                             max_tokens=120, temperature=0.0)
            r = json.loads(json_extract.strip_json(res.output_text, "object"))
            role = (r.get("role") or "").strip().upper()
            if role in ("VERTICAL", "HORIZONTAL", "SYSTEM"):
                return role, int(r.get("confidence", 3)), (r.get("why") or "")[:80], res.input_tokens, res.output_tokens
        except Exception as e:
            es = str(e).lower()
            if ("429" in str(e) or "throttl" in es) and attempt < 2:
                import random
                time.sleep((3 * (2 ** attempt)) + random.uniform(0, 3)); continue
            break
    return None, 0, "unresolved", 0, 0


def classify(conn, min_mass=10):
    story_mod, vocab, cov = SM.build_story_modules(conn, min_mass=min_mass)
    # epic -> stories, story -> concept bodies
    epic_stories = defaultdict(list)
    for src, dst in conn.execute("SELECT src, dst FROM ontology_edges WHERE relation='story_in_epic'"):
        epic_stories[dst].append(src)
    s2c = defaultdict(list)
    for src, dst in conn.execute("SELECT src, dst FROM ontology_edges WHERE relation='concept_in_story'"):
        s2c[dst].append(src)
    # concept bodies (all grouped)
    need = {c for cs in s2c.values() for c in cs}
    body = {}
    nl = list(need)
    for i in range(0, len(nl), 900):
        chunk = nl[i:i+900]
        for cid, b in conn.execute(
            "SELECT concept_id, body_raw FROM unified_concepts WHERE concept_id IN (%s)" % ",".join("?"*len(chunk)), chunk):
            body[cid] = b
    epics = {eid: (name, sc) for eid, name, sc in conn.execute("SELECT epic_id, name, story_count FROM ontology_epic")}
    uc_title = {r[0]: r[1] for r in conn.execute("SELECT concept_id, title FROM unified_concepts")}

    rows = []
    for eid, (name, sc) in epics.items():
        stories = epic_stories.get(eid, [])
        sample_titles = []
        for s in stories[:12]:
            cids = s2c.get(s, [])
            if cids:
                sample_titles.append(uc_title.get(cids[0], ""))
        modbag = Counter(story_mod[s] for s in stories if story_mod.get(s))
        tot_mod = sum(modbag.values())
        dom_mod, dom_n = (modbag.most_common(1)[0] if modbag else (None, 0))
        dom_share = (dom_n / tot_mod) if tot_mod else 0.0
        distinct_mods = len(modbag)
        # business-value ratio over the epic's concepts
        cc = Counter()
        for s in stories:
            for c in s2c.get(s, []):
                cc[_consequence_class(body.get(c, ""))] += 1
        n_conseq = sum(cc.values()) or 1
        biz_ratio = (cc["business"] + cc["mixed"]) / n_conseq
        tech_ratio = cc["technical"] / n_conseq
        dom_is_tech = dom_mod in _TECH_MODULES

        # ── Axis-1 STRUCTURAL role (graph-free; a prior/cross-check for the semantic call) ──
        if tot_mod and dom_share >= 0.55 and distinct_mods <= 4 and not dom_is_tech:
            struct_role = "VERTICAL"
        elif dom_is_tech or (biz_ratio < 0.30 and distinct_mods >= 5):
            struct_role = "SYSTEM"
        elif distinct_mods >= 5 and biz_ratio >= 0.30:
            struct_role = "HORIZONTAL"
        else:
            struct_role = "MIXED"

        rows.append({
            "epic_id": eid, "name": name, "stories": sc,
            "dom_module": dom_mod, "dom_share": round(dom_share, 2),
            "distinct_modules": distinct_mods, "biz_ratio": round(biz_ratio, 2),
            "tech_ratio": round(tech_ratio, 2), "struct_role": struct_role,
            "sample_titles": sample_titles,
        })
    return rows, len(vocab), cov


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--min-mass", type=int, default=10)
    ap.add_argument("--show", type=int, default=10, help="examples per role")
    ap.add_argument("--semantic", action="store_true", help="add the LLM theme-role judgment")
    ap.add_argument("--profile", default=""); ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--model", default="anthropic.claude-haiku-4-5")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--out", default="", help="optional: write full per-epic JSON to this path")
    ap.add_argument("--commit", action="store_true", help="persist labels to ontology_epic_class (sibling table)")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    rows, vocab_n, cov = classify(conn, min_mass=args.min_mass)

    if args.semantic:
        import bedrock_client
        llm = bedrock_client.create_llm_client(args.profile, args.region)
        ti = to = 0
        t0 = time.time()
        done = 0
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = {ex.submit(semantic_role, llm, args.model, r, r["sample_titles"]): r for r in rows}
            for fut in as_completed(futs):
                r = futs[fut]
                srole, sconf, why, i, o = fut.result()
                r["sem_role"], r["sem_conf"], r["sem_why"] = srole, sconf, why
                ti += i; to += o; done += 1
                if done % 100 == 0 or done == len(rows):
                    cost = ti/1e6*1.0 + to/1e6*5.0
                    print(json.dumps({"stage": "semantic", "done": f"{done}/{len(rows)}",
                                      "cost_usd": round(cost, 3), "elapsed_s": round(time.time()-t0,1)}), flush=True)
        # ── reconcile structural + semantic → FINAL role ──
        # Semantic decides the V/H/S meaning; structural is the cross-check. Agreement → confident.
        # Disagreement or low semantic confidence → MIXED (flagged for judgment, §9.2). SYSTEM stays
        # a CANDIDATE regardless (system-vs-plumbing needs the served-vertical graph — Phase B).
        for r in rows:
            s, st = r.get("sem_role"), r["struct_role"]
            if s is None:
                r["role"] = "MIXED"; r["role_note"] = "semantic unresolved"
            elif s == st or (st == "MIXED"):
                r["role"] = s; r["role_note"] = "agree" if s == st else "semantic (struct mixed)"
            elif r.get("sem_conf", 0) >= 4:
                r["role"] = s; r["role_note"] = f"semantic>struct ({st})"
            else:
                r["role"] = "MIXED"; r["role_note"] = f"disagree sem={s}/struct={st}"
    else:
        for r in rows:
            r["role"] = r["struct_role"]; r["role_note"] = "structural-only"

    dist = Counter(r["role"] for r in rows)
    print(json.dumps({"epics": len(rows), "module_vocab": vocab_n, "module_coverage": round(cov, 2),
                      "role_distribution": dict(dist), "semantic": args.semantic}, indent=2))
    for role in ["VERTICAL", "HORIZONTAL", "SYSTEM", "MIXED"]:
        ex = [r for r in rows if r["role"] == role]
        ex.sort(key=lambda r: -r["stories"])
        print(f"\n=== {role} ({len(ex)}) — top {args.show} by size ===")
        for r in ex[:args.show]:
            note = r.get("role_note", "")
            print(f"  [{r['stories']:3}st dom={r['dom_module'] or '-':4} share={r['dom_share']:.2f} "
                  f"mods={r['distinct_modules']:2} biz={r['biz_ratio']:.2f}] {r['name'][:52]:52} ({note})")

    if args.out:
        for r in rows:
            r.pop("sample_titles", None)
        with open(args.out, "w") as f:
            json.dump({"module_vocab_n": vocab_n, "coverage": cov, "epics": rows}, f, indent=2)
        print(f"\n[classify] wrote {args.out}")

    if args.commit:
        import datetime as _dt
        now = _dt.datetime.now(_dt.UTC).isoformat()
        # SIBLING table — never mutates ontology_epic in place (additive, reversible). SYSTEM is
        # persisted as a CANDIDATE (system_split='candidate') — the system-wide-business-rule vs
        # true-plumbing decision is deferred to Phase B (served-vertical). Materiality is a
        # SEPARATE axis (recovered elsewhere) — this table carries only structural role.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ontology_epic_class (
                epic_id TEXT PRIMARY KEY,
                role TEXT NOT NULL,             -- VERTICAL | HORIZONTAL | SYSTEM | MIXED
                system_split TEXT,              -- 'candidate' for SYSTEM (Phase-B decides), else NULL
                dom_module TEXT, dom_share REAL, distinct_modules INTEGER, biz_ratio REAL,
                struct_role TEXT, sem_role TEXT, sem_conf INTEGER,
                role_note TEXT, sem_why TEXT, created_at TEXT NOT NULL
            );
            """
        )
        conn.execute("DELETE FROM ontology_epic_class")  # full rewrite each run (idempotent)
        for r in rows:
            conn.execute(
                "INSERT INTO ontology_epic_class "
                "(epic_id,role,system_split,dom_module,dom_share,distinct_modules,biz_ratio,"
                " struct_role,sem_role,sem_conf,role_note,sem_why,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["epic_id"], r["role"], "candidate" if r["role"] == "SYSTEM" else None,
                 r["dom_module"], r["dom_share"], r["distinct_modules"], r["biz_ratio"],
                 r["struct_role"], r.get("sem_role"), r.get("sem_conf"),
                 r.get("role_note"), r.get("sem_why"), now))
        conn.commit()
        print(f"[classify] committed {len(rows)} epic classifications to ontology_epic_class "
              f"(SYSTEM persisted as candidate — Phase-B decides system-vs-plumbing)")


if __name__ == "__main__":
    main()
