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

UDIFF_BASE_URLS = [
    "https://nsearchives.nseindia.com/content/fo/",
    "https://archives.nseindia.com/content/fo/",
]
# NSE's newer public report distribution uses the /content/fo/ directory.
# Keep the older archive locations as fallbacks for historical availability.
DAT_BASE_URLS = [
    "https://nsearchives.nseindia.com/content/fo/",
    "https://archives.nseindia.com/content/fo/",
    "https://nsearchives.nseindia.com/archives/nsccl/fao/",
    "https://archives.nseindia.com/archives/nsccl/fao/",
]
MIN_STOCK_SYMBOLS = 200

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}

# ------------------------------------------------------------
# DOWNLOAD + NORMALIZE NSE F&O BHAVCOPY (UDiFF ZIP OR DAT)
# ------------------------------------------------------------

CANONICAL_COLUMNS = [
    "Trade_Date", "Symbol", "Expiry_Date", "Previous_Close_Price",
    "Open_Price", "High_Price", "Low_Price", "Close_Price",
    "Open_Interest", "Change_In_Open_Interest", "Volume", "Turnover",
]


def validate_futures_frame(frame, expected_date, source_name):
    """Validate date, stock coverage and core fields.
    Deferred contracts with no trades may have zero OHLC; OHLC is checked
    strictly only after selecting each stock's nearest unexpired contract.
    """
    if frame is None or frame.empty:
        raise ValueError(f"{source_name}: empty report")
    missing = [c for c in CANONICAL_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"{source_name}: missing fields {missing}")

    frame = frame[CANONICAL_COLUMNS].copy()
    frame["Trade_Date"] = pd.to_datetime(frame["Trade_Date"], errors="coerce")
    frame["Expiry_Date"] = pd.to_datetime(frame["Expiry_Date"], errors="coerce")
    frame["Symbol"] = frame["Symbol"].astype(str).str.strip()
    numeric = [
        "Previous_Close_Price", "Open_Price", "High_Price", "Low_Price",
        "Close_Price", "Open_Interest", "Change_In_Open_Interest",
        "Volume", "Turnover",
    ]
    for col in numeric:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    frame = frame.dropna(subset=[
        "Trade_Date", "Expiry_Date", "Symbol", "Open_Interest", "Volume"
    ])
    frame = frame[frame["Symbol"].ne("")].copy()

    actual_dates = set(frame["Trade_Date"].dt.date)
    if actual_dates != {expected_date}:
        raise ValueError(
            f"{source_name}: expected {expected_date}, found "
            f"{sorted(str(d) for d in actual_dates)}"
        )

    frame = frame[frame["Expiry_Date"].dt.date >= expected_date].copy()
    if (frame["Open_Interest"] < 0).any() or (frame["Volume"] < 0).any():
        raise ValueError(f"{source_name}: negative OI or volume")
    if frame.duplicated(["Symbol", "Expiry_Date"]).any():
        raise ValueError(f"{source_name}: duplicate symbol/expiry contracts")
    symbols = frame["Symbol"].nunique()
    if symbols < MIN_STOCK_SYMBOLS:
        raise ValueError(
            f"{source_name}: only {symbols} stock symbols; minimum "
            f"accepted is {MIN_STOCK_SYMBOLS}"
        )
    return frame.sort_values(["Symbol", "Expiry_Date"]).reset_index(drop=True)


def parse_udiff_zip(payload, expected_date):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        csv_names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError("UDiFF ZIP contains no CSV")
        with archive.open(csv_names[0]) as stream:
            raw = pd.read_csv(stream)

    required = [
        "TradDt", "TckrSymb", "XpryDt", "FinInstrmTp", "PrvsClsgPric",
        "OpnPric", "HghPric", "LwPric", "ClsPric", "OpnIntrst",
        "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal",
    ]
    missing = [c for c in required if c not in raw.columns]
    if missing:
        raise ValueError(f"UDiFF schema missing fields: {missing}")
    raw = raw[raw["FinInstrmTp"].astype(str).str.strip().eq("STF")].copy()

    frame = pd.DataFrame({
        "Trade_Date": raw["TradDt"],
        "Symbol": raw["TckrSymb"],
        "Expiry_Date": raw["XpryDt"],
        "Previous_Close_Price": raw["PrvsClsgPric"],
        "Open_Price": raw["OpnPric"],
        "High_Price": raw["HghPric"],
        "Low_Price": raw["LwPric"],
        "Close_Price": raw["ClsPric"],
        "Open_Interest": raw["OpnIntrst"],
        "Change_In_Open_Interest": raw["ChngInOpnIntrst"],
        "Volume": raw["TtlTradgVol"],
        "Turnover": raw["TtlTrfVal"],
    })
    return validate_futures_frame(frame, expected_date, "UDiFF ZIP")


def parse_dat(payload, expected_date):
    """Parse the 26-column, headerless NSE FNO_BC DDMMYYYY DAT layout.

    Verified 1-based fields: instrument=2, symbol=3, expiry=4,
    previous close=10, open=11, high=12, low=13, close=14,
    contracts=16, turnover=17, OI=18, change in OI=19, trade date=22.
    """
    raw = pd.read_csv(
        io.BytesIO(payload), header=None, dtype=str,
        keep_default_na=False, skipinitialspace=False
    )
    if raw.shape[1] != 26:
        raise ValueError(
            f"F&O DAT schema changed: expected 26 columns, found {raw.shape[1]}"
        )
    raw = raw[raw.iloc[:, 1].astype(str).str.strip().eq("FUTSTK")].copy()
    if raw.empty:
        raise ValueError("F&O DAT contains no FUTSTK records")

    frame = pd.DataFrame({
        "Trade_Date": raw.iloc[:, 21].str.strip(),
        "Symbol": raw.iloc[:, 2].str.strip(),
        "Expiry_Date": raw.iloc[:, 3].str.strip(),
        "Previous_Close_Price": raw.iloc[:, 9].str.strip(),
        "Open_Price": raw.iloc[:, 10].str.strip(),
        "High_Price": raw.iloc[:, 11].str.strip(),
        "Low_Price": raw.iloc[:, 12].str.strip(),
        "Close_Price": raw.iloc[:, 13].str.strip(),
        "Open_Interest": raw.iloc[:, 17].str.strip(),
        "Change_In_Open_Interest": raw.iloc[:, 18].str.strip(),
        "Volume": raw.iloc[:, 15].str.strip(),
        "Turnover": raw.iloc[:, 16].str.strip(),
    })
    frame["Trade_Date"] = pd.to_datetime(
        frame["Trade_Date"], format="%d/%b/%Y", errors="coerce"
    )
    frame["Expiry_Date"] = pd.to_datetime(
        frame["Expiry_Date"], format="%d%m%Y", errors="coerce"
    )
    return validate_futures_frame(frame, expected_date, "F&O DAT")


def download_bhavcopy():
    """
    Search trading dates newest-first. For each date, try the NSE DAT report
    first and UDiFF ZIP second. A candidate is accepted only after full schema,
    exact trade-date, stock coverage and field validation.
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    errors = []

    for days_back in range(0, 11):
        check_date = TODAY_IST - timedelta(days=days_back)
        yyyymmdd = check_date.strftime("%Y%m%d")
        ddmmyyyy = check_date.strftime("%d%m%Y")

        # DAT is deliberately attempted first because it is now a published
        # NSE F&O bhavcopy source and contains all fields required by Aurora.
        candidates = [
            ("F&O DAT", f"FNO_BC{ddmmyyyy}.DAT", DAT_BASE_URLS, parse_dat),
            ("UDiFF ZIP", f"BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip",
             UDIFF_BASE_URLS, parse_udiff_zip),
        ]

        for source_name, filename, base_urls, parser in candidates:
            for base_url in base_urls:
                url = base_url + filename
                try:
                    print(f"Checking {source_name}: {url}")
                    response = session.get(url, timeout=40)

                    if response.status_code != 200:
                        print(
                            f"Unavailable: {source_name} {check_date} "
                            f"returned HTTP {response.status_code}"
                        )
                        continue

                    if len(response.content) < 1000:
                        message = (
                            f"{source_name} {check_date}: response too small "
                            f"({len(response.content)} bytes)"
                        )
                        errors.append(message)
                        print("Rejected candidate:", message)
                        continue

                    if source_name == "UDiFF ZIP" and not response.content.startswith(b"PK"):
                        message = f"UDiFF ZIP {check_date}: response is not a ZIP archive"
                        errors.append(message)
                        print("Rejected candidate:", message)
                        continue

                    parsed = parser(response.content, check_date)
                    print(
                        f"ACCEPTED {source_name} for {check_date}: "
                        f"{len(parsed)} FUTSTK contracts / "
                        f"{parsed['Symbol'].nunique()} symbols"
                    )
                    print(f"Selected source: {source_name}")
                    return parsed, check_date, source_name

                except Exception as exc:
                    message = f"{source_name} {check_date} via {base_url}: {exc}"
                    errors.append(message)
                    print("Rejected candidate:", message)

    raise RuntimeError(
        "No valid NSE F&O DAT or UDiFF ZIP report found for today or "
        "the previous 10 calendar days. Latest candidate errors:\n"
        + "\n".join(errors[-16:])
    )

# ------------------------------------------------------------
# SELECT NEAREST UNEXPIRED STOCK FUTURES
# ------------------------------------------------------------

def select_front_month_stock_futures(df):
    required = [
        "Trade_Date", "Symbol", "Expiry_Date", "Close_Price",
        "Open_Interest", "Volume", "Open_Price", "High_Price", "Low_Price",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"Normalized F&O report missing fields: {missing}")

    selected = df.copy()
    selected["Trade_Date"] = pd.to_datetime(selected["Trade_Date"], errors="coerce")
    selected["Expiry_Date"] = pd.to_datetime(selected["Expiry_Date"], errors="coerce")
    selected["Symbol"] = selected["Symbol"].astype(str).str.strip()
    selected = selected.dropna(subset=["Trade_Date", "Expiry_Date", "Symbol"])
    selected = selected.sort_values(["Symbol", "Expiry_Date"])
    front = selected.groupby("Symbol", as_index=False).first()

    if front["Symbol"].nunique() < MIN_STOCK_SYMBOLS:
        raise RuntimeError(
            f"Front-month selection returned only {front['Symbol'].nunique()} symbols"
        )

    required_front = [
        "Open_Price", "High_Price", "Low_Price", "Close_Price",
        "Open_Interest", "Volume",
    ]
    if front[required_front].isna().any().any():
        raise RuntimeError("Front-month snapshot contains missing required values")
    bad_ohlc = (
        (front["Open_Price"] <= 0)
        | (front["High_Price"] <= 0)
        | (front["Low_Price"] <= 0)
        | (front["Close_Price"] <= 0)
        | (front["High_Price"] < front[["Open_Price", "Close_Price", "Low_Price"]].max(axis=1))
        | (front["Low_Price"] > front[["Open_Price", "Close_Price", "High_Price"]].min(axis=1))
    )
    if bad_ohlc.any():
        bad_symbols = front.loc[bad_ohlc, "Symbol"].astype(str).tolist()
        raise RuntimeError("Invalid front-month OHLC for: " + ", ".join(bad_symbols[:20]))

    for column in ["Trade_Date", "Expiry_Date"]:
        front[column] = pd.to_datetime(front[column]).dt.strftime("%Y-%m-%d")
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

def save_daily_data(front, bhavcopy_date, source_name):

    date_string = bhavcopy_date.strftime("%Y-%m-%d")

    front["Bhavcopy_Date"] = date_string
    front["Bhavcopy_Source"] = source_name
    front["Refresh_Time_IST"] = NOW_IST.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    # Current/latest snapshot
    front.to_csv(OUTPUT_FILE, index=False)

    # Save permanent daily snapshot
    daily_snapshot = HISTORY_DIR / f"snapshot_{date_string}.csv"

    # Replace same-date files too, so a corrected NSE republication is used.
    front.to_csv(daily_snapshot, index=False)
    print("Saved / refreshed daily snapshot:", daily_snapshot)

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

    df, bhavcopy_date, source_name = download_bhavcopy()

    front = select_front_month_stock_futures(df)

    if front.empty:
        raise RuntimeError("No stock futures selected.")

    print("\nBhavcopy date:", bhavcopy_date)
    print("Bhavcopy source:", source_name)
    print("Selected stock futures:", len(front))
    print("OHLC, OI, change in OI and volume fields validated.")

    save_daily_data(front, bhavcopy_date, source_name)

    print("\nSUCCESS")
    print("Latest snapshot:", OUTPUT_FILE)
    print("Classification history:", CLASSIFICATION_FILE)


if __name__ == "__main__":
    main()
