# ============================================================
# AURORA FUTURES OI CLASSIFICATION
# Rebuilds classifications from consecutive daily snapshots
# ============================================================

from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

DATA_DIR = Path("data")
HISTORY_DIR = DATA_DIR / "history"
CLASSIFICATION_FILE = DATA_DIR / "oi_classification_history.csv"

HISTORY_DIR.mkdir(parents=True, exist_ok=True)

REQUIRED_COLUMNS = [
    "Trade_Date",
    "Symbol",
    "Expiry_Date",
    "Close_Price",
    "Open_Interest",
]


def read_snapshot(file_path):
    """Read and normalize one daily front-month snapshot."""
    df = pd.read_csv(file_path)

    missing = [column for column in REQUIRED_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError(f"{file_path.name} is missing columns: {missing}")

    df["Trade_Date"] = pd.to_datetime(df["Trade_Date"], errors="coerce")
    df["Expiry_Date"] = pd.to_datetime(df["Expiry_Date"], errors="coerce")
    df["Close_Price"] = pd.to_numeric(df["Close_Price"], errors="coerce")
    df["Open_Interest"] = pd.to_numeric(df["Open_Interest"], errors="coerce")

    df["Symbol"] = df["Symbol"].astype(str).str.strip()

    # Remove incomplete rows and any duplicate symbol/expiry rows.
    df = df.dropna(
        subset=["Trade_Date", "Symbol", "Expiry_Date", "Close_Price", "Open_Interest"]
    )
    df = df.drop_duplicates(subset=["Symbol", "Expiry_Date"], keep="last")

    return df


def get_refresh_time(current):
    if (
        "Refresh_Time_IST" in current.columns
        and current["Refresh_Time_IST"].notna().any()
    ):
        return str(current["Refresh_Time_IST"].dropna().iloc[0])

    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime(
        "%Y-%m-%d %H:%M:%S IST"
    )


def classify_pair(previous, current):
    """Classify current day's contracts against the previous snapshot."""
    previous_date = previous["Trade_Date"].max()
    current_date = current["Trade_Date"].max()

    # Monthly reset: do not compare across calendar months.
    if (
        current_date.month != previous_date.month
        or current_date.year != previous_date.year
    ):
        print(
            f"Skipping month boundary: "
            f"{previous_date:%Y-%m-%d} -> {current_date:%Y-%m-%d}"
        )
        return pd.DataFrame()

    merged = current.merge(
        previous,
        on=["Symbol", "Expiry_Date"],
        how="inner",
        suffixes=("_current", "_previous"),
    )

    if merged.empty:
        print(
            f"No matching symbol/expiry contracts for "
            f"{previous_date:%Y-%m-%d} -> {current_date:%Y-%m-%d}"
        )
        return pd.DataFrame()

    merged["Price_Change_Pct"] = (
        (merged["Close_Price_current"] - merged["Close_Price_previous"])
        / merged["Close_Price_previous"]
        * 100
    )

    merged["OI_Change_Pct"] = (
        (merged["Open_Interest_current"] - merged["Open_Interest_previous"])
        / merged["Open_Interest_previous"]
        * 100
    )

    merged = merged[
        merged["Close_Price_previous"].notna()
        & (merged["Close_Price_previous"] != 0)
        & merged["Open_Interest_previous"].notna()
        & (merged["Open_Interest_previous"] != 0)
        & merged["Price_Change_Pct"].notna()
        & merged["OI_Change_Pct"].notna()
    ].copy()

    if merged.empty:
        return pd.DataFrame()

    def classification(row):
        price_change = row["Price_Change_Pct"]
        oi_change = row["OI_Change_Pct"]

        if price_change > 0 and oi_change > 0:
            return "Long Buildup"
        elif price_change < 0 and oi_change > 0:
            return "Short Buildup"
        elif price_change > 0 and oi_change < 0:
            return "Short Covering"
        elif price_change < 0 and oi_change < 0:
            return "Long Unwinding"
        else:
            return "Neutral"

    merged["Classification"] = merged.apply(classification, axis=1)

    refresh_time = get_refresh_time(current)

    result = pd.DataFrame({
        "Trade_Date": merged["Trade_Date_current"],
        "Previous_Trade_Date": merged["Trade_Date_previous"],
        "Symbol": merged["Symbol"],
        "Expiry_Date": merged["Expiry_Date"],
        "Close_Price": merged["Close_Price_current"],
        "Close_Price_Previous": merged["Close_Price_previous"],
        "Price_Change_Pct": merged["Price_Change_Pct"],
        "Open_Interest": merged["Open_Interest_current"],
        "Open_Interest_Previous": merged["Open_Interest_previous"],
        "OI_Change_Pct": merged["OI_Change_Pct"],
        "Volume": merged["Volume_current"] if "Volume_current" in merged.columns else None,
        "Classification": merged["Classification"],
        "Refresh_Time_IST": refresh_time,
    })

    for column in ["Trade_Date", "Previous_Trade_Date", "Expiry_Date"]:
        result[column] = pd.to_datetime(result[column]).dt.strftime("%Y-%m-%d")

    return result


def classify_futures():
    # Use all available daily snapshots in chronological order.
    snapshot_files = sorted(HISTORY_DIR.glob("snapshot_*.csv"))

    if len(snapshot_files) < 2:
        raise FileNotFoundError(
            "At least two daily snapshots are required in data/history/."
        )

    snapshots = []
    for file_path in snapshot_files:
        snapshot = read_snapshot(file_path)
        if snapshot.empty:
            print("Skipping empty snapshot:", file_path.name)
            continue

        trade_date = snapshot["Trade_Date"].max()
        snapshots.append((trade_date, file_path, snapshot))

    snapshots.sort(key=lambda item: item[0])

    if len(snapshots) < 2:
        raise ValueError("Fewer than two valid snapshots were found.")

    results = []

    print(f"Found {len(snapshots)} historical snapshots.")

    for index in range(1, len(snapshots)):
        previous_date, previous_file, previous = snapshots[index - 1]
        current_date, current_file, current = snapshots[index]

        print(
            f"Processing {previous_date:%Y-%m-%d} -> "
            f"{current_date:%Y-%m-%d}"
        )

        pair_result = classify_pair(previous, current)

        if not pair_result.empty:
            results.append(pair_result)
            print("  Classified contracts:", len(pair_result))
        else:
            print("  No classifications generated.")

    if not results:
        print("No classification results were generated.")
        return

    # Rebuild the file from snapshots each run; this backfills missing dates
    # and prevents duplicate records from accumulating.
    combined = pd.concat(results, ignore_index=True)

    combined = combined.drop_duplicates(
        subset=["Trade_Date", "Symbol", "Expiry_Date"],
        keep="last",
    )

    combined = combined.sort_values(["Trade_Date", "Symbol"])

    combined.to_csv(CLASSIFICATION_FILE, index=False)

    print("\nClassification history rebuilt:", CLASSIFICATION_FILE)
    print("Total classified rows:", len(combined))
    print("\nClassification counts:")
    print(combined["Classification"].value_counts().to_string())


if __name__ == "__main__":
    classify_futures()
