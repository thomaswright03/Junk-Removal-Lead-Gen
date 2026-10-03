// Leads tab: the court case box, the lookup buttons, the filters and one
// page of the lead list (the server filters, sorts and pages it).
"use strict";
const LEAD_COLUMNS = ["Priority", "Latest event", "What", "Property", "Owner / landlord", "Phone", "Email", "Miles", "Outreach", "Status"];
function leadRow(l) {
  const th = LEAD_COLUMNS;
  return `<tr class="click" data-id="${l.id}" data-label="${esc(title(fullAddress(l) || l.plaintiff || l.source_id))}">
    <td class="num" data-th="${th[0]}">${scoreChip(l)}</td>
    <td class="datecell" data-th="${th[1]}"><span>${dateCell(l)}</span></td>
    <td data-th="${th[2]}"><span>${esc(whatLabel(l))}${noticeChip(l)}${l.next_court_date ? `<span class="small-line" style="display:block">court ${esc(courtDate(l.next_court_date))}</span>` : ""}</span></td>
    <td data-th="${th[3]}"><span>${l.address ? esc(title(fullAddress(l))) + addressNote(l) : '<span class="muted">address needed</span>'}${l.property_use ? `<span class="small-line" style="display:block">${esc(title(l.property_use))}</span>` : ""}</span></td>
    <td data-th="${th[4]}"><span>${ownerLine(l)}</span></td>
    <td data-th="${th[5]}" style="white-space:nowrap">${phoneCell(l)}</td>
    <td data-th="${th[6]}" class="${l.owner_email ? "" : "m-hide"}">${emailCell(l)}</td>
    <td class="num m-hide" data-th="${th[7]}">${l.miles != null ? l.miles.toFixed(1) : "–"}</td>
    <td data-th="${th[8]}" class="${l.channel ? "" : "m-hide"}">${chDot(l.channel)}</td>
    <td data-th="${th[9]}">${statusChip(l.status)}</td></tr>`;
}
function renderLeads() {
  const list = S.list || { leads: [], total: 0, offset: 0, limit: 100 };
  const rows = list.leads;
  const c = S.counts || {};
  const opts = (pairs, cur) => pairs.map(([v, t]) => `<option value="${v}" ${v === cur ? "selected" : ""}>${t}</option>`).join("");
  const vc = S.view_counts || {}, view = S.settings.lead_view || "eviction_notice";
  // What's filtering the list, for the empty message and its Clear button.
  const typeLabel = { code_violation: "code cases", eviction: "evictions", absentee: "owner lives elsewhere", entity: "company or trust owner", has_phone: "has a phone", no_phone: "no phone yet", no_address: "address needed", guessed_address: "address to confirm" };
  const active = [ui.q.trim() ? `“${esc(ui.q.trim())}”` : "", typeLabel[ui.type] || "", ui.channel ? (ui.channel === "none" ? "not assigned" : esc(chName(ui.channel))) : "",
    ui.status && ui.status !== "open" ? esc(STATUS_LABEL[ui.status] || ui.status) : ""].filter(Boolean);
  const emptyMsg = active.length ? `No leads match ${active.join(", ")}${view !== "all" ? ` in “${esc(VIEW_LABEL[view])}”` : ""}. <button class="btn small" id="fClear">Clear search and filters</button>`
    : view === "all" ? "No leads match. Try “Any status”, or press Check for new evictions."
    : (S.daily || {}).running ? "Checking the court calendar for evictions. New cases show up here when the check finishes."
    : `No ${view === "eviction_notice" ? "eviction cases with a notice, judgment or writ" : "eviction cases"} yet. Press <b>Check for new evictions</b>
       to search the court calendar now (it also runs by itself every morning), or paste case links above.` +
      (vc.unchecked ? ` ${vc.unchecked} eviction cases haven't been checked yet; they appear here once their case page shows a notice (next check ${esc((S.daily || {}).next_run || "tomorrow 6:00 AM")}).` : "");
  const viewName = v => v === "all" && vc.code_cases ? `${VIEW_LABEL[v]}: ${vc[v] ?? 0}, ${vc.code_cases} of them code cases` : `${VIEW_LABEL[v]} (${vc[v] ?? 0})`;
  const evOpen = c.evictions_open || 0, evAddr = c.evictions_with_address || 0;
  const addrLine = evOpen ? `<p class="small-line" id="addrShare">${evAddr} of ${evOpen} open eviction lead${evOpen === 1 ? " has" : "s have"} a confirmed or typed property address.
    ${evOpen > evAddr ? `<button class="linkbtn" id="fNeedAddr">Show the ones that need one</button>` : ""}</p>` : "";
  const nFilters = [ui.type, ui.status !== "open" ? ui.status : "", ui.channel, ui.sort !== "score" ? ui.sort : ""].filter(Boolean).length;
  const first = list.total ? list.offset + 1 : 0, last = list.offset + rows.length;
  $("#tab-leads").innerHTML = `
    <div class="card mb12">
      <div class="row">
        <label class="wide-pick">Show <select id="fView">${opts(Object.keys(VIEW_LABEL).map(v => [v, viewName(v)]), view)}</select></label>
        <button class="btn m-only" id="toolsToggle" aria-expanded="${!!ui.toolsOpen}" aria-controls="leadTools">${ui.toolsOpen ? "Hide" : "Add cases, update cases, find phones"}</button>
      </div>
      <div id="leadTools" class="${ui.toolsOpen ? "open" : ""}">
      <div class="row mt8">
        <input type="text" id="cLinks" style="flex:1;min-width:260px" aria-label="Justice Court case links" placeholder="Paste Justice Court case links, e.g. https://www.jp.pima.gov/CaseSearch/jcDisplayCase.aspx?ID=1234567">
        <button class="btn primary" id="cAdd">Add cases</button>
        <button class="btn" id="cUpdate" title="Re-read every open eviction case page for new documents (notice, judgment, writ) and court dates. Runs in the background.">Update court cases</button>
      </div>
      <p class="hint" style="margin-bottom:0">Every morning Lead Desk searches the Justice Court calendar for eviction hearings, reads each new case page (with a short pause between cases) and keeps cases whose documents include an eviction notice. It re-reads open cases every few days and the day after each hearing, so a judgment or writ (lockout), the moment a unit needs clearing, moves the case to the top. You can also paste case links here. ${CODE_COVERAGE} Pick “All leads” under Show to see them.</p>
    <div class="row mt8">
      <button class="btn" id="lFind" title="Look up office phone, email and website for landlords, LLC owners and apartment complexes">Find landlord phones &amp; emails</button>
      <a class="btn" href="/api/skiptrace.csv" download="phone-lookup-list.csv" title="Owners still missing a phone, in the layout phone-lookup (skip-tracing) services such as BatchSkipTracing take">Download list for a phone-lookup service</a>
      <label class="btn" title="CSV with phone/email columns, from a phone-lookup service or your own list. Fills only empty fields; never changes a number you typed in.">Import phones / emails<input type="file" id="lImport" accept=".csv" hidden></label>
      <span class="muted" style="font-size:13px">${c.with_phone || 0} leads have a phone, ${c.with_email || 0} an email</span>
    </div>
    </div>
    </div>
    <div class="filters ${ui.filtersOpen ? "open" : ""}">
      <div class="searchrow"><input type="search" id="q" aria-label="Search leads" placeholder="Search address, owner, landlord, case, parcel…" value="${esc(ui.q)}">
      <button class="btn m-only" id="filtersToggle" aria-expanded="${!!ui.filtersOpen}">Filters${nFilters ? ` (${nFilters})` : ""}</button></div>
      <select id="fType" aria-label="Kind of lead">${opts([["", "All leads"], ["code_violation", "Code cases"], ["eviction", "Evictions"], ["absentee", "Owner lives elsewhere"], ["entity", "Company or trust owner"], ["has_phone", "Has a phone"], ["no_phone", "No phone yet"], ["no_address", "Address needed"], ["guessed_address", "Address to confirm"]], ui.type)}</select>
      <select id="fStatus" aria-label="Status">${opts([["open", "Open (not won/lost)"], ["", "Any status"], ...S.statuses.map(s => [s, STATUS_LABEL[s] || title(s)])], ui.status)}</select>
      <select id="fChannel" aria-label="Outreach method">${opts([["", "Any outreach method"], ["none", "Not assigned"], ...Object.entries(S.channels)], ui.channel)}</select>
      <select id="fSort" aria-label="Sort">${opts([["score", "Highest priority first"], ["date", "Newest first (latest court or city event)"], ["miles", "Closest first"]], ui.sort)}</select>
    </div>
    ${addrLine}
    <div class="tablewrap"><table class="cards" id="leadTable">
      <thead><tr>${LEAD_COLUMNS.map((t, i) => `<th${i === 0 || i === 7 ? ' class="num"' : ""}>${t}</th>`).join("")}</tr></thead>
      <tbody>${rows.map(leadRow).join("") || `<tr><td colspan="10" class="empty">${emptyMsg}</td></tr>`}
      </tbody></table></div>
    <div class="pager">
      <span class="muted" id="shown">${list.total ? `${first}–${last} of ${list.total} shown` : "0 shown"}</span>
      <span class="row">
        <button class="btn" id="pPrev" ${list.offset > 0 ? "" : "disabled"}>Previous ${list.limit}</button>
        <button class="btn" id="pNext" ${last < list.total ? "" : "disabled"}>Next ${list.limit}</button>
      </span>
    </div>
    <p class="hint">Order: evictions with a writ (lockout) first, then those with a judgment, then everything else by priority. Priority adds up how far the eviction has got or how much hauling a code case suggests, whether the owner lives elsewhere or is a company, repeat owners, and how recent the latest court or city event is (an upcoming hearing doesn't count). Miles are straight-line from ${esc(S.settings.base_address)}.</p>`;
  const filter = (id, key) => $(id).onchange = async e => { ui[key] = e.target.value; ui.offset = 0; syncUrl(); await reloadList(); $(id).focus(); };
  $("#q").oninput = e => { ui.q = e.target.value; ui.offset = 0; syncUrl(); clearTimeout(renderLeads.t); renderLeads.t = setTimeout(searchNow, 250); };
  filter("#fType", "type"); filter("#fStatus", "status"); filter("#fChannel", "channel"); filter("#fSort", "sort");
  const clear = $("#fClear");
  if (clear) clear.onclick = async () => { Object.assign(ui, { q: "", type: "", status: "open", channel: "", offset: 0 }); syncUrl(); await reloadList(); $("#q").focus(); };
  const need = $("#fNeedAddr");
  if (need) need.onclick = async () => { Object.assign(ui, { type: "no_address", offset: 0 }); syncUrl(); await reloadList(); $("#fType").focus(); };
  $("#toolsToggle").onclick = () => { ui.toolsOpen = !ui.toolsOpen; renderLeads(); $("#toolsToggle").focus(); };
  $("#filtersToggle").onclick = () => { ui.filtersOpen = !ui.filtersOpen; renderLeads(); $("#filtersToggle").focus(); };
  $("#pPrev").onclick = () => page(-1);
  $("#pNext").onclick = () => page(1);
  $("#fView").onchange = e => { ui.offset = 0; act(() => api("/api/settings", { lead_view: e.target.value }), null, e.currentTarget); };
  $("#cAdd").onclick = e => addCases($("#cLinks").value, e.currentTarget);
  $("#cLinks").onkeydown = e => { if (e.key === "Enter") addCases(e.target.value, $("#cAdd")); };
  $("#cUpdate").onclick = e => updateCases(e.currentTarget);
  $("#lFind").onclick = e => findContacts(e.currentTarget);
  $("#lImport").onchange = async e => { const f = e.target.files[0]; if (f) await importContacts(f); e.target.value = ""; };
  for (const [name, sel] of Object.entries({ cases: "#cUpdate", contacts: "#lFind" })) {
    const j = (S.jobs || {})[name], b = $(sel);
    if (b && j && j.running) { b.disabled = true; b.textContent = `${j.label}… ${j.total ? `${j.done} of ${j.total}` : ""}`; }
    if (b && S.paused) { b.disabled = true; b.title = "Paused in Settings"; }
  }
  if (S.paused) $("#cAdd").disabled = true;
  bindLabels($("#tab-leads"));
  bindRows($("#tab-leads"));
}
async function page(step) {
  const list = S.list || { limit: 100 };
  ui.offset = Math.max(0, ui.offset + step * list.limit);
  syncUrl(); await reloadList();
  $("#tab-leads").scrollIntoView();
}
async function searchNow() {
  // Re-render while keeping focus, and the cursor at the end, in the search box.
  const focused = document.activeElement && document.activeElement.id === "q";
  await reloadList();
  const q = $("#q"); if (q && focused) { q.focus(); q.setSelectionRange(q.value.length, q.value.length); }
}
