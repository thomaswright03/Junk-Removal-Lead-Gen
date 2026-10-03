// Outreach tab: split leads between methods, and each method's work list
// (the server sends the list for the method that's showing).
"use strict";
// Methods ticked for Assign leads: the server's suggestion (the most methods
// the unassigned leads can all be worked by) until Steve changes the ticks.
const comboKey = chans => Object.keys(S.channels).filter(c => chans.includes(c)).join("+");
const methodList = chans => chans.map(chName).join(" + ");
function splitLine(chans) {
  const sp = S.split || { combos: {}, followed: 0, suggested: [] };
  const n = chans.length ? sp.combos[comboKey(chans)] || 0 : 0;
  const followed = sp.followed ? ` ${sp.followed} more go to the method already working their landlord.` : "";
  if (!chans.length) return { n, html: "Tick at least one method." };
  const idle = Object.keys(S.channels).filter(c => !chans.includes(c) && !sp.combos[c]);
  const idleNote = idle.length ? ` ${esc(methodList(idle))}: no unassigned lead ${idle.length > 1 ? "they" : "it"} can work yet${idle.includes("door_hanger") ? " (door hangers need a property address)" : ""}.` : "";
  if (n) return { n, html: `<strong>${n} lead${n === 1 ? "" : "s"}</strong> can be split between ${esc(methodList(chans))}.${followed}${idleNote}` };
  // Nothing every ticked method can work: name the method that blocks it.
  const fixes = chans.map(c => chans.filter(x => x !== c)).filter(rest => rest.length && sp.combos[comboKey(rest)])
    .sort((a, b) => sp.combos[comboKey(b)] - sp.combos[comboKey(a)]);
  const fix = fixes[0];
  const drop = fix ? chans.find(c => !fix.includes(c)) : null;
  const why = chans.includes("door_hanger") ? " Door hangers need a property address (and a unit number at an apartment or condo complex)." : "";
  return { n, html: `<strong>No unassigned lead</strong> can be worked by all of ${esc(methodList(chans))}.${why}${followed}`
    + (drop ? ` <button class="btn small" id="aDrop" data-drop="${drop}">Untick ${esc(chName(drop))} (${sp.combos[comboKey(fix)]} leads)</button>` : "") };
}
function otherChoices(chans) {
  const sp = S.split || { combos: {} };
  return Object.entries(sp.combos).filter(([k, n]) => n && k !== comboKey(chans) && k.includes("+"))
    .sort((a, b) => b[1] - a[1]).slice(0, 3)
    .map(([k, n]) => `<button class="linkbtn" data-combo="${k}">${esc(methodList(k.split("+")))}: ${n}</button>`).join(" · ");
}
function renderOutreach() {
  const c = S.counts || {}, per = c.channels || {};
  const unassignedCount = c.unassigned || 0;
  const t = ui.outreachTab;
  const sp = S.split || { suggested: [] };
  if (!ui.aChannels) ui.aChannels = sp.suggested.length ? sp.suggested.slice() : Object.keys(S.channels);
  const chans = ui.aChannels;
  const line = splitLine(chans), others = otherChoices(chans);
  $("#tab-outreach").innerHTML = `
    <div class="card">
      <h2>Split leads between outreach methods</h2>
      <p class="hint">Assign leads deals the best of your ${unassignedCount} unassigned leads evenly across the ticked methods, so Results can say which one wins jobs.</p>
      <details class="hint"><summary>How the split works</summary><ul>
        <li>Only leads every ticked method can work are used: door hangers need a property address (and a unit number at an apartment complex).</li>
        <li>All of one landlord's leads go to the same method, now and later, so no company hears from you twice.</li>
        <li>Each method gets the same mix of strong and weak leads, evictions and code cases.</li>
        <li>Each method reaches someone different or makes a different offer: the lead shows which.</li></ul></details>
      <div class="row">
        <label>Leads this round <input type="number" id="aCount" value="${Math.min(40, line.n) || 40}" min="1" style="width:80px"></label>
        ${Object.entries(S.channels).map(([c, n]) => `<label class="ch"><input type="checkbox" class="aCh" value="${c}" ${chans.includes(c) ? "checked" : ""}><span class="dot" style="background:var(--c-${c})"></span>${esc(n)}</label>`).join("")}
        <button class="btn primary" id="aGo" ${line.n ? "" : "disabled"}>Assign leads</button>
      </div>
      <p class="hint mt8" id="aSplit" aria-live="polite">${line.html}</p>
      ${others ? `<p class="hint">Other choices: ${others}</p>` : ""}
    </div>
    <div class="tabs2">${Object.entries(S.channels).map(([c, n]) => `<button data-otab="${c}" class="${c === t ? "on" : ""}"><span class="dot" style="background:var(--c-${c})"></span> ${esc(n)} · ${(per[c] || {}).to_do || 0} to do / ${(per[c] || {}).active || 0}</button>`).join("")}</div>
    <div id="oBody"></div>`;
  const setChans = (list, focusSel) => { ui.aChannels = list; renderOutreach(); const f = $(focusSel); if (f) f.focus(); };
  document.querySelectorAll(".aCh").forEach(b => b.onchange = () =>
    setChans([...document.querySelectorAll(".aCh:checked")].map(x => x.value), `.aCh[value="${b.value}"]`));
  const drop = $("#aDrop");
  if (drop) drop.onclick = () => setChans(chans.filter(c => c !== drop.dataset.drop), "#aGo");
  document.querySelectorAll("[data-combo]").forEach(b => b.onclick = () => setChans(b.dataset.combo.split("+"), "#aGo"));
  $("#aGo").onclick = async e => {
    const btn = e.currentTarget;
    if (!chans.length) return toast("Tick at least one outreach method.");
    const n = +$("#aCount").value;
    const most = Math.min(n, line.n);
    const single = chans.length === 1;
    if (!(await confirmBox({ title: `Assign ${most} lead${most === 1 ? "" : "s"}${single ? ` to ${chName(chans[0])} only` : ""}?`,
      body: (single ? `Only one method is ticked, so this round won't compare methods: all ${most} go to ${chName(chans[0])}. Tick another method to compare.`
        : `They are split between ${methodList(chans)}, and each one then shows up in that method's work list.`) + ((S.split || {}).followed ? ` Up to ${Math.min(n, S.split.followed)} more go to the method already working their landlord.` : ""),
      ok: `Assign ${most} lead${most === 1 ? "" : "s"}` }))) return;
    ui.aChannels = null;  // the next suggestion fits the leads that are left
    act(() => api("/api/assign", { count: n, channels: chans, single_method: single }), r => {
      const got = Object.entries(r.assigned).map(([c, k]) => `${chName(c)} ${k}`).join(", ");
      const followed = Object.values(r.followed || {}).reduce((a, b) => a + b, 0);
      const lo = r.left_out || {};
      return `Assigned: ${got}.` + (followed ? ` ${followed} more went to the method already working their landlord.` : "")
        + (lo.needs_address ? ` ${lo.needs_address} leads left out because they have no property address (add one on the lead, or untick Door hanger).` : "")
        + (lo.needs_unit ? ` ${lo.needs_unit} left out because they're at an apartment or condo complex with no unit number (type the unit or confirm the address on the lead, or untick Door hanger).` : "")
        + (lo.no_contact ? ` ${lo.no_contact} left out because a ticked method has no one to contact.` : "");
    }, btn);
  };
  document.querySelectorAll("[data-otab]").forEach(b => b.onclick = async () => {
    ui.outreachTab = b.dataset.otab; syncUrl(); await reloadList(); $(`[data-otab="${b.dataset.otab}"]`).focus();
  });
  const body = $("#oBody");
  const q = listLeads();
  if (t === "door_hanger") renderRoute(body, q);
  else if (t === "phone") renderCalls(body, q, "phone");
  else renderManagers(body, q);
  if (S.list && S.list.total > q.length) body.insertAdjacentHTML("beforeend", `<p class="hint">Showing the first ${q.length} of ${S.list.total}.</p>`);
  bindRows(body);
}
function routeOrder(q) {
  // Nearest-neighbour walk starting from Steve's base.
  const left = q.filter(l => l.lat != null), out = [];
  let cur = { lat: S.settings.base_lat, lon: S.settings.base_lon };
  if (cur.lat == null && left.length) cur = left[0];
  const d = (a, b) => Math.hypot(a.lat - b.lat, (a.lon - b.lon) * Math.cos(a.lat * Math.PI / 180));
  while (left.length) {
    let bi = 0; left.forEach((l, i) => { if (d(cur, l) < d(cur, left[bi])) bi = i; });
    cur = left.splice(bi, 1)[0]; out.push(cur);
  }
  return out.concat(q.filter(l => l.lat == null));
}
function renderRoute(el, q) {
  const r = routeOrder(q);
  const gmaps = r.filter(l => l.lat != null).slice(0, 9);
  const dirUrl = gmaps.length ? "https://www.google.com/maps/dir/" + encodeURIComponent(S.settings.base_address) + "/" + gmaps.map(l => `${l.lat},${l.lon}`).join("/") : null;
  el.innerHTML = `<div class="card">
    <h2>Door hangers: ${r.length} stops</h2>
    <p class="hint">Ordered as a drive starting from ${esc(S.settings.base_address)}. Google Maps directions take up to 9 stops at a time. Log each stop as you go, from this list or the lead.</p>
    <div class="row">
      ${dirUrl ? `<a class="btn" href="${dirUrl}" target="_blank" rel="noopener">Directions for first ${gmaps.length}</a>` : ""}
      <button class="btn" id="rPrint" ${r.length ? "" : "disabled"}>Print route sheet</button>
    </div></div>
    ${r.length ? `<div class="tablewrap"><table class="cards"><thead><tr><th>#</th><th>Address</th><th>What</th><th class="num">Miles</th><th></th></tr></thead><tbody>
      ${r.map((l, i) => `<tr class="click" data-id="${l.id}"><td data-th="Stop">${i + 1}</td><td data-th="Address"><span>${esc(title(fullAddress(l)))}${addressNote(l)}</span></td><td data-th="What">${esc(whatLabel(l))}</td><td class="num" data-th="Miles">${l.miles != null ? l.miles.toFixed(1) : "–"}</td>
      <td data-th="Log"><button class="btn small" data-quick="${l.id}" data-kind="visited">Hanger left</button></td></tr>`).join("")}</tbody></table></div>` : emptyQueue()}`;
  if (r.length) $("#rPrint").onclick = () => {
    $("#print").innerHTML = `<div class="route"><h2>Door hanger route · ${esc(fmtDate(S.today))}</h2><p>${esc(fill("door_hanger", {}))}</p><table><tr><th>#</th><th>Address</th><th>What</th><th>Done</th></tr>
      ${r.map((l, i) => `<tr><td>${i + 1}</td><td>${esc(title(fullAddress(l)))}${addressNote(l, true) ? ` (${esc(addressNote(l, true))})` : ""}</td><td>${esc(whatLabel(l))}</td><td>☐</td></tr>`).join("")}</table></div>`;
    window.print();
  };
  bindQuick(el);
}
function bindQuick(el) {
  el.querySelectorAll("[data-quick]").forEach(b => b.onclick = () =>
    act(() => api("/api/touch", { lead_id: +b.dataset.quick, kind: b.dataset.kind }), r => r.logged ? "Logged: " + b.textContent : "Already logged a moment ago", b));
}
// When work-list leads have no phone number: why, and what to do about it.
function noPhoneNote(q, what) {
  const missing = q.filter(l => !l.owner_phone).length;
  if (!missing) return "";
  const why = googleReady()
    ? "Press Find landlord phones to look them up again, or use each lead's Search link to find a number by hand."
    : "Court cases don't include phone numbers, and without a Google Places key Lead Desk checks only OpenStreetMap, which knows few Tucson landlords. Adding a key finds most office numbers; at the default limits it costs nothing.";
  return `<div class="notice mb12" id="noPhoneNote" role="note"><strong>No phone number yet for ${missing} of ${q.length} ${what}.</strong>
    ${q.length > missing ? "The ones with a number are at the top. " : ""}${why}
    <div class="row mt8">${googleReady() ? "" : `<button class="btn primary" data-setup>Set up phone lookups</button>`}
    <button class="btn" data-findphones ${S.paused ? "disabled" : ""}>Find landlord phones now</button></div></div>`;
}
function bindNoPhoneNote(el) {
  el.querySelectorAll("[data-setup]").forEach(b => b.onclick = async () => {
    Object.assign(ui, { tab: "leads", showSetup: true, setupOpen: true, type: "", offset: 0 }); syncUrl(true); await reloadList();
    const g = $("#setupTitle"); if (g) { g.scrollIntoView(); g.focus(); }
  });
  el.querySelectorAll("[data-findphones]").forEach(b => b.onclick = () => findContacts(b));
}
function renderCalls(el, q, ch) {
  el.innerHTML = `<div class="card">
    <h2>Calls: ${q.length} to make</h2>
    <p class="hint">Numbers found by the lookup or imported show here; otherwise use the search link. Check each number against the Do Not Call registry before cold-calling a cell phone, and log every attempt. Never auto-dial or mass-text.</p>
    ${noPhoneNote(q, "calls")}
    ${q.some(l => l.lead_type === "eviction") || !q.length ? `<h3>Script for eviction landlords</h3><div class="script" data-script="eviction">${esc(fill(ch, { lead_type: "eviction", owner_entity: 1 }))}</div>` : ""}
    ${q.some(l => l.lead_type !== "eviction") ? `<h3>Script for code-case owners</h3><div class="script" data-script="code">${esc(fill(ch, { lead_type: "code_violation", address: "[address]" }))}</div>` : ""}
    <p class="hint">Each lead's own page shows the script filled in with its owner and address.</p></div>
    ${q.length ? `<div class="tablewrap"><table class="cards"><thead><tr><th class="num">Priority</th><th>Owner</th><th>Property</th><th>Phone</th><th>Log</th></tr></thead><tbody>
      ${q.map(l => { const who = l.owner_name || l.plaintiff || ""; return `<tr class="click" data-id="${l.id}"><td class="num" data-th="Priority">${scoreChip(l)}</td><td data-th="Owner"><span>${ownerLine(l)}</span></td><td data-th="Property"><span>${l.address ? esc(title(fullAddress(l))) + addressNote(l) : '<span class="muted">address needed</span>'}</span></td>
      <td style="white-space:nowrap" data-th="Phone">${l.owner_phone ? phoneCell(l) : `<a href="https://www.google.com/search?q=${encodeURIComponent(who + " Tucson AZ phone")}" target="_blank" rel="noopener">Search</a>`}</td>
      <td data-th="Log"><div class="row">${touchButtons(ch).slice(0, 3).map(([k, t]) => `<button class="btn small" data-quick="${l.id}" data-kind="${k}">${t}</button>`).join("")}</div></td></tr>`; }).join("")}
    </tbody></table></div>` : emptyQueue()}`;
  bindQuick(el);
  bindNoPhoneNote(el);
}
function renderManagers(el, leads) {
  const groups = {};
  for (const l of leads) { const k = (l.plaintiff || l.owner_name || "Unknown").toUpperCase(); (groups[k] = groups[k] || []).push(l); }
  // Companies with a phone or email first, then those on the most leads.
  const reachable = ls => ls.some(l => l.owner_phone || l.owner_email);
  const list = Object.entries(groups).sort((a, b) => reachable(b[1]) - reachable(a[1]) || b[1].length - a[1].length);
  el.innerHTML = `<div class="card">
    <h2>Landlords and property managers: ${list.length}</h2>
    <p class="hint">One pitch per company, not per property: offer a standing move-out clean-out rate. Companies with a phone or email come first, then those on several leads. Use “Other properties this owner has” on a lead to see their portfolio.</p>
    ${noPhoneNote(leads.filter(l => l.status !== "won"), "leads here")}
    <div class="script">${esc(fill("property_manager", {}))}</div></div>
    ${list.length ? `<div class="tablewrap"><table class="cards"><thead><tr><th>Company / owner</th><th>Phone / email</th><th class="num">Leads</th><th>Properties</th><th>Contacted</th><th>Log</th></tr></thead><tbody>
      ${list.map(([name, ls]) => { const ids = ls.map(l => l.id).join(","); const done = ls.some(l => l.touches.length);
        return `<tr class="click" data-id="${ls[0].id}"><td data-th="Company"><strong>${esc(title(name))}</strong>${ls[0].owner_address ? `<div class="muted" style="font-size:13px">${esc(title(ls[0].owner_address))}, ${esc(title(ls[0].owner_city))} ${esc(ls[0].owner_state || "")}</div>` : ""}</td>
        <td style="white-space:nowrap" data-th="Contact">${(() => { const c = ls.find(x => x.owner_phone || x.owner_email); return c ? `${c.owner_phone ? phoneCell(c) : ""}${c.owner_phone && c.owner_email ? "<br>" : ""}${c.owner_email ? emailCell(c) : ""}` : '<span class="muted">–</span>'; })()}</td>
        <td class="num" data-th="Leads">${ls.length}</td><td data-th="Properties"><span>${ls.slice(0, 3).map(l => l.address ? esc(title(l.address)) : esc(l.source_id)).join("<br>")}${ls.length > 3 ? `<br><span class="muted">+${ls.length - 3} more</span>` : ""}</span></td>
        <td data-th="Contacted">${done ? '<span class="chip good">yes</span>' : '<span class="chip">no</span>'}</td>
        <td data-th="Log"><div class="row"><a class="btn small" href="https://www.google.com/search?q=${encodeURIComponent(name + " Tucson")}" target="_blank" rel="noopener">Search</a>
        ${touchButtons("property_manager").map(([k, t]) => `<button class="btn small" data-group="${ids}" data-kind="${k}">${t}</button>`).join("")}</div></td></tr>`; }).join("")}
    </tbody></table></div>` : emptyQueue()}`;
  bindNoPhoneNote(el);
  el.querySelectorAll("[data-group]").forEach(b => b.onclick = () => {
    const ids = b.dataset.group.split(",").map(Number);
    // One contact covers the company; cost is logged once, on the first lead.
    act(async () => { const r = await api("/api/touch", { lead_id: ids[0], kind: b.dataset.kind });
      if (ids.length > 1 && r.logged) await api("/api/touch", { lead_ids: ids.slice(1), kind: b.dataset.kind, cost: 0, notes: "same company contact" });
      return r; }, r => r.logged ? "Logged: " + b.textContent : "Already logged a moment ago", b);
  });
}
const emptyQueue = () => `<div class="card empty">Nothing in this queue. Assign leads above, or set a channel on a lead.</div>`;

