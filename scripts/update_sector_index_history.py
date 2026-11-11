# ============================================================
# AURORA SECTOR INDEX HISTORY UPDATER
# Source: NSE daily all-indices archive CSV.
# Retains mapped index history; skips weekends and holidays.
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
COLS = ["Trade_Date", "Index_Name", "Closing_Index_Value"]


def load_mapping():
    if not MAP_FILE.exists():
        raise FileNotFoundError(
            "Missing data/stock_sector_proxy_mapping_clean.csv. Add the approved 218-stock mapping file before running."
        )
    m = pd.read_csv(MAP_FILE)
    required = {"Symbol", "Industry", "Mapping_Status", "Sector_Index_Proxy", "Proxy_Type", "Proxy_Available"}
    if not required.issubset(m.columns):
        raise ValueError(f"Sector mapping missing columns: {sorted(required - set(m.columns))}")
    return m


def download_day(day):
    url = f"https://archives.nseindia.com/content/indices/ind_close_all_{day:%d%m%Y}.csv"
    try:
        r = requests.get(url, headers=HEADERS, timeout=35)
        if r.status_code != 200 or not r.text.strip():
            return None
        raw = pd.read_csv(io.StringIO(r.text))
        # NSE archive header variants seen across versions.
        name_col = next((c for c in raw.columns if c.strip().lower() == "index name"), None)
        close_col = next((c for c in raw.columns if c.strip().lower() in {"closing", "closing index value"}), None)
        date_col = next((c for c in raw.columns if c.strip().lower() in {"index date", "index_date", "date"}), None)
        if not name_col or not close_col:
            raise ValueError(f"Unexpected NSE indices CSV columns: {list(raw.columns)}")
        out = pd.DataFrame()
        out["Index_Name"] = raw[name_col].astype(str).str.strip()
        out["Closing_Index_Value"] = pd.to_numeric(raw[close_col], errors="coerce")
        if date_col:
            parsed = pd.to_datetime(raw[date_col], dayfirst=True, errors="coerce")
            out["Trade_Date"] = parsed.dt.strftime("%Y-%m-%d")
            out = out[out["Trade_Date"].notna()]
        else:
            out["Trade_Date"] = day.strftime("%Y-%m-%d")
        out = out.dropna(subset=["Closing_Index_Value"])
        out = out[out["Trade_Date"] == day.strftime("%Y-%m-%d")]
        return out[COLS]
    except Exception as exc:
        print(f"Index archive unavailable/invalid for {day}: {exc}")
        return None


def main():
    mapping = load_mapping()
    proxies = set(mapping.loc[mapping["Proxy_Available"].astype(str).str.lower().isin(["true", "1", "yes"]), "Sector_Index_Proxy"].dropna().astype(str))
    if HISTORY_FILE.exists():
        history = pd.read_csv(HISTORY_FILE)
    else:
        history = pd.DataFrame(columns=COLS)
    for c in COLS:
        if c not in history.columns:
            history[c] = pd.Series(dtype="object")
    history = history[COLS]
    history["Trade_Date"] = pd.to_datetime(history["Trade_Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    history["Index_Name"] = history["Index_Name"].astype(str).str.strip()
    history["Closing_Index_Value"] = pd.to_numeric(history["Closing_Index_Value"], errors="coerce")
    history = history.dropna(subset=["Trade_Date", "Closing_Index_Value"])
    # Search recent days so the job gracefully handles holidays and late NSE publication.
    fetched = False
    for offset in range(0, 11):
        day = TODAY - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        date_text = day.strftime("%Y-%m-%d")
        if ((history["Trade_Date"] == date_text) & history["Index_Name"].isin(proxies)).any():
            fetched = True
            break
        daily = download_day(day)
        if daily is not None and not daily.empty:
            daily = daily[daily["Index_Name"].isin(proxies)]
            if not daily.empty:
                history = pd.concat([history, daily], ignore_index=True)
                print(f"Downloaded {len(daily)} mapped index rows for {date_text}")
                fetched = True
                break
        time.sleep(1.2)
    if not fetched:
        print("No new mapped index archive found; retaining previous sector history.")
    history = history.drop_duplicates(["Trade_Date", "Index_Name"], keep="last")
    history = history.sort_values(["Index_Name", "Trade_Date"])
    # Keep at least 100 sessions per mapped proxy; do not trim a shorter initial history.
    history["Trade_Date_dt"] = pd.to_datetime(history["Trade_Date"], errors="coerce")
    history = history.sort_values(["Index_Name", "Trade_Date_dt"])
    history = history.groupby("Index_Name", group_keys=False).tail(120)
    history = history[COLS].sort_values(["Trade_Date", "Index_Name"])
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    history.to_csv(HISTORY_FILE, index=False)
    latest = history["Trade_Date"].max() if not history.empty else "unavailable"
    print(f"Sector index history saved; latest session: {latest}; rows: {len(history)}")


if __name__ == "__main__":
    main()
