# ============================================================
# AURORA FUTURES OI TRACKER
# NSE UDiFF Bhavcopy + Front-month futures + Historical OI
# Timezone: India Standard Time
# ============================================================

from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import zipfile
import io

# ------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")
NOW_IST = datetime.now(IST)
TODAY_IST = NOW_IST.date()

DATA_DIR = Path("data")
HISTORY_DIR = DATA_DIR / "history"

DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_FILE = DATA_DIR / "front_month_snapshot.csv"
CLASSIFICATION_FILE = DATA_DIR / "oi_classification_history.csv"

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

                response = session.get(url, timeout=30)

                if response.status_code != 200:
                    continue

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

                print("Bhavcopy downloaded:", check_date)
                print("Rows:", len(df))

                return df, check_date

            except Exception as e:
                print(f"Download attempt failed: {e}")

    raise RuntimeError(
        "No valid NSE F&O Bhavcopy found for today "
        "or previous 10 calendar days."
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

    missing = [c for c in required_columns if c not in df.columns]

    if missing:
        raise RuntimeError(f"Missing Bhavcopy columns: {missing}")

    df = df[
        df["FinInstrmTp"].astype(str).str.strip() == "STF"
    ].copy()

    df["TradDt"] = pd.to_datetime(df["TradDt"], errors="coerce")
    df["XpryDt"] = pd.to_datetime(df["XpryDt"], errors="coerce")

    df["ClsPric"] = pd.to_numeric(df["ClsPric"], errors="coerce")
    df["OpnIntrst"] = pd.to_numeric(df["OpnIntrst"], errors="coerce")
    df["TtlTradgVol"] = pd.to_numeric(
        df["TtlTradgVol"], errors="coerce"
    )

    df = df.dropna(
        subset=[
            "TradDt", "TckrSymb", "XpryDt",
            "ClsPric", "OpnIntrst"
        ]
    )

    df = df[
        df["XpryDt"].dt.date >= df["TradDt"].dt.date
    ].copy()

    df = df.sort_values(["TckrSymb", "XpryDt"])

    front = df.groupby("TckrSymb", as_index=False).first()

    front = front[
        [
            "TradDt", "TckrSymb", "XpryDt",
            "ClsPric", "OpnIntrst", "TtlTradgVol"
        ]
    ].copy()

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

    front["Trade_Date"] = pd.to_datetime(
        front["Trade_Date"]
    ).dt.strftime("%Y-%m-%d")

    front["Expiry_Date"] = pd.to_datetime(
        front["Expiry_Date"]
    ).dt.strftime("%Y-%m-%d")

    return front


# ------------------------------------------------------------
# LOAD PREVIOUS SAME-MONTH SNAPSHOT
# ------------------------------------------------------------

def get_previous_snapshot(current_date):

    files = sorted(HISTORY_DIR.glob("snapshot_*.csv"))

    previous_files = []

    for file in files:

        try:
            file_date = datetime.strptime(
                file.stem.replace("snapshot_", ""),
                "%Y-%m-%d"
            ).date()

            if file_date < current_date and (
                file_date.year == current_date.year
                and file_date.month == current_date.month
            ):
                previous_files.append((file_date, file))

        except ValueError:
            continue

    if not previous_files:
        return None, None

    previous_date, previous_file = max(
        previous_files, key=lambda x: x[0]
    )

    previous = pd.read_csv(previous_file)

    print("Previous same-month snapshot:", previous_date)

    return previous, previous_date


# ------------------------------------------------------------
# CLASSIFY DAILY PRICE + OI MOVEMENT
# ------------------------------------------------------------

def classify_oi(current, previous, current_date):

    current = current.copy()
    previous = previous.copy()

    current["Expiry_Date"] = current["Expiry_Date"].astype(str)
    previous["Expiry_Date"] = previous["Expiry_Date"].astype(str)

    merged = current.merge(
        previous[
            [
                "Symbol", "Expiry_Date",
                "Close_Price", "Open_Interest"
            ]
        ],
        on=["Symbol", "Expiry_Date"],
        how="inner",
        suffixes=("", "_Previous")
    )

    if merged.empty:
        print("No matching symbol + expiry contracts.")
        return pd.DataFrame()

    merged["Price_Change_%"] = (
        (merged["Close_Price"] - merged["Close_Price_Previous"])
        / merged["Close_Price_Previous"].replace(0, pd.NA)
        * 100
    )

    merged["OI_Change_%"] = (
        (merged["Open_Interest"] - merged["Open_Interest_Previous"])
        / merged["Open_Interest_Previous"].replace(0, pd.NA)
        * 100
    )

    price_up = merged["Price_Change_%"] > 0
    price_down = merged["Price_Change_%"] < 0
    oi_up = merged["OI_Change_%"] > 0
    oi_down = merged["OI_Change_%"] < 0

    merged["Classification"] = "Neutral / Mixed"

    merged.loc[price_up & oi_up, "Classification"] = "Long Buildup"
    merged.loc[price_down & oi_up, "Classification"] = "Short Buildup"
    merged.loc[price_up & oi_down, "Classification"] = "Short Covering"
    merged.loc[price_down & oi_down, "Classification"] = "Long Unwinding"

    merged["Previous_Trade_Date"] = previous["Trade_Date"].iloc[0]
    merged["Trade_Date"] = current_date.strftime("%Y-%m-%d")

    merged["Refresh_Time_IST"] = NOW_IST.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    columns = [
        "Trade_Date",
        "Previous_Trade_Date",
        "Symbol",
        "Expiry_Date",
        "Close_Price",
        "Close_Price_Previous",
        "Price_Change_%",
        "Open_Interest",
        "Open_Interest_Previous",
        "OI_Change_%",
        "Volume",
        "Classification",
        "Refresh_Time_IST",
    ]

    return merged[columns].copy()


# ------------------------------------------------------------
# SAVE DAILY HISTORY AND CLASSIFICATION
# ------------------------------------------------------------

def save_daily_data(front, bhavcopy_date):

    date_string = bhavcopy_date.strftime("%Y-%m-%d")

    front["Bhavcopy_Date"] = date_string
    front["Refresh_Time_IST"] = NOW_IST.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    # Current/latest snapshot
    front.to_csv(OUTPUT_FILE, index=False)

    # Save permanent daily snapshot
    daily_snapshot = HISTORY_DIR / f"snapshot_{date_string}.csv"

    if not daily_snapshot.exists():
        front.to_csv(daily_snapshot, index=False)
        print("Saved daily snapshot:", daily_snapshot)
    else:
        print("Daily snapshot already exists:", daily_snapshot)

    # Avoid processing the same date twice
    if CLASSIFICATION_FILE.exists():

        history = pd.read_csv(CLASSIFICATION_FILE)

        if not history.empty and (
            history["Trade_Date"].astype(str) == date_string
        ).any():

            print("Classification already exists for this date.")
            print("Duplicate processing skipped.")
            return

    # Find previous snapshot from same calendar month
    previous, previous_date = get_previous_snapshot(bhavcopy_date)

    if previous is None:

        print(
            "No previous same-month trading snapshot available. "
            "Monthly reset / initial history: classification skipped."
        )

        return

    classification = classify_oi(
        front,
        previous,
        bhavcopy_date
    )

    if classification.empty:
        print("No classifications generated.")
        return

    if CLASSIFICATION_FILE.exists():

        old_history = pd.read_csv(CLASSIFICATION_FILE)

        combined = pd.concat(
            [old_history, classification],
            ignore_index=True
        )

    else:
        combined = classification

    combined = combined.drop_duplicates(
        subset=["Trade_Date", "Symbol", "Expiry_Date"],
        keep="last"
    )

    combined.to_csv(CLASSIFICATION_FILE, index=False)

    print("Classification history updated.")
    print("Matched contracts:", len(classification))

    print("\nClassification counts:")
    print(
        classification["Classification"]
        .value_counts()
        .to_string()
    )


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():

    print("=" * 55)
    print("AURORA FUTURES OI TRACKER")
    print("Current IST:", NOW_IST.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 55)

    df, bhavcopy_date = download_bhavcopy()

    front = select_front_month_stock_futures(df)

    if front.empty:
        raise RuntimeError("No stock futures selected.")

    print("\nBhavcopy date:", bhavcopy_date)
    print("Selected stock futures:", len(front))

    save_daily_data(front, bhavcopy_date)

    print("\nSUCCESS")
    print("Latest snapshot:", OUTPUT_FILE)
    print("Classification history:", CLASSIFICATION_FILE)


if __name__ == "__main__":
    main()
