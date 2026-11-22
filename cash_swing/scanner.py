import json
import os
import time
import warnings
from datetime import datetime
from io import StringIO
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")
IST = ZoneInfo("Asia/Kolkata")
NIFTY200_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.niftyindices.com/"}

PORTFOLIO_CAPITAL = float(os.getenv("CASH_PORTFOLIO_CAPITAL", "1000000"))
RISK_PER_TRADE_PCT = float(os.getenv("CASH_RISK_PER_TRADE_PCT", "1.0"))
MAX_STOCK_ALLOCATION_PCT = float(os.getenv("CASH_MAX_STOCK_ALLOCATION_PCT", "20.0"))

# Historical sample: 1.25 ATR stop, 2R target, bearish MACD exit.
# Tuple: trades, hard-stop %, MACD-exit %, 2R achieved %, average realised R.
HISTORICAL_ATR_2R = {
    "APOLLOHOSP": (15, 33.3, 26.7, 40.0, 0.4),
    "ICICIBANK": (15, 46.7, 20.0, 33.3, 0.1),
    "ONGC": (14, 71.4, 0.0, 28.6, -0.1),
    "BHEL": (12, 50.0, 25.0, 16.7, -0.2),
    "TCS": (19, 42.1, 10.5, 47.4, 0.5),
    "LAURUSLABS": (18, 38.9, 22.2, 38.9, 0.4),
    "BSE": (15, 60.0, 13.3, 26.7, -0.2),
    "EICHERMOT": (18, 33.3, 22.2, 38.9, 0.4),
    "BAJFINANCE": (16, 43.8, 6.2, 50.0, 0.5),
    "TATASTEEL": (14, 35.7, 7.1, 50.0, 0.7),
    "LT": (15, 20.0, 26.7, 53.3, 0.9),
    "SUNPHARMA": (14, 28.6, 7.1, 50.0, 0.9),
    "INFY": (16, 62.5, 6.2, 31.2, -0.3),
    "MCX": (15, 46.7, 6.7, 46.7, 0.3),
    "M&M": (18, 61.1, 5.6, 33.3, 0.0),
    "TVSMOTOR": (13, 46.2, 30.8, 23.1, -0.2),
    "SBIN": (15, 26.7, 20.0, 53.3, 0.8),
    "HDFCBANK": (16, 37.5, 6.2, 50.0, 0.8),
    "CHOLAFIN": (17, 47.1, 5.9, 47.1, 0.4),
    "SHRIRAMFIN": (13, 61.5, 30.8, 7.7, -0.6),
}

def get_universe():
    response = requests.get(NIFTY200_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    universe = pd.read_csv(StringIO(response.text))
    universe["Symbol"] = universe["Symbol"].astype(str).str.strip().str.upper()
    universe = universe[universe["Symbol"].notna() & (universe["Symbol"] != "") & (universe["Symbol"] != "NAN")]
    universe = universe.drop_duplicates("Symbol").copy()
    if len(universe) < 180:
        raise RuntimeError(f"Nifty 200 constituent file returned only {len(universe)} symbols; refusing incomplete scan.")
    universe["Yahoo_Symbol"] = universe["Symbol"] + ".NS"
    return universe

def calculate_indicators(df):
    df = df.copy().sort_index()
    close, high, low = df["Close"], df["High"], df["Low"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["SIGNAL"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["HIST"] = df["MACD"] - df["SIGNAL"]

    prev_close = close.shift(1)
    tr = pd.concat([(high-low), (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    df["ATR14"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()

    returns = close.pct_change()
    for period in (20, 60, 120, 252):
        df[f"HV{period}"] = returns.rolling(period).std() * np.sqrt(252)
    df["HV20_RUPEES"] = close * df["HV20"] / np.sqrt(252)
    df["HV60_RUPEES"] = close * df["HV60"] / np.sqrt(252)

    crossover = (
        (df["MACD"] > df["SIGNAL"]) &
        (df["MACD"].shift(1) <= df["SIGNAL"].shift(1)) &
        (df["MACD"] < 0) & (df["SIGNAL"] < 0)
    )
    three_positive = (df["HIST"] > 0) & (df["HIST"].shift(1) > 0) & (df["HIST"].shift(2) > 0)
    # Signal is confirmed on the third positive-histogram close;
    # the bullish crossover occurred two sessions earlier.
    df["ENTRY_SIGNAL"] = crossover.shift(2).fillna(False).astype(bool) & three_positive
    df["BEARISH_EXIT"] = (
        (df["MACD"] < df["SIGNAL"]) &
        (df["MACD"].shift(1) >= df["SIGNAL"].shift(1))
    )
    return df

def clean(value, digits=2):
    if pd.isna(value) or not np.isfinite(float(value)):
        return None
    return round(float(value), digits)

def main():
    os.makedirs("cash_swing/data", exist_ok=True)
    os.makedirs("cash_swing/reports", exist_ok=True)

    universe = get_universe()
    symbols = universe["Yahoo_Symbol"].tolist()
    print(f"Downloading daily data for {len(symbols)} Nifty 200 stocks...")

    raw = yf.download(
        tickers=symbols,
        period="3y",
        interval="1d",
        auto_adjust=True,
        group_by="ticker",
        threads=True,
        progress=False
    )
    if raw is None or raw.empty:
        raise RuntimeError("Yahoo Finance returned no daily market data.")

    name_map = dict(zip(universe["Symbol"], universe.get("Company Name", universe["Symbol"])))
    records = []
    failures = []
    market_dates = []

    for ticker in symbols:
        symbol = ticker.replace(".NS", "")
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                if ticker not in raw.columns.get_level_values(0):
                    failures.append(symbol)
                    continue
                frame = raw[ticker].copy()
            else:
                frame = raw.copy()

            required = ["Open", "High", "Low", "Close", "Volume"]
            if not all(col in frame.columns for col in required):
                failures.append(symbol)
                continue
            frame = frame[required].dropna(subset=["Open", "High", "Low", "Close"])
            if len(frame) < 300:
                failures.append(symbol)
                continue

            frame = calculate_indicators(frame)
            latest = frame.iloc[-1]
            signal_date = pd.Timestamp(frame.index[-1]).strftime("%Y-%m-%d")
            market_dates.append(signal_date)

            if not bool(latest["ENTRY_SIGNAL"]):
                continue

            ref_close = float(latest["Close"])
            atr = float(latest["ATR14"])
            hv60_rupees = float(latest["HV60_RUPEES"])
            if not all(np.isfinite(x) and x > 0 for x in (ref_close, atr, hv60_rupees)):
                continue

            historical = HISTORICAL_ATR_2R.get(symbol)
            for model, stop_distance in (
                ("1.25 ATR14", 1.25 * atr),
                ("2 x HV60 daily rupee move", 2.0 * hv60_rupees),
            ):
                risk_per_share = stop_distance
                provisional_stop = ref_close - stop_distance
                risk_budget = PORTFOLIO_CAPITAL * RISK_PER_TRADE_PCT / 100.0
                capital_cap = PORTFOLIO_CAPITAL * MAX_STOCK_ALLOCATION_PCT / 100.0
                qty_by_risk = int(risk_budget // risk_per_share)
                qty_by_capital = int(capital_cap // ref_close)
                quantity = max(0, min(qty_by_risk, qty_by_capital))

                records.append({
                    "symbol": symbol,
                    "company": str(name_map.get(symbol, symbol)),
                    "signal_date": signal_date,
                    "reference_close": clean(ref_close),
                    "entry_price_status": "Provisional; replace with next session actual open",
                    "entry_price": None,
                    "stop_model": model,
                    "atr14": clean(atr),
                    "hv60_annualised_pct": clean(float(latest["HV60"]) * 100),
                    "hv60_daily_rupee_move": clean(hv60_rupees),
                    "stop_loss_provisional": clean(provisional_stop),
                    "risk_per_share_provisional": clean(risk_per_share),
                    "target_2r_provisional": clean(ref_close + 2 * risk_per_share),
                    "target_3r_provisional": clean(ref_close + 3 * risk_per_share),
                    "quantity_provisional": quantity,
                    "capital_required_provisional": clean(quantity * ref_close),
                    "planned_risk_provisional": clean(quantity * risk_per_share),
                    "historical_sample_trades_atr_2r": historical[0] if historical else None,
                    "historical_atr_stop_hit_pct": historical[1] if historical else None,
                    "historical_macd_exit_pct": historical[2] if historical else None,
                    "historical_atr_2r_achieved_pct": historical[3] if historical else None,
                    "historical_atr_average_realised_r": historical[4] if historical else None,
                    "historical_3r_stock_specific": "Not established",
                    "historical_hv60_stock_specific": "Not established",
                })
        except Exception as exc:
            print(f"Skipped {symbol}: {exc}")
            failures.append(symbol)

    latest_market_date = max(market_dates) if market_dates else None
    generated = datetime.now(IST).isoformat()
    recommendations = sorted(records, key=lambda x: (x["symbol"], x["stop_model"]))

    payload = {
        "scanner": "Aurora Cash Swing Scanner",
        "version": "1.0",
        "universe": "Nifty 200",
        "market_date": latest_market_date,
        "generated_at_ist": generated,
        "stocks_in_universe": len(universe),
        "stocks_scanned": len(universe) - len(set(failures)),
        "failed_symbols": sorted(set(failures)),
        "qualifying_stock_count": len(set(r["symbol"] for r in recommendations)),
        "trade_plan_count": len(recommendations),
        "capital_settings": {
            "portfolio_capital": PORTFOLIO_CAPITAL,
            "risk_per_trade_pct": RISK_PER_TRADE_PCT,
            "max_stock_allocation_pct": MAX_STOCK_ALLOCATION_PCT,
        },
        "entry_rule": "MACD 12,26,9 bullish crossover below zero; histogram positive for three consecutive sessions; entry confirmation at third positive-histogram close; execution next session open.",
        "exit_rule": "Hard stop or bearish MACD crossover at next session open; maximum holding 20 trading sessions.",
        "recommendations": recommendations,
    }

    with open("cash_swing/data/latest.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    pd.DataFrame(recommendations).to_csv("cash_swing/reports/recommendations.csv", index=False)
    universe.to_csv("cash_swing/data/nifty200_universe.csv", index=False)

    print("AURORA CASH SWING SCAN COMPLETE")
    print(f"Market date: {latest_market_date}")
    print(f"Universe: {len(universe)} | Scanned: {payload['stocks_scanned']} | Failed: {len(set(failures))}")
    print(f"Qualifying stocks: {payload['qualifying_stock_count']} | Trade plans: {len(recommendations)}")
    print("Output: cash_swing/data/latest.json")

if __name__ == "__main__":
    main()
