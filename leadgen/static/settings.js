// Settings tab.
"use strict";
const TEMPLATE_LABEL = { door_hanger: "Door hanger", phone: "Phone call: owner of a code-case property", phone_eviction: "Phone call: landlord on an eviction", property_manager: "Landlord / property manager pitch" };
// Which lead a template's preview uses: a code case for the code-case call,
// an eviction for the rest (falling back to whatever lead there is).
const previewChannel = c => c === "phone_eviction" ? "phone" : c;
function previewLead(c) {
  const sm = S.samples || {};
  return c === "phone" ? sm.code_violation || sm.eviction : sm.eviction || sm.code_violation;
}
function theme() { try { return localStorage.getItem("leaddesk.theme") || "system"; } catch (e) { return "system"; } }
function setTheme(t) {
  try { if (t === "system") localStorage.removeItem("leaddesk.theme"); else localStorage.setItem("leaddesk.theme", t); } catch (e) { /* storage blocked */ }
  applyTheme();
}
function limitField(id, value, step, label) {
  const unlimited = value == null;
  return `<label>${label} <input id="${id}" type="number" min="0" step="${step}" value="${unlimited ? "" : value}" style="width:90px" ${unlimited ? "disabled" : ""}></label>
    <label class="ch"><input type="checkbox" id="${id}None" ${unlimited ? "checked" : ""}> no limit</label>${fieldError(id)}`;
}
// The Settings boxes and the server's rules for each (see checkFields).
function settingsChecks() {
  const checks = {
    sName: () => !$("#sName").value.trim() ? "Type your business name: every door hanger and call script uses it. Nothing was saved."
      : checkText($("#sName").value.trim(), "Business name"),
    sPhone: () => checkText($("#sPhone").value.trim(), "Main phone"),
    sBase: () => checkText($("#sBase").value.trim(), "Base address"),
    sGoogle: () => checkText($("#sGoogle").value.trim(), "Google key"),
    sGoogleLimit: () => checkWhole($("#sGoogleLimit"), "Google lookups per month"),
    sGoogleDaily: () => checkWhole($("#sGoogleDaily"), "Google lookups per day"),
  };
  for (const c of Object.keys(S.channels)) checks["cost-" + c] = () => checkMoney($("#cost-" + c), `Cost per contact for ${S.channels[c]}`, MAX_CONTACT_DOLLARS);
  for (const c of Object.keys(S.settings.templates)) checks["tpl-" + c] = () => $("#tpl-" + c).value.length >= 5000 ? "A message can be up to 4,999 characters. Shorten it." : null;
  return checks;
}
// The name the server gives each setting in an error, and its box.
function settingsFields() {
  const out = { business_name: "sName", business_phone: "sPhone", base_address: "sBase", google_places_api_key: "sGoogle",
    google_monthly_limit: "sGoogleLimit", google_daily_limit: "sGoogleDaily" };
  for (const c of Object.keys(S.channels)) out["costs." + c] = "cost-" + c;
  for (const c of Object.keys(S.settings.templates)) out["templates." + c] = "tpl-" + c;
  return out;
}
function renderSettings() {
  const st = S.settings;
  $("#tab-settings").innerHTML = `
    <p class="hint">Your business details, outreach costs and messages, and the pause switch.</p>
    <div class="card"><h2>Pause</h2>
      <p class="hint">Stops everything that contacts other websites: the daily check, court case reads, owner and map lookups, City code case fetches, and all phone and email lookups (including paid Google lookups). Use it if the court asks you to stop or the Google bill rises. Your leads and notes stay as they are.${S.paused_by_env ? " <strong>LEADDESK_PAUSED is set on the server, so Lead Desk stays paused until it is removed there.</strong>" : ""}</p>
      <label class="ch"><input type="checkbox" id="sPaused" ${st.paused ? "checked" : ""} aria-describedby="sPausedNow"> Pause Lead Desk</label>
      <p class="hint" id="sPausedNow"><strong>Takes effect as soon as you tick or untick it</strong>; it doesn't wait for “Save settings”. Everything below this box is saved with the button at the bottom.</p>
    </div>
    <div class="card"><h2>Business</h2>
      <div class="grid4">
        <label>Business name<br><input id="sName" value="${esc(st.business_name)}" style="width:100%">${fieldError("sName")}</label>
        <label>Main phone<br><input id="sPhone" value="${esc(st.business_phone)}" placeholder="(520) 555-0100" style="width:100%">${fieldError("sPhone")}</label>
        <label>Base address (for miles and routes)<br><input id="sBase" value="${esc(st.base_address)}" style="width:100%">${fieldError("sBase")}</label>
      </div>
      <p class="hint" id="baseFound">${st.base_lat != null ? "Base address found on the map: miles and the door-hanger route start from it." : "Base address not found on the map yet. It's looked up when you save; if this stays, check the address (street, city and “AZ”). Miles and the route start from it once found."}</p>
    </div>
    <div class="card"><h2>Phone and email lookup</h2>
      <p class="hint">“Find landlord phones &amp; emails” always checks OpenStreetMap and company websites for free. A Google Places API key (Google Maps Platform, pay per lookup after the monthly free credit) finds far more office numbers. Google's terms limit how long results may be kept, so Google-found contacts are re-checked after 30 days.</p>
      <label class="ch"><input type="checkbox" id="sGoogleOn" ${st.google_enabled !== false ? "checked" : ""}> Use Google lookups</label>
      <div class="row" style="margin-top:8px"><input id="sGoogle" type="password" aria-label="Google Places API key" placeholder="${st.google_key_set ? "Key saved. Paste a new one to replace it" : "Google Places API key"}" style="flex:1;min-width:260px" autocomplete="off">
      ${st.google_key_from_env ? `<span class="chip good">key set on the server</span>` : st.google_key_set ? `<span class="chip good">key saved</span> <button class="btn small danger" id="sGoogleClear">Remove key</button>` : `<span class="chip">no key</span>`}${fieldError("sGoogle")}</div>
      ${st.google_key_from_env ? `<p class="hint">The key comes from GOOGLE_PLACES_API_KEY on the server (or the GitHub secret), so removing it here wouldn't stop it. Untick “Use Google lookups”, set the limits to 0, or pause Lead Desk to stop Google searches.</p>` : ""}
      <div class="row" style="margin-top:8px">${limitField("sGoogleLimit", st.google_monthly_limit, 100, "Google lookups per month, at most")}
      <span class="hint">${st.google_used_this_month || 0} used this month. Google gives 1,000 a month free, then charges about $35 per 1,000. 0 means none.</span></div>
      <div class="row" style="margin-top:8px">${limitField("sGoogleDaily", st.google_daily_limit, 1, "Google lookups per day, at most")}
      <span class="hint">${st.google_used_today || 0} used today. 0 means none.</span></div>
    </div>
    <div class="card"><h2>Outreach methods</h2>
      <p class="hint">A separate tracking phone number per method (Google Voice, CallRail and similar) is the cleanest way to know which one a caller came from. Cost is what one contact costs, including printing.</p>
      <div class="tablewrap"><table><thead><tr><th>Method</th><th>Cost per contact $</th><th>Tracking number</th></tr></thead><tbody>
      ${Object.entries(S.channels).map(([c, n]) => `<tr><td>${chDot(c)}</td><td><input type="number" step="0.01" min="0" id="cost-${c}" data-cost="${c}" value="${st.costs[c] ?? 0}" style="width:90px" aria-label="Cost per contact for ${esc(n)}">${fieldError("cost-" + c)}</td>
        <td><input data-track="${c}" value="${esc(st.tracking_numbers[c] || "")}" placeholder="uses main phone" aria-label="Tracking number for ${esc(n)}"></td></tr>`).join("")}</tbody></table></div></div>
    <div class="card"><h2>Messages</h2><p class="hint">What each outreach method says. Click a field button to put it where the cursor is; the preview shows the message for one of your leads.</p>
      ${Object.keys(st.templates).map(c => `<h3><label for="tpl-${c}">${esc(TEMPLATE_LABEL[c] || c)}</label></h3><textarea id="tpl-${c}" data-tpl="${c}">${esc(st.templates[c] || "")}</textarea>${fieldError("tpl-" + c)}
        <div class="row fields"><span class="small-line">Insert:</span>${TEMPLATE_FIELDS.map(([f, label, tip]) => `<button class="btn small" data-field="${esc(f)}" data-for="tpl-${c}" title="${esc(tip)}">+ ${esc(label)}</button>`).join("")}</div>
        <p class="small-line">Preview${previewLead(c) ? ` for ${esc(title(previewLead(c).owner_name || previewLead(c).plaintiff || previewLead(c).source_id))}` : " (no lead of this kind yet)"}:</p>
        <div class="script" id="pv-${c}" aria-live="polite"></div>`).join("")}
    </div>
    <div class="card"><h2>Appearance</h2>
      <label>Theme <select id="sTheme">${[["system", "Same as this computer"], ["light", "Light"], ["dark", "Dark"]].map(([v, t]) => `<option value="${v}" ${theme() === v ? "selected" : ""}>${t}</option>`).join("")}</select></label>
      <span class="hint">Saved in this browser.</span>
    </div>
    <button class="btn primary" id="sSave">Save settings</button> <span class="hint">Saves everything above except Pause (which applies at once) and Theme (saved in this browser).</span>`;
  $("#sTheme").onchange = e => setTheme(e.target.value);
  // Live preview, and field buttons that insert at the cursor.
  const preview = box => { const c = box.dataset.tpl; $("#pv-" + c).textContent = fillText(box.value, previewChannel(c), previewLead(c) || {}); };
  document.querySelectorAll("[data-tpl]").forEach(box => { preview(box); box.addEventListener("input", () => preview(box)); });
  document.querySelectorAll("[data-field]").forEach(b => b.onclick = () => {
    const box = $("#" + b.dataset.for), at = box.selectionStart ?? box.value.length, end = box.selectionEnd ?? at;
    box.value = box.value.slice(0, at) + b.dataset.field + box.value.slice(end);
    box.focus(); box.setSelectionRange(at + b.dataset.field.length, at + b.dataset.field.length); preview(box);
  });
  for (const id of ["sGoogleLimit", "sGoogleDaily"]) $("#" + id + "None").onchange = e => { $("#" + id).disabled = e.target.checked; };
  $("#sPaused").onchange = e => act(() => api("/api/settings", { paused: e.target.checked }),
    e.target.checked ? "Paused: nothing will be checked or looked up until you turn this off." : r => r.message || "Lead Desk is running again.", e.target);
  const gc = $("#sGoogleClear");
  if (gc) gc.onclick = async e => {
    const btn = e.currentTarget;
    if (await confirmBox({ title: "Remove the saved Google key?", body: "Phone lookups will then use only OpenStreetMap and company websites until you add a key again.", ok: "Remove key", danger: true }))
      act(() => api("/api/settings", { clear_google_key: true }), "Google key removed", btn);
  };
  const lim = id => $("#" + id + "None").checked ? null : ($("#" + id).value === "" ? 0 : Number($("#" + id).value));
  markFields($("#tab-settings"));
  recheckOnInput(settingsChecks());
  for (const id of ["sGoogleLimit", "sGoogleDaily"]) $("#" + id + "None").addEventListener("change", () => { if (!settingsChecks()[id]()) clearFieldError(id); });
  $("#sSave").onclick = e => {
    if (!checkFields(settingsChecks())) return;
    const costs = {}, tracking = {}, templates = {};
    document.querySelectorAll("[data-cost]").forEach(i => costs[i.dataset.cost] = Number(i.value || 0));
    document.querySelectorAll("[data-track]").forEach(i => tracking[i.dataset.track] = i.value.trim());
    document.querySelectorAll("[data-tpl]").forEach(i => templates[i.dataset.tpl] = i.value);
    act(() => api("/api/settings", { business_name: $("#sName").value.trim(), business_phone: $("#sPhone").value.trim(), base_address: $("#sBase").value.trim(), google_places_api_key: $("#sGoogle").value.trim(), google_enabled: $("#sGoogleOn").checked, google_monthly_limit: lim("sGoogleLimit"), google_daily_limit: lim("sGoogleDaily"), costs, tracking_numbers: tracking, templates }), "Settings saved", e.currentTarget,
      undefined, err => showFieldErrors(err, settingsFields()));
  };
}
