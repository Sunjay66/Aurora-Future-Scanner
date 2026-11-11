"""Map all current F&O stocks to their most relevant NSE sectoral index.

Primary assignments are based on official NSE Indices sectoral-index
constituent CSVs. If a stock belongs to more than one index, the explicit
priority below selects the closest/specialist sector index. No company is
assigned to a sector without constituent evidence; residuals remain UNMAPPED
for a separate industry-classification review.
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
BASE = "https://www.niftyindices.com/IndexConstituent/"

# Official constituent download endpoints. Existing legacy endpoints are
# retained; newer sectoral series use the current *_list.csv naming.
INDEX_FILES = {
    "NIFTY PSU BANK": "ind_niftypsubanklist.csv",
    "NIFTY PRIVATE BANK": "ind_nifty_privatebanklist.csv",
    "NIFTY BANK": "ind_niftybanklist.csv",
    "NIFTY AUTO": "ind_niftyautolist.csv",
    "NIFTY IT": "ind_niftyitlist.csv",
    "NIFTY PHARMA": "ind_niftypharmalist.csv",
    "NIFTY METAL": "ind_niftymetallist.csv",
    "NIFTY FMCG": "ind_niftyfmcglist.csv",
    "NIFTY REALTY": "ind_niftyrealtylist.csv",
    "NIFTY FINANCIAL SERVICES": "ind_niftyfinancelist.csv",
    "NIFTY MEDIA": "ind_niftymedialist.csv",
    "NIFTY HEALTHCARE": "ind_niftyhealthcarelist.csv",
    "NIFTY OIL & GAS": "ind_niftyoilgaslist.csv",
    "NIFTY ENERGY": "ind_niftyenergylist.csv",
    "NIFTY CAPITAL GOODS": "ind_niftyCapitalGoods_list.csv",
    "NIFTY CEMENT": "ind_niftyCement_list.csv",
    "NIFTY CHEMICALS": "ind_niftyChemicals_list.csv",
    "NIFTY COMMERCIAL & TRANSPORT SERVICES": "ind_niftyCommercialandTransportServices_list.csv",
    "NIFTY CONSTRUCTION": "ind_niftyConstruction_list.csv",
    "NIFTY CONSUMER DURABLES": "ind_niftyConsumerDurables_list.csv",
    "NIFTY CONSUMER SERVICES": "ind_niftyConsumerServices_list.csv",
    "NIFTY FINANCIAL SERVICES EX BANK": "ind_niftyFinancialServicesExBank_list.csv",
    "NIFTY HOSPITALS": "ind_niftyHospitals_list.csv",
    "NIFTY HOUSING FINANCE": "ind_niftyHousingFinance_list.csv",
    "NIFTY INSURANCE": "ind_niftyInsurance_list.csv",
    "NIFTY NBFC": "ind_niftyNBFC_list.csv",
    "NIFTY POWER": "ind_niftyPower_list.csv",
    "NIFTY RETAIL": "ind_niftyRetail_list.csv",
    "NIFTY TELECOMMUNICATIONS": "ind_niftyTelecommunications_list.csv",
    "NIFTY REITS & REALTY": "ind_niftyREITsRealty_list.csv",
    "NIFTY500 HEALTHCARE": "ind_nifty500Healthcare_list.csv",
    "NIFTY MIDSMALL FINANCIAL SERVICES": "ind_niftyMidSmallFinancialServices_list.csv",
    "NIFTY MIDSMALL HEALTHCARE": "ind_niftyMidSmallHealthcare_list.csv",
    "NIFTY MIDSMALL IT & TELECOM": "ind_niftyMidSmallITandTelecom_list.csv",
}
INDEX_URLS = {name: BASE + filename for name, filename in INDEX_FILES.items()}

# Sector-specialist priority prevents broad indices (Bank/Financial Services)
# from overriding a more specific industry index.
PRIMARY_PRIORITY = [
    "NIFTY PSU BANK", "NIFTY PRIVATE BANK", "NIFTY BANK",
    "NIFTY INSURANCE", "NIFTY NBFC", "NIFTY HOUSING FINANCE",
    "NIFTY FINANCIAL SERVICES EX BANK", "NIFTY FINANCIAL SERVICES",
    "NIFTY AUTO", "NIFTY IT", "NIFTY TELECOMMUNICATIONS",
    "NIFTY PHARMA", "NIFTY HOSPITALS", "NIFTY HEALTHCARE",
    "NIFTY MIDSMALL HEALTHCARE", "NIFTY500 HEALTHCARE",
    "NIFTY FMCG", "NIFTY CONSUMER DURABLES", "NIFTY CONSUMER SERVICES",
    "NIFTY RETAIL", "NIFTY MEDIA", "NIFTY METAL", "NIFTY CEMENT",
    "NIFTY CHEMICALS", "NIFTY CAPITAL GOODS", "NIFTY CONSTRUCTION",
    "NIFTY COMMERCIAL & TRANSPORT SERVICES", "NIFTY REALTY",
    "NIFTY REITS & REALTY", "NIFTY OIL & GAS", "NIFTY POWER",
    "NIFTY ENERGY", "NIFTY MIDSMALL IT & TELECOM",
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
        raise ValueError(f"CSV unexpectedly small ({len(response.content)} bytes)")
    frame = pd.read_csv(io.BytesIO(response.content), encoding="utf-8-sig")
    frame.columns = [str(c).replace("\ufeff", "").strip() for c in frame.columns]
    symbol_col = next((c for c in frame.columns if c.lower() in {"symbol", "ticker"}), None)
    if not symbol_col:
        raise ValueError(f"No Symbol column; received {list(frame.columns)}")
    symbols = set(frame[symbol_col].dropna().astype(str).str.strip().str.upper())
    symbols = {s for s in symbols if s and s not in {"NAN", "SYMBOL"}}
    if not symbols:
        raise ValueError("Parsed zero constituent symbols")
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
            print(f"WARNING {index_name}: {failures[index_name]}")

    essential = {"NIFTY PSU BANK", "NIFTY PRIVATE BANK", "NIFTY BANK", "NIFTY IT"}
    if not essential.issubset(constituents):
        raise RuntimeError(
            "Essential constituent lists unavailable; refusing partial mapping. "
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
            "Mapping_Source": "NSE Indices official sectoral constituent CSV",
            "Mapping_Refresh_IST": refresh,
        })

    result = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    if len(result) != len(fo_symbols) or result["Symbol"].duplicated().any():
        raise RuntimeError("Mapping output failed one-row-per-stock validation.")
    mapped = int(result["Mapping_Status"].eq("MAPPED").sum())
    unmapped = len(result) - mapped
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
        print("Unavailable index lists (review required):")
        for name, reason in failures.items():
            print(f" - {name}: {reason}")
    if unmapped:
        print("UNMAPPED SYMBOLS: " + ", ".join(result.loc[result["Mapping_Status"] == "UNMAPPED", "Symbol"]))
    print(f"Saved: {MAPPING}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
