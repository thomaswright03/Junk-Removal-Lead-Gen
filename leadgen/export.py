"""Write leads out as CSV (for spreadsheets / CRM import) or a single HTML
page Steve can open in a browser and filter."""

import csv
import html
import json
from urllib.parse import quote_plus

from .util import az_today

COLUMNS = (
    "id", "lead_type", "event_date", "status", "address", "city", "zip",
    "plaintiff", "defendant", "description", "source", "source_id", "lat", "lon",
    "in_pima", "parcel", "owner_name", "owner_address", "owner_city", "owner_state",
    "owner_zip", "owner_phone", "owner_email", "owner_absentee", "owner_entity", "property_use", "channel", "notes",
    "first_seen", "url",
)


def _maps_link(row):
    if row["lat"] is not None and row["lon"] is not None:
        return f"https://www.google.com/maps/search/?api=1&query={row['lat']},{row['lon']}"
    if row["address"]:
        return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(
            f"{row['address']}, {row['city'] or 'Tucson'}, AZ"
        )
    return ""


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS + ("map",))
        for r in rows:
            w.writerow([r[c] for c in COLUMNS] + [_maps_link(r)])
    return len(rows)


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pima County Leads</title>
<style>
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e3e6;--accent:#2f6fde;--chip:#f2f2f4}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ededef;--muted:#9a9aa2;--line:#2b2b30;--accent:#7aa7ff;--chip:#222226}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
header{padding:16px;border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0 0 4px}.sub{color:var(--muted)}
.controls{display:flex;flex-wrap:wrap;gap:8px;padding:12px 16px}
input,select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
input{flex:1;min-width:180px}
.wrap{overflow-x:auto;padding:0 16px 24px}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
th{position:sticky;top:0;background:var(--bg);font-weight:600}
.chip{display:inline-block;padding:1px 8px;border-radius:10px;background:var(--chip);font-size:12px}
a{color:var(--accent)}.muted{color:var(--muted)}
</style></head><body>
<header><h1>Pima County clean-out leads</h1>
<div class="sub">Generated __DATE__ &middot; <span id="count"></span></div></header>
<div class="controls">
<input id="q" placeholder="Search address, landlord, case number...">
<select id="type"><option value="">All types</option>__TYPES__</select>
<select id="status"><option value="">All statuses</option>__STATUSES__</select>
</div>
<div class="wrap"><table><thead><tr>
<th>Date</th><th>Type</th><th>Address</th><th>Landlord / owner</th><th>Details</th><th>Status</th><th>Case</th>
</tr></thead><tbody id="rows"></tbody></table></div>
<script>
const LEADS = __DATA__;
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function render(){
  const q = document.getElementById("q").value.toLowerCase();
  const t = document.getElementById("type").value, st = document.getElementById("status").value;
  const rows = LEADS.filter(l => (!t || l.lead_type === t) && (!st || l.status === st) &&
    (!q || JSON.stringify(l).toLowerCase().includes(q)));
  document.getElementById("count").textContent = rows.length + " of " + LEADS.length + " leads";
  document.getElementById("rows").innerHTML = rows.map(l => `<tr>
    <td>${esc(l.event_date)}</td><td><span class="chip">${esc(l.lead_type)}</span></td>
    <td>${l.address ? (l.map ? `<a href="${esc(l.map)}" target="_blank" rel="noopener">${esc(l.address)}</a>` : esc(l.address)) : '<span class="muted">address needed</span>'}</td>
    <td>${esc(l.plaintiff)}</td><td>${esc(l.description)}</td><td>${esc(l.status)}</td>
    <td class="muted">${esc(l.source_id)}</td></tr>`).join("");
}
["q","type","status"].forEach(id => document.getElementById(id).addEventListener("input", render));
render();
</script></body></html>
"""


def write_html(rows, path, today=None):
    data = []
    for r in rows:
        d = {c: r[c] for c in COLUMNS}
        d["map"] = _maps_link(r)
        data.append(d)
    types = sorted({d["lead_type"] for d in data})
    statuses = sorted({d["status"] for d in data})
    opts = lambda vals: "".join(
        f'<option value="{html.escape(v)}">{html.escape(v)}</option>' for v in vals
    )
    page = (
        _PAGE.replace("__DATE__", (today or az_today()).isoformat())
        .replace("__TYPES__", opts(types))
        .replace("__STATUSES__", opts(statuses))
        # Keep "</script>" in data from closing the script tag.
        .replace("__DATA__", json.dumps(data, default=str).replace("</", "<\\/"))
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return len(rows)
