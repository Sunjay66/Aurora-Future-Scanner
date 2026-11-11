"""Diagnostic-only check of the three NSE participant derivatives reports.

This script does not write or modify any scanner data. It tests both known NSE
archive hosts for each expected report and validates downloaded content using
the same parsers as update_institutional_derivatives.py.
"""
from datetime import datetime
from io import BytesIO
from pathlib import Path
import argparse

import requests

from update_institutional_derivatives import (
    BASE_URLS,
    HEADERS,
    read_participant_csv,
    fii_rows,
)

REPORTS = [
    ("Participant OI CSV", lambda d: f"fao_participant_oi_{d.strftime('%d%m%Y')}.csv", "csv"),
    ("Participant Volume CSV", lambda d: f"fao_participant_vol_{d.strftime('%d%m%Y')}.csv", "csv"),
    ("FII derivatives XLS", lambda d: f"fii_stats_{d.strftime('%d-%b-%Y')}.xls", "xls"),
]


def validate(kind, content, trade_date):
    if kind == "csv":
        frame = read_participant_csv(content)
        return f"VALID CSV: {len(frame)} participant rows; required columns present"
    rows = fii_rows(content, trade_date)
    return f"VALID XLS: {len(rows)} segment rows; all four segments parsed"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2026-11-09", help="Report date YYYY-MM-DD")
    args = parser.parse_args()
    report_day = datetime.strptime(args.date, "%Y-%m-%d").date()
    compact = report_day.strftime("%d%m%Y")
    print(f"DIAGNOSTIC ONLY — report date: {report_day.isoformat()}")
    print("No repository data files will be written or changed.")
    print("=" * 72)

    session = requests.Session()
    session.headers.update(HEADERS)
    all_valid = True

    for label, filename_fn, kind in REPORTS:
        filename = filename_fn(report_day)
        print(f"\n{label}: {filename}")
        valid_on_any_host = False
        for base in BASE_URLS:
            url = base + filename
            try:
                response = session.get(url, timeout=30)
                content = response.content or b""
                content_type = response.headers.get("Content-Type", "(not supplied)")
                print(f"  URL: {url}")
                print(
                    f"  HTTP: {response.status_code}; bytes: {len(content)}; "
                    f"Content-Type: {content_type}"
                )
                if response.status_code != 200:
                    print(f"  RESULT: HTTP failure; preview={content[:100]!r}")
                    continue
                if not content:
                    print("  RESULT: Empty response body")
                    continue
                try:
                    parsed = validate(kind, content, report_day.isoformat())
                    print(f"  RESULT: {parsed}")
                    valid_on_any_host = True
                    break
                except Exception as exc:
                    print(
                        f"  RESULT: Downloaded but parser rejected content: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    print(f"  BODY PREFIX: {content[:160]!r}")
            except requests.RequestException as exc:
                print(f"  URL: {url}")
                print(f"  RESULT: Request exception: {type(exc).__name__}: {exc}")
        if not valid_on_any_host:
            all_valid = False
            print(f"  FINAL: NOT RETRIEVED/VALIDATED — {label}")
        else:
            print(f"  FINAL: OK — {label}")

    print("\n" + "=" * 72)
    if all_valid:
        print("DIAGNOSTIC RESULT: All three reports downloaded and validated.")
    else:
        print("DIAGNOSTIC RESULT: One or more reports failed. See per-host details above.")
    # Diagnostic failure is a report finding, not a failed diagnostic job.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
