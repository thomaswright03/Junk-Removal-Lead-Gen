// Results tab: which outreach method turns leads into paid jobs.
"use strict";
function renderResults() {
  const R = S.results;
  const tot = k => R.reduce((s, r) => s + (r[k] || 0), 0);
  const touched = tot("touched");
  const maxRate = Math.max(0.0001, ...R.map(r => r.response_rate || 0));
  const maxRpd = Math.max(0.0001, ...R.map(r => r.revenue_per_dollar || 0));
  const C = S.comparison || { fair: false, ready: false, reasons: [] };
  const ranked = R.filter(r => r.touched).sort((a, b) => (b.won - a.won) || ((b.response_rate || 0) - (a.response_rate || 0)));
  const leader = C.fair ? ranked[0] : null;
  const share = v => v == null ? "–" : Math.round(v * 100) + "%";
  $("#tab-results").innerHTML = `
    <div class="grid4" style="margin-bottom:16px">
      <div class="kpi"><div class="v">${touched}</div><div class="l">leads contacted</div></div>
      <div class="kpi"><div class="v">${tot("responded")}</div><div class="l">responses</div></div>
      <div class="kpi"><div class="v">${tot("won")}</div><div class="l">jobs won</div></div>
      <div class="kpi"><div class="v">${money(tot("revenue"))}</div><div class="l">revenue · ${money(tot("cost"))} spent</div></div>
    </div>
    <div class="card">
      <h2>${!touched ? "No results yet" : !C.fair ? "No fair comparison yet" : leader && (leader.won || leader.responded) ? `${C.ready ? "Working best" : "Leading so far"}: ${esc(leader.label)}` : "No responses yet"}</h2>
      <p class="hint">${!touched ? "Split leads on the Outreach tab and log each contact. Results fill in as you mark leads responded, quoted or won."
        : !C.fair ? "The methods didn't get the same kind of leads, so a ranking would show which leads each one got, not how well it works. Use Assign leads on the Outreach tab to hand out new leads evenly."
        : C.ready ? "Every method got the same kind of leads and has at least 20 contacts. Compare cost per job and revenue per dollar before shifting effort."
        : "Every method got the same kind of leads. Keep going before deciding."}</p>
      ${C.reasons.length && touched ? `<ul class="hint">${C.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
      <div class="tablewrap"><table>
        <thead><tr><th>Method</th><th class="num">Assigned</th><th class="num">Contacted</th><th class="num">Responded</th><th class="num">Quoted</th><th class="num">Won</th><th class="num">Response rate</th><th class="num">Win rate</th><th class="num">Spent</th><th class="num">Revenue</th><th class="num">Cost per job</th><th class="num">Revenue per $1</th></tr></thead>
        <tbody>${R.map(r => `<tr><td>${chDot(r.channel)}</td><td class="num">${r.assigned}</td><td class="num">${r.touched}</td><td class="num">${r.responded}</td><td class="num">${r.quoted}</td><td class="num">${r.won}</td>
          <td class="num">${pct(r.response_rate)}</td><td class="num">${pct(r.win_rate)}</td><td class="num">${money(r.cost)}</td><td class="num">${money(r.revenue)}</td>
          <td class="num">${r.cost_per_win == null ? "–" : money(r.cost_per_win)}</td><td class="num">${r.revenue_per_dollar == null ? (r.revenue ? "free" : "–") : money(r.revenue_per_dollar)}</td></tr>`).join("")}</tbody>
      </table></div>
    </div>
    <div class="card"><h2>What each method got</h2><p class="hint">For a fair comparison these should be close: the same share of leads with an address, of evictions, and a similar average priority.</p>
      <div class="tablewrap"><table><thead><tr><th>Method</th><th class="num">Leads</th><th class="num">With an address</th><th class="num">Evictions</th><th class="num">Average priority</th><th class="num">Set by hand</th></tr></thead>
      <tbody>${R.map(r => `<tr><td>${chDot(r.channel)}</td><td class="num">${r.mix.leads}</td><td class="num">${share(r.mix.with_address)}</td><td class="num">${share(r.mix.evictions)}</td><td class="num">${r.mix.avg_score ?? "–"}</td><td class="num">${r.mix.set_by_hand}</td></tr>`).join("")}</tbody></table></div></div>
    <div class="card"><h2>Response rate</h2><p class="hint">Share of contacted leads that called back or said yes.</p>
      <div class="bars">${R.map(r => bar(r, r.response_rate || 0, maxRate, pct(r.response_rate))).join("")}</div></div>
    <div class="card"><h2>Revenue per dollar spent</h2><p class="hint">Phone and property-manager outreach cost Steve's time, not cash; add a cost per contact in Settings to compare them fairly.</p>
      <div class="bars">${R.map(r => bar(r, r.revenue_per_dollar || 0, maxRpd, r.revenue_per_dollar == null ? (r.revenue ? "free" : "–") : money(r.revenue_per_dollar))).join("")}</div></div>`;
}
const bar = (r, v, max, label) => `<div class="bar"><div>${chDot(r.channel)}</div><div class="track"><div class="fill" style="width:${Math.round(100 * v / max)}%;background:var(--c-${r.channel})"></div></div><div class="num" style="text-align:right">${label}</div></div>`;

