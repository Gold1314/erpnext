# Semantic Metric Layer + KPI Packs - Design

Status: **v1 implemented** - pure-core catalog + engine, Frappe loaders,
whitelisted API, Metric Definition / Metric Snapshot doctypes and the
"KPI Scorecard" script report. Blueprint reference:
`GAP_CLOSURE_BLUEPRINT.md` section **W7 - Analytics** ("Metric doctype
(measure, dimensions, grain, ...) with a governed catalog; shipped KPI packs
per module (DSO, DPO, inventory turns, OTIF, forecast accuracy, close
duration)").

## 1. Problem

Oracle OTBI / Fusion Data Intelligence sells a *governed* semantic layer:
business metrics defined once, centrally, so "DSO" means one thing in a
dashboard, a report and an executive's slide. ERPNext has plenty of reports,
but each recomputes its own numbers - the Financial Ratios report has its own
notion of net sales, the AR report its own ageing, a customisation its own
DSO. Nothing declares what a metric *is*, and nothing stops two answers from
disagreeing.

This module is that missing catalog: 24 metrics, each declared once as data
(unit, grain, direction, category, formula, named inputs), one pure engine
that evaluates them, one set of loaders that supplies the inputs from the same
tables the statements read, and one governance doctype for thresholds.

## 2. Architecture

**Pure core, thin adapters** - the doctrine used by
`erpnext/accounts/forecasting/` and `erpnext/manufacturing/scheduling/`:

```
                    ┌──────────────────────────────────────────────┐
                    │                metric engine                 │
   loaders.py  ───▶ │  MetricSpec (registry) + named inputs        │ ──▶ api.py
 (GL Entry, SI,     │  ─▶ MetricValue (rounded, warned, guarded)   │     ├─ KPI Scorecard report
  SO/DN, PO/PR,     │  ─▶ status vs Threshold (direction aware)    │     ├─ Metric Snapshot rows
  Close Cycle,      │  ─▶ trend vs prior equal-length period       │     └─ agents / UI (catalog)
  Anomaly, SoD,     │  no frappe, no ambient clock                 │
  Sales Forecast)   └──────────────────────────────────────────────┘
```

| File | Frappe? | Responsibility |
|---|---|---|
| `models.py` | no | `MetricSpec`, `MetricValue`, `Threshold`, unit/grain/direction/category vocabularies, per-unit precision |
| `engine.py` | no | `compute_metric`, `compute_many`, `evaluate_threshold`, `trend`, `round_for_unit`, the guarded `Calc` |
| `registry.py` | no | **the KPI pack** - 24 `MetricSpec`s, formulas as data + arithmetic as pure lambdas |
| `loaders.py` | yes | one function per input group; every SQL statement in the module lives here |
| `api.py` | yes | `get_kpis`, `snapshot_kpis`, `get_metric_catalog`, `snapshot_all_companies` |
| `test_analytics.py` | no | 48 unit tests, runnable without a site |

Run the pure tests with no bench and no site:

```
$ python erpnext/analytics/test_analytics.py
...
Ran 48 tests in 0.005s

OK
```

### Why the arithmetic lives with the spec

`MetricSpec.fn` is a pure `(inputs, calc) -> number | None` lambda. The
alternative - an expression string parsed at runtime - buys nothing here and
loses static checking. Every division goes through `Calc.div`, so the guards
are implemented **once**: a zero or missing denominator yields `None` plus a
warning, never an exception, and never a fake zero.

The same rule governs inputs: if a declared input is absent from the pool (a
loader failed), the metric reports `None`/`Unknown` rather than computing on a
placeholder - a DSO of "0 days" because the receivable feed was down would be
worse than no answer. A loader that legitimately has nothing to count (a module
the customer has not used yet) passes a real `0` with its own warning, which
computes normally.

### Thresholds: one field set, two readings

`Threshold(green_min, amber_min, red_max)` is read according to the metric's
`direction`:

| direction | Green | Amber | Red |
|---|---|---|---|
| `higher_is_better` | `value >= green_min` | `value >= amber_min` | otherwise, or `value < red_max` |
| `lower_is_better` | `value <= green_min` | `value <= amber_min` | otherwise, or `value > red_max` |
| `neutral` | never | never | never - always `Unknown` |

`red_max` is a *hard bound* evaluated first (e.g. working capital below zero
is Red no matter what the bands say). A metric with no bounds configured is
`Unknown`, never Red - an ungoverned metric must not look like a failing one.
`None` values are always `Unknown`.

## 3. The KPI pack (24 metrics)

| # | Key | Unit | Dir. | Category | Formula (as declared) |
|---|---|---|---|---|---|
| 1 | `current_ratio` | Ratio | higher | Liquidity | `current_assets / current_liabilities` |
| 2 | `quick_ratio` | Ratio | higher | Liquidity | `quick_assets / current_liabilities` |
| 3 | `working_capital` | Currency | higher | Liquidity | `current_assets - current_liabilities` |
| 4 | `cash_balance` | Currency | higher | Liquidity | `cash_and_bank` |
| 5 | `dso` | Days | lower | Receivables | `365 * ar_balance / revenue` |
| 6 | `dpo` | Days | neutral | Receivables | `365 * ap_balance / cogs` |
| 7 | `dio` | Days | lower | Receivables | `365 * inventory_balance / cogs` |
| 8 | `cash_conversion_cycle` | Days | lower | Receivables | `dso + dio - dpo`, recomputed from raw balances |
| 9 | `ar_overdue_pct` | Percent | lower | Receivables | `100 * ar_overdue_outstanding / ar_total_outstanding` |
| 10 | `collection_effectiveness_index` | Percent | higher | Receivables | `100 * (opening_ar + revenue - ar_total) / (opening_ar + revenue - ar_current)` |
| 11 | `revenue` | Currency | higher | Profitability | `revenue` |
| 12 | `cogs` | Currency | neutral | Profitability | `cogs` |
| 13 | `opex` | Currency | neutral | Profitability | `opex` |
| 14 | `gross_margin_pct` | Percent | higher | Profitability | `100 * (revenue - cogs) / revenue` |
| 15 | `operating_margin_pct` | Percent | higher | Profitability | `100 * (revenue - cogs - operating_expense) / revenue` |
| 16 | `net_margin_pct` | Percent | higher | Profitability | `100 * (revenue - cogs - opex) / revenue` |
| 17 | `revenue_growth_pct` | Percent | higher | Growth | `100 * (revenue - prior_revenue) / prior_revenue` |
| 18 | `inventory_turns` | Ratio | higher | Operations | `cogs / average_inventory` |
| 19 | `otif_pct` | Percent | higher | Operations | `100 * otif_lines / delivered_lines` |
| 20 | `purchase_price_variance_pct` | Percent | lower | Operations | `100 * ppv_variance_amount / ppv_baseline_amount` |
| 21 | `close_duration_days` | Days | lower | Governance | `close_cycle_days_total / close_cycles_completed` |
| 22 | `open_high_anomalies` | Count | lower | Governance | `open_high_anomalies` |
| 23 | `sod_open_violations` | Count | lower | Governance | `open_sod_violations` |
| 24 | `forecast_accuracy_mape` | Percent | lower | Governance | `forecast_mape_sum / forecast_mape_count` |

Rounding is per unit: Currency 2dp, Percent 2dp, Days 1dp, Ratio 3dp,
Count 0dp (integer).

### Deliberate omissions

- **EBITDA proxy** - ERPNext's chart of accounts does not reliably separate
  interest from other indirect expenses, so an "EBITDA" here would be a guess
  wearing a precise name. `operating_margin_pct` (excluding Depreciation and
  Tax account types) is shipped instead, as the brief allowed.
- **`stock_availability_pct`** - would need an item-level definition of
  "should have been available" (Bin vs reorder level vs demand). No clean
  loader exists, so it is left out rather than shipped with a made-up
  denominator.

### Shipped default thresholds

Bands are shipped only where the answer is uncontroversial; everything else is
created blank and reports `Unknown` until a customer sets it.

| Metric | Green | Amber | Red bound |
|---|---|---|---|
| `current_ratio` | ≥ 2.0 | ≥ 1.2 | - |
| `quick_ratio` | ≥ 1.0 | ≥ 0.8 | - |
| `working_capital` | - | - | < 0 is Red |
| `dso` | ≤ 45 | ≤ 60 | - |
| `ar_overdue_pct` | ≤ 10% | ≤ 20% | - |
| `collection_effectiveness_index` | ≥ 80% | ≥ 60% | - |
| `otif_pct` | ≥ 95% | ≥ 90% | - |
| `purchase_price_variance_pct` | ≤ 1% | ≤ 3% | - |
| `close_duration_days` | ≤ 5 | ≤ 10 | - |
| `open_high_anomalies` | ≤ 0 | ≤ 5 | - |
| `sod_open_violations` | ≤ 0 | ≤ 3 | - |
| `forecast_accuracy_mape` | ≤ 20% | ≤ 30% | - |

No bands ship for `dio`, `cash_conversion_cycle`, `gross_margin_pct`,
`operating_margin_pct`, `net_margin_pct`, `revenue_growth_pct`,
`inventory_turns`, `cash_balance`, `revenue` (industry-specific), or for the
three `neutral` metrics (`dpo`, `cogs`, `opex`), which are never graded.

## 4. Agreement with existing reports (what each loader mirrors)

The whole point of a semantic layer is that it does **not** invent a second
version of the truth. Every loader is pinned to an existing ERPNext
computation:

| Loader / input | Mirrors | Where |
|---|---|---|
| `get_pl_inputs` → `revenue`, `cogs`, `opex`, `operating_expense` | Profit and Loss Statement period logic: GL Entry with `is_cancelled = 0`, `posting_date` in window, ledger accounts of the root type, `voucher_type != 'Period Closing Voucher'` | `erpnext/accounts/report/profit_and_loss_statement/profit_and_loss_statement.py:39-59` → `financial_statements.py:338` (`get_data`), `financial_statements.py:617` (`set_gl_entries_by_account`), `financial_statements.py:687` (`get_accounting_entries`), `financial_statements.py:783` (`get_account_filter_query`), `financial_statements.py:800` (`apply_additional_conditions`) |
| `get_pl_inputs` → `cogs` account selection | Financial Ratios' COGS bucket: `root_type = Expense` and `account_type = 'Cost of Goods Sold'` | `erpnext/accounts/report/financial_ratios/financial_ratios.py:108`, `financial_ratios.py:255-258` (`update_balances`) |
| `get_bs_inputs` → `ar_balance`, `ap_balance`, `inventory_balance`, `cash_and_bank` | `get_balance_on(date=..., company=..., account_type=...)` - the exact call the ratio report's turnover inputs use | `erpnext/accounts/utils.py:204` (`get_balance_on`), `financial_ratios.py:264-282` (`avg_ratio_balance`) |
| `get_bs_inputs` → `average_inventory` | `(opening + closing) / 2` where opening is `period_start - 1 day` | `financial_ratios.py:266-280` (`avg_ratio_balance`) |
| `get_bs_inputs` → `quick_assets` | Financial Ratios' definition of quick assets: ledger accounts of type Bank, Cash, Receivable | `financial_ratios.py:247-249` (`update_balances`) |
| `get_bs_inputs` → `current_assets`, `current_liabilities` | Primary: accounts tagged `account_type = 'Current Asset' / 'Current Liability'`, which is what the ratio report reads off the group rows. Fallback: `account_category` values from the shipped IFRS balance sheet template | `financial_ratios.py:86-112`, `financial_ratios.py:244-246`; `erpnext/accounts/financial_report_template/account_categories.json` |
| balance SQL predicate | `is_cancelled = 0`, `posting_date <= date`, company scoped, `sum(debit) - sum(credit)` in company currency | `erpnext/accounts/utils.py:336-352` (`get_balance_on`) |
| `get_ar_ageing_inputs` | Sales Invoice `outstanding_amount` vs `due_date`, the same two fields the Accounts Receivable report ages on | `erpnext/accounts/report/accounts_receivable/` |
| `get_ops_inputs` (OTIF) | Sales Order Item `delivery_date` / `delivered_qty` vs Delivery Note posting dates | see §5 |
| `get_ops_inputs` (PPV) | Purchase Receipt Item `base_rate` vs its Purchase Order Item `base_rate` | see §5 |
| `get_governance_inputs` | `Close Cycle` / `Close Task` (close manager), `Anomaly Finding` (anomaly detection), `SoD Violation Log` (SoD scanner), `Sales Forecast Item.mape` (statistical forecasting) - all built earlier on this branch | see §6 |

### Known, documented divergences

1. **Revenue scope.** `financial_ratios.py` uses *Direct Income* group
   accounts as "net sales" (`update_balances`, `financial_ratios.py:251-254`).
   The metric layer's `revenue` is **total Income root_type**, i.e. the P&L
   statement's "Total Income (Credit)" row. The *formulas* for
   `gross_margin_pct` and `net_margin_pct` are byte-for-byte the ratio
   report's (`(net_sales - cogs) / net_sales` and
   `profit_after_tax / net_sales`); only the revenue scope differs, and it
   differs *towards* the P&L statement, which is the number a CFO quotes.
   Charts of accounts that tag Direct Income exhaustively will see identical
   values.
2. **`dio` vs `inventory_turns`.** `inventory_turns` uses the *average* stock
   balance (mirroring `avg_ratio_balance`), while `dio` uses the *closing*
   balance, which is the conventional days-inventory definition. They are
   therefore not exact reciprocals of each other times 365; both are
   individually consistent with their source.
3. **Receivable ageing is live.** `Sales Invoice.outstanding_amount` is a
   current-state field, so an `ar_overdue_pct` for a *past* period end
   reflects today's collection state. The loader emits a warning saying so
   rather than silently implying a historical replay. (A future version can
   replay the Payment Ledger for a true as-of ageing.)
4. **Currency.** All money inputs are company currency. Receivable ageing
   multiplies `outstanding_amount` (party account currency) by the invoice
   `conversion_rate`.

## 5. Operational metric definitions (the ones that need pinning down)

**OTIF (`otif_pct`), line level.** Population: every Sales Order line that
received a delivery in the period - submitted, non-return `Delivery Note Item`
rows carrying `so_detail`, joined to `Sales Order Item`. A line is
on-time-in-full when:

- *on time*: `MAX(Delivery Note.posting_date)` for that line ≤ the line's
  promised `Sales Order Item.delivery_date` (a line with no promised date
  counts as on time - nothing was promised), and
- *in full*: `Sales Order Item.delivered_qty >= Sales Order Item.qty`
  (0.01 tolerance for float noise).

Line level, not order level, because the qty and date promises live on the
line. Deliveries without a sales order link are out of scope by construction.

**PPV (`purchase_price_variance_pct`), receipt-time.** For submitted,
non-return `Purchase Receipt Item` rows in the period linked to a
`Purchase Order Item`:

- variance = `Σ qty * (receipt base_rate - order base_rate)`
- baseline = `Σ qty * order base_rate`

Positive = paying more than ordered. Landed costs, revaluations and
invoice-time price changes are deliberately **not** included: this is the
purchasing-discipline number ("did we pay what we agreed?"), not a full
standard-cost variance.

**Close duration (`close_duration_days`).** Completed `Close Cycle` records
whose `period_end_date` falls in the window; one cycle's duration is the days
from `period_end_date` to the latest `Close Task.signed_off_on` of that cycle.
Cycles with no signed-off task are excluded (nothing to measure) and reported
as a warning, so an unfinished implementation cannot flatter the average.

## 6. Verified fieldnames

Every field the loaders touch, checked against the doctype JSON in this repo:

| Doctype | Fields used |
|---|---|
| GL Entry | `account`, `company`, `posting_date`, `debit`, `credit`, `is_cancelled`, `voucher_type` |
| Account | `name`, `company`, `is_group`, `root_type`, `account_type`, `account_category`, `lft`, `rgt` |
| Sales Invoice | `docstatus`, `company`, `posting_date`, `due_date`, `outstanding_amount`, `conversion_rate` |
| Delivery Note | `name`, `docstatus`, `company`, `posting_date`, `is_return` |
| Delivery Note Item | `parent`, `so_detail` |
| Sales Order Item | `name`, `delivery_date`, `qty`, `delivered_qty` |
| Purchase Receipt | `name`, `docstatus`, `company`, `posting_date`, `is_return` |
| Purchase Receipt Item | `parent`, `purchase_order_item`, `qty`, `base_rate` |
| Purchase Order Item | `name`, `base_rate` |
| Close Cycle | `name`, `company`, `status` (`Completed`), `period_end_date` |
| Close Task | `close_cycle`, `signed_off_on` |
| Anomaly Finding | `company`, `severity` (`High`), `status` (`Open`, `Investigating`) |
| SoD Violation Log | `status` (`Open`) - **no company field**: the count is site-wide, and the loader warns |
| Sales Forecast | `name`, `company`, `docstatus`, `posting_date` |
| Sales Forecast Item | `parent`, `parenttype`, `mape` |
| Company | `name`, `default_currency` |

Doctypes built earlier on this branch (`Close Cycle`, `Anomaly Finding`,
`SoD Violation Log`, `Sales Forecast`) are guarded with
`frappe.db.table_exists`, so an app that has not migrated them yet gets `0`
plus a warning instead of a traceback.

## 7. Governance doctypes

**Metric Definition** (module Accounts, `autoname: field:metric_key`,
`track_changes`). The customer-owned half of a metric: `enabled`, the three
threshold bounds, an optional `company` scope and free-text `notes`. The
catalog-owned half - `unit`, `direction`, `category`, `formula_text`,
`description` - is read-only and re-synced from `registry.py` on every save,
so a definition can never drift from the code that computes it.
`validate()` rejects a `metric_key` that is not in the catalog and rejects
threshold bands that contradict the metric's direction (e.g. a Green boundary
*below* Amber on a higher-is-better metric). The list view carries the
**Install Default Metrics** button (`sod_rule_list.js` pattern) calling the
whitelisted `install_default_metrics()`, which creates one row per catalog
entry, never overwriting an existing one.

*Known v1 limitation*: `autoname: field:metric_key` means one definition row
per metric, so the `company` field scopes that single row rather than allowing
a per-company row *and* a global one at the same time. The resolution order in
`api._thresholds` (company row → global row → registry default) is already
written for the multi-row case, so switching the naming to
`{metric_key}-{company}` later is a naming change, not a logic change.

*Float bounds and the meaning of 0*: Frappe `Float` columns are
`NOT NULL DEFAULT 0`, so a stored `0` is ambiguous. Two rules, stated on the
form itself: (a) a row whose three bounds are all `0` configures nothing and
the registry default applies; (b) in any other row, `0` is a real bound only
for `Count` metrics ("zero open anomalies" is a legitimate target), and means
"unset" for money, ratio, percent and days metrics.

**Metric Snapshot** (module Accounts, `autoname: format:KPI-{#####}`). One
persisted value per company + metric + period, with `status`, `computed_on`,
the `inputs_json` audit trail and any `warnings`. All fields are read-only on
the form - snapshots are produced by `snapshot_kpis`, which is **idempotent**:
re-running a period updates the existing row rather than appending a second
version of history. The list view colours by status.

## 8. API

```python
erpnext.analytics.api.get_kpis(company, period_start=None, period_end=None, keys=None)
erpnext.analytics.api.snapshot_kpis(company, period_start=None, period_end=None, keys=None)
erpnext.analytics.api.get_metric_catalog(keys=None)
```

- `get_kpis` returns one dict per metric: `key`, `label`, `unit`, `category`,
  `value`, `status`, `prior_value`, `change`, `pct_change`,
  `direction_is_good`, `sparkline`, `formula_text`, `description`,
  `inputs_used`, `warnings`, and both period windows. The period defaults to
  the current calendar month; the comparison period is always the immediately
  preceding window of **equal length**.
- `snapshot_kpis` writes/updates `Metric Snapshot` rows and returns
  `{created, updated, metrics}`.
- `get_metric_catalog` returns definitions only - no numbers, no DB access -
  which is what a UI or an LLM agent should read *before* quoting a metric.

Roles: read requires System Manager / Accounts Manager / Accounts User /
Auditor; snapshotting requires System Manager / Accounts Manager.

## 9. KPI Scorecard report

Script report, `ref_doctype: Metric Definition`, module Accounts, roles
Accounts Manager / Accounts User / Auditor. Filters: `company` (required),
`period_start` / `period_end` (default current month), optional `category`.
Rows are grouped by category (Liquidity → Receivables → Profitability →
Growth → Operations → Governance) with the metric label, value formatted by
unit, a status pill, the prior-period value, absolute and % change, the
formula and the metric key. The chart is the status mix; the summary cards
count Green / Amber / Red / Not Graded. Loader warnings are surfaced once, in
the report message, instead of once per metric. An inner button saves the
current view as a snapshot.

## 10. Recommended follow-ups (not applied - no hooks.py edit in this change)

```python
# hooks.py
scheduler_events = {
    "cron": {
        # 03:00 on the 3rd of each month - after the prior month is closed
        "0 3 3 * *": ["erpnext.analytics.api.snapshot_all_companies"],
    },
}
```

`snapshot_all_companies()` already exists in `api.py` (not whitelisted) and
snapshots every company for the resolved period, logging and continuing past a
failing company.

Workspace links to add to the **Accounting** workspace (not applied):

| Type | Label | Link to |
|---|---|---|
| Link (Report) | KPI Scorecard | `KPI Scorecard` |
| Link (DocType) | Metric Definition | `Metric Definition` |
| Link (DocType) | Metric Snapshot | `Metric Snapshot` |

The Report Center fixture (`erpnext/report_center/accounting.json`) is the
natural front door for "KPI Scorecard" as well - a `Report Center Link` row
pointing at the report, in the same style as the existing curated entries.

## 11. Next steps

1. **Warehouse sidecar** (the other half of W7): snapshot rows are already the
   grain a star schema wants - `company × metric × period` - so a DuckDB
   export is a straight dump of `Metric Snapshot`.
2. **Per-company threshold rows** once the naming change above is made.
3. **True as-of AR ageing** by replaying the Payment Ledger, removing
   divergence 3.
4. **Dimension grain**: today's grain vocabulary is `Company` and
   `Company+Period`; cost centre / project grains are the obvious extension
   and only need the loaders to take a filter object.
