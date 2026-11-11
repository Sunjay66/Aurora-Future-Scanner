"""Build an auditable F&O-stock to NSE sector-index mapping.

Constituents are downloaded from NSE Indices official CSV endpoints. A single
primary index is assigned using a documented specificity order; all matching
indices are retained in a separate field for audit. Unmatched stocks remain
UNMAPPED rather than being guessed.
"""
from __future__ import annotations

import io
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
ACTIVITY = ROOT / "data" / "stock_activity_history.csv"
MAPPING = ROOT / "data" / "stock_sector_mapping.csv"
TEMP = MAPPING.with_suffix(".csv.tmp")
IST = ZoneInfo("Asia/Kolkata")

# Official NSE Indices constituent CSV endpoints.
INDEX_URLS = {
    "NIFTY AUTO": "https://www.niftyindices.com/IndexConstituent/ind_niftyautolist.csv",
    "NIFTY BANK": "https://www.niftyindices.com/IndexConstituent/ind_niftybanklist.csv",
    "NIFTY PSU BANK": "https://www.niftyindices.com/IndexConstituent/ind_niftypsubanklist.csv",
    "NIFTY PRIVATE BANK": "https://www.niftyindices.com/IndexConstituent/ind_nifty_privatebanklist.csv",
    "NIFTY IT": "https://www.niftyindices.com/IndexConstituent/ind_niftyitlist.csv",
    "NIFTY PHARMA": "https://www.niftyindices.com/IndexConstituent/ind_niftypharmalist.csv",
    "NIFTY METAL": "https://www.niftyindices.com/IndexConstituent/ind_niftymetallist.csv",
    "NIFTY FMCG": "https://www.niftyindices.com/IndexConstituent/ind_niftyfmcglist.csv",
    "NIFTY REALTY": "https://www.niftyindices.com/IndexConstituent/ind_niftyrealtylist.csv",
    "NIFTY FINANCIAL SERVICES": "https://www.niftyindices.com/IndexConstituent/ind_niftyfinancelist.csv",
    "NIFTY MEDIA": "https://www.niftyindices.com/IndexConstituent/ind_niftymedialist.csv",
    "NIFTY HEALTHCARE": "https://www.niftyindices.com/IndexConstituent/ind_niftyhealthcarelist.csv",
    "NIFTY OIL & GAS": "https://www.niftyindices.com/IndexConstituent/ind_niftyoilgaslist.csv",
    "NIFTY ENERGY": "https://www.niftyindices.com/IndexConstituent/ind_niftyenergylist.csv",
}

# More specific banking indices take precedence over the broad Nifty Bank.
PRIMARY_PRIORITY = [
    "NIFTY PSU BANK",
    "NIFTY PRIVATE BANK",
    "NIFTY AUTO",
    "NIFTY IT",
    "NIFTY PHARMA",
    "NIFTY FMCG",
    "NIFTY METAL",
    "NIFTY REALTY",
    "NIFTY MEDIA",
    "NIFTY HEALTHCARE",
    "NIFTY OIL & GAS",
    "NIFTY ENERGY",
    "NIFTY BANK",
    "NIFTY FINANCIAL SERVICES",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://www.niftyindices.com/",
}

OUTPUT_COLUMNS = [
    "Symbol", "Primary_Sector_Index", "All_Matching_Indices",
    "Mapping_Status", "Mapping_Source", "Mapping_Refresh_IST",
]


def fetch_constituents(session: requests.Session, index_name: str, url: str) -> set[str]:
    response = session.get(url, timeout=40)
    response.raise_for_status()
    if len(response.content) < 50:
        raise ValueError(f"{index_name}: constituent CSV unexpectedly small ({len(response.content)} bytes)")
    frame = pd.read_csv(io.BytesIO(response.content), encoding="utf-8-sig")
    frame.columns = [str(c).replace("\ufeff", "").strip() for c in frame.columns]
    symbol_col = next((c for c in frame.columns if c.lower() in {"symbol", "ticker"}), None)
    if not symbol_col:
        raise ValueError(f"{index_name}: no Symbol column; received {list(frame.columns)}")
    symbols = set(frame[symbol_col].dropna().astype(str).str.strip().str.upper())
    symbols = {s for s in symbols if s and s not in {"NAN", "SYMBOL"}}
    if not symbols:
        raise ValueError(f"{index_name}: parsed zero constituent symbols")
    return symbols


def main() -> int:
    if not ACTIVITY.exists() or ACTIVITY.stat().st_size == 0:
        raise RuntimeError("Stock activity history is missing or empty.")
    activity = pd.read_csv(ACTIVITY, usecols=["Symbol"])
    fo_symbols = sorted(set(activity["Symbol"].dropna().astype(str).str.strip().str.upper()))
    if not fo_symbols:
        raise RuntimeError("No F&O stock symbols found.")

    session = requests.Session()
    session.headers.update(HEADERS)
    constituents: dict[str, set[str]] = {}
    failures = {}
    for index_name, url in INDEX_URLS.items():
        try:
            constituents[index_name] = fetch_constituents(session, index_name, url)
            print(f"OK {index_name}: {len(constituents[index_name])} constituents")
        except Exception as exc:
            failures[index_name] = f"{type(exc).__name__}: {exc}"
            print(f"WARNING {index_name}: constituent list unavailable: {failures[index_name]}")

    essential = {"NIFTY PSU BANK", "NIFTY PRIVATE BANK", "NIFTY BANK", "NIFTY IT"}
    if not essential.issubset(constituents):
        raise RuntimeError(
            "Essential index constituent lists unavailable; refusing to create partial mapping. "
            f"Missing: {sorted(essential - set(constituents))}"
        )

    refresh = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for symbol in fo_symbols:
        matches = [name for name in PRIMARY_PRIORITY if name in constituents and symbol in constituents[name]]
        primary = matches[0] if matches else "UNMAPPED"
        rows.append({
            "Symbol": symbol,
            "Primary_Sector_Index": primary,
            "All_Matching_Indices": "|".join(matches),
            "Mapping_Status": "MAPPED" if matches else "UNMAPPED",
            "Mapping_Source": "NSE Indices official constituent CSV",
            "Mapping_Refresh_IST": refresh,
        })

    result = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    mapped = int((result["Mapping_Status"] == "MAPPED").sum())
    unmapped = int((result["Mapping_Status"] == "UNMAPPED").sum())
    if len(result) != len(fo_symbols) or result["Symbol"].duplicated().any():
        raise RuntimeError("Mapping output failed one-row-per-stock validation.")
    if mapped == 0:
        raise RuntimeError("No F&O stocks mapped; refusing to write.")

    TEMP.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(TEMP, index=False)
    check = pd.read_csv(TEMP)
    if len(check) != len(fo_symbols) or check["Symbol"].duplicated().any():
        raise RuntimeError("Written mapping failed validation.")
    TEMP.replace(MAPPING)

    print("=" * 68)
    print(f"SUCCESS: mapping created for {len(result)} F&O stocks.")
    print(f"Mapped: {mapped}; unmapped: {unmapped}")
    print("Primary index counts:")
    print(result["Primary_Sector_Index"].value_counts().to_string())
    if failures:
        print("Unavailable optional constituent lists:")
        for name, reason in failures.items():
            print(f" - {name}: {reason}")
    print(f"Saved: {MAPPING}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
