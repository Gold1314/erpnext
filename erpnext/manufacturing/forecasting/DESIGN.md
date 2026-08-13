# Statistical Demand Forecasting

Replaces the `demand_qty = 1.0` scaffold in Sales Forecast with real
statistical forecasting (blueprint W4/4a), following the repo's pure-core +
adapter doctrine (section 2): the engine is Frappe-free, unit-testable
without a site, and reusable outside this doctype.

## Layout

| File | Role | Frappe? |
| --- | --- | --- |
| `models.py` | Dataclasses (`TimeSeries`, `ForecastPoint`, `ModelFit`, `BacktestResult`) + pure period arithmetic (Weekly = Monday buckets, Monthly = first-of-month buckets) | no |
| `engine.py` | Forecast models with `fit(history)` / `forecast(horizon)` | no |
| `backtest.py` | Rolling-origin evaluation, metrics, champion selection | no |
| `loaders.py` | DB access: per-item zero-filled demand history from SO/SI/DN | yes |
| `test_forecasting.py` | Pure unit tests, runnable as `python .../test_forecasting.py` (importlib self-bootstrap, same pattern as `erpnext/accounts/forecasting/test_engine.py`) | no |

No numpy/scipy — every model is a short closed-form recursion, keeping the
no-heavy-deps discipline. No ambient time in the pure core; "today" only
enters in Frappe-side callers.

## Models (`engine.py`)

- **Naive** — last value. Floor for champion selection; never infeasible.
- **Seasonal Naive** — value from one season ago (`season_length`).
- **Single Exponential Smoothing** — alpha.
- **Holt Linear** — alpha, beta, optional damping `phi` (default 1.0).
- **Holt-Winters Additive / Multiplicative** — alpha, beta, gamma,
  `season_length`; classic initialization (level = first-season mean,
  seasonal indices averaged per period across complete seasons, trend from
  the first two seasons). Multiplicative falls back to additive when the
  history contains non-positive values (`params["fallback"] = "additive"`).
- **Croston / Croston SBA** — intermittent demand: separate smoothing of
  non-zero demand sizes and inter-demand intervals; SBA applies the
  Syntetos-Boylan correction `× (1 − alpha/2)`.

Parameters left `None` are grid-searched over `{0.1, 0.3, 0.5, 0.7, 0.9}`
minimizing in-sample one-step-ahead MSE. Forecasts clamp negatives to 0.

## Backtesting (`backtest.py`)

Rolling-origin, expanding window, one-step-ahead: the last *k* periods
(default ≈ n/4, clamped to [2, 8]) are held out one origin at a time. Folds
whose training window is too short for a model are skipped; `n_folds`
reports what was actually scored.

Metrics (error = forecast − actual, so positive bias = over-forecasting):

- **MAPE** (%) over folds with non-zero actuals; if every holdout actual is
  zero, a MAD-based WMAPE fallback anchors on the mean non-zero |history|.
- **bias** = mean error / mean actual (fraction).
- **MAD**, **RMSE** in demand units.

`select_champion(history, season_length, candidate_models, horizon, holdout)`
returns `(champion ModelFit refitted on the full history, all
BacktestResults)`, choosing lowest MAPE with a lower-|bias| stability
tiebreak. Intermittency auto-classification: > 40% zero periods restricts
candidates to Croston / Croston SBA / Naive. `fit_named_model` serves the
non-Auto path and degrades gracefully (HW → Holt → SES → Naive) when the
history can't support the requested model (seasonal models need two full
seasons).

## Data source (`loaders.py`)

Mirrors the Exponential Smoothing Forecasting report: aggregate child-table
`stock_qty` of **submitted** Sales Orders (`transaction_date`), Sales
Invoices (`posting_date`), or Delivery Notes (`posting_date`), filtered by
company and (group-)warehouse via `get_child_warehouses`. Output is a
per-item `TimeSeries` **zero-filled over every period** in the requested
window — required for correct intermittency detection and seasonal indexing.

## Sales Forecast integration

`generate_demand()` branches on the new `generation_method` field. Manual is
byte-for-byte the old behavior. Statistical loads history (defaults: 24
months before `from_date` up to `from_date`; season length 12 Monthly / 52
Weekly), runs `select_champion` (or the pinned `forecast_model`) per item,
and appends child rows using the same delivery-date convention as manual
(`from_date + n` periods), stamping `forecast_model` / `mape` / `bias` on
each row. Items with under 3 history periods or all-zero history are listed
in a `frappe.msgprint` and skipped.

Rows stay 100% MPS/MRP-compatible: `create_mps`, `get_sales_forecast_data`,
and the MRP report only read `item_code`, `item_name`, `uom`,
`delivery_date`, `demand_qty` (+ `parentfield = "items"`), all of which are
populated exactly as before; the three new columns are additive.

## Forecast Accuracy report

`manufacturing/report/forecast_accuracy`: scores submitted forecast rows
whose period has fully elapsed against actual demand from the same loaders
(one query per frequency × warehouse group). Detail rows show forecast vs
actual, absolute error, and APE; a bold per-item summary row carries MAPE
and bias; the chart plots total forecast vs actual per period.
