"""Join each F&O stock's primary NSE sector index into stock activity history."""
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ACTIVITY = ROOT / "data" / "stock_activity_history.csv"
MAPPING = ROOT / "data" / "stock_sector_mapping.csv"
SECTOR = ROOT / "data" / "sector_index_history.csv"
OUTPUT = ROOT / "data" / "stock_sector_activity_history.csv"
TEMP = OUTPUT.with_suffix(".csv.tmp")


def read_required(path, required, label):
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"{label} missing or empty: {path}")
    frame = pd.read_csv(path, dtype={"Trade_Date": str, "Symbol": str})
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"{label} missing columns: {sorted(missing)}")
    return frame


def main():
    activity = read_required(
        ACTIVITY,
        {"Trade_Date", "Symbol", "Expiry_Date", "Close_Price", "Open_Interest",
         "Delivery_Pct", "Is_Muhurat", "Include_In_Calculations"},
        "Stock activity history",
    )
    mapping = read_required(
        MAPPING,
        {"Symbol", "Primary_Sector_Index", "Mapping_Status"},
        "Stock-sector mapping",
    )
    sector = read_required(
        SECTOR,
        {"Trade_Date", "Index_Name", "Close", "Daily_Change_Pct",
         "Is_Muhurat", "Include_In_Calculations"},
        "Sector-index history",
    )

    for frame, label in [(activity, "activity"), (mapping, "mapping"), (sector, "sector")]:
        if frame["Trade_Date"].isna().any() if "Trade_Date" in frame.columns else False:
            raise RuntimeError(f"{label}: missing trading dates")

    activity["Trade_Date"] = pd.to_datetime(activity["Trade_Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    sector["Trade_Date"] = pd.to_datetime(sector["Trade_Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    activity["Symbol"] = activity["Symbol"].astype(str).str.strip().str.upper()
    mapping["Symbol"] = mapping["Symbol"].astype(str).str.strip().str.upper()
    sector["Index_Name"] = sector["Index_Name"].astype(str).str.strip().str.upper()

    if mapping["Symbol"].duplicated().any():
        raise RuntimeError("Mapping contains duplicate stock symbols.")
    if sector.duplicated(["Trade_Date", "Index_Name"]).any():
        raise RuntimeError("Sector history contains duplicate date/index records.")

    merged = activity.merge(
        mapping[["Symbol", "Primary_Sector_Index", "Mapping_Status"]],
        on="Symbol", how="left", validate="many_to_one",
    )
    merged["Primary_Sector_Index"] = merged["Primary_Sector_Index"].fillna("UNMAPPED")
    merged["Mapping_Status"] = merged["Mapping_Status"].fillna("UNMAPPED")

    sector_lookup = sector.rename(columns={
        "Index_Name": "Primary_Sector_Index",
        "Close": "Sector_Index_Close",
        "Daily_Change_Pct": "Sector_Daily_Change_Pct",
        "Is_Muhurat": "Sector_Is_Muhurat",
        "Include_In_Calculations": "Sector_Include_In_Calculations",
    })
    merged = merged.merge(
        sector_lookup[[
            "Trade_Date", "Primary_Sector_Index", "Sector_Index_Close",
            "Sector_Daily_Change_Pct", "Sector_Is_Muhurat",
            "Sector_Include_In_Calculations",
        ]],
        on=["Trade_Date", "Primary_Sector_Index"],
        how="left",
        validate="many_to_one",
    )

    merged["Sector_Index_Match"] = (
        merged["Mapping_Status"].eq("MAPPED")
        & merged["Sector_Index_Close"].notna()
        & merged["Include_In_Calculations"].astype(str).str.lower().eq("true")
    )

    # Cumulative sector performance starts at the first regular session close
    # available in this stock's current front-month contract history.
    merged["Sector_Cumulative_Change_Pct"] = pd.NA
    regular = merged[
        merged["Include_In_Calculations"].astype(str).str.lower().eq("true")
        & merged["Sector_Index_Close"].notna()
    ].copy()
    regular = regular.sort_values(["Symbol", "Trade_Date"])
    first_close = regular.groupby("Symbol")["Sector_Index_Close"].transform("first")
    regular["Sector_Cumulative_Change_Pct"] = (
        (regular["Sector_Index_Close"] / first_close - 1) * 100
    ).round(4)
    merged.loc[regular.index, "Sector_Cumulative_Change_Pct"] = regular["Sector_Cumulative_Change_Pct"]

    merged = merged.sort_values(["Trade_Date", "Symbol"]).reset_index(drop=True)
    if len(merged) != len(activity):
        raise RuntimeError("Sector join changed stock activity row count.")
    if merged.duplicated(["Trade_Date", "Symbol"]).any():
        raise RuntimeError("Sector join created duplicate stock/date rows.")

    # Unmapped stocks are reported, not assigned a guessed sector.
    mapped_symbols = mapping.loc[mapping["Mapping_Status"] == "MAPPED", "Symbol"].nunique()
    unmatched = mapping.loc[mapping["Mapping_Status"] != "MAPPED", "Symbol"].tolist()
    latest_date = merged["Trade_Date"].max()
    latest = merged[merged["Trade_Date"] == latest_date]
    latest_matches = int(latest["Sector_Index_Close"].notna().sum())

    TEMP.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(TEMP, index=False)
    check = pd.read_csv(TEMP, dtype={"Trade_Date": str, "Symbol": str})
    if len(check) != len(activity) or check.duplicated(["Trade_Date", "Symbol"]).any():
        raise RuntimeError("Written stock-sector activity file failed validation.")
    TEMP.replace(OUTPUT)

    print(f"SUCCESS: {len(merged)} stock/date rows joined.")
    print(f"Mapped symbols: {mapped_symbols}; unmapped symbols: {len(unmatched)}")
    print(f"Latest date {latest_date}: sector index values matched for {latest_matches}/{len(latest)} stocks.")
    print(f"Muhurat rows retained: {int(merged['Is_Muhurat'].astype(str).str.lower().eq('true').sum())}")
    print(f"Regular calculation-eligible rows: {int(merged['Include_In_Calculations'].astype(str).str.lower().eq('true').sum())}")
    if unmatched:
        print("UNMAPPED SYMBOLS: " + ", ".join(unmatched))
    print("Primary sector index counts:")
    print(mapping["Primary_Sector_Index"].value_counts().to_string())
    print(f"Saved: {OUTPUT}")


if __name__ == "__main__":
    main()
