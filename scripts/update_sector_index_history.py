# ============================================================
# AURORA SECTOR INDEX HISTORY UPDATER
# Daily NSE all-indices archive; backfills recent missing sessions.
# ============================================================
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import io
import time
import requests
import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
TODAY = datetime.now(IST).date()
DATA = Path("data")
MAP_FILE = DATA / "stock_sector_proxy_mapping_clean.csv"
HISTORY_FILE = DATA / "sector_index_history_clean.csv"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept": "text/csv,*/*",
    "Referer": "https://www.nseindia.com/",
}
COLS = ["Index_Name", "Trade_Date", "Closing_Index_Value"]


def load_mapping():
    if not MAP_FILE.exists():
        raise FileNotFoundError(f"Missing {MAP_FILE}")
    mapping = pd.read_csv(MAP_FILE)
    required = {"Symbol", "Industry", "Mapping_Status", "Sector_Index_Proxy", "Proxy_Type", "Proxy_Available"}
    if not required.issubset(mapping.columns):
        raise ValueError(f"Sector mapping missing columns: {sorted(required - set(mapping.columns))}")
    mapping["Proxy_Available"] = mapping["Proxy_Available"].astype(str).str.strip().str.lower().isin(["true", "1", "yes"])
    return mapping


def download_day(day):
    url = f"https://archives.nseindia.com/content/indices/ind_close_all_{day:%d%m%Y}.csv"
    try:
        response = requests.get(url, headers=HEADERS, timeout=35)
        if response.status_code != 200 or not response.content:
            return None
        raw = pd.read_csv(io.BytesIO(response.content))
        raw.columns = [str(c).strip() for c in raw.columns]
        name_col = next((c for c in raw.columns if c.lower() == "index name"), None)
        close_col = next((c for c in raw.columns if c.lower() in {"closing", "closing index value"}), None)
        date_col = next((c for c in raw.columns if c.lower() in {"index date", "index_date", "date"}), None)
        if not name_col or not close_col:
            raise ValueError(f"Unexpected NSE index CSV columns: {raw.columns.tolist()}")
        out = pd.DataFrame({
            "Index_Name": raw[name_col].astype(str).str.strip(),
            "Closing_Index_Value": pd.to_numeric(raw[close_col], errors="coerce"),
        })
        if date_col:
            parsed = pd.to_datetime(raw[date_col], dayfirst=True, errors="coerce")
            out["Trade_Date"] = parsed.dt.strftime("%Y-%m-%d")
        else:
            out["Trade_Date"] = day.strftime("%Y-%m-%d")
        out = out.dropna(subset=["Closing_Index_Value", "Trade_Date"])
        out = out[out["Trade_Date"] == day.strftime("%Y-%m-%d")]
        return out[COLS]
    except Exception as exc:
        print(f"Index archive unavailable/invalid for {day}: {exc}")
        return None


def main():
    mapping = load_mapping()
    proxies = set(mapping.loc[mapping["Proxy_Available"], "Sector_Index_Proxy"].dropna().astype(str).str.strip())
    if not proxies:
        raise RuntimeError("No available sector index proxies found in mapping.")

    history = pd.read_csv(HISTORY_FILE) if HISTORY_FILE.exists() else pd.DataFrame(columns=COLS)
    if not set(COLS).issubset(history.columns):
        raise ValueError(f"Sector history requires columns: {COLS}")
    history = history[COLS].copy()
    history["Trade_Date"] = pd.to_datetime(history["Trade_Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    history["Index_Name"] = history["Index_Name"].astype(str).str.strip()
    history["Closing_Index_Value"] = pd.to_numeric(history["Closing_Index_Value"], errors="coerce")
    history = history.dropna(subset=["Trade_Date", "Closing_Index_Value"])
    history = history[history["Index_Name"].isin(proxies)]

    # Backfill the recent 15 calendar days, covering weekends and NSE holidays.
    added = 0
    for offset in range(0, 16):
        day = TODAY - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        date_text = day.strftime("%Y-%m-%d")
        present = set(history.loc[history["Trade_Date"] == date_text, "Index_Name"])
        if proxies.issubset(present):
            continue
        daily = download_day(day)
        if daily is None or daily.empty:
            time.sleep(0.5)
            continue
        daily = daily[daily["Index_Name"].isin(proxies)]
        if daily.empty:
            continue
        history = pd.concat([history, daily], ignore_index=True)
        added += len(daily)
        print(f"Backfilled {len(daily)} mapped indices for {date_text}")
        time.sleep(0.8)

    history = history.drop_duplicates(["Trade_Date", "Index_Name"], keep="last")
    history["Trade_Date_dt"] = pd.to_datetime(history["Trade_Date"], errors="coerce")
    history = history.sort_values(["Index_Name", "Trade_Date_dt"])
    history = history.groupby("Index_Name", group_keys=False).tail(120)
    history = history[COLS].sort_values(["Trade_Date", "Index_Name"])
    history.to_csv(HISTORY_FILE, index=False)

    latest_by_index = history.groupby("Index_Name")["Trade_Date"].max().to_dict()
    lagging = {k: v for k, v in latest_by_index.items() if v != max(latest_by_index.values())}
    print(f"Sector history saved. Added rows: {added}. Proxies: {len(proxies)}. Rows: {len(history)}")
    if lagging:
        print("WARNING: proxy histories do not share the same latest date:", lagging)
    else:
        print("All mapped proxies have the same latest date:", max(latest_by_index.values()))


if __name__ == "__main__":
    main()
