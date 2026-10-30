// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// jsdom click-through of the launcher page against a live (stubbed) launcher.
// usage: node ui_flow_sim.js <base-url> <run-id-without-key> <comma-separated shipped-key run ids>
// Prints one line per milestone (GROUND, STEP1, PROPOSED, CONFIRM, READY, COST, RESULT, SHIPPED).
"use strict";
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const [base, rid, shippedArg] = process.argv.slice(2);
const shipped = (shippedArg || "").split(",").filter(Boolean);
const html = fs.readFileSync(path.join(__dirname, "..", "launcher_ui", "index.html"), "utf8");
const dom = new JSDOM(html, {
  url: base + "/", runScripts: "dangerously", pretendToBeVisual: true,
  beforeParse(w) {
    w.fetch = (p, o) => fetch(new URL(p, base), o);
    w.CSS = { escape: (s) => String(s).replace(/["\\]/g, "\\$&") };
    w.scrollTo = () => {};
    w.Element.prototype.scrollIntoView = function () {};
    w.confirm = (m) => { console.log("DIALOG " + m); return true; };
  },
});
const w = dom.window, doc = w.document;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(what, fn, timeout = 90000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    let v; try { v = fn(); } catch (e) { v = null; }
    if (v) return v;
    await sleep(200);
  }
  const panels = [...doc.querySelectorAll(".eval-panel")].map((p) => p.dataset.run + ": " + p.textContent.replace(/\s+/g, " ").slice(0, 600));
  throw new Error("timed out waiting for " + what + "\n" + panels.join("\n"));
}
const panel = (id) => doc.querySelector(`.eval-panel[data-run="${id}"]`);
const text = (n) => (n ? n.textContent.replace(/\s+/g, " ").trim() : "");
const flow = (id) => panel(id) && panel(id).querySelector('[data-ev="flow"]');
function button(id, re) {
  const p = panel(id); if (!p) return null;
  return [...p.querySelectorAll("button, a[data-act]")].find((b) => re.test(text(b)) && !b.disabled) || null;
}
function line(id, re) {
  const f = flow(id); if (!f) return null;
  const n = [...f.querySelectorAll("p")].find((x) => re.test(text(x)));
  return n ? text(n) : null;
}
const click = (b) => b.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));

async function open(id) {
  const a = await until(`row ${id}`, () => doc.querySelector(`[data-open="${id}"]`));
  if (!panel(id)) click(a);
  await until(`panel ${id}`, () => panel(id));
}

(async () => {
  await open(rid);
  const g = await until("grounding line", () => { const t = text(panel(rid).querySelector('[data-ev="ground"]')); return /Grounded|Needs attention/.test(t) && t; });
  console.log("GROUND " + g);
  click(await until("Score this spec", () => button(rid, /^Score this spec$/)));
  console.log("STEP1 " + await until("step 1 intro", () => line(rid, /To score correctness/)));
  click(await until("Build answer key", () => button(rid, /^Build answer key$/)));
  console.log("PROPOSED " + await until("proposed counts", () => line(rid, /proposed · \d+ confirmed/)));
  click(await until("Approve all N rules", () => button(rid, /^Approve all \d+ rules$/)));
  console.log("CONFIRM " + await until("confirmation", () => line(rid, /^Approve all \d+ proposed rules\?/)));
  click(await until("Approve all (yes)", () => button(rid, /^Approve all$/)));
  console.log("READY " + await until("key ready", () => line(rid, /^Answer key ready —/)));
  console.log("COST " + await until("cost", () => line(rid, /^Estimated cost/)));
  click(await until("Start scoring", () => button(rid, /^Start scoring$/)));
  console.log("RESULT " + await until("result", () => line(rid, /^Scored:/)));
  for (const id of shipped) {
    await open(id);
    const b = await until(`score button ${id}`, () => button(id, /^(Score this spec|Score again)$/));
    click(b);
    console.log(`SHIPPED ${id} ` + await until(`shipped key ${id}`, () => line(id, /^Answer key ready —/)));
    console.log(`SHIPPEDCOST ${id} ` + await until(`shipped cost ${id}`, () => line(id, /^(Estimated cost|.*exited|.*error)/i)));
    const gr = text(panel(id).querySelector('[data-ev="ground"]'));
    console.log(`SHIPPEDGROUND ${id} ${gr}`);
  }
  w.close();
  process.exit(0);
})().catch((e) => { console.log("FAIL " + e.message); process.exit(1); });
