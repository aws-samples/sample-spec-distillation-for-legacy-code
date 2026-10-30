// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
//
// Lean pipeline, layer 1: deterministic structure extraction (no LLM).
//
// Parses every *.vb file with Roslyn (Microsoft.CodeAnalysis.VisualBasic, syntax only) and every
// *.sql file (plus SQL text embedded in VB string literals) with ScriptDom
// (Microsoft.SqlServer.TransactSql.ScriptDom, TSql160Parser). Writes one JSON document (facts.json)
// to stdout. Every fact carries "file" plus "start"/"end" (1-based, inclusive) or "line".
//
// Usage: dotnet LeanStructure.dll <source-root>   (normally run via the Docker image, see Dockerfile)

using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.VisualBasic;
using Microsoft.CodeAnalysis.VisualBasic.Syntax;
using Microsoft.SqlServer.TransactSql.ScriptDom;
using Fact = System.Collections.Generic.Dictionary<string, object>;

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
            string lang = ext switch { ".vb" => "vb.net", ".sql" => "t-sql", _ => "other" };
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

        var derived = Derive();
        var doc = new Fact
        {
            ["schema"] = "lean-facts/v1",
            ["tools"] = new Fact { ["vb"] = "Microsoft.CodeAnalysis.VisualBasic 4.8.0 (syntax only)",
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
            ["sql"] = new Fact
            {
                ["procs"] = SqlProcs, ["triggers"] = SqlTriggers, ["tables"] = SqlTables,
                ["seed_rows"] = SqlSeedRows, ["scripts"] = SqlScripts, ["numeric_literals"] = SqlNumericLiterals,
                ["parse_errors"] = SqlParseErrors,
            },
            ["comments"] = Comments,
            ["derived"] = derived,
        };
        var opts = new JsonSerializerOptions { WriteIndented = true,
            Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping };
        Console.Out.Write(JsonSerializer.Serialize(doc, opts));
        Console.Error.WriteLine($"files={FilesOut.Count} vb_methods={VbMethods.Count} calls={VbCalls.Count} " +
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
        return !(t.StartsWith("'") || t.StartsWith("--") || t.StartsWith("/*") || t.StartsWith("REM ", StringComparison.OrdinalIgnoreCase));
    }

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
        var t = (v ?? "").Trim().TrimEnd('D', 'd', 'R', 'r', 'F', 'f', '@', '#', '!', 'S', 's', 'I', 'i', 'L', 'l');
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

    // ================================================================== derived cross-file facts
    static Fact Derive()
    {
        var d = new Fact();

        // proc index: definition + callers (VB and SQL)
        var procIdx = new List<Fact>();
        var allProcNames = SqlProcs.Select(p => (string)p["name"]).Concat(VbProcRefs.Select(r => (string)r["proc"]))
            .Concat(SqlProcs.SelectMany(p => ((List<Fact>)p["exec_calls"]).Select(x => (string)x["proc"])))
            .Distinct(StringComparer.OrdinalIgnoreCase).OrderBy(x => x).ToList();
        foreach (var pn in allProcNames)
        {
            var def = SqlProcs.FirstOrDefault(p => string.Equals((string)p["name"], pn, StringComparison.OrdinalIgnoreCase));
            var vbCallers = VbProcRefs.Where(r => string.Equals((string)r["proc"], pn, StringComparison.OrdinalIgnoreCase))
                .Select(r => new Fact { ["from"] = r["from"], ["file"] = r["file"], ["line"] = r["line"] }).ToList();
            var sqlCallers = SqlProcs.Concat(SqlTriggers).SelectMany(p => ((List<Fact>)p["exec_calls"])
                    .Where(x => string.Equals((string)x["proc"], pn, StringComparison.OrdinalIgnoreCase))
                    .Select(x => new Fact { ["from"] = p["name"], ["file"] = p["file"], ["line"] = x["line"] })).ToList();
            procIdx.Add(new Fact
            {
                ["proc"] = pn, ["defined"] = def != null, ["file"] = def?["file"], ["start"] = def?["start"], ["end"] = def?["end"],
                ["vb_callers"] = vbCallers, ["sql_callers"] = sqlCallers,
            });
        }
        d["proc_index"] = procIdx;
        d["missing_procs"] = procIdx.Where(p => !(bool)p["defined"]).Select(p => p["proc"]).ToList();

        // VB -> proc edges at method level, so callers of a proc wrapper are visible
        // method reachability
        var incoming = VbCalls.GroupBy(c => (string)c["to"]).ToDictionary(g => g.Key, g => g.Count());
        d["vb_methods_without_callers"] = AllMethods
            .Where(m => m.Handles.Count == 0 && m.Kind is not ("Constructor" or "Property") && !incoming.ContainsKey(m.Id)
                        && !Regex.IsMatch(m.Name, @"^(InitializeComponent|Dispose)$"))
            .Select(m => new Fact
            {
                ["method"] = m.Id, ["file"] = m.File, ["start"] = m.Start, ["end"] = m.End, ["access"] = m.Access,
                ["note"] = m.Access == "Private" ? "private, no caller in corpus" : "public, no caller in corpus (could be called from outside the corpus)",
            }).ToList();
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
        foreach (var c in VbConstants)
        {
            var n = NormName((string)c["name"]);
            var n2 = Regex.Replace(n, "^(dflt|default|max|min)", "");
            var hits = sqlNames.Where(s => s.norm == n || (n2.Length >= 6 && s.norm.EndsWith(n2))).Select(s => s.where).ToList();
            if (hits.Count > 0)
                xl.Add(new Fact { ["vb_const"] = c["name"], ["value"] = c["value"], ["file"] = c["file"], ["start"] = c["start"], ["end"] = c["end"], ["sql_matches"] = hits });
        }
        d["cross_layer_name_matches"] = xl;

        // numeric values (>= 2) that appear in both VB and SQL
        static string NormNum(string v)
        {
            v = (v ?? "").TrimEnd('D', 'd', 'R', 'r', 'F', 'f', '@', '#', '!');
            return decimal.TryParse(v, NumberStyles.Any, CultureInfo.InvariantCulture, out var x) ? x.ToString("0.####", CultureInfo.InvariantCulture) : null;
        }
        var vbNums = VbConstants.Select(c => (v: NormNum((string)c["value"]), f: new Fact { ["file"] = c["file"], ["line"] = c["start"], ["what"] = $"Const {c["name"]}", ["text"] = c["text"] }))
            .Concat(VbLiterals.Where(l => (string)l["kind"] == "number").Select(l => (v: NormNum((string)l["value"]), f: new Fact { ["file"] = l["file"], ["line"] = l["line"], ["what"] = $"literal in {l["from"]} ({l["context"]})", ["text"] = l["text"] })))
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
            ["sql_procs"] = SqlProcs.Count, ["sql_triggers"] = SqlTriggers.Count, ["sql_tables"] = SqlTables.Count,
            ["sql_seed_rows"] = SqlSeedRows.Count, ["sql_proc_exec_calls"] = SqlProcs.Sum(p => ((List<Fact>)p["exec_calls"]).Count),
            ["sql_parse_errors"] = SqlParseErrors.Count, ["comment_blocks"] = Comments.Count,
            ["commented_out_code_blocks"] = ((List<Fact>)d["commented_out_code"]).Count,
            ["missing_procs"] = ((List<object>)d["missing_procs"]).Count,
        };
        return d;
    }
}
