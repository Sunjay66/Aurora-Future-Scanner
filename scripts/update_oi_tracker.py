from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import requests
import zipfile
import io
import json

# ============================================================
# AURORA FUTURES OI TRACKER
# Initial NSE Bhavcopy download and front-month selection
# ============================================================

IST = ZoneInfo("Asia/Kolkata")
TODAY_IST = datetime.now(IST).date()

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

NSE_URL = (
    "https://nsearchives.nseindia.com/content/fo/"
    f"BhavCopy_NSE_FO_0_0_0_{TODAY_IST.strftime('%Y%m%d')}_F_0000.csv.zip"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}

def download_bhavcopy(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        csv_name = next(
            name for name in z.namelist()
            if name.lower().endswith(".csv")
        )
        with z.open(csv_name) as f:
            df = pd.read_csv(f)

    return df

def select_front_month_stock_futures(df):
    required = [
        "TradDt", "TckrSymb", "XpryDt",
        "ClsPric", "OpnIntrst", "TtlTradgVol",
    ]

    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"Bhavcopy columns missing: {missing}")

    df = df.copy()

    # NSE stock futures instrument type
    if "FinInstrmTp" in df.columns:
        df = df[df["FinInstrmTp"] == "STF"].copy()
    elif "FinInstrmNm" in df.columns:
        df = df[df["FinInstrmNm"].astype(str).str.contains(
            "FUTSTK", case=False, na=False
        )].copy()
    else:
        raise ValueError("Cannot identify stock futures instrument type.")

    df["XpryDt"] = pd.to_datetime(df["XpryDt"], errors="coerce")
    df["TradDt"] = pd.to_datetime(df["TradDt"], errors="coerce")

    df = df.dropna(subset=["TckrSymb", "XpryDt", "TradDt"])

    # Select nearest expiry for each stock
    df = df.sort_values(["TckrSymb", "XpryDt"])
    front = df.groupby("TckrSymb", as_index=False).first()

    return front

def main():
    print("Aurora Futures OI Tracker")
    print("IST date:", datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"))
    print("Downloading:", NSE_URL)

    df = download_bhavcopy(NSE_URL)
    front = select_front_month_stock_futures(df)

    output = DATA_DIR / "front_month_snapshot.csv"
    front.to_csv(output, index=False)

    print("Bhavcopy rows:", len(df))
    print("Front-month stock futures:", len(front))
    print("Saved:", output)

if __name__ == "__main__":
    main()
