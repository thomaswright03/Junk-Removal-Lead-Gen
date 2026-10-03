// Outreach tab: split leads between methods, and each method's work list
// (the server sends the list for the method that's showing).
"use strict";
function renderOutreach() {
  const c = S.counts || {}, per = c.channels || {};
  const unassignedCount = c.unassigned || 0;
  const t = ui.outreachTab;
  $("#tab-outreach").innerHTML = `
    <div class="card">
      <h2>Split leads between outreach methods</h2>
      <p class="hint">Hands out the best unassigned leads so each method gets the same kind of leads, and the Results tab can say fairly which one works. Only leads every ticked method can work are used (door hangers need a property address), every lead of one landlord goes to the same method, and each method gets the same mix of strong and weak leads. ${unassignedCount} leads are unassigned.</p>
      <div class="row">
        <label>Leads this round <input type="number" id="aCount" value="${Math.min(40, unassignedCount) || 40}" min="1" style="width:80px"></label>
        ${Object.entries(S.channels).map(([c, n]) => `<label class="ch"><input type="checkbox" class="aCh" value="${c}" checked><span class="dot" style="background:var(--c-${c})"></span>${esc(n)}</label>`).join("")}
        <button class="btn primary" id="aGo" ${unassignedCount ? "" : "disabled"}>Assign leads</button>
      </div>
    </div>
    <div class="tabs2">${Object.entries(S.channels).map(([c, n]) => `<button data-otab="${c}" class="${c === t ? "on" : ""}"><span class="dot" style="background:var(--c-${c})"></span> ${esc(n)} · ${(per[c] || {}).to_do || 0} to do / ${(per[c] || {}).active || 0}</button>`).join("")}</div>
    <div id="oBody"></div>`;
  $("#aGo").onclick = e => {
    const chans = [...document.querySelectorAll(".aCh:checked")].map(x => x.value);
    if (!chans.length) return toast("Tick at least one outreach method.");
    const n = +$("#aCount").value;
    if (!confirm(`Hand out up to ${n} leads between ${chans.map(chName).join(", ")}? Each lead then shows up in that method's work list.`)) return;
    act(() => api("/api/assign", { count: n, channels: chans }), r => {
      const got = Object.entries(r.assigned).map(([c, k]) => `${chName(c)} ${k}`).join(", ");
      const followed = Object.values(r.followed || {}).reduce((a, b) => a + b, 0);
      const lo = r.left_out || {};
      return `Assigned: ${got}.` + (followed ? ` ${followed} more went to the method already working their landlord.` : "")
        + (lo.needs_address ? ` ${lo.needs_address} leads left out because they have no property address (add one on the lead, or untick Door hanger).` : "")
        + (lo.needs_unit ? ` ${lo.needs_unit} left out because they're at an apartment or condo complex with no unit number (type the unit or confirm the address on the lead, or untick Door hanger).` : "")
        + (lo.no_contact ? ` ${lo.no_contact} left out because a ticked method has no one to contact.` : "");
    }, e.currentTarget);
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
function renderCalls(el, q, ch) {
  el.innerHTML = `<div class="card">
    <h2>Calls: ${q.length} to make</h2>
    <p class="hint">Numbers found by the lookup or imported show here; otherwise use the search link. Check each number against the Do Not Call registry before cold-calling a cell phone, and log every attempt. Never auto-dial or mass-text.</p>
    ${q.some(l => l.lead_type === "eviction") || !q.length ? `<h3>Script for eviction landlords</h3><div class="script" data-script="eviction">${esc(fill(ch, { lead_type: "eviction", owner_entity: 1 }))}</div>` : ""}
    ${q.some(l => l.lead_type !== "eviction") ? `<h3>Script for code-case owners</h3><div class="script" data-script="code">${esc(fill(ch, { lead_type: "code_violation", address: "[address]" }))}</div>` : ""}
    <p class="hint">Each lead's own page shows the script filled in with its owner and address.</p></div>
    ${q.length ? `<div class="tablewrap"><table class="cards"><thead><tr><th class="num">Priority</th><th>Owner</th><th>Property</th><th>Phone</th><th>Log</th></tr></thead><tbody>
      ${q.map(l => { const who = l.owner_name || l.plaintiff || ""; return `<tr class="click" data-id="${l.id}"><td class="num" data-th="Priority">${scoreChip(l.score)}</td><td data-th="Owner"><span>${ownerLine(l)}</span></td><td data-th="Property"><span>${l.address ? esc(title(fullAddress(l))) + addressNote(l) : '<span class="muted">address needed</span>'}</span></td>
      <td style="white-space:nowrap" data-th="Phone">${l.owner_phone ? phoneCell(l) : `<a href="https://www.google.com/search?q=${encodeURIComponent(who + " Tucson AZ phone")}" target="_blank" rel="noopener">Search</a>`}</td>
      <td data-th="Log"><div class="row">${touchButtons(ch).slice(0, 3).map(([k, t]) => `<button class="btn small" data-quick="${l.id}" data-kind="${k}">${t}</button>`).join("")}</div></td></tr>`; }).join("")}
    </tbody></table></div>` : emptyQueue()}`;
  bindQuick(el);
}
function renderManagers(el, leads) {
  const groups = {};
  for (const l of leads) { const k = (l.plaintiff || l.owner_name || "Unknown").toUpperCase(); (groups[k] = groups[k] || []).push(l); }
  const list = Object.entries(groups).sort((a, b) => b[1].length - a[1].length);
  el.innerHTML = `<div class="card">
    <h2>Landlords and property managers: ${list.length}</h2>
    <p class="hint">One pitch per company, not per property: offer a standing move-out clean-out rate. Companies that show up on several leads come first. Use “Other properties this owner has” on a lead to see their portfolio.</p>
    <div class="script">${esc(fill("property_manager", {}))}</div></div>
    ${list.length ? `<div class="tablewrap"><table class="cards"><thead><tr><th>Company / owner</th><th>Phone / email</th><th class="num">Leads</th><th>Properties</th><th>Contacted</th><th>Log</th></tr></thead><tbody>
      ${list.map(([name, ls]) => { const ids = ls.map(l => l.id).join(","); const done = ls.some(l => l.touches.length);
        return `<tr class="click" data-id="${ls[0].id}"><td data-th="Company"><strong>${esc(title(name))}</strong>${ls[0].owner_address ? `<div class="muted" style="font-size:12px">${esc(title(ls[0].owner_address))}, ${esc(title(ls[0].owner_city))} ${esc(ls[0].owner_state || "")}</div>` : ""}</td>
        <td style="white-space:nowrap" data-th="Contact">${(() => { const c = ls.find(x => x.owner_phone || x.owner_email); return c ? `${c.owner_phone ? phoneCell(c) : ""}${c.owner_phone && c.owner_email ? "<br>" : ""}${c.owner_email ? emailCell(c) : ""}` : '<span class="muted">–</span>'; })()}</td>
        <td class="num" data-th="Leads">${ls.length}</td><td data-th="Properties"><span>${ls.slice(0, 3).map(l => l.address ? esc(title(l.address)) : esc(l.source_id)).join("<br>")}${ls.length > 3 ? `<br><span class="muted">+${ls.length - 3} more</span>` : ""}</span></td>
        <td data-th="Contacted">${done ? '<span class="chip good">yes</span>' : '<span class="chip">no</span>'}</td>
        <td data-th="Log"><div class="row"><a class="btn small" href="https://www.google.com/search?q=${encodeURIComponent(name + " Tucson")}" target="_blank" rel="noopener">Search</a>
        ${touchButtons("property_manager").map(([k, t]) => `<button class="btn small" data-group="${ids}" data-kind="${k}">${t}</button>`).join("")}</div></td></tr>`; }).join("")}
    </tbody></table></div>` : emptyQueue()}`;
  el.querySelectorAll("[data-group]").forEach(b => b.onclick = () => {
    const ids = b.dataset.group.split(",").map(Number);
    // One contact covers the company; cost is logged once, on the first lead.
    act(async () => { const r = await api("/api/touch", { lead_id: ids[0], kind: b.dataset.kind });
      if (ids.length > 1 && r.logged) await api("/api/touch", { lead_ids: ids.slice(1), kind: b.dataset.kind, cost: 0, notes: "same company contact" });
      return r; }, r => r.logged ? "Logged: " + b.textContent : "Already logged a moment ago", b);
  });
}
const emptyQueue = () => `<div class="card empty">Nothing in this queue. Assign leads above, or set a channel on a lead.</div>`;

