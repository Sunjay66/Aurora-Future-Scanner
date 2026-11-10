"""
Refresh combined cash-market FII/FPI and DII flows.

Primary source: MSEI's official combined NSE+BSE+MSEI activity page.
The script accepts data only when both FII/FPI and DII rows are present
for the same report date and Buy - Sell reconciles to Net within rounding.
It is idempotent and leaves the existing CSV untouched on any failure.
"""
from datetime import datetime
from io import StringIO
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "institutional_data" / "cash_market.csv"
SOURCE_URL = "https://beta.msei.in/downloads/equity-reports/fii-dii-activities"
SOURCE_LABEL = "NSE+BSE+MSEI"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.msei.in/",
}
COLUMNS = [
    "Trade_Date", "Session_Type", "Category", "Buy_Value_Cr",
    "Sell_Value_Cr", "Net_Value_Cr", "Source",
]


def norm(value):
    return re.sub(r"[^a-z]", "", str(value).lower())


def parse_number(value):
    text = str(value).strip().replace(",", "").replace("₹", "").replace("Rs.", "")
    text = text.replace("−", "-")
    number = pd.to_numeric(text, errors="coerce")
    if pd.isna(number):
        raise ValueError(f"Invalid numeric value in combined report: {value!r}")
    return float(number)


def parse_report(html):
    tables = pd.read_html(StringIO(html))
    extracted = []
    for table in tables:
        frame = table.copy()
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = [
                " ".join(str(part) for part in col if str(part) != "nan")
                for col in frame.columns
            ]
        else:
            frame.columns = [str(col) for col in frame.columns]

        lookup = {norm(col): col for col in frame.columns}
        def find_column(*needles):
            for key, original in lookup.items():
                if any(needle in key for needle in needles):
                    return original
            return None

        category_col = find_column("category")
        date_col = find_column("date")
        buy_col = find_column("buyvalue")
        sell_col = find_column("sellvalue")
        net_col = find_column("netvalue")
        if not all([category_col, date_col, buy_col, sell_col, net_col]):
            continue

        for _, row in frame.iterrows():
            category_raw = str(row[category_col]).strip().upper()
            if category_raw in {"FII/FPI", "FII", "FPI"}:
                category = "FII/FPI"
            elif category_raw == "DII":
                category = "DII"
            else:
                continue
            date_value = pd.to_datetime(str(row[date_col]).strip(), dayfirst=True, errors="coerce")
            if pd.isna(date_value):
                continue
            extracted.append({
                "Trade_Date": date_value.strftime("%Y-%m-%d"),
                "Category": category,
                "Buy_Value_Cr": parse_number(row[buy_col]),
                "Sell_Value_Cr": parse_number(row[sell_col]),
                "Net_Value_Cr": parse_number(row[net_col]),
            })

    if not extracted:
        raise ValueError("No combined FII/FPI and DII activity rows found on MSEI page.")

    # Page may render the same data in multiple tables; retain one row per date/category.
    unique = {}
    for item in extracted:
        unique[(item["Trade_Date"], item["Category"])] = item
    latest_date = max(key[0] for key in unique)
    latest = [item for (d, _), item in unique.items() if d == latest_date]
    by_category = {item["Category"]: item for item in latest}
    if set(by_category) != {"FII/FPI", "DII"}:
        raise ValueError(f"Combined report for {latest_date} does not contain both FII/FPI and DII.")

    for item in latest:
        if abs((item["Buy_Value_Cr"] - item["Sell_Value_Cr"]) - item["Net_Value_Cr"]) > 0.02:
            raise ValueError(f"Buy minus sell does not reconcile for {item['Category']} on {latest_date}.")

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    report_day = datetime.strptime(latest_date, "%Y-%m-%d").date()
    if (today - report_day).days > 7 or report_day > today:
        raise ValueError(f"Combined report date {latest_date} is stale or in the future.")

    return [
        {
            "Trade_Date": latest_date,
            "Session_Type": "Regular",
            "Category": item["Category"],
            "Buy_Value_Cr": f'{item["Buy_Value_Cr"]:.2f}',
            "Sell_Value_Cr": f'{item["Sell_Value_Cr"]:.2f}',
            "Net_Value_Cr": f'{item["Net_Value_Cr"]:.2f}',
            "Source": SOURCE_LABEL,
        }
        for item in sorted(latest, key=lambda x: x["Category"])
    ]


def update_history(new_rows):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    if OUTPUT.exists():
        old = pd.read_csv(OUTPUT, dtype=str).fillna("")
        for col in COLUMNS:
            if col not in old.columns:
                old[col] = ""
        old = old[COLUMNS]
    else:
        old = pd.DataFrame(columns=COLUMNS)

    report_date = new_rows[0]["Trade_Date"]
    # Replace same-date rows so a corrected provisional report is reflected on rerun.
    old = old[old["Trade_Date"].astype(str) != report_date]
    combined = pd.concat([old, pd.DataFrame(new_rows)[COLUMNS]], ignore_index=True)
    combined = combined.sort_values("Trade_Date", ascending=False, kind="stable")
    combined.to_csv(OUTPUT, index=False)


def main():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        response = session.get(SOURCE_URL, timeout=40)
        response.raise_for_status()
        rows = parse_report(response.text)
        update_history(rows)
        print(
            f"SUCCESS: combined cash-market data updated through {rows[0]['Trade_Date']} "
            f"({len(rows)} participant rows) from {SOURCE_URL}"
        )
        for row in rows:
            print(
                f'{row["Category"]}: buy={row["Buy_Value_Cr"]}, '
                f'sell={row["Sell_Value_Cr"]}, net={row["Net_Value_Cr"]} ₹ Cr'
            )
        return 0
    except Exception as exc:
        print(f"Cash-market refresh rejected; existing CSV preserved: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
