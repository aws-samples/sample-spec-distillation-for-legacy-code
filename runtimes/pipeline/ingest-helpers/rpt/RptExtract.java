// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import com.crystaldecisions.sdk.occa.report.application.ReportClientDocument;
import com.crystaldecisions.sdk.occa.report.application.DataDefController;
import com.crystaldecisions.sdk.occa.report.data.*;

/**
 * RptExtract — the .rpt INGEST HELPER tool (invoked by ingest_helpers.py's RptJrcHelper).
 *
 * Opens a local Crystal Reports .rpt via the SAP Java Reporting Component (JRC) and emits the
 * report DEFINITION as plain text on stdout: tables, result fields, formula fields (business
 * logic), record-selection formula (the WHERE), group-selection, parameters, groups, summaries.
 * No DB connection required — the STRUCTURE is offline-recoverable (only the fully-materialized
 * live SQL string needs a datasource, which we deliberately don't resolve).
 *
 * BATCH CONTRACT (one JVM for the whole set — never one JVM per file): stdin is a manifest of
 * TAB-separated `<rpt_path>\t<out_file>` lines. The JVM boots ONCE, loops every line, writes each
 * report's extracted text to its <out_file>, and prints a status line to stdout per report:
 *     OK\t<rpt_path>\t<out_file>\t<bytes>      (extraction succeeded, text written)
 *     FAIL\t<rpt_path>\t<reason>              (this report failed; others continue)
 * One bad report NEVER kills the batch (per-report try/catch). Exit 0 always if the loop ran; the
 * Python side reads the status lines and, for FAIL rows, falls through the ladder (ole-envelope /
 * referential). Warm JVM + JIT means reports get faster as the batch proceeds.
 *
 * Single-file convenience: `RptExtract <one.rpt>` (no stdin) prints that report's text to stdout
 * (used by the one-off CLI path; the batch path is what materialization uses at scale).
 *
 * Classpath (provisioned, not bundled): JVM 17 + the SAP CRSDK lib jars (CrystalReportsRuntime.jar,
 * CrystalCommon2.jar, JDBInterface.jar, commons/log4j/xpp3/cvom) + icu4j (com.ibm.icu.text.Collator).
 * Proven on ARM macOS with CR 12.2 JRC + icu4j 4.8.1.1 + openjdk@17.
 */
public class RptExtract {

    public static void main(String[] args) throws Exception {
        if (args.length >= 1) {
            // single-file convenience mode → stdout
            String text = extract(args[0]);
            System.out.print(text);
            return;
        }
        // batch mode: read <rpt>\t<out> lines from stdin, one warm JVM for all
        java.io.BufferedReader in = new java.io.BufferedReader(
                new java.io.InputStreamReader(System.in, java.nio.charset.StandardCharsets.UTF_8));
        String line;
        while ((line = in.readLine()) != null) {
            line = line.trim();
            if (line.isEmpty()) continue;
            String[] parts = line.split("\t", 2);
            String rpt = parts[0];
            String outFile = parts.length > 1 ? parts[1] : null;
            try {
                String text = extract(rpt);
                if (outFile != null) {
                    java.nio.file.Files.write(java.nio.file.Paths.get(outFile),
                            text.getBytes(java.nio.charset.StandardCharsets.UTF_8));
                    System.out.println("OK\t" + rpt + "\t" + outFile + "\t" + text.length());
                } else {
                    System.out.print(text);
                }
            } catch (Throwable t) {
                System.out.println("FAIL\t" + rpt + "\t"
                        + t.getClass().getSimpleName() + ": " + oneLine(String.valueOf(t.getMessage())));
            }
        }
    }

    /** Extract one report's definition to text. Throws on open failure (caller decides fallback). */
    static String extract(String path) throws Exception {
        StringBuilder out = new StringBuilder();
        ReportClientDocument doc = new ReportClientDocument();
        doc.open(path, 0);   // throws if unopenable → caller falls through the ladder
        out.append("# REPORT DEFINITION: ").append(path).append("\n");
        try {
            DataDefController ddc = doc.getDataDefController();
            IDataDefinition def = ddc.getDataDefinition();

            section(out, "TABLES", () -> {
                Tables t = doc.getDatabaseController().getDatabase().getTables();
                for (int i = 0; i < t.size(); i++) {
                    ITable tb = t.getTable(i);
                    out.append("  ").append(tb.getName())
                       .append(eq(tb.getName(), tb.getAlias()) ? "" : " (alias " + tb.getAlias() + ")")
                       .append("\n");
                }
            });
            section(out, "RESULT FIELDS", () -> {
                Fields f = def.getResultFields();
                for (int i = 0; i < f.size(); i++)
                    out.append("  ").append(((IField) f.getField(i)).getFormulaForm()).append("\n");
            });
            section(out, "FORMULA FIELDS", () -> {
                Fields<IFormulaField> f = def.getFormulaFields();
                for (int i = 0; i < f.size(); i++) {
                    IFormulaField ff = f.get(i);
                    out.append("  @").append(ff.getName()).append(" = ").append(oneLine(ff.getText())).append("\n");
                }
            });
            section(out, "RECORD SELECTION FORMULA", () -> {
                IFilter rf = def.getRecordFilter();
                String rs = rf == null ? null : rf.getFreeEditingText();
                out.append("  ").append(isBlank(rs) ? "(none)" : oneLine(rs)).append("\n");
            });
            section(out, "GROUP SELECTION FORMULA", () -> {
                IFilter gf = def.getGroupFilter();
                String gs = gf == null ? null : gf.getFreeEditingText();
                out.append("  ").append(isBlank(gs) ? "(none)" : oneLine(gs)).append("\n");
            });
            section(out, "PARAMETERS", () -> {
                Fields<IParameterField> f = def.getParameterFields();
                for (int i = 0; i < f.size(); i++) {
                    IParameterField pf = f.get(i);
                    out.append("  ?").append(pf.getName()).append("  (reportName=").append(pf.getReportName()).append(")\n");
                }
            });
            section(out, "GROUPS", () -> {
                Groups g = def.getGroups();
                for (int i = 0; i < g.size(); i++)
                    out.append("  group on: ").append(fieldName(g.getGroup(i).getConditionField())).append("\n");
            });
            section(out, "SUMMARY FIELDS", () -> {
                Fields<ISummaryField> f = def.getSummaryFields();
                for (int i = 0; i < f.size(); i++) {
                    ISummaryField sf = f.get(i);
                    out.append("  ").append(fieldName(sf.getSummarizedField())).append(" = ")
                       .append(opName(sf.getOperation())).append("(").append(fieldName(sf.getSummarizedField())).append(")\n");
                }
            });
        } catch (Throwable t) {
            out.append("\n(extraction interrupted: ").append(t.getClass().getSimpleName()).append(")\n");
        } finally {
            try { doc.close(); } catch (Throwable ignore) {}
        }
        return out.toString();
    }

    interface Body { void run() throws Exception; }
    static void section(StringBuilder out, String title, Body b) {
        out.append("\n## ").append(title).append("\n");
        try { b.run(); } catch (Throwable t) { out.append("  (unavailable: ").append(t.getClass().getSimpleName()).append(")\n"); }
    }
    static String fieldName(IField f) {
        if (f == null) return "(none)";
        try { String ff = f.getFormulaForm(); if (!isBlank(ff)) return ff; } catch (Throwable ignore) {}
        try { String n = f.getName(); if (!isBlank(n)) return n; } catch (Throwable ignore) {}
        return String.valueOf(f);
    }
    static String opName(Object op) {
        if (op == null) return "?";
        String s = String.valueOf(op);
        int dot = s.lastIndexOf('.');
        return dot >= 0 ? s.substring(dot + 1) : s;
    }
    static String oneLine(String s) { return s == null ? "" : s.replaceAll("\\s+", " ").trim(); }
    static boolean isBlank(String s) { return s == null || s.trim().isEmpty(); }
    static boolean eq(String a, String b) { return a == null ? b == null : a.equals(b); }
}
