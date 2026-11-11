"""Maintain NSE cash-market delivery history in one normalized CSV.

Accepts both NSE Security-wise Delivery Positions (MTO .DAT) and
Full Bhavcopy and Security Deliverable Data (.csv). Dashboard logic reads
only data/delivery_history.csv and is independent of the source extension.

Safety rules:
- The existing historical file must exist and pass validation before updates.
- Existing records are never replaced by an empty or malformed download.
- The combined dataset is validated and written atomically only after checks pass.
"""
from pathlib import Path
from datetime import datetime, timedelta
from io import StringIO
import csv
import re

import requests
import pandas as pd

DATA_DIR = Path("data")
SNAPSHOT_DIR = DATA_DIR / "history"
OUTPUT = DATA_DIR / "delivery_history.csv"
TEMP_OUTPUT = OUTPUT.with_suffix(".csv.tmp")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}
MUHURAT_DATES = {"2026-11-08"}
COLUMNS = [
    "Trade_Date",
    "Session_Type",
    "Symbol",
    "Quantity_Traded",
    "Deliverable_Quantity",
    "Delivery_Pct",
]


def latest_snapshot_date():
    dates = []
    for path in SNAPSHOT_DIR.glob("snapshot_*.csv"):
        match = re.search(r"snapshot_(\d{4}-\d{2}-\d{2})", path.name)
        if match:
            dates.append(match.group(1))
    if not dates:
        raise RuntimeError(
            "No dated OI snapshots found; cannot determine delivery-report date range."
        )
    return max(dates)


def validate_history(frame, label, required_latest=None):
    """Reject empty, malformed, duplicated, or internally inconsistent history."""
    if frame is None or frame.empty:
        raise RuntimeError(f"{label} is empty. Refusing to continue.")

    missing = set(COLUMNS) - set(frame.columns)
    if missing:
        raise RuntimeError(
            f"{label} is missing required columns: {sorted(missing)}"
        )

    checked = frame[COLUMNS].copy()
    checked["Trade_Date"] = checked["Trade_Date"].astype(str).str.strip()
    checked["Session_Type"] = checked["Session_Type"].astype(str).str.strip()
    checked["Symbol"] = checked["Symbol"].astype(str).str.strip().str.upper()

    parsed_dates = pd.to_datetime(checked["Trade_Date"], format="%Y-%m-%d", errors="coerce")
    if parsed_dates.isna().any():
        raise RuntimeError(f"{label} contains invalid Trade_Date values.")

    if checked["Symbol"].eq("").any():
        raise RuntimeError(f"{label} contains blank stock symbols.")

    if not checked["Session_Type"].isin(["Regular", "Muhurat"]).all():
        raise RuntimeError(f"{label} contains an unrecognised Session_Type.")

    for column in ["Quantity_Traded", "Deliverable_Quantity", "Delivery_Pct"]:
        checked[column] = pd.to_numeric(checked[column], errors="coerce")
        if checked[column].isna().any():
            raise RuntimeError(f"{label} contains non-numeric values in {column}.")

    if (checked["Quantity_Traded"] <= 0).any():
        raise RuntimeError(f"{label} contains zero or negative traded quantities.")
    if (checked["Deliverable_Quantity"] < 0).any():
        raise RuntimeError(f"{label} contains negative deliverable quantities.")
    if (checked["Deliverable_Quantity"] > checked["Quantity_Traded"]).any():
        raise RuntimeError(f"{label} contains deliverable quantity above traded quantity.")

    calculated_pct = (
        checked["Deliverable_Quantity"] / checked["Quantity_Traded"] * 100
    )
    if ((calculated_pct - checked["Delivery_Pct"]).abs() > 0.02).any():
        raise RuntimeError(f"{label} contains inconsistent Delivery_Pct values.")

    duplicate_count = checked.duplicated(["Trade_Date", "Symbol"]).sum()
    if duplicate_count:
        raise RuntimeError(
            f"{label} contains {duplicate_count} duplicate Trade_Date/Symbol records."
        )

    max_date = checked["Trade_Date"].max()
    if required_latest and max_date < required_latest:
        raise RuntimeError(
            f"{label} is stale: latest date is {max_date}, "
            f"but latest OI snapshot is {required_latest}. "
            "Existing file has been left unchanged."
        )

    return checked


def make_record(day, symbol, traded, deliverable, reported_pct=None):
    symbol = str(symbol).strip().upper()
    traded, deliverable = int(float(traded)), int(float(deliverable))
    if not symbol or traded <= 0 or deliverable < 0 or deliverable > traded:
        return None
    pct = deliverable / traded * 100
    if reported_pct is not None and abs(pct - float(reported_pct)) > 0.02:
        return None
    return {
        "Trade_Date": day.isoformat(),
        "Session_Type": "Muhurat" if day.isoformat() in MUHURAT_DATES else "Regular",
        "Symbol": symbol,
        "Quantity_Traded": traded,
        "Deliverable_Quantity": deliverable,
        "Delivery_Pct": round(pct, 4),
    }


def parse_mto(content, day):
    text = content.decode("utf-8", errors="replace")
    records = []
    for row in csv.reader(StringIO(text)):
        if len(row) < 7 or row[0].strip() != "20" or row[3].strip().upper() != "EQ":
            continue
        try:
            rec = make_record(day, row[2], row[4], row[5], row[6])
            if rec:
                records.append(rec)
        except (ValueError, IndexError):
            continue
    return records


def parse_full_bhavdata(content, day):
    frame = pd.read_csv(
        StringIO(content.decode("utf-8", errors="replace")),
        skipinitialspace=True,
    )
    frame.columns = [str(c).strip().upper().replace(" ", "_") for c in frame.columns]
    required = {"SYMBOL", "SERIES", "TOTTRDQTY", "DELIVQTY", "DELIVPER"}
    # NSE column names vary slightly in punctuation/spacing across versions.
    aliases = {
        "TOTAL_TRADED_QUANTITY": "TOTTRDQTY",
        "TOTAL_TRADED_QTY": "TOTTRDQTY",
        "TOT_TRD_QTY": "TOTTRDQTY",
        "DELIVERABLE_QTY": "DELIVQTY",
        "DELIV_QTY": "DELIVQTY",
        "DELIVERABLE_QTY_TO_TRADED_QTY": "DELIVPER",
        "DELIV_PER": "DELIVPER",
        "DELIVERABLE_QTY_TO_TRADED_QTY_%": "DELIVPER",
        "DELIVERABLE_QTY_TO_TRADED_QTY_PERCENT": "DELIVPER",
    }
    frame = frame.rename(columns={k: v for k, v in aliases.items() if k in frame.columns})
    if not required.issubset(frame.columns):
        raise ValueError(f"Unrecognised Full Bhavcopy CSV columns: {list(frame.columns)}")

    records = []
    for _, row in frame.iterrows():
        if str(row.get("SERIES", "")).strip().upper() != "EQ":
            continue
        try:
            rec = make_record(
                day,
                row["SYMBOL"],
                row["TOTTRDQTY"],
                row["DELIVQTY"],
                row["DELIVPER"],
            )
            if rec:
                records.append(rec)
        except (ValueError, TypeError):
            continue
    return records


def fetch_report(session, day):
    ddmmyyyy = day.strftime("%d%m%Y")
    sources = [
        (
            "MTO DAT",
            f"https://nsearchives.nseindia.com/archives/equities/mto/MTO_{ddmmyyyy}.DAT",
            parse_mto,
        ),
        (
            "Full Bhavcopy CSV",
            f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv",
            parse_full_bhavdata,
        ),
    ]
    for label, url, parser in sources:
        try:
            response = session.get(url, timeout=30)
            if response.status_code != 200 or not response.content:
                continue
            records = parser(response.content, day)
            if records:
                print(f"{label}: {day} — {len(records)} EQ securities")
                return records
            print(f"{label}: downloaded but no valid EQ records for {day}")
        except (requests.RequestException, ValueError, pd.errors.ParserError) as exc:
            print(f"{label} unavailable/invalid for {day}: {exc}")
    return []


def main():
    latest_text = latest_snapshot_date()
    latest = datetime.strptime(latest_text, "%Y-%m-%d").date()
    start = latest - timedelta(days=14)

    # Never bootstrap from an empty/missing file during a routine refresh.
    if not OUTPUT.exists() or OUTPUT.stat().st_size == 0:
        raise RuntimeError(
            f"{OUTPUT} is missing or empty. Refusing to create or replace "
            "the historical dataset automatically."
        )

    existing_raw = pd.read_csv(
        OUTPUT,
        dtype={"Trade_Date": str, "Symbol": str},
    )
    existing = validate_history(
        existing_raw,
        "Existing delivery history",
        required_latest=latest_text,
    )
    existing_rows = len(existing)

    session = requests.Session()
    session.headers.update(HEADERS)
    downloaded = []
    day = start
    while day <= latest:
        records = fetch_report(session, day)
        if records:
            downloaded.extend(records)
        day += timedelta(days=1)

    if downloaded:
        fresh = pd.DataFrame(downloaded, columns=COLUMNS)
        # Validate source records before allowing them into historical data.
        fresh = validate_history(fresh, "New NSE delivery records")
        combined = pd.concat([existing, fresh], ignore_index=True)
        combined = combined.drop_duplicates(["Trade_Date", "Symbol"], keep="last")
        combined = combined.sort_values(["Trade_Date", "Symbol"]).reset_index(drop=True)
    else:
        # A temporary NSE outage must not erase or rewrite existing history.
        combined = existing.copy()
        print("No new NSE delivery reports downloaded; preserving existing history.")

    final = validate_history(
        combined,
        "Final delivery history",
        required_latest=latest_text,
    )
    if len(final) < existing_rows:
        raise RuntimeError(
            "Final delivery history has fewer rows than the existing file. "
            "Refusing to replace the existing dataset."
        )

    # Write to a temporary file, read it back, validate, then atomically replace.
    final.to_csv(TEMP_OUTPUT, index=False)
    written = pd.read_csv(
        TEMP_OUTPUT,
        dtype={"Trade_Date": str, "Symbol": str},
    )
    validate_history(written, "Written temporary delivery history", required_latest=latest_text)
    TEMP_OUTPUT.replace(OUTPUT)

    print(
        "Delivery history available through",
        final["Trade_Date"].max(),
        "rows:",
        len(final),
        "(previous rows:",
        existing_rows,
        ")",
    )


if __name__ == "__main__":
    main()
