# Aurora Cash Swing Scanner

Independent cash-equity swing scanner module within the Aurora repository.

- Universe: official Nifty 200 constituent CSV.
- Data: 3 years of daily adjusted OHLCV from Yahoo Finance.
- Entry: MACD 12,26,9 bullish crossover below zero; histogram positive for three consecutive sessions; confirmation at the third positive-histogram close.
- Execution: next trading session open.
- Stop alternatives: 1.25 × ATR14 and 2 × HV60 daily rupee movement.
- Targets: 2R and 3R.
- Exit: hard stop, bearish MACD crossover at next session open, or 20-session time exit.
- Position sizing: configurable portfolio capital, risk percentage and per-stock capital cap.

Evening scan figures are provisional until the next session opening price is available. The dashboard must not present the prior close as an executed entry.

Historical stock-specific statistics are currently available only for the 20-stock ATR/2R sample. HV60 and stock-specific 3R history are labelled not established rather than inferred.
