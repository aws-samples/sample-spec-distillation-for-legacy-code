// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
//
// Lean pipeline, layer 1: deterministic structure extraction (no LLM).
//
// Parses every *.vb file with Roslyn (Microsoft.CodeAnalysis.VisualBasic, syntax only), every *.cs
// file with Roslyn (Microsoft.CodeAnalysis.CSharp, syntax only) and every *.sql file (plus SQL text
// embedded in VB/C# string literals) with ScriptDom (Microsoft.SqlServer.TransactSql.ScriptDom,
// TSql160Parser). Writes one JSON document (facts.json) to stdout. Every fact carries "file" plus
// "start"/"end" (1-based, inclusive) or "line".
//
// VB.NET and C# produce the SAME fact schema, under "vb" and "cs" respectively: types, methods,
// members, constants, calls, proc_refs, inline_sql, literals, const_uses, comparisons,
// fingerprints, column_refs, unit_literals, local_aliases, attributes, enums, io_calls. Comments
// from both land in the shared top-level "comments" list, tagged by "language". facts_post.py
// merges the two sections into one language-neutral view, so duplicate-logic and cross-layer
// comparison candidates span VB, C# and T-SQL without any per-language special casing.
//
// Usage: dotnet LeanStructure.dll <source-root>   (normally run via the Docker image, see Dockerfile)
//
// NOTE on namespaces: the VB syntax namespace is imported unqualified (the VB rig came first), so
// every C# syntax type is reached through the `CS` alias and every C# SyntaxKind through `CSK`.
// `node.Kind()` is an extension method that exists in BOTH languages' namespaces, so the C# code
// below compares `RawKind` via IsK() instead of importing Microsoft.CodeAnalysis.CSharp.

using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.VisualBasic;
using Microsoft.CodeAnalysis.VisualBasic.Syntax;
using Microsoft.SqlServer.TransactSql.ScriptDom;
using Fact = System.Collections.Generic.Dictionary<string, object>;
using CS = Microsoft.CodeAnalysis.CSharp.Syntax;
using CSK = Microsoft.CodeAnalysis.CSharp.SyntaxKind;

static class Program
{
    // ------------------------------------------------------------------ shared state
    static string Root;
    static readonly Dictionary<string, string[]> FileLines = new();
    static readonly List<Fact> FilesOut = new();

    // VB
    static readonly List<Fact> VbTypes = new();
    static readonly List<Fact> VbMethods = new();
    static readonly List<Fact> VbMembers = new();      // fields + properties with declared types
    static readonly List<Fact> VbConstants = new();
    static readonly List<Fact> VbCalls = new();
    static readonly List<Fact> VbProcRefs = new();
    static readonly List<Fact> VbInlineSql = new();
    static readonly List<Fact> VbLiterals = new();
    static readonly List<Fact> VbConstUses = new();
    static readonly List<Fact> Comments = new();       // VB + SQL comment blocks
    static readonly List<Fact> VbComparisons = new();  // every comparison / Case clause, operands split
    static readonly List<Fact> VbFingerprints = new(); // normalized expression/statement fingerprints
    static readonly List<Fact> VbColumnRefs = new();   // string literals naming a DB column/param (row("Col"), AddWithValue("@p"))
    static readonly List<Fact> VbUnitLiterals = new(); // every literal per method (for literal clusters)
    static readonly List<Fact> VbLocalAliases = new(); // Dim x = f("Col", 5000D) -> x ~ Col (default 5000)
    static readonly List<Fact> VbAttributes = new();
    static readonly List<Fact> VbEnums = new();
    static readonly List<Fact> VbIoCalls = new();      // HTTP / serialization / file / mail / config / process APIs
    static readonly HashSet<string> KnownColumns = new(StringComparer.OrdinalIgnoreCase);

    // C# — the SAME fact schema as VB, so facts_post.py can merge the two without special cases.
    static readonly List<Fact> CsTypes = new();
    static readonly List<Fact> CsMethods = new();
    static readonly List<Fact> CsMembers = new();
    static readonly List<Fact> CsConstants = new();
    static readonly List<Fact> CsCalls = new();
    static readonly List<Fact> CsProcRefs = new();
    static readonly List<Fact> CsInlineSql = new();
    static readonly List<Fact> CsLiterals = new();
    static readonly List<Fact> CsConstUses = new();
    static readonly List<Fact> CsComparisons = new();
    static readonly List<Fact> CsFingerprints = new();
    static readonly List<Fact> CsColumnRefs = new();
    static readonly List<Fact> CsUnitLiterals = new();
    static readonly List<Fact> CsLocalAliases = new();
    static readonly List<Fact> CsAttributes = new();
    static readonly List<Fact> CsEnums = new();
    static readonly List<Fact> CsIoCalls = new();

    // SQL
    static readonly List<Fact> SqlProcs = new();
    static readonly List<Fact> SqlTriggers = new();
    static readonly List<Fact> SqlTables = new();
    static readonly List<Fact> SqlSeedRows = new();
    static readonly List<Fact> SqlScripts = new();
    static readonly List<Fact> SqlParseErrors = new();
    static readonly List<Fact> SqlNumericLiterals = new();

    static readonly HashSet<string> KnownProcs = new(StringComparer.OrdinalIgnoreCase);

    static int Main(string[] args)
    {
        Root = Path.GetFullPath(args.Length > 0 ? args[0] : ".");
        var all = Directory.GetFiles(Root, "*", SearchOption.AllDirectories)
            .Select(p => Path.GetRelativePath(Root, p).Replace('\\', '/'))
            .Where(r => !r.Split('/').Any(s => s.StartsWith(".")))
            .OrderBy(r => r, StringComparer.Ordinal).ToList();
        foreach (var rel in all)
        {
            var text = File.ReadAllText(Path.Combine(Root, rel));
            var lines = text.Replace("\r\n", "\n").Split('\n');
            if (lines.Length > 0 && lines[^1] == "") lines = lines[..^1];
            FileLines[rel] = lines;
            var ext = Path.GetExtension(rel).ToLowerInvariant();
            string lang = ext switch { ".vb" => "vb.net", ".cs" => "c#", ".sql" => "t-sql", _ => "other" };
            FilesOut.Add(new Fact { ["file"] = rel, ["language"] = lang, ["start"] = 1, ["end"] = lines.Length });
        }

        // SQL first so VB string literals can be matched against known procs.
        foreach (var rel in all.Where(r => r.EndsWith(".sql", StringComparison.OrdinalIgnoreCase)))
            AnalyzeSqlFile(rel);
        foreach (var t in SqlTables) foreach (var c in (List<Fact>)t["columns"]) KnownColumns.Add((string)c["name"]);
        foreach (var p in SqlProcs) foreach (var prm in (List<Fact>)p["params"]) KnownColumns.Add(((string)prm["name"]).TrimStart('@'));
        var vbFiles = all.Where(r => r.EndsWith(".vb", StringComparison.OrdinalIgnoreCase)).ToList();
        var trees = vbFiles.ToDictionary(r => r, r => VisualBasicSyntaxTree.ParseText(
            File.ReadAllText(Path.Combine(Root, r)), path: r));
        foreach (var (rel, tree) in trees) VbDeclarations(rel, tree);
        foreach (var (rel, tree) in trees) VbBodies(rel, tree);
        foreach (var (rel, tree) in trees) VbV1(rel, tree);

        // C#: the same three passes (declare everything first so calls can resolve across files).
        var csFiles = all.Where(r => r.EndsWith(".cs", StringComparison.OrdinalIgnoreCase)).ToList();
        var csTrees = csFiles.ToDictionary(r => r, r => Microsoft.CodeAnalysis.CSharp.CSharpSyntaxTree.ParseText(
            File.ReadAllText(Path.Combine(Root, r)), path: r));
        // Syntax errors per file (Roslyn error-recovers, so a broken file still yields facts).
        // Consumers such as eval/mutate use these to prove an edit kept a file parseable.
        var parseDiagnostics = new List<Fact>();
        foreach (var (rel, tree) in trees.Select(kv => (kv.Key, (SyntaxTree)kv.Value))
                     .Concat(csTrees.Select(kv => (kv.Key, (SyntaxTree)kv.Value))))
            foreach (var dg in tree.GetDiagnostics().Where(x => x.Severity == DiagnosticSeverity.Error))
                parseDiagnostics.Add(new Fact
                {
                    ["file"] = rel, ["line"] = dg.Location.GetLineSpan().StartLinePosition.Line + 1,
                    ["id"] = dg.Id, ["message"] = dg.GetMessage(CultureInfo.InvariantCulture),
                });
        foreach (var (rel, tree) in csTrees) CsDeclarations(rel, tree);
        foreach (var (rel, tree) in csTrees) CsBodies(rel, tree);
        foreach (var (rel, tree) in csTrees) CsV1(rel, tree);

        var derived = Derive();
        var doc = new Fact
        {
            ["schema"] = "lean-facts/v1",
            ["tools"] = new Fact { ["vb"] = "Microsoft.CodeAnalysis.VisualBasic 4.8.0 (syntax only)",
                                   ["cs"] = "Microsoft.CodeAnalysis.CSharp 4.8.0 (syntax only)",
                                   ["sql"] = "Microsoft.SqlServer.TransactSql.ScriptDom 161 (TSql160Parser)" },
            ["files"] = FilesOut,
            ["vb"] = new Fact
            {
                ["types"] = VbTypes, ["methods"] = VbMethods, ["members"] = VbMembers,
                ["constants"] = VbConstants, ["calls"] = VbCalls, ["proc_refs"] = VbProcRefs,
                ["inline_sql"] = VbInlineSql, ["literals"] = VbLiterals, ["const_uses"] = VbConstUses,
                ["comparisons"] = VbComparisons, ["fingerprints"] = VbFingerprints, ["column_refs"] = VbColumnRefs,
                ["unit_literals"] = VbUnitLiterals, ["local_aliases"] = VbLocalAliases, ["attributes"] = VbAttributes,
                ["enums"] = VbEnums, ["io_calls"] = VbIoCalls,
            },
            ["cs"] = new Fact
            {
                ["types"] = CsTypes, ["methods"] = CsMethods, ["members"] = CsMembers,
                ["constants"] = CsConstants, ["calls"] = CsCalls, ["proc_refs"] = CsProcRefs,
                ["inline_sql"] = CsInlineSql, ["literals"] = CsLiterals, ["const_uses"] = CsConstUses,
                ["comparisons"] = CsComparisons, ["fingerprints"] = CsFingerprints, ["column_refs"] = CsColumnRefs,
                ["unit_literals"] = CsUnitLiterals, ["local_aliases"] = CsLocalAliases, ["attributes"] = CsAttributes,
                ["enums"] = CsEnums, ["io_calls"] = CsIoCalls,
            },
            ["sql"] = new Fact
            {
                ["procs"] = SqlProcs, ["triggers"] = SqlTriggers, ["tables"] = SqlTables,
                ["seed_rows"] = SqlSeedRows, ["scripts"] = SqlScripts, ["numeric_literals"] = SqlNumericLiterals,
                ["parse_errors"] = SqlParseErrors,
            },
            ["comments"] = Comments,
            ["derived"] = derived,
            ["parse_diagnostics"] = parseDiagnostics,
        };
        var opts = new JsonSerializerOptions { WriteIndented = true,
            Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping };
        Console.Out.Write(JsonSerializer.Serialize(doc, opts));
        Console.Error.WriteLine($"files={FilesOut.Count} vb_methods={VbMethods.Count} vb_calls={VbCalls.Count} " +
                                $"cs_methods={CsMethods.Count} cs_calls={CsCalls.Count} " +
                                $"procs={SqlProcs.Count} triggers={SqlTriggers.Count} tables={SqlTables.Count}");
        return 0;
    }

    static string Clip(string s, int n = 200)
    {
        s = Regex.Replace(s ?? "", @"\s+", " ").Trim();
        return s.Length <= n ? s : s[..n] + "...";
    }

    static string LineText(string file, int line) =>
        FileLines.TryGetValue(file, out var ls) && line >= 1 && line <= ls.Length ? Clip(ls[line - 1], 220) : "";

    static string NormName(string s) => Regex.Replace(s ?? "", @"[\[\]_@]", "").ToLowerInvariant();

    // ================================================================== SQL
    static readonly string[] PseudoTables = { "inserted", "deleted" };

    static string ObjName(SchemaObjectName n) => n?.BaseIdentifier?.Value;

    static string FragText(TSqlFragment f)
    {
        if (f == null || f.FirstTokenIndex < 0) return "";
        var sb = new StringBuilder();
        for (int i = f.FirstTokenIndex; i <= f.LastTokenIndex; i++) sb.Append(f.ScriptTokenStream[i].Text);
        return Clip(sb.ToString(), 240);
    }

    static int EndLine(TSqlFragment f)
    {
        var t = f.ScriptTokenStream[f.LastTokenIndex];
        return t.Line + t.Text.Count(c => c == '\n');
    }

    /// <summary>Collects reads/writes/exec/raise/return/comparisons/literal SETs inside one fragment.</summary>
    sealed class SqlCollector : TSqlFragmentVisitor
    {
        public readonly int LineOffset;   // added to ScriptDom line numbers (0 for .sql files)
        public readonly bool MapToFixedLine; public readonly int FixedStart, FixedEnd;
        readonly HashSet<int> _writeOffsets = new();
        public readonly List<Fact> Reads = new(), Writes = new(), Execs = new(), Raises = new(),
            Returns = new(), Comparisons = new(), LiteralSets = new(), MsgAssigns = new(), Numerics = new(),
            Strings = new(), ColRefs = new(), VarCols = new(), ResultSets = new(), Fingerprints = new();
        public readonly HashSet<string> Pseudo = new(StringComparer.OrdinalIgnoreCase);
        // alias / table references seen anywhere in the unit: (alias-or-name, table, offset)
        readonly List<(string alias, string table, int offset)> _tableRefs = new();
        readonly List<(string qual, string col, int offset, int line, string mode, string table)> _cols = new();

        public SqlCollector(int lineOffset = 0) { LineOffset = lineOffset; }
        public SqlCollector(int fixedStart, int fixedEnd) { MapToFixedLine = true; FixedStart = fixedStart; FixedEnd = fixedEnd; }

        int L(TSqlFragment f) => MapToFixedLine ? FixedStart : f.StartLine + LineOffset;

        static List<NamedTableReference> NamedRefs(TSqlFragment f)
        {
            var c = new NtrCollector(); f?.Accept(c); return c.Refs;
        }
        sealed class NtrCollector : TSqlFragmentVisitor
        {
            public readonly List<NamedTableReference> Refs = new();
            public override void Visit(NamedTableReference n) => Refs.Add(n);
        }

        void Write(TableReference target, FromClause from, string op, TSqlFragment stmt)
        {
            if (target is not NamedTableReference nt) return;
            var name = ObjName(nt.SchemaObject);
            _writeOffsets.Add(nt.StartOffset);
            if (from != null && nt.SchemaObject.SchemaIdentifier == null)
            {
                foreach (var r in NamedRefs(from))
                    if (string.Equals(r.Alias?.Value, name, StringComparison.OrdinalIgnoreCase) ||
                        string.Equals(ObjName(r.SchemaObject), name, StringComparison.OrdinalIgnoreCase))
                    {
                        name = ObjName(r.SchemaObject); _writeOffsets.Add(r.StartOffset); break;
                    }
            }
            if (PseudoTables.Contains(name, StringComparer.OrdinalIgnoreCase)) { Pseudo.Add(name); return; }
            Writes.Add(new Fact { ["table"] = name, ["op"] = op, ["line"] = L(stmt) });
            LastWriteTable = name;
        }
        string LastWriteTable;

        public override void Visit(UpdateSpecification u)
        {
            LastWriteTable = null;
            Write(u.Target, u.FromClause, "UPDATE", u);
            Fp(u, "update");
            foreach (var sc in u.SetClauses.OfType<AssignmentSetClause>())
            {
                var col = sc.Column?.MultiPartIdentifier?.Identifiers.LastOrDefault()?.Value;
                if (col != null && LastWriteTable != null)
                    _cols.Add((null, col, sc.Column.StartOffset, L(sc), "write", LastWriteTable));
                if (col != null && sc.NewValue is Literal lit)
                    LiteralSets.Add(new Fact { ["column"] = col, ["value"] = lit.Value, ["line"] = L(sc), ["text"] = FragText(sc),
                                               ["table"] = LastWriteTable });
            }
        }
        public override void Visit(InsertSpecification i)
        {
            LastWriteTable = null;
            Write(i.Target, null, "INSERT", i);
            Fp(i, "insert");
            if (LastWriteTable != null)
                foreach (var c in i.Columns)
                {
                    var col = c.MultiPartIdentifier?.Identifiers.LastOrDefault()?.Value;
                    if (col != null) _cols.Add((null, col, c.StartOffset, L(c), "write", LastWriteTable));
                }
        }
        public override void Visit(DeleteSpecification d) => Write(d.Target, d.FromClause, "DELETE", d);
        public override void Visit(MergeSpecification m) => Write(m.Target, null, "MERGE", m);
        public override void Visit(SelectStatement s)
        {
            if (s.Into != null)
                Writes.Add(new Fact { ["table"] = ObjName(s.Into), ["op"] = "SELECT INTO", ["line"] = L(s) });
            // a top-level SELECT that assigns no variables returns a result set to the caller
            if (s.Into == null && s.QueryExpression is QuerySpecification qs &&
                qs.SelectElements.Count > 0 && !qs.SelectElements.Any(e => e is SelectSetVariable))
                ResultSets.Add(new Fact { ["line"] = L(s), ["columns"] = qs.SelectElements.Select(e => FragText(e)).ToList(),
                                          ["from"] = qs.FromClause == null ? new List<string>() : NamedRefs(qs.FromClause).Select(r => ObjName(r.SchemaObject)).Distinct().ToList() });
        }

        public override void Visit(IfStatement i) { Fp(i.Predicate, "if"); }
        public override void Visit(WhileStatement w) { Fp(w.Predicate, "while"); }
        public override void Visit(WhereClause w) { if (w.SearchCondition != null) Fp(w.SearchCondition, "where"); }
        public override void Visit(ExistsPredicate e) { Fp(e, "exists"); }
        public override void Visit(SearchedCaseExpression c) { Fp(c, "case"); }
        public override void Visit(SimpleCaseExpression c) { Fp(c, "case"); }

        public override void Visit(InPredicate p)
        {
            if (p.Subquery != null) return;
            var vals = p.Values.Select(v => v is Literal l ? l.Value : FragText(v)).ToList();
            Comparisons.Add(new Fact { ["line"] = L(p), ["op"] = p.NotDefined ? "NotIn" : "In", ["text"] = FragText(p),
                                       ["has_literal"] = p.Values.Any(v => v is Literal), ["left"] = FragText(p.Expression),
                                       ["right"] = "(" + string.Join(", ", vals) + ")", ["values"] = vals });
            Fp(p, "in");
        }

        public override void Visit(StringLiteral l) =>
            Strings.Add(new Fact { ["line"] = L(l), ["value"] = l.Value });

        public override void Visit(ColumnReferenceExpression c)
        {
            var ids = c.MultiPartIdentifier?.Identifiers;
            if (ids == null || ids.Count == 0) return;
            var col = ids[^1].Value; var qual = ids.Count >= 2 ? ids[^2].Value : null;
            _cols.Add((qual, col, c.StartOffset, L(c), "read", null));
        }

        public override void Visit(SelectSetVariable s)
        {
            if (s.Expression is Literal lit)
                MsgAssigns.Add(new Fact { ["line"] = L(s), ["variable"] = s.Variable.Name, ["value"] = lit.Value });
            else if (s.Expression is ColumnReferenceExpression cr)
                VarCols.Add(new Fact { ["line"] = L(s), ["variable"] = s.Variable.Name, ["column"] = cr.MultiPartIdentifier.Identifiers[^1].Value });
            else
                VarCols.Add(new Fact { ["line"] = L(s), ["variable"] = s.Variable.Name, ["expr"] = FragText(s.Expression) });
        }

        // token fingerprints: k1 keeps variable names, k2 abstracts them; aliases/schemas dropped
        void Fp(TSqlFragment f, string kind)
        {
            if (f == null || f.FirstTokenIndex < 0) return;
            var t1 = new List<string>(); var t2 = new List<string>();
            var toks = f.ScriptTokenStream;
            for (int i = f.FirstTokenIndex; i <= f.LastTokenIndex; i++)
            {
                var t = toks[i];
                if (t.TokenType is TSqlTokenType.WhiteSpace or TSqlTokenType.SingleLineComment or TSqlTokenType.MultilineComment) continue;
                if (t.TokenType is TSqlTokenType.Identifier or TSqlTokenType.QuotedIdentifier)
                {
                    int j = i + 1; while (j <= f.LastTokenIndex && toks[j].TokenType == TSqlTokenType.WhiteSpace) j++;
                    if (j <= f.LastTokenIndex && toks[j].TokenType == TSqlTokenType.Dot) { i = j; continue; } // alias./schema.
                    var n = t.Text.Trim('[', ']').ToLowerInvariant(); t1.Add(n); t2.Add(n); continue;
                }
                if (t.TokenType == TSqlTokenType.Variable) { t1.Add(t.Text.TrimStart('@').ToLowerInvariant()); t2.Add("@"); continue; }
                var x = t.TokenType is TSqlTokenType.AsciiStringLiteral or TSqlTokenType.UnicodeStringLiteral ? t.Text : t.Text.ToUpperInvariant();
                t1.Add(x); t2.Add(x);
            }
            if (t1.Count < 5) return;
            Fingerprints.Add(new Fact { ["kind"] = kind, ["line"] = L(f), ["end"] = MapToFixedLine ? FixedEnd : EndLine(f) + LineOffset,
                                        ["k1"] = string.Join(" ", t1), ["k2"] = string.Join(" ", t2), ["ntok"] = t1.Count,
                                        ["text"] = FragText(f) });
        }

        public override void Visit(NamedTableReference n)
        {
            _tableRefs.Add((n.Alias?.Value ?? ObjName(n.SchemaObject), ObjName(n.SchemaObject), n.StartOffset));
            if (n.Alias != null) _tableRefs.Add((ObjName(n.SchemaObject), ObjName(n.SchemaObject), n.StartOffset));
            if (_writeOffsets.Contains(n.StartOffset)) return;
            var name = ObjName(n.SchemaObject);
            if (PseudoTables.Contains(name, StringComparer.OrdinalIgnoreCase)) { Pseudo.Add(name); return; }
            Reads.Add(new Fact { ["table"] = name, ["line"] = L(n) });
        }

        public override void Visit(ExecuteSpecification e)
        {
            if (e.ExecutableEntity is ExecutableProcedureReference p)
            {
                var name = ObjName(p.ProcedureReference?.ProcedureReference?.Name);
                if (name != null) Execs.Add(new Fact { ["proc"] = name, ["line"] = L(e), ["text"] = FragText(e) });
            }
        }

        public override void Visit(RaiseErrorStatement r) =>
            Raises.Add(new Fact { ["line"] = L(r), ["message"] = FragText(r.FirstParameter), ["kind"] = "RAISERROR" });
        public override void Visit(ThrowStatement t) =>
            Raises.Add(new Fact { ["line"] = L(t), ["message"] = FragText(t.Message), ["kind"] = "THROW" });
        public override void Visit(ReturnStatement r)
        {
            if (r.Expression != null) Returns.Add(new Fact { ["line"] = L(r), ["value"] = FragText(r.Expression) });
        }

        public override void Visit(BooleanComparisonExpression b)
        {
            bool lit = IsLit(b.FirstExpression) || IsLit(b.SecondExpression);
            Comparisons.Add(new Fact { ["line"] = L(b), ["op"] = b.ComparisonType.ToString(),
                                       ["text"] = FragText(b), ["has_literal"] = lit,
                                       ["left"] = FragText(b.FirstExpression), ["right"] = FragText(b.SecondExpression) });
            if (lit) Fp(b, "comparison");
        }
        static bool IsLit(ScalarExpression e) => e is Literal ||
            (e is UnaryExpression u && u.Expression is Literal) || (e is ParenthesisExpression p && IsLit(p.Expression));

        public override void Visit(SetVariableStatement s)
        {
            if (s.Expression is Literal lit)
                MsgAssigns.Add(new Fact { ["line"] = L(s), ["variable"] = s.Variable.Name, ["value"] = lit.Value });
            else if (s.Expression is ScalarSubquery sq && sq.QueryExpression is QuerySpecification q && q.SelectElements.FirstOrDefault() is SelectScalarExpression se
                     && se.Expression is ColumnReferenceExpression cr)
                VarCols.Add(new Fact { ["line"] = L(s), ["variable"] = s.Variable.Name, ["column"] = cr.MultiPartIdentifier.Identifiers[^1].Value });
        }

        public override void Visit(IntegerLiteral l) => Num(l);
        public override void Visit(NumericLiteral l) => Num(l);
        public override void Visit(MoneyLiteral l) => Num(l);
        void Num(Literal l) => Numerics.Add(new Fact { ["line"] = L(l), ["value"] = l.Value });

        public Fact Summary(bool includeNumerics = false)
        {
            static List<Fact> Dedup(List<Fact> xs, params string[] keys) =>
                xs.GroupBy(x => string.Join("|", keys.Select(k => x[k]?.ToString())))
                  .Select(g => { var f = new Fact(g.First()); f["lines"] = g.Select(x => (int)x["line"]).Distinct().OrderBy(i => i).ToList(); f.Remove("line"); return f; })
                  .ToList();
            var s = new Fact
            {
                ["reads"] = Dedup(Reads, "table"),
                ["writes"] = Dedup(Writes, "table", "op"),
                ["exec_calls"] = Execs,
                ["raises"] = Raises,
                ["returns"] = Returns,
                ["comparisons"] = Comparisons,
                ["literal_sets"] = LiteralSets,
                ["literal_assigns"] = MsgAssigns,
            };
            if (Pseudo.Count > 0) s["pseudo_tables"] = Pseudo.OrderBy(x => x).ToList();
            if (includeNumerics) s["numeric_literals"] = Numerics;
            s["string_literals"] = Dedup(Strings, "value");
            s["var_from_column"] = VarCols;
            s["result_sets"] = ResultSets;
            s["fingerprints"] = Fingerprints;
            // resolve column qualifiers to tables (nearest alias declaration); unqualified -> table null
            var cols = new List<Fact>();
            var writeOffs = _cols.Where(c => c.mode == "write").Select(c => c.offset).ToHashSet();
            foreach (var c in _cols)
            {
                if (c.mode == "read" && writeOffs.Contains(c.offset)) continue;
                var table = c.table;
                if (table == null && c.qual != null)
                {
                    var cand = _tableRefs.Where(r => string.Equals(r.alias, c.qual, StringComparison.OrdinalIgnoreCase))
                                         .OrderBy(r => Math.Abs(r.offset - c.offset)).FirstOrDefault();
                    table = cand.table ?? (PseudoTables.Contains(c.qual, StringComparer.OrdinalIgnoreCase) ? c.qual : null);
                }
                if (table == null && c.qual == null)
                {
                    var distinct = _tableRefs.Select(r => r.table).Where(t => t != null).Distinct(StringComparer.OrdinalIgnoreCase).ToList();
                    if (distinct.Count == 1) table = distinct[0];
                }
                if (c.col.StartsWith("@")) continue;
                cols.Add(new Fact { ["table"] = table, ["column"] = c.col, ["mode"] = c.mode, ["line"] = c.line,
                                    ["qualified"] = c.qual != null,
                                    ["candidates"] = table != null ? null : _tableRefs.Select(r => r.table).Where(t => t != null).Distinct(StringComparer.OrdinalIgnoreCase).ToList() });
            }
            s["columns"] = cols.GroupBy(x => $"{x["table"]}|{x["column"]}|{x["mode"]}")
                .Select(g => { var f = new Fact(g.First()); f["lines"] = g.Select(x => (int)x["line"]).Distinct().OrderBy(i => i).ToList(); f.Remove("line"); return f; })
                .ToList();
            return s;
        }
    }

    static (TSqlFragment frag, IList<ParseError> errs) ParseSql(string text)
    {
        var parser = new TSql160Parser(true);
        using var rdr = new StringReader(text);
        var frag = parser.Parse(rdr, out var errs);
        return (frag, errs);
    }

    static void AnalyzeSqlFile(string rel)
    {
        var raw = File.ReadAllText(Path.Combine(Root, rel));
        // Crystal Reports command parameters ({?Name}) are not T-SQL; swap for variables (same line).
        var crystal = Regex.Matches(raw, @"\{\?(\w+)\}").Select(m => m.Groups[1].Value).Distinct().ToList();
        var text = Regex.Replace(raw, @"\{\?(\w+)\}", "@crystal_$1");
        var (frag, errs) = ParseSql(text);
        foreach (var e in errs)
            SqlParseErrors.Add(new Fact { ["file"] = rel, ["line"] = e.Line, ["message"] = e.Message });

        // comments
        if (frag?.ScriptTokenStream != null)
        {
            var cmts = frag.ScriptTokenStream
                .Where(t => t.TokenType == TSqlTokenType.SingleLineComment || t.TokenType == TSqlTokenType.MultilineComment)
                .Select(t => (line: t.Line, text: t.Text.TrimEnd('\r', '\n'))).ToList();
            AddCommentBlocks(rel, cmts.Select(c => (c.line, c.text.StartsWith("--") ? c.text[2..] : c.text)).ToList(), "t-sql");
        }
        if (frag is not TSqlScript script) return;

        var other = new List<TSqlStatement>();
        foreach (var batch in script.Batches)
        foreach (var st in batch.Statements)
        {
            switch (st)
            {
                case ProcedureStatementBody p:
                {
                    var name = ObjName(p.ProcedureReference.Name);
                    KnownProcs.Add(name);
                    var c = new SqlCollector(); p.Accept(c);
                    var f = new Fact
                    {
                        ["name"] = name, ["kind"] = "procedure", ["file"] = rel, ["start"] = p.StartLine, ["end"] = EndLine(p),
                        ["params"] = p.Parameters.Select(x => new Fact
                        {
                            ["name"] = x.VariableName.Value, ["type"] = FragText(x.DataType),
                            ["default"] = x.Value == null ? null : FragText(x.Value),
                            ["output"] = x.Modifier == ParameterModifier.Output, ["line"] = x.StartLine,
                        }).ToList(),
                    };
                    foreach (var kv in c.Summary()) f[kv.Key] = kv.Value;
                    AddNumerics(rel, name, c);
                    SqlProcs.Add(f);
                    break;
                }
                case FunctionStatementBody fn:
                {
                    var name = ObjName(fn.Name);
                    KnownProcs.Add(name);
                    var c = new SqlCollector(); fn.Accept(c);
                    var f = new Fact
                    {
                        ["name"] = name, ["kind"] = "function", ["file"] = rel, ["start"] = fn.StartLine, ["end"] = EndLine(fn),
                        ["returns_type"] = FragText(fn.ReturnType),
                        ["params"] = fn.Parameters.Select(x => new Fact
                        {
                            ["name"] = x.VariableName.Value, ["type"] = FragText(x.DataType),
                            ["default"] = x.Value == null ? null : FragText(x.Value), ["output"] = false, ["line"] = x.StartLine,
                        }).ToList(),
                    };
                    foreach (var kv in c.Summary()) f[kv.Key] = kv.Value;
                    AddNumerics(rel, name, c);
                    SqlProcs.Add(f);
                    break;
                }
                case ViewStatementBody vw:
                {
                    var name = ObjName(vw.SchemaObjectName);
                    var c = new SqlCollector(); vw.Accept(c);
                    var f = new Fact
                    {
                        ["name"] = name, ["kind"] = "view", ["file"] = rel, ["start"] = vw.StartLine, ["end"] = EndLine(vw),
                        ["params"] = new List<Fact>(),
                    };
                    foreach (var kv in c.Summary()) f[kv.Key] = kv.Value;
                    AddNumerics(rel, name, c);
                    SqlProcs.Add(f);
                    break;
                }
                case TriggerStatementBody t:
                {
                    var c = new SqlCollector(); t.Accept(c);
                    var f = new Fact
                    {
                        ["name"] = ObjName(t.Name), ["file"] = rel, ["start"] = t.StartLine, ["end"] = EndLine(t),
                        ["table"] = ObjName(t.TriggerObject?.Name), ["timing"] = t.TriggerType.ToString(),
                        ["events"] = t.TriggerActions.Select(a => a.TriggerActionType.ToString()).ToList(),
                    };
                    foreach (var kv in c.Summary()) f[kv.Key] = kv.Value;
                    AddNumerics(rel, (string)f["name"], c);
                    SqlTriggers.Add(f);
                    break;
                }
                case CreateTableStatement ct:
                {
                    var cols = new List<Fact>(); var checks = new List<Fact>(); var keys = new List<Fact>();
                    foreach (var cd in ct.Definition.ColumnDefinitions)
                    {
                        cols.Add(new Fact
                        {
                            ["name"] = cd.ColumnIdentifier.Value, ["type"] = FragText(cd.DataType), ["line"] = cd.StartLine,
                            ["default"] = cd.DefaultConstraint == null ? null : FragText(cd.DefaultConstraint.Expression),
                            ["nullable"] = !cd.Constraints.OfType<NullableConstraintDefinition>().Any(n => !n.Nullable),
                        });
                        foreach (var cc in cd.Constraints.OfType<CheckConstraintDefinition>())
                            checks.Add(new Fact { ["line"] = cc.StartLine, ["column"] = cd.ColumnIdentifier.Value, ["text"] = FragText(cc.CheckCondition) });
                    }
                    foreach (var tc in ct.Definition.TableConstraints)
                    {
                        if (tc is CheckConstraintDefinition cc)
                            checks.Add(new Fact { ["line"] = cc.StartLine, ["name"] = cc.ConstraintIdentifier?.Value, ["text"] = FragText(cc.CheckCondition) });
                        else
                            keys.Add(new Fact { ["line"] = tc.StartLine, ["kind"] = tc.GetType().Name.Replace("ConstraintDefinition", ""), ["text"] = FragText(tc) });
                    }
                    var c = new SqlCollector(); ct.Accept(c); AddNumerics(rel, ObjName(ct.SchemaObjectName), c);
                    SqlTables.Add(new Fact
                    {
                        ["name"] = ObjName(ct.SchemaObjectName), ["file"] = rel, ["start"] = ct.StartLine, ["end"] = EndLine(ct),
                        ["columns"] = cols, ["checks"] = checks, ["keys"] = keys,
                    });
                    break;
                }
                case IfStatement ifs when ifs.ThenStatement is DropObjectsStatement:
                    break; // IF OBJECT_ID(...) DROP ... boilerplate
                case InsertStatement ins when ins.InsertSpecification.InsertSource is ValuesInsertSource vs:
                {
                    var target = (ins.InsertSpecification.Target as NamedTableReference)?.SchemaObject;
                    var cols = ins.InsertSpecification.Columns.Select(cr => cr.MultiPartIdentifier.Identifiers.Last().Value).ToList();
                    foreach (var row in vs.RowValues)
                        SqlSeedRows.Add(new Fact
                        {
                            ["table"] = ObjName(target), ["file"] = rel, ["start"] = row.StartLine, ["end"] = EndLine(row),
                            ["columns"] = cols, ["values"] = row.ColumnValues.Select(FragText).ToList(),
                        });
                    var c = new SqlCollector(); ins.Accept(c); AddNumerics(rel, "seed:" + ObjName(target), c);
                    break;
                }
                default:
                    other.Add(st);
                    break;
            }
        }
        if (other.Count > 0)
        {
            var c = new SqlCollector();
            foreach (var st in other) st.Accept(c);
            AddNumerics(rel, "script", c);
            var f = new Fact
            {
                ["file"] = rel, ["start"] = other.Min(s => s.StartLine), ["end"] = other.Max(EndLine),
                ["statements"] = other.Select(s => new Fact { ["kind"] = s.GetType().Name, ["start"] = s.StartLine, ["end"] = EndLine(s) }).ToList(),
            };
            if (crystal.Count > 0) f["crystal_params"] = crystal;
            foreach (var kv in c.Summary()) f[kv.Key] = kv.Value;
            SqlScripts.Add(f);
        }
    }

    static void AddNumerics(string rel, string unit, SqlCollector c)
    {
        foreach (var n in c.Numerics)
            SqlNumericLiterals.Add(new Fact { ["file"] = rel, ["line"] = n["line"], ["unit"] = unit, ["value"] = n["value"],
                                              ["context"] = LineText(rel, (int)n["line"]) });
    }

    // ================================================================== comments
    static readonly Regex CodeLike = new(
        @"^\s*(If\b.*\bThen\b|End (If|Sub|Function|Select)|For Each\b|For\b.*=|Next\b|Dim\b|Call\b|Return\b|" +
        @"(Private|Public|Friend|Protected)\s+(Sub|Function)\b|Select Case\b|Case\b|Exit Sub|" +
        @"[A-Za-z_][\w.]*\s*(=|&=|\+=)\s*\S|[A-Za-z_][\w.]*\(.*\)\s*$|" +
        @"(SELECT|UPDATE|INSERT|DELETE|IF|BEGIN|END|SET|EXEC)\b)", RegexOptions.Compiled);

    static void AddCommentBlocks(string rel, List<(int line, string text)> items, string lang)
    {
        items = items.Where(i => !Regex.IsMatch(i.text, @"Copyright .*SPDX-License-Identifier")).OrderBy(i => i.line).ToList();
        int i = 0;
        while (i < items.Count)
        {
            int j = i;
            while (j + 1 < items.Count && items[j + 1].line == items[j].line + 1) j++;
            var block = items.GetRange(i, j - i + 1);
            var codeLines = block.Where(b => LooksLikeCode(b.text, lang)).Select(b => b.line).ToList();
            var txt = string.Join("\n", block.Select(b => b.text.TrimEnd()));
            if (!Regex.IsMatch(txt, @"^[\s=\-'*/]*$"))
                Comments.Add(new Fact
                {
                    ["file"] = rel, ["start"] = block[0].line, ["end"] = block[^1].line, ["language"] = lang,
                    ["text"] = txt.Length > 1200 ? txt[..1200] + "..." : txt,
                    ["code_like_lines"] = codeLines,
                    ["trailing"] = block.Count == 1 && IsTrailing(rel, block[0].line, lang),
                });
            i = j + 1;
        }
    }

    static bool IsTrailing(string rel, int line, string lang)
    {
        var t = FileLines[rel][line - 1].TrimStart();
        return !(t.StartsWith("'") || t.StartsWith("--") || t.StartsWith("//") || t.StartsWith("/*")
                 || t.StartsWith("REM ", StringComparison.OrdinalIgnoreCase));
    }

    // Commented-out C#: a statement/declaration shape, then confirmed by a real parse.
    static readonly Regex CodeLikeCs = new(
        @"^\s*(if\s*\(|else\b|for\s*\(|foreach\s*\(|while\s*\(|switch\s*\(|return\b|break;|continue;|" +
        @"throw\b|using\s+\w|var\s+\w+\s*=|(public|private|protected|internal|static)\s+|new\s+\w|" +
        @"[A-Za-z_][\w.]*\s*(=|\+=|-=|\|\|=)\s*\S|[A-Za-z_][\w.<>]*\(.*\)\s*;?\s*$|\}|\{)",
        RegexOptions.Compiled);

    static bool LooksLikeCode(string t, string lang)
    {
        t = t.Trim();
        if (t.Length < 3 || t.StartsWith("-") || t.StartsWith("=")) return false;
        if (lang == "vb.net")
        {
            if (!CodeLike.IsMatch(t)) return false;
            var st = SyntaxFactory.ParseExecutableStatement(t);
            bool ok = !st.ContainsDiagnostics && st is not EmptyStatementSyntax;
            var decl = Regex.IsMatch(t, @"^(Private|Public|Friend|Protected)\s+(Sub|Function)\b|^End (Sub|Function|If|Select)\b|^Next\b|^For\b");
            return ok || decl;
        }
        if (lang == "c#")
        {
            if (!CodeLikeCs.IsMatch(t)) return false;
            var st = Microsoft.CodeAnalysis.CSharp.SyntaxFactory.ParseStatement(t);
            if (!st.ContainsDiagnostics && st is not CS.EmptyStatementSyntax) return true;
            // a member declaration or a brace is code too, and will not parse as a statement
            return Regex.IsMatch(t, @"^(public|private|protected|internal|static|override|virtual|async)\s+\w|^[\{\}]+$");
        }
        return Regex.IsMatch(t, @"^(SELECT|UPDATE|INSERT|DELETE|IF|SET|EXEC|BEGIN|END)\b.*[=@(]", RegexOptions.IgnoreCase)
               && ParseSql(t).errs.Count == 0;
    }

    // ================================================================== VB
    static (int s, int e) Span(SyntaxNode n)
    {
        var ls = n.GetLocation().GetLineSpan();
        return (ls.StartLinePosition.Line + 1, ls.EndLinePosition.Line + 1);
    }

    sealed class MethodInfo { public string Id, Class, Name, File, Kind, Access; public int Start, End; public List<string> Handles = new(); }
    static readonly Dictionary<string, List<MethodInfo>> MethodsByName = new(StringComparer.OrdinalIgnoreCase);
    static readonly Dictionary<string, string> TypeKind = new(StringComparer.OrdinalIgnoreCase);       // class/module
    static readonly Dictionary<(string cls, string member), string> MemberType = new();               // field/property type
    static readonly Dictionary<string, Fact> ConstByName = new(StringComparer.OrdinalIgnoreCase);
    static readonly List<MethodInfo> AllMethods = new();

    static string TypeName(TypeSyntax t) => t switch
    {
        null => null,
        QualifiedNameSyntax q => q.Right.Identifier.ValueText,
        IdentifierNameSyntax i => i.Identifier.ValueText,
        GenericNameSyntax g => g.Identifier.ValueText,
        _ => t.ToString(),
    };

    static string DeclType(AsClauseSyntax a) => a switch
    {
        SimpleAsClauseSyntax s => TypeName(s.Type),
        AsNewClauseSyntax n when n.NewExpression is ObjectCreationExpressionSyntax o => TypeName(o.Type),
        _ => null,
    };

    static string EnclosingType(SyntaxNode n) =>
        n.Ancestors().OfType<TypeBlockSyntax>().FirstOrDefault()?.BlockStatement.Identifier.ValueText;

    static void VbDeclarations(string rel, SyntaxTree tree)
    {
        var root = tree.GetRoot();
        foreach (var tb in root.DescendantNodes().OfType<TypeBlockSyntax>())
        {
            var (s, e) = Span(tb);
            var name = tb.BlockStatement.Identifier.ValueText;
            var kind = tb is ModuleBlockSyntax ? "module" : tb is ClassBlockSyntax ? "class" : tb.Kind().ToString();
            var inherits = tb.Inherits.SelectMany(i => i.Types).Select(t => t.ToString()).ToList();
            TypeKind[name] = kind;
            VbTypes.Add(new Fact
            {
                ["name"] = name, ["kind"] = kind, ["file"] = rel, ["start"] = s, ["end"] = e, ["inherits"] = inherits,
                ["partial"] = tb.BlockStatement.Modifiers.Any(m => m.IsKind(SyntaxKind.PartialKeyword)),
                ["is_form"] = inherits.Any(i => i.EndsWith("Form")),
            });
        }
        foreach (var mb in root.DescendantNodes().OfType<MethodBlockBaseSyntax>())
        {
            string name, kind; var handles = new List<string>(); SyntaxTokenList mods;
            switch (mb)
            {
                case MethodBlockSyntax m:
                    name = m.SubOrFunctionStatement.Identifier.ValueText;
                    kind = m.SubOrFunctionStatement.SubOrFunctionKeyword.ValueText;
                    mods = m.SubOrFunctionStatement.Modifiers;
                    if (m.SubOrFunctionStatement.HandlesClause != null)
                        handles = m.SubOrFunctionStatement.HandlesClause.Events.Select(ev => ev.ToString()).ToList();
                    break;
                case ConstructorBlockSyntax c:
                    name = "New"; kind = "Constructor"; mods = c.SubNewStatement.Modifiers; break;
                case AccessorBlockSyntax a when a.Parent is PropertyBlockSyntax pb:
                    continue; // handled via property block below
                default:
                    continue;
            }
            AddMethod(rel, mb, name, kind, mods, handles);
        }
        foreach (var pb in root.DescendantNodes().OfType<PropertyBlockSyntax>())
        {
            AddMethod(rel, pb, pb.PropertyStatement.Identifier.ValueText, "Property", pb.PropertyStatement.Modifiers, new());
            var cls = EnclosingType(pb);
            var t = DeclType(pb.PropertyStatement.AsClause);
            if (cls != null && t != null) MemberType[(cls, pb.PropertyStatement.Identifier.ValueText)] = t;
        }
        foreach (var ps in root.DescendantNodes().OfType<PropertyStatementSyntax>().Where(p => p.Parent is not PropertyBlockSyntax))
        {
            var cls = EnclosingType(ps); var t = DeclType(ps.AsClause);
            if (cls != null && t != null) MemberType[(cls, ps.Identifier.ValueText)] = t;
        }
        foreach (var fd in root.DescendantNodes().OfType<FieldDeclarationSyntax>())
        {
            var cls = EnclosingType(fd);
            bool isConst = fd.Modifiers.Any(m => m.IsKind(SyntaxKind.ConstKeyword));
            foreach (var d in fd.Declarators)
            foreach (var n in d.Names)
            {
                var (s, e) = Span(fd);
                var t = DeclType(d.AsClause);
                var nm = n.Identifier.ValueText;
                if (cls != null && t != null) MemberType[(cls, nm)] = t;
                if (isConst)
                {
                    var f = new Fact
                    {
                        ["name"] = nm, ["owner"] = cls, ["file"] = rel, ["start"] = s, ["end"] = e, ["type"] = t,
                        ["value"] = d.Initializer?.Value.ToString(), ["text"] = LineText(rel, s),
                    };
                    VbConstants.Add(f); ConstByName[nm] = f;
                }
                else
                    VbMembers.Add(new Fact
                    {
                        ["name"] = nm, ["owner"] = cls, ["file"] = rel, ["start"] = s, ["end"] = e, ["type"] = t,
                        ["kind"] = "field", ["withevents"] = fd.Modifiers.Any(m => m.IsKind(SyntaxKind.WithEventsKeyword)),
                        ["initializer"] = d.Initializer == null ? null : Clip(d.Initializer.Value.ToString(), 80),
                    });
            }
        }
        // comments
        var cm = root.DescendantTrivia().Where(t => t.IsKind(SyntaxKind.CommentTrivia))
            .Select(t =>
            {
                var line = t.GetLocation().GetLineSpan().StartLinePosition.Line + 1;
                var txt = t.ToString();
                txt = txt.StartsWith("'") ? txt[1..] : Regex.Replace(txt, "^REM\\s?", "", RegexOptions.IgnoreCase);
                return (line, txt);
            }).ToList();
        AddCommentBlocks(rel, cm, "vb.net");
    }

    static void AddMethod(string rel, SyntaxNode node, string name, string kind, SyntaxTokenList mods, List<string> handles)
    {
        var cls = EnclosingType(node);
        var (s, e) = Span(node);
        var access = mods.FirstOrDefault(m => m.IsKind(SyntaxKind.PublicKeyword) || m.IsKind(SyntaxKind.PrivateKeyword) ||
                                              m.IsKind(SyntaxKind.FriendKeyword) || m.IsKind(SyntaxKind.ProtectedKeyword)).ValueText;
        var mi = new MethodInfo { Id = $"{cls}.{name}", Class = cls, Name = name, File = rel, Kind = kind,
                                  Access = string.IsNullOrEmpty(access) ? "Public" : access, Start = s, End = e, Handles = handles };
        AllMethods.Add(mi);
        if (!MethodsByName.TryGetValue(name, out var l)) MethodsByName[name] = l = new();
        l.Add(mi);
        VbMethods.Add(new Fact
        {
            ["id"] = mi.Id, ["class"] = cls, ["name"] = name, ["kind"] = kind, ["access"] = mi.Access,
            ["file"] = rel, ["start"] = s, ["end"] = e, ["handles"] = handles,
            ["is_event_handler"] = handles.Count > 0,
        });
    }

    static MethodInfo EnclosingMethod(string rel, SyntaxNode n)
    {
        var line = Span(n).s;
        return AllMethods.Where(m => m.File == rel && m.Start <= line && line <= m.End)
                         .OrderBy(m => m.End - m.Start).FirstOrDefault();
    }

    static readonly Regex SqlStart = new(@"^\s*(SELECT|INSERT|UPDATE|DELETE|DECLARE|EXEC|EXECUTE|WITH|MERGE)\b", RegexOptions.IgnoreCase | RegexOptions.Compiled);

    static bool IsConcat(SyntaxNode n) => n.IsKind(SyntaxKind.ConcatenateExpression) || n.IsKind(SyntaxKind.AddExpression);

    static string FlattenSql(ExpressionSyntax e) => e switch
    {
        LiteralExpressionSyntax l when l.IsKind(SyntaxKind.StringLiteralExpression) => l.Token.ValueText,
        BinaryExpressionSyntax b when IsConcat(b) => FlattenSql(b.Left) + FlattenSql(b.Right),
        ParenthesizedExpressionSyntax p => FlattenSql(p.Expression),
        _ => " @vbexpr ",
    };

    static readonly HashSet<SyntaxKind> CmpKinds = new()
    {
        SyntaxKind.EqualsExpression, SyntaxKind.NotEqualsExpression, SyntaxKind.LessThanExpression,
        SyntaxKind.LessThanOrEqualExpression, SyntaxKind.GreaterThanExpression, SyntaxKind.GreaterThanOrEqualExpression,
    };
    static readonly HashSet<SyntaxKind> ArithKinds = new()
    {
        SyntaxKind.MultiplyExpression, SyntaxKind.DivideExpression, SyntaxKind.AddExpression, SyntaxKind.SubtractExpression,
        SyntaxKind.IntegerDivideExpression, SyntaxKind.ModuloExpression,
    };

    static string LiteralContext(SyntaxNode lit)
    {
        foreach (var a in lit.Ancestors())
        {
            if (a is StatementSyntax && a is not ReturnStatementSyntax) return null;
            if (a is BinaryExpressionSyntax b && CmpKinds.Contains(b.Kind())) return "comparison";
            if (a is CaseClauseSyntax) return "case";
            if (a is ReturnStatementSyntax) return "return";
            if (a is BinaryExpressionSyntax b2 && ArithKinds.Contains(b2.Kind())) return "arithmetic";
            if (a is ArgumentListSyntax) return null;
        }
        return null;
    }

    static void VbBodies(string rel, SyntaxTree tree)
    {
        var root = tree.GetRoot();

        // ---- calls
        foreach (var node in root.DescendantNodes())
        {
            ExpressionSyntax target = null;
            if (node is InvocationExpressionSyntax inv) target = inv.Expression;
            else if (node is ExpressionStatementSyntax es && es.Expression is (IdentifierNameSyntax or MemberAccessExpressionSyntax)) target = es.Expression;
            else if (node is UnaryExpressionSyntax ua && ua.IsKind(SyntaxKind.AddressOfExpression)) target = ua.Operand;
            if (target == null) continue;
            if (node is InvocationExpressionSyntax && node.Parent is InvocationExpressionSyntax) { }
            var caller = EnclosingMethod(rel, node);
            var (name, receiver) = target switch
            {
                IdentifierNameSyntax id => (id.Identifier.ValueText, (ExpressionSyntax)null),
                MemberAccessExpressionSyntax ma => (ma.Name.Identifier.ValueText, ma.Expression),
                _ => (null, null),
            };
            if (name == null || !MethodsByName.TryGetValue(name, out var cands)) continue;
            var cls = EnclosingType(node);
            MethodInfo callee = null; string how = null;
            if (receiver == null)
            {
                callee = cands.FirstOrDefault(c => c.Class == cls);
                how = "same-class";
                if (callee == null) { callee = cands.FirstOrDefault(c => TypeKind.GetValueOrDefault(c.Class) == "module"); how = "module"; }
                if (callee == null) { callee = cands.Count == 1 ? cands[0] : null; how = "by-name"; }
            }
            else
            {
                var rtype = ReceiverType(receiver, cls, caller, root);
                if (rtype != null) { callee = cands.FirstOrDefault(c => string.Equals(c.Class, rtype, StringComparison.OrdinalIgnoreCase)); how = "receiver-type"; }
                if (callee == null && cands.Count == 1) { callee = cands[0]; how = "by-name"; }
            }
            if (callee == null || (caller != null && callee.Id == caller.Id && node is not InvocationExpressionSyntax)) continue;
            var line = Span(node).s;
            VbCalls.Add(new Fact
            {
                ["from"] = caller?.Id ?? $"{cls}.<field-init>", ["to"] = callee.Id, ["file"] = rel, ["line"] = line,
                ["callee_file"] = callee.File, ["resolution"] = how, ["text"] = LineText(rel, line),
            });
        }

        // ---- proc refs + inline SQL
        var handled = new HashSet<SyntaxNode>();
        foreach (var lit in root.DescendantNodes().OfType<LiteralExpressionSyntax>().Where(l => l.IsKind(SyntaxKind.StringLiteralExpression)))
        {
            var v = lit.Token.ValueText;
            var m = Regex.Match(v, @"^\s*(?:dbo\.)?([A-Za-z_]\w*)\s*$");
            if (m.Success && (KnownProcs.Contains(m.Groups[1].Value) || Regex.IsMatch(m.Groups[1].Value, @"^(usp|sp|bsp|vsp|proc)_", RegexOptions.IgnoreCase)))
            {
                var line = Span(lit).s; var meth = EnclosingMethod(rel, lit);
                VbProcRefs.Add(new Fact
                {
                    ["from"] = meth?.Id, ["file"] = rel, ["line"] = line, ["proc"] = m.Groups[1].Value,
                    ["defined_in_corpus"] = KnownProcs.Contains(m.Groups[1].Value), ["text"] = LineText(rel, line),
                });
            }
        }
        foreach (var e in root.DescendantNodes().OfType<ExpressionSyntax>())
        {
            if (!(e is LiteralExpressionSyntax l0 && l0.IsKind(SyntaxKind.StringLiteralExpression)) && !(e is BinaryExpressionSyntax b0 && IsConcat(b0))) continue;
            if (e.Parent is BinaryExpressionSyntax pb && IsConcat(pb)) continue;
            if (e.Parent is ParenthesizedExpressionSyntax) continue;
            var sql = FlattenSql(e);
            if (!SqlStart.IsMatch(sql) || !Regex.IsMatch(sql, @"\b(FROM|INTO|SET|VALUES|WHERE)\b", RegexOptions.IgnoreCase)) continue;
            var (s, en) = Span(e);
            var meth = EnclosingMethod(rel, e);
            // variable the SQL is stored in, so later "sql &= ..." fragments can be appended
            string varName = e.Parent switch
            {
                EqualsValueSyntax ev when ev.Parent is VariableDeclaratorSyntax vd => vd.Names[0].Identifier.ValueText,
                AssignmentStatementSyntax asg when asg.Left is IdentifierNameSyntax idn => idn.Identifier.ValueText,
                _ => null,
            };
            var frags = new List<Fact>();
            if (varName != null && meth != null)
            {
                var methNode = e.Ancestors().OfType<MethodBlockBaseSyntax>().FirstOrDefault();
                foreach (var asg in methNode?.DescendantNodes().OfType<AssignmentStatementSyntax>() ?? Enumerable.Empty<AssignmentStatementSyntax>())
                {
                    if (!asg.IsKind(SyntaxKind.ConcatenateAssignmentStatement) && !asg.IsKind(SyntaxKind.AddAssignmentStatement)) continue;
                    if (asg.Left is not IdentifierNameSyntax lid || !string.Equals(lid.Identifier.ValueText, varName, StringComparison.OrdinalIgnoreCase)) continue;
                    if (asg.SpanStart < e.SpanStart) continue;
                    var (fs, fe) = Span(asg);
                    var cond = asg.Ancestors().TakeWhile(a => a != methNode).Any(a => a is MultiLineIfBlockSyntax or SingleLineIfStatementSyntax or SelectBlockSyntax);
                    frags.Add(new Fact { ["start"] = fs, ["end"] = fe, ["conditional"] = cond, ["sql"] = FlattenSql(asg.Right) });
                    sql += FlattenSql(asg.Right);
                    en = Math.Max(en, fe);
                }
            }
            var (frag, errs) = ParseSql(sql);
            var c = new SqlCollector(s, en);
            frag?.Accept(c);
            var f = new Fact
            {
                ["from"] = meth?.Id, ["file"] = rel, ["start"] = s, ["end"] = en, ["variable"] = varName,
                ["sql"] = Clip(sql, 600), ["parse_ok"] = errs.Count == 0, ["appended_fragments"] = frags,
            };
            if (errs.Count > 0) f["parse_error"] = errs[0].Message;
            foreach (var kv in c.Summary()) if (kv.Key is not ("exec_calls" or "returns" or "raises" or "literal_assigns")) f[kv.Key] = kv.Value;
            VbInlineSql.Add(f);
        }

        // ---- literals and constant uses
        bool InConstDecl(SyntaxNode n) =>
            n.Ancestors().OfType<FieldDeclarationSyntax>().Any(fd => fd.Modifiers.Any(m => m.IsKind(SyntaxKind.ConstKeyword))) ||
            n.Ancestors().OfType<LocalDeclarationStatementSyntax>().Any(ld => ld.Modifiers.Any(m => m.IsKind(SyntaxKind.ConstKeyword)));
        foreach (var lit in root.DescendantNodes().OfType<LiteralExpressionSyntax>())
        {
            bool num = lit.IsKind(SyntaxKind.NumericLiteralExpression);
            bool str = lit.IsKind(SyntaxKind.StringLiteralExpression) && lit.Token.ValueText.Length is > 0 and <= 12;
            if (!num && !str) continue;
            if (InConstDecl(lit)) continue;
            var ctx = LiteralContext(lit);
            if (ctx == null) continue;
            var val = num ? Convert.ToString(lit.Token.Value, CultureInfo.InvariantCulture) : lit.Token.ValueText;
            if (ctx == "arithmetic" && (val == "0" || val == "1" || str)) continue;
            var line = Span(lit).s;
            VbLiterals.Add(new Fact
            {
                ["file"] = rel, ["line"] = line, ["from"] = EnclosingMethod(rel, lit)?.Id, ["kind"] = num ? "number" : "string",
                ["value"] = val, ["context"] = ctx, ["text"] = LineText(rel, line),
            });
        }
        foreach (var id in root.DescendantNodes().OfType<IdentifierNameSyntax>())
        {
            var nm = id.Identifier.ValueText;
            if (!ConstByName.TryGetValue(nm, out var cdef)) continue;
            if (InConstDecl(id)) continue;
            var line = Span(id).s;
            var cmp = id.Ancestors().TakeWhile(a => a is not StatementSyntax).OfType<BinaryExpressionSyntax>().FirstOrDefault(b => CmpKinds.Contains(b.Kind()));
            VbConstUses.Add(new Fact
            {
                ["const"] = nm, ["const_file"] = cdef["file"], ["file"] = rel, ["line"] = line,
                ["from"] = EnclosingMethod(rel, id)?.Id, ["comparison"] = cmp == null ? null : cmp.OperatorToken.ValueText,
                ["text"] = LineText(rel, line),
            });
        }
    }


    // ================================================================== VB, v1 additions
    static string OpSym(SyntaxKind k) => k switch
    {
        SyntaxKind.EqualsExpression => "=", SyntaxKind.NotEqualsExpression => "<>", SyntaxKind.LessThanExpression => "<",
        SyntaxKind.LessThanOrEqualExpression => "<=", SyntaxKind.GreaterThanExpression => ">",
        SyntaxKind.GreaterThanOrEqualExpression => ">=", _ => k.ToString(),
    };

    static string ConstValue(string name) =>
        ConstByName.TryGetValue(name, out var c) ? (string)c["value"] : null;

    static string NormNumText(string v)
    {
        // VB type characters (D R F S I L @ # !) plus the C# literal suffixes (m/M decimal,
        // u/U unsigned, d/f/l) — `5000m` and `5000` are the same threshold and must normalise
        // to the same string, or a C#-vs-T-SQL cross-layer comparison can never match.
        var t = (v ?? "").Trim().TrimEnd('D', 'd', 'R', 'r', 'F', 'f', '@', '#', '!', 'S', 's',
                                         'I', 'i', 'L', 'l', 'M', 'm', 'U', 'u');
        return decimal.TryParse(t, NumberStyles.Any, CultureInfo.InvariantCulture, out var x) ? x.ToString("0.####", CultureInfo.InvariantCulture) : null;
    }

    /// <summary>Operand facts: identifiers (lowercased), string-indexer names, literal/constant value.</summary>
    static Fact Operand(ExpressionSyntax e)
    {
        var ids = new List<string>(); var consts = new List<string>(); string value = null;
        foreach (var n in e.DescendantNodesAndSelf())
        {
            if (n is IdentifierNameSyntax id)
            {
                var nm = id.Identifier.ValueText;
                if (ConstByName.ContainsKey(nm)) consts.Add(nm);
                ids.Add(nm);
            }
            else if (n is LiteralExpressionSyntax l && l.IsKind(SyntaxKind.StringLiteralExpression) &&
                     l.Parent is SimpleArgumentSyntax && Regex.IsMatch(l.Token.ValueText, @"^@?[A-Za-z_]\w{1,40}$"))
                ids.Add(l.Token.ValueText.TrimStart('@'));
        }
        var core = e;
        while (core is ParenthesizedExpressionSyntax pe) core = pe.Expression;
        if (core is LiteralExpressionSyntax lit)
            value = lit.IsKind(SyntaxKind.NumericLiteralExpression) ? NormNumText(lit.Token.Text) : lit.Token.ValueText;
        else if (core is IdentifierNameSyntax cid && ConstByName.ContainsKey(cid.Identifier.ValueText))
        {
            var cv = ConstValue(cid.Identifier.ValueText) ?? "";
            value = cv.StartsWith("\"") ? cv.Trim('"') : (NormNumText(cv) ?? cv);
        }
        else if (core is UnaryExpressionSyntax u && u.IsKind(SyntaxKind.UnaryMinusExpression) && u.Operand is LiteralExpressionSyntax ul)
            value = "-" + NormNumText(ul.Token.Text);
        return new Fact { ["text"] = Clip(e.ToString(), 120), ["ids"] = ids.Distinct(StringComparer.OrdinalIgnoreCase).ToList(),
                          ["consts"] = consts.Distinct().ToList(), ["value"] = value };
    }

    static bool IsInvokedName(SyntaxToken t)
    {
        var p = t.Parent as IdentifierNameSyntax;
        if (p == null) return false;
        if (p.Parent is InvocationExpressionSyntax inv && inv.Expression == p) return true;
        if (p.Parent is MemberAccessExpressionSyntax ma && ma.Name == p && ma.Parent is InvocationExpressionSyntax) return true;
        return false;
    }

    /// <summary>k1: names kept (constants -> values); k2: simple locals/params/fields -> $, member/invoked names kept.</summary>
    static (string k1, string k2, int n) VbFp(SyntaxNode node)
    {
        var a = new List<string>(); var b = new List<string>();
        SyntaxToken prev = default;
        foreach (var t in node.DescendantTokens())
        {
            if (t.IsKind(SyntaxKind.MeKeyword)) { prev = t; continue; }
            if (t.IsKind(SyntaxKind.DotToken) && prev.IsKind(SyntaxKind.MeKeyword)) { prev = t; continue; }
            if (t.IsKind(SyntaxKind.IdentifierToken))
            {
                var nm = t.ValueText;
                if (ConstByName.ContainsKey(nm)) { var v = ConstValue(nm) ?? nm; a.Add("#" + v); b.Add("#" + v); }
                else
                {
                    var afterDot = prev.IsKind(SyntaxKind.DotToken);
                    a.Add(nm.ToLowerInvariant());
                    b.Add(afterDot || IsInvokedName(t) ? nm.ToLowerInvariant() : "$");
                }
            }
            else if (t.IsKind(SyntaxKind.IntegerLiteralToken) || t.IsKind(SyntaxKind.DecimalLiteralToken) || t.IsKind(SyntaxKind.FloatingLiteralToken))
            { var v = NormNumText(t.Text) ?? t.Text; a.Add(v); b.Add(v); }
            else if (t.IsKeyword()) { a.Add(t.ValueText.ToLowerInvariant()); b.Add(t.ValueText.ToLowerInvariant()); }
            else { a.Add(t.Text); b.Add(t.Text); }
            prev = t;
        }
        return (string.Join(" ", a), string.Join(" ", b), a.Count);
    }

    static void AddFp(string rel, SyntaxNode n, string kind)
    {
        if (n == null) return;
        var (k1, k2, cnt) = VbFp(n);
        if (cnt < 5) return;
        var (s, e) = Span(n);
        VbFingerprints.Add(new Fact
        {
            ["file"] = rel, ["from"] = EnclosingMethod(rel, n)?.Id, ["kind"] = kind, ["start"] = s, ["end"] = e,
            ["k1"] = k1, ["k2"] = k2, ["ntok"] = cnt, ["text"] = Clip(n.ToString(), 160),
        });
    }

    static readonly (string rx, string kind)[] IoApis =
    {
        (@"^(HttpClient|WebClient|HttpWebRequest|WebRequest|RestClient|RestRequest|HttpRequestMessage)$", "http"),
        (@"^(GetAsync|PostAsync|PutAsync|DeleteAsync|SendAsync|GetStringAsync|UploadString|DownloadString|UploadData|DownloadData|GetResponse|GetRequestStream)$", "http"),
        (@"^(SerializeObject|DeserializeObject|JsonConvert|JavaScriptSerializer|JsonSerializer|JObject|JToken|JArray|DataContractJsonSerializer)$", "json"),
        (@"^(XmlSerializer|DataContractSerializer|XmlDocument|XDocument|XmlWriter|XmlReader|WriteXml|ReadXml)$", "xml"),
        (@"^(StreamWriter|StreamReader|WriteAllText|AppendAllText|ReadAllText|WriteAllLines|ReadAllLines|FileStream|OpenTextFile|FileOpen|PrintLine|WriteLine|FileCopy|Copy|Move)$", "file"),
        (@"^(SmtpClient|MailMessage|Send)$", "email"),
        (@"^(AppSettings|ConnectionStrings|GetSetting|SaveSetting|Registry|RegistryKey|GetEnvironmentVariable)$", "config"),
        (@"^(Process|Shell|Start)$", "process"),
        (@"^(MessageQueue|QueueClient|ServiceBusClient|BlobClient|SendMessage)$", "messaging"),
        (@"^(ReportDocument|SetParameterValue|ExportToDisk|ExportToStream|PrintToPrinter|CrystalReportViewer)$", "report"),
    };

    static void VbV1(string rel, SyntaxTree tree)
    {
        var root = tree.GetRoot();
        bool InConstDecl(SyntaxNode n) =>
            n.Ancestors().OfType<FieldDeclarationSyntax>().Any(fd => fd.Modifiers.Any(m => m.IsKind(SyntaxKind.ConstKeyword)));

        // ---- comparisons (binary) with operands split
        foreach (var b in root.DescendantNodes().OfType<BinaryExpressionSyntax>().Where(x => CmpKinds.Contains(x.Kind())))
        {
            if (InConstDecl(b)) continue;
            var ctx = b.Ancestors().FirstOrDefault(a => a is StatementSyntax) switch
            {
                IfStatementSyntax or ElseIfStatementSyntax or SingleLineIfStatementSyntax => "if",
                ReturnStatementSyntax => "return",
                WhileStatementSyntax or DoStatementSyntax or LoopStatementSyntax => "loop",
                _ => "expr",
            };
            VbComparisons.Add(new Fact
            {
                ["file"] = rel, ["line"] = Span(b).s, ["from"] = EnclosingMethod(rel, b)?.Id, ["op"] = OpSym(b.Kind()),
                ["left"] = Operand(b.Left), ["right"] = Operand(b.Right), ["context"] = ctx, ["text"] = LineText(rel, Span(b).s),
            });
        }
        // ---- Select Case clauses as comparisons against the selector
        foreach (var sb in root.DescendantNodes().OfType<SelectBlockSyntax>())
        {
            var sel = sb.SelectStatement.Expression;
            foreach (var cb in sb.CaseBlocks)
            foreach (var cc in cb.CaseStatement.Cases)
            {
                ExpressionSyntax val = null; string op = "=";
                if (cc is SimpleCaseClauseSyntax sc) val = sc.Value;
                else if (cc is RelationalCaseClauseSyntax rc) { val = rc.Value; op = rc.OperatorToken.ValueText; }
                if (val == null) continue;
                VbComparisons.Add(new Fact
                {
                    ["file"] = rel, ["line"] = Span(cc).s, ["from"] = EnclosingMethod(rel, cc)?.Id, ["op"] = op,
                    ["left"] = Operand(sel), ["right"] = Operand(val), ["context"] = "case", ["text"] = LineText(rel, Span(cc).s),
                });
            }
            AddFp(rel, sb, "select-case");
        }

        // ---- fingerprints
        foreach (var n in root.DescendantNodes())
        {
            switch (n)
            {
                case IfStatementSyntax i: AddFp(rel, i.Condition, "if"); break;
                case ElseIfStatementSyntax ei: AddFp(rel, ei.Condition, "if"); break;
                case SingleLineIfStatementSyntax si: AddFp(rel, si.Condition, "if"); break;
                case ReturnStatementSyntax r when r.Expression != null: AddFp(rel, r.Expression, "return"); break;
                case AssignmentStatementSyntax asg when asg.Right is BinaryExpressionSyntax || asg.Right is InvocationExpressionSyntax:
                    AddFp(rel, asg, "assign"); break;
                case LocalDeclarationStatementSyntax ld when ld.Declarators.Any(d => d.Initializer != null && d.Initializer.Value is BinaryExpressionSyntax or InvocationExpressionSyntax):
                    foreach (var d in ld.Declarators.Where(d => d.Initializer != null)) AddFp(rel, d.Initializer.Value, "init");
                    break;
            }
        }
        foreach (var mb in root.DescendantNodes().OfType<MethodBlockBaseSyntax>())
        {
            if (mb.Statements.Count < 2) continue;
            // body-only fingerprint (signature excluded)
            var fps = mb.Statements.Select(st => VbFp(st)).ToList();
            var cnt = fps.Sum(x => x.n);
            var body = string.Join(" ", fps.Select(x => x.k2));
            var (s, e) = Span(mb);
            VbFingerprints.Add(new Fact
            {
                ["file"] = rel, ["from"] = EnclosingMethod(rel, mb)?.Id, ["kind"] = "method-body", ["start"] = s, ["end"] = e,
                ["k1"] = string.Join(" ", fps.Select(x => x.k1)), ["k2"] = body, ["ntok"] = cnt,
                ["text"] = Clip(mb.BlockStatement.ToString(), 160),
            });
        }

        // ---- string literals that name DB columns / proc params; every literal per method
        foreach (var l in root.DescendantNodes().OfType<LiteralExpressionSyntax>())
        {
            if (InConstDecl(l)) continue;
            var line = Span(l).s; var from = EnclosingMethod(rel, l)?.Id;
            if (l.IsKind(SyntaxKind.StringLiteralExpression))
            {
                var v = l.Token.ValueText;
                if (v.Length > 0 && v.Length <= 200)
                    VbUnitLiterals.Add(new Fact { ["file"] = rel, ["line"] = line, ["from"] = from, ["kind"] = "string", ["value"] = v });
                var bare = v.TrimStart('@');
                if (l.Parent is SimpleArgumentSyntax arg && Regex.IsMatch(v, @"^@?[A-Za-z_]\w*$") && KnownColumns.Contains(bare))
                {
                    var inv = arg.Parent?.Parent as InvocationExpressionSyntax;
                    var target = inv?.Expression switch
                    {
                        MemberAccessExpressionSyntax ma => ma.Name.Identifier.ValueText,
                        IdentifierNameSyntax id => id.Identifier.ValueText, _ => null,
                    };
                    var isWrite = inv != null && inv.Parent is AssignmentStatementSyntax asg && asg.Left == inv;
                    var how = v.StartsWith("@") || (target != null && Regex.IsMatch(target, "^(AddWithValue|Add|Parameters)$")) ? "sql-param"
                              : isWrite ? "write" : "read";
                    VbColumnRefs.Add(new Fact { ["file"] = rel, ["line"] = line, ["from"] = from, ["name"] = bare, ["how"] = how,
                                                ["via"] = target, ["text"] = LineText(rel, line) });
                }
            }
            else if (l.IsKind(SyntaxKind.NumericLiteralExpression))
            {
                var v = NormNumText(l.Token.Text);
                if (v != null)
                    VbUnitLiterals.Add(new Fact { ["file"] = rel, ["line"] = line, ["from"] = from, ["kind"] = "number", ["value"] = v });
            }
        }
        foreach (var id in root.DescendantNodes().OfType<IdentifierNameSyntax>())
        {
            var nm = id.Identifier.ValueText;
            if (!ConstByName.ContainsKey(nm) || InConstDecl(id)) continue;
            var cv = ConstValue(nm) ?? "";
            VbUnitLiterals.Add(new Fact { ["file"] = rel, ["line"] = Span(id).s, ["from"] = EnclosingMethod(rel, id)?.Id,
                                          ["kind"] = cv.StartsWith("\"") ? "string" : "number",
                                          ["value"] = cv.StartsWith("\"") ? cv.Trim('"') : (NormNumText(cv) ?? cv), ["const"] = nm });
        }

        // ---- locals initialised / assigned from a column-ish expression
        void Alias(SyntaxNode at, string name, ExpressionSyntax value)
        {
            if (value == null) return;
            var ali = new List<string>(); string dflt = null;
            foreach (var n in value.DescendantNodesAndSelf())
            {
                if (n is LiteralExpressionSyntax l && l.IsKind(SyntaxKind.StringLiteralExpression) && Regex.IsMatch(l.Token.ValueText, @"^@?[A-Za-z_]\w{1,40}$"))
                    ali.Add(l.Token.ValueText.TrimStart('@'));
                else if (n is LiteralExpressionSyntax ln && ln.IsKind(SyntaxKind.NumericLiteralExpression)) dflt ??= NormNumText(ln.Token.Text);
                else if (n is IdentifierNameSyntax idn && ConstByName.ContainsKey(idn.Identifier.ValueText)) ali.Add(idn.Identifier.ValueText);
            }
            var core = value;
            if (core is MemberAccessExpressionSyntax mae) ali.Add(mae.Name.Identifier.ValueText);
            if (core is IdentifierNameSyntax ide) ali.Add(ide.Identifier.ValueText);
            if (ali.Count == 0) return;
            VbLocalAliases.Add(new Fact { ["file"] = rel, ["line"] = Span(at).s, ["from"] = EnclosingMethod(rel, at)?.Id, ["name"] = name,
                                          ["aliases"] = ali.Distinct(StringComparer.OrdinalIgnoreCase).ToList(), ["default"] = dflt,
                                          ["text"] = LineText(rel, Span(at).s) });
        }
        foreach (var vd in root.DescendantNodes().OfType<VariableDeclaratorSyntax>())
            if (vd.Initializer != null && vd.Parent is LocalDeclarationStatementSyntax)
                foreach (var nm in vd.Names) Alias(vd, nm.Identifier.ValueText, vd.Initializer.Value);
        foreach (var asg in root.DescendantNodes().OfType<AssignmentStatementSyntax>())
            if (asg.IsKind(SyntaxKind.SimpleAssignmentStatement) && asg.Left is IdentifierNameSyntax ln2)
                Alias(asg, ln2.Identifier.ValueText, asg.Right);

        // ---- attributes, enums, external I/O
        foreach (var at in root.DescendantNodes().OfType<AttributeSyntax>())
        {
            var owner = at.Ancestors().FirstOrDefault(a => a is StatementSyntax || a is DeclarationStatementSyntax);
            string target = owner switch
            {
                ClassStatementSyntax c => c.Identifier.ValueText, StructureStatementSyntax st => st.Identifier.ValueText,
                MethodStatementSyntax m => m.Identifier.ValueText, PropertyStatementSyntax ps => ps.Identifier.ValueText,
                FieldDeclarationSyntax fd => string.Join(",", fd.Declarators.SelectMany(d => d.Names).Select(n => n.Identifier.ValueText)),
                EnumStatementSyntax es => es.Identifier.ValueText, InterfaceStatementSyntax it => it.Identifier.ValueText,
                _ => null,
            };
            VbAttributes.Add(new Fact { ["file"] = rel, ["line"] = Span(at).s, ["name"] = at.Name.ToString(),
                                        ["args"] = at.ArgumentList?.ToString(), ["target"] = target, ["owner"] = EnclosingType(at) });
        }
        foreach (var eb in root.DescendantNodes().OfType<EnumBlockSyntax>())
        {
            var (s, e) = Span(eb);
            VbEnums.Add(new Fact
            {
                ["file"] = rel, ["start"] = s, ["end"] = e, ["name"] = eb.EnumStatement.Identifier.ValueText, ["owner"] = EnclosingType(eb),
                ["members"] = eb.Members.OfType<EnumMemberDeclarationSyntax>().Select(m => new Fact
                {
                    ["name"] = m.Identifier.ValueText, ["value"] = m.Initializer?.Value.ToString(), ["line"] = Span(m).s,
                }).ToList(),
            });
        }
        foreach (var n in root.DescendantNodes())
        {
            string api = n switch
            {
                ObjectCreationExpressionSyntax oc => TypeName(oc.Type),
                InvocationExpressionSyntax inv => inv.Expression switch
                {
                    MemberAccessExpressionSyntax ma => ma.Name.Identifier.ValueText,
                    IdentifierNameSyntax id => id.Identifier.ValueText, _ => null,
                },
                MemberAccessExpressionSyntax ma2 when ma2.Parent is not InvocationExpressionSyntax &&
                    Regex.IsMatch(ma2.Name.Identifier.ValueText, "^(AppSettings|ConnectionStrings)$") => ma2.Name.Identifier.ValueText,
                _ => null,
            };
            if (api == null) continue;
            if (MethodsByName.ContainsKey(api)) continue; // corpus-defined method of the same name
            foreach (var (rx, kind) in IoApis)
            {
                if (!Regex.IsMatch(api, rx)) continue;
                if (kind == "file" && api is "WriteLine" or "Copy" or "Move" &&
                    !(n is InvocationExpressionSyntax iv && iv.Expression is MemberAccessExpressionSyntax m2 && Regex.IsMatch(m2.Expression.ToString(), @"(File|Writer|sw|fs|IO)\b", RegexOptions.IgnoreCase))) break;
                if (kind == "email" && api == "Send" && !(n is InvocationExpressionSyntax iv2 && Regex.IsMatch(iv2.Expression.ToString(), "(smtp|mail)", RegexOptions.IgnoreCase))) break;
                if (kind == "process" && api == "Start" && !(n is InvocationExpressionSyntax iv3 && iv3.Expression.ToString().Contains("Process"))) break;
                var line = Span(n).s;
                VbIoCalls.Add(new Fact { ["file"] = rel, ["line"] = line, ["from"] = EnclosingMethod(rel, n)?.Id, ["kind"] = kind,
                                         ["api"] = api, ["text"] = LineText(rel, line) });
                break;
            }
        }
        foreach (var l in root.DescendantNodes().OfType<LiteralExpressionSyntax>().Where(x => x.IsKind(SyntaxKind.StringLiteralExpression)))
        {
            var v = l.Token.ValueText; string kind = null;
            if (Regex.IsMatch(v, @"^(https?|ftp|net\.tcp)://", RegexOptions.IgnoreCase)) kind = "url";
            else if (Regex.IsMatch(v, @"^\s*[\{\[]\s*""")) kind = "json-literal";
            else if (Regex.IsMatch(v, @"\.(csv|xml|json|txt|xls|xlsx|pdf|rpt|dat|ini|config)$", RegexOptions.IgnoreCase)) kind = "file-name";
            if (kind == null) continue;
            var line = Span(l).s;
            VbIoCalls.Add(new Fact { ["file"] = rel, ["line"] = line, ["from"] = EnclosingMethod(rel, l)?.Id, ["kind"] = kind,
                                     ["api"] = Clip(v, 120), ["text"] = LineText(rel, line) });
        }
    }

    static string ReceiverType(ExpressionSyntax recv, string cls, MethodInfo caller, SyntaxNode root)
    {
        switch (recv)
        {
            case MeExpressionSyntax: case MyClassExpressionSyntax: return cls;
            case IdentifierNameSyntax id:
            {
                var n = id.Identifier.ValueText;
                if (TypeKind.ContainsKey(n)) return n;
                if (cls != null && MemberType.TryGetValue((cls, n), out var t)) return t;
                // local Dim x As T inside the caller
                if (caller != null)
                {
                    var methNode = recv.Ancestors().OfType<MethodBlockBaseSyntax>().FirstOrDefault();
                    var decl = methNode?.DescendantNodes().OfType<VariableDeclaratorSyntax>()
                        .FirstOrDefault(d => d.Names.Any(x => string.Equals(x.Identifier.ValueText, n, StringComparison.OrdinalIgnoreCase)));
                    var lt = decl == null ? null : DeclType(decl.AsClause);
                    if (lt != null) return lt;
                    var prm = methNode?.BlockStatement.ParameterList?.Parameters
                        .FirstOrDefault(p => string.Equals(p.Identifier.Identifier.ValueText, n, StringComparison.OrdinalIgnoreCase));
                    if (prm?.AsClause != null) return TypeName(prm.AsClause.Type);
                }
                return null;
            }
            case MemberAccessExpressionSyntax ma:
            {
                var inner = ma.Expression == null ? cls : ReceiverType(ma.Expression, cls, caller, root);
                var mn = ma.Name.Identifier.ValueText;
                if (inner != null && MemberType.TryGetValue((inner, mn), out var t)) return t;
                var any = MemberType.Where(kv => string.Equals(kv.Key.member, mn, StringComparison.OrdinalIgnoreCase)).Select(kv => kv.Value).Distinct().ToList();
                return any.Count == 1 ? any[0] : null;
            }
            default: return null;
        }
    }

    // ================================================================== C#
    // Same facts, same shape, same keys as the VB rig — only the syntax API differs. Kept as a
    // parallel set of passes rather than folded into the VB ones because Roslyn's two syntax trees
    // share no node types; everything downstream of the parse (SqlCollector, AddCommentBlocks,
    // the fact dictionaries) IS shared.

    static readonly Dictionary<string, List<MethodInfo>> CsMethodsByName = new(StringComparer.OrdinalIgnoreCase);
    static readonly Dictionary<string, string> CsTypeKind = new(StringComparer.OrdinalIgnoreCase);
    static readonly Dictionary<(string cls, string member), string> CsMemberType = new();
    static readonly Dictionary<string, Fact> CsConstByName = new(StringComparer.OrdinalIgnoreCase);
    static readonly List<MethodInfo> CsAllMethods = new();

    static bool IsK(SyntaxNode n, CSK k) => n != null && n.RawKind == (int)k;
    static bool IsK(SyntaxToken t, CSK k) => t.RawKind == (int)k;

    static readonly HashSet<int> CsCmpKinds = new()
    {
        (int)CSK.EqualsExpression, (int)CSK.NotEqualsExpression, (int)CSK.LessThanExpression,
        (int)CSK.LessThanOrEqualExpression, (int)CSK.GreaterThanExpression, (int)CSK.GreaterThanOrEqualExpression,
    };
    static readonly HashSet<int> CsArithKinds = new()
    {
        (int)CSK.MultiplyExpression, (int)CSK.DivideExpression, (int)CSK.AddExpression,
        (int)CSK.SubtractExpression, (int)CSK.ModuloExpression,
    };

    static string CsTypeName(CS.TypeSyntax t) => t switch
    {
        null => null,
        CS.QualifiedNameSyntax q => q.Right.Identifier.ValueText,
        CS.IdentifierNameSyntax i => i.Identifier.ValueText,
        CS.GenericNameSyntax g => g.Identifier.ValueText,
        CS.NullableTypeSyntax n => CsTypeName(n.ElementType),
        CS.ArrayTypeSyntax a => CsTypeName(a.ElementType),
        CS.PredefinedTypeSyntax p => p.Keyword.ValueText,
        _ => t.ToString(),
    };

    static string CsEnclosingType(SyntaxNode n) =>
        n.Ancestors().OfType<CS.BaseTypeDeclarationSyntax>().FirstOrDefault()?.Identifier.ValueText;

    static string CsAccess(SyntaxTokenList mods)
    {
        foreach (var m in mods)
            if (IsK(m, CSK.PublicKeyword) || IsK(m, CSK.PrivateKeyword) || IsK(m, CSK.ProtectedKeyword)
                || IsK(m, CSK.InternalKeyword)) return m.ValueText;
        return "private";          // C# default for a type member
    }

    static bool CsHasMod(SyntaxTokenList mods, CSK k) => mods.Any(m => IsK(m, k));

    /// <summary>A string-valued literal token's text, for any C# string form (normal, verbatim, raw).</summary
    static bool CsStringValue(CS.ExpressionSyntax e, out string value)
    {
        value = null;
        if (e is CS.LiteralExpressionSyntax l && l.Token.Value is string s) { value = s; return true; }
        return false;
    }

    static void CsAddMethod(string rel, SyntaxNode node, string name, string kind, SyntaxTokenList mods)
    {
        var cls = CsEnclosingType(node);
        var (s, e) = Span(node);
        var mi = new MethodInfo { Id = $"{cls}.{name}", Class = cls, Name = name, File = rel, Kind = kind,
                                  Access = CsAccess(mods), Start = s, End = e };
        CsAllMethods.Add(mi);
        if (!CsMethodsByName.TryGetValue(name, out var l)) CsMethodsByName[name] = l = new();
        l.Add(mi);
        CsMethods.Add(new Fact
        {
            ["id"] = mi.Id, ["class"] = cls, ["name"] = name, ["kind"] = kind, ["access"] = mi.Access,
            ["file"] = rel, ["start"] = s, ["end"] = e, ["handles"] = new List<string>(),
            ["is_event_handler"] = false, ["static"] = CsHasMod(mods, CSK.StaticKeyword),
            ["async"] = CsHasMod(mods, CSK.AsyncKeyword),
        });
    }

    static MethodInfo CsEnclosingMethod(string rel, SyntaxNode n)
    {
        var line = Span(n).s;
        return CsAllMethods.Where(m => m.File == rel && m.Start <= line && line <= m.End)
                           .OrderBy(m => m.End - m.Start).FirstOrDefault();
    }

    static void CsDeclarations(string rel, SyntaxTree tree)
    {
        var root = tree.GetRoot();
        foreach (var td in root.DescendantNodes().OfType<CS.TypeDeclarationSyntax>())
        {
            var (s, e) = Span(td);
            var name = td.Identifier.ValueText;
            var kind = td switch
            {
                CS.ClassDeclarationSyntax => "class", CS.InterfaceDeclarationSyntax => "interface",
                CS.StructDeclarationSyntax => "struct", CS.RecordDeclarationSyntax => "record",
                _ => "class",
            };
            var inherits = td.BaseList?.Types.Select(t => t.Type.ToString()).ToList() ?? new List<string>();
            CsTypeKind[name] = CsHasMod(td.Modifiers, CSK.StaticKeyword) ? "static class" : kind;
            CsTypes.Add(new Fact
            {
                ["name"] = name, ["kind"] = kind, ["file"] = rel, ["start"] = s, ["end"] = e,
                ["inherits"] = inherits,
                ["partial"] = CsHasMod(td.Modifiers, CSK.PartialKeyword),
                ["static"] = CsHasMod(td.Modifiers, CSK.StaticKeyword),
                // a WinForms/WPF code-behind: the same role signal the VB rig uses
                ["is_form"] = inherits.Any(i => i.EndsWith("Form") || i.EndsWith("Window") || i.EndsWith("Page")),
            });
        }
        foreach (var ed in root.DescendantNodes().OfType<CS.EnumDeclarationSyntax>())
        {
            var (s, e) = Span(ed);
            CsTypeKind[ed.Identifier.ValueText] = "enum";
            CsEnums.Add(new Fact
            {
                ["file"] = rel, ["start"] = s, ["end"] = e, ["name"] = ed.Identifier.ValueText,
                ["owner"] = CsEnclosingType(ed),
                ["members"] = ed.Members.Select(m => new Fact
                {
                    ["name"] = m.Identifier.ValueText, ["value"] = m.EqualsValue?.Value.ToString(),
                    ["line"] = Span(m).s,
                }).ToList(),
            });
        }
        foreach (var md in root.DescendantNodes().OfType<CS.MethodDeclarationSyntax>())
            CsAddMethod(rel, md, md.Identifier.ValueText, "Method", md.Modifiers);
        foreach (var cd in root.DescendantNodes().OfType<CS.ConstructorDeclarationSyntax>())
            CsAddMethod(rel, cd, cd.Identifier.ValueText, "Constructor", cd.Modifiers);
        foreach (var lf in root.DescendantNodes().OfType<CS.LocalFunctionStatementSyntax>())
            CsAddMethod(rel, lf, lf.Identifier.ValueText, "LocalFunction", lf.Modifiers);
        foreach (var pd in root.DescendantNodes().OfType<CS.PropertyDeclarationSyntax>())
        {
            CsAddMethod(rel, pd, pd.Identifier.ValueText, "Property", pd.Modifiers);
            var cls = CsEnclosingType(pd);
            var t = CsTypeName(pd.Type);
            var (s, e) = Span(pd);
            if (cls != null && t != null) CsMemberType[(cls, pd.Identifier.ValueText)] = t;
            CsMembers.Add(new Fact
            {
                ["name"] = pd.Identifier.ValueText, ["owner"] = cls, ["file"] = rel, ["start"] = s,
                ["end"] = e, ["type"] = t, ["kind"] = "property", ["withevents"] = false,
                ["initializer"] = pd.Initializer == null ? null : Clip(pd.Initializer.Value.ToString(), 80),
            });
        }
        foreach (var fd in root.DescendantNodes().OfType<CS.FieldDeclarationSyntax>())
        {
            var cls = CsEnclosingType(fd);
            // `const` and `static readonly` both state a fixed threshold in C#; both are constants.
            bool isConst = CsHasMod(fd.Modifiers, CSK.ConstKeyword)
                           || (CsHasMod(fd.Modifiers, CSK.StaticKeyword) && CsHasMod(fd.Modifiers, CSK.ReadOnlyKeyword));
            var t = CsTypeName(fd.Declaration.Type);
            var (s, e) = Span(fd);
            foreach (var v in fd.Declaration.Variables)
            {
                var nm = v.Identifier.ValueText;
                if (cls != null && t != null) CsMemberType[(cls, nm)] = t;
                if (isConst)
                {
                    var f = new Fact
                    {
                        ["name"] = nm, ["owner"] = cls, ["file"] = rel, ["start"] = s, ["end"] = e,
                        ["type"] = t, ["value"] = v.Initializer?.Value.ToString(), ["text"] = LineText(rel, s),
                    };
                    CsConstants.Add(f); CsConstByName[nm] = f;
                }
                else
                    CsMembers.Add(new Fact
                    {
                        ["name"] = nm, ["owner"] = cls, ["file"] = rel, ["start"] = s, ["end"] = e,
                        ["type"] = t, ["kind"] = "field", ["withevents"] = false,
                        ["initializer"] = v.Initializer == null ? null : Clip(v.Initializer.Value.ToString(), 80),
                    });
            }
        }
        // comments (// , /* */ and /// doc comments), into the shared ledger
        var cm = root.DescendantTrivia()
            .Where(t => t.RawKind == (int)CSK.SingleLineCommentTrivia
                        || t.RawKind == (int)CSK.MultiLineCommentTrivia
                        || t.RawKind == (int)CSK.SingleLineDocumentationCommentTrivia)
            .Select(t =>
            {
                var line = t.GetLocation().GetLineSpan().StartLinePosition.Line + 1;
                var txt = t.ToString();
                if (txt.StartsWith("///")) txt = txt[3..];
                else if (txt.StartsWith("//")) txt = txt[2..];
                else if (txt.StartsWith("/*")) txt = txt[2..].TrimEnd('/', '*');
                return (line, txt);
            }).ToList();
        AddCommentBlocks(rel, cm, "c#");
    }

    static string CsReceiverType(CS.ExpressionSyntax recv, string cls, MethodInfo caller)
    {
        switch (recv)
        {
            case CS.ThisExpressionSyntax: case CS.BaseExpressionSyntax: return cls;
            case CS.IdentifierNameSyntax id:
            {
                var n = id.Identifier.ValueText;
                if (CsTypeKind.ContainsKey(n)) return n;                 // static call: Helper.Foo()
                if (cls != null && CsMemberType.TryGetValue((cls, n), out var t)) return t;
                var methNode = recv.Ancestors().OfType<CS.BaseMethodDeclarationSyntax>().FirstOrDefault()
                               ?? (SyntaxNode)recv.Ancestors().OfType<CS.PropertyDeclarationSyntax>().FirstOrDefault();
                if (methNode != null)
                {
                    var decl = methNode.DescendantNodes().OfType<CS.VariableDeclaratorSyntax>()
                        .FirstOrDefault(d => string.Equals(d.Identifier.ValueText, n, StringComparison.Ordinal));
                    if (decl?.Parent is CS.VariableDeclarationSyntax vds)
                    {
                        var lt = CsTypeName(vds.Type);
                        // `var x = new clsFoo()` -> the creation names the type
                        if (lt == "var" && decl.Initializer?.Value is CS.ObjectCreationExpressionSyntax oc)
                            lt = CsTypeName(oc.Type);
                        if (lt != null && lt != "var") return lt;
                    }
                    var prm = (methNode as CS.BaseMethodDeclarationSyntax)?.ParameterList?.Parameters
                        .FirstOrDefault(p => string.Equals(p.Identifier.ValueText, n, StringComparison.Ordinal));
                    if (prm?.Type != null) return CsTypeName(prm.Type);
                }
                return null;
            }
            case CS.MemberAccessExpressionSyntax ma:
            {
                var inner = ma.Expression == null ? cls : CsReceiverType(ma.Expression, cls, caller);
                var mn = ma.Name.Identifier.ValueText;
                if (inner != null && CsMemberType.TryGetValue((inner, mn), out var t)) return t;
                var any = CsMemberType.Where(kv => string.Equals(kv.Key.member, mn, StringComparison.Ordinal))
                                      .Select(kv => kv.Value).Distinct().ToList();
                return any.Count == 1 ? any[0] : null;
            }
            case CS.InvocationExpressionSyntax inv: return CsReceiverType(inv.Expression, cls, caller);
            case CS.ObjectCreationExpressionSyntax oc2: return CsTypeName(oc2.Type);
            default: return null;
        }
    }

    static bool CsIsConcat(SyntaxNode n) => IsK(n, CSK.AddExpression);

    static string CsFlattenSql(CS.ExpressionSyntax e)
    {
        if (CsStringValue(e, out var s)) return s;
        switch (e)
        {
            case CS.BinaryExpressionSyntax b when CsIsConcat(b):
                return CsFlattenSql(b.Left) + CsFlattenSql(b.Right);
            case CS.ParenthesizedExpressionSyntax p: return CsFlattenSql(p.Expression);
            case CS.InterpolatedStringExpressionSyntax i:
                return string.Concat(i.Contents.Select(c =>
                    c is CS.InterpolatedStringTextSyntax t ? t.TextToken.ValueText : " @csexpr "));
            default: return " @csexpr ";
        }
    }

    static void CsBodies(string rel, SyntaxTree tree)
    {
        var root = tree.GetRoot();

        // ---- calls -------------------------------------------------------------------------
        foreach (var node in root.DescendantNodes())
        {
            CS.ExpressionSyntax target = null;
            if (node is CS.InvocationExpressionSyntax inv) target = inv.Expression;
            else if (node is CS.AssignmentExpressionSyntax asg && IsK(asg, CSK.AddAssignmentExpression)
                     && asg.Right is (CS.IdentifierNameSyntax or CS.MemberAccessExpressionSyntax))
                target = asg.Right;                                  // `btn.Click += OnClick`
            if (target == null) continue;
            var caller = CsEnclosingMethod(rel, node);
            var (name, receiver) = target switch
            {
                CS.IdentifierNameSyntax id => (id.Identifier.ValueText, (CS.ExpressionSyntax)null),
                CS.MemberAccessExpressionSyntax ma => (ma.Name.Identifier.ValueText, ma.Expression),
                CS.GenericNameSyntax g => (g.Identifier.ValueText, (CS.ExpressionSyntax)null),
                _ => (null, null),
            };
            if (name == null || !CsMethodsByName.TryGetValue(name, out var cands)) continue;
            var cls = CsEnclosingType(node);
            MethodInfo callee = null; string how = null;
            if (receiver == null)
            {
                callee = cands.FirstOrDefault(c => c.Class == cls); how = "same-class";
                if (callee == null) { callee = cands.FirstOrDefault(c => CsTypeKind.GetValueOrDefault(c.Class) == "static class"); how = "static-class"; }
                if (callee == null) { callee = cands.Count == 1 ? cands[0] : null; how = "by-name"; }
            }
            else
            {
                var rtype = CsReceiverType(receiver, cls, caller);
                if (rtype != null) { callee = cands.FirstOrDefault(c => string.Equals(c.Class, rtype, StringComparison.Ordinal)); how = "receiver-type"; }
                if (callee == null && cands.Count == 1) { callee = cands[0]; how = "by-name"; }
            }
            if (callee == null) continue;
            if (caller != null && callee.Id == caller.Id && node is not CS.InvocationExpressionSyntax) continue;
            var line = Span(node).s;
            CsCalls.Add(new Fact
            {
                ["from"] = caller?.Id ?? $"{cls}.<field-init>", ["to"] = callee.Id, ["file"] = rel,
                ["line"] = line, ["callee_file"] = callee.File, ["resolution"] = how,
                ["text"] = LineText(rel, line),
            });
        }

        // ---- stored-procedure references --------------------------------------------------
        foreach (var lit in root.DescendantNodes().OfType<CS.LiteralExpressionSyntax>())
        {
            if (lit.Token.Value is not string v) continue;
            var m = Regex.Match(v, @"^\s*(?:dbo\.)?([A-Za-z_]\w*)\s*$");
            if (!m.Success) continue;
            if (!KnownProcs.Contains(m.Groups[1].Value)
                && !Regex.IsMatch(m.Groups[1].Value, @"^(usp|sp|bsp|vsp|proc)_", RegexOptions.IgnoreCase)) continue;
            var line = Span(lit).s;
            CsProcRefs.Add(new Fact
            {
                ["from"] = CsEnclosingMethod(rel, lit)?.Id, ["file"] = rel, ["line"] = line,
                ["proc"] = m.Groups[1].Value,
                ["defined_in_corpus"] = KnownProcs.Contains(m.Groups[1].Value), ["text"] = LineText(rel, line),
            });
        }

        // ---- inline SQL -------------------------------------------------------------------
        foreach (var e in root.DescendantNodes().OfType<CS.ExpressionSyntax>())
        {
            bool isStr = e is CS.LiteralExpressionSyntax l0 && l0.Token.Value is string;
            bool isInterp = e is CS.InterpolatedStringExpressionSyntax;
            bool isConcat = e is CS.BinaryExpressionSyntax b0 && CsIsConcat(b0);
            if (!isStr && !isInterp && !isConcat) continue;
            if (e.Parent is CS.BinaryExpressionSyntax pb && CsIsConcat(pb)) continue;   // handled at the root
            if (e.Parent is CS.ParenthesizedExpressionSyntax) continue;
            var sql = CsFlattenSql(e);
            if (!SqlStart.IsMatch(sql) || !Regex.IsMatch(sql, @"\b(FROM|INTO|SET|VALUES|WHERE)\b", RegexOptions.IgnoreCase)) continue;
            var (s, en) = Span(e);
            var meth = CsEnclosingMethod(rel, e);
            string varName = e.Parent switch
            {
                CS.EqualsValueClauseSyntax ev when ev.Parent is CS.VariableDeclaratorSyntax vd => vd.Identifier.ValueText,
                CS.AssignmentExpressionSyntax a when a.Left is CS.IdentifierNameSyntax idn => idn.Identifier.ValueText,
                _ => null,
            };
            var frags = new List<Fact>();
            if (varName != null)
            {
                SyntaxNode scope = e.Ancestors().OfType<CS.BaseMethodDeclarationSyntax>().FirstOrDefault();
                foreach (var a in scope?.DescendantNodes().OfType<CS.AssignmentExpressionSyntax>() ?? Enumerable.Empty<CS.AssignmentExpressionSyntax>())
                {
                    if (!IsK(a, CSK.AddAssignmentExpression)) continue;
                    if (a.Left is not CS.IdentifierNameSyntax lid || !string.Equals(lid.Identifier.ValueText, varName, StringComparison.Ordinal)) continue;
                    if (a.SpanStart < e.SpanStart) continue;
                    var (fs, fe) = Span(a);
                    var cond = a.Ancestors().TakeWhile(x => x != scope)
                                .Any(x => x is CS.IfStatementSyntax or CS.SwitchStatementSyntax or CS.ConditionalExpressionSyntax);
                    frags.Add(new Fact { ["start"] = fs, ["end"] = fe, ["conditional"] = cond, ["sql"] = CsFlattenSql(a.Right) });
                    sql += CsFlattenSql(a.Right);
                    en = Math.Max(en, fe);
                }
            }
            var (frag, errs) = ParseSql(sql);
            var c = new SqlCollector(s, en);
            frag?.Accept(c);
            var f = new Fact
            {
                ["from"] = meth?.Id, ["file"] = rel, ["start"] = s, ["end"] = en, ["variable"] = varName,
                ["sql"] = Clip(sql, 600), ["parse_ok"] = errs.Count == 0, ["appended_fragments"] = frags,
            };
            if (errs.Count > 0) f["parse_error"] = errs[0].Message;
            foreach (var kv in c.Summary()) if (kv.Key is not ("exec_calls" or "returns" or "raises" or "literal_assigns")) f[kv.Key] = kv.Value;
            CsInlineSql.Add(f);
        }

        // ---- literals in a decision context, and constant uses ----------------------------
        bool CsInConstDecl(SyntaxNode n) =>
            n.Ancestors().OfType<CS.FieldDeclarationSyntax>().Any(fd => CsHasMod(fd.Modifiers, CSK.ConstKeyword)
                                                                       || CsHasMod(fd.Modifiers, CSK.ReadOnlyKeyword)) ||
            n.Ancestors().OfType<CS.LocalDeclarationStatementSyntax>().Any(ld => CsHasMod(ld.Modifiers, CSK.ConstKeyword));
        foreach (var lit in root.DescendantNodes().OfType<CS.LiteralExpressionSyntax>())
        {
            bool num = IsK(lit.Token, CSK.NumericLiteralToken);
            bool str = lit.Token.Value is string sv && sv.Length is > 0 and <= 12;
            if (!num && !str) continue;
            if (CsInConstDecl(lit)) continue;
            var ctx = CsLiteralContext(lit);
            if (ctx == null) continue;
            var val = num ? Convert.ToString(lit.Token.Value, CultureInfo.InvariantCulture) : (string)lit.Token.Value;
            if (ctx == "arithmetic" && (val == "0" || val == "1" || str)) continue;
            var line = Span(lit).s;
            CsLiterals.Add(new Fact
            {
                ["file"] = rel, ["line"] = line, ["from"] = CsEnclosingMethod(rel, lit)?.Id,
                ["kind"] = num ? "number" : "string", ["value"] = val, ["context"] = ctx,
                ["text"] = LineText(rel, line),
            });
        }
        foreach (var id in root.DescendantNodes().OfType<CS.IdentifierNameSyntax>())
        {
            var nm = id.Identifier.ValueText;
            if (!CsConstByName.TryGetValue(nm, out var cdef)) continue;
            if (CsInConstDecl(id)) continue;
            var line = Span(id).s;
            var cmp = id.Ancestors().TakeWhile(a => a is not CS.StatementSyntax)
                        .OfType<CS.BinaryExpressionSyntax>().FirstOrDefault(b => CsCmpKinds.Contains(b.RawKind));
            CsConstUses.Add(new Fact
            {
                ["const"] = nm, ["const_file"] = cdef["file"], ["file"] = rel, ["line"] = line,
                ["from"] = CsEnclosingMethod(rel, id)?.Id,
                ["comparison"] = cmp == null ? null : cmp.OperatorToken.ValueText,
                ["text"] = LineText(rel, line),
            });
        }
    }

    static string CsLiteralContext(SyntaxNode lit)
    {
        foreach (var a in lit.Ancestors())
        {
            if (a is CS.StatementSyntax && a is not CS.ReturnStatementSyntax) return null;
            if (a is CS.BinaryExpressionSyntax b && CsCmpKinds.Contains(b.RawKind)) return "comparison";
            if (a is CS.SwitchLabelSyntax) return "case";
            if (a is CS.ReturnStatementSyntax) return "return";
            if (a is CS.BinaryExpressionSyntax b2 && CsArithKinds.Contains(b2.RawKind)) return "arithmetic";
            if (a is CS.ArgumentListSyntax) return null;
        }
        return null;
    }

    static string CsOpSym(int rawKind) => rawKind switch
    {
        var k when k == (int)CSK.EqualsExpression => "=",
        var k when k == (int)CSK.NotEqualsExpression => "<>",
        var k when k == (int)CSK.LessThanExpression => "<",
        var k when k == (int)CSK.LessThanOrEqualExpression => "<=",
        var k when k == (int)CSK.GreaterThanExpression => ">",
        var k when k == (int)CSK.GreaterThanOrEqualExpression => ">=",
        _ => "?",
    };

    static string CsConstValue(string name) =>
        CsConstByName.TryGetValue(name, out var c) ? (string)c["value"] : null;

    /// <summary>Operand facts: identifiers (and string-indexer / parameter names), literal or constant value.</summary>
    static Fact CsOperand(CS.ExpressionSyntax e)
    {
        var ids = new List<string>(); var consts = new List<string>(); string value = null;
        foreach (var n in e.DescendantNodesAndSelf())
        {
            if (n is CS.IdentifierNameSyntax id)
            {
                var nm = id.Identifier.ValueText;
                if (CsConstByName.ContainsKey(nm)) consts.Add(nm);
                ids.Add(nm);
            }
            else if (n is CS.LiteralExpressionSyntax l && l.Token.Value is string s
                     && (l.Parent is CS.ArgumentSyntax or CS.BracketedArgumentListSyntax)
                     && Regex.IsMatch(s, @"^@?[A-Za-z_]\w{1,40}$"))
                ids.Add(s.TrimStart('@'));
        }
        var core = e;
        while (core is CS.ParenthesizedExpressionSyntax pe) core = pe.Expression;
        if (core is CS.LiteralExpressionSyntax lit)
            value = IsK(lit.Token, CSK.NumericLiteralToken) ? NormNumText(lit.Token.Text)
                                                            : lit.Token.Value as string;
        else if (core is CS.IdentifierNameSyntax cid && CsConstByName.ContainsKey(cid.Identifier.ValueText))
        {
            var cv = CsConstValue(cid.Identifier.ValueText) ?? "";
            value = cv.StartsWith("\"") ? cv.Trim('"') : (NormNumText(cv) ?? cv);
        }
        else if (core is CS.PrefixUnaryExpressionSyntax u && IsK(u, CSK.UnaryMinusExpression)
                 && u.Operand is CS.LiteralExpressionSyntax ul)
            value = "-" + NormNumText(ul.Token.Text);
        else if (core is CS.MemberAccessExpressionSyntax ema
                 && CsConstByName.ContainsKey(ema.Name.Identifier.ValueText))
        {
            var cv = CsConstValue(ema.Name.Identifier.ValueText) ?? "";
            value = cv.StartsWith("\"") ? cv.Trim('"') : (NormNumText(cv) ?? cv);
        }
        return new Fact { ["text"] = Clip(e.ToString(), 120),
                          ["ids"] = ids.Distinct(StringComparer.OrdinalIgnoreCase).ToList(),
                          ["consts"] = consts.Distinct().ToList(), ["value"] = value };
    }

    static bool CsIsInvokedName(SyntaxToken t)
    {
        if (t.Parent is not CS.IdentifierNameSyntax p) return false;
        if (p.Parent is CS.InvocationExpressionSyntax inv && inv.Expression == p) return true;
        if (p.Parent is CS.MemberAccessExpressionSyntax ma && ma.Name == p
            && ma.Parent is CS.InvocationExpressionSyntax) return true;
        return false;
    }

    /// <summary>k1: names kept (constants -> values); k2: simple locals/params/fields -> $.</summary>
    static (string k1, string k2, int n) CsFp(SyntaxNode node)
    {
        var a = new List<string>(); var b = new List<string>();
        SyntaxToken prev = default;
        foreach (var t in node.DescendantTokens())
        {
            if (IsK(t, CSK.ThisKeyword)) { prev = t; continue; }
            if (IsK(t, CSK.DotToken) && IsK(prev, CSK.ThisKeyword)) { prev = t; continue; }
            if (IsK(t, CSK.IdentifierToken))
            {
                var nm = t.ValueText;
                if (CsConstByName.ContainsKey(nm)) { var v = CsConstValue(nm) ?? nm; a.Add("#" + v); b.Add("#" + v); }
                else
                {
                    var afterDot = IsK(prev, CSK.DotToken);
                    a.Add(nm.ToLowerInvariant());
                    b.Add(afterDot || CsIsInvokedName(t) ? nm.ToLowerInvariant() : "$");
                }
            }
            else if (IsK(t, CSK.NumericLiteralToken)) { var v = NormNumText(t.Text) ?? t.Text; a.Add(v); b.Add(v); }
            else if (t.IsKeyword()) { a.Add(t.ValueText.ToLowerInvariant()); b.Add(t.ValueText.ToLowerInvariant()); }
            else { a.Add(t.Text); b.Add(t.Text); }
            prev = t;
        }
        return (string.Join(" ", a), string.Join(" ", b), a.Count);
    }

    static void CsAddFp(string rel, SyntaxNode n, string kind)
    {
        if (n == null) return;
        var (k1, k2, cnt) = CsFp(n);
        if (cnt < 5) return;
        var (s, e) = Span(n);
        CsFingerprints.Add(new Fact
        {
            ["file"] = rel, ["from"] = CsEnclosingMethod(rel, n)?.Id, ["kind"] = kind, ["start"] = s,
            ["end"] = e, ["k1"] = k1, ["k2"] = k2, ["ntok"] = cnt, ["text"] = Clip(n.ToString(), 160),
        });
    }

    static void CsV1(string rel, SyntaxTree tree)
    {
        var root = tree.GetRoot();
        bool InConstDecl(SyntaxNode n) =>
            n.Ancestors().OfType<CS.FieldDeclarationSyntax>().Any(fd => CsHasMod(fd.Modifiers, CSK.ConstKeyword)
                                                                        || CsHasMod(fd.Modifiers, CSK.ReadOnlyKeyword));

        // ---- comparisons, operands split
        foreach (var b in root.DescendantNodes().OfType<CS.BinaryExpressionSyntax>().Where(x => CsCmpKinds.Contains(x.RawKind)))
        {
            if (InConstDecl(b)) continue;
            var ctx = b.Ancestors().FirstOrDefault(a => a is CS.StatementSyntax) switch
            {
                CS.IfStatementSyntax => "if",
                CS.ReturnStatementSyntax => "return",
                CS.WhileStatementSyntax or CS.DoStatementSyntax or CS.ForStatementSyntax => "loop",
                _ => "expr",
            };
            CsComparisons.Add(new Fact
            {
                ["file"] = rel, ["line"] = Span(b).s, ["from"] = CsEnclosingMethod(rel, b)?.Id,
                ["op"] = CsOpSym(b.RawKind), ["left"] = CsOperand(b.Left), ["right"] = CsOperand(b.Right),
                ["context"] = ctx, ["text"] = LineText(rel, Span(b).s),
            });
        }
        // ---- switch labels as comparisons against the selector (statement and expression forms)
        foreach (var sw in root.DescendantNodes().OfType<CS.SwitchStatementSyntax>())
            foreach (var sec in sw.Sections)
                foreach (var lab in sec.Labels)
                {
                    CS.ExpressionSyntax val = lab switch
                    {
                        CS.CaseSwitchLabelSyntax cs1 => cs1.Value,
                        CS.CasePatternSwitchLabelSyntax cp when cp.Pattern is CS.ConstantPatternSyntax cpat => cpat.Expression,
                        _ => null,
                    };
                    if (val == null) continue;
                    CsComparisons.Add(new Fact
                    {
                        ["file"] = rel, ["line"] = Span(lab).s, ["from"] = CsEnclosingMethod(rel, lab)?.Id,
                        ["op"] = "=", ["left"] = CsOperand(sw.Expression), ["right"] = CsOperand(val),
                        ["context"] = "case", ["text"] = LineText(rel, Span(lab).s),
                    });
                }
        foreach (var sw in root.DescendantNodes().OfType<CS.SwitchExpressionSyntax>())
        {
            foreach (var arm in sw.Arms)
                if (arm.Pattern is CS.ConstantPatternSyntax cpat)
                    CsComparisons.Add(new Fact
                    {
                        ["file"] = rel, ["line"] = Span(arm).s, ["from"] = CsEnclosingMethod(rel, arm)?.Id,
                        ["op"] = "=", ["left"] = CsOperand(sw.GoverningExpression), ["right"] = CsOperand(cpat.Expression),
                        ["context"] = "case", ["text"] = LineText(rel, Span(arm).s),
                    });
            CsAddFp(rel, sw, "switch");
        }
        foreach (var sw in root.DescendantNodes().OfType<CS.SwitchStatementSyntax>()) CsAddFp(rel, sw, "switch");

        // ---- fingerprints
        foreach (var n in root.DescendantNodes())
        {
            switch (n)
            {
                case CS.IfStatementSyntax i: CsAddFp(rel, i.Condition, "if"); break;
                case CS.WhileStatementSyntax w: CsAddFp(rel, w.Condition, "while"); break;
                case CS.ReturnStatementSyntax r when r.Expression != null: CsAddFp(rel, r.Expression, "return"); break;
                case CS.ExpressionStatementSyntax es when es.Expression is CS.AssignmentExpressionSyntax a
                        && a.Right is CS.BinaryExpressionSyntax or CS.InvocationExpressionSyntax:
                    CsAddFp(rel, es, "assign"); break;
                case CS.LocalDeclarationStatementSyntax ld
                        when ld.Declaration.Variables.Any(d => d.Initializer?.Value is CS.BinaryExpressionSyntax or CS.InvocationExpressionSyntax):
                    foreach (var d in ld.Declaration.Variables.Where(d => d.Initializer != null))
                        CsAddFp(rel, d.Initializer.Value, "init");
                    break;
            }
        }
        foreach (var mb in root.DescendantNodes().OfType<CS.BaseMethodDeclarationSyntax>())
        {
            var stmts = mb.Body?.Statements;
            if (stmts == null || stmts.Value.Count < 2) continue;
            var fps = stmts.Value.Select(st => CsFp(st)).ToList();
            var (s, e) = Span(mb);
            CsFingerprints.Add(new Fact
            {
                ["file"] = rel, ["from"] = CsEnclosingMethod(rel, mb)?.Id, ["kind"] = "method-body",
                ["start"] = s, ["end"] = e, ["k1"] = string.Join(" ", fps.Select(x => x.k1)),
                ["k2"] = string.Join(" ", fps.Select(x => x.k2)), ["ntok"] = fps.Sum(x => x.n),
                ["text"] = Clip(mb.ToString(), 160),
            });
        }

        // ---- string literals naming DB columns / proc params; every literal per method
        foreach (var l in root.DescendantNodes().OfType<CS.LiteralExpressionSyntax>())
        {
            if (InConstDecl(l)) continue;
            var line = Span(l).s; var from = CsEnclosingMethod(rel, l)?.Id;
            if (l.Token.Value is string v)
            {
                if (v.Length > 0 && v.Length <= 200)
                    CsUnitLiterals.Add(new Fact { ["file"] = rel, ["line"] = line, ["from"] = from, ["kind"] = "string", ["value"] = v });
                var bare = v.TrimStart('@');
                if (Regex.IsMatch(v, @"^@?[A-Za-z_]\w*$") && KnownColumns.Contains(bare)
                    && (l.Parent is CS.ArgumentSyntax or CS.BracketedArgumentListSyntax
                        || l.Parent?.Parent is CS.BracketedArgumentListSyntax))
                {
                    // `row["Col"]` / `cmd.Parameters.AddWithValue("@Col", x)` / `rdr.GetString("Col")`
                    var inv = l.Ancestors().OfType<CS.InvocationExpressionSyntax>().FirstOrDefault();
                    var ea = l.Ancestors().OfType<CS.ElementAccessExpressionSyntax>().FirstOrDefault();
                    string target = inv?.Expression switch
                    {
                        CS.MemberAccessExpressionSyntax ma => ma.Name.Identifier.ValueText,
                        CS.IdentifierNameSyntax id => id.Identifier.ValueText, _ => null,
                    };
                    bool isWrite = ea != null && ea.Parent is CS.AssignmentExpressionSyntax asg && asg.Left == ea;
                    var how = v.StartsWith("@") || (target != null && Regex.IsMatch(target, "^(AddWithValue|Add|Parameters)$"))
                              ? "sql-param" : isWrite ? "write" : "read";
                    CsColumnRefs.Add(new Fact { ["file"] = rel, ["line"] = line, ["from"] = from, ["name"] = bare,
                                                ["how"] = how, ["via"] = target ?? (ea != null ? "indexer" : null),
                                                ["text"] = LineText(rel, line) });
                }
            }
            else if (IsK(l.Token, CSK.NumericLiteralToken))
            {
                var v2 = NormNumText(l.Token.Text);
                if (v2 != null)
                    CsUnitLiterals.Add(new Fact { ["file"] = rel, ["line"] = line, ["from"] = from, ["kind"] = "number", ["value"] = v2 });
            }
        }
        foreach (var id in root.DescendantNodes().OfType<CS.IdentifierNameSyntax>())
        {
            var nm = id.Identifier.ValueText;
            if (!CsConstByName.ContainsKey(nm) || InConstDecl(id)) continue;
            var cv = CsConstValue(nm) ?? "";
            CsUnitLiterals.Add(new Fact { ["file"] = rel, ["line"] = Span(id).s, ["from"] = CsEnclosingMethod(rel, id)?.Id,
                                          ["kind"] = cv.StartsWith("\"") ? "string" : "number",
                                          ["value"] = cv.StartsWith("\"") ? cv.Trim('"') : (NormNumText(cv) ?? cv),
                                          ["const"] = nm });
        }

        // ---- locals initialised / assigned from a column-ish expression
        void Alias(SyntaxNode at, string name, CS.ExpressionSyntax value)
        {
            if (value == null) return;
            var ali = new List<string>(); string dflt = null;
            foreach (var n in value.DescendantNodesAndSelf())
            {
                if (n is CS.LiteralExpressionSyntax l && l.Token.Value is string s
                    && Regex.IsMatch(s, @"^@?[A-Za-z_]\w{1,40}$")) ali.Add(s.TrimStart('@'));
                else if (n is CS.LiteralExpressionSyntax ln && IsK(ln.Token, CSK.NumericLiteralToken))
                    dflt ??= NormNumText(ln.Token.Text);
                else if (n is CS.IdentifierNameSyntax idn && CsConstByName.ContainsKey(idn.Identifier.ValueText))
                    ali.Add(idn.Identifier.ValueText);
            }
            if (value is CS.MemberAccessExpressionSyntax mae) ali.Add(mae.Name.Identifier.ValueText);
            if (value is CS.IdentifierNameSyntax ide) ali.Add(ide.Identifier.ValueText);
            if (ali.Count == 0) return;
            CsLocalAliases.Add(new Fact { ["file"] = rel, ["line"] = Span(at).s, ["from"] = CsEnclosingMethod(rel, at)?.Id,
                                          ["name"] = name, ["aliases"] = ali.Distinct(StringComparer.OrdinalIgnoreCase).ToList(),
                                          ["default"] = dflt, ["text"] = LineText(rel, Span(at).s) });
        }
        foreach (var vd in root.DescendantNodes().OfType<CS.VariableDeclaratorSyntax>())
            if (vd.Initializer != null && vd.Parent?.Parent is CS.LocalDeclarationStatementSyntax)
                Alias(vd, vd.Identifier.ValueText, vd.Initializer.Value);
        foreach (var a in root.DescendantNodes().OfType<CS.AssignmentExpressionSyntax>())
            if (IsK(a, CSK.SimpleAssignmentExpression) && a.Left is CS.IdentifierNameSyntax ln2)
                Alias(a, ln2.Identifier.ValueText, a.Right);

        // ---- attributes
        foreach (var at in root.DescendantNodes().OfType<CS.AttributeSyntax>())
        {
            var owner = at.Ancestors().FirstOrDefault(a => a is CS.MemberDeclarationSyntax);
            string target = owner switch
            {
                CS.BaseTypeDeclarationSyntax td => td.Identifier.ValueText,
                CS.MethodDeclarationSyntax md => md.Identifier.ValueText,
                CS.PropertyDeclarationSyntax pd => pd.Identifier.ValueText,
                CS.FieldDeclarationSyntax fd => string.Join(",", fd.Declaration.Variables.Select(v => v.Identifier.ValueText)),
                _ => null,
            };
            CsAttributes.Add(new Fact { ["file"] = rel, ["line"] = Span(at).s, ["name"] = at.Name.ToString(),
                                        ["args"] = at.ArgumentList?.ToString(), ["target"] = target,
                                        ["owner"] = CsEnclosingType(at) });
        }

        // ---- external I/O (same API table as the VB rig)
        foreach (var n in root.DescendantNodes())
        {
            string api = n switch
            {
                CS.ObjectCreationExpressionSyntax oc => CsTypeName(oc.Type),
                CS.InvocationExpressionSyntax inv => inv.Expression switch
                {
                    CS.MemberAccessExpressionSyntax ma => ma.Name.Identifier.ValueText,
                    CS.IdentifierNameSyntax id => id.Identifier.ValueText,
                    CS.GenericNameSyntax g => g.Identifier.ValueText, _ => null,
                },
                CS.MemberAccessExpressionSyntax ma2 when ma2.Parent is not CS.InvocationExpressionSyntax &&
                    Regex.IsMatch(ma2.Name.Identifier.ValueText, "^(AppSettings|ConnectionStrings)$") => ma2.Name.Identifier.ValueText,
                _ => null,
            };
            if (api == null || CsMethodsByName.ContainsKey(api)) continue;
            foreach (var (rx, kind) in IoApis)
            {
                if (!Regex.IsMatch(api, rx)) continue;
                if (kind == "file" && api is "WriteLine" or "Copy" or "Move" &&
                    !(n is CS.InvocationExpressionSyntax iv && iv.Expression is CS.MemberAccessExpressionSyntax m2
                      && Regex.IsMatch(m2.Expression.ToString(), @"(File|Writer|sw|fs|IO)\b", RegexOptions.IgnoreCase))) break;
                if (kind == "email" && api == "Send" && !(n is CS.InvocationExpressionSyntax iv2 && Regex.IsMatch(iv2.Expression.ToString(), "(smtp|mail)", RegexOptions.IgnoreCase))) break;
                if (kind == "process" && api == "Start" && !(n is CS.InvocationExpressionSyntax iv3 && iv3.Expression.ToString().Contains("Process"))) break;
                var line = Span(n).s;
                CsIoCalls.Add(new Fact { ["file"] = rel, ["line"] = line, ["from"] = CsEnclosingMethod(rel, n)?.Id,
                                         ["kind"] = kind, ["api"] = api, ["text"] = LineText(rel, line) });
                break;
            }
        }
        foreach (var l in root.DescendantNodes().OfType<CS.LiteralExpressionSyntax>())
        {
            if (l.Token.Value is not string v) continue;
            string kind = null;
            if (Regex.IsMatch(v, @"^(https?|ftp|net\.tcp)://", RegexOptions.IgnoreCase)) kind = "url";
            else if (Regex.IsMatch(v, @"^\s*[\{\[]\s*""")) kind = "json-literal";
            else if (Regex.IsMatch(v, @"\.(csv|xml|json|txt|xls|xlsx|pdf|rpt|dat|ini|config)$", RegexOptions.IgnoreCase)) kind = "file-name";
            if (kind == null) continue;
            var line = Span(l).s;
            CsIoCalls.Add(new Fact { ["file"] = rel, ["line"] = line, ["from"] = CsEnclosingMethod(rel, l)?.Id,
                                     ["kind"] = kind, ["api"] = Clip(v, 120), ["text"] = LineText(rel, line) });
        }
    }

    // ================================================================== derived cross-file facts
    static Fact Derive()
    {
        var d = new Fact();

        // proc index: definition + callers (VB, C# and SQL)
        var procIdx = new List<Fact>();
        var clrProcRefs = VbProcRefs.Concat(CsProcRefs).ToList();
        var allProcNames = SqlProcs.Select(p => (string)p["name"]).Concat(clrProcRefs.Select(r => (string)r["proc"]))
            .Concat(SqlProcs.SelectMany(p => ((List<Fact>)p["exec_calls"]).Select(x => (string)x["proc"])))
            .Distinct(StringComparer.OrdinalIgnoreCase).OrderBy(x => x).ToList();
        foreach (var pn in allProcNames)
        {
            var def = SqlProcs.FirstOrDefault(p => string.Equals((string)p["name"], pn, StringComparison.OrdinalIgnoreCase));
            var vbCallers = VbProcRefs.Where(r => string.Equals((string)r["proc"], pn, StringComparison.OrdinalIgnoreCase))
                .Select(r => new Fact { ["from"] = r["from"], ["file"] = r["file"], ["line"] = r["line"] }).ToList();
            var csCallers = CsProcRefs.Where(r => string.Equals((string)r["proc"], pn, StringComparison.OrdinalIgnoreCase))
                .Select(r => new Fact { ["from"] = r["from"], ["file"] = r["file"], ["line"] = r["line"] }).ToList();
            var sqlCallers = SqlProcs.Concat(SqlTriggers).SelectMany(p => ((List<Fact>)p["exec_calls"])
                    .Where(x => string.Equals((string)x["proc"], pn, StringComparison.OrdinalIgnoreCase))
                    .Select(x => new Fact { ["from"] = p["name"], ["file"] = p["file"], ["line"] = x["line"] })).ToList();
            procIdx.Add(new Fact
            {
                ["proc"] = pn, ["defined"] = def != null, ["file"] = def?["file"], ["start"] = def?["start"], ["end"] = def?["end"],
                ["vb_callers"] = vbCallers, ["cs_callers"] = csCallers, ["sql_callers"] = sqlCallers,
            });
        }
        d["proc_index"] = procIdx;
        d["missing_procs"] = procIdx.Where(p => !(bool)p["defined"]).Select(p => p["proc"]).ToList();

        // VB/C# -> proc edges at method level, so callers of a proc wrapper are visible
        // method reachability (one list per language; the keys stay stable for existing consumers)
        static List<Fact> NoCallers(List<MethodInfo> methods, List<Fact> calls)
        {
            var incoming = calls.GroupBy(c => (string)c["to"]).ToDictionary(g => g.Key, g => g.Count());
            return methods
                .Where(m => m.Handles.Count == 0 && m.Kind is not ("Constructor" or "Property")
                            && !incoming.ContainsKey(m.Id)
                            && !Regex.IsMatch(m.Name, @"^(InitializeComponent|Dispose|Main)$"))
                .Select(m => new Fact
                {
                    ["method"] = m.Id, ["file"] = m.File, ["start"] = m.Start, ["end"] = m.End, ["access"] = m.Access,
                    ["note"] = m.Access is "Private" or "private" ? "private, no caller in corpus"
                                                                  : "public, no caller in corpus (could be called from outside the corpus)",
                }).ToList();
        }
        d["vb_methods_without_callers"] = NoCallers(AllMethods, VbCalls);
        d["cs_methods_without_callers"] = NoCallers(CsAllMethods, CsCalls);
        d["vb_event_handlers"] = AllMethods.Where(m => m.Handles.Count > 0)
            .Select(m => new Fact { ["method"] = m.Id, ["file"] = m.File, ["start"] = m.Start, ["end"] = m.End, ["handles"] = m.Handles }).ToList();

        // table index
        var tbl = new Dictionary<string, Fact>(StringComparer.OrdinalIgnoreCase);
        Fact T(string n)
        {
            if (!tbl.TryGetValue(n, out var f))
                tbl[n] = f = new Fact { ["table"] = n, ["ddl"] = null, ["read_by"] = new List<Fact>(), ["written_by"] = new List<Fact>(), ["triggers"] = new List<Fact>() };
            return f;
        }
        foreach (var t in SqlTables) T((string)t["name"])["ddl"] = new Fact { ["file"] = t["file"], ["start"] = t["start"], ["end"] = t["end"] };
        void AddAccess(IEnumerable<Fact> units, string kind, Func<Fact, string> unitName)
        {
            foreach (var u in units)
            {
                foreach (var r in (List<Fact>)u["reads"])
                    ((List<Fact>)T((string)r["table"])["read_by"]).Add(new Fact { ["unit"] = unitName(u), ["kind"] = kind, ["file"] = u["file"], ["lines"] = r["lines"] });
                foreach (var w in (List<Fact>)u["writes"])
                    ((List<Fact>)T((string)w["table"])["written_by"]).Add(new Fact { ["unit"] = unitName(u), ["kind"] = kind, ["op"] = w["op"], ["file"] = u["file"], ["lines"] = w["lines"] });
            }
        }
        AddAccess(SqlProcs, "proc", u => (string)u["name"]);
        AddAccess(SqlTriggers, "trigger", u => (string)u["name"]);
        AddAccess(SqlScripts, "script", u => (string)u["file"]);
        AddAccess(VbInlineSql, "vb-inline-sql", u => (string)u["from"]);
        AddAccess(CsInlineSql, "cs-inline-sql", u => (string)u["from"]);
        foreach (var t in SqlTriggers)
            if (t["table"] is string tn)
                ((List<Fact>)T(tn)["triggers"]).Add(new Fact { ["trigger"] = t["name"], ["file"] = t["file"], ["start"] = t["start"], ["end"] = t["end"], ["events"] = t["events"] });
        d["table_index"] = tbl.Values.OrderBy(f => (string)f["table"]).ToList();

        // VB constants whose names match SQL columns / proc params (possible duplicated rule across layers)
        var sqlNames = new List<(string norm, Fact where)>();
        foreach (var t in SqlTables)
            foreach (var c in (List<Fact>)t["columns"])
                sqlNames.Add((NormName((string)c["name"]), new Fact { ["kind"] = "column", ["name"] = $"{t["name"]}.{c["name"]}", ["file"] = t["file"], ["line"] = c["line"] }));
        foreach (var p in SqlProcs)
            foreach (var prm in (List<Fact>)p["params"])
                sqlNames.Add((NormName((string)prm["name"]), new Fact { ["kind"] = "param", ["name"] = $"{p["name"]}.{prm["name"]}", ["file"] = p["file"], ["line"] = prm["line"] }));
        var xl = new List<Fact>();
        foreach (var c in VbConstants.Concat(CsConstants))
        {
            var n = NormName((string)c["name"]);
            var n2 = Regex.Replace(n, "^(dflt|default|max|min)", "");
            var hits = sqlNames.Where(s => s.norm == n || (n2.Length >= 6 && s.norm.EndsWith(n2))).Select(s => s.where).ToList();
            if (hits.Count > 0)
                // key kept as "vb_const" so existing consumers (facts_block, facts_post) keep working;
                // `lang` says which CLR language it came from.
                xl.Add(new Fact { ["vb_const"] = c["name"], ["lang"] = CsConstants.Contains(c) ? "c#" : "vb.net",
                                  ["value"] = c["value"], ["file"] = c["file"], ["start"] = c["start"],
                                  ["end"] = c["end"], ["sql_matches"] = hits });
        }
        d["cross_layer_name_matches"] = xl;

        // numeric values (>= 2) that appear in both VB and SQL
        static string NormNum(string v)
        {
            v = (v ?? "").TrimEnd('D', 'd', 'R', 'r', 'F', 'f', '@', '#', '!', 'M', 'm', 'U', 'u');
            return decimal.TryParse(v, NumberStyles.Any, CultureInfo.InvariantCulture, out var x) ? x.ToString("0.####", CultureInfo.InvariantCulture) : null;
        }
        var vbNums = VbConstants.Concat(CsConstants).Select(c => (v: NormNum((string)c["value"]), f: new Fact { ["file"] = c["file"], ["line"] = c["start"], ["what"] = $"Const {c["name"]}", ["text"] = c["text"] }))
            .Concat(VbLiterals.Concat(CsLiterals).Where(l => (string)l["kind"] == "number").Select(l => (v: NormNum((string)l["value"]), f: new Fact { ["file"] = l["file"], ["line"] = l["line"], ["what"] = $"literal in {l["from"]} ({l["context"]})", ["text"] = l["text"] })))
            .Where(x => x.v != null && decimal.Parse(x.v, CultureInfo.InvariantCulture) is var dv && Math.Abs(dv) >= 2).ToList();
        var sqlNums = SqlNumericLiterals.Select(l => (v: NormNum((string)l["value"]), f: new Fact { ["file"] = l["file"], ["line"] = l["line"], ["what"] = $"literal in {l["unit"]}", ["text"] = l["context"] }))
            .Where(x => x.v != null).ToList();
        d["shared_numeric_values"] = vbNums.Select(x => x.v).Distinct().Where(v => sqlNums.Any(s => s.v == v))
            .OrderBy(v => decimal.Parse(v, CultureInfo.InvariantCulture))
            .Select(v => new Fact { ["value"] = v, ["vb"] = vbNums.Where(x => x.v == v).Select(x => x.f).ToList(), ["sql"] = sqlNums.Where(x => x.v == v).Select(x => x.f).ToList() })
            .ToList();

        // comments that name an identifier defined in another file
        var defs = new List<(string name, string file, string kind)>();
        defs.AddRange(AllMethods.Where(m => m.Name.Length > 5).Select(m => (m.Name, m.File, "vb-method")));
        defs.AddRange(VbTypes.Select(t => ((string)t["name"], (string)t["file"], "vb-type")));
        defs.AddRange(VbConstants.Select(c => ((string)c["name"], (string)c["file"], "vb-const")));
        defs.AddRange(CsAllMethods.Where(m => m.Name.Length > 5).Select(m => (m.Name, m.File, "cs-method")));
        defs.AddRange(CsTypes.Select(t => ((string)t["name"], (string)t["file"], "cs-type")));
        defs.AddRange(CsConstants.Select(c => ((string)c["name"], (string)c["file"], "cs-const")));
        defs.AddRange(SqlProcs.Select(p => ((string)p["name"], (string)p["file"], "proc")));
        defs.AddRange(SqlTriggers.Select(p => ((string)p["name"], (string)p["file"], "trigger")));
        defs.AddRange(SqlTables.Select(p => ((string)p["name"], (string)p["file"], "table")));
        foreach (var t in SqlTables)
            foreach (var c in (List<Fact>)t["columns"])
                if (((string)c["name"]).Length >= 8) defs.Add(($"{t["name"]}.{c["name"]}", (string)t["file"], "column"));
        var mentions = new List<Fact>();
        foreach (var c in Comments)
        {
            var txt = (string)c["text"];
            foreach (var (name, file, kind) in defs)
            {
                if (file == (string)c["file"]) continue;
                var bare = name.Contains('.') ? name : name;
                var rx = new Regex(@"(?<![\w])" + Regex.Escape(bare.Contains('.') ? bare.Split('.')[1] : bare) + @"(?![\w])", RegexOptions.IgnoreCase);
                if (kind == "column" && !txt.Contains(name.Split('.')[0], StringComparison.OrdinalIgnoreCase)) continue;
                if (rx.IsMatch(txt))
                    mentions.Add(new Fact { ["file"] = c["file"], ["start"] = c["start"], ["end"] = c["end"], ["mentions"] = name, ["kind"] = kind, ["defined_in"] = file });
            }
        }
        d["comment_mentions"] = mentions;
        d["commented_out_code"] = Comments.Where(c => ((List<int>)c["code_like_lines"]).Count > 0)
            .Select(c => new Fact { ["file"] = c["file"], ["start"] = c["start"], ["end"] = c["end"], ["code_like_lines"] = c["code_like_lines"] }).ToList();

        d["counts"] = new Fact
        {
            ["files"] = FilesOut.Count, ["vb_types"] = VbTypes.Count, ["vb_forms"] = VbTypes.Count(t => (bool)t["is_form"]),
            ["vb_methods"] = VbMethods.Count, ["vb_event_handlers"] = AllMethods.Count(m => m.Handles.Count > 0),
            ["vb_calls"] = VbCalls.Count, ["vb_proc_refs"] = VbProcRefs.Count, ["vb_inline_sql"] = VbInlineSql.Count,
            ["vb_constants"] = VbConstants.Count, ["vb_literals"] = VbLiterals.Count, ["vb_const_uses"] = VbConstUses.Count,
            ["cs_types"] = CsTypes.Count, ["cs_methods"] = CsMethods.Count, ["cs_calls"] = CsCalls.Count,
            ["cs_proc_refs"] = CsProcRefs.Count, ["cs_inline_sql"] = CsInlineSql.Count,
            ["cs_constants"] = CsConstants.Count, ["cs_literals"] = CsLiterals.Count,
            ["cs_const_uses"] = CsConstUses.Count, ["cs_comparisons"] = CsComparisons.Count,
            ["cs_column_refs"] = CsColumnRefs.Count, ["cs_io_calls"] = CsIoCalls.Count,
            ["sql_procs"] = SqlProcs.Count, ["sql_triggers"] = SqlTriggers.Count, ["sql_tables"] = SqlTables.Count,
            ["sql_seed_rows"] = SqlSeedRows.Count, ["sql_proc_exec_calls"] = SqlProcs.Sum(p => ((List<Fact>)p["exec_calls"]).Count),
            ["sql_parse_errors"] = SqlParseErrors.Count, ["comment_blocks"] = Comments.Count,
            ["commented_out_code_blocks"] = ((List<Fact>)d["commented_out_code"]).Count,
            ["missing_procs"] = ((List<object>)d["missing_procs"]).Count,
        };
        return d;
    }
}
