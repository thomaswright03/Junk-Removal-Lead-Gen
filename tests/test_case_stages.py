"""Case stage from court papers: a judgment or writ only when the court
entered one for the landlord, and the latest dispositive paper decides.

The pages in fixtures/jp_cases are made-up cases (invented names and
numbers) in the layout of the Justice Court's jcDisplayCase.aspx page, one
per kind of paper and stage change."""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from leadgen import db
from leadgen.sources.pima_jp_case import (
    ENDED_STAGES,
    case_stage,
    cases_due,
    paper_kind,
    parse_case_html,
    rederive_stages,
)
from leadgen.web import App

CASES = Path(__file__).parent / "fixtures" / "jp_cases"
TODAY = date(2026, 10, 3)
URL = "https://www.jp.pima.gov/CaseSearch/jcDisplayCase.aspx?ID={}"


def read(name: str, today: date = TODAY):
    html = (CASES / f"{name}.html").read_text()
    return parse_case_html(html, today=today)


@pytest.mark.parametrize(
    "name, stage, judgment, writ",
    [
        # The tenant paid; the eviction hearing is still to come.
        ("satisfied_before_hearing", "satisfied", None, None),
        # Judgment, a motion to set it aside, a stipulated dismissal; Disposed.
        ("set_aside_then_stipulated_dismissal", "dismissed", None, None),
        # A motion to set aside that isn't decided yet changes nothing.
        ("judgment_motion_pending", "judgment", "2026-09-22", None),
        # The court granted it: no judgment any more.
        ("judgment_set_aside", "notice", None, None),
        # Applying for a writ is not a writ.
        ("writ_applied_for", "judgment", "2026-09-23", None),
        # A writ of garnishment collects money; it is not a lockout.
        ("writ_of_garnishment", "judgment", "2026-08-20", None),
        ("writ_issued", "writ", "2026-09-21", "2026-09-29"),
        # Judgment and writ rows dated after today haven't happened.
        ("future_events_only", "notice", None, None),
        ("judgment_then_dismissal", "dismissed", None, None),
        # A dismissal, then the tenant missed a payment: judgment and writ.
        ("dismissal_then_writ", "writ", "2026-09-22", "2026-09-30"),
        ("disposed_no_judgment", "closed", None, None),
        # Disposed because the court decided for the landlord: still a lead.
        ("disposed_with_judgment", "judgment", "2026-09-24", None),
        ("judgment_for_defendant", "dismissed", None, None),
        ("writ_quashed", "judgment", "2026-09-15", None),
        ("motion_to_dismiss_denied", "judgment", "2026-09-18", None),
        ("notice_hearing_next_week", "notice", None, None),
    ],
)
def test_stage_from_each_kind_of_paper(name, stage, judgment, writ):
    lead = read(name)
    assert (lead.case_stage, lead.judgment_date, lead.writ_date) == (stage, judgment, writ)
    if stage not in ("judgment", "writ"):
        assert "Judgment for the landlord" not in lead.description
        assert "Writ of restitution issued" not in lead.description


def test_future_papers_count_once_their_day_comes():
    assert read("future_events_only", today=date(2026, 10, 9)).case_stage == "judgment"


@pytest.mark.parametrize(
    "text, kind",
    [
        ("CIV - JUDGMENT - EVICTION", "judgment"),
        ("CIV - DEFAULT JUDGMENT AGAINST DEFENDANT", "judgment"),
        ("10/14/2026 02:00 PM Hearing Eviction Action Judgment for Plaintiff", "judgment"),
        ("Judgment for Defendant", "for_tenant"),
        ("Judgment in favor of the Tenant", "for_tenant"),
        ("Satisfaction of Judgment", "satisfied"),
        ("CIV - PARTIAL SATISFACTION OF JUDGMENT", None),
        ("CIV - MOTION TO SET ASIDE JUDGMENT", None),
        ("CIV - ORDER GRANTING MOTION TO SET ASIDE JUDGMENT", "set_aside"),
        ("Judgment Vacated", "set_aside"),
        ("CIV - APPLICATION FOR WRIT OF RESTITUTION", None),
        ("CIV - WRIT OF RESTITUTION ISSUED", "writ"),
        ("CIV - WRIT OF GARNISHMENT (NON-EARNINGS)", None),
        ("CIV - WRIT OF EXECUTION", None),
        ("Judgment Debtor Exam Vacated", None),
        ("CIV - ORDER QUASHING WRIT OF RESTITUTION", "writ_quashed"),
        ("CIV - STIPULATED DISMISSAL", "dismissed"),
        ("CIV - NOTICE OF VOLUNTARY DISMISSAL", "dismissed"),
        ("CIV - MOTION TO DISMISS", None),
        ("CIV - ORDER DENYING MOTION TO DISMISS", None),
        ("CIV - ORDER GRANTING MOTION TO DISMISS", "dismissed"),
        ("Hearing Eviction Action Vacated", None),
        ("CIV - REQUEST FOR LOCKOUT", None),
        ("CIV - NOTICE OF HEARING", None),
    ],
)
def test_what_one_paper_means(text, kind):
    assert paper_kind(text) == kind


def test_latest_paper_decides_in_either_order():
    judgment = {"Document SubType": "CIV - JUDGMENT - EVICTION", "FILE DATE": "9/10/2026"}
    dismissal = {"Document SubType": "CIV - STIPULATED DISMISSAL", "FILE DATE": "9/20/2026"}
    writ = {"Document SubType": "CIV - WRIT OF RESTITUTION", "FILE DATE": "9/25/2026"}
    assert case_stage([judgment, dismissal], [], "Open", True, today=TODAY)[0] == "dismissed"
    dismissal_first = dict(dismissal, **{"FILE DATE": "9/01/2026"})
    assert case_stage([dismissal_first, judgment], [], "Open", True, today=TODAY)[0] == "judgment"
    assert case_stage([judgment, dismissal, writ], [], "Open", True, today=TODAY) == (
        "writ",
        "2026-09-10",
        "2026-09-25",
    )
    # The court's status says dismissed: ended, unless a writ is in force.
    assert case_stage([judgment], [], "Dismissed", True, today=TODAY)[0] == "dismissed"
    assert case_stage([judgment, writ], [], "Dismissed", True, today=TODAY)[0] == "writ"
    # The parties table's "Judgment For" is a judgment too, for whichever side.
    assert case_stage([], [], "Open", True, [{"for": "Plaintiff", "date": "2026-09-30"}], TODAY)[:2] == (
        "judgment",
        "2026-09-30",
    )
    assert case_stage([], [], "Open", True, [{"for": "Defendant", "date": "2026-09-30"}], TODAY)[0] == "dismissed"


def _add(conn, name, page_id, today=TODAY):
    html = (CASES / f"{name}.html").read_text()
    lead = parse_case_html(html, url=URL.format(page_id), today=today)
    db.upsert(conn, lead)
    conn.commit()
    return lead


def test_ended_cases_leave_the_default_view_and_rank_order_is_honest(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    names = [
        "satisfied_before_hearing",
        "set_aside_then_stipulated_dismissal",
        "judgment_then_dismissal",
        "disposed_no_judgment",
        "judgment_for_defendant",
        "writ_issued",
        "judgment_motion_pending",
        "notice_hearing_next_week",
    ]
    for i, name in enumerate(names):
        _add(conn, name, 2000100 + i)
    leads = App(path).state()["leads"]
    shown = [l["source_id"] for l in leads]
    assert shown[:2] == ["CV26-090007-EA", "CV26-090003-EA"]  # the writ, then the judgment
    assert set(shown) == {"CV26-090007-EA", "CV26-090003-EA", "CV26-090016-EA"}
    stages = {r["source_id"]: r["case_stage"] for r in conn.execute("SELECT source_id, case_stage FROM leads")}
    assert all(stages[s] in ENDED_STAGES for s in stages if s not in shown)


def test_rereading_a_case_clears_a_judgment_that_ended(tmp_path):
    conn = db.connect(tmp_path / "l.db")
    _add(conn, "judgment_motion_pending", 2000200)
    row = conn.execute("SELECT case_stage, judgment_date FROM leads").fetchone()
    assert (row["case_stage"], row["judgment_date"]) == ("judgment", "2026-09-22")
    # Same case, read again after the parties settled.
    html = (CASES / "judgment_motion_pending.html").read_text()
    html = html.replace(
        "</table>\n</td></tr></table>",
        "<tr><td>Civil Documents</td><td>CIV - STIPULATED DISMISSAL</td><td>CIV - STIPULATED DISMISSAL</td>"
        "<td>10/01/2026</td></tr>\n  </table>\n</td></tr></table>",
    )
    db.upsert(conn, parse_case_html(html, url=URL.format(2000200), today=TODAY))
    row = conn.execute("SELECT case_stage, judgment_date, description FROM leads").fetchone()
    assert (row["case_stage"], row["judgment_date"]) == ("dismissed", None)
    assert "Case dismissed" in row["description"] and "Judgment for the landlord" not in row["description"]


def _old_row(conn, name, page_id, stage, judgment_date=None, writ_date=None, keep_parties=False):
    """A case stored by an older version: its stage from the old rules and
    papers saved without the parties table's judgments."""
    lead = _add(conn, name, page_id)
    raw = dict(lead.raw)
    if not keep_parties:
        raw.pop("judgments")
    conn.execute(
        "UPDATE leads SET case_stage = ?, judgment_date = ?, writ_date = ?, raw_json = ?, "
        "description = 'Case open | Eviction notice filed | Judgment for the landlord' WHERE source_id = ?",
        (stage, judgment_date, writ_date, json.dumps(raw), lead.source_id),
    )
    conn.commit()
    return lead.source_id


def test_stored_cases_get_their_stage_again_at_startup(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    satisfied = _old_row(conn, "satisfied_before_hearing", 2000301, "judgment", "2026-09-25")
    dismissed = _old_row(conn, "set_aside_then_stipulated_dismissal", 2000302, "judgment", "2025-03-10")
    applied = _old_row(conn, "writ_applied_for", 2000303, "writ", "2026-09-23", "2026-09-29")
    # Judgment known only from the parties table (no paper says judgment).
    party_only = _old_row(conn, "notice_hearing_next_week", 2000304, "judgment", "2026-09-30")
    conn.execute("DELETE FROM settings WHERE key = 'case_stage_rules'")
    conn.commit()
    conn.close()

    conn = db.connect(path)  # an older database opened by this version
    rows = {r["source_id"]: r for r in conn.execute("SELECT * FROM leads")}
    assert rows[satisfied]["case_stage"] == "satisfied" and rows[satisfied]["judgment_date"] is None
    assert "Judgment for the landlord" not in rows[satisfied]["description"]
    assert rows[dismissed]["case_stage"] == "dismissed"
    assert (rows[applied]["case_stage"], rows[applied]["writ_date"]) == ("judgment", None)
    assert (rows[party_only]["case_stage"], rows[party_only]["judgment_date"]) == ("judgment", "2026-09-30")
    # All four are read again soon, parties table and all.
    assert {r["case_checked_at"] for r in rows.values()} == {"2000-01-01T00:00:00+00:00"}
    due = [r["id"] for r in cases_due(conn)]
    assert rows[applied]["id"] in due and rows[party_only]["id"] in due
    # Done once: a later open leaves the rows alone.
    conn.execute("UPDATE leads SET case_stage = 'writ' WHERE source_id = ?", (applied,))
    conn.commit()
    conn.close()
    assert (
        db.connect(path).execute("SELECT case_stage FROM leads WHERE source_id = ?", (applied,)).fetchone()[0] == "writ"
    )


def test_rederive_keeps_rows_already_right():
    conn = db.connect(":memory:")
    _add(conn, "writ_issued", 2000400)
    assert rederive_stages(conn, today=TODAY) == 0


def test_young_ended_cases_are_read_weekly_old_ones_never():
    conn = db.connect(":memory:")
    _add(conn, "judgment_then_dismissal", 2000500)  # filed Aug 25, 2026
    _add(conn, "set_aside_then_stipulated_dismissal", 2000501)  # filed in 2025
    now = datetime(2026, 10, 3, 18, tzinfo=timezone.utc)
    conn.execute("UPDATE leads SET case_checked_at = ?", ((now - timedelta(days=2)).isoformat(),))
    assert cases_due(conn, now=now) == []
    later = now + timedelta(days=6)
    assert [r["url"] for r in cases_due(conn, now=later)] == [URL.format(2000500)]
    assert cases_due(conn, now=now + timedelta(days=90)) == []  # too old to come back
