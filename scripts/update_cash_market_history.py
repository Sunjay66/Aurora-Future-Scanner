# ============================================================
# AURORA CASH MARKET OHLC HISTORY
# Downloads NSE CM UDiFF bhavcopy for ADR-14 calculation.
# This is indicator data preparation, NOT a backtest.
# ============================================================

from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import io
import zipfile

import pandas as pd
import requests

IST = ZoneInfo("Asia/Kolkata")
TODAY = datetime.now(IST).date()
DATA_DIR = Path("data")
CASH_DIR = DATA_DIR / "cash_history"
CASH_DIR.mkdir(parents=True, exist_ok=True)

BASE_URLS = [
    "https://nsearchives.nseindia.com/content/cm/",
    "https://archives.nseindia.com/content/cm/",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}
OUTPUT_COLUMNS = [
    "Trade_Date", "Symbol", "Open_Price", "High_Price",
    "Low_Price", "Close_Price"
]


def download_cash_bhavcopy(check_date):
    date_string = check_date.strftime("%Y%m%d")
    filename = f"BhavCopy_NSE_CM_0_0_0_{date_string}_F_0000.csv.zip"
    session = requests.Session()
    session.headers.update(HEADERS)

    for base_url in BASE_URLS:
        try:
            response = session.get(base_url + filename, timeout=30)
            if response.status_code != 200 or not response.content.startswith(b"PK"):
                continue
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                csv_names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
                if not csv_names:
                    continue
                with archive.open(csv_names[0]) as stream:
                    frame = pd.read_csv(stream)
            if not frame.empty:
                return frame
        except Exception as exc:
            print(f"Cash bhavcopy attempt failed for {check_date}: {exc}")
    return None


def normalize_cash_bhavcopy(raw, expected_date):
    required = ["TradDt", "TckrSymb", "SctySrs", "OpnPric", "HghPric", "LwPric", "ClsPric"]
    missing = [c for c in required if c not in raw.columns]
    if missing:
        raise RuntimeError(
            "NSE CM UDiFF schema differs; missing columns: "
            + ", ".join(missing)
            + ". No cash OHLC rows were written."
        )

    df = raw.loc[raw["SctySrs"].astype(str).str.strip().eq("EQ")].copy()
    df["TradDt"] = pd.to_datetime(df["TradDt"], errors="coerce")
    for col in ["OpnPric", "HghPric", "LwPric", "ClsPric"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["TradDt", "TckrSymb", "OpnPric", "HghPric", "LwPric", "ClsPric"])
    df = df[df["TradDt"].dt.date.eq(expected_date)].copy()
    df = df.rename(columns={
        "TradDt": "Trade_Date",
        "TckrSymb": "Symbol",
        "OpnPric": "Open_Price",
        "HghPric": "High_Price",
        "LwPric": "Low_Price",
        "ClsPric": "Close_Price",
    })
    df["Trade_Date"] = df["Trade_Date"].dt.strftime("%Y-%m-%d")
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    df = df[OUTPUT_COLUMNS].drop_duplicates(["Trade_Date", "Symbol"], keep="last")
    return df


def existing_trading_dates():
    dates = []
    for path in CASH_DIR.glob("cash_ohlc_*.csv"):
        try:
            dates.append(datetime.strptime(path.stem.replace("cash_ohlc_", ""), "%Y-%m-%d").date())
        except ValueError:
            continue
    return sorted(set(dates), reverse=True)


def save_one_day(check_date):
    target = CASH_DIR / f"cash_ohlc_{check_date:%Y-%m-%d}.csv"
    if target.exists():
        return True
    raw = download_cash_bhavcopy(check_date)
    if raw is None:
        return False
    daily = normalize_cash_bhavcopy(raw, check_date)
    if daily.empty:
        return False
    daily.to_csv(target, index=False)
    print(f"Saved cash OHLC: {target.name} ({len(daily)} EQ symbols)")
    return True


def main():
    print("AURORA CASH OHLC HISTORY — ADR-14 input (not backtesting)")
    # First find the latest cash session published by NSE.
    latest_found = False
    for offset in range(0, 11):
        check_date = TODAY - timedelta(days=offset)
        if save_one_day(check_date):
            latest_found = True
            break
    if not latest_found:
        print("No new cash bhavcopy found; retaining existing history.")

    # ADR-14 for the latest futures report date needs 14 daily ranges
    # plus the preceding close used to calculate the first range percentage.
    futures_file = DATA_DIR / "front_month_snapshot.csv"
    if futures_file.exists():
        futures = pd.read_csv(futures_file)
        futures["Trade_Date"] = pd.to_datetime(futures["Trade_Date"], errors="coerce")
        report_date = futures["Trade_Date"].max()
        if pd.isna(report_date):
            report_date = pd.Timestamp(TODAY)
        report_date = report_date.date()
    else:
        report_date = TODAY

    known = {d for d in existing_trading_dates() if d <= report_date}
    print(f"ADR-14 warm-up through latest futures date {report_date}: {len(known)}/15 cash sessions.")
    for offset in range(1, 91):
        if len(known) >= 15:
            break
        check_date = report_date - timedelta(days=offset)
        if check_date.weekday() >= 5 or check_date in known:
            continue
        if save_one_day(check_date):
            known.add(check_date)
    print(f"Cash sessions available on/before report date: {len({d for d in existing_trading_dates() if d <= report_date})}")
    if len(known) < 15:
        print("WARNING: ADR-14 remains unavailable until 15 valid cash sessions exist on/before the futures report date.")
    else:
        print("ADR-14 cash-market warm-up is ready for the latest futures report date.")


if __name__ == "__main__":
    main()
