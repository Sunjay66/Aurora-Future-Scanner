"""
Download the latest complete set of NSE participant derivatives reports and
update the audited institutional CSV history.

The script only writes when OI, volume and FII XLS reports are all available
for the same date and pass basic structural validation. It replaces that
date's rows, so manual reruns are safe and idempotent.
"""
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo
import sys

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "institutional_data"
OUT.mkdir(parents=True, exist_ok=True)

OI_FILE = OUT / "derivatives_oi.csv"
VOLUME_FILE = OUT / "derivatives_volume.csv"
FII_FILE = OUT / "fii_derivatives_stats.csv"

BASE_URLS = [
    "https://nsearchives.nseindia.com/content/fo/",
    "https://archives.nseindia.com/content/fo/",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}
PARTICIPANTS = ["FII", "DII", "Client", "Pro"]
SEGMENTS = [
    ("Index Futures", "Future Index Long", "Future Index Short"),
    ("Stock Futures", "Future Stock Long", "Future Stock Short"),
]
OI_COLUMNS = [
    "Trade_Date", "Session_Type", "Participant", "Segment",
    "Long_OI_Contracts", "Short_OI_Contracts", "Net_OI_Contracts",
    "Previous_Net_OI_Contracts", "Change_Net_OI_Contracts", "Source",
]
VOLUME_COLUMNS = [
    "Trade_Date", "Session_Type", "Participant", "Segment",
    "Buy_Contracts", "Sell_Contracts", "Total_Volume_Contracts",
    "Net_Buy_Sell_Contracts", "Source",
]
FII_COLUMNS = [
    "Trade_Date", "Session_Type", "Segment", "Buy_Contracts",
    "Buy_Value_Cr", "Sell_Contracts", "Sell_Value_Cr",
    "Net_Buy_Sell_Contracts", "Net_Buy_Sell_Value_Cr",
    "EOD_OI_Contracts", "EOD_OI_Value_Cr", "Source",
]
FII_SEGMENTS = {
    "INDEX FUTURES": "Index Futures",
    "INDEX OPTIONS": "Index Options",
    "STOCK FUTURES": "Stock Futures",
    "STOCK OPTIONS": "Stock Options",
}


def download(session, filename):
    for base in BASE_URLS:
        try:
            response = session.get(base + filename, timeout=30)
            if response.status_code == 200 and response.content:
                return response.content
        except requests.RequestException as exc:
            print(f"Download failed for {filename}: {exc}")
    return None


def read_participant_csv(content):
    frame = pd.read_csv(BytesIO(content), header=1)
    frame.columns = [str(column).strip() for column in frame.columns]
    required = ["Client Type"] + [
        col for _, long_col, short_col in SEGMENTS
        for col in (long_col, short_col)
    ]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"Participant report missing columns: {missing}")
    frame["Client Type"] = frame["Client Type"].astype(str).str.strip()
    frame = frame[frame["Client Type"].isin(PARTICIPANTS)].copy()
    if set(frame["Client Type"]) != set(PARTICIPANTS) or len(frame) != 4:
        raise ValueError("Participant report does not contain exactly FII, DII, Client and Pro.")
    return frame


def participant_rows(frame, trade_date, is_oi):
    rows = []
    for _, source_row in frame.iterrows():
        participant = source_row["Client Type"]
        for segment, long_col, short_col in SEGMENTS:
            long_value = int(pd.to_numeric(source_row[long_col], errors="raise"))
            short_value = int(pd.to_numeric(source_row[short_col], errors="raise"))
            if is_oi:
                rows.append({
                    "Trade_Date": trade_date, "Session_Type": "Regular",
                    "Participant": participant, "Segment": segment,
                    "Long_OI_Contracts": long_value,
                    "Short_OI_Contracts": short_value,
                    "Net_OI_Contracts": long_value - short_value,
                    "Previous_Net_OI_Contracts": "",
                    "Change_Net_OI_Contracts": "", "Source": "NSE",
                })
            else:
                rows.append({
                    "Trade_Date": trade_date, "Session_Type": "Regular",
                    "Participant": participant, "Segment": segment,
                    "Buy_Contracts": long_value, "Sell_Contracts": short_value,
                    "Total_Volume_Contracts": long_value + short_value,
                    "Net_Buy_Sell_Contracts": long_value - short_value,
                    "Source": "NSE",
                })
    return rows


def fii_rows(content, trade_date):
    # NSE XLS: the first column contains the aggregate segment names;
    # the following six columns are buy contracts/value, sell contracts/value,
    # and end-of-day OI contracts/value.
    frame = pd.read_excel(BytesIO(content), header=None, engine="xlrd")
    rows = []
    for _, source_row in frame.iterrows():
        label = str(source_row.iloc[0]).strip().upper()
        if label not in FII_SEGMENTS:
            continue
        values = [
            pd.to_numeric(source_row.iloc[index], errors="coerce")
            for index in range(1, 7)
        ]
        if any(pd.isna(value) for value in values):
            raise ValueError(f"Incomplete FII statistics row: {label}")
        buy, buy_value, sell, sell_value, eod_oi, eod_value = values
        rows.append({
            "Trade_Date": trade_date, "Session_Type": "Regular",
            "Segment": FII_SEGMENTS[label],
            "Buy_Contracts": int(buy), "Buy_Value_Cr": f"{float(buy_value):.2f}",
            "Sell_Contracts": int(sell), "Sell_Value_Cr": f"{float(sell_value):.2f}",
            "Net_Buy_Sell_Contracts": int(buy - sell),
            "Net_Buy_Sell_Value_Cr": f"{float(buy_value - sell_value):.2f}",
            "EOD_OI_Contracts": int(eod_oi),
            "EOD_OI_Value_Cr": f"{float(eod_value):.2f}", "Source": "NSE",
        })
    if len(rows) != 4:
        raise ValueError(f"Expected 4 aggregate FII segments; found {len(rows)}.")
    return rows


def replace_date_rows(path, incoming_rows, columns):
    incoming = pd.DataFrame(incoming_rows).fillna("")
    incoming = incoming[columns]
    trade_date = incoming["Trade_Date"].iloc[0]
    if path.exists():
        old = pd.read_csv(path, dtype=str).fillna("")
        for column in columns:
            if column not in old.columns:
                old[column] = ""
        old = old[columns]
        old = old[old["Trade_Date"].astype(str) != trade_date]
    else:
        old = pd.DataFrame(columns=columns)
    combined = pd.concat([old, incoming], ignore_index=True)
    combined = combined.sort_values("Trade_Date", ascending=False, kind="stable")
    combined.to_csv(path, index=False)


def main():
    session = requests.Session()
    session.headers.update(HEADERS)
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()

    for days_back in range(0, 11):
        report_date = today - timedelta(days=days_back)
        compact = report_date.strftime("%d%m%Y")
        fii_date = report_date.strftime("%d-%b-%Y")
        trade_date = report_date.strftime("%Y-%m-%d")

        oi_bytes = download(session, f"fao_participant_oi_{compact}.csv")
        volume_bytes = download(session, f"fao_participant_vol_{compact}.csv")
        fii_bytes = download(session, f"fii_stats_{fii_date}.xls")

        if not all([oi_bytes, volume_bytes, fii_bytes]):
            print(f"No complete NSE report set for {trade_date}; checking previous date.")
            continue

        try:
            oi_source = read_participant_csv(oi_bytes)
            volume_source = read_participant_csv(volume_bytes)
            oi_data = participant_rows(oi_source, trade_date, True)
            volume_data = participant_rows(volume_source, trade_date, False)
            fii_data = fii_rows(fii_bytes, trade_date)

            if len(oi_data) != 8 or len(volume_data) != 8 or len(fii_data) != 4:
                raise ValueError("Unexpected report row count; refusing to update.")

            # All three reports are parsed and validated before any CSV is changed.
            replace_date_rows(OI_FILE, oi_data, OI_COLUMNS)
            replace_date_rows(VOLUME_FILE, volume_data, VOLUME_COLUMNS)
            replace_date_rows(FII_FILE, fii_data, FII_COLUMNS)
            print(f"SUCCESS: institutional derivatives updated through {trade_date}.")
            return 0
        except Exception as exc:
            print(f"Rejected NSE report set for {trade_date}: {exc}")

    print("No complete, valid three-report set found in the last 11 calendar days.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
