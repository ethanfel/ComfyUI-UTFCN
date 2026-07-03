import { app } from "../../scripts/app.js";

/*
 * UTFCN — Use The F***ing Core Nodes (frontend).
 *
 * The backend (/utfcn/scan) tells us, for every custom node type, which core (or
 * other-pack) nodes could stand in for it, split into:
 *   verified  — curated rule or an identical signature; safe to auto-apply.
 *   partial   — structurally and semantically compatible but looser; confirm first.
 *
 * This file turns that into three things:
 *   1. a toast tip when you interactively drop a replaceable custom node;
 *   2. a "Replace custom nodes with core / available…" command + Extensions menu
 *      entry that previews every swap in the open graph before applying;
 *   3. a right-click "Replace with core / available" item on individual nodes.
 *
 * Every actual swap goes through the same engine (planSwap → applySwap): it only
 * touches slots it can rewire losslessly and reports anything it can't.
 */

const EXT = "UTFCN";
let INDEX = null;            // { sources, candidates, stats }
const shapeCache = new Map(); // targetType -> { inputs, outputs, widgetNames } | null

/* -------------------------------------------------------------------------- */
/* data                                                                        */
/* -------------------------------------------------------------------------- */

async function loadIndex(refresh = false) {
    try {
        const r = await app.api.fetchApi("/utfcn/scan" + (refresh ? "?refresh=1" : ""));
        INDEX = await r.json();
    } catch (e) {
        INDEX = { sources: {}, candidates: {}, stats: {} };
        console.error("[UTFCN] scan failed:", e);
    }
    if (refresh) shapeCache.clear();
    return INDEX;
}

const sourceInfo = (type) => INDEX?.sources?.[type];
const isCustom = (type) => sourceInfo(type)?.source === "custom";
const candidatesFor = (type) => INDEX?.candidates?.[type] || [];

// The type key to look a node up by. ComfyUI keeps an UNINSTALLED ("missing")
// node as a placeholder whose original type lives in last_serialization.type.
const nodeType = (n) => n?.last_serialization?.type || n?.comfyClass || n?.type;
const isMissing = (n) => !!n?.has_errors || (INDEX && !INDEX.sources?.[nodeType(n)]);

// Missing nodes aren't in the registry, so /utfcn/scan can't know their signature.
// Ask the backend to match them from the serialized slots ComfyUI preserved, and
// fold the results into INDEX.candidates so the rest of the code is agnostic.
async function matchMissing() {
    if (!INDEX) await loadIndex();
    const items = [], seen = new Set();
    for (const n of app.graph?._nodes || []) {
        const t = nodeType(n);
        if (!t || seen.has(t) || INDEX.candidates[t] || INDEX.sources[t]) continue; // known/installed
        const s = n.last_serialization;
        if (!s) continue;
        seen.add(t);
        const inputs = {};
        (s.inputs || []).forEach((inp) => { if (inp?.name) inputs[inp.name] = inp.type; });
        items.push({
            type: t,
            display: s.title || n.title || t,
            inputs,
            outputs: (s.outputs || []).map((o) => o.type),
            output_names: (s.outputs || []).map((o) => o.name),
        });
    }
    if (!items.length) return;
    try {
        const r = await app.api.fetchApi("/utfcn/match", {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ nodes: items }),
        });
        Object.assign(INDEX.candidates, (await r.json()).candidates || {});
    } catch (e) { console.error("[UTFCN] match failed:", e); }
}

function toast(severity, detail, life = 5000) {
    try { app.extensionManager?.toast?.add?.({ severity, summary: EXT, detail, life }); }
    catch { /* older ComfyUI: no toast API */ }
    if (severity === "error") console.error("[UTFCN]", detail);
}

/* -------------------------------------------------------------------------- */
/* swap engine                                                                 */
/* -------------------------------------------------------------------------- */

/** True if two slot type strings can be connected (handles "*" and "A,B" unions). */
function typeOk(a, b) {
    if (a == null || b == null) return false;
    if (a === "*" || b === "*" || a === "" || b === "") return true;
    const A = String(a).split(","), B = String(b).split(",");
    return A.some((x) => B.includes(x));
}

/** A widget the user converted into an input slot — its value lives on the input, not the widget. */
const isConvertedWidget = (w) => w?.type === "converted-widget" || w?.type === "hidden";

/** Inspect a target type's slot/widget layout once (creating a throwaway node) and cache it. */
function targetShape(type) {
    if (shapeCache.has(type)) return shapeCache.get(type);
    let node = null;
    try { node = window.LiteGraph.createNode(type); } catch { /* unregistered */ }
    const shape = node && {
        inputs: (node.inputs || []).map((s) => ({ name: s.name, type: s.type })),
        outputs: (node.outputs || []).map((s) => ({ name: s.name, type: s.type })),
        widgetNames: (node.widgets || []).map((w) => w.name),
    };
    shapeCache.set(type, shape || null);
    return shape || null;
}

/**
 * Work out exactly how `node` would map onto `targetType`, honouring an optional
 * curated `rule` (name remaps). Only *connected* inputs and *linked* outputs must
 * map — an unmappable one is a hard problem; a dropped widget value is a warning.
 */
function planSwap(node, targetType, rule) {
    const shape = targetShape(targetType);
    if (!shape) return { ok: false, problems: [`“${targetType}” is not available`], warns: [], targetType };

    const problems = [], warns = [], inMap = [], outMap = [], wMap = [];
    const usedIn = new Set(), usedOut = new Set();

    (node.inputs || []).forEach((inp, i) => {
        if (inp.link == null) return;                                   // unconnected → nothing to carry
        const want = rule?.inputs?.[inp.name] ?? inp.name;
        let j = shape.inputs.findIndex((s, k) => !usedIn.has(k) && s.name === want);
        if (j < 0) j = shape.inputs.findIndex((s, k) => !usedIn.has(k) && typeOk(inp.type, s.type));
        if (j < 0) { problems.push(`input “${inp.name}” (${inp.type}) has no match`); return; }
        if (!typeOk(inp.type, shape.inputs[j].type)) { problems.push(`input “${inp.name}”: ${inp.type} ≠ ${shape.inputs[j].type}`); return; }
        usedIn.add(j); inMap.push({ src: i, dst: j });
    });

    (node.outputs || []).forEach((out, i) => {
        const links = (out.links || []).length;
        if (!links) return;                                            // no downstream → nothing to carry
        const want = rule?.outputs?.[out.name] ?? out.name;
        let j = shape.outputs.findIndex((s, k) => !usedOut.has(k) && s.name === want);
        if (j < 0) j = shape.outputs.findIndex((s, k) => !usedOut.has(k) && typeOk(out.type, s.type));
        if (j < 0) { problems.push(`output “${out.name}” (${out.type}, ${links} link${links > 1 ? "s" : ""}) has no match`); return; }
        if (!typeOk(shape.outputs[j].type, out.type)) { problems.push(`output “${out.name}”: ${out.type} ≠ ${shape.outputs[j].type}`); return; }
        usedOut.add(j); outMap.push({ src: i, dst: j });
    });

    (node.widgets || []).forEach((w) => {
        if (w.name == null || isConvertedWidget(w)) return;
        const want = rule?.widgets?.[w.name] ?? w.name;
        if (shape.widgetNames.includes(want)) wMap.push({ from: w.name, to: want });
        else if (w.value !== undefined && w.value !== null && w.value !== "") warns.push(`widget “${w.name}” value not carried`);
    });

    return { ok: problems.length === 0, problems, warns, inMap, outMap, wMap, targetType };
}

/** Perform the swap described by `plan`: create the target, move links + widget values, delete the source. Returns the new node (or null). */
function applySwap(node, plan, rule) {
    const graph = node.graph;
    if (!graph || !plan.ok) return null;

    graph.beforeChange?.();
    const t = window.LiteGraph.createNode(plan.targetType);
    if (!t) { graph.afterChange?.(); return null; }
    graph.add(t);
    t.pos = [node.pos[0], node.pos[1]];
    if (node.color) t.color = node.color;
    if (node.bgcolor) t.bgcolor = node.bgcolor;

    // widget values first (setting them may lay out extra widgets)
    plan.wMap.forEach((m) => {
        const sw = (node.widgets || []).find((w) => w.name === m.from);
        const tw = (t.widgets || []).find((w) => w.name === m.to);
        if (sw && tw && sw.value !== undefined) { tw.value = sw.value; try { tw.callback?.(tw.value); } catch {} }
    });

    // snapshot link records BEFORE we start mutating the graph
    const inLinks = plan.inMap
        .map((m) => ({ dst: m.dst, l: graph.links[node.inputs[m.src].link] }))
        .filter((x) => x.l);
    const outLinks = [];
    plan.outMap.forEach((m) => {
        (node.outputs[m.src].links || []).slice().forEach((id) => {
            const l = graph.links[id];
            if (l) outLinks.push({ dst: m.dst, l });
        });
    });

    // upstream → target
    inLinks.forEach(({ dst, l }) => graph.getNodeById(l.origin_id)?.connect(l.origin_slot, t, dst));
    // target → downstream
    outLinks.forEach(({ dst, l }) => { const d = graph.getNodeById(l.target_id); if (d) t.connect(dst, d, l.target_slot); });

    graph.remove(node);
    graph.afterChange?.();
    app.canvas?.setDirty(true, true);
    return t;
}

/** First verified candidate whose swap is feasible right now (used by force mode). */
function firstVerifiedPlan(node) {
    for (const c of candidatesFor(nodeType(node))) {
        if (!c.verified) continue;
        const plan = planSwap(node, c.to, c);
        if (plan.ok) return { cand: c, plan };
    }
    return null;
}

/* -------------------------------------------------------------------------- */
/* preview dialog                                                              */
/* -------------------------------------------------------------------------- */

function injectStyle() {
    if (document.getElementById("utfcn-style")) return;
    const s = document.createElement("style");
    s.id = "utfcn-style";
    s.textContent = `
    .utfcn-overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:10000;display:flex;align-items:center;justify-content:center;font-family:sans-serif}
    .utfcn-modal{background:var(--comfy-menu-bg,#202020);color:var(--fg-color,#ddd);border:1px solid #444;border-radius:8px;max-width:820px;width:92%;max-height:82vh;display:flex;flex-direction:column;box-shadow:0 8px 40px rgba(0,0,0,.5)}
    .utfcn-modal h2{margin:0;padding:14px 18px;font-size:15px;border-bottom:1px solid #3a3a3a;display:flex;gap:8px;align-items:baseline}
    .utfcn-modal h2 small{color:#888;font-weight:400;font-size:12px}
    .utfcn-body{overflow:auto;padding:6px 0}
    .utfcn-body table{width:100%;border-collapse:collapse;font-size:12.5px}
    .utfcn-body td,.utfcn-body th{padding:6px 12px;text-align:left;border-bottom:1px solid #2e2e2e;vertical-align:middle}
    .utfcn-body th{position:sticky;top:0;background:var(--comfy-menu-bg,#202020);color:#9aa;font-weight:600;z-index:1}
    .utfcn-body tr.dis{opacity:.5}
    .utfcn-arrow{color:#666;padding:0 2px}
    .utfcn-from{color:#e0a}.utfcn-to{color:#6c9}
    .utfcn-pack{color:#888;font-size:11px}
    .utfcn-badge{font-size:11px;padding:1px 6px;border-radius:4px;white-space:nowrap}
    .utfcn-ok{background:#1e3a24;color:#8fdca0}.utfcn-warn{background:#3a331e;color:#e6cf7a}.utfcn-no{background:#3a1e1e;color:#e69a9a}
    .utfcn-modal select{background:#111;color:#ddd;border:1px solid #444;border-radius:4px;padding:2px 4px;max-width:260px}
    .utfcn-foot{display:flex;gap:10px;justify-content:space-between;align-items:center;padding:12px 18px;border-top:1px solid #3a3a3a}
    .utfcn-foot .sp{color:#888;font-size:12px}
    .utfcn-btn{background:#333;color:#eee;border:1px solid #555;border-radius:6px;padding:7px 16px;cursor:pointer;font-size:13px}
    .utfcn-btn:hover{background:#3d3d3d}
    .utfcn-btn.primary{background:#2d6cdf;border-color:#2d6cdf}.utfcn-btn.primary:hover{background:#3b78e7}
    .utfcn-btn:disabled{opacity:.5;cursor:not-allowed}
    `;
    document.head.appendChild(s);
}

/**
 * Show the preview table for `rows` ([{node, cands}]) and apply the ones the user keeps checked.
 * Verified + feasible swaps start checked; partials and infeasible ones don't.
 */
function showPreview(rows) {
    injectStyle();

    // per-row UI state: chosen candidate index + its plan
    const state = rows.map(({ node, cands }) => {
        let sel = cands.findIndex((c) => c.verified && planSwap(node, c.to, c).ok);
        if (sel < 0) sel = cands.findIndex((c) => planSwap(node, c.to, c).ok);
        if (sel < 0) sel = 0;
        return { sel };
    });

    const overlay = document.createElement("div");
    overlay.className = "utfcn-overlay";
    overlay.innerHTML = `
      <div class="utfcn-modal">
        <h2>🔁 UTFCN — Replace with core / available <small>${rows.length} candidate node${rows.length === 1 ? "" : "s"} in this workflow</small></h2>
        <div class="utfcn-body"><table>
          <thead><tr><th></th><th>Node</th><th>Replace with</th><th>Status</th></tr></thead>
          <tbody></tbody>
        </table></div>
        <div class="utfcn-foot">
          <span class="sp"></span>
          <span><button class="utfcn-btn cancel">Cancel</button> <button class="utfcn-btn primary apply">Apply selected</button></span>
        </div>
      </div>`;

    const tbody = overlay.querySelector("tbody");
    const summary = overlay.querySelector(".sp");
    const applyBtn = overlay.querySelector(".apply");
    const close = () => overlay.remove();

    function planForRow(i) {
        const { node, cands } = rows[i];
        const c = cands[state[i].sel];
        return { c, plan: c ? planSwap(node, c.to, c) : { ok: false, problems: ["no candidate"], warns: [] } };
    }

    function renderRow(i) {
        const { c, plan } = planForRow(i);
        const tr = tbody.children[i];
        const cb = tr.querySelector("input[type=checkbox]");
        const status = tr.querySelector(".utfcn-status");

        cb.disabled = !plan.ok;
        tr.classList.toggle("dis", !plan.ok);
        if (!plan.ok) {
            cb.checked = false;
            status.innerHTML = `<span class="utfcn-badge utfcn-no">✗ ${plan.problems[0]}</span>`;
        } else if (c.verified) {
            status.innerHTML = `<span class="utfcn-badge utfcn-ok">✓ ${c.tier === "curated" ? "curated" : "exact match"}</span>` +
                (plan.warns.length ? ` <span class="utfcn-badge utfcn-warn">${plan.warns.length} note</span>` : "");
        } else {
            status.innerHTML = `<span class="utfcn-badge utfcn-warn">⚠ heuristic ${(c.score * 100) | 0}%</span>`;
        }
    }

    rows.forEach(({ node, cands }, i) => {
        const t = nodeType(node);
        const pack = sourceInfo(t)?.pack || (isMissing(node) ? "⚠ not installed" : "?");
        const opts = cands.map((c, k) =>
            `<option value="${k}">${c.verified ? "✓" : "⚠"} ${c.to_display} · ${c.source === "core" ? "core" : c.pack}</option>`).join("");
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><input type="checkbox"></td>
          <td><span class="utfcn-from">${node.title || t}</span> <span class="utfcn-pack">#${node.id} · ${pack}</span></td>
          <td><span class="utfcn-arrow">→</span> <select>${opts}</select></td>
          <td class="utfcn-status"></td>`;
        tbody.appendChild(tr);

        const sel = tr.querySelector("select");
        sel.value = String(state[i].sel);
        sel.addEventListener("change", () => { state[i].sel = +sel.value; renderRow(i); updateSummary(); });
        tr.querySelector("input[type=checkbox]").addEventListener("change", updateSummary);
    });

    function updateSummary() {
        let checked = 0;
        tbody.querySelectorAll("input[type=checkbox]").forEach((cb) => { if (cb.checked) checked++; });
        summary.textContent = `${checked} of ${rows.length} selected`;
        applyBtn.disabled = checked === 0;
    }

    // initial render + default-check verified feasible rows
    rows.forEach((_, i) => {
        renderRow(i);
        const { c, plan } = planForRow(i);
        tbody.children[i].querySelector("input[type=checkbox]").checked = !!(plan.ok && c?.verified);
    });
    updateSummary();

    overlay.querySelector(".cancel").addEventListener("click", close);
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(); });
    applyBtn.addEventListener("click", () => {
        let done = 0, failed = 0, notes = 0;
        rows.forEach((row, i) => {
            const cb = tbody.children[i].querySelector("input[type=checkbox]");
            if (!cb.checked) return;
            const { c, plan } = planForRow(i);
            if (applySwap(row.node, plan, c)) { done++; notes += plan.warns.length; } else failed++;
        });
        close();
        if (done) toast("success", `Replaced ${done} node${done === 1 ? "" : "s"}${notes ? ` · ${notes} widget value(s) not carried` : ""}`);
        if (failed) toast("error", `${failed} replacement(s) failed`);
        if (!done && !failed) toast("info", "Nothing was selected");
    });

    document.body.appendChild(overlay);
}

/* -------------------------------------------------------------------------- */
/* feature 2: bulk replace (command + menu)                                    */
/* -------------------------------------------------------------------------- */

async function openBulkDialog() {
    if (!INDEX) await loadIndex();
    await matchMissing();               // include uninstalled / red "missing" nodes
    const rows = [];
    for (const node of app.graph?._nodes || []) {
        const cands = candidatesFor(nodeType(node));
        if (cands.length) rows.push({ node, cands });
    }
    if (!rows.length) { toast("info", "No custom nodes with a known core / available equivalent here 🎉"); return; }
    showPreview(rows);
}

/* -------------------------------------------------------------------------- */
/* feature 3: single-node right-click                                          */
/* -------------------------------------------------------------------------- */

function replaceSingle(node, cand) {
    const plan = planSwap(node, cand.to, cand);
    if (!plan.ok) { toast("warn", `Can't replace “${node.title || node.type}”: ${plan.problems[0]}`); return; }
    if (applySwap(node, plan, cand)) {
        toast("success", `Replaced with ${cand.to_display}${plan.warns.length ? ` · ${plan.warns.length} widget value(s) not carried` : ""}`);
    } else {
        toast("error", "Replacement failed");
    }
}

// Patch the canvas-level menu builder (not per-node-type) so the item also
// appears on UNINSTALLED "missing" placeholders, which never register a type.
function installMenu() {
    const C = window.LGraphCanvas;
    if (!C || C.prototype.__utfcn_menu) return;
    C.prototype.__utfcn_menu = true;
    const orig = C.prototype.getNodeMenuOptions;
    C.prototype.getNodeMenuOptions = function (node) {
        const options = orig ? orig.apply(this, arguments) : [];
        try {
            const cands = candidatesFor(nodeType(node));
            if (cands.length) {
                const submenu = cands.map((c) => ({
                    content: `${c.verified ? "✓" : "⚠"} ${c.to_display} ${c.source === "core" ? "(core)" : "(" + c.pack + ")"}`,
                    callback: () => replaceSingle(node, c),
                }));
                options.push(null, { content: "🔁 Replace with core / available", has_submenu: true, submenu: { options: submenu } });
            }
        } catch (e) { console.error("[UTFCN] menu error:", e); }
        return options;
    };
}

/* -------------------------------------------------------------------------- */
/* feature 1: on add — Off / Suggest / Force                                   */
/* -------------------------------------------------------------------------- */

// "Off" | "Suggest" | "Force (auto-replace with core)"
let ADD_MODE = "Suggest";
let loadingGraph = false;
let addQueue = [], addTimer = null;
const isForce = () => ADD_MODE.startsWith("Force");

// Never act while a workflow is loading — force mode must not silently rewrite
// graphs the user opens/imports; it only touches nodes they add themselves.
function guardGraphLoading() {
    const orig = app.loadGraphData?.bind(app);
    if (!orig) return;
    app.loadGraphData = async function (...a) {
        loadingGraph = true;
        try { return await orig(...a); }
        finally { setTimeout(() => { loadingGraph = false; matchMissing(); }, 150); } // pick up missing nodes
    };
}

function onNodeAdded(node) {
    if (loadingGraph || ADD_MODE === "Off") return;
    const t = nodeType(node);
    if (!isCustom(t) || !candidatesFor(t).length) return;
    addQueue.push(node);
    clearTimeout(addTimer);
    addTimer = setTimeout(flushAdds, 250); // let the add settle, and batch pastes
}

function flushAdds() {
    const nodes = addQueue.filter((n) => n?.graph); // still in the graph
    addQueue = [];
    if (!nodes.length) return;

    if (isForce()) {
        // auto-swap only VERIFIED candidates — heuristics are never applied silently
        let swapped = 0, last = null;
        for (const node of nodes) {
            const pick = firstVerifiedPlan(node);
            if (!pick) continue;
            const t = applySwap(node, pick.plan, pick.cand);
            if (t) { swapped++; last = t; }
        }
        if (swapped) {
            if (last) try { app.canvas?.selectNode?.(last); } catch {}
            toast("success", `Force mode: switched ${swapped} node${swapped === 1 ? "" : "s"} to core / available`);
        }
        return;
    }

    // Suggest mode: one quiet tip per unique type (stay silent on big pastes)
    const types = [...new Set(nodes.map((n) => nodeType(n)))];
    if (types.length > 4) return;
    types.forEach((tp) => {
        const cands = candidatesFor(tp);
        const best = cands.find((c) => c.verified) || cands[0];
        if (!best) return;
        const where = best.source === "core" ? "a core node" : `“${best.pack}”`;
        toast("info", `“${sourceInfo(tp)?.display || tp}” has ${where} equivalent: “${best.to_display}”. Right-click ▸ Replace with core / available.`, 7000);
    });
}

function hookNodeAdded() {
    const g = app.graph;
    if (!g || g.__utfcn_hooked) return;
    g.__utfcn_hooked = true;
    const prev = g.onNodeAdded;
    g.onNodeAdded = function (node) {
        prev?.call(this, node);
        try { onNodeAdded(node); } catch {}
    };
}

/* -------------------------------------------------------------------------- */
/* registration                                                                */
/* -------------------------------------------------------------------------- */

app.registerExtension({
    name: "utfcn.core",

    settings: [
        {
            id: "UTFCN.onAdd",
            name: "When adding a custom node that has a core / available equivalent",
            tooltip: "Off: do nothing.  Suggest: show a tip.  Force: automatically replace it with the equivalent (verified matches only).",
            category: ["UTFCN", "On add", "mode"],
            type: "combo",
            options: ["Off", "Suggest", "Force (auto-replace with core)"],
            defaultValue: "Suggest",
            onChange: (v) => { if (v) ADD_MODE = v; },
        },
    ],

    commands: [
        { id: "UTFCN.replaceAll", label: "UTFCN: Replace custom nodes with core / available…", function: openBulkDialog },
        {
            id: "UTFCN.refresh", label: "UTFCN: Refresh equivalence index",
            function: async () => { await loadIndex(true); toast("success", `Index refreshed · ${INDEX?.stats?.replaceable ?? 0} replaceable node type(s)`); },
        },
    ],

    menuCommands: [
        { path: ["Extensions", "UTFCN"], commands: ["UTFCN.replaceAll", "UTFCN.refresh"] },
    ],

    async setup() {
        await loadIndex();
        installMenu();          // right-click item (covers installed AND missing nodes)
        guardGraphLoading();
        hookNodeAdded();
        matchMissing();         // in case a workflow is already open at startup
        const s = INDEX?.stats;
        if (s?.replaceable) console.log(`[UTFCN] ${s.replaceable} replaceable type(s): ${s.verified} verified, ${s.uninstalled ?? 0} for uninstalled packs.`);
    },
});
