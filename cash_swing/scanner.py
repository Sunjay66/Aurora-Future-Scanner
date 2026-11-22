import json, os, warnings
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
MAX_HOLDING_SESSIONS = 20

# Completed historical sample: 1.25 ATR / 2R / bearish MACD exit.
# trades, hard-stop %, MACD-exit %, 2R achieved %, average realised R.
HISTORICAL_ATR_2R = {
    "APOLLOHOSP": (15, 33.3, 26.7, 40.0, 0.4), "ICICIBANK": (15, 46.7, 20.0, 33.3, 0.1),
    "ONGC": (14, 71.4, 0.0, 28.6, -0.1), "BHEL": (12, 50.0, 25.0, 16.7, -0.2),
    "TCS": (19, 42.1, 10.5, 47.4, 0.5), "LAURUSLABS": (18, 38.9, 22.2, 38.9, 0.4),
    "BSE": (15, 60.0, 13.3, 26.7, -0.2), "EICHERMOT": (18, 33.3, 22.2, 38.9, 0.4),
    "BAJFINANCE": (16, 43.8, 6.2, 50.0, 0.5), "TATASTEEL": (14, 35.7, 7.1, 50.0, 0.7),
    "LT": (15, 20.0, 26.7, 53.3, 0.9), "SUNPHARMA": (14, 28.6, 7.1, 50.0, 0.9),
    "INFY": (16, 62.5, 6.2, 31.2, -0.3), "MCX": (15, 46.7, 6.7, 46.7, 0.3),
    "M&M": (18, 61.1, 5.6, 33.3, 0.0), "TVSMOTOR": (13, 46.2, 30.8, 23.1, -0.2),
    "SBIN": (15, 26.7, 20.0, 53.3, 0.8), "HDFCBANK": (16, 37.5, 6.2, 50.0, 0.8),
    "CHOLAFIN": (17, 47.1, 5.9, 47.1, 0.4), "SHRIRAMFIN": (13, 61.5, 30.8, 7.7, -0.6),
}

def get_universe():
    r = requests.get(NIFTY200_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    u = pd.read_csv(StringIO(r.text))
    u["Symbol"] = u["Symbol"].astype(str).str.strip().str.upper()
    u = u[u["Symbol"].notna() & (u["Symbol"] != "") & (u["Symbol"] != "NAN")]
    u = u.drop_duplicates("Symbol").copy()
    if len(u) < 180:
        raise RuntimeError(f"Incomplete Nifty 200 universe: {len(u)} symbols")
    u["Yahoo_Symbol"] = u["Symbol"] + ".NS"
    return u

def calculate_indicators(df):
    d = df.copy().sort_index()
    c, h, l = d["Close"], d["High"], d["Low"]
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    d["MACD"] = macd
    d["SIGNAL"] = macd.ewm(span=9, adjust=False).mean()
    d["HIST"] = d["MACD"] - d["SIGNAL"]
    pc = c.shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    d["ATR14"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    ret = c.pct_change()
    for p in (20, 60, 120, 252):
        d[f"HV{p}"] = ret.rolling(p).std() * np.sqrt(252)
    d["HV20_RUPEES"] = c * d["HV20"] / np.sqrt(252)
    d["HV60_RUPEES"] = c * d["HV60"] / np.sqrt(252)
    cross_up = (d["MACD"] > d["SIGNAL"]) & (d["MACD"].shift(1) <= d["SIGNAL"].shift(1)) & (d["MACD"] < 0) & (d["SIGNAL"] < 0)
    three_green = (d["HIST"] > 0) & (d["HIST"].shift(1) > 0) & (d["HIST"].shift(2) > 0)
    d["ENTRY_SIGNAL"] = cross_up.shift(2).fillna(False).astype(bool) & three_green
    d["BEARISH_EXIT"] = (d["MACD"] < d["SIGNAL"]) & (d["MACD"].shift(1) >= d["SIGNAL"].shift(1))
    return d

def n(v, digits=2):
    try:
        v = float(v)
        return round(v, digits) if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None

def build_lifecycle(symbol, d, company):
    """Two parallel hypothetical stop models; one active trade per stock/model at a time."""
    signals = np.flatnonzero(d["ENTRY_SIGNAL"].fillna(False).to_numpy())
    results = []
    for model in ("1.25 ATR14", "2 x HV60 daily rupee move"):
        next_free = 0
        for sig_pos in signals:
            entry_pos = sig_pos + 1
            if entry_pos >= len(d) or entry_pos < next_free:
                continue
            sig = d.iloc[sig_pos]
            entry = float(d.iloc[entry_pos]["Open"])
            atr = float(sig["ATR14"])
            hv60 = float(sig["HV60"])
            if not all(np.isfinite(x) and x > 0 for x in (entry, atr, hv60)):
                continue
            risk = 1.25 * atr if model == "1.25 ATR14" else 2.0 * (entry * hv60 / np.sqrt(252))
            stop = entry - risk
            target2, target3 = entry + 2*risk, entry + 3*risk
            risk_budget = PORTFOLIO_CAPITAL * RISK_PER_TRADE_PCT / 100
            capital_cap = PORTFOLIO_CAPITAL * MAX_STOCK_ALLOCATION_PCT / 100
            qty = max(0, min(int(risk_budget // risk), int(capital_cap // entry))) if risk > 0 else 0

            exit_type, exit_date, exit_price = "OPEN", None, None
            exit_signal_date = None
            last_pos = min(entry_pos + MAX_HOLDING_SESSIONS - 1, len(d)-1)
            target2_hit = target3_hit = False
            mfe_price = entry
            for j in range(entry_pos, last_pos + 1):
                bar = d.iloc[j]
                o, hi, lo, cl = map(float, (bar["Open"], bar["High"], bar["Low"], bar["Close"]))
                mfe_price = max(mfe_price, hi)
                # Gap exits at open take priority.
                if o <= stop:
                    exit_type, exit_date, exit_price = "HARD STOP (GAP)", pd.Timestamp(d.index[j]), o
                    break
                if o >= target3:
                    exit_type, exit_date, exit_price = "3R TARGET (GAP)", pd.Timestamp(d.index[j]), o
                    target2_hit = target3_hit = True
                    break
                if o >= target2:
                    target2_hit = True
                # Conservative daily-bar assumption: if stop and target touch same bar, stop first.
                if lo <= stop:
                    exit_type, exit_date, exit_price = "HARD STOP", pd.Timestamp(d.index[j]), stop
                    break
                if hi >= target3:
                    target2_hit = target3_hit = True
                    exit_type, exit_date, exit_price = "3R TARGET", pd.Timestamp(d.index[j]), target3
                    break
                if hi >= target2:
                    target2_hit = True
                # MACD crossover is known at close; execution is next session open.
                if bool(bar["BEARISH_EXIT"]):
                    exit_signal_date = pd.Timestamp(d.index[j])
                    if j + 1 < len(d):
                        exit_type, exit_date = "MACD EXIT", pd.Timestamp(d.index[j+1])
                        exit_price = float(d.iloc[j+1]["Open"])
                    else:
                        exit_type = "MACD EXIT — NEXT OPEN PENDING"
                    break
                if j == last_pos:
                    if last_pos + 1 < len(d):
                        exit_type, exit_date = "20-SESSION TIME EXIT", pd.Timestamp(d.index[last_pos])
                        exit_price = cl
                    else:
                        exit_type = "OPEN — 20-SESSION WINDOW NOT COMPLETE"

            if exit_type.startswith("OPEN") or exit_type.endswith("PENDING"):
                status = "OPEN POSITION"
                next_free = len(d)  # current live position blocks another signal
            else:
                status = "CLOSED"
                next_free = (d.index.get_loc(exit_date) + 1) if exit_date in d.index else last_pos + 1

            pnl_pct = ((exit_price / entry) - 1) * 100 if exit_price is not None else None
            realised_r = ((exit_price - entry) / risk) if exit_price is not None and risk else None
            latest_close = float(d.iloc[-1]["Close"])
            current_pnl_pct = ((latest_close / entry) - 1) * 100
            latest_date = pd.Timestamp(d.index[-1])
            age = len(d) - 1 - entry_pos

            results.append({
                "symbol": symbol, "company": company, "stop_model": model,
                "signal_date": pd.Timestamp(d.index[sig_pos]).strftime("%Y-%m-%d"),
                "entry_date": pd.Timestamp(d.index[entry_pos]).strftime("%Y-%m-%d"),
                "entry_price": n(entry), "risk_per_share": n(risk), "atr14_at_signal": n(atr),
                "hv60_annualised_pct_at_signal": n(hv60*100),
                "hv60_daily_rupee_at_entry": n(entry*hv60/np.sqrt(252)),
                "stop_loss": n(stop), "target_2r": n(target2), "target_3r": n(target3),
                "quantity": qty, "capital_required": n(qty*entry), "planned_risk": n(qty*risk),
                "latest_market_date": latest_date.strftime("%Y-%m-%d"),
                "latest_close": n(latest_close), "unrealised_return_pct": n(current_pnl_pct),
                "sessions_held": int(age), "target_2r_touched": bool(target2_hit),
                "target_3r_touched": bool(target3_hit),
                "exit_signal_date": exit_signal_date.strftime("%Y-%m-%d") if exit_signal_date is not None else None,
                "exit_date": exit_date.strftime("%Y-%m-%d") if exit_date is not None else None,
                "exit_price": n(exit_price), "exit_type": exit_type, "status": status,
                "realised_return_pct": n(pnl_pct), "realised_r": n(realised_r),
            })
    return results

def main():
    os.makedirs("cash_swing/data", exist_ok=True)
    os.makedirs("cash_swing/reports", exist_ok=True)
    universe = get_universe()
    symbols = universe["Yahoo_Symbol"].tolist()
    print(f"Downloading daily data for {len(symbols)} Nifty 200 stocks...")
    raw = yf.download(tickers=symbols, period="3y", interval="1d", auto_adjust=True,
                      group_by="ticker", threads=True, progress=False)
    if raw is None or raw.empty:
        raise RuntimeError("Yahoo Finance returned no daily market data.")

    name_col = "Company Name" if "Company Name" in universe.columns else None
    names = dict(zip(universe["Symbol"], universe[name_col] if name_col else universe["Symbol"]))
    failures, latest_dates, current_recommendations, lifecycles = [], [], [], []

    for ticker in symbols:
        sym = ticker[:-3]
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                if ticker not in raw.columns.get_level_values(0):
                    failures.append(sym); continue
                frame = raw[ticker].copy()
            else:
                frame = raw.copy()
            needed = ["Open", "High", "Low", "Close", "Volume"]
            if not all(c in frame.columns for c in needed):
                failures.append(sym); continue
            frame = frame[needed].dropna(subset=["Open","High","Low","Close"])
            if len(frame) < 300:
                failures.append(sym); continue
            d = calculate_indicators(frame)
            latest = d.iloc[-1]
            market_date = pd.Timestamp(d.index[-1]).strftime("%Y-%m-%d")
            latest_dates.append(market_date)

            # Current confirmed signal recommendations, provisional until next open.
            if bool(latest["ENTRY_SIGNAL"]):
                ref = float(latest["Close"]); atr = float(latest["ATR14"]); hv = float(latest["HV60"])
                hist = HISTORICAL_ATR_2R.get(sym)
                for model, risk in (("1.25 ATR14", 1.25*atr), ("2 x HV60 daily rupee move", 2*ref*hv/np.sqrt(252))):
                    qty = max(0, min(int((PORTFOLIO_CAPITAL*RISK_PER_TRADE_PCT/100)//risk),
                                     int((PORTFOLIO_CAPITAL*MAX_STOCK_ALLOCATION_PCT/100)//ref))) if risk > 0 else 0
                    current_recommendations.append({
                        "symbol": sym, "company": str(names.get(sym,sym)), "signal_date": market_date,
                        "reference_close": n(ref), "entry_price_status": "Provisional; next session actual open required",
                        "stop_model": model, "atr14": n(atr), "hv60_annualised_pct": n(hv*100),
                        "hv60_daily_rupee_move": n(ref*hv/np.sqrt(252)),
                        "stop_loss_provisional": n(ref-risk), "target_2r_provisional": n(ref+2*risk),
                        "target_3r_provisional": n(ref+3*risk), "quantity_provisional": qty,
                        "capital_required_provisional": n(qty*ref),
                        "historical_sample_trades_atr_2r": hist[0] if hist else None,
                        "historical_atr_stop_hit_pct": hist[1] if hist else None,
                        "historical_macd_exit_pct": hist[2] if hist else None,
                        "historical_atr_2r_achieved_pct": hist[3] if hist else None,
                        "historical_atr_average_realised_r": hist[4] if hist else None,
                        "historical_3r_stock_specific": "Not established",
                        "historical_hv60_stock_specific": "Not established"
                    })

            lifecycles.extend(build_lifecycle(sym, d, str(names.get(sym,sym))))
        except Exception as exc:
            print(f"Skipped {sym}: {exc}")
            failures.append(sym)

    latest_market_date = max(latest_dates) if latest_dates else None
    lifecycle_df = pd.DataFrame(lifecycles)
    if not lifecycle_df.empty:
        lifecycle_df = lifecycle_df.sort_values(["entry_date","symbol","stop_model"])
    open_positions = lifecycle_df[lifecycle_df["status"] == "OPEN POSITION"].to_dict("records") if not lifecycle_df.empty else []
    closed_trades = lifecycle_df[lifecycle_df["status"] == "CLOSED"].to_dict("records") if not lifecycle_df.empty else []
    macd_exits = lifecycle_df[lifecycle_df["exit_type"].str.startswith("MACD EXIT")].to_dict("records") if not lifecycle_df.empty else []
    stop_exits = lifecycle_df[lifecycle_df["exit_type"].str.startswith("HARD STOP")].to_dict("records") if not lifecycle_df.empty else []
    recent_closed = [x for x in closed_trades if x.get("exit_date") and latest_market_date and x["exit_date"] >= (pd.Timestamp(latest_market_date)-pd.Timedelta(days=30)).strftime("%Y-%m-%d")]

    payload = {
        "scanner":"Aurora Cash Swing Scanner","version":"1.1","universe":"Nifty 200",
        "market_date":latest_market_date,"generated_at_ist":datetime.now(IST).isoformat(),
        "stocks_in_universe":len(universe),"stocks_scanned":len(universe)-len(set(failures)),
        "failed_symbols":sorted(set(failures)),
        "qualifying_stock_count":len(set(x["symbol"] for x in current_recommendations)),
        "trade_plan_count":len(current_recommendations),"recommendations":current_recommendations,
        "lifecycle_summary":{
            "open_position_models":len(open_positions),"macd_exit_models":len(macd_exits),
            "hard_stop_exit_models":len(stop_exits),"closed_trade_models":len(closed_trades)
        },
        "open_positions":open_positions,
        "macd_exit_alerts":macd_exits[-100:],
        "hard_stop_alerts":stop_exits[-100:],
        "recent_closed_trades":recent_closed[-200:],
        "capital_settings":{"portfolio_capital":PORTFOLIO_CAPITAL,"risk_per_trade_pct":RISK_PER_TRADE_PCT,
                            "max_stock_allocation_pct":MAX_STOCK_ALLOCATION_PCT},
        "entry_rule":"MACD 12,26,9 bullish crossover below zero; histogram positive for three consecutive sessions; confirmation at third positive-histogram close; execution next session open.",
        "exit_rule":"Hard stop, 2R/3R target, bearish MACD crossover at next session open, or 20-session time exit.",
        "lifecycle_note":"Two stop models are shown as parallel alternatives, not two simultaneous real positions. Daily OHLC ambiguity where stop and target are touched in the same candle is resolved conservatively in favour of the stop."
    }
    with open("cash_swing/data/latest.json","w",encoding="utf-8") as f: json.dump(payload,f,indent=2,ensure_ascii=False)
    universe.to_csv("cash_swing/data/nifty200_universe.csv",index=False)
    lifecycle_df.to_csv("cash_swing/reports/trade_lifecycle_history.csv",index=False)
    pd.DataFrame(current_recommendations).to_csv("cash_swing/reports/recommendations.csv",index=False)
    print("AURORA CASH SWING SCAN + LIFECYCLE MONITOR COMPLETE")
    print(f"Market date: {latest_market_date} | Universe: {len(universe)} | Scanned: {payload['stocks_scanned']} | Failed: {len(set(failures))}")
    print(f"Current qualifying stocks: {payload['qualifying_stock_count']}")
    print(f"Open position models: {len(open_positions)} | MACD exits: {len(macd_exits)} | Hard-stop exits: {len(stop_exits)}")
    print("Outputs: cash_swing/data/latest.json and cash_swing/reports/trade_lifecycle_history.csv")

if __name__ == "__main__":
    main()
