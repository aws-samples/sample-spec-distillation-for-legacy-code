# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MINE TESTS: every xUnit / NUnit / MSTest test method becomes a "protected behaviour" candidate.

Tests are independent evidence: somebody wrote down, in executable form, a behaviour the system
must keep. Each candidate carries

* the test method's own location,
* the production code it exercises (the system under test), resolved from the calls in the test
  body to their definitions: from the facts call graph when ``--facts`` is given, else by name
  against the structure index, keeping only methods whose declaring type is named in the test,
* a statement built from the test name and its key assertions (fixed template, no AI).

Mock-framework calls (NSubstitute ``Received``/``Returns``, Moq ``Setup``/``Verify``), assertions
and framework methods never count as the system under test.
"""
from __future__ import annotations

import re

from kg_source import Candidate, Index, SourceFile, cap1, clip, humanize, loc

TEST_ATTR = re.compile(r"^\s*[\[<]\s*(?P<a>Fact|Theory|Test|TestCase|TestCaseSource|TestMethod|"
                       r"DataTestMethod)\b(?P<rest>.*)$")
DATA_ATTR = re.compile(r"^\s*[\[<]\s*(InlineData|TestCase|DataRow|MemberData|ClassData)\b")
ASSERT = re.compile(r"\b(Assert\s*\.\s*\w+|\.Should\(\)|Expect\s*\(|Verify\s*\(|\.Received\s*\()")
MOCK_OR_FRAMEWORK = {
    "Received", "DidNotReceive", "Returns", "ReturnsForAnyArgs", "Throws", "Arg", "Any", "Is",
    "Setup", "Verify", "VerifyAll", "It", "For", "Substitute", "Mock", "Object", "Equal", "NotEqual",
    "True", "False", "Null", "NotNull", "Single", "Empty", "NotEmpty", "Contains", "DoesNotContain",
    "IsType", "ThrowsAsync", "Should", "Be", "ToList", "ToArray", "First", "FirstOrDefault",
    "Count", "Where", "Select", "Add", "GetAwaiter", "GetResult", "Result", "ConfigureAwait",
    "ToString", "Equals", "GetType", "Dispose", "Create", "Build", "WithDefaultValues",
    "SendAsync", "GetAsync", "PostAsync", "PutAsync", "DeleteAsync", "ReadAsStringAsync",
    "EnsureSuccessStatusCode", "Content", "FromJson", "ToJson", "Value", "Next", "Then",
}
#: framework types a test instantiates that are never the system under test, even when the
#: code base happens to declare a type (or a Razor component) with the same short name
BCL_TYPES = {
    "List", "Dictionary", "HashSet", "Queue", "Stack", "Exception", "Task", "Guid", "DateTime",
    "TimeSpan", "Uri", "HttpClient", "StringContent", "HttpRequestMessage", "MemoryStream",
    "CancellationTokenSource", "Random", "StringBuilder", "Lazy", "Tuple", "KeyValuePair", "Action",
    "Func", "Regex", "Results", "Object", "String", "Array", "Index", "Error", "Privacy",
}


def _test_methods(f: SourceFile, index: Index) -> list[dict]:
    out = []
    n = len(f.lines)
    i = 0
    while i < n:
        m = TEST_ATTR.match(f.code[i])
        if not m:
            i += 1
            continue
        attr_line = i + 1
        data_rows = 0
        j = i + 1
        while j < n and (f.code[j].strip().startswith(("[", "<")) or not f.code[j].strip()):
            if DATA_ATTR.match(f.code[j]):
                data_rows += 1
            j += 1
        if j >= n:
            break
        meth = index.method_at(f.rel, j + 1)
        if meth is None or meth.start != j + 1:
            hm = re.search(r"(\w+)\s*\(", f.code[j])
            if not hm:
                i = j
                continue
            name, start, end = hm.group(1), j + 1, j + 1
            depth, opened = 0, False
            for k in range(j, min(n, j + 400)):
                for ch in f.code[k]:
                    if ch == "{":
                        depth, opened = depth + 1, True
                    elif ch == "}":
                        depth -= 1
                if opened and depth <= 0:
                    end = k + 1
                    break
            cls = index.where(f.rel, j + 1)[0]
        else:
            name, start, end, cls = meth.name, meth.start, meth.end, meth.cls
        out.append({"name": name, "cls": cls, "attr": m.group("a"), "attr_line": attr_line,
                    "start": start, "end": end, "data_rows": data_rows})
        i = end
    return out


def _sut(f: SourceFile, t: dict, index: Index, facts_calls: dict, files: dict) -> list[tuple[str, int, int, str]]:
    """(file, start, end, 'Class.Method') of production code the test exercises."""
    body = "\n".join(f.code[t["start"] - 1:t["end"]])
    whole = f.code_text
    out: dict[str, tuple[str, int, int, str]] = {}
    for c in facts_calls.get(f"{t['cls']}.{t['name']}", []):
        callee_file = c.get("callee_file")
        if c["to"].split(".")[-1] in MOCK_OR_FRAMEWORK | BCL_TYPES:
            continue
        if callee_file and callee_file in files and not files[callee_file].is_test:
            for m in index.methods_by_name.get(c["to"].split(".")[-1], []):
                if m.file == callee_file and m.id == c["to"]:
                    out[m.id] = (m.file, m.start, m.end, m.id)
    if not out:
        created = set(re.findall(r"\bnew\s+([A-Z]\w*)\s*(?:<[^()]*>)?\s*\(", body))
        created |= set(re.findall(r"\bnew\s+([A-Z]\w*)\s*(?:<[^()]*>)?\s*\(", whole))
        named = set(re.findall(r"\b([A-Z]\w+)\b", whole)) - BCL_TYPES
        created -= BCL_TYPES
        called = re.findall(r"(?:\.|\bawait\s+|^|\s)([A-Z]\w*)\s*(?:<[^()]*>)?\s*\(", body, re.M)
        for name in called:
            if name in MOCK_OR_FRAMEWORK or name in BCL_TYPES:
                continue
            for m in index.methods_by_name.get(name, []):
                if files.get(m.file) is None or files[m.file].is_test:
                    continue
                if m.cls in created or m.cls in named:
                    out.setdefault(m.id, (m.file, m.start, m.end, m.id))
        for cname in sorted(created):
            for ty in index.types_by_name.get(cname, []):
                if files.get(ty.file) and not files[ty.file].is_test and ty.kind != "interface":
                    ctor = next((m for m in index.methods.get(ty.file, [])
                                 if m.cls == cname and m.kind == "Constructor"), None)
                    if ctor and not any(v[3].startswith(cname + ".") for v in out.values()):
                        out.setdefault(ctor.id, (ctor.file, ctor.start, ctor.end, ctor.id))
    # rank: methods of types instantiated in the test first, then by name
    return sorted(out.values(), key=lambda v: (v[3].split(".")[0] not in body, v[3]))[:3]


def _callees(mid: tuple[str, int, int, str], index: Index, files: dict, facts_calls: dict) -> list[tuple]:
    """Production methods called from one production method (one hop)."""
    rel, a, b, ident = mid
    out = {}
    for c in facts_calls.get(ident, []):
        cf = c.get("callee_file")
        if c["to"].split(".")[-1] in MOCK_OR_FRAMEWORK | BCL_TYPES:
            continue
        for m in index.methods_by_name.get(c["to"].split(".")[-1], []):
            if m.file == cf and m.id == c["to"]:
                out[m.id] = (m.file, m.start, m.end, m.id)
    if facts_calls:
        return list(out.values())
    f = files.get(rel)
    if f is None:
        return []
    body = "\n".join(f.code[a - 1:b])
    own = ident.split(".")[0]
    created = set(re.findall(r"\bnew\s+([A-Z]\w*)\s*(?:<[^()]*>)?\s*\(", body))
    named = (set(re.findall(r"\b([A-Z]\w+)\b", body)) | {own}) - BCL_TYPES
    created -= BCL_TYPES
    for name in re.findall(r"(?:\.|^|\s)([A-Z]\w*)\s*(?:<[^()]*>)?\s*\(", body, re.M):
        if name in MOCK_OR_FRAMEWORK or name in BCL_TYPES:
            continue
        for m in index.methods_by_name.get(name, []):
            if not files.get(m.file) or files[m.file].is_test:
                continue
            # a call on a field/local whose type is declared in this class's file counts too
            if m.cls in named or m.cls in created or (m.cls and m.cls in files[rel].code_text):
                out.setdefault(m.id, (m.file, m.start, m.end, m.id))
    for cname in created:
        for m in index.methods_by_name.get(cname, []):
            if m.kind == "Constructor" and files.get(m.file) and not files[m.file].is_test:
                out.setdefault(m.id, (m.file, m.start, m.end, m.id))
    return list(out.values())


def reach(sut: list[tuple], index: Index, files: dict, facts_calls: dict, depth: int = 2) -> list[dict]:
    """Locations reachable from the system under test within ``depth`` hops (for matching only)."""
    seen = {s[3]: s for s in sut}
    frontier = list(sut)
    for _ in range(depth):
        nxt = []
        for s in frontier:
            for c in _callees(s, index, files, facts_calls):
                if c[3] not in seen:
                    seen[c[3]] = c
                    nxt.append(c)
        frontier = nxt
    return [loc(a, b, c) for a, b, c, _ in sorted(seen.values())]


def _properties_read(f: SourceFile, t: dict, index: Index, files: dict) -> list[dict]:
    """Declarations of properties the test reads (``basket.TotalItems``) on types it names."""
    body = "\n".join(f.code[t["start"] - 1:t["end"]])
    names = set(re.findall(r"\.([A-Z]\w*)\b(?!\s*[(<])", body)) - MOCK_OR_FRAMEWORK
    types = (set(re.findall(r"\b([A-Z]\w+)\b", f.code_text)) - BCL_TYPES)
    out = []
    for ty in sorted(types):
        for decl in index.types_by_name.get(ty, []):
            pf = files.get(decl.file)
            if pf is None or pf.is_test:
                continue
            for k in range(decl.start, decl.end + 1):
                pm = re.search(r"\b([A-Z]\w*)\s*(?:=>|\{\s*get)", pf.code[k - 1])
                if pm and pm.group(1) in names:
                    out.append(loc(decl.file, k))
    return out


def _assertions(f: SourceFile, t: dict) -> list[str]:
    out = []
    for k in range(t["start"], t["end"] + 1):
        if ASSERT.search(f.code[k - 1]):
            out.append(clip(f.lines[k - 1].strip(), 110))
    return out


def _kind_of(rel: str) -> str:
    low = rel.lower()
    if "functional" in low or "publicapiintegration" in low or "endpoint" in low:
        return "interface"
    if "integration" in low:
        return "workflow"
    return "rule"


def mine_tests(files: dict[str, SourceFile], index: Index, facts: dict | None = None) -> list[Candidate]:
    facts_calls: dict[str, list[dict]] = {}
    if facts:
        for sec in ("cs", "vb"):
            for c in (facts.get(sec) or {}).get("calls") or []:
                if c.get("from"):
                    facts_calls.setdefault(c["from"], []).append(c)
    out = []
    for rel, f in sorted(files.items()):
        if not f.is_test or f.lang not in ("cs", "vb"):
            continue
        for t in _test_methods(f, index):
            sut = _sut(f, t, index, facts_calls, files)
            asserts = _assertions(f, t)
            subject = humanize(t["cls"])
            behaviour = humanize(re.sub(r"Async$", "", t["name"]))
            behaviour = re.sub(r"\bcant\b", "can't", re.sub(r"\bdoesnt\b", "doesn't", behaviour))
            stmt = f"Protected behaviour ({subject}): {behaviour}"
            if t["data_rows"]:
                stmt += f" ({t['data_rows']} data rows)"
            if asserts:
                stmt += f"; asserts `{clip(asserts[0], 100)}`"
                if len(asserts) > 1:
                    stmt += f" and {len(asserts) - 1} more"
            if sut:
                stmt += f"; exercises {', '.join(s[3] for s in sut)}"
            stmt += "."
            locs = [loc(rel, t["attr_line"], t["end"])] + [loc(a, b, c) for a, b, c, _ in sut]
            keys = [f"member:{w}" for w in behaviour.split() if len(w) > 3][:6]
            out.append(Candidate(_kind_of(rel), stmt, locs, "tests", "test_method",
                                 keys=keys, extra={"snippet": clip(f.lines[t["start"] - 1]),
                                                   "test": f"{t['cls']}.{t['name']}",
                                                   "framework_attr": t["attr"],
                                                   "sut": [s[3] for s in sut],
                                                   "direct": [loc(a, b, c) for a, b, c, _ in sut],
                                                   "subject": subject.split(),
                                                   "behaviour": sorted(set(behaviour.replace("'", "").split())
                                                                       - set(subject.split())),
                                                   "reach": locs + reach(sut, index, files, facts_calls)
                                                   + _properties_read(f, t, index, files),
                                                   "assertions": asserts[:5]}))
    return out
