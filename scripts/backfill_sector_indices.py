"""Backfill NSE sector-index OHLC history from dated daily-index archive CSVs.

Historical-development utility only. The future daily production downloader
will resolve the current Daily Snapshot report from NSE's main All Reports page.
"""
from __future__ import annotations

import argparse
import io
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]
FUTURES_ACTIVITY = ROOT / "data" / "stock_activity_history.csv"
OUTPUT = ROOT / "data" / "sector_index_history.csv"
TEMP_OUTPUT = OUTPUT.with_suffix(".csv.tmp")

ARCHIVE_BASES = [
    "https://nsearchives.nseindia.com/content/indices/",
    "https://archives.nseindia.com/content/indices/",
]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://www.nseindia.com/",
}

# Canonical display names used inside Aurora, with accepted NSE report labels.
INDEX_ALIASES = {
    "NIFTY BANK": {"NIFTY BANK", "NIFTY BANK INDEX"},
    "NIFTY PSU BANK": {"NIFTY PSU BANK"},
    "NIFTY PRIVATE BANK": {"NIFTY PRIVATE BANK"},
    "NIFTY IT": {"NIFTY IT"},
    "NIFTY AUTO": {"NIFTY AUTO"},
    "NIFTY PHARMA": {"NIFTY PHARMA"},
    "NIFTY METAL": {"NIFTY METAL"},
    "NIFTY FMCG": {"NIFTY FMCG"},
    "NIFTY REALTY": {"NIFTY REALTY"},
    "NIFTY FINANCIAL SERVICES": {"NIFTY FINANCIAL SERVICES", "NIFTY FIN SERVICE"},
    "NIFTY ENERGY": {"NIFTY ENERGY"},
    "NIFTY MEDIA": {"NIFTY MEDIA"},
    "NIFTY HEALTHCARE": {"NIFTY HEALTHCARE", "NIFTY HEALTHCARE INDEX"},
    "NIFTY OIL & GAS": {"NIFTY OIL & GAS", "NIFTY OIL AND GAS"},
    "NIFTY CAPITAL GOODS": {"NIFTY CAPITAL GOODS"},
    "NIFTY CEMENT": {"NIFTY CEMENT"},
    "NIFTY CHEMICALS": {"NIFTY CHEMICALS"},
    "NIFTY COMMERCIAL & TRANSPORT SERVICES": {"NIFTY COMMERCIAL & TRANSPORT SERVICES"},
    "NIFTY CONSTRUCTION": {"NIFTY CONSTRUCTION"},
    "NIFTY CONSUMER DURABLES": {"NIFTY CONSUMER DURABLES"},
    "NIFTY CONSUMER SERVICES": {"NIFTY CONSUMER SERVICES"},
    "NIFTY FINANCIAL SERVICES EX BANK": {"NIFTY FINANCIAL SERVICES EX BANK", "NIFTY FINANCIAL SERVICES EX-BANK"},
    "NIFTY HOSPITALS": {"NIFTY HOSPITALS"},
    "NIFTY HOUSING FINANCE": {"NIFTY HOUSING FINANCE"},
    "NIFTY INSURANCE": {"NIFTY INSURANCE"},
    "NIFTY NBFC": {"NIFTY NBFC"},
    "NIFTY POWER": {"NIFTY POWER"},
    "NIFTY RETAIL": {"NIFTY RETAIL"},
    "NIFTY TELECOMMUNICATIONS": {"NIFTY TELECOMMUNICATIONS", "NIFTY TELECOM"},
    "NIFTY REITS & REALTY": {"NIFTY REITS & REALTY"},
    "NIFTY500 HEALTHCARE": {"NIFTY500 HEALTHCARE", "NIFTY 500 HEALTHCARE"},
    "NIFTY MIDSMALL FINANCIAL SERVICES": {"NIFTY MIDSMALL FINANCIAL SERVICES"},
    "NIFTY MIDSMALL HEALTHCARE": {"NIFTY MIDSMALL HEALTHCARE"},
    "NIFTY MIDSMALL IT & TELECOM": {"NIFTY MIDSMALL IT & TELECOM"},
}

OUTPUT_COLUMNS = [
    "Trade_Date", "Index_Name", "Open", "High", "Low", "Close",
    "Daily_Change_Pct", "Is_Muhurat", "Include_In_Calculations",
    "Source_File", "Refresh_Time_IST",
]

COLUMN_ALIASES = {
    "Index_Name": ["Index Name", "IndexName", "INDEX_NAME", "Index"],
    "Trade_Date": ["Index Date", "IndexDate", "HistoricalDate", "Date", "DATE"],
    "Open": ["Open Index Value", "OPEN", "Open"],
    "High": ["High Index Value", "HIGH", "High"],
    "Low": ["Low Index Value", "LOW", "Low"],
    "Close": ["Closing Index Value", "Close Index Value", "CLOSE", "Close"],
    "Daily_Change_Pct": ["Change(%)", "Change (%)", "% Change", "Percent Change"],
}

MUHURAT_DATES = {"2026-11-08"}


def normalize_columns(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame.columns = [str(c).replace("\ufeff", "").strip() for c in frame.columns]
    rename = {}
    for target, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in frame.columns:
                rename[alias] = target
                break
    return frame.rename(columns=rename)


def parse_snapshot(content: bytes, expected_date: date, filename: str) -> pd.DataFrame:
    if not content or not content.strip():
        raise ValueError("Empty index snapshot.")
    try:
        raw = pd.read_csv(io.BytesIO(content), encoding="utf-8-sig")
    except Exception:
        raw = pd.read_csv(io.BytesIO(content), encoding="latin-1")
    frame = normalize_columns(raw)

    required = ["Index_Name", "Trade_Date", "Open", "High", "Low", "Close"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(
            f"Unrecognised NSE index snapshot schema; missing {missing}; "
            f"received columns={list(raw.columns)}"
        )

    frame["Trade_Date"] = pd.to_datetime(frame["Trade_Date"], errors="coerce", dayfirst=True).dt.date
    frame["Index_Name"] = frame["Index_Name"].astype(str).str.strip().str.upper()
    frame = frame[frame["Trade_Date"] == expected_date].copy()

    reverse_alias = {}
    for canonical, aliases in INDEX_ALIASES.items():
        for alias in aliases:
            reverse_alias[alias.upper()] = canonical
    frame["Index_Name"] = frame["Index_Name"].map(reverse_alias)
    frame = frame.dropna(subset=["Index_Name"])

    for column in ["Open", "High", "Low", "Close"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    frame = frame[
        (frame["Open"] > 0) & (frame["High"] > 0)
        & (frame["Low"] > 0) & (frame["Close"] > 0)
        & (frame["High"] >= frame[["Open", "Close", "Low"]].max(axis=1))
        & (frame["Low"] <= frame[["Open", "Close", "High"]].min(axis=1))
    ].copy()

    if frame.empty:
        raise ValueError("None of the configured sector indices were found for this date.")
    if frame["Index_Name"].duplicated().any():
        duplicates = frame.loc[frame["Index_Name"].duplicated(), "Index_Name"].tolist()
        raise ValueError(f"Duplicate configured index records: {duplicates}")

    frame["Trade_Date"] = frame["Trade_Date"].map(lambda value: value.isoformat())
    frame["Daily_Change_Pct"] = pd.NA
    if "Daily_Change_Pct" in raw.columns:
        pass
    elif "Daily_Change_Pct" in frame.columns:
        pass
    frame["Is_Muhurat"] = frame["Trade_Date"].isin(MUHURAT_DATES)
    frame["Include_In_Calculations"] = ~frame["Is_Muhurat"]
    frame["Source_File"] = filename
    frame["Refresh_Time_IST"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    return frame[OUTPUT_COLUMNS].sort_values("Index_Name").reset_index(drop=True)


def download_day(session: requests.Session, day: date) -> pd.DataFrame | None:
    token = day.strftime("%d%m%Y")
    filename = f"ind_close_all_{token}.csv"
    for base in ARCHIVE_BASES:
        url = base + filename
        try:
            response = session.get(url, timeout=40)
            if response.status_code != 200:
                print(f"{day}: {base} HTTP {response.status_code}")
                continue
            try:
                result = parse_snapshot(response.content, day, filename)
                found = sorted(result["Index_Name"].unique())
                print(f"{day}: validated {len(result)} configured indices: {', '.join(found)}")
                return result
            except Exception as exc:
                print(f"{day}: archive rejected: {type(exc).__name__}: {exc}")
        except requests.RequestException as exc:
            print(f"{day}: request failed at {base}: {exc}")
        time.sleep(1.1)
    print(f"{day}: no usable index snapshot found (holiday, non-trading date or archive unavailable).")
    return None


def validate_existing(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    missing = [c for c in OUTPUT_COLUMNS if c not in frame.columns]
    if missing:
        raise RuntimeError(f"Existing sector index history missing columns: {missing}")
    frame = frame[OUTPUT_COLUMNS].copy()
    frame["Trade_Date"] = pd.to_datetime(frame["Trade_Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    frame["Index_Name"] = frame["Index_Name"].astype(str).str.strip().str.upper()
    if frame[["Trade_Date", "Index_Name"]].isna().any().any():
        raise RuntimeError("Existing sector index history contains invalid keys.")
    if frame.duplicated(["Trade_Date", "Index_Name"]).any():
        raise RuntimeError("Existing sector index history contains duplicate date/index rows.")
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", help="Start date YYYY-MM-DD; defaults to stock activity history start")
    parser.add_argument("--end-date", help="End date YYYY-MM-DD; defaults to stock activity history end")
    args = parser.parse_args()

    if not FUTURES_ACTIVITY.exists() or FUTURES_ACTIVITY.stat().st_size == 0:
        raise RuntimeError("Stock activity history is required before sector index backfill.")
    activity = pd.read_csv(FUTURES_ACTIVITY, usecols=["Trade_Date"], dtype={"Trade_Date": str})
    activity_dates = sorted(pd.to_datetime(activity["Trade_Date"], errors="coerce").dropna().dt.date.unique())
    if not activity_dates:
        raise RuntimeError("No valid dates found in stock activity history.")

    start = datetime.strptime(args.start_date, "%Y-%m-%d").date() if args.start_date else activity_dates[0]
    end = datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date else activity_dates[-1]
    if end < start:
        raise SystemExit("--end-date must be on or after --start-date")
    if (end - start).days > 120:
        raise SystemExit("A single backfill is limited to 120 calendar days.")

    print("AURORA NSE SECTOR-INDEX HISTORICAL BACKFILL")
    print("Source: NSE dated daily index snapshot archive")
    print(f"Requested period: {start} through {end}")
    print(f"Required stock activity dates in period: {len([d for d in activity_dates if start <= d <= end])}")
    print("8 November Muhurat records will be retained but excluded from calculations.")

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
        raise RuntimeError("No valid index archives downloaded; existing output left unchanged.")
    incoming = pd.concat(downloads, ignore_index=True)

    if OUTPUT.exists():
        existing = validate_existing(pd.read_csv(OUTPUT, dtype={"Trade_Date": str}))
    else:
        existing = pd.DataFrame(columns=OUTPUT_COLUMNS)
    combined = pd.concat([existing, incoming], ignore_index=True)
    combined = combined.drop_duplicates(["Trade_Date", "Index_Name"], keep="last")
    combined = combined.sort_values(["Trade_Date", "Index_Name"]).reset_index(drop=True)

    # Calculate index daily movement against the previous regular session.
    combined["Daily_Change_Pct"] = pd.NA
    regular_mask = ~combined["Trade_Date"].isin(MUHURAT_DATES)
    regular = combined.loc[regular_mask].sort_values(["Index_Name", "Trade_Date"]).copy()
    regular["Previous_Regular_Close"] = regular.groupby("Index_Name")["Close"].shift(1)
    regular["Daily_Change_Pct"] = (
        (regular["Close"] - regular["Previous_Regular_Close"])
        / regular["Previous_Regular_Close"] * 100
    ).where(regular["Previous_Regular_Close"] > 0).round(4)
    combined.loc[regular.index, "Daily_Change_Pct"] = regular["Daily_Change_Pct"]
    combined["Is_Muhurat"] = combined["Trade_Date"].isin(MUHURAT_DATES)
    combined["Include_In_Calculations"] = ~combined["Is_Muhurat"]

    if len(combined) < len(existing):
        raise RuntimeError("Backfill would reduce existing sector index history; refusing to write.")

    TEMP_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(TEMP_OUTPUT, index=False)
    check = validate_existing(pd.read_csv(TEMP_OUTPUT, dtype={"Trade_Date": str}))
    if len(check) != len(combined):
        raise RuntimeError("Written sector index file failed row-count validation.")
    TEMP_OUTPUT.replace(OUTPUT)

    dates = sorted(combined["Trade_Date"].unique())
    print("=" * 68)
    print(f"SUCCESS: {len(incoming)} index/date rows downloaded across {len(downloads)} dates.")
    print(f"Combined file: {len(combined)} rows; {combined['Index_Name'].nunique()} configured indices.")
    print(f"Coverage: {dates[0]} through {dates[-1]}")
    print(f"Saved: {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
