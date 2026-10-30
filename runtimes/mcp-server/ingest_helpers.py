#!/usr/bin/env python3
"""ingest_helpers.py — the source→text INGEST HELPER architecture (the normalization stage that
runs BEFORE paging). One narrow contract, a pluggable registry (MCP-tool-like), and a graded
fallback ladder. This is what turns a non-text-native source (a binary .rpt, an image, a doc)
into the common text substrate the stills read — OR books it as a metadata-only stub when no
helper can lift it (never dumping raw binary into the token budget).

WHY (measured): a binary flattened into a page is ~49% dead noise (UTF-8 replacement + NUL) and,
re-read once per still (11 stills), wastes significant Sonnet input on a single binary family
alone — for ~0 recoverable signal (the OLE structure is destroyed by the text decode).
So: lift at ingest (once) to concentrated text, or emit presence-only metadata. Never page raw
bytes.

THE MODEL — every source file resolves to one of three CONTENT-FIDELITY kinds (recorded on the
[FILE:] marker so downstream knows how much to trust a citation):
  raw          text-native (.cs/.sql/…): content is the file itself, line-numbered, cite-by-line.
  inferred     a helper produced DERIVED text (e.g. .rpt via JRC, image via vision): real signal,
               but NO source-line mapping — a citation resolves to the FILE, not a line.
  referential  no helper available (opaque binary): NO body paged (proven wasteful) — only a
               marker + path + size, so the file is BOOKED as present at ~10 tokens. (Back-compat:
               a referential block MAY carry a body that readers MUST ignore — lets an existing
               run's pages be re-marked without stripping their internals; new runs emit empty.)

THE HELPER CONTRACT — a helper is pluggable and may be `local` (shell/subprocess, own runtime) or
`remote` (SaaS/API). It declares which file types it handles + an availability probe, and lifts:
    handles(path) -> bool
    available() -> (ok: bool, reason: str)          # e.g. JVM + JRC jars present?
    lift(path) -> LiftResult{text, method, confidence, notes}
Helpers are DUMB normalizers: convert-to-common-text. The intelligence stays in ONE place (the
stills). 50 helpers that each emit text >> 50 helpers that each talk to an LLM.

FALLBACK LADDER (grade-not-gate): try registered helpers best-first; the first AVAILABLE one that
handles the type and returns text wins → `inferred`. If a helper handles the type but is
unavailable (missing runtime), fall to the next. If none lift it → `raw` for text-native, else
`referential` (booked). Every outcome carries its method + confidence (L4: log the uncertainty).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# ── content-fidelity kinds (the tri-state recorded on the [FILE:] marker) ─────────────────────
KIND_RAW = "raw"
KIND_INFERRED = "inferred"
KIND_REFERENTIAL = "referential"


@dataclass
class LiftResult:
    """A helper's output. text=None means the helper declined/failed (fall through the ladder)."""
    text: str | None
    method: str                      # e.g. "rpt-jrc", "image-vision" — recorded as VIA:
    confidence: float                # 0.0-1.0, the helper's self-assessed recovery confidence
    notes: str = ""                  # human note (what was/wasn't recovered), for audit
    ok: bool = True                  # False = attempted and errored (surface, don't crash)


@dataclass
class Resolution:
    """The per-file decision the materialization pass consumes."""
    path: str
    kind: str                        # raw | inferred | referential
    text: str | None = None          # the pageable text (inferred); None for raw/referential
    method: str = ""                 # VIA: tag (inferred) or "" (raw/referential)
    confidence: float = 1.0
    notes: str = ""
    source_size: int = 0             # bytes of the ORIGINAL source file (SIZE: on the marker)


# ── text-native extensions: these are `raw`, no helper needed ─────────────────────────────────
# A conservative set of clearly text-native code/markup/config extensions. Anything not here and
# not handled by a helper falls to `referential` (booked) unless it sniffs as text at resolve time.
_TEXT_NATIVE = {
    "cs", "vb", "c", "cpp", "h", "hpp", "java", "js", "ts", "jsx", "tsx", "py", "rb", "go",
    "php", "vbs", "ps1", "sh", "bat", "cmd", "sql", "pls", "plsql", "r", "scala", "kt", "swift",
    "html", "htm", "cshtml", "aspx", "ascx", "xaml", "css", "scss", "less", "jsp", "vue",
    "xml", "xsd", "wsdl", "config", "json", "jsonl", "yaml", "yml", "toml", "ini", "csv", "tsv",
    "md", "markdown", "txt", "text", "rst", "adoc", "tex", "properties", "gitignore",
    "gitattributes", "dockerignore", "sln", "csproj", "vbproj", "vcxproj", "proj", "props",
    "targets", "resx", "resw", "rdl", "rdlc", "wxs", "wxi", "dtsx",
    # COBOL source, copybooks and job control are plain text (fixed/free format) — page them raw.
    "cob", "cbl", "cpy", "jcl", "pco",
}

# Bump when resolve()/materialize() semantics change (kind decisions, cache layout). Part of the
# ingest-cache key and of the Phase 0 source identity, so a change re-materializes.
INGEST_VERSION = "2"


def _ext(path: str) -> str:
    name = path.replace("\\", "/").split("/")[-1].lower()
    return name.rsplit(".", 1)[-1] if "." in name else ""


def _is_text_native(path: str) -> bool:
    return _ext(path) in _TEXT_NATIVE


def _sniffs_as_text(path: str, probe: int = 8192) -> bool:
    """Cheap content sniff for UNKNOWN extensions (the promise in the _TEXT_NATIVE comment): the
    first `probe` bytes are non-empty, contain no NUL, and decode as UTF-8 (or are >=95% printable
    latin-1). Text that sniffs clean is paged `raw` instead of booked `referential`."""
    try:
        with open(path, "rb") as f:
            head = f.read(probe)
    except OSError:
        return False
    if not head or b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        if len(head) == probe and e.start >= len(head) - 4:  # multibyte char cut by the probe
            return True
    printable = sum(1 for b in head if b in (9, 10, 13) or 32 <= b < 127 or b >= 160)
    return printable / len(head) >= 0.95


# ── the pluggable helper base ─────────────────────────────────────────────────────────────────
class IngestHelper:
    """Base contract. Subclasses declare id/kind, which types they handle, an availability probe,
    and the lift. `kind` is 'local' (shell/subprocess) or 'remote' (API). `rank` orders the
    ladder (lower = tried first). A helper that handles a type but is unavailable is skipped."""
    id: str = "base"
    version: str = "1"               # bump when the helper's output changes (part of cache key)
    kind: str = "local"
    rank: int = 100

    def handles(self, path: str) -> bool:
        raise NotImplementedError

    def available(self) -> tuple[bool, str]:
        return True, ""

    def lift(self, path: str) -> LiftResult:
        raise NotImplementedError


# ── .rpt via SAP JRC (Java) — the PROVEN report-definition extractor ──────────────────────────
# Runs a tiny Java tool over the JRC classpath (JVM 17 + the crsdk lib jars + icu4j) that opens
# the report and emits tables/fields/formulas/record-selection/params/groups/summaries as text.
# Provisioned, not pip: availability is PROBED (java present + jars present); absent → skipped →
# .rpt degrades to `referential`. Paths come from env so the framework code carries no machine
# specifics (the jars live under gitignored examples/ on this box; a deploy points env at its own).
class RptJrcHelper(IngestHelper):
    id = "rpt-jrc"
    kind = "local"
    rank = 10

    ENV_JAVA = "RPT_JRC_JAVA"          # path to java 17 binary
    ENV_CLASSPATH = "RPT_JRC_CLASSPATH"  # classpath: crsdk lib jars + icu4j + the compiled tool dir
    ENV_MAIN = "RPT_JRC_MAIN"          # main class (default RptExtract)

    def handles(self, path: str) -> bool:
        return _ext(path) == "rpt"

    def available(self) -> tuple[bool, str]:
        java = os.environ.get(self.ENV_JAVA) or shutil.which("java")
        if not java:
            return False, "no java (set RPT_JRC_JAVA or put java on PATH)"
        cp = os.environ.get(self.ENV_CLASSPATH)
        if not cp:
            return False, "RPT_JRC_CLASSPATH unset (JRC jars + tool dir)"
        # cheap sanity: at least one classpath entry exists
        entries = [e for e in cp.split(os.pathsep) if e]
        if not any(Path(e).exists() or list(Path(e).parent.glob(Path(e).name)) for e in entries[:3]):
            return False, "RPT_JRC_CLASSPATH entries not found on disk"
        return True, ""

    NOTES = ("JRC report definition (tables/fields/formulas/selection/params/groups/summaries); "
             "live SQL string not resolved offline")

    def _jvm(self):
        return (os.environ.get(self.ENV_JAVA) or shutil.which("java"),
                os.environ.get(self.ENV_CLASSPATH), os.environ.get(self.ENV_MAIN, "RptExtract"))

    def lift(self, path: str) -> LiftResult:
        """Single-file lift — spawns ONE JVM for this file. Fine for the one-off/CLI path; the
        materialize pass uses lift_batch() so a whole corpus pays ONE JVM start, not N."""
        ok, reason = self.available()
        if not ok:
            return LiftResult(None, self.id, 0.0, notes=reason, ok=False)
        java, cp, main = self._jvm()
        try:
            proc = subprocess.run([java, "-cp", cp, main, path],
                                  capture_output=True, text=True, timeout=180)
        except (subprocess.TimeoutExpired, OSError) as e:
            return LiftResult(None, self.id, 0.0, notes=f"jrc invoke failed: {e}", ok=False)
        text = (proc.stdout or "").strip()
        if proc.returncode != 0 or not text:
            err = (proc.stderr or "").strip().splitlines()
            return LiftResult(None, self.id, 0.0,
                              notes=f"jrc rc={proc.returncode}: {err[0] if err else 'no output'}",
                              ok=False)
        return LiftResult(text, self.id, 0.9, notes=self.NOTES)

    def lift_batch(self, jobs: list[tuple[str, str]], timeout: int = 3600) -> dict[str, LiftResult]:
        """Batch lift: ONE warm JVM for the whole set. `jobs` = [(rpt_path, out_file), …]; the tool
        writes each report's text to its out_file and prints OK/FAIL status per report. Returns
        {rpt_path -> LiftResult}. A report the JVM fails on gets an ok=False result (→ ladder). If
        the JVM can't start at all, ALL jobs get ok=False (→ ladder for each)."""
        results: dict[str, LiftResult] = {}
        ok, reason = self.available()
        if not ok:
            return {p: LiftResult(None, self.id, 0.0, notes=reason, ok=False) for p, _ in jobs}
        java, cp, main = self._jvm()
        manifest = "".join(f"{p}\t{o}\n" for p, o in jobs)
        try:
            proc = subprocess.run([java, "-cp", cp, main], input=manifest,
                                  capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as e:
            return {p: LiftResult(None, self.id, 0.0, notes=f"jrc batch failed: {e}", ok=False)
                    for p, _ in jobs}
        for line in (proc.stdout or "").splitlines():
            parts = line.rstrip("\n").split("\t")
            if parts[0] == "OK" and len(parts) >= 4:
                _, rpt, outf, nbytes = parts[0], parts[1], parts[2], parts[3]
                results[rpt] = LiftResult(_OUT_MARKER + outf, self.id, 0.9, notes=self.NOTES)
            elif parts[0] == "FAIL" and len(parts) >= 3:
                results[parts[1]] = LiftResult(None, self.id, 0.0, notes=parts[2], ok=False)
        # any job with no status line (JVM died mid-stream) → ok=False
        for p, _ in jobs:
            results.setdefault(p, LiftResult(None, self.id, 0.0,
                               notes="no status from jrc batch (jvm died?)", ok=False))
        return results


# Sentinel: batch lift writes text directly to the out_file; LiftResult.text carries a marker
# "<_OUT_MARKER><path>" telling materialize the text is already on disk at <path> (no re-write).
_OUT_MARKER = "\0FILE:"


# ── .rpt (and other OLE) envelope fallback — pip-clean, near-free, WEAK ───────────────────────
# When the JRC helper is unavailable, recover at least the OLE container METADATA (title/author/
# stream inventory) via olefile. This is NOT the report content (proven: streams are proprietary-
# encoded), so confidence is LOW and it's clearly labeled — a small step above `referential`.
class OleEnvelopeHelper(IngestHelper):
    id = "ole-envelope"
    kind = "local"
    rank = 50

    def handles(self, path: str) -> bool:
        return _ext(path) in ("rpt", "doc", "xls", "ppt", "msg")  # OLE compound-doc families

    def available(self) -> tuple[bool, str]:
        try:
            import olefile  # noqa: F401
            return True, ""
        except Exception:
            return False, "olefile not installed"

    def lift(self, path: str) -> LiftResult:
        try:
            import olefile
        except Exception as e:
            return LiftResult(None, self.id, 0.0, notes=str(e), ok=False)
        if not olefile.isOleFile(path):
            return LiftResult(None, self.id, 0.0, notes="not an OLE file", ok=False)
        try:
            ole = olefile.OleFileIO(path)
            lines = [f"# OLE ENVELOPE (metadata only; report CONTENT not recovered): {Path(path).name}"]
            try:
                md = ole.get_metadata()
                for attr in ("title", "subject", "author", "keywords", "comments",
                             "creating_application", "company", "last_saved_by"):
                    v = getattr(md, attr, None)
                    if v:
                        vs = v.decode("latin-1", "replace") if isinstance(v, bytes) else str(v)
                        vs = vs.split("\x00")[0].strip()
                        if vs:
                            lines.append(f"  {attr}: {vs}")
            except Exception as e:
                # Metadata is best-effort enrichment; the stream listing below still yields a
                # useful result. But don't vanish the failure — record it in the output so a
                # missing-metadata case is visible (not silently indistinguishable from "no metadata").
                lines.append(f"  (metadata read skipped: {type(e).__name__}: {e})")
            streams = ["/".join(s) for s in ole.listdir(streams=True)]
            lines.append("  streams: " + ", ".join(sorted(streams)))
            ole.close()
            return LiftResult("\n".join(lines), self.id, 0.15,
                              notes="OLE envelope metadata only — proprietary stream content not extractable")
        except Exception as e:
            return LiftResult(None, self.id, 0.0, notes=f"olefile error: {e}", ok=False)


# ── the registry + resolver ───────────────────────────────────────────────────────────────────
_REGISTRY: list[IngestHelper] = [RptJrcHelper(), OleEnvelopeHelper()]


def register(helper: IngestHelper) -> None:
    _REGISTRY.append(helper)
    _REGISTRY.sort(key=lambda h: h.rank)


def registry() -> list[IngestHelper]:
    return sorted(_REGISTRY, key=lambda h: h.rank)


def helper_versions() -> dict:
    """{helper_id: version:availability} + the ingest version — recorded in the source identity so
    a helper upgrade or a runtime appearing/disappearing (e.g. JRC jars) re-materializes."""
    out = {"_ingest": INGEST_VERSION}
    for h in registry():
        ok, _ = h.available()
        out[h.id] = f"{h.version}:{'available' if ok else 'unavailable'}"
    return out


def resolve(path: str, source_size: int | None = None) -> Resolution:
    """Resolve ONE source file to a Resolution (kind + optional lifted text). The ladder:
      1. any registered helper that handles the type AND is available AND returns text → inferred
      2. else text-native extension → raw
      3. else → referential (booked, no body)
    Grade-not-gate: a helper that errors or is unavailable is skipped (its reason is recorded on
    the chosen Resolution's notes if we fall past it)."""
    size = source_size if source_size is not None else _safe_size(path)
    skipped = []
    for h in registry():
        if not h.handles(path):
            continue
        ok, reason = h.available()
        if not ok:
            skipped.append(f"{h.id} unavailable ({reason})")
            continue
        res = h.lift(path)
        if res.text is not None and res.text.strip():
            note = res.notes + (("; skipped: " + "; ".join(skipped)) if skipped else "")
            return Resolution(path, KIND_INFERRED, text=res.text, method=res.method,
                              confidence=res.confidence, notes=note, source_size=size)
        skipped.append(f"{h.id} no-text ({res.notes})")
    # no helper lifted it
    if _is_text_native(path):
        return Resolution(path, KIND_RAW, source_size=size,
                          notes="; ".join(skipped) if skipped else "")
    if not skipped and _sniffs_as_text(path):
        # Unknown extension with no helper claiming it, but the bytes are text: page it raw.
        return Resolution(path, KIND_RAW, source_size=size,
                          notes="unknown extension; content sniffed as text")
    return Resolution(path, KIND_REFERENTIAL, source_size=size,
                      notes=("no ingest helper available; booked as present. "
                             + ("; ".join(skipped) if skipped else "")).strip())


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ── materialization pass (Phase 0.5): resolve every candidate → materialized manifest ─────────
# Resolves the size chicken-egg: paging needs each unit's SIZE up front, but an `inferred` unit's
# size is the HELPER OUTPUT size, unknown until the helper runs. So materialization runs helpers
# HERE (once), writes lifted text to a cache dir, and emits a manifest row per file with the kind,
# unit location, and size — which paging then consumes with all sizes known. Idempotent: a lifted
# artifact is reused if present (helpers are expensive; never re-lift needlessly).
#
# Materialized manifest = TSV, one row per file:
#   <path>\t<kind>\t<unit_path_or_dash>\t<unit_size>\t<source_size>\t<method>\t<confidence>
# where unit_path is the cache file for `inferred` (holds the lifted text), or '-' for raw/referential.

import csv
import json


def _content_sha(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return "unreadable"
    return h.hexdigest()


def _cache_file(cache: Path, content_sha: str, helper) -> Path:
    """Content-addressed lift cache: keyed on sha256(source bytes) + helper id/version + the
    ingest version — NOT on the path. A changed file misses the cache, a moved file with the same
    bytes hits it, and a helper upgrade misses it."""
    if isinstance(helper, IngestHelper):
        hid, hver = helper.id, helper.version
    else:
        hid = str(helper)
        hver = next((h.version for h in registry() if h.id == hid), "0")
    return cache / f"{content_sha}.{hid}-v{hver}.i{INGEST_VERSION}.ingest.txt"


def materialize(manifest_in: str, source_root: str, cache_dir: str, manifest_out: str) -> dict:
    """Resolve every path in manifest_in → materialized manifest_out. Lifts `inferred` files into
    cache_dir. BATCHES all .rpt through ONE warm JVM (never one JVM per file). Idempotent: an
    already-cached lift is reused (helpers are expensive)."""
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    with open(manifest_in) as f:
        paths = [ln.strip() for ln in f if ln.strip()]

    counts = {"raw": 0, "inferred": 0, "referential": 0, "lifted": 0, "lift_reused": 0,
              "lift_failed": 0}

    def full_of(rel):
        return os.path.join(source_root, rel) if source_root else rel

    # ── Phase A: batch the .rpt files through ONE JVM (writing directly to cache) ──
    rpt_helper = next((h for h in registry() if isinstance(h, RptJrcHelper)), None)
    rpt_batched: dict[str, LiftResult] = {}
    if rpt_helper is not None and rpt_helper.available()[0]:
        jobs = []
        for rel in paths:
            if _ext(full_of(rel)) != "rpt":
                continue
            cf = _cache_file(cache, _content_sha(full_of(rel)), rpt_helper)
            if cf.exists() and cf.stat().st_size > 0:
                rpt_batched[rel] = LiftResult(_OUT_MARKER + str(cf), rpt_helper.id, 0.9,
                                              notes=rpt_helper.NOTES)  # reuse
                counts["lift_reused"] += 1
            else:
                jobs.append((full_of(rel), str(cf)))
        if jobs:
            batch = rpt_helper.lift_batch(jobs)
            # map full-path results back to rel
            full_to_rel = {full_of(r): r for r in paths}
            for fp, res in batch.items():
                rel = full_to_rel.get(fp, fp)
                rpt_batched[rel] = res
                if res.ok and res.text:
                    counts["lifted"] += 1
                else:
                    counts["lift_failed"] += 1

    # ── Phase B: build each manifest row ──
    rows = []
    for rel in paths:
        full = full_of(rel)
        size = _safe_size(full)
        unit, unit_size, kind, method, conf, notes = "-", 0, None, "", 1.0, ""

        pre = rpt_batched.get(rel)
        if pre is not None and pre.ok and pre.text and pre.text.startswith(_OUT_MARKER):
            # batch JRC already wrote the text to cache
            cf = pre.text[len(_OUT_MARKER):]
            kind, method, conf, notes = KIND_INFERRED, pre.method, pre.confidence, pre.notes
            unit, unit_size = cf, _safe_size(cf)
        else:
            # JRC batch failed for this .rpt (or not a .rpt) → normal ladder (ole-envelope/raw/referential)
            res = resolve(full, source_size=size)
            kind, method, conf, notes = res.kind, res.method, res.confidence, res.notes
            if pre is not None and not pre.ok:
                notes = (f"jrc: {pre.notes}; " + notes).strip("; ")
            if kind == KIND_INFERRED and res.text is not None:
                cf = _cache_file(cache, _content_sha(full), res.method)
                if not (cf.exists() and cf.stat().st_size > 0):
                    cf.write_text(res.text, encoding="utf-8")
                    counts["lifted"] += 1
                else:
                    counts["lift_reused"] += 1
                unit, unit_size = str(cf), cf.stat().st_size

        counts[kind] = counts.get(kind, 0) + 1
        rows.append([rel, kind, unit, str(unit_size), str(size), method, f"{conf:.2f}", notes])

    with open(manifest_out, "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        for r in rows:
            w.writerow(r)
    return {"total": len(paths), **counts, "manifest_out": manifest_out}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Ingest helpers: source→text normalization + registry")
    sub = ap.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("resolve", help="resolve one file → kind/method/confidence (JSON)")
    rp.add_argument("path")

    mp = sub.add_parser("materialize", help="resolve a manifest → lifted cache + materialized manifest")
    mp.add_argument("manifest_in")
    mp.add_argument("manifest_out")
    mp.add_argument("--source-root", default="")
    mp.add_argument("--cache-dir", required=True)

    reg = sub.add_parser("registry", help="list helpers + availability")

    args = ap.parse_args()

    if args.cmd == "registry":
        out = []
        for h in registry():
            ok, reason = h.available()
            out.append({"id": h.id, "kind": h.kind, "rank": h.rank,
                        "available": ok, "reason": reason})
        print(json.dumps(out, indent=2))
        return 0

    if args.cmd == "resolve":
        res = resolve(args.path)
        print(json.dumps({"path": res.path, "kind": res.kind, "method": res.method,
                          "confidence": res.confidence, "source_size": res.source_size,
                          "has_text": bool(res.text), "notes": res.notes}, indent=2))
        return 0

    if args.cmd == "materialize":
        rep = materialize(args.manifest_in, args.source_root, args.cache_dir, args.manifest_out)
        print(json.dumps(rep, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
