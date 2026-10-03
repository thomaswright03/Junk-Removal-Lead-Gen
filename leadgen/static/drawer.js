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
  ui.open = id; ui.returnFocus = from || document.activeElement;
  renderDrawer(); syncUrl(true);
  const h = $("#drawer h2"); if (h) h.focus();
}
async function closeDrawer() {
  if (!(await leaveDrawer())) return;
  const id = ui.open; ui.open = null; $("#drawer").classList.remove("open"); syncUrl(true);
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
function detailLines(l) {
  const parts = String(l.description || "").split(" | ").map(p => p.trim()).filter(Boolean);
  if (l.lead_type === "code_violation") {
    const [kind, status, ...rest] = parts.length >= 3 ? parts : [null, null, ...parts];
    return [rest.join(" — "), kind && `City case type: ${kind}`, status && `City status: ${status}`].filter(Boolean);
  }
  const hearingShown = !!l.next_court_date || l.date_label === "Hearing";
  return parts.filter(p => !(
    (/^case /i.test(p) && l.case_status) || /eviction notice/i.test(p) ||
    /^(writ of restitution|judgment for the landlord|case dismissed)/i.test(p) ||
    (hearingShown && /hearing|eviction action/i.test(p))));
}
// Court cases list no property address: the ways to find it, in one place.
function addressGuide(l) {
  const tenant = (l.defendant || "").split(";")[0].trim();
  const steps = [
    l.plaintiff || l.owner_name ? "Check the landlord's other properties (button below) and pick the likely one." : "",
    tenant ? `Look up the tenant: <a href="https://www.google.com/search?q=${encodeURIComponent(tenant + " Tucson AZ")}" target="_blank" rel="noopener">search “${esc(title(tenant))}”</a>.` : "",
    "Ask the landlord when you call.",
    "A Justice Court records request lists property addresses: import its CSV with “Import court page / CSV” and matching cases fill in.",
  ].filter(Boolean);
  return `<p class="hint"><strong>Find the address:</strong></p><ol class="hint guide">${steps.map(t => `<li>${t}</li>`).join("")}</ol>`;
}
const REACH_TEXT = {
  both: "Phone or email, and a door hanger at the property.",
  contact: "Phone or email. No confirmed property address yet, so no door hanger.",
  address: "A door hanger or visit at the property. No phone or email yet.",
  none: "Nothing yet: no phone, email or confirmed address. Find landlord phones on the Leads tab, or find the address below.",
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
    <p class="small-line" id="dWhy">Priority ${l.score}: ${esc(scoreParts(l) || "no points yet")}</p>
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

    <div class="card" style="margin-top:16px">
      <h2>Property address</h2>
      ${l.address ? `<p>${esc(title(fullAddress(l)))}${addressNote(l)}</p>` : ""}
      ${l.address && (l.address_source === "landlord" || l.door_hanger_problem === "needs_unit") ? `<div class="row mb12"><button class="btn small" id="dConfirm">Confirm address</button>
        <span class="hint" style="margin:0">${l.address_source === "landlord" ? "Checked that the eviction is at this property?" : "No unit number, but a door hanger at this address is fine (for example at the leasing office)?"}</span></div>` : ""}
      ${l.lead_type === "eviction" && (!l.address || l.address_source === "landlord") ? addressGuide(l) : ""}
      <p class="hint">${l.address ? "Correct it here if it's wrong." : "Court case pages don't list the property."} Saving finds it on the map, looks up the parcel and owner, fills in the miles, and makes the lead eligible for door hangers.</p>
      <div class="row">
        <input id="dAddress" data-draft="address" placeholder="Street address, e.g. 123 W Main St" value="${esc(draftOf(l, "address"))}" style="flex:1;min-width:200px" aria-label="Property street address">
        <input id="dUnit" data-draft="unit" placeholder="Unit" value="${esc(draftOf(l, "unit"))}" style="width:80px" aria-label="Unit">
        <button class="btn small" id="dSaveAddress">Save address</button>
      </div>
      ${who && (l.owner_entity || l.lead_type === "eviction") ? `<div class="row" style="margin-top:8px"><button class="btn small" id="dOwnerProps">Other properties this owner has</button></div><div id="ownerProps"></div>` : ""}
    </div>

    <div class="card">
      <h2>Contact</h2>
      <p class="hint">Check that a found number belongs to this owner before calling. Editing here marks it as entered by hand, and lookups and imports won't overwrite it.</p>
      <div class="row">
        <input id="dPhone" data-draft="owner_phone" placeholder="Phone" value="${esc(draftOf(l, "owner_phone"))}" style="width:150px" aria-label="Phone">
        <input id="dEmail" data-draft="owner_email" placeholder="Email" value="${esc(draftOf(l, "owner_email"))}" style="flex:1;min-width:180px" aria-label="Email">
        <button class="btn small" id="dSaveContact">Save contact</button>
      </div>
    </div>

    <div class="card">
      <h2>Outreach</h2>
      <div class="row" style="margin:8px 0">
        <select id="dChannel" aria-label="Outreach method">${[["", "Not assigned"], ...Object.entries(S.channels)].map(([v, t]) => `<option value="${v}" ${v === (ch || "") ? "selected" : ""} ${v && !l.eligible.includes(v) ? "disabled" : ""}>${t}${v && !l.eligible.includes(v) ? (v !== "door_hanger" ? " (no one to contact)" : l.door_hanger_problem === "needs_unit" ? " (needs a unit number or a confirmed address)" : " (needs an address)") : ""}</option>`).join("")}</select>
        <button class="btn small" id="dSaveCh">Set method</button>
      </div>
      ${ch ? `${pitchLine(ch, l)}<div class="script">${esc(fill(ch, l))}</div>
      <div class="row" style="margin-top:8px">
        ${touchButtons(ch).map(([k, t]) => `<button class="btn small" data-touch="${k}">${t}</button>`).join("")}
      </div>` : `<p class="hint">Pick a method to see the message and log outreach.</p>`}
      ${l.touches.length ? `<h3>History</h3>${l.touches.map(t => `<div class="row muted" style="font-size:13px">${esc(fmtDate(t.created_at))} · ${esc(chName(t.channel))} · ${esc(t.kind.replaceAll("_", " "))}${t.cost ? " · " + money(t.cost) : ""}${t.notes ? " · " + esc(t.notes) : ""}
        <button class="btn small" data-untouch="${t.id}" aria-label="Remove this ${esc(t.kind.replaceAll("_", " "))} entry">Remove</button></div>`).join("")}` : ""}
    </div>

    <div class="card">
      <h2>Result</h2>
      <div class="row" style="margin:8px 0">
        ${["responded", "quoted", "won", "lost", "skip"].map(s => `<button class="btn small ${l.status === s ? "primary" : ""}" data-status="${s}">${STATUS_LABEL[s]}</button>`).join("")}
        ${l.status !== "new" ? `<button class="btn small" data-status="new">Reset to new</button>` : ""}
      </div>
      <div class="row">
        <label>Quote $ <input id="dQuote" data-draft="quote_amount" type="number" min="0" step="1" style="width:100px" value="${esc(draftOf(l, "quote_amount"))}"></label>
        <label>Job revenue $ <input id="dRev" data-draft="job_revenue" type="number" min="0" step="1" style="width:100px" value="${esc(draftOf(l, "job_revenue"))}"></label>
      </div>
      <h3><label for="dNotes">Notes</label></h3>
      <textarea id="dNotes" data-draft="notes" aria-describedby="dNotesCount" placeholder="Who you talked to, what they need, next step…">${esc(draftOf(l, "notes"))}</textarea>
      <div class="counter" id="dNotesCount" aria-live="polite"></div>
      <div class="row" style="margin-top:8px"><button class="btn primary" id="dSave">Save result</button></div>
    </div>`;
  d.classList.add("open");
  d.scrollTop = scroll;
  if (focusId && $("#" + focusId)) { const f = $("#" + focusId); f.focus(); if (sel) try { f.setSelectionRange(...sel); } catch (e) { /* number box */ } }
  noteCounter();
  d.querySelectorAll("[data-draft]").forEach(i => i.oninput = () => {
    const before = hasDraft(l.id); setDraft(l, i.dataset.draft, i.value);
    if (i.id === "dNotes") noteCounter();
    if (before !== hasDraft(l.id)) renderDrawer();  // show or hide "unsaved changes"
  });
  const conf = $("#dConfirm");
  if (conf) conf.onclick = e => act(() => api("/api/lead", { id: l.id, fields: { confirm_address: true } }),
    "Address confirmed. Door hangers can go to this lead.", e.currentTarget);
  $("#dClose").onclick = closeDrawer;
  $("#dSaveContact").onclick = async e => {
    const r = await act(() => api("/api/lead", { id: l.id, fields: { owner_phone: $("#dPhone").value, owner_email: $("#dEmail").value } }), "Contact saved", e.currentTarget);
    if (r) { clearDrafts(l.id, ["owner_phone", "owner_email"]); renderDrawer(); }
  };
  $("#dSaveAddress").onclick = async e => {
    const r = await act(() => api("/api/lead", { id: l.id, fields: { address: $("#dAddress").value, unit: $("#dUnit").value } }),
      r => r.message || "Address saved", e.currentTarget);
    if (r) { clearDrafts(l.id, ["address", "unit"]); renderDrawer(); }
  };
  $("#dSaveCh").onclick = async e => {
    const extra = pendingResult(l);
    const r = await act(() => api("/api/lead", { id: l.id, fields: { ...extra, channel: $("#dChannel").value } }), "Method saved", e.currentTarget);
    if (r) { clearDrafts(l.id, Object.keys(extra)); renderDrawer(); }
  };
  d.querySelectorAll("[data-touch]").forEach(b => b.onclick = () =>
    act(() => api("/api/touch", { lead_id: l.id, kind: b.dataset.touch }), r => r.logged ? "Logged: " + b.textContent : "Already logged a moment ago", b));
  d.querySelectorAll("[data-untouch]").forEach(b => b.onclick = () =>
    act(() => api("/api/touch/delete", { id: +b.dataset.untouch }), "Removed from the history", b));
  d.querySelectorAll("[data-status]").forEach(b => b.onclick = async () => {
    const extra = pendingResult(l), before = l.status, to = b.dataset.status;
    const r = await act(() => api("/api/lead", { id: l.id, fields: { ...extra, status: to } }),
      `Marked ${STATUS_LABEL[to].toLowerCase()}` + (Object.keys(extra).length ? " and saved your notes" : ""), b,
      () => act(() => api("/api/lead", { id: l.id, fields: { status: before } }), `Back to ${STATUS_LABEL[before].toLowerCase()}`));
    if (r) { clearDrafts(l.id, Object.keys(extra)); renderDrawer(); }
  });
  $("#dSave").onclick = async e => {
    const quote = moneyField($("#dQuote").value), rev = moneyField($("#dRev").value);
    const r = await act(() => api("/api/lead", { id: l.id, fields: { quote_amount: quote, job_revenue: rev, notes: $("#dNotes").value,
      ...(rev ? { status: "won" } : quote && !["won", "lost"].includes(l.status) ? { status: "quoted" } : {}) } }), "Saved", e.currentTarget);
    if (r) { clearDrafts(l.id, RESULT_FIELDS); renderDrawer(); }
  };
  const op = $("#dOwnerProps");
  if (op) op.onclick = async () => {
    op.disabled = true; $("#ownerProps").innerHTML = '<p class="hint">Looking up the county assessor…</p>';
    try {
      const rows = await api("/api/owner?name=" + encodeURIComponent(who));
      $("#ownerProps").innerHTML = rows.length ? `<p class="hint">${rows.length} parcel${rows.length > 1 ? "s" : ""} owned by names starting “${esc(who)}”. Pick one to use it as this lead's address.</p>
        <div class="tablewrap" style="max-height:260px;overflow:auto"><table><tbody>${rows.map(r => `<tr><td>${esc(title(r.site_address || "(no site address)"))}</td><td class="muted">${esc(title(r.property_use || ""))}</td>
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
