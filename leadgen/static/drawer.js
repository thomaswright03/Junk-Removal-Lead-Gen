// The lead drawer: everything about one lead, its address, contact,
// outreach and result, with typed-but-unsaved changes kept as drafts.
"use strict";
// ---------- Lead drawer -----------------------------------------------------
// Unsaved typing in the drawer, by lead id. The drawer is redrawn after every
// action and by the background refresh; it fills its boxes from here first,
// so nothing typed is lost. Kept in sessionStorage so a reload keeps it too.
let drafts = {};
try { drafts = JSON.parse(sessionStorage.getItem("leaddesk.drafts") || "{}") || {}; } catch (e) { drafts = {}; }
function storeDrafts() { try { sessionStorage.setItem("leaddesk.drafts", JSON.stringify(drafts)); } catch (e) { /* private mode */ } }
const draftOf = (l, field) => { const d = drafts[l.id]; return d && field in d ? d[field] : (l[field] ?? ""); };
function setDraft(l, field, value) {
  const d = drafts[l.id] = drafts[l.id] || {};
  if (String(value) === String(l[field] ?? "")) delete d[field]; else d[field] = value;
  if (!Object.keys(d).length) delete drafts[l.id];
  storeDrafts();
}
function clearDrafts(id, fields) {
  const d = drafts[id]; if (!d) return;
  for (const f of fields) delete d[f];
  if (!Object.keys(d).length) delete drafts[id];
  storeDrafts();
}
const hasDraft = id => !!(drafts[id] && Object.keys(drafts[id]).length);
const RESULT_FIELDS = ["notes", "quote_amount", "job_revenue"];
const moneyField = v => v === "" || v == null ? null : Number(v);
// Unsaved notes, quote and revenue, sent along with any other drawer action
// so a status change or channel change saves them too.
function pendingResult(l) {
  const d = drafts[l.id] || {}, out = {};
  for (const f of RESULT_FIELDS) if (f in d) out[f] = f === "notes" ? d[f] : moneyField(d[f]);
  return out;
}
// Before leaving a lead with unsaved typing: ask, in the page's own dialog.
async function leaveDrawer() {
  if (ui.open == null || !hasDraft(ui.open)) return true;
  return confirmBox({ title: "Leave without saving?", body: "This lead has changes you haven't saved (notes, quote, contact or address). What you typed stays filled in if you come back to it.",
    ok: "Leave without saving" });
}
async function openLead(id, from) {
  if (ui.open != null && ui.open !== id && !(await leaveDrawer())) return;
  if (ui.open !== id) clearDrawerErrors();
  ui.open = id; ui.returnFocus = from || document.activeElement;
  renderDrawer(); syncUrl(true);
  const h = $("#drawer h2"); if (h) h.focus();
}
// Open a lead that may not be in the list showing (a lead a round left out):
// the page asks the server for it.
async function openAnyLead(id, from) {
  if (findLead(id)) return openLead(id, from);
  if (ui.open != null && ui.open !== id && !(await leaveDrawer())) return;
  if (ui.open !== id) clearDrawerErrors();
  ui.open = id; ui.returnFocus = from || document.activeElement;
  syncUrl(true); await reloadList();
  const h = $("#drawer h2"); if (h) h.focus();
}
async function closeDrawer() {
  if (!(await leaveDrawer())) return;
  const id = ui.open; ui.open = null; clearDrawerErrors(); $("#drawer").classList.remove("open"); syncUrl(true);
  const back = document.querySelector(`[data-id="${id}"]`) || ui.returnFocus;
  if (back && back.focus) back.focus();
}
// The open drawer is a dialog: Tab and Shift+Tab stay inside it.
$("#drawer").addEventListener("keydown", e => {
  if (e.key !== "Tab") return;
  const items = [...$("#drawer").querySelectorAll("a[href], button, input, select, textarea, [tabindex]:not([tabindex='-1'])")]
    .filter(el => !el.disabled && el.offsetParent !== null);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (e.shiftKey && (document.activeElement === first || !$("#drawer").contains(document.activeElement) || document.activeElement.id === "dTitle")) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});
// What the source said about the lead, as readable lines, leaving out what
// the drawer already shows (case status, notice, stage, the hearing).
// Why the door hanger option is off for a lead (see outreach.door_hanger_problem).
const DOOR_HANGER_WHY = { no_address: " (needs an address)", unconfirmed: " (the address is a guess: confirm it first)", needs_unit: " (needs the unit number)" };
function detailLines(l) {
  const parts = String(l.description || "").split(" | ").map(p => p.trim()).filter(Boolean);
  if (l.lead_type === "code_violation") {
    const [kind, status, ...rest] = parts.length >= 3 ? parts : [null, null, ...parts];
    return [rest.join(" — "), kind && `City case type: ${kind}`, status && `City status: ${status}`].filter(Boolean);
  }
  const hearingShown = !!l.next_court_date || l.date_label === "Hearing";
  return parts.filter(p => !(
    (/^case /i.test(p) && l.case_status) || /eviction notice/i.test(p) ||
    /^(writ of restitution|judgment for the landlord|judgment satisfied|case dismissed|case closed with)/i.test(p) ||
    (hearingShown && /hearing|eviction action/i.test(p))));
}
// Court cases list no property address: the ways to find it, in one place.
function addressGuide(l) {
  const tenant = (l.defendant || "").split(";")[0].trim();
  const steps = [
    l.plaintiff || l.owner_name ? "Check the landlord's other properties (button below) and pick the likely one." : "",
    tenant ? `Look up the tenant: <a href="https://www.google.com/search?q=${encodeURIComponent(tenant + " Tucson AZ")}" target="_blank" rel="noopener">search “${esc(title(tenant))}”</a>.` : "",
    "Ask the landlord when you call.",
    "A Justice Court records request lists property addresses: import its CSV under More tools (Import a court file) and matching cases fill in.",
  ].filter(Boolean);
  return `<p class="hint"><strong>Find the address:</strong></p><ol class="hint guide">${steps.map(t => `<li>${t}</li>`).join("")}</ol>`;
}
// The drawer's boxes, the server's rules for each, and the name the server
// uses for each in an error (see checkFields).
const DRAWER_CHECKS = {
  dQuote: () => checkMoney($("#dQuote"), "Quote"),
  dRev: () => checkMoney($("#dRev"), "Job revenue"),
  dNotes: () => { const n = $("#dNotes").value.length, max = S.notes_limit || 2000;
    return n > max ? `Notes can be up to ${max.toLocaleString("en-US")} characters; this is ${n.toLocaleString("en-US")}. Shorten it and save again.` : null; },
  dPhone: () => checkPhone($("#dPhone").value),
  dEmail: () => checkEmail($("#dEmail").value),
  dAddress: () => { const a = $("#dAddress").value.trim(), u = $("#dUnit").value.trim();
    return a.length > 200 ? "That address is too long. Type just the street address." : u && !a ? "Type the street address as well as the unit." : null; },
  dUnit: () => $("#dUnit").value.trim().replace(/^#/, "").trim().length > 20 ? "That unit is too long. Type just the unit number, like 12B." : null,
};
const DRAWER_FIELDS = { quote_amount: "dQuote", job_revenue: "dRev", notes: "dNotes", owner_phone: "dPhone", owner_email: "dEmail", address: "dAddress", unit: "dUnit" };
const pick = (obj, keys) => Object.fromEntries(keys.map(k => [k, obj[k]]));
const drawerOk = ids => checkFields(pick(DRAWER_CHECKS, ids));
const drawerError = e => showFieldErrors(e, DRAWER_FIELDS);
// Errors belong to the lead they were made on.
const clearDrawerErrors = () => Object.keys(DRAWER_CHECKS).forEach(id => delete fieldErrors[id]);
const REACH_TEXT = {
  both: "Phone or email, and a door hanger at the property.",
  contact: "Phone or email. No usable property address yet (a confirmed home, or a unit at a complex), so no door hanger.",
  address: "A door hanger or visit at the property. No phone or email yet.",
  none: "Nothing yet: no phone, email or usable address (a whole complex needs the unit). Search for the landlord's number (Find phone, below) and paste it under Contact, or find the address below.",
};
function renderDrawer() {
  const d = $("#drawer");
  const l = findLead(ui.open);
  if (!l) { d.classList.remove("open"); return; }
  // Keep the scroll position and the box being typed in across the redraw.
  const scroll = d.scrollTop, focusId = d.contains(document.activeElement) ? document.activeElement.id : null;
  const sel = focusId && document.activeElement.selectionStart != null ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
  const map = mapUrl(l);
  const who = l.owner_name || l.plaintiff;
  const ownerMail = l.owner_address ? `${esc(title(l.owner_address))}<br>${esc(title(l.owner_city))} ${esc(l.owner_state || "")} ${esc(l.owner_zip || "")}` : '<span class="muted">–</span>';
  const ch = l.channel;
  const dirty = hasDraft(l.id);
  d.innerHTML = `
    <button class="btn small close" id="dClose">Close</button>
    <h2 tabindex="-1" id="dTitle">${l.address ? esc(title(fullAddress(l))) : esc(title(l.plaintiff || l.source_id))}</h2>
    <div class="row">${scoreChip(l)} ${statusChip(l.status)} ${chDot(ch)}${dirty ? ' <span class="chip warn">unsaved changes</span>' : ""}</div>
    <p class="small-line" id="dWhy">Why it's priority ${l.score}: ${esc(priorityWhy(l))}</p>
    <dl>
      <dt>What</dt><dd>${esc(whatLabel(l))}${noticeChip(l)}</dd>
      ${l.case_status ? `<dt>Case status</dt><dd>${esc(l.case_status)}</dd>` : ""}
      ${l.next_court_date ? `<dt>Next hearing</dt><dd>${esc(courtDate(l.next_court_date))}</dd>` : ""}
      ${(lines => lines.length ? `<dt>Details</dt><dd>${lines.map(t => `<div>${esc(t)}</div>`).join("")}</dd>` : "")(detailLines(l))}
      ${leadDates(l)}
      <dt>Case</dt><dd>${esc(l.source_id)}${l.url ? ` · <a href="${esc(l.url)}" target="_blank" rel="noopener">${/jcDisplayCase/i.test(l.url) ? "court case page" : "source"}</a>` : ""}</dd>
      ${l.lead_type === "eviction" ? `<dt>Landlord</dt><dd>${esc(l.plaintiff || "")}</dd><dt>Tenant</dt><dd>${esc(l.defendant || "")}</dd>` : ""}
      <dt>Owner</dt><dd>${ownerLine(l)}</dd>
      <dt>Mailing address</dt><dd>${ownerMail}</dd>
      <dt>Property</dt><dd>${esc(title(l.property_use || ""))}${l.year_built ? ` · built ${esc(l.year_built)}` : ""}${l.parcel ? ` · parcel <a href="https://gis.pima.gov/D.HTM?P=${encodeURIComponent(l.parcel)}" target="_blank" rel="noopener">${esc(l.parcel)}</a>` : ""}</dd>
      <dt>Location</dt><dd>${map ? `<a href="${map}" target="_blank" rel="noopener">Open map</a>` : "–"}${l.miles != null ? ` · ${l.miles.toFixed(1)} mi from base` : ""}</dd>
      <dt>Phone</dt><dd>${phoneCell(l)}</dd>
      <dt>Email</dt><dd>${emailCell(l)}</dd>
      ${l.lead_type === "eviction" ? `<dt>Can reach by</dt><dd id="dReach">${esc(REACH_TEXT[l.reach] || "")}${reachChip(l)}</dd>` : ""}
      ${l.owner_website ? `<dt>Website</dt><dd><a href="${esc(/^https?:/i.test(l.owner_website) ? l.owner_website : "https://" + l.owner_website)}" target="_blank" rel="noopener">${esc(l.owner_website.replace(/^https?:\/\//i, ""))}</a></dd>` : ""}
      ${l.contact_source ? `<dt>Contact from</dt><dd class="muted">${esc(SOURCE_LABEL[l.contact_source] || l.contact_source)}${l.contact_name ? ` · matched “${esc(l.contact_name)}”` : ""}</dd>` : ""}
      ${who ? `<dt>Find phone</dt><dd><a href="https://www.google.com/search?q=${encodeURIComponent(who + " Tucson AZ phone")}" target="_blank" rel="noopener">Search the web</a>${l.owner_entity || l.plaintiff ? ` · <a href="https://ecorp.azcc.gov/EntitySearch/Index" target="_blank" rel="noopener">AZ Corp Commission</a> (lists the company's registered contact, its “statutory agent”)` : ""}</dd>` : ""}
    </dl>

    <div class="card mt16">
      <h2>Property address</h2>
      ${l.address ? `<p>${esc(title(fullAddress(l)))}${addressNote(l)}</p>` : ""}
      ${l.door_hanger_problem === "needs_unit" ? `<p class="notice hint" id="dUnitNeeded"><strong>Unit or space number needed.</strong> This is the whole ${isMultifamily(l) ? "complex" : "property"}, not the tenant's door, so no door hanger can go yet${(l.address_lead_count || 0) > 1 ? `, and ${l.address_lead_count - 1} other open case${l.address_lead_count > 2 ? "s are" : " is"} here too` : ""}. Ask the landlord for the unit when you call (about every case here at once), or find it in the court's records request, then type it in the Unit box below.</p>` : ""}
      ${l.address && l.address_source === "landlord" ? `<div class="row mb12"><button class="btn small" id="dConfirm">${l.door_hanger_problem === "needs_unit" ? "Confirm the complex" : "Confirm address"}</button>
        <span class="hint m0">Checked that the eviction is at this ${l.door_hanger_problem === "needs_unit" ? "complex? The unit is still needed for a door hanger." : "property?"}</span></div>` : ""}
      ${l.lead_type === "eviction" && (!l.address || l.address_source === "landlord") ? addressGuide(l) : ""}
      <p class="hint">${l.address ? "Correct it here if it's wrong." : "Court case pages don't list the property."} Saving finds it on the map, looks up the parcel and owner, fills in the miles, and makes the lead eligible for door hangers.</p>
      <div class="row">
        <input id="dAddress" data-draft="address" placeholder="Street address, e.g. 123 W Main St" value="${esc(draftOf(l, "address"))}" class="grow" aria-label="Property street address">
        <input id="dUnit" data-draft="unit" placeholder="${l.door_hanger_problem === "needs_unit" ? "Unit needed" : "Unit"}" value="${esc(draftOf(l, "unit"))}" class="w-110" aria-label="Unit or space number${l.door_hanger_problem === "needs_unit" ? " (needed for a door hanger)" : ""}">
        <button class="btn small" id="dSaveAddress">Save address</button>
        ${fieldError("dAddress")}${fieldError("dUnit")}
      </div>
      ${who && (l.owner_entity || l.lead_type === "eviction") ? `<div class="row mt8"><button class="btn small" id="dOwnerProps">Other properties this owner has</button></div><div id="ownerProps"></div>` : ""}
    </div>

    <div class="card">
      <h2>Contact</h2>
      <p class="hint">Check that a found number belongs to this owner before calling. Editing here marks it as entered by hand, and lookups and imports won't overwrite it.</p>
      <div class="row">
        <input id="dPhone" data-draft="owner_phone" placeholder="Phone" value="${esc(draftOf(l, "owner_phone"))}" class="w-150" aria-label="Phone">
        <input id="dEmail" data-draft="owner_email" placeholder="Email" value="${esc(draftOf(l, "owner_email"))}" class="grow" aria-label="Email">
        <button class="btn small" id="dSaveContact">Save contact</button>
        ${fieldError("dPhone")}${fieldError("dEmail")}
      </div>
    </div>

    <div class="card">
      <h2>Outreach</h2>
      <div class="row my8">
        <select id="dChannel" aria-label="Outreach method">${[["", "Not assigned"], ...Object.entries(S.channels)].map(([v, t]) => `<option value="${v}" ${v === (ch || "") ? "selected" : ""} ${v && !l.eligible.includes(v) ? "disabled" : ""}>${t}${v && !l.eligible.includes(v) ? (v !== "door_hanger" ? " (no one to contact)" : DOOR_HANGER_WHY[l.door_hanger_problem] || " (needs an address)") : v && !(l.ready || []).includes(v) ? (v === "phone" ? " (no phone number yet)" : " (no phone or email yet)") : ""}</option>`).join("")}</select>
        <button class="btn small" id="dSaveCh">Set method</button>
      </div>
      ${ch ? `${pitchLine(ch, l)}${phoneMissing(ch, l)}<div class="script">${esc(fill(ch, l))}</div>
      <div class="row mt8">
        ${touchButtons(ch).map(([k, t]) => `<button class="btn small" data-touch="${k}">${t}</button>`).join("")}
      </div>` : `<p class="hint">Pick a method to see the message and log outreach.</p>`}
      ${l.touches.length ? `<h3>History</h3>${l.touches.map(t => `<div class="row small-line">${esc(fmtDate(t.created_at))} · ${esc(chName(t.channel))} · ${esc(t.kind.replaceAll("_", " "))}${t.cost ? " · " + money(t.cost) : ""}${t.notes ? " · " + esc(t.notes) : ""}
        <button class="btn small" data-untouch="${t.id}" aria-label="Remove this ${esc(t.kind.replaceAll("_", " "))} entry">Remove</button></div>`).join("")}` : ""}
    </div>

    <div class="card">
      <h2>Result</h2>
      <div class="row my8">
        ${["responded", "quoted", "won", "lost", "skip"].map(s => `<button class="btn small ${l.status === s ? "primary" : ""}" data-status="${s}">${STATUS_LABEL[s]}</button>`).join("")}
        ${l.status !== "new" ? `<button class="btn small" data-status="new">Reset to new</button>` : ""}
      </div>
      <div class="row">
        <label>Quote $ <input id="dQuote" data-draft="quote_amount" type="number" min="0" step="1" class="w-100" value="${esc(draftOf(l, "quote_amount"))}"></label>
        <label>Job revenue $ <input id="dRev" data-draft="job_revenue" type="number" min="0" step="1" class="w-100" value="${esc(draftOf(l, "job_revenue"))}"></label>
        ${fieldError("dQuote")}${fieldError("dRev")}
      </div>
      <h3><label for="dNotes">Notes</label></h3>
      <textarea id="dNotes" data-draft="notes" aria-describedby="dNotesCount" placeholder="Who you talked to, what they need, next step…">${esc(draftOf(l, "notes"))}</textarea>
      <div class="counter" id="dNotesCount" aria-live="polite"></div>
      ${fieldError("dNotes")}
      <div class="row mt8"><button class="btn primary" id="dSave">Save result</button></div>
    </div>`;
  d.classList.add("open");
  d.scrollTop = scroll;
  if (focusId && $("#" + focusId)) { const f = $("#" + focusId); f.focus(); if (sel) try { f.setSelectionRange(...sel); } catch (e) { /* number box */ } }
  noteCounter();
  markFields(d);
  recheckOnInput(DRAWER_CHECKS);
  // A unit typed in or removed can fix the address box's error too.
  $("#dUnit").addEventListener("input", () => { if (fieldErrors.dAddress && !DRAWER_CHECKS.dAddress()) clearFieldError("dAddress"); });
  d.querySelectorAll("[data-draft]").forEach(i => i.oninput = () => {
    const before = hasDraft(l.id); setDraft(l, i.dataset.draft, i.value);
    if (i.id === "dNotes") noteCounter();
    if (before !== hasDraft(l.id)) renderDrawer();  // show or hide "unsaved changes"
  });
  const conf = $("#dConfirm");
  if (conf) conf.onclick = e => act(() => api("/api/lead", { id: l.id, fields: { confirm_address: true } }),
    r => r.message || "Address confirmed. Door hangers can go to this lead.", e.currentTarget);
  $("#dClose").onclick = closeDrawer;
  bindGotoSettings($("#drawer"));
  $("#dSaveContact").onclick = async e => {
    if (!drawerOk(["dPhone", "dEmail"])) return;
    const r = await act(() => api("/api/lead", { id: l.id, fields: { owner_phone: $("#dPhone").value, owner_email: $("#dEmail").value } }), "Contact saved", e.currentTarget, undefined, drawerError);
    if (r) { clearDrafts(l.id, ["owner_phone", "owner_email"]); renderDrawer(); }
  };
  $("#dSaveAddress").onclick = async e => {
    if (!drawerOk(["dAddress", "dUnit"])) return;
    const r = await act(() => api("/api/lead", { id: l.id, fields: { address: $("#dAddress").value, unit: $("#dUnit").value } }),
      r => r.message || "Address saved", e.currentTarget, undefined, drawerError);
    if (r) { clearDrafts(l.id, ["address", "unit"]); renderDrawer(); }
  };
  $("#dSaveCh").onclick = async e => {
    const extra = pendingResult(l);
    if (!drawerOk(["dQuote", "dRev", "dNotes"])) return;  // typed result fields go along
    const r = await act(() => api("/api/lead", { id: l.id, fields: { ...extra, channel: $("#dChannel").value } }), "Method saved", e.currentTarget, undefined, drawerError);
    if (r) { clearDrafts(l.id, Object.keys(extra)); renderDrawer(); }
  };
  d.querySelectorAll("[data-touch]").forEach(b => b.onclick = () =>
    act(() => api("/api/touch", { lead_id: l.id, kind: b.dataset.touch }), r => r.logged ? "Logged: " + b.textContent : "Already logged a moment ago", b));
  // Removing an entry offers Undo, which puts the same entry back (its time and cost too).
  d.querySelectorAll("[data-untouch]").forEach(b => b.onclick = async () => {
    let removed = null;
    await act(async () => { const r = await api("/api/touch/delete", { id: +b.dataset.untouch }); removed = r.removed; return r; },
      "Removed from the history", b,
      () => act(() => api("/api/touch/restore", { removed }), "Put back in the history"));
  });
  d.querySelectorAll("[data-status]").forEach(b => b.onclick = async () => {
    const extra = pendingResult(l), before = l.status, to = b.dataset.status;
    if (!drawerOk(["dQuote", "dRev", "dNotes"])) return;
    const r = await act(() => api("/api/lead", { id: l.id, fields: { ...extra, status: to } }),
      `Marked ${STATUS_LABEL[to].toLowerCase()}` + (Object.keys(extra).length ? " and saved your notes" : ""), b,
      () => act(() => api("/api/lead", { id: l.id, fields: { status: before } }), `Back to ${STATUS_LABEL[before].toLowerCase()}`), drawerError);
    if (r) { clearDrafts(l.id, Object.keys(extra)); renderDrawer(); }
  });
  $("#dSave").onclick = async e => {
    const btn = e.currentTarget;
    if (!drawerOk(["dQuote", "dRev", "dNotes"])) return;
    const quote = moneyField($("#dQuote").value), rev = moneyField($("#dRev").value);
    // Revenue means the job was done: Won. A lead marked Lost or Skip only
    // becomes Won when Steve says so; otherwise its status stays.
    // (A Skip lead only moves for a new quote, not one already saved on it.)
    let status = rev ? "won" : quote && !["won", "lost"].includes(l.status) && !(l.status === "skip" && quote === l.quote_amount) ? "quoted" : null;
    let kept = null;
    // Any amount that would move a Lost or Skip lead asks first.
    if (status && ["lost", "skip"].includes(l.status)) {
      const was = STATUS_LABEL[l.status], to = STATUS_LABEL[status];
      const why = status === "won" ? "You entered job revenue, which usually means you did the job."
        : "You entered a quote, which usually means the owner is interested again.";
      const yes = await confirmBox({ title: `Mark this ${was.toLowerCase()} lead as ${to.toLowerCase()}?`,
        body: `${why} Mark it ${to}, or keep it as ${was} and just save the amount and notes.`,
        ok: `Mark ${to.toLowerCase()}`, cancel: `Keep it ${was.toLowerCase()}` });
      if (!yes) { status = null; kept = was; }
    }
    // Keeping a Lost or Skip lead as it is says so to the server, which
    // otherwise marks a lead with new job revenue Won (or asks, for these).
    const r = await act(() => api("/api/lead", { id: l.id, fields: { quote_amount: quote, job_revenue: rev, notes: $("#dNotes").value,
      ...(status ? { status } : kept ? { status: l.status } : {}) } }), kept ? `Saved. The lead stays ${kept}.` : status === "won" ? "Saved and marked won" : status === "quoted" && l.status !== "quoted" ? "Saved and marked quoted" : "Saved", btn, undefined, drawerError);
    if (r) { clearDrafts(l.id, RESULT_FIELDS); renderDrawer(); }
  };
  const op = $("#dOwnerProps");
  if (op) op.onclick = async () => {
    op.disabled = true; $("#ownerProps").innerHTML = '<p class="hint">Looking up the county assessor…</p>';
    try {
      const rows = await api("/api/owner?name=" + encodeURIComponent(who));
      $("#ownerProps").innerHTML = rows.length ? `<p class="hint">${rows.length} parcel${rows.length > 1 ? "s" : ""} owned by names starting “${esc(who)}”. Pick one to use it as this lead's address.</p>
        <div class="tablewrap scrollbox"><table><tbody>${rows.map(r => `<tr><td>${esc(title(r.site_address || "(no site address)"))}</td><td class="muted">${esc(title(r.property_use || ""))}</td>
          <td>${r.site_address ? `<button class="btn small" data-use-address="${esc(r.site_address)}">Use</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`
        : '<p class="hint">No parcels found under that name. Landlords often own through a differently named LLC.</p>';
      $("#ownerProps").querySelectorAll("[data-use-address]").forEach(b => b.onclick = () => {
        const a = $("#dAddress"); a.value = b.dataset.useAddress; setDraft(l, "address", a.value); a.focus();
      });
    } catch (e) { $("#ownerProps").innerHTML = `<p class="hint">Couldn't look up the owner's properties: ${esc(e.message)}</p>`; op.disabled = false; }
  };
}
// "123 / 2,000": notes have a limit, shown as they are typed.
function noteCounter() {
  const box = $("#dNotes"), out = $("#dNotesCount"); if (!box || !out) return;
  const max = S.notes_limit || 2000, n = box.value.length;
  out.textContent = `${n.toLocaleString()} / ${max.toLocaleString()} characters` + (n > max ? ` — ${(n - max).toLocaleString()} over the limit; shorten the notes to save them` : "");
  out.classList.toggle("over", n > max);
}
// The lead's dates, each with what it is.
function leadDates(l) {
  const rows = [];
  if (l.event_date && l.date_label !== "Hearing") rows.push([l.date_label || "Date", fmtDate(l.event_date)]);
  if (l.judgment_date) rows.push(["Judgment", fmtDate(l.judgment_date)]);
  if (l.writ_date) rows.push(["Writ", fmtDate(l.writ_date)]);
  if (l.date_label === "Hearing" && l.event_date && !l.next_court_date) rows.push(["Hearing", fmtDate(l.event_date)]);
  return rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}
