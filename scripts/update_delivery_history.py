# ============================================================
# AURORA NSE DELIVERY HISTORY UPDATER
# Backfills every futures-snapshot trading date and refreshes the
# latest date from NSE's "Full Bhavcopy and Security Deliverable data".
# Source: sec_bhavdata_full_DDMMYYYY.csv
# ============================================================

from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time

import pandas as pd
import requests

IST = ZoneInfo("Asia/Kolkata")
DATA = Path("data")
HISTORY_DIR = DATA / "history"
OUTPUT = DATA / "delivery_history.csv"
BASE_URLS = [
    "https://nsearchives.nseindia.com/products/content/",
    "https://archives.nseindia.com/products/content/",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/csv,application/csv,*/*",
    "Referer": "https://www.nseindia.com/",
}
OUTPUT_COLUMNS = [
    "Trade_Date", "Session_Type", "Symbol",
    "Quantity_Traded", "Deliverable_Quantity", "Delivery_Pct",
]


def read_existing():
    if not OUTPUT.exists():
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    df = pd.read_csv(OUTPUT, dtype=str, keep_default_na=False)
    for col in OUTPUT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[OUTPUT_COLUMNS]


def required_report_dates():
    dates = set()
    for path in sorted(HISTORY_DIR.glob("snapshot_*.csv")):
        try:
            frame = pd.read_csv(path, usecols=["Trade_Date"])
            parsed = pd.to_datetime(frame["Trade_Date"], errors="coerce").dropna()
            dates.update(parsed.dt.strftime("%Y-%m-%d").unique().tolist())
        except Exception as exc:
            print(f"WARNING: Could not read trading date from {path.name}: {exc}")
    if not dates:
        raise RuntimeError("No futures snapshot dates found; cannot determine delivery history coverage.")
    return sorted(dates)


def parse_full_bhavcopy(content, expected_date):
    from io import BytesIO

    raw = pd.read_csv(BytesIO(content), dtype=str, keep_default_na=False)
    raw.columns = [str(c).strip().upper() for c in raw.columns]
    required = {
        "SYMBOL", "SERIES", "DATE1", "TTL_TRD_QNTY",
        "DELIV_QTY", "DELIV_PER",
    }
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError(f"NSE full bhavcopy schema missing columns: {missing}")

    # NSE's legacy full bhavcopy uses DD-Mon-YYYY in DATE1.
    raw["DATE1_PARSED"] = pd.to_datetime(raw["DATE1"].astype(str).str.strip(), errors="coerce", dayfirst=True)
    raw = raw[raw["DATE1_PARSED"].dt.strftime("%Y-%m-%d").eq(expected_date)].copy()
    if raw.empty:
        raise ValueError(f"NSE file contains no rows for expected date {expected_date}")

    raw["SERIES"] = raw["SERIES"].astype(str).str.strip().str.upper()
    raw = raw[raw["SERIES"].eq("EQ")].copy()
    if len(raw) < 1000:
        raise ValueError(f"Only {len(raw)} EQ rows found for {expected_date}; refusing incomplete delivery data.")

    raw["SYMBOL"] = raw["SYMBOL"].astype(str).str.strip().str.upper()
    raw["TTL_TRD_QNTY"] = pd.to_numeric(raw["TTL_TRD_QNTY"].astype(str).str.replace(",", "", regex=False), errors="coerce")
    raw["DELIV_QTY"] = pd.to_numeric(raw["DELIV_QTY"].astype(str).str.replace(",", "", regex=False).replace({"-": ""}), errors="coerce")
    raw["DELIV_PER"] = pd.to_numeric(raw["DELIV_PER"].astype(str).str.replace("%", "", regex=False).replace({"-": ""}), errors="coerce")

    # Prefer NSE's published DELIV_PER. Recalculate only if it is absent
    # and both quantities are available; never convert missing delivery to zero.
    calculated = raw["DELIV_QTY"].div(raw["TTL_TRD_QNTY"].replace(0, pd.NA)) * 100
    raw["DELIV_PER"] = raw["DELIV_PER"].where(raw["DELIV_PER"].notna(), calculated)
    invalid = raw["DELIV_PER"].notna() & ~raw["DELIV_PER"].between(0, 100)
    if invalid.any():
        raise ValueError(f"{int(invalid.sum())} delivery percentages outside 0-100 for {expected_date}")

    out = pd.DataFrame({
        "Trade_Date": expected_date,
        "Session_Type": "Regular",
        "Symbol": raw["SYMBOL"],
        "Quantity_Traded": raw["TTL_TRD_QNTY"],
        "Deliverable_Quantity": raw["DELIV_QTY"],
        "Delivery_Pct": raw["DELIV_PER"].round(4),
    })
    out = out[out["Symbol"].ne("")].drop_duplicates(["Trade_Date", "Symbol"], keep="last")
    if len(out) < 1000:
        raise ValueError(f"Only {len(out)} unique EQ symbols found for {expected_date}; refusing incomplete data.")
    return out


def download_date(session, date_text):
    day = datetime.strptime(date_text, "%Y-%m-%d").date()
    filename = f"sec_bhavdata_full_{day:%d%m%Y}.csv"
    errors = []
    for base in BASE_URLS:
        url = base + filename
        try:
            response = session.get(url, timeout=40)
            if response.status_code != 200:
                errors.append(f"{response.status_code} {url}")
                continue
            if len(response.content) < 20000:
                errors.append(f"too-small response ({len(response.content)} bytes) {url}")
                continue
            return parse_full_bhavcopy(response.content, date_text)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError(" | ".join(errors) if errors else "No NSE delivery source responded.")


def main():
    print("AURORA DELIVERY HISTORY — NSE Full Bhavcopy")
    dates = required_report_dates()
    existing = read_existing()
    existing_dates = set(existing["Trade_Date"].astype(str).str.strip())

    # Build a 20-session delivery history ending on the latest futures date.
    # Snapshot dates are always included. Then walk backwards over weekdays;
    # NSE holidays/non-trading days are skipped when the archive is unavailable.
    latest_date = max(dates)
    latest_day = datetime.strptime(latest_date, "%Y-%m-%d").date()
    existing_valid_dates = set(
        existing.loc[
            pd.to_datetime(existing["Trade_Date"], errors="coerce").dt.strftime("%Y-%m-%d").notna(),
            "Trade_Date"
        ].astype(str).str.strip()
    )
    existing_valid_dates = {d for d in existing_valid_dates if d <= latest_date}
    targets = {d for d in dates if d not in existing_dates or d == latest_date}

    # Refresh latest report date and backfill enough prior sessions for a
    # genuine 20-session average. Try weekdays only; NSE holidays return 404.
    valid_session_count = len(existing_valid_dates)
    candidate = latest_day
    while valid_session_count < 20 and (latest_day - candidate).days <= 60:
        candidate_text = candidate.strftime("%Y-%m-%d")
        if candidate.weekday() < 5 and candidate_text not in existing_valid_dates:
            targets.add(candidate_text)
        candidate = candidate - timedelta(days=1)
        # Count only confirmed dates after each successful download below.
        if len(targets) > 80:
            break

    targets = sorted(targets, reverse=True)
    print(f"Futures snapshot dates: {len(dates)}; existing delivery dates: {len(existing_dates)}")
    print(f"Confirmed delivery sessions on/before {latest_date}: {valid_session_count}/20")
    print(f"Dates to download/refresh: {targets}")

    session = requests.Session()
    session.headers.update(HEADERS)
    successful = []
    failed = []
    confirmed_dates = set(existing_valid_dates)
    for date_text in targets:
        try:
            daily = download_date(session, date_text)
            successful.append(daily)
            confirmed_dates.add(date_text)
            print(f"OK {date_text}: {len(daily)} EQ symbols; delivery values present for {daily['Delivery_Pct'].notna().sum()}")
            if len(confirmed_dates) >= 20 and date_text < latest_date:
                # All remaining older candidates are unnecessary for the 20-session window.
                break
        except Exception as exc:
            failed.append((date_text, str(exc)))
            print(f"SKIP {date_text}: no verified NSE delivery report ({exc})")
        time.sleep(0.35)

    if successful:
        refreshed_dates = {frame["Trade_Date"].iloc[0] for frame in successful}
        existing = existing[~existing["Trade_Date"].astype(str).isin(refreshed_dates)]
        result = pd.concat([existing, *successful], ignore_index=True)
        result = result.drop_duplicates(["Trade_Date", "Symbol"], keep="last")
        result = result.sort_values(["Trade_Date", "Symbol"])
        result.to_csv(OUTPUT, index=False)
        print(f"Saved {OUTPUT}: {len(result)} rows across {result['Trade_Date'].nunique()} dates.")

    if failed:
        print(f"WARNING: {len(failed)} date(s) could not be downloaded. Existing history was preserved for those dates.")
        for date_text, message in failed:
            print(f"  {date_text}: {message}")
    else:
        print("All required futures dates have delivery data refreshed or confirmed.")

    # Do not fail the entire market-data pipeline because NSE's separate
    # delivery report may be published later than the F&O bhavcopy.
    print(f"Delivery updater completed: {len(confirmed_dates)}/20 verified sessions available through {latest_date}.")
    if len(confirmed_dates) < 20:
        print("WARNING: 20-session average will remain blank where 20 valid observations are unavailable.")


if __name__ == "__main__":
    main()
