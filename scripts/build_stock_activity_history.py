"""Build stock-level activity history by joining futures and cash delivery data.

Muhurat sessions are retained and explicitly marked, but excluded from
session-to-session OI calculations and all calculation-ready metrics.
"""
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FUTURES = ROOT / "data" / "stock_futures_history.csv"
DELIVERY = ROOT / "data" / "delivery_history.csv"
OUTPUT = ROOT / "data" / "stock_activity_history.csv"
TEMP = OUTPUT.with_suffix(".csv.tmp")

MUHURAT_DATES = {"2026-11-08"}

FUTURES_REQUIRED = {
    "Trade_Date", "Symbol", "Expiry_Date", "Open_Price", "High_Price",
    "Low_Price", "Close_Price", "Open_Interest", "Volume"
}
DELIVERY_REQUIRED = {
    "Trade_Date", "Session_Type", "Symbol", "Quantity_Traded",
    "Deliverable_Quantity", "Delivery_Pct"
}


def load_valid(path, required, label):
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"{label} is missing or empty: {path}")
    frame = pd.read_csv(path, dtype={"Trade_Date": str, "Symbol": str})
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"{label} missing columns: {sorted(missing)}")
    frame["Trade_Date"] = pd.to_datetime(frame["Trade_Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    frame["Symbol"] = frame["Symbol"].astype(str).str.strip().str.upper()
    if frame[["Trade_Date", "Symbol"]].isna().any().any():
        raise RuntimeError(f"{label} contains invalid date/symbol keys.")
    if frame.duplicated(["Trade_Date", "Symbol"]).any():
        raise RuntimeError(f"{label} contains duplicate date/symbol keys.")
    return frame


def main():
    futures = load_valid(FUTURES, FUTURES_REQUIRED, "Futures history")
    delivery = load_valid(DELIVERY, DELIVERY_REQUIRED, "Delivery history")

    for col in ["Open_Price", "High_Price", "Low_Price", "Close_Price", "Open_Interest", "Volume"]:
        futures[col] = pd.to_numeric(futures[col], errors="coerce")
    for col in ["Quantity_Traded", "Deliverable_Quantity", "Delivery_Pct"]:
        delivery[col] = pd.to_numeric(delivery[col], errors="coerce")

    merged = futures.merge(
        delivery[["Trade_Date", "Symbol", "Session_Type", "Quantity_Traded",
                  "Deliverable_Quantity", "Delivery_Pct"]],
        on=["Trade_Date", "Symbol"], how="left", validate="one_to_one",
        indicator=True
    )
    missing = merged[merged["_merge"] != "both"]
    if not missing.empty:
        examples = missing[["Trade_Date", "Symbol"]].head(10).to_dict("records")
        raise RuntimeError(
            f"Delivery match missing for {len(missing)} futures rows; examples={examples}. "
            "No derived file written."
        )
    merged = merged.drop(columns="_merge")

    merged["Session_Type"] = merged["Session_Type"].fillna("Regular")
    merged["Is_Muhurat"] = merged["Trade_Date"].isin(MUHURAT_DATES)
    merged["Include_In_Calculations"] = ~merged["Is_Muhurat"]

    # Daily price movement follows the agreed opening-to-closing definition.
    merged["Daily_Price_Movement_Pct"] = (
        (merged["Close_Price"] - merged["Open_Price"]) / merged["Open_Price"] * 100
    ).where(merged["Include_In_Calculations"])

    # Compare OI only with the previous included regular session for that symbol.
    regular = merged[merged["Include_In_Calculations"]].sort_values(["Symbol", "Trade_Date"]).copy()
    regular["Previous_Regular_OI"] = regular.groupby("Symbol")["Open_Interest"].shift(1)
    regular["Daily_OI_Change_Pct"] = (
        (regular["Open_Interest"] - regular["Previous_Regular_OI"])
        / regular["Previous_Regular_OI"] * 100
    ).where(regular["Previous_Regular_OI"] > 0)
    merged["Previous_Regular_OI"] = pd.NA
    merged["Daily_OI_Change_Pct"] = pd.NA
    merged = merged.set_index(["Trade_Date", "Symbol"])
    regular = regular.set_index(["Trade_Date", "Symbol"])
    merged.loc[regular.index, "Previous_Regular_OI"] = regular["Previous_Regular_OI"]
    merged.loc[regular.index, "Daily_OI_Change_Pct"] = regular["Daily_OI_Change_Pct"]
    merged = merged.reset_index()

    merged["Daily_Price_Movement_Pct"] = pd.to_numeric(merged["Daily_Price_Movement_Pct"], errors="coerce").round(4)
    merged["Daily_OI_Change_Pct"] = pd.to_numeric(merged["Daily_OI_Change_Pct"], errors="coerce").round(4)
    merged["Delivery_Pct"] = pd.to_numeric(merged["Delivery_Pct"], errors="coerce").round(4)
    merged = merged.sort_values(["Trade_Date", "Symbol"]).reset_index(drop=True)

    if merged.empty:
        raise RuntimeError("Joined stock activity history is empty.")
    if merged["Is_Muhurat"].sum() == 0 and MUHURAT_DATES.intersection(set(merged["Trade_Date"])):
        raise RuntimeError("Muhurat session flag validation failed.")

    TEMP.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(TEMP, index=False)
    check = pd.read_csv(TEMP, dtype={"Trade_Date": str, "Symbol": str})
    if len(check) != len(merged) or check.duplicated(["Trade_Date", "Symbol"]).any():
        raise RuntimeError("Output validation failed; existing output left unchanged.")
    if check["Include_In_Calculations"].astype(str).str.lower().eq("true").sum() != merged["Include_In_Calculations"].sum():
        raise RuntimeError("Calculation inclusion flags failed output validation.")
    TEMP.replace(OUTPUT)

    print(f"SUCCESS: joined {len(merged)} stock/date rows.")
    print(f"Dates: {merged['Trade_Date'].nunique()}; symbols on latest date: {merged[merged['Trade_Date'] == merged['Trade_Date'].max()]['Symbol'].nunique()}")
    print(f"Delivery matches: {len(merged) - merged['Delivery_Pct'].isna().sum()}/{len(merged)}")
    print(f"Muhurat rows retained but excluded: {int(merged['Is_Muhurat'].sum())}")
    print(f"Regular calculation-eligible rows: {int(merged['Include_In_Calculations'].sum())}")
    print(f"Saved: {OUTPUT}")


if __name__ == "__main__":
    main()
