# ============================================================
# AURORA FUTURES OI CLASSIFICATION
# Classifies front-month stock futures using daily price and OI
# ============================================================

from pathlib import Path
import pandas as pd

DATA_DIR = Path("data")
HISTORY_DIR = DATA_DIR / "history"

LATEST_FILE = DATA_DIR / "front_month_snapshot.csv"
CLASSIFICATION_FILE = DATA_DIR / "oi_classification_history.csv"

HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def classify_futures():

    if not LATEST_FILE.exists():
        raise FileNotFoundError(
            f"Latest snapshot not found: {LATEST_FILE}"
        )

    current = pd.read_csv(LATEST_FILE)

    required = [
        "Trade_Date",
        "Symbol",
        "Expiry_Date",
        "Close_Price",
        "Open_Interest",
    ]

    missing = [c for c in required if c not in current.columns]

    if missing:
        raise ValueError(f"Missing columns: {missing}")

    current["Trade_Date"] = pd.to_datetime(
        current["Trade_Date"], errors="coerce"
    )

    current["Expiry_Date"] = pd.to_datetime(
        current["Expiry_Date"], errors="coerce"
    )

    current["Close_Price"] = pd.to_numeric(
        current["Close_Price"], errors="coerce"
    )

    current["Open_Interest"] = pd.to_numeric(
        current["Open_Interest"], errors="coerce"
    )

    current_date = current["Trade_Date"].max()

    if pd.isna(current_date):
        raise ValueError("Invalid Trade_Date in snapshot.")

    date_string = current_date.strftime("%Y-%m-%d")

    # Save a permanent snapshot if not already present
    snapshot_file = HISTORY_DIR / f"snapshot_{date_string}.csv"

    if not snapshot_file.exists():
        current.to_csv(snapshot_file, index=False)
        print("Saved daily snapshot:", snapshot_file.name)
    else:
        print("Daily snapshot already exists:", snapshot_file.name)

    # Find the immediately previous available trading snapshot
    previous_files = sorted(
        HISTORY_DIR.glob("snapshot_*.csv")
    )

    previous_files = [
        f for f in previous_files
        if f.name != snapshot_file.name
    ]

    if not previous_files:
        print(
            "No previous trading snapshot available. "
            "Classification cannot yet be calculated."
        )
        return

    previous_file = previous_files[-1]

    previous = pd.read_csv(previous_file)

    previous["Trade_Date"] = pd.to_datetime(
        previous["Trade_Date"], errors="coerce"
    )

    previous["Expiry_Date"] = pd.to_datetime(
        previous["Expiry_Date"], errors="coerce"
    )

    previous["Close_Price"] = pd.to_numeric(
        previous["Close_Price"], errors="coerce"
    )

    previous["Open_Interest"] = pd.to_numeric(
        previous["Open_Interest"], errors="coerce"
    )

    previous_date = previous["Trade_Date"].max()

    print("Current date:", date_string)
    print("Previous snapshot:", previous_date.strftime("%Y-%m-%d"))

    # First trading day of a new calendar month:
    # do not compare with the previous month.
    if current_date.month != previous_date.month or \
       current_date.year != previous_date.year:

        print(
            "Monthly reset: previous snapshot belongs to "
            "a different month. Classification skipped."
        )
        return

    # Compare the same symbol and exact expiry
    merged = current.merge(
        previous,
        on=["Symbol", "Expiry_Date"],
        how="inner",
        suffixes=("_current", "_previous")
    )

    if merged.empty:
        print("No matching symbol + expiry contracts found.")
        return

    merged["Price_Change_Pct"] = (
        (
            merged["Close_Price_current"]
            - merged["Close_Price_previous"]
        )
        / merged["Close_Price_previous"]
        * 100
    )

    merged["OI_Change_Pct"] = (
        (
            merged["Open_Interest_current"]
            - merged["Open_Interest_previous"]
        )
        / merged["Open_Interest_previous"]
        * 100
    )

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

    merged["Classification"] = merged.apply(
        classification, axis=1
    )

    result = pd.DataFrame({
        "Trade_Date": merged["Trade_Date_current"],
        "Symbol": merged["Symbol"],
        "Expiry_Date": merged["Expiry_Date"],
        "Close_Price": merged["Close_Price_current"],
        "Price_Change_Pct": merged["Price_Change_Pct"],
        "Open_Interest": merged["Open_Interest_current"],
        "OI_Change_Pct": merged["OI_Change_Pct"],
        "Volume": merged.get("Volume_current"),
        "Classification": merged["Classification"],
    })

    result["Trade_Date"] = (
        pd.to_datetime(result["Trade_Date"])
        .dt.strftime("%Y-%m-%d")
    )

    result["Expiry_Date"] = (
        pd.to_datetime(result["Expiry_Date"])
        .dt.strftime("%Y-%m-%d")
    )

    # Append classifications while avoiding duplicate dates/contracts
    if CLASSIFICATION_FILE.exists():

        old = pd.read_csv(CLASSIFICATION_FILE)

        combined = pd.concat(
            [old, result],
            ignore_index=True
        )

    else:
        combined = result

    combined = combined.drop_duplicates(
        subset=["Trade_Date", "Symbol", "Expiry_Date"],
        keep="last"
    )

    combined.to_csv(
        CLASSIFICATION_FILE,
        index=False
    )

    print("\nClassification saved:", CLASSIFICATION_FILE)
    print("Matched contracts:", len(result))

    print("\nClassification counts:")
    print(result["Classification"].value_counts().to_string())


if __name__ == "__main__":
    classify_futures()
