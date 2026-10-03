from datetime import date, datetime, timezone

from leadgen.util import az_today, decode_text, is_multifamily, is_residential, pick


def test_today_is_the_arizona_date_in_the_evening():
    # 18:00 in Tucson is 01:00 UTC the next day.
    evening = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)
    assert az_today(evening) == date(2026, 10, 3)


def test_pick_matches_columns_loosely():
    row = {" Phone 1 ": " 520-555-0100 ", "Email": "", None: "extra"}
    assert pick(row, ("phone", "phone 1")) == "520-555-0100"
    assert pick(row, ("email",)) is None


def test_decode_text_reads_excel_csv():
    assert decode_text("Calle Ñandú 5".encode("cp1252")) == "Calle Ñandú 5"
    assert decode_text("﻿Calle Ñandú 5".encode("utf-8")) == "Calle Ñandú 5"


def test_one_definition_of_multifamily():
    assert is_multifamily("APARTMENTS 25+ UNITS") and not is_multifamily("SFR GRADE 010-3")
    assert is_residential("RESIDENTIAL RENTAL") and is_residential("CONDO")
