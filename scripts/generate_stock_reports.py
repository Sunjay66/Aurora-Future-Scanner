# ============================================================
# AURORA MONTHLY STOCK REPORT GENERATOR
# Builds current-month and archived stock-wise reports.
# No backtesting or strategy performance simulation.
# ============================================================

from pathlib import Path
import json
import pandas as pd

DATA = Path("data")
HISTORY = DATA / "history"
CASH_HISTORY = DATA / "cash_history"
CURRENT_DIR = DATA / "current_month"
ARCHIVE_DIR = DATA / "archive"
CURRENT_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

FUTURES_REQUIRED = ["Trade_Date", "Symbol", "Expiry_Date", "Close_Price", "Open_Interest", "Volume"]
CASH_REQUIRED = ["Trade_Date", "Symbol", "Open_Price", "High_Price", "Low_Price", "Close_Price"]


def read_csv_safe(path):
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def load_futures_history():
    frames = []
    for path in sorted(HISTORY.glob("snapshot_*.csv")):
        frame = read_csv_safe(path)
        if frame.empty:
            continue
        missing = [c for c in FUTURES_REQUIRED if c not in frame.columns]
        if missing:
            raise ValueError(f"{path} missing required fields: {missing}")
        frames.append(frame)
    if not frames:
        raise FileNotFoundError("No daily futures snapshots found in data/history/.")
    df = pd.concat(frames, ignore_index=True)
    df["Trade_Date"] = pd.to_datetime(df["Trade_Date"], errors="coerce")
    df["Expiry_Date"] = pd.to_datetime(df["Expiry_Date"], errors="coerce")
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    for c in ["Close_Price", "Open_Interest", "Volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["Trade_Date", "Symbol"])
    return df.drop_duplicates(["Trade_Date", "Symbol"], keep="last")


def load_cash_history():
    frames = []
    for path in sorted(CASH_HISTORY.glob("cash_ohlc_*.csv")):
        frame = read_csv_safe(path)
        if frame.empty:
            continue
        missing = [c for c in CASH_REQUIRED if c not in frame.columns]
        if missing:
            raise ValueError(f"{path} missing required fields: {missing}")
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=CASH_REQUIRED)
    df = pd.concat(frames, ignore_index=True)
    df["Trade_Date"] = pd.to_datetime(df["Trade_Date"], errors="coerce")
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    for c in ["High_Price", "Low_Price", "Close_Price"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["Trade_Date", "Symbol", "High_Price", "Low_Price", "Close_Price"])
    df = df.drop_duplicates(["Trade_Date", "Symbol"], keep="last")
    df = df.sort_values(["Symbol", "Trade_Date"])
    df["Previous_Cash_Close"] = df.groupby("Symbol")["Close_Price"].shift(1)
    df["Daily_Range_Rs"] = df["High_Price"] - df["Low_Price"]
    df["Daily_Range_Pct"] = (
        df["Daily_Range_Rs"] / df["Previous_Cash_Close"].replace(0, pd.NA) * 100
    )
    # Rolling 14 sessions, including prior-month sessions for warm-up.
    df["ADR_14_Rs"] = df.groupby("Symbol")["Daily_Range_Rs"].transform(
        lambda s: s.rolling(14, min_periods=14).mean()
    )
    df["ADR_14_Pct"] = df.groupby("Symbol")["Daily_Range_Pct"].transform(
        lambda s: s.rolling(14, min_periods=14).mean()
    )
    return df


def load_delivery():
    path = DATA / "delivery_history.csv"
    df = read_csv_safe(path)
    if df.empty or not {"Trade_Date", "Symbol", "Delivery_Pct"}.issubset(df.columns):
        return pd.DataFrame(columns=["Trade_Date", "Symbol", "Delivery_Pct"])
    df["Trade_Date"] = pd.to_datetime(df["Trade_Date"], errors="coerce")
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    df["Delivery_Pct"] = pd.to_numeric(df["Delivery_Pct"], errors="coerce")
    return df[["Trade_Date", "Symbol", "Delivery_Pct"]].drop_duplicates(
        ["Trade_Date", "Symbol"], keep="last"
    )



def build_delivery_universe_report(futures, delivery):
    """
    One row per current front-month F&O stock.
    The 20-session mean includes the latest trade date and requires
    20 valid NSE delivery observations. Missing values remain blank.
    """
    latest_date = futures["Trade_Date"].max()
    latest = futures[futures["Trade_Date"].eq(latest_date)].copy()
    symbols = sorted(latest["Symbol"].dropna().astype(str).str.strip().unique())

    history = delivery.copy()
    if not history.empty:
        history["Trade_Date"] = pd.to_datetime(history["Trade_Date"], errors="coerce")
        history["Symbol"] = history["Symbol"].astype(str).str.strip()
        history["Delivery_Pct"] = pd.to_numeric(history["Delivery_Pct"], errors="coerce")
        history = history.dropna(subset=["Trade_Date", "Symbol"])
        history = history[history["Trade_Date"].le(latest_date)]
        history = history.drop_duplicates(["Trade_Date", "Symbol"], keep="last")

    output = []
    for symbol in symbols:
        rows = history[history["Symbol"].eq(symbol)].sort_values("Trade_Date", ascending=False)
        recent = rows.head(20)
        valid = recent["Delivery_Pct"].dropna().head(20)
        latest_row = rows[rows["Trade_Date"].eq(latest_date)]
        latest_pct = latest_row["Delivery_Pct"].iloc[-1] if not latest_row.empty else pd.NA
        avg_pct = valid.mean() if len(valid) == 20 else pd.NA
        change_pct = (
            (float(latest_pct) / float(avg_pct) - 1) * 100
            if pd.notna(latest_pct) and pd.notna(avg_pct) and float(avg_pct) != 0
            else pd.NA
        )
        output.append({
            "Trade_Date": latest_date.strftime("%Y-%m-%d"),
            "Symbol": symbol,
            "Delivery_Pct": latest_pct,
            "Avg_Delivery_Pct_20D": avg_pct,
            "Change_vs_20D_Avg_Pct": change_pct,
            "Valid_Delivery_Sessions_20D": len(valid),
        })

    result = pd.DataFrame(output)
    for col in ["Delivery_Pct", "Avg_Delivery_Pct_20D", "Change_vs_20D_Avg_Pct"]:
        result[col] = pd.to_numeric(result[col], errors="coerce").round(2)
    result = result.sort_values(
        ["Delivery_Pct", "Symbol"],
        ascending=[False, True],
        na_position="last"
    )
    target = DATA / "delivery_report.csv"
    result.to_csv(target, index=False)
    print(
        f"Delivery stock report: {len(result)} F&O stocks for "
        f"{latest_date:%Y-%m-%d}; 20-session averages available for "
        f"{result['Avg_Delivery_Pct_20D'].notna().sum()} stocks."
    )


def load_sector_data():
    mapping_path = DATA / "stock_sector_proxy_mapping_clean.csv"
    history_path = DATA / "sector_index_history_clean.csv"
    if not mapping_path.exists():
        raise FileNotFoundError("Missing data/stock_sector_proxy_mapping_clean.csv; import the approved stock-sector mapping.")
    if not history_path.exists():
        raise FileNotFoundError("Missing data/sector_index_history_clean.csv; import the filtered 100-session sector index history.")
    mapping = read_csv_safe(mapping_path)
    required = {"Symbol", "Industry", "Mapping_Status", "Sector_Index_Proxy", "Proxy_Type", "Proxy_Available"}
    if not required.issubset(mapping.columns):
        raise ValueError(f"Sector mapping missing columns: {sorted(required - set(mapping.columns))}")
    mapping["Symbol"] = mapping["Symbol"].astype(str).str.strip()
    mapping["Proxy_Available"] = mapping["Proxy_Available"].astype(str).str.lower().isin(["true", "1", "yes"])
    mapping.loc[~mapping["Proxy_Available"], "Sector_Index_Proxy"] = pd.NA
    history = read_csv_safe(history_path)
    if not {"Trade_Date", "Index_Name", "Closing_Index_Value"}.issubset(history.columns):
        raise ValueError("Sector index history must contain Trade_Date, Index_Name, Closing_Index_Value.")
    history["Trade_Date"] = pd.to_datetime(history["Trade_Date"], errors="coerce")
    history["Index_Name"] = history["Index_Name"].astype(str).str.strip()
    history["Closing_Index_Value"] = pd.to_numeric(history["Closing_Index_Value"], errors="coerce")
    history = history.dropna(subset=["Trade_Date", "Closing_Index_Value"])
    history = history.drop_duplicates(["Trade_Date", "Index_Name"], keep="last")
    history = history.sort_values(["Index_Name", "Trade_Date"])
    history["Sector_Daily_Change_Pct"] = history.groupby("Index_Name")["Closing_Index_Value"].pct_change() * 100
    history["Report_Month"] = history["Trade_Date"].dt.strftime("%Y-%m")
    history["Month_First_Index_Close"] = history.groupby(["Index_Name", "Report_Month"])["Closing_Index_Value"].transform("first")
    history["Sector_Monthly_Change_Pct"] = (
        (history["Closing_Index_Value"] - history["Month_First_Index_Close"])
        / history["Month_First_Index_Close"].replace(0, pd.NA) * 100
    )
    history = history.rename(columns={"Trade_Date": "Sector_Index_Date"})
    return mapping, history[[
        "Sector_Index_Date", "Index_Name", "Closing_Index_Value",
        "Sector_Daily_Change_Pct", "Sector_Monthly_Change_Pct"
    ]]


def build_report():
    futures = load_futures_history()
    cash = load_cash_history()
    delivery = load_delivery()
    sector_mapping, sector_history = load_sector_data()

    if cash.empty:
        cash_fields = ["Trade_Date", "Symbol", "Open_Price", "High_Price", "Low_Price",
                       "Cash_Close", "ADR_14_Rs", "ADR_14_Pct"]
        cash_report = pd.DataFrame(columns=cash_fields)
    else:
        cash_report = cash.rename(columns={"Close_Price": "Cash_Close"})[
            ["Trade_Date", "Symbol", "Open_Price", "High_Price", "Low_Price",
             "Cash_Close", "ADR_14_Rs", "ADR_14_Pct"]
        ]

    report = futures.merge(cash_report, on=["Trade_Date", "Symbol"], how="left")
    report = report.merge(delivery, on=["Trade_Date", "Symbol"], how="left")
    report = report.merge(
        sector_mapping[["Symbol", "Industry", "Mapping_Status", "Sector_Index_Proxy", "Proxy_Type", "Proxy_Available"]],
        on="Symbol", how="left"
    )
    report = report.merge(
        sector_history,
        left_on=["Trade_Date", "Sector_Index_Proxy"],
        right_on=["Sector_Index_Date", "Index_Name"],
        how="left"
    )
    report["Sector_Daily_Change_Pct"] = pd.to_numeric(report["Sector_Daily_Change_Pct"], errors="coerce")
    report["Sector_Monthly_Change_Pct"] = pd.to_numeric(report["Sector_Monthly_Change_Pct"], errors="coerce")

    # Monthly cumulative comparisons restart on each calendar month.
    report["Report_Month"] = report["Trade_Date"].dt.strftime("%Y-%m")
    report = report.sort_values(["Report_Month", "Symbol", "Trade_Date"])
    grouped = report.groupby(["Report_Month", "Symbol"], sort=False)
    report["Month_First_Futures_Close"] = grouped["Close_Price"].transform("first")
    report["Month_First_Open_Interest"] = grouped["Open_Interest"].transform("first")
    report["Monthly_Price_Change_Pct"] = (
        (report["Close_Price"] - report["Month_First_Futures_Close"])
        / report["Month_First_Futures_Close"].replace(0, pd.NA) * 100
    )
    report["Monthly_OI_Change_Pct"] = (
        (report["Open_Interest"] - report["Month_First_Open_Interest"])
        / report["Month_First_Open_Interest"].replace(0, pd.NA) * 100
    )
    report["Monthly_Daily_Records"] = grouped["Trade_Date"].transform("count")

    # Preserve exact dates as ISO text for browser CSV parsing.
    report["Trade_Date"] = report["Trade_Date"].dt.strftime("%Y-%m-%d")
    report["Expiry_Date"] = report["Expiry_Date"].dt.strftime("%Y-%m-%d")
    for c in ["ADR_14_Rs", "ADR_14_Pct", "Delivery_Pct",
              "Monthly_Price_Change_Pct", "Monthly_OI_Change_Pct",
              "Sector_Daily_Change_Pct", "Sector_Monthly_Change_Pct"]:
        if c in report.columns:
            report[c] = pd.to_numeric(report[c], errors="coerce").round(4)

    # Keep report fields ordered and stable.
    columns = [
        "Report_Month", "Trade_Date", "Symbol", "Expiry_Date",
        "Close_Price", "Price_Change_Pct", "Open_Interest", "OI_Change_Pct",
        "Volume", "Cash_Close", "Open_Price", "High_Price", "Low_Price",
        "ADR_14_Rs", "ADR_14_Pct", "Delivery_Pct",
        "Monthly_Price_Change_Pct", "Monthly_OI_Change_Pct",
        "Monthly_Daily_Records", "Industry", "Mapping_Status",
        "Sector_Index_Proxy", "Proxy_Type", "Sector_Index_Date",
        "Sector_Daily_Change_Pct", "Sector_Monthly_Change_Pct",
    ]
    # Add daily classification when the classifier history is available.
    class_path = DATA / "oi_classification_history.csv"
    classification = read_csv_safe(class_path)
    if not classification.empty and {"Trade_Date", "Symbol"}.issubset(classification.columns):
        classification["Trade_Date"] = pd.to_datetime(classification["Trade_Date"], errors="coerce").dt.strftime("%Y-%m-%d")
        class_fields = ["Trade_Date", "Symbol"]
        for field in ["Price_Change_Pct", "OI_Change_Pct", "Classification"]:
            if field in classification.columns:
                class_fields.append(field)
        report = report.merge(
            classification[class_fields].drop_duplicates(
                ["Trade_Date", "Symbol"], keep="last"
            ),
            on=["Trade_Date", "Symbol"], how="left"
        )
        for field in ["Price_Change_Pct", "OI_Change_Pct", "Classification"]:
            if field in report.columns and field not in columns:
                columns.append(field)
    report = report[[c for c in columns if c in report.columns]]

    current_month = report["Report_Month"].max()
    if not current_month:
        raise RuntimeError("No current report month could be identified.")

    current = report[report["Report_Month"] == current_month].copy()
    current.to_csv(CURRENT_DIR / "current_cycle.csv", index=False)
    # Separate current F&O-universe delivery ranking (latest date + 20-session mean).
    build_delivery_universe_report(futures, delivery)

    archive_months = sorted(m for m in report["Report_Month"].dropna().unique() if m < current_month)
    for month in archive_months:
        month_dir = ARCHIVE_DIR / month
        month_dir.mkdir(parents=True, exist_ok=True)
        report[report["Report_Month"] == month].to_csv(month_dir / "stock_report.csv", index=False)

    manifest = {
        "current_month": current_month,
        "archive_months": archive_months,
        "current_file": "data/current_month/current_cycle.csv",
        "generated_at": pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d %H:%M:%S IST"),
        "adr_method": "Cash-market equity OHLC; rolling 14 trading sessions; prior-month sessions used as warm-up.",
        "backtesting": False,
    }
    (DATA / "report_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Current month report: {current_month} ({len(current)} rows)")
    print(f"Archived months refreshed: {len(archive_months)}")
    print("No backtesting performed.")


if __name__ == "__main__":
    build_report()
