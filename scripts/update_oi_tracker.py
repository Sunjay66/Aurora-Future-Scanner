# ============================================================
# AURORA FUTURES OI TRACKER
# NSE UDiFF Bhavcopy downloader + front-month stock futures
# Timezone: India Standard Time
# ============================================================

from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import zipfile
import io
import sys

# ------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")
NOW_IST = datetime.now(IST)
TODAY_IST = NOW_IST.date()

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_FILE = DATA_DIR / "front_month_snapshot.csv"

BASE_URLS = [
    "https://nsearchives.nseindia.com/content/fo/",
    "https://archives.nseindia.com/content/fo/",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}

# ------------------------------------------------------------
# DOWNLOAD LATEST AVAILABLE BHAVCOPY
# ------------------------------------------------------------

def download_bhavcopy():

    session = requests.Session()
    session.headers.update(HEADERS)

    # Try today and up to 10 previous calendar days.
    for days_back in range(0, 11):

        check_date = TODAY_IST - timedelta(days=days_back)
        date_string = check_date.strftime("%Y%m%d")

        filename = (
            f"BhavCopy_NSE_FO_0_0_0_{date_string}_F_0000.csv.zip"
        )

        for base_url in BASE_URLS:

            url = base_url + filename

            try:
                print(f"Checking NSE Bhavcopy: {check_date}")

                response = session.get(
                    url,
                    timeout=30
                )

                if response.status_code != 200:
                    continue

                # Confirm response is a ZIP file
                if not response.content.startswith(b"PK"):
                    continue

                with zipfile.ZipFile(
                    io.BytesIO(response.content)
                ) as z:

                    csv_files = [
                        name for name in z.namelist()
                        if name.lower().endswith(".csv")
                    ]

                    if not csv_files:
                        continue

                    with z.open(csv_files[0]) as csv_file:
                        df = pd.read_csv(csv_file)

                if df.empty:
                    continue

                print("\nBhavcopy successfully downloaded.")
                print("Bhavcopy date:", check_date)
                print("Rows:", len(df))

                return df, check_date

            except Exception as e:
                print(f"Download attempt failed: {e}")

    raise RuntimeError(
        "No valid NSE F&O Bhavcopy found for today "
        "or the previous 10 calendar days."
    )


# ------------------------------------------------------------
# SELECT FRONT-MONTH STOCK FUTURES
# ------------------------------------------------------------

def select_front_month_stock_futures(df):

    required_columns = [
        "TradDt",
        "TckrSymb",
        "XpryDt",
        "FinInstrmTp",
        "ClsPric",
        "OpnIntrst",
        "TtlTradgVol",
    ]

    missing = [
        col for col in required_columns
        if col not in df.columns
    ]

    if missing:
        raise RuntimeError(
            f"Required Bhavcopy columns missing: {missing}"
        )

    # Stock futures only: STF
    df = df[
        df["FinInstrmTp"].astype(str).str.strip() == "STF"
    ].copy()

    # Convert dates
    df["TradDt"] = pd.to_datetime(
        df["TradDt"],
        errors="coerce"
    )

    df["XpryDt"] = pd.to_datetime(
        df["XpryDt"],
        errors="coerce"
    )

    # Remove invalid rows
    df = df.dropna(
        subset=[
            "TradDt",
            "TckrSymb",
            "XpryDt",
            "ClsPric",
            "OpnIntrst",
        ]
    )

    # Exclude expired contracts
    df = df[
        df["XpryDt"].dt.date >= df["TradDt"].dt.date
    ].copy()

    # Sort by symbol and expiry
    df = df.sort_values(
        ["TckrSymb", "XpryDt"]
    )

    # Select nearest expiry for each stock
    front = df.groupby(
        "TckrSymb",
        as_index=False
    ).first()

    # Keep useful columns
    output_columns = [
        "TradDt",
        "TckrSymb",
        "XpryDt",
        "ClsPric",
        "OpnIntrst",
        "TtlTradgVol",
    ]

    front = front[output_columns].copy()

    front = front.rename(
        columns={
            "TradDt": "Trade_Date",
            "TckrSymb": "Symbol",
            "XpryDt": "Expiry_Date",
            "ClsPric": "Close_Price",
            "OpnIntrst": "Open_Interest",
            "TtlTradgVol": "Volume",
        }
    )

    front["Trade_Date"] = (
        pd.to_datetime(front["Trade_Date"])
        .dt.strftime("%Y-%m-%d")
    )

    front["Expiry_Date"] = (
        pd.to_datetime(front["Expiry_Date"])
        .dt.strftime("%Y-%m-%d")
    )

    return front


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():

    print("=" * 50)
    print("AURORA FUTURES OI TRACKER")
    print("Current IST:", NOW_IST.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 50)

    df, bhavcopy_date = download_bhavcopy()

    front = select_front_month_stock_futures(df)

    if front.empty:
        raise RuntimeError(
            "No stock futures were selected from the Bhavcopy."
        )

    front["Bhavcopy_Date"] = bhavcopy_date.strftime("%Y-%m-%d")
    front["Refresh_Time_IST"] = NOW_IST.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    front.to_csv(
        OUTPUT_FILE,
        index=False
    )

    print("\nSUCCESS")
    print("Bhavcopy date:", bhavcopy_date)
    print("Selected stock futures:", len(front))
    print("Output file:", OUTPUT_FILE)

    print("\nSample records:")
    print(front.head(5).to_string(index=False))


if __name__ == "__main__":
    main()
