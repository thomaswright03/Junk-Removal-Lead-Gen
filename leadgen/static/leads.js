// Leads tab: the court case box, the lookup buttons, the filters and one
// page of the lead list (the server filters, sorts and pages it).
"use strict";
const LEAD_COLUMNS = ["Priority", "Latest event", "What", "Property", "Owner / landlord", "Phone", "Email", "Miles", "Outreach", "Status"];
// The landlord (eviction) or owner a phone number is looked for.
const landlordOf = l => (l.plaintiff || l.owner_name || "").split(";")[0].trim();
const canFindPhone = l => !l.owner_phone && !!landlordOf(l);
function leadRow(l) {
  const th = LEAD_COLUMNS;
  return `<tr class="click" data-id="${l.id}" data-label="${esc(title(fullAddress(l) || l.plaintiff || l.source_id))}">
    <td class="num" data-th="${th[0]}">${scoreChip(l)}</td>
    <td class="datecell" data-th="${th[1]}"><span>${dateCell(l)}</span></td>
    <td data-th="${th[2]}"><span>${esc(whatLabel(l))}${noticeChip(l)}${l.next_court_date ? `<span class="small-line" style="display:block">court ${esc(courtDate(l.next_court_date))}</span>` : ""}</span></td>
    <td data-th="${th[3]}"><span>${l.address ? esc(title(fullAddress(l))) + addressNote(l) : '<span class="muted">address needed</span>'}${l.property_use ? `<span class="small-line" style="display:block">${esc(title(l.property_use))}</span>` : ""}</span></td>
    <td data-th="${th[4]}"><span>${ownerLine(l)}</span></td>
    <td data-th="${th[5]}" class="phonecell">${phoneCell(l)}${canFindPhone(l) ? ` <button class="btn small" data-findphone="${l.id}" aria-expanded="${ui.findOpen === l.id}" aria-controls="find-${l.id}">Find phone</button>` : ""}${reachChip(l)}</td>
    <td data-th="${th[6]}" class="${l.owner_email ? "" : "m-hide"}">${emailCell(l)}</td>
    <td class="num m-hide" data-th="${th[7]}">${l.miles != null ? l.miles.toFixed(1) : "–"}</td>
    <td data-th="${th[8]}" class="${l.channel ? "" : "m-hide"}">${chDot(l.channel)}</td>
    <td data-th="${th[9]}">${statusChip(l.status)}</td></tr>
    ${canFindPhone(l) ? `<tr class="findrow" id="find-${l.id}" ${ui.findOpen === l.id ? "" : "hidden"}><td colspan="10">${ui.findOpen === l.id ? findPanel(l) : ""}</td></tr>` : ""}`;
}
// Find phone, on the lead's own row: searches for the landlord in a new tab
// and a box to paste the number into, so a number goes in without leaving
// the list (no Google key needed).
function findPanel(l) {
  const who = landlordOf(l), name = esc(title(who));
  const web = "https://www.google.com/search?q=" + encodeURIComponent(`"${who.replace(/[",]/g, " ").replace(/\s+/g, " ").trim()}" Tucson AZ phone`);
  const maps = "https://www.google.com/maps/search/" + encodeURIComponent(who + " Tucson AZ");
  const others = l.owner_lead_count > 1 ? ` (${l.owner_lead_count - 1} more)` : "";
  return `<div class="findphone" role="group" aria-label="Find a phone number for ${name}">
    <p class="hint"><strong>Find a number for ${name}.</strong> Open a search, copy the office or leasing number, paste it here.
      Companies are often listed under a property or management name: the Corporation Commission lists the company's agent and managers to search for too.</p>
    <div class="row">
      <a class="btn small" href="${web}" target="_blank" rel="noopener">Search the web</a>
      <a class="btn small" href="${maps}" target="_blank" rel="noopener">Search Google Maps</a>
      <a class="btn small" href="https://ecorp.azcc.gov/EntitySearch/Index" target="_blank" rel="noopener" data-copyname="${esc(who)}" title="Arizona Corporation Commission company search. The name is copied: paste it into its search box.">AZ Corporation Commission (copies the name)</a>
      ${l.owner_website ? `<a class="btn small" href="${esc(/^https?:/i.test(l.owner_website) ? l.owner_website : "https://" + l.owner_website)}" target="_blank" rel="noopener">Company website</a>` : ""}
    </div>
    <div class="row mt8">
      <input id="fp-${l.id}" inputmode="tel" autocomplete="off" placeholder="Paste the number, e.g. (520) 555-0100" aria-label="Phone number for ${name}" style="width:240px">
      <label class="ch"><input type="checkbox" id="fpAll-${l.id}" checked> Also on this landlord's other leads with no number${others}</label>
      <button class="btn small primary" data-savephone="${l.id}">Save number</button>
      ${fieldError("fp-" + l.id)}
    </div></div>`;
}
function bindFindPhone(root) {
  root.querySelectorAll("[data-findphone]").forEach(b => b.onclick = () => {
    const id = +b.dataset.findphone;
    ui.findOpen = ui.findOpen === id ? null : id;
    renderLeads();
    const box = $("#fp-" + id);
    if (box) box.focus(); else { const again = document.querySelector(`[data-findphone="${id}"]`); if (again) again.focus(); }
  });
  root.querySelectorAll("[data-copyname]").forEach(a => a.addEventListener("click", () => {
    try { navigator.clipboard.writeText(a.dataset.copyname).then(() => toast("Name copied: paste it into the Commission's search box."), () => {}); } catch (e) { /* no clipboard */ }
  }));
  root.querySelectorAll("[data-savephone]").forEach(b => {
    const id = +b.dataset.savephone, box = $("#fp-" + id);
    const save = async () => {
      const err = checkPhone(box.value) || (box.value.trim() ? null : "Paste the phone number first.");
      if (err) return setFieldError("fp-" + id, err);
      const all = $("#fpAll-" + id).checked;
      const r = await act(() => api("/api/lead", { id, fields: { owner_phone: box.value }, same_landlord: all }),
        r => `Number saved${r.also ? ` here and on ${r.also} more lead${r.also === 1 ? "" : "s"} of this landlord` : ""}. `
          + `${(S.counts || {}).evictions_reachable || 0} of ${(S.counts || {}).evictions_open || 0} open eviction leads can be reached now.`,
        b, undefined, e => showFieldErrors(e, { owner_phone: "fp-" + id }));
      if (r) {
        ui.findOpen = null; clearFieldError("fp-" + id); renderLeads();
        const next = document.querySelector("#leadTable [data-findphone]");  // on to the next lead with no number
        if (next) next.focus();
      }
    };
    b.onclick = save;
    box.onkeydown = e => { if (e.key === "Enter") save(); };
    box.oninput = () => clearFieldError("fp-" + id);
  });
}
// "How this works": everything that explains the list, in one place, shut
// until asked for so the list starts at the top of the screen.
function leadHelp(view, c, addrLine) {
  return `<div class="card mb12" id="leadHelp">
    ${addrLine}
    <p class="small-line" id="coverage">${CODE_COVERAGE}${view === "all" ? "" : " Pick “All leads” under Show to see them."}</p>
    <p class="small-line">${c.with_phone || 0} leads have a phone, ${c.with_email || 0} an email.
      Order: writs first, then judgments (each only while 45 days old or less), then everything else by priority. Hover a priority number to see what it's made of. Miles are straight-line from ${esc(S.settings.base_address)}.</p>
    <details class="hint" id="coverageMore"><summary>What Lead Desk covers</summary><ul>
      <li><b>Evictions:</b> every eviction hearing on the Pima County Consolidated Justice Court's calendar (the Green Valley and Ajo justice courts keep their own and aren't read).</li>
      <li><b>Clean-out leads (code cases):</b> City of Tucson code-enforcement cases only. Nothing yet for Marana, Oro Valley, Sahuarita, South Tucson or unincorporated Pima County, which is much of the northwest near your base.</li>
      <li><b>Not collected:</b> foreclosures (trustee sale notices), probate and estate clean-outs, and county code enforcement. Adding one is your call (some cost money); ask for it.</li></ul></details>
    <details class="hint"><summary>How Lead Desk finds evictions</summary>
      <p>Every morning it searches the Justice Court calendar for eviction hearings, reads each new case page (with a short pause between cases) and keeps cases whose documents include an eviction notice. It re-reads open cases every few days and the day after each hearing, so a judgment or writ, the moment a unit needs clearing, moves the case to the top. The calendar lists only upcoming hearings: cases already past their hearing (most judgments and writs) come in with the court's records request.</p></details>
    <details class="hint" id="glossary"><summary>What the court and property words mean</summary><dl>
      <dt>Eviction notice</dt><dd>The landlord's written notice to the tenant, filed in the court case: the eviction is under way.</dd>
      <dt>Judgment</dt><dd>The court ruled for the landlord. A writ usually follows within days.</dd>
      <dt>Writ (lockout)</dt><dd>A writ of restitution: the court's order to put the tenant out. The unit needs clearing now.</dd>
      <dt>Hearing</dt><dd>The court date for the case. It hasn't happened yet, so it doesn't make a lead fresher.</dd>
      <dt>Parcel</dt><dd>The county's number for a piece of property; it tells Lead Desk the owner of record.</dd>
      <dt>Owner lives elsewhere</dt><dd>On a City code case, the owner's mailing address isn't the property: a landlord, not someone living there. (On an eviction the owner is the landlord, whose office is nearly always elsewhere, so it isn't shown or counted there.)</dd>
      <dt>Priority</dt><dd>Points for how far the eviction has got (or how much hauling a code case suggests), a code case owner who lives elsewhere, a company owner, an owner with several leads, and how recent the latest court or city event is.</dd>
      <dt>Phone-lookup service</dt><dd>A paid service (“skip tracing”) that finds phone numbers for a list of owners.</dd>
    </dl></details>
    <button class="linkbtn" id="helpClose">Close</button>
  </div>`;
}
function renderLeads() {
  const list = S.list || { leads: [], total: 0, offset: 0, limit: 100 };
  const rows = list.leads;
  const c = S.counts || {};
  const opts = (pairs, cur) => pairs.map(([v, t]) => `<option value="${v}" ${v === cur ? "selected" : ""}>${t}</option>`).join("");
  const vc = S.view_counts || {}, view = S.settings.lead_view || "eviction_notice";
  // What's filtering the list, for the empty message and its Clear button.
  const typeLabel = { address_work: "needs a usable address", code_violation: "code cases", eviction: "evictions", absentee: "owner lives elsewhere", entity: "company or trust owner", has_phone: "has a phone", no_phone: "no phone yet", no_address: "address needed", guessed_address: "address to confirm", reachable: "can be reached", unreachable: "can't be reached yet" };
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
  const evReach = c.evictions_reachable || 0, evNone = evOpen - evReach;
  const A = S.addresses || {}, wk = A.week_ago, rec = A.records || {};
  const share = (n, d) => d ? Math.round(100 * n / d) + "%" : "–";
  const trend = wk && wk.open ? `; ${share(wk.with_address, wk.open)} a week ago` : "";
  const addrLine = evOpen ? `<p class="small-line" id="addrShare">${evAddr} of ${evOpen} open eviction lead${evOpen === 1 ? " has" : "s have"} an address a door hanger can go to (${share(evAddr, evOpen)}${trend}): typed, confirmed or from the court, with a unit where the parcel has several homes.
    ${evOpen > evAddr && ui.type !== "address_work" ? `<button class="linkbtn" id="fNeedAddr">Work through the ones that need one</button>` : ""}
    ${rec.due && ui.type !== "address_work" ? ` · <span class="chip warn">records request due</span>` : ""}</p>` : "";
  // One short status line: how many eviction leads can be reached now, and
  // the ways to more (the setup steps, Help), each one click away.
  const todo = (googleReady() ? 0 : 1) + (recordsStarted() ? 0 : 1);
  const statusLine = `<p class="statusline" id="reachLine">${evOpen
      ? `<span><strong>${evReach} of ${evOpen}</strong> open eviction lead${evOpen === 1 ? "" : "s"} can be reached now.</span>
        ${evNone && ui.type !== "unreachable" ? `<button class="linkbtn" id="fUnreach">Show the ${evNone} that can't</button>` : ""}`
      : `<span>No open eviction leads yet.</span>`}
    <span class="sep" aria-hidden="true">·</span><button class="linkbtn" id="setupShow" aria-expanded="${!!ui.showSetup}" aria-controls="setupGuide">Get phones and addresses</button>${todo ? ` <span class="chip warn">${todo} step${todo > 1 ? "s" : ""} to do</span>` : ""}
    <span class="sep" aria-hidden="true">·</span><button class="linkbtn" id="helpToggle" aria-expanded="${!!ui.helpOpen}" aria-controls="leadHelp">How this works</button></p>`;
  const queue = ui.type === "address_work";
  const nFilters = [ui.type, ui.status !== "open" ? ui.status : "", ui.channel, ui.sort !== "score" ? ui.sort : ""].filter(Boolean).length;
  const first = list.total ? list.offset + 1 : 0, last = list.offset + rows.length;
  $("#tab-leads").innerHTML = `
    <div class="card mb12 toolbar">
      <div class="row">
        <label class="wide-pick">Show <select id="fView">${opts(Object.keys(VIEW_LABEL).map(v => [v, viewName(v)]), view)}</select></label>
        <button class="btn m-only" id="toolsToggle" aria-expanded="${!!ui.toolsOpen}" aria-controls="leadTools">${ui.toolsOpen ? "Hide" : "Add cases, update cases, find phones"}</button>
        <div id="leadTools" class="row tools ${ui.toolsOpen ? "open" : ""}">
          <input type="text" id="cLinks" aria-label="Justice Court case links" placeholder="Paste Justice Court case links to add cases">
          <button class="btn primary" id="cAdd">Add cases</button>
          <button class="btn" id="cUpdate" title="Re-read every open eviction case page for new documents (notice, judgment, writ) and court dates. Runs in the background.">Update court cases</button>
          <button class="btn" id="lFind" title="Look up office phone, email and website for landlords, LLC owners and apartment complexes (OpenStreetMap and company websites, and Google Places when a key is set)">Find landlord phones &amp; emails</button>
          <a class="btn" href="/api/skiptrace.csv" download="phone-lookup-list.csv" title="Owners still missing a phone, in the layout phone-lookup (skip-tracing) services such as BatchSkipTracing take">Phone-lookup list</a>
          <label class="btn" title="CSV with phone/email columns, from a phone-lookup service or your own list. Fills only empty fields; never changes a number you typed in.">Import phones / emails<input type="file" id="lImport" accept=".csv" hidden></label>
        </div>
      </div>
    </div>
    <div class="filters ${ui.filtersOpen ? "open" : ""}">
      <div class="searchrow"><input type="search" id="q" aria-label="Search leads" placeholder="Search address, owner, landlord, case, parcel…" value="${esc(ui.q)}">
      <button class="btn m-only" id="filtersToggle" aria-expanded="${!!ui.filtersOpen}">Filters${nFilters ? ` (${nFilters})` : ""}</button></div>
      <select id="fType" aria-label="Kind of lead">${opts([["", "Any kind of lead"], ["code_violation", "Code cases"], ["eviction", "Evictions"], ["absentee", "Owner lives elsewhere"], ["entity", "Company or trust owner"], ["has_phone", "Has a phone"], ["no_phone", "No phone yet"], ["no_address", "Address needed"], ["guessed_address", "Address to confirm"], ["address_work", "Address work queue (evictions)"], ["reachable", "Can be reached (phone, email or address)"], ["unreachable", "Can't be reached yet"]], ui.type)}</select>
      <select id="fStatus" aria-label="Status">${opts([["open", "Open (not won/lost)"], ["", "Any status"], ...S.statuses.map(s => [s, STATUS_LABEL[s] || title(s)])], ui.status)}</select>
      <select id="fChannel" aria-label="Outreach method">${opts([["", "Any outreach method"], ["none", "Not assigned"], ...Object.entries(S.channels)], ui.channel)}</select>
      <select id="fSort" aria-label="Sort">${opts([["score", "Highest priority first"], ["date", "Newest first (latest court or city event)"], ["miles", "Closest first"]], ui.sort)}</select>
    </div>
    ${statusLine}
    ${ui.showSetup ? setupGuide(addrLine) : ""}
    ${ui.helpOpen ? leadHelp(view, c, ui.showSetup ? "" : addrLine) : ""}
    ${queue ? recordsCard(rec) + addressQueue(rows, emptyMsg) : `<div class="tablewrap sticky-head"><table class="cards" id="leadTable">
      <thead><tr>${LEAD_COLUMNS.map((t, i) => `<th${i === 0 || i === 7 ? ' class="num"' : ""}>${t}</th>`).join("")}</tr></thead>
      <tbody>${rows.map(leadRow).join("") || `<tr><td colspan="10" class="empty">${emptyMsg}</td></tr>`}
      </tbody></table></div>`}
    <div class="pager">
      <span class="muted" id="shown">${list.total ? `${first}–${last} of ${list.total} shown` : "0 shown"}</span>
      <span class="row">
        <button class="btn" id="pPrev" ${list.offset > 0 ? "" : "disabled"}>Previous ${list.limit}</button>
        <button class="btn" id="pNext" ${last < list.total ? "" : "disabled"}>Next ${list.limit}</button>
      </span>
    </div>`;
  const filter = (id, key) => $(id).onchange = async e => { ui[key] = e.target.value; ui.offset = 0; syncUrl(); await reloadList(); $(id).focus(); };
  $("#q").oninput = e => { ui.q = e.target.value; ui.offset = 0; syncUrl(); clearTimeout(renderLeads.t); renderLeads.t = setTimeout(searchNow, 250); };
  filter("#fType", "type"); filter("#fStatus", "status"); filter("#fChannel", "channel"); filter("#fSort", "sort");
  const clear = $("#fClear");
  if (clear) clear.onclick = async () => { Object.assign(ui, { q: "", type: "", status: "open", channel: "", offset: 0 }); syncUrl(); await reloadList(); $("#q").focus(); };
  const need = $("#fNeedAddr");
  if (need) need.onclick = async () => { Object.assign(ui, { type: "address_work", offset: 0 }); syncUrl(); await reloadList(); $("#fType").focus(); };
  const unreach = $("#fUnreach");
  if (unreach) unreach.onclick = async () => { Object.assign(ui, { type: "unreachable", offset: 0 }); syncUrl(); await reloadList(); $("#fType").focus(); };
  $("#setupShow").onclick = () => { ui.showSetup = !ui.showSetup; renderLeads(); const g = ui.showSetup && $("#setupTitle"); if (g) g.focus(); else $("#setupShow").focus(); };
  $("#helpToggle").onclick = () => { ui.helpOpen = !ui.helpOpen; renderLeads(); $("#helpToggle").focus(); };
  const hc = $("#helpClose");
  if (hc) hc.onclick = () => { ui.helpOpen = false; renderLeads(); $("#helpToggle").focus(); };
  if (ui.showSetup) bindSetupGuide();
  if (queue) bindAddressQueue();
  bindFindPhone($("#tab-leads"));
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
  markFields($("#tab-leads"));
  stickyHeadOffset();
}
// The lead table's column names stay in view while the list scrolls (on a
// computer; a phone shows one card per lead): under the page header when the
// table fits the window, else at the top of the table's own scrolling box,
// so the page never scrolls sideways (see .sticky-head in app.css).
function stickyHeadOffset() {
  const h = document.querySelector("header");
  document.documentElement.style.setProperty("--header-h", (h && getComputedStyle(h).position === "sticky" ? h.offsetHeight : 0) + "px");
  fitTable();
}
function fitTable() {
  const wrap = document.querySelector(".tablewrap.sticky-head"), table = wrap && wrap.querySelector("table");
  if (!table) return;
  wrap.classList.remove("fits");
  // Not on screen (another tab is open): measured when the Leads tab shows.
  if (wrap.clientWidth) wrap.classList.toggle("fits", table.scrollWidth <= wrap.clientWidth);
}
// The tab's width changes when it is shown, the window is resized or the
// drawer opens: measure again (only for a change of width, so this can't loop).
if (window.ResizeObserver) {
  let lastWidth = -1;
  new ResizeObserver(entries => {
    const w = Math.round(entries[0].contentRect.width);
    if (w !== lastWidth) { lastWidth = w; fitTable(); }
  }).observe(document.getElementById("tab-leads"));
}
window.addEventListener("resize", () => stickyHeadOffset());
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

// ---------- address work queue ----------------------------------------------
// Evictions with no address, or only a guess from the landlord's parcels:
// the case, tenant and landlord next to the landlord's properties, with one
// click to confirm the guess or use a property.
const RECORDS_FORM = "https://www.jp.pima.gov/OnlineRecordsRequest/Default.aspx";
function recordsAsk(rec) {
  return `A list of eviction (special detainer) cases filed from ${fmtDate(rec.request_from)} to ${fmtDate(rec.request_to)}, `
    + "and of any eviction case with a judgment or writ of restitution entered in those dates, "
    + "with the case number, plaintiff, defendant and property address of each, and the date of any judgment and of any writ of restitution, "
    + "as a spreadsheet (CSV or Excel).";
}
function recordsCard(rec) {
  const last = rec.last_import ? `Last file imported ${esc(fmtDate(rec.last_import))}${rec.last_filled != null ? `, ${rec.last_filled} address${rec.last_filled === 1 ? "" : "es"} filled in` : ""}.` : "No court file imported yet.";
  const sent = rec.waiting ? ` Request sent ${esc(fmtDate(rec.last_request))}: import the file when it arrives.` : "";
  const due = rec.due ? '<span class="chip warn">due now</span>' : `next one due ${esc(fmtDate(rec.due_on))}`;
  return `<div class="card mb12" id="recordsCard"><h2>Addresses from the court: records request</h2>
    <p class="hint">Court case pages have no property address, but the court's records request does. ${last}${sent} Ask every ${rec.every_days || 14} days: ${due}.</p>
    ${rec.first ? `<p class="notice info hint" id="backfillNote"><strong>First request: the last month.</strong> The court calendar only lists upcoming hearings, so cases already past their hearing, the judgments and writs (lockouts) that need clearing now, only come in this way. Import the file and they show at the top of the list.</p>` : ""}
    <ol class="hint guide">
      <li>Open the court's <a href="${RECORDS_FORM}" target="_blank" rel="noopener">online records request form</a> (there may be a small fee).</li>
      <li>Ask for: <q id="recAsk">${esc(recordsAsk(rec))}</q> <button class="btn small" id="recCopy">Copy the request</button></li>
      <li>${rec.waiting ? "Sent." : `When you've sent it, <button class="btn small" data-recsent>mark the request as sent</button> so Lead Desk tracks when the next one is due.`}</li>
      <li>When the file arrives, save it as CSV (in Excel: File, Save As, CSV) and import it:
        <label class="btn small" title="The court's records-request file, saved as CSV">Import the court's file<input type="file" id="recFile" accept=".csv" hidden></label>
        Rows whose case number matches a case here fill in its address; an address you typed or confirmed is kept.</li>
    </ol></div>`;
}
function addressRow(l) {
  const tenant = (l.defendant || "").split(";")[0].trim(), landlord = (l.plaintiff || l.owner_name || "").split(";")[0].trim();
  const guess = l.address && l.address_source === "landlord";
  return `<tr class="click" data-id="${l.id}" data-label="${esc(title(landlord || l.source_id))}">
    <td class="num" data-th="Priority">${scoreChip(l)}</td>
    <td data-th="Case"><span>${esc(l.source_id)}${noticeChip(l)}<span class="small-line" style="display:block">tenant ${esc(title(tenant) || "–")}</span></span></td>
    <td data-th="Landlord"><span>${esc(title(landlord) || "–")}</span></td>
    <td data-th="Address now"><span>${guess ? `${esc(title(fullAddress(l)))} <span class="chip warn" title="The landlord owns one property in the county, so the eviction is probably there">landlord's only ${isMultifamily(l) ? "complex" : "property"}</span>
      <button class="btn small" data-aconfirm="${l.id}">Confirm</button>` : '<span class="muted">none yet</span>'}</span></td>
    <td data-th="Find it"><span>${landlord ? `<button class="btn small" data-aprops="${l.id}" aria-expanded="false">Landlord's properties</button>` : ""}
      ${tenant ? `<a href="https://www.google.com/search?q=${encodeURIComponent(tenant + " Tucson AZ")}" target="_blank" rel="noopener">search tenant</a>` : ""}</span></td></tr>
    <tr class="aprops" id="aprops-${l.id}" hidden><td colspan="5"></td></tr>`;
}
function addressQueue(rows, emptyMsg) {
  return `<div class="tablewrap"><table class="cards" id="addrTable">
    <thead><tr><th class="num">Priority</th><th>Case and tenant</th><th>Landlord</th><th>Address now</th><th>Find it</th></tr></thead>
    <tbody>${rows.map(addressRow).join("") || `<tr><td colspan="5" class="empty">${emptyMsg}</td></tr>`}</tbody></table></div>
    <p class="hint">Click a case to type the address yourself. Confirm or Use saves it at once: Lead Desk then finds it on the map, looks up the owner and fills in the miles.</p>`;
}
function bindAddressQueue() {
  const root = $("#tab-leads");
  const copy = $("#recCopy");
  if (copy) copy.onclick = async () => {
    const text = $("#recAsk").textContent;
    try { await navigator.clipboard.writeText(text); toast("Request copied. Paste it into the court's form."); }
    catch (e) { const r = document.createRange(); r.selectNodeContents($("#recAsk")); getSelection().removeAllRanges(); getSelection().addRange(r); toast("Selected: press Ctrl+C (or ⌘C) to copy."); }
  };
  bindRecordsSent(root);
  const rf = $("#recFile");
  if (rf) rf.onchange = async e => { const f = e.target.files[0]; if (f) await importLeadsFile(f); e.target.value = ""; };
  root.querySelectorAll("[data-aconfirm]").forEach(b => b.onclick = () =>
    act(() => api("/api/lead", { id: +b.dataset.aconfirm, fields: { confirm_address: true } }), "Address confirmed. Door hangers can go to this lead.", b));
  root.querySelectorAll("[data-aprops]").forEach(b => b.onclick = async () => {
    const id = +b.dataset.aprops, l = listLeads().find(x => x.id === id), row = $("#aprops-" + id), cell = row.firstElementChild;
    if (!row.hidden) { row.hidden = true; b.setAttribute("aria-expanded", "false"); return; }
    row.hidden = false; b.setAttribute("aria-expanded", "true");
    const who = (l.plaintiff || l.owner_name || "").split(";")[0].trim();
    cell.innerHTML = '<p class="hint">Looking up the county assessor…</p>';
    try {
      const props = (await api("/api/owner?name=" + encodeURIComponent(who))).filter(p => p.site_address);
      cell.innerHTML = props.length ? `<p class="hint">${props.length} propert${props.length === 1 ? "y" : "ies"} owned by names starting “${esc(title(who))}”. Use the one the tenant rents.</p>
        <div class="tablewrap" style="max-height:260px;overflow:auto"><table><tbody>${props.map(p => `<tr><td>${esc(title(p.site_address))}</td><td class="muted">${esc(title(p.property_use || ""))}</td>
        <td><button class="btn small" data-ause="${esc(p.site_address)}">Use</button></td></tr>`).join("")}</tbody></table></div>`
        : '<p class="hint">No properties found under that name. Landlords often own through a differently named LLC: try the records request above, or click the case to type the address.</p>';
      cell.querySelectorAll("[data-ause]").forEach(u => u.onclick = () =>
        act(() => api("/api/lead", { id, fields: { address: u.dataset.ause } }), r => r.message || "Address saved", u));
    } catch (e) { cell.innerHTML = `<p class="hint">Couldn't look up the landlord's properties: ${esc(e.message)}</p>`; }
  });
}

// ---------- setup guide: making eviction leads reachable ---------------------
// Court cases carry no phone number and no property address. The status line
// above the list says how many steps are left; "Get phones and addresses"
// opens the guide, which walks through both (it stays shut until asked for,
// so the list starts at the top of the screen).
const googleReady = () => !!(S.settings.google_key_set && S.settings.google_enabled !== false);
const recordsStarted = () => { const r = (S.addresses || {}).records || {}; return !!(r.last_request || r.last_import); };
const setupDone = () => googleReady() && recordsStarted();
// What Google can cost a month at the limits in Settings (1,000 free, then about $35 per 1,000).
function googleCostLine() {
  const st = S.settings, daily = st.google_daily_limit, monthly = st.google_monthly_limit;
  const most = Math.min(monthly == null ? Infinity : monthly, daily == null ? Infinity : daily * 31);
  const limits = `Lead Desk stops at ${daily == null ? "no daily limit" : `${daily} a day`} and ${monthly == null ? "no monthly limit" : `${monthly.toLocaleString()} a month`} (change these in Settings)`;
  if (most === Infinity) return `${limits}, so the cost depends on how many landlords are looked up.`;
  return most <= 1000 ? `${limits}, so it stays within the free amount: $0 a month.`
    : `${limits}, so the most it can cost is about $${Math.ceil((most - 1000) * 0.035)} a month.`;
}
function setupGuide(addrLine) {
  const st = S.settings, c = S.counts || {}, rec = (S.addresses || {}).records || {};
  const evOpen = c.evictions_open || 0;
  const done = t => `<span class="chip good">${esc(t)}</span>`;
  const keyStep = googleReady()
    ? `${done(st.google_key_from_env ? "key set on the server" : "key saved")}
       <p class="hint">${esc(googleCostLine())} ${c.with_phone || 0} leads have a phone so far.</p>
       <button class="btn" id="setupFind">Find landlord phones now</button>`
    : st.google_key_set
    ? `<span class="chip warn">Google lookups are off</span>
       <p class="hint">A key is saved but “Use Google lookups” is off in Settings, so only OpenStreetMap is checked. ${esc(googleCostLine())}</p>
       <button class="btn primary" id="setupGoogleOn">Turn Google lookups on and find phones</button>`
    : `<span class="chip">not set up</span>
       <p class="hint">Lead Desk looks up each landlord's office number on Google Places. Without a key it checks only OpenStreetMap, which knows few Tucson landlords: use <b>Find phone</b> on each lead instead (about half a minute a lead).
       Cost: Google gives 1,000 of these lookups a month free, then charges about $35 per 1,000. ${esc(googleCostLine())}</p>
       <details class="hint" id="keySteps" open><summary>How to get a key (about 10 minutes, step by step)</summary><ol class="guide">
         <li>Open <a href="https://console.cloud.google.com/projectcreate" target="_blank" rel="noopener">Google Cloud: New project</a> and sign in with any Google account. Type <b>Lead Desk</b> as the project name and press <b>Create</b>.</li>
         <li>Open <a href="https://console.cloud.google.com/billing/linkedaccount" target="_blank" rel="noopener">Billing for the project</a>, press <b>Link a billing account</b> (or <b>Create billing account</b>) and add a card. Google asks for one even for the free amount; Lead Desk's limits keep the bill at $0 unless you raise them.</li>
         <li>Open <a href="https://console.cloud.google.com/apis/library/places.googleapis.com" target="_blank" rel="noopener">Places API (New)</a>, check the project name at the top says Lead Desk, and press <b>Enable</b>.</li>
         <li>Open <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener">Credentials</a>, press <b>+ Create credentials</b>, then <b>API key</b>. Press the copy button next to the key.</li>
         <li>Safer, and takes a minute: press <b>Edit API key</b>, under <b>API restrictions</b> pick <b>Restrict key</b>, tick <b>Places API (New)</b> and press <b>Save</b>.</li>
         <li>Paste the key in the box below and press <b>Save key and find phones</b>.</li></ol></details>
       <div class="row"><input id="setupKey" type="password" autocomplete="off" aria-label="Google Places API key" aria-describedby="setupKey-err" placeholder="Paste the Google Places API key" style="flex:1;min-width:240px">
         <button class="btn primary" id="setupKeySave">Save key and find phones</button></div>
       ${fieldError("setupKey")}`;
  const recState = rec.waiting ? done(`sent ${fmtDate(rec.last_request)}: waiting for the file`)
    : rec.last_import ? done(`last file imported ${fmtDate(rec.last_import)}`) : '<span class="chip">not sent yet</span>';
  const recDue = rec.due ? '<span class="chip warn">next one due now</span>' : `next one due ${esc(fmtDate(rec.due_on))}`;
  // What each step would reach, from the leads now open (counted on the
  // server, see leadlist.eviction_reach), and the one to do first: the
  // undone step that reaches the most leads.
  const lookN = c.lookup_leads || 0, firms = c.lookup_companies || 0, recN = c.records_leads || 0;
  const perDay = st.google_daily_limit, days = perDay ? Math.ceil(firms / perDay) : 0;
  const keyYield = evOpen ? `<p class="hint yield" id="yieldGoogle"><b>What it reaches:</b> up to ${lookN} of ${evOpen} open eviction lead${evOpen === 1 ? "" : "s"},
    the ones whose landlord is a company (${firms} compan${firms === 1 ? "y" : "ies"} not looked up yet). Google lists businesses, not private landlords, and won't have a number for every company.
    ${firms && perDay ? `At ${perDay} lookups a day that takes about ${days} day${days === 1 ? "" : "s"}.` : ""}</p>` : "";
  const recYield = evOpen ? `<p class="hint yield" id="yieldRecords"><b>What it reaches:</b> up to ${recN} of ${evOpen} open eviction lead${evOpen === 1 ? "" : "s"},
    the ones with no address a door hanger can go to. The file lists the property for every case filed in the dates you ask for (a unit number too, where the court has one); the court takes days to answer. An address gives a door hanger, not a phone number.</p>` : "";
  const todo = [!googleReady() && { id: "google", n: lookN }, !recordsStarted() && { id: "records", n: recN }].filter(Boolean);
  const first = todo.length ? todo.reduce((a, b) => (b.n > a.n ? b : a)).id : null;
  const startHere = id => id === first && todo.length > 1 ? ' <span class="chip acc" title="Of the steps left, this one reaches the most open eviction leads">start here</span>' : "";
  const steps = {
    google: `<li id="setupGoogle"><strong>Add a Google Places key</strong>${startHere("google")} ${keyStep}${googleReady() ? "" : keyYield}</li>`,
    records: `<li id="setupRecords"><strong>Ask the court for the property addresses, and last month's judgments and writs</strong>${startHere("records")} ${recState} · ${recDue}
        <p class="hint">The Justice Court's records request lists each eviction case with its property address. When the file comes, import it and matching cases fill in. Ask every ${rec.every_days || 14} days.
        ${rec.first ? "The first request reaches back a month: the court calendar only lists upcoming hearings, so the judgments and writs of the last weeks, the units to clear now, only come in this way." : ""}</p>
        ${recordsStarted() ? "" : recYield}
        ${addrLine || ""}
        <div class="row"><button class="btn" id="setupRecOpen">Show the records request steps</button>
        ${rec.waiting ? "" : `<button class="btn" data-recsent>I've sent the request</button>`}</div></li>`,
  };
  const order = first === "records" ? ["records", "google"] : ["google", "records"];
  return `<div class="card mb12 setup" id="setupGuide">
    <h2 id="setupTitle" tabindex="-1">Get phone numbers and addresses for eviction leads${setupDone() ? "" : ` <span class="chip warn">${googleReady() || recordsStarted() ? "1 step" : "2 steps"} to do</span>`}</h2>
    <p class="hint">Court cases name the landlord and the tenant but list no phone number and no property address, so a new eviction can't be called or visited until Lead Desk finds them. <b>Find phone</b> on a lead's row finds one number now, by hand. These two steps fill in most of the rest by themselves${first && todo.length > 1 ? "; start with the one marked" : ""}:</p>
    <ol class="setup-steps">
      ${order.map(id => steps[id]).join("")}
    </ol>
    <button class="linkbtn" id="setupHide">Close</button>
  </div>`;
}
async function saveGoogleKeyAndFind(body, btn) {
  const r = await act(() => api("/api/settings", body), "Google key saved. Looking up landlord phones now.", btn,
    undefined, e => showFieldErrors(e, { google_places_api_key: "setupKey" }));
  if (r) await findContacts();
}
function bindSetupGuide() {
  const save = $("#setupKeySave"), key = $("#setupKey");
  if (save) save.onclick = e => {
    const v = key.value.trim();
    if (!v) return setFieldError("setupKey", "Paste the Google Places API key first.");
    if (v.length > 300) return setFieldError("setupKey", "That's longer than a Google key (up to 300 characters). Copy just the key.");
    saveGoogleKeyAndFind({ google_places_api_key: v, google_enabled: true }, e.currentTarget);
  };
  if (key) { key.oninput = () => clearFieldError("setupKey"); key.onkeydown = e => { if (e.key === "Enter") save.click(); }; }
  const on = $("#setupGoogleOn");
  if (on) on.onclick = e => saveGoogleKeyAndFind({ google_enabled: true }, e.currentTarget);
  const find = $("#setupFind");
  if (find) find.onclick = e => findContacts(e.currentTarget);
  $("#setupRecOpen").onclick = async () => { Object.assign(ui, { type: "address_work", offset: 0 }); syncUrl(); await reloadList(); const r = $("#recordsCard h2"); if (r) r.scrollIntoView(); };
  $("#setupHide").onclick = () => { ui.showSetup = false; renderLeads(); $("#setupShow").focus(); };
  bindRecordsSent($("#setupGuide"));
}
// "I've sent the request": the next one is due in two weeks, and the guide shows it's waiting for the file.
function bindRecordsSent(root) {
  root.querySelectorAll("[data-recsent]").forEach(b => b.onclick = () =>
    act(() => api("/api/settings", { records_requested: true }), "Noted. Import the court's file when it arrives; the next request is due in two weeks.", b));
}
