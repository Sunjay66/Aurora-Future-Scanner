"""Download and maintain NSE security-wise deliverable quantity history.

The rolling averages are calculated by the dashboard from earlier regular
sessions only. This file stores raw daily observations; Muhurat is retained
but tagged separately so it can be excluded from regular-session averages.
"""
from pathlib import Path
from datetime import datetime, timedelta
from io import StringIO
import csv
import re
import requests
import pandas as pd

IST_TODAY = datetime.now().date()
DATA_DIR = Path("data")
SNAPSHOT_DIR = DATA_DIR / "history"
OUTPUT = DATA_DIR / "delivery_history.csv"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}
# Special NSE sessions are retained but excluded from normal-session averages.
MUHURAT_DATES = {"2026-11-08"}
COLUMNS = ["Trade_Date", "Session_Type", "Symbol", "Quantity_Traded", "Deliverable_Quantity", "Delivery_Pct"]


def latest_snapshot_date():
    dates = []
    for path in SNAPSHOT_DIR.glob("snapshot_*.csv"):
        match = re.search(r"snapshot_(\d{4}-\d{2}-\d{2})", path.name)
        if match:
            dates.append(match.group(1))
    if not dates:
        raise RuntimeError("No dated OI snapshots found; cannot determine delivery-report date range.")
    return max(dates)


def fetch_report(session, day):
    ddmmyyyy = day.strftime("%d%m%Y")
    filename = f"MTO_{ddmmyyyy}.DAT"
    urls = [
        f"https://nsearchives.nseindia.com/content/equities/{filename}",
        f"https://archives.nseindia.com/content/equities/{filename}",
    ]
    for url in urls:
        try:
            response = session.get(url, timeout=25)
            if response.status_code == 200 and response.content:
                text = response.content.decode("utf-8", errors="replace")
                rows = list(csv.reader(StringIO(text)))
                records = []
                for row in rows:
                    if len(row) < 7 or row[0].strip() != "20" or row[3].strip().upper() != "EQ":
                        continue
                    try:
                        traded = int(float(row[4]))
                        deliverable = int(float(row[5]))
                        pct = float(row[6])
                    except (ValueError, IndexError):
                        continue
                    if not row[2].strip() or traded <= 0 or deliverable < 0 or deliverable > traded:
                        continue
                    calculated = deliverable / traded * 100
                    if abs(calculated - pct) > 0.02:
                        continue
                    records.append({
                        "Trade_Date": day.isoformat(),
                        "Session_Type": "Muhurat" if day.isoformat() in MUHURAT_DATES else "Regular",
                        "Symbol": row[2].strip(),
                        "Quantity_Traded": traded,
                        "Deliverable_Quantity": deliverable,
                        "Delivery_Pct": round(calculated, 4),
                    })
                if records:
                    return records
        except requests.RequestException as exc:
            print(f"Download failed {url}: {exc}")
    return []


def main():
    latest = datetime.strptime(latest_snapshot_date(), "%Y-%m-%d").date()
    start = latest - timedelta(days=14)
    session = requests.Session()
    session.headers.update(HEADERS)
    existing = pd.read_csv(OUTPUT, dtype={"Trade_Date": str, "Symbol": str}) if OUTPUT.exists() else pd.DataFrame(columns=COLUMNS)
    downloaded = []
    day = start
    while day <= latest:
        # Only look for sessions represented in the most recent 15 calendar days.
        records = fetch_report(session, day)
        if records:
            downloaded.extend(records)
            print(f"Delivery report {day}: {len(records)} EQ securities")
        day += timedelta(days=1)

    if downloaded:
        fresh = pd.DataFrame(downloaded, columns=COLUMNS)
        combined = pd.concat([existing, fresh], ignore_index=True)
        combined = combined.drop_duplicates(["Trade_Date", "Symbol"], keep="last")
        combined = combined.sort_values(["Trade_Date", "Symbol"])
        combined.to_csv(OUTPUT, index=False)
    elif not OUTPUT.exists():
        pd.DataFrame(columns=COLUMNS).to_csv(OUTPUT, index=False)

    final = pd.read_csv(OUTPUT) if OUTPUT.exists() else pd.DataFrame()
    if not final.empty:
        print("Delivery history updated through", final["Trade_Date"].max(), "rows:", len(final))
    else:
        print("No MTO reports downloaded; existing delivery history retained.")


if __name__ == "__main__":
    main()
