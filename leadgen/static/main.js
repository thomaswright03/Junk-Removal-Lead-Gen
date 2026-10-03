// Wiring: drawing the tab that's showing, the address bar, the header
// buttons, polling while a check or job runs, and starting up.
"use strict";
function render() {
  renderHeader();
  if (ui.tab === "leads") renderLeads();
  if (ui.tab === "outreach") renderOutreach();
  if (ui.tab === "results") renderResults();
  if (ui.tab === "settings") renderSettings();
  if (ui.open != null) renderDrawer(); else $("#drawer").classList.remove("open");
}
// The tab, filters and open lead live in the address (#tab=leads&type=has_phone&lead=12),
// so a reload or Back shows the same view and a link can be shared.
const URL_KEYS = { tab: "tab", q: "q", type: "type", status: "status", channel: "channel", sort: "sort", offset: "from", outreachTab: "method", open: "lead" };
const UI_DEFAULTS = { ...ui };
function syncUrl(push) {
  const p = new URLSearchParams();
  for (const [k, name] of Object.entries(URL_KEYS)) if (ui[k] != null && ui[k] !== UI_DEFAULTS[k]) p.set(name, ui[k]);
  const hash = p.toString() ? "#" + p.toString() : location.pathname + location.search;
  if (("#" + p.toString()) === location.hash || (!p.toString() && !location.hash)) return;
  history[push ? "pushState" : "replaceState"](null, "", hash);
}
function readUrl() {
  const p = new URLSearchParams(location.hash.slice(1));
  for (const [k, name] of Object.entries(URL_KEYS)) ui[k] = p.has(name) ? p.get(name) : UI_DEFAULTS[k];
  ui.open = ui.open != null && ui.open !== "" ? +ui.open : null;
  ui.offset = Math.max(0, parseInt(ui.offset, 10) || 0);
}
window.addEventListener("popstate", () => {
  readUrl();
  if (ui.open == null) $("#drawer").classList.remove("open");  // Back closes the lead at once
  if (S) load().catch(e => toast(e.message, 8000));
});
document.querySelectorAll("#nav button").forEach(b => b.onclick = async () => {
  ui.tab = b.dataset.tab; syncUrl(true);
  await reloadList();  // the new tab's list comes with it
});
$("#refreshBtn").onclick = e => act(() => api("/api/refresh", {}), r => r.message || "Checking the court calendar. The leads update when the check finishes.", e.currentTarget);
// While the daily check or a job runs, ask for its progress every few
// seconds (a small request: no leads), and reload the leads when it ends.
// Drawer typing is kept (see drafts), and the page isn't redrawn under
// someone typing in a box.
const typing = () => document.activeElement && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
function watch() {
  if (watch.t) return;
  const before = new Set(runningJobs().map(j => j.name));
  watch.t = setInterval(async () => {
    try {
      const wasDaily = S.daily.running;
      Object.assign(S, await api("/api/status"));
      const finished = [];
      for (const name of [...before]) {
        const j = (S.jobs || {})[name];
        if (j && !j.running) { before.delete(name); finished.push(j.error || JOB_DONE[name](j.result || {})); }
      }
      runningJobs().forEach(j => before.add(j.name));
      const dailyDone = wasDaily && !S.daily.running;
      const idle = !S.daily.running && !runningJobs().length;
      if (idle) { clearInterval(watch.t); watch.t = null; }
      if (finished.length || dailyDone) {
        const fresh = await api(stateQuery());
        if (typing()) { S = fresh; renderHeader(); } else { S = fresh; render(); }
        if (dailyDone) finished.push("Done: " + (S.daily.summary || S.daily.message || ""));
        toast(finished.join(" "), 9000);
      } else renderHeader();
    } catch (e) { /* keep trying */ }
  }, 3000);
}
$("#importFile").onchange = async e => {
  const f = e.target.files[0]; if (!f) return;
  await importLeadsFile(f);
  e.target.value = "";
  if (ui.tab !== "leads") { ui.tab = "leads"; syncUrl(true); await reloadList(); }
};
// A saved court page, or a CSV: leads, or a records-request file that fills
// in addresses of cases already here.
async function importLeadsFile(f) {
  const source = /\.csv$/i.test(f.name) ? "csv_import" : "pima_jp_calendar";
  await act(async () => send(`/api/import?source=${source}&filename=${encodeURIComponent(f.name)}`, { method: "POST", body: await f.arrayBuffer() }),
    r => (r.addresses_filled || r.addresses_kept ? `${r.addresses_filled || 0} propert${r.addresses_filled === 1 ? "y address" : "y addresses"} filled in for court cases already in Lead Desk (matched by case number).` +
        (r.addresses_kept ? ` ${r.addresses_kept} more matched but kept the address already on the lead (typed, confirmed or from the court).` : "") + " " : "")
      + (r.imported || !(r.addresses_filled || r.addresses_kept) ? `Imported ${r.imported} lead${r.imported === 1 ? "" : "s"}: ${r.new} new, ${r.updated} already listed.` : "")
      + (r.with_notice != null ? (r.with_notice ? " An eviction notice is filed." : " No eviction notice in this case yet.") : "")
      + (r.waiting_for_case_check ? ` ${r.waiting_for_case_check} are marked “case not checked” until their court page is read (next check ${S.daily.next_run || "tomorrow 6:00 AM"}).` : "")
      + (r.unreadable_dates ? ` ${r.unreadable_dates} date${r.unreadable_dates === 1 ? "" : "s"} couldn't be read and were left empty.` : "")
      + (r.odd_dates ? ` ${r.odd_dates} row${r.odd_dates === 1 ? " had a date that looks" : "s had dates that look"} wrong (more than a year ago or ahead); check the file, as such leads show as Old.` : "")
      + (r.imported ? " They're on the Leads tab." : ""));
}
bindLabels(document.querySelector("header"));
// Escape closes the lead (unless the confirm dialog is up: Escape cancels that).
// Run after this key press is done, so a "leave without saving?" question it
// raises isn't closed again by the same Escape.
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && ui.open != null && !$("#confirmBox").open) { e.preventDefault(); setTimeout(closeDrawer); }
});
window.addEventListener("beforeunload", e => { if (Object.keys(drafts).length) { e.preventDefault(); e.returnValue = ""; } });
function start() {
  readUrl();
  $("#sub").textContent = "Loading…";
  load().catch(e => {
    $("#sub").innerHTML = `Couldn't load the leads. ${esc(e.message)} <button class="btn small" id="retry">Retry</button>`;
    $("#retry").onclick = start;
  });
}
start();
