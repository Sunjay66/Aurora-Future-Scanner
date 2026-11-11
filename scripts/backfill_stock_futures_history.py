"""Backfill daily front-month stock-futures OHLC, OI and volume from NSE archives.

This is a historical-development utility. It deliberately uses dated UDiFF
archive ZIPs and is not part of the normal daily production refresh.
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "stock_futures_history.csv"
TEMP_OUTPUT = OUTPUT.with_suffix(".csv.tmp")

ARCHIVE_BASES = [
    "https://archives.nseindia.com/content/fo/",
    "https://nsearchives.nseindia.com/content/fo/",
]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}

OUTPUT_COLUMNS = [
    "Trade_Date",
    "Symbol",
    "Expiry_Date",
    "Open_Price",
    "High_Price",
    "Low_Price",
    "Close_Price",
    "Open_Interest",
    "Volume",
    "Source_File",
    "Refresh_Time_IST",
]

ALIASES = {
    "Trade_Date": ["TradDt", "BizDt", "TRADE_DATE", "BUSINESS_DATE"],
    "Symbol": ["TckrSymb", "SYMBOL", "TICKER_SYMBOL"],
    "Expiry_Date": ["XpryDt", "EXPIRY_DATE", "EXPIRY_DT"],
    "Instrument_Type": ["FinInstrmTp", "INSTRUMENT_TYPE", "INSTRUMENT"],
    "Open_Price": ["OpnPric", "OPEN_PRICE", "OPEN"],
    "High_Price": ["HghPric", "HIGH_PRICE", "HIGH"],
    "Low_Price": ["LwPric", "LOW_PRICE", "LOW"],
    "Close_Price": ["ClsPric", "CLOSE_PRICE", "CLOSE"],
    "Open_Interest": ["OpnIntrst", "OPEN_INTEREST", "OPEN_INT"],
    "Volume": ["TtlTradgVol", "TOTAL_TRADED_VOLUME", "CONTRACTS", "VOLUME"],
}


def normalize_columns(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame.columns = [str(c).strip() for c in frame.columns]
    rename = {}
    for target, candidates in ALIASES.items():
        for candidate in candidates:
            if candidate in frame.columns:
                rename[candidate] = target
                break
    return frame.rename(columns=rename)


def parse_udiff_zip(content: bytes, expected_date: date, filename: str) -> pd.DataFrame:
    if not content.startswith(b"PK"):
        raise ValueError("Response is not a ZIP archive.")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        csv_names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise ValueError(f"Expected one CSV inside ZIP; found {len(csv_names)}.")
        with archive.open(csv_names[0]) as handle:
            raw = pd.read_csv(handle, low_memory=False)

    frame = normalize_columns(raw)
    required = OUTPUT_COLUMNS[:9]
    # Instrument_Type is an additional selector, not an output field.
    if "Instrument_Type" not in frame.columns:
        raise ValueError("Bhavcopy is missing FinInstrmTp / instrument type.")
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ValueError(f"Bhavcopy missing required fields: {missing}")

    frame["Trade_Date"] = pd.to_datetime(frame["Trade_Date"], errors="coerce").dt.date
    frame["Expiry_Date"] = pd.to_datetime(frame["Expiry_Date"], errors="coerce").dt.date
    frame = frame[frame["Instrument_Type"].astype(str).str.strip().str.upper() == "STF"].copy()
    frame = frame[frame["Trade_Date"] == expected_date].copy()
    frame = frame[frame["Expiry_Date"] >= expected_date].copy()

    for column in ["Open_Price", "High_Price", "Low_Price", "Close_Price",
                   "Open_Interest", "Volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame["Symbol"] = frame["Symbol"].astype(str).str.strip().str.upper()
    frame = frame.dropna(subset=[
        "Trade_Date", "Expiry_Date", "Symbol", "Open_Price", "High_Price",
        "Low_Price", "Close_Price", "Open_Interest", "Volume"
    ])
    frame = frame[frame["Symbol"].ne("")].copy()
    frame = frame[
        (frame["Open_Price"] > 0)
        & (frame["High_Price"] > 0)
        & (frame["Low_Price"] > 0)
        & (frame["Close_Price"] > 0)
        & (frame["Open_Interest"] >= 0)
        & (frame["Volume"] >= 0)
        & (frame["High_Price"] >= frame[["Open_Price", "Close_Price", "Low_Price"]].max(axis=1))
        & (frame["Low_Price"] <= frame[["Open_Price", "Close_Price", "High_Price"]].min(axis=1))
    ].copy()

    if frame.empty:
        raise ValueError("No valid stock-futures (STF) records for the requested date.")

    # For each symbol, retain the nearest unexpired stock-futures contract.
    frame = frame.sort_values(["Symbol", "Expiry_Date", "Volume"], ascending=[True, True, False])
    front = frame.drop_duplicates(subset=["Symbol"], keep="first").copy()

    front["Trade_Date"] = front["Trade_Date"].map(date.isoformat)
    front["Expiry_Date"] = front["Expiry_Date"].map(date.isoformat)
    front["Source_File"] = filename
    front["Refresh_Time_IST"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    front = front[OUTPUT_COLUMNS].sort_values("Symbol").reset_index(drop=True)

    if front["Symbol"].duplicated().any():
        raise ValueError("Duplicate symbols remain after front-month selection.")
    return front


def download_day(session: requests.Session, day: date) -> pd.DataFrame | None:
    date_token = day.strftime("%Y%m%d")
    filename = f"BhavCopy_NSE_FO_0_0_0_{date_token}_F_0000.csv.zip"
    last_status = None
    for base in ARCHIVE_BASES:
        url = base + filename
        try:
            response = session.get(url, timeout=45)
            last_status = response.status_code
            if response.status_code != 200:
                print(f"{day}: {base} HTTP {response.status_code}")
                continue
            try:
                result = parse_udiff_zip(response.content, day, filename)
                print(f"{day}: downloaded and validated {len(result)} front-month stock futures")
                return result
            except Exception as exc:
                print(f"{day}: archive received but rejected: {type(exc).__name__}: {exc}")
        except requests.RequestException as exc:
            print(f"{day}: request failed at {base}: {exc}")
    if last_status in (404, 403):
        print(f"{day}: no usable archive found (likely non-trading date or unavailable archive).")
    return None


def validate_existing(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    missing = [c for c in OUTPUT_COLUMNS if c not in frame.columns]
    if missing:
        raise RuntimeError(f"Existing stock-futures history missing columns: {missing}")
    frame = frame[OUTPUT_COLUMNS].copy()
    frame["Trade_Date"] = pd.to_datetime(frame["Trade_Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    frame["Expiry_Date"] = pd.to_datetime(frame["Expiry_Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if frame[["Trade_Date", "Expiry_Date", "Symbol"]].isna().any().any():
        raise RuntimeError("Existing stock-futures history contains invalid keys.")
    if frame.duplicated(["Trade_Date", "Symbol"]).any():
        raise RuntimeError("Existing stock-futures history contains duplicate date/symbol rows.")
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True, help="First archive date, YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="Last archive date, YYYY-MM-DD")
    args = parser.parse_args()

    start = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    end = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    if end < start:
        raise SystemExit("--end-date must be on or after --start-date")
    if (end - start).days > 120:
        raise SystemExit("A single backfill is limited to 120 calendar days; use smaller batches.")

    print("AURORA HISTORICAL STOCK-FUTURES BACKFILL")
    print(f"Source: NSE dated UDiFF archive ZIPs")
    print(f"Date range: {start} through {end}")
    print(f"Destination: {OUTPUT.relative_to(ROOT)}")
    print("This script does not modify existing OI classification snapshots.")

    session = requests.Session()
    session.headers.update(HEADERS)
    downloads = []
    day = start
    while day <= end:
        result = download_day(session, day)
        if result is not None:
            downloads.append(result)
        day += timedelta(days=1)

    if not downloads:
        raise RuntimeError("No valid historical bhavcopy files were downloaded; output left unchanged.")

    incoming = pd.concat(downloads, ignore_index=True)
    if incoming.empty:
        raise RuntimeError("Downloaded archive set produced no stock-futures rows; output left unchanged.")

    if OUTPUT.exists():
        existing_raw = pd.read_csv(OUTPUT, dtype={"Trade_Date": str, "Symbol": str})
        existing = validate_existing(existing_raw)
    else:
        existing = pd.DataFrame(columns=OUTPUT_COLUMNS)

    combined = pd.concat([existing, incoming], ignore_index=True)
    combined = combined.drop_duplicates(["Trade_Date", "Symbol"], keep="last")
    combined = combined.sort_values(["Trade_Date", "Symbol"]).reset_index(drop=True)

    if len(combined) < len(existing):
        raise RuntimeError("Backfill would reduce existing history; refusing to write.")

    TEMP_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(TEMP_OUTPUT, index=False)
    check = pd.read_csv(TEMP_OUTPUT, dtype={"Trade_Date": str, "Symbol": str})
    validate_existing(check)
    TEMP_OUTPUT.replace(OUTPUT)

    dates = sorted(combined["Trade_Date"].unique())
    print("=" * 68)
    print(f"SUCCESS: {len(incoming)} rows downloaded across {len(downloads)} trading dates.")
    print(f"Combined history: {len(combined)} rows across {len(dates)} dates.")
    print(f"Date coverage: {dates[0]} through {dates[-1]}")
    print(f"Symbols on latest date: {combined[combined['Trade_Date'] == dates[-1]]['Symbol'].nunique()}")
    print(f"Saved: {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
