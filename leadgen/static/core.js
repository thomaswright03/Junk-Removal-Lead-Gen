// Lead Desk page: shared state, talking to the server, formatting helpers
// and the header. The other scripts draw one part of the page each.
"use strict";
let S = null;                 // server state: settings, counts, one page of leads (S.list), the open lead (S.lead)
const ui = { tab: "leads", q: "", type: "", status: "open", channel: "", sort: "score", offset: 0, outreachTab: "door_hanger", open: null };

const $ = (s, el = document) => el.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = v => v == null ? "–" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 });
const pct = v => v == null ? "–" : (v * 100).toFixed(v < 0.1 && v > 0 ? 1 : 0) + "%";
const title = s => String(s ?? "").toLowerCase().replace(/\b\w/g, c => c.toUpperCase())
  .replace(/\b(Llc|Lllp|Lp|Po|Nw|Ne|Sw|Se|Hoa|Usa|Az)\b/g, w => w.toUpperCase());

const OFFLINE = "Lead Desk didn't answer. Check that it is still running (and your internet connection), then try again.";
async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  return send(path, opt);
}
async function send(path, opt) {
  let r;
  try { r = await fetch(path, opt); } catch (e) { throw new Error(OFFLINE); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = new Error(j.error || (r.status >= 500 ? "Something went wrong on the Lead Desk server. Try again in a minute." : "That didn't work. Reload the page and try again."));
    e.field = j.field || null;  // the form field the server refused, when it says
    throw e;
  }
  return j;
}
function toast(msg, ms = 3200, undo) {
  const t = $("#toast");
  t.innerHTML = esc(msg) + (undo ? ` <button class="btn small" id="toastUndo">Undo</button>` : "");
  t.style.display = "block";
  if (undo) $("#toastUndo").onclick = () => { t.style.display = "none"; undo(); };
  clearTimeout(toast.t); toast.t = setTimeout(() => t.style.display = "none", undo ? Math.max(ms, 8000) : ms);
}
// Ask before an action, in the page's own dialog: the buttons name the
// action ("Assign 40 leads", "Remove key"). Resolves true for the action,
// false for Cancel, Escape or a click outside.
function confirmBox({ title: head, body, ok, danger }) {
  const dlg = $("#confirmBox");
  $("#confirmTitle").textContent = head;
  $("#confirmBody").textContent = body || "";
  const okBtn = $("#confirmOk");
  okBtn.textContent = ok; okBtn.classList.toggle("danger-fill", !!danger); okBtn.classList.toggle("primary", !danger);
  const back = document.activeElement;
  return new Promise(resolve => {
    dlg.returnValue = "cancel";
    dlg.onclose = () => { resolve(dlg.returnValue === "ok"); if (back && back.focus && document.contains(back)) back.focus(); };
    dlg.onclick = e => { if (e.target === dlg) dlg.close("cancel"); };
    dlg.showModal();
    okBtn.focus();
  });
}
// What the page asks the server for: one page of the lead list for the tab
// that's showing (filtered, sorted and paged on the server) and the open lead.
const QUEUE_LIMIT = 500;
function stateQuery() {
  const p = new URLSearchParams();
  if (ui.tab === "leads") {
    p.set("list", "leads");
    for (const k of ["q", "type", "status", "channel", "sort", "offset"]) p.set(k, ui[k]);
  } else if (ui.tab === "settings") {
    p.set("samples", "1");  // a lead of each kind, for the message previews
  } else if (ui.tab === "outreach") {
    // Calls and landlords: leads with a phone or email first, the ones to search for after.
    p.set("list", "queue"); p.set("channel", ui.outreachTab); p.set("sort", ui.outreachTab === "door_hanger" ? "score" : "contact"); p.set("limit", QUEUE_LIMIT);
    // Door hangers and calls: the leads still to do. Landlords: everyone in that method.
    if (ui.outreachTab === "property_manager") p.set("status", "active");
    else { p.set("status", "new"); p.set("untouched", "1"); }
  }
  if (ui.open != null) p.set("lead", ui.open);
  return "/api/state?" + p.toString();
}
async function load() {
  S = await api(stateQuery());
  render();
}
// Reload after a filter, page or tab change; a failure shows as a toast.
async function reloadList() {
  try { await load(); } catch (e) { toast(e.message, 8000); }
}
const listLeads = () => (S && S.list && S.list.leads) || [];
const findLead = id => (S.lead && S.lead.id === id ? S.lead : listLeads().find(l => l.id === id));
// Run one action: the button is disabled while it runs (so a double click
// can't log twice), the page reloads, then the result is shown in a toast
// (so the toast never describes leads the list isn't showing yet). A failure
// shows in a toast, unless ``onError`` shows it somewhere better (under the
// form field it is about) and returns true.
async function act(fn, okMsg, btn, undo, onError) {
  if (btn) { if (btn.disabled) return; btn.disabled = true; btn.setAttribute("aria-busy", "true"); }
  try {
    const r = await fn();
    await load();
    const msg = okMsg && (typeof okMsg === "function" ? okMsg(r) : okMsg);
    if (msg) toast(msg, Math.max(3200, msg.length * 60), undo);
    return r;
  }
  catch (e) { if (!(onError && onError(e))) toast(e.message, 8000); }
  finally { if (btn) { btn.disabled = false; btn.removeAttribute("aria-busy"); } }
}

// ---------- form field errors -----------------------------------------------
// A value that can't be saved is reported under its box, which is marked
// invalid (aria-invalid) and focused. The message stays, across redraws of
// the page, until the value is corrected. Keyed by the box's id.
const fieldErrors = {};
const fieldError = id => `<span class="field-error" id="${id}-err" role="alert"${fieldErrors[id] ? "" : " hidden"}>${esc(fieldErrors[id] || "")}</span>`;
// Marks the box invalid (after a redraw too); its error line must be in the page (fieldError).
function markField(id) {
  const box = document.getElementById(id); if (!box) return;
  const bad = !!fieldErrors[id];
  if (bad) box.setAttribute("aria-invalid", "true"); else box.removeAttribute("aria-invalid");
  const ids = (box.getAttribute("aria-describedby") || "").split(" ").filter(x => x && x !== id + "-err");
  if (bad) ids.push(id + "-err");
  if (ids.length) box.setAttribute("aria-describedby", ids.join(" ")); else box.removeAttribute("aria-describedby");
  const line = document.getElementById(id + "-err");
  if (line) { line.textContent = fieldErrors[id] || ""; line.hidden = !bad; }
}
function setFieldError(id, msg, focus = true) {
  fieldErrors[id] = msg; markField(id);
  const box = document.getElementById(id);
  if (box && focus) box.focus();
  return false;
}
function clearFieldError(id) { if (fieldErrors[id]) { delete fieldErrors[id]; markField(id); } }
const markFields = root => root.querySelectorAll("[id]").forEach(el => { if (fieldErrors[el.id]) markField(el.id); });
// The server's rules for what the forms send (forms.py, web.py), checked in
// the browser first so a mistake shows at once and nothing is sent. Each
// returns the server's own message, or null when the value is fine.
const MAX_JOB_DOLLARS = 100000, MAX_CONTACT_DOLLARS = 1000;
function checkMoney(box, label, most = MAX_JOB_DOLLARS) {
  if (box.validity && box.validity.badInput) return `${label} must be a dollar amount, like 250.`;
  const v = String(box.value ?? "").trim().replace(/[$,]/g, "");
  if (!v) return null;
  const n = Number(v);
  if (!isFinite(n)) return `${label} must be a dollar amount, like 250.`;
  if (n < 0) return `${label} can't be negative.`;
  if (n > most) return `${label} can be at most $${most.toLocaleString("en-US")}. Check for an extra zero.`;
  return null;
}
function checkPhone(v) {
  v = String(v ?? "").trim(); if (!v) return null;
  let d = v.replace(/\D/g, ""); if (d.length === 11 && d[0] === "1") d = d.slice(1);
  return d.length === 10 ? null : "The phone number needs 10 digits, like (520) 555-0100.";
}
const checkEmail = v => { v = String(v ?? "").trim(); return !v || /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v) ? null : "That email address doesn't look right. Check it and save again."; };
const checkText = (v, label, most = 300) => String(v ?? "").length > most ? `${label} must be text (up to ${most} characters).` : null;
function checkWhole(box, label) {
  if (box.disabled) return null;  // "no limit"
  if (box.validity && box.validity.badInput) return `${label} must be a whole number, or no limit.`;
  const v = String(box.value).trim(); if (!v) return null;  // blank is 0
  const n = Number(v);
  return isFinite(n) && n >= 0 && n === Math.floor(n) ? null : `${label} must be a whole number of 0 or more (0 means none).`;
}
// Check several boxes ({id: () => message or null}): each failure is shown
// under its box and the first is focused. True when all are fine.
function checkFields(checks) {
  let first = null;
  for (const [id, check] of Object.entries(checks)) {
    const msg = document.getElementById(id) ? check() : null;
    if (msg) { setFieldError(id, msg, false); first = first || id; } else clearFieldError(id);
  }
  if (first) document.getElementById(first).focus();
  return !first;
}
// While a box shows an error, typing re-checks it and clears the error once fixed.
function recheckOnInput(checks) {
  for (const [id, check] of Object.entries(checks)) {
    const box = document.getElementById(id); if (!box) continue;
    box.addEventListener("input", () => { if (fieldErrors[id] && !check()) clearFieldError(id); });
  }
}
// A refusal from the server about one field (``{"field": ...}``): shown under
// that field's box when ``boxes`` maps it to one on the page. True when shown.
function showFieldErrors(e, boxes) {
  const id = e && e.field && boxes[e.field];
  if (!id || !document.getElementById(id)) return false;
  setFieldError(id, e.message);
  return true;
}

// ---------- helpers ---------------------------------------------------------
const chName = c => S.channels[c] || "Unassigned";
const chDot = c => c ? `<span class="ch"><span class="dot" style="background:var(--c-${c})"></span>${esc(chName(c))}</span>` : `<span class="muted">–</span>`;
// The priority number, with what it is made of on hover (and read out by
// screen readers): "Eviction 35 · owner lives elsewhere 20 · filed 3 days ago 15".
const scoreParts = l => (l.score_parts || []).map(([t, p]) => `${t} ${p}`).join(" · ");
function scoreChip(l) {
  const s = l.score, cls = s >= 60 ? "good" : s >= 40 ? "acc" : s >= 25 ? "warn" : "";
  const why = scoreParts(l);
  return `<span class="score chip ${cls}" title="Priority ${s}: ${esc(why)}" aria-label="Priority ${s}: ${esc(why)}">${s}</span>`;
}
const STATUS_LABEL = { new: "New", contacted: "Contacted", responded: "Responded", quoted: "Quoted", won: "Won", lost: "Lost", skip: "Skip", stale: "Old" };
function statusChip(s) {
  const m = { new: "acc", contacted: "", responded: "warn", quoted: "warn", won: "good", lost: "bad", skip: "", stale: "" };
  return `<span class="chip ${m[s] || ""}">${esc(STATUS_LABEL[s] || s)}</span>`;
}
function whatLabel(l) {
  if (l.lead_type === "eviction") return "Eviction";
  if (l.lead_type === "civil") return "Civil case";
  return l.code_label || (l.description || "").split(" | ")[0] || l.lead_type;
}
function mapUrl(l) {
  if (l.lat != null && l.lon != null) return `https://www.google.com/maps/search/?api=1&query=${l.lat},${l.lon}`;
  if (l.address) return "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(l.address + ", " + (l.city || "Tucson") + ", AZ");
  return null;
}
function ownerLine(l) {
  const who = l.owner_name || l.plaintiff;
  if (!who) return `<span class="muted">${l.enriched_at ? "not found" : "lookup pending"}</span>`;
  let tags = "";
  if (l.owner_absentee) tags += ` <span class="chip warn" title="Owner's mailing address is somewhere else">owner lives elsewhere</span>`;
  if (l.owner_entity) tags += ` <span class="chip" title="Company, trust or estate">company owner</span>`;
  if (l.owner_lead_count > 1) tags += ` <span class="chip acc" title="Owner has several leads">${l.owner_lead_count} leads</span>`;
  return esc(title(who)) + tags;
}
const fullAddress = l => l.address ? l.address + (l.unit ? " #" + String(l.unit).replace(/^#/, "") : "") : "";
const isMultifamily = l => /APART|MULTI|MFR|CONDO|TOWNHOUSE|MOBILE HOME PARK/i.test(l.property_use || "");
// Where an address came from, when it isn't certain: a guess from the
// landlord's parcels, or an apartment complex with no unit number.
function addressNote(l, plain) {
  const notes = [];
  if (l.address_source === "landlord") notes.push([`landlord's only ${isMultifamily(l) ? "complex" : "property"} — confirm`,
    "Court cases list no address. The landlord owns one property in the county, so the eviction is probably there. Check it, then press Confirm address on the lead."]);
  if (l.door_hanger_problem === "needs_unit") notes.push(["unit needed for a door hanger", "An apartment or condo parcel: type the unit number, or confirm the address, before a door hanger goes out."]);
  if (plain) return notes.map(n => n[0]).join("; ");
  return notes.map(([t, tip]) => ` <span class="chip warn" title="${esc(tip)}">${esc(t)}</span>`).join("");
}
// The one message renderer: fills a template from Settings for one lead.
// Phone calls on eviction cases use the landlord script, code cases the owner one.
const templateKey = (channel, l) => channel === "phone" && l.lead_type === "eviction" ? "phone_eviction" : channel;
function fill(channel, l) {
  const st = S.settings;
  return fillText(st.templates[templateKey(channel, l)] || st.templates[channel] || "", channel, l);
}
// The fields a template can use, with what each becomes (Settings shows them as buttons).
const TEMPLATE_FIELDS = [["{owner}", "Owner name", "The owner's or landlord's name"],
  ["{owner_first}", "First name", "The owner's first name, or “there” for a company or trust"],
  ["{address}", "Address", "The property address, or “your property” when it isn't known"],
  ["{at_address}", "“at” address", "“ at 123 Main St” when the address is known, otherwise nothing"],
  ["{phone}", "Your phone", "This method's tracking number, or your main phone"], ["{business}", "Business name", "Your business name"]];
function fillText(text, channel, l) {
  const st = S.settings, owner = (l.owner_name || l.plaintiff || "").trim();
  // Worked out on the server: blank for companies, trusts and apartments.
  const first = l.owner_first || "";
  const phone = (st.tracking_numbers || {})[channel] || st.business_phone || "[phone]";
  const addr = title(fullAddress(l));
  return text
    .replaceAll("{owner}", title(owner) || "Property Owner")
    .replaceAll("{owner_first}", first || "there")
    .replaceAll("{at_address}", addr ? ` at ${addr}` : "")
    .replaceAll("{address}", addr || "your property")
    .replaceAll("{phone}", phone)
    .replaceAll("{business}", st.business_name || "");
}
// Who a method reaches on this lead and what it offers, in one line.
function pitchLine(channel, l) {
  const p = (S.pitches || {})[templateKey(channel, l)];
  return p ? `<p class="small-line pitch" data-pitch="${esc(channel)}"><b>Reaches:</b> ${esc(p.who)} · <b>Offer:</b> ${esc(p.offer)}</p>` : "";
}
// Where the eviction case is: notice filed, judgment, writ (lockout), etc.
function noticeChip(l) {
  if (l.lead_type === "code_violation") return ` <span class="chip acc" title="Open City of Tucson code-enforcement case (code cases cover the City of Tucson only)">city code case</span>`;
  if (l.lead_type !== "eviction") return "";
  const stage = l.case_stage;
  if (stage === "writ") return ` <span class="chip bad" title="Writ of restitution: the tenant is being locked out, so the unit needs clearing now">writ issued${l.writ_date ? " " + esc(fmtDate(l.writ_date)) : ""}</span>`;
  if (stage === "judgment") return ` <span class="chip warn" title="The court ruled for the landlord; a writ (lockout) usually follows within days">judgment${l.judgment_date ? " " + esc(fmtDate(l.judgment_date)) : ""}</span>`;
  if (stage === "dismissed") return ` <span class="chip" title="The case was dismissed">dismissed</span>`;
  if (l.eviction_notice === 1) return ` <span class="chip good" title="An eviction notice is filed in the court case">notice filed</span>`;
  if (l.eviction_notice === 0) return ` <span class="chip" title="No eviction notice in the case documents yet">no notice yet</span>`;
  return ` <span class="chip warn" title="${l.url ? "Case page not read yet; the daily check reads it, or press Update court cases." : "Added by you, with no court case link to check."}">${l.url ? "case not checked" : "added by you"}</span>`;
}
// One date format everywhere: "Oct 1, 2026" and "2:00 PM". Plain dates are
// shown as they are; timestamps (stored in UTC) are shown in Tucson time.
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const fmtTime = (h, m) => `${(+h % 12) || 12}:${m} ${+h < 12 ? "AM" : "PM"}`;
function fmtDate(v) {
  if (!v) return "";
  const s = String(v);
  if (/T\d\d:\d\d/.test(s)) {
    const d = new Date(s);
    if (!isNaN(d)) return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "America/Phoenix" });
  }
  const m = s.match(/^(\d{4})-(\d\d)-(\d\d)/);
  return m ? `${MONTHS[+m[2] - 1]} ${+m[3]}, ${m[1]}` : s;
}
const courtDate = v => { if (!v) return ""; const m = String(v).match(/ (\d\d):(\d\d)$/); return fmtDate(v) + (m ? `, ${fmtTime(m[1], m[2])}` : ""); };
// Every date says what it is: the latest real event (Filed, Judgment, Writ,
// Opened), or for a case not read yet, its upcoming Hearing.
function leadDate(l) {
  if (l.latest_date) return [l.latest_label, l.latest_date];
  if (l.event_date) return [l.date_label || "Dated", l.event_date];
  return [null, null];
}
function dateCell(l) {
  const [label, d] = leadDate(l);
  return d ? `<span class="small-line">${esc(label)}</span><br>${esc(fmtDate(d))}` : '<span class="muted">–</span>';
}
const fmtDateTime = v => { if (!v) return ""; const d = new Date(v); return isNaN(d) ? fmtDate(v) : d.toLocaleString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/Phoenix" }); };
const VIEW_LABEL = { eviction_notice: "Evictions with a notice, judgment or writ", evictions: "All evictions", all: "All leads (City code cases too)" };
// Where City code cases come from: say it wherever they are offered.
const CODE_COVERAGE = "City code cases cover the City of Tucson only; unincorporated Pima County, Marana, Oro Valley, Sahuarita and South Tucson aren't included.";
const SOURCE_LABEL = { manual: "entered by hand", import: "imported file", osm: "OpenStreetMap", google: "Google Places",
  "osm+website": "OpenStreetMap + company website", "google+website": "Google Places + company website", website: "company website" };
function phoneCell(l) {
  return l.owner_phone ? `<a href="tel:${esc(l.owner_phone.replace(/\D/g, ""))}">${esc(l.owner_phone)}</a>` : '<span class="muted">–</span>';
}
// Evictions with no phone, email or known address can't be contacted yet.
function reachChip(l) {
  if (l.lead_type !== "eviction" || l.reach !== "none") return "";
  return ` <span class="chip warn" title="No phone, email or confirmed property address yet. Find landlord phones, or a court records request, fills these in.">can't reach yet</span>`;
}
function emailCell(l) {
  return l.owner_email ? `<a href="mailto:${esc(l.owner_email)}">${esc(l.owner_email)}</a>` : '<span class="muted">–</span>';
}
const touchButtons = ch => (S.touch_kinds || {})[ch] || [];

// ---------- long jobs -------------------------------------------------------
// "Update court cases" and "Find landlord phones" run in the background on
// this computer (progress shows in the header); online they do one batch.
const PAUSED_STOP = " Stopped because Lead Desk was paused.";
const JOB_DONE = {
  cases: r => `Re-read ${r.checked} court case${r.checked === 1 ? "" : "s"}, ${r.with_notice} with an eviction notice` + (r.failed ? `, ${r.failed} couldn't be read` : "")
    + (r.paused ? "." + PAUSED_STOP : r.cancelled ? " (cancelled)" : r.stopped_early ? ". More remain; press again to continue." : "."),
  contacts: r => `Checked ${r.checked} companies: contacts found for ${r.found} leads, none for ${r.not_found}.` +
    (r.google_used ? "" : " Add a Google Places key in Settings to find more.")
    + (r.errors ? ` ${r.errors} lookup${r.errors === 1 ? "" : "s"} failed; they'll be tried again${r.error_cause === "google_key" ? ". Google refused the key: check it in Settings" : ""}.` : "")
    + (r.paused ? PAUSED_STOP : r.cancelled ? " (cancelled)" : ""),
};
async function startJob(name, path, body, btn) {
  await act(() => api(path, body), r => r.paused && !r.result ? r.message || "Lead Desk is paused." : (!r.started && !r.result) ? r.message
    : r.result ? JOB_DONE[name](r.result) : "Started. Progress shows at the top of the page.", btn);
}
const updateCases = btn => startJob("cases", "/api/cases/update", { force: true }, btn);
const findContacts = btn => startJob("contacts", "/api/find-contacts", {}, btn);
async function addCases(text, btn) {
  if (!text.trim()) return toast("Paste one or more case page links first.");
  await act(() => api("/api/cases/add", { text }), r => r.paused && !(r.new + r.updated) ? r.message || "Lead Desk is paused." :
    `Cases: ${r.new} new, ${r.updated} updated, ${r.with_notice} with an eviction notice` +
    (r.failed ? `, ${r.failed} could not be read (the court site may be busy; try again later)` : "") + (r.skipped.length ? `. Skipped (not case links): ${r.skipped.slice(0, 3).join(", ")}` : "")
    + (r.paused ? "." + PAUSED_STOP : ""), btn);
}
async function importContacts(file) {
  await act(async () => send(`/api/import?source=contacts&filename=${encodeURIComponent(file.name)}`, { method: "POST", body: await file.arrayBuffer() }),
    r => `Phones and emails: ${r.matched} of ${r.rows} rows matched a lead, ${r.updated} leads filled in.`
    + (r.skipped_manual ? ` ${r.skipped_manual} row${r.skipped_manual > 1 ? "s" : ""} skipped because the lead has a number entered by hand.` : "")
    + (r.kept_existing ? ` ${r.kept_existing} lead${r.kept_existing > 1 ? "s" : ""} kept the different number they already had.` : "")
    + (r.rows && !r.matched ? " No rows matched: the file needs a lead_id, parcel, property address or owner name column." : ""));
}

// ---------- header ----------------------------------------------------------
const runningJobs = () => Object.values(S.jobs || {}).filter(j => j.running);
function jobLine(j) {
  const n = j.total ? `${j.done} of ${j.total}` : "starting";
  return `<span class="job">${esc(j.label)}: ${n}${j.total ? ` <progress max="${j.total}" value="${j.done}"></progress>` : ""}
    ${j.cancelling ? "stopping…" : `<button class="btn small" data-cancel="${esc(j.name)}">Cancel</button>`}</span>`;
}
function renderHeader() {
  const c = S.counts || {};
  const d = S.daily || {};
  const unchecked = (S.view_counts || {}).unchecked || 0;
  const msg = d.message || "starting the daily check";
  const problems = d.problems || [];
  $("#purpose").textContent = `· clean-out job leads for ${S.settings.business_name || "your junk-removal business"}: Pima County evictions and City of Tucson code cases`;
  $("#purpose").title = $("#purpose").textContent.slice(2);  // in full, when a wide header cuts it short
  // One short line: open leads, when the last check ran, a warning sign if
  // anything failed. The rest is under Details.
  const when = d.running ? `${msg[0].toUpperCase() + msg.slice(1)}…`
    : d.retry ? `Today's check failed (${d.retry.failed.join(", ")}) · trying again ${(d.next_run || "today " + d.retry.time).replace(/ \(.*\)$/, "")}`
    : d.interrupted ? `Today's check stopped part way (paused) · next check ${d.next_run || "within the next few minutes"}`
    : d.last_run ? `Last check ${d.finished_at ? fmtDateTime(d.finished_at) : fmtDate(d.last_run)}` : "Not checked yet";
  const warn = problems.length && !d.running ? ` <span class="warn-sign" role="img" aria-label="Something failed in the last check" title="${esc(problems.join("; "))}">⚠</span>` : "";
  const open = !!ui.headerDetails;
  $("#sub").innerHTML = `<span role="status" aria-live="polite">${c.active || 0} open leads · ${esc(when)}</span>${warn}
    <button class="linkbtn" id="subMore" aria-expanded="${open}" aria-controls="subDetails">${open ? "Hide details" : "Details"}</button>`;
  const details = [
    `${c.assigned || 0} in outreach`,
    c.evictions_open ? `${c.evictions_reachable || 0} of ${c.evictions_open} open eviction leads can be reached (phone, email or known address)` : "",
    c.owners_pending ? `${c.owners_pending} owners not looked up yet` : "",
    unchecked ? `${unchecked} court case${unchecked > 1 ? "s" : ""} still to check (${d.running ? "checking now" : "next check " + (d.next_run || "tomorrow 6:00 AM")})` : "",
    d.last_run && d.summary ? `Last check ${fmtDate(d.last_run)}: ${d.summary}` : "",
    problems.length ? `Problems: ${problems.join("; ")} (tried again on the next check)` : "",
  ].filter(Boolean);
  const box = $("#subDetails");
  box.hidden = !open;
  box.innerHTML = details.map(t => `<div>${esc(t)}</div>`).join("");
  $("#subMore").onclick = () => { ui.headerDetails = !ui.headerDetails; renderHeader(); $("#subMore").focus(); };
  $("#jobs").innerHTML = runningJobs().map(jobLine).join(" · ");
  $("#jobs").querySelectorAll("[data-cancel]").forEach(b => b.onclick = () =>
    act(() => api("/api/job/cancel", { name: b.dataset.cancel }), r => r.message, b));
  const banner = $("#pausedBanner");
  banner.hidden = !S.paused;
  if (S.paused) banner.innerHTML = `<strong>Paused.</strong> The daily check, court case reads and phone lookups are stopped${S.paused_by_env ? " by LEADDESK_PAUSED in the server settings" : ""}.
    ${S.paused_by_env ? "" : `<button class="btn small" id="unpause">Turn the pause off</button>`}`;
  const up = $("#unpause"); if (up) up.onclick = e => act(() => api("/api/settings", { paused: false }), r => r.message || "Lead Desk is running again.", e.currentTarget);
  const b = $("#refreshBtn"); b.disabled = !!d.running || !!S.paused; b.textContent = d.running ? "Checking…" : "Check for new evictions";
  if (d.running || runningJobs().length) watch();
  document.querySelectorAll("#nav button").forEach(b => { b.classList.toggle("on", b.dataset.tab === ui.tab); b.setAttribute("aria-current", b.dataset.tab === ui.tab ? "page" : "false"); });
  for (const t of ["leads", "outreach", "results", "settings"]) $("#tab-" + t).hidden = t !== ui.tab;
}

// File pickers are labels styled as buttons: make them reachable with Tab too.
function bindLabels(root) {
  root.querySelectorAll("label.btn").forEach(lb => {
    lb.tabIndex = 0; lb.setAttribute("role", "button");
    lb.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); lb.querySelector("input").click(); } };
  });
}
// Lead rows open the drawer on click, or on Enter / Space when focused with Tab.
function bindRows(root) {
  root.querySelectorAll("tr.click[data-id], [data-open]").forEach(tr => {
    tr.tabIndex = 0;
    tr.setAttribute("aria-label", "Open lead " + (tr.dataset.label || ""));
    tr.onclick = e => {
      if (e.target.closest("a,button,input,select")) return;
      openLead(+(tr.dataset.id || tr.dataset.open), tr);
    };
    tr.onkeydown = e => {
      if (e.target !== tr || (e.key !== "Enter" && e.key !== " ")) return;
      e.preventDefault(); openLead(+(tr.dataset.id || tr.dataset.open), tr);
    };
  });
}
