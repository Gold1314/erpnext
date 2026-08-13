# Cash Flow Forecasting Engine - Design

Status: **v1 implemented** - pure-core engine + Frappe loaders + "Cash Flow
Forecast" script report. Blueprint reference: `GAP_CLOSURE_BLUEPRINT.md` W1
item 1a.

## 1. Problem

ERPNext knows everything needed to answer "what does my cash position look
like over the next 90 days?" - due dates on invoices, order pipelines,
subscription billing cycles, bank/cash GL balances - but nothing assembles it
into a forward-looking view. The existing Cash Flow report is backward-looking
(actuals from the GL). CFOs get the forward view from spreadsheets.

## 2. Architecture

**Pure core, thin adapters** - modeled exactly on
`erpnext/manufacturing/scheduling/`:

```
                ┌───────────────────────────────────────────────┐
                │              forecasting engine               │
  loaders.py ─▶ │  CashFlowItem[] + opening balance + scenario  │ ─▶ report /
 (SI/PI payment │  ─▶ periods (bucketed, running balance)       │    future SPA
  schedules,    │  periodicity: Daily | Weekly | Monthly        │
  SO/PO, Subs,  │  read-only: no writes, no ambient time        │
  GL balances)  │                                               │
                └───────────────────────────────────────────────┘
```

- `models.py` / `engine.py`: **no frappe imports**. Dataclasses in, dataclass
  result out. Start/end dates are passed in - no `today()` inside the core -
  so the engine is deterministic and unit-testable without a site
  (`python erpnext/accounts/forecasting/test_engine.py`).
- `loaders.py`: all `frappe.qb` / `frappe.db.sql` work. Each loader returns
  `list[CashFlowItem]` in **company currency** (positive = inflow).
- `erpnext/accounts/report/cash_flow_forecast/`: script report calling
  loaders → engine, rendering rows per source type, per-period columns, a
  closing-balance chart and summary tiles.

### Core model (`models.py`)

- `CashFlowItem(posting_date, amount, source_type, party, party_type,
  reference_doctype, reference_name, bank_account, currency)` - one expected
  cash movement. `source_type` ∈ {Receivable, Payable, Sales Order,
  Purchase Order, Subscription, Opening Balance}.
- `ForecastScenario(receivable_delay_days, payable_delay_days,
  include_sales_orders, include_purchase_orders, confidence_haircut_pct)` -
  what-if knobs, applied **inside the engine** so any caller gets identical
  semantics.
- `ForecastPeriod(key, label, from_date, to_date, inflows{source: amt},
  outflows{source: amt}, opening_balance, closing_balance)` with derived
  `total_inflow` / `total_outflow` / `net`.
- `ForecastResult(periods, opening_balance, periodicity)` with derived
  `closing_balance` and source-type ordering helpers.

### Algorithm (`engine.py`)

1. `apply_scenario`: drop excluded pipeline sources; shift Receivable /
   Payable dates by the delay knobs; scale Sales Order / Purchase Order
   amounts by `1 − haircut/100` (floored at 0). Input items are never
   mutated.
2. `build_periods`: contiguous inclusive buckets. Daily = one per day;
   Weekly = 7-day buckets anchored on `start`, last clipped; Monthly =
   partial first month to calendar month-end, whole months after, last
   clipped. Pure `calendar`/`datetime` stdlib - no external deps.
3. Bucketing: each item lands in the period containing its date. Items
   **before `start` roll into the first period** (overdue receivables and
   payables are expected to settle imminently). Items after `end` drop.
4. Running balance: `closing = opening + Σ net`, carried period to period.

## 3. Data sources (loaders) and verified fieldnames

Every fieldname below was checked against the doctype JSON before use.

| Loader | Doctype(s) | Fields read |
|---|---|---|
| `get_receivables` / `get_payables` (via `get_invoice_items`) | `Payment Schedule` (child) | `due_date`, `payment_amount`, `paid_amount`, `discounted_amount`, `parent`, `parenttype` |
| | `Sales Invoice` / `Purchase Invoice` (parent) | `name`, `customer`/`supplier`, `company`, `docstatus`, `outstanding_amount`, `conversion_rate`, `due_date`, `posting_date`, `party_account_currency` |
| `get_sales_order_pipeline` | `Sales Order` | `name`, `customer`, `delivery_date`, `transaction_date`, `base_grand_total`, `per_billed`, `status`, `company`, `docstatus` |
| `get_purchase_order_pipeline` | `Purchase Order` | `name`, `supplier`, `schedule_date`, `transaction_date`, `base_grand_total`, `per_billed`, `status`, `company`, `docstatus` |
| `get_subscription_inflows` | `Subscription` | `name`, `status`, `party_type`, `party`, `current_invoice_end`, `next_billing_period_end`, `end_date`, `days_until_due`, `company` |
| | `Subscription Plan Detail` (child) | `parent`, `qty`, `plan` |
| | `Subscription Plan` | `cost`, `currency`, `billing_interval`, `billing_interval_count` |
| `get_opening_balance` | `GL Entry` + `Account` | `debit`, `credit`, `posting_date`, `is_cancelled`, `company`, `account`; `Account.account_type` ∈ ('Bank', 'Cash') |

Per-source semantics:

- **Receivables/Payables**: `Payment Schedule` rows joined to submitted
  invoices with `outstanding_amount > 0`; per-row unpaid =
  `payment_amount − paid_amount − discounted_amount` (transaction currency),
  converted to company currency via the invoice's `conversion_rate`.
  Invoices with **no** schedule rows fall back to invoice-level
  `due_date` + `outstanding_amount` (converted when `party_account_currency`
  differs from the company currency). Return invoices are naturally excluded
  by the `outstanding_amount > 0` filter.
- **SO/PO pipeline**: submitted orders, `status ∉ {Closed, On Hold}`,
  `per_billed < 100`. Remaining = `base_grand_total × (100 − per_billed)/100`,
  expected on `delivery_date` (SO) / `schedule_date` (PO), falling back to
  `transaction_date`. Uncertainty is handled by the scenario haircut, not by
  modeling payment terms on orders.
- **Subscriptions**: Active subscriptions; per-cycle amount =
  Σ(`qty × Subscription Plan.cost`); billing dates projected from
  `current_invoice_end` stepping by `billing_interval` ×
  `billing_interval_count` (Day/Week/Month/Year) until the horizon or
  `end_date`; cash expected `days_until_due` after each billing date.
  `party_type = Supplier` subscriptions produce outflows.
- **Opening balance**: `Σ debit − Σ credit` over non-cancelled GL Entries of
  Bank/Cash accounts up to the day before the forecast start - the same
  semantics as `erpnext.accounts.utils.get_balance_on`, done in one grouped
  query instead of per-account calls.

## 4. Report

Script report **Cash Flow Forecast** (module Accounts, ref doctype GL Entry,
roles Accounts Manager / Accounts User).

- Filters: company, from/to date (default today → +90d), periodicity
  (Daily/Weekly/Monthly, default Weekly), include SO/PO checkboxes,
  receivable/payable delay days, pipeline haircut % (default 20).
- Rows: Opening Balance → inflow rows per source type → Total Inflows →
  outflow rows (shown negative) → Total Outflows → Net Cash Flow → Closing
  Balance. Bold rows carry `bold: 1` and are styled by the JS formatter
  (pattern from `exponential_smoothing_forecasting.js` / `gross_profit.js`).
- Chart: closing balance line + net-cash-flow bars per period (dict shape
  mirrors `exponential_smoothing_forecasting.py`); summary tiles for opening
  balance, total in/out, projected closing.

## 5. Simplifications (v1) and follow-ups

1. **Currency**: everything is reported in company currency. Payment-schedule
   amounts convert via invoice `conversion_rate`; multi-currency subscription
   plans (plan currency ≠ company currency) are skipped. FX-shift scenario
   overlays (blueprint) are v2.
2. **Payment Schedule vs. outstanding drift**: per-row unpaid can exceed the
   invoice's remaining outstanding when credit notes / unallocated payments
   were applied outside the schedule; rows are not rescaled to the invoice
   outstanding in v1.
3. **Order advances**: `advance_paid` is not netted from the SO/PO pipeline
   (it is stored in party-account currency; converting reliably needs the
   payment ledger). The haircut knob is the stated uncertainty control.
4. **Subscription pricing**: only `Subscription Plan.cost` (Fixed Rate) is
   priced; "Based On Price List"/"Monthly Rate" plans contribute whatever
   `cost` holds (typically 0). Trialing/Grace Period subscriptions excluded.
5. **Not yet sourced** (blueprint lists them for later): early-pay discount
   windows (`discount_date`), recurring Journal Entry templates,
   `Payment Order` batches, per-`bank_account` split (the `bank_account`
   field on `CashFlowItem` is already there for it).
6. **No persistence**: v1 is report-only; the blueprint's `Cash Flow
   Forecast` doctype + saved scenarios come later.

## 6. Recommended wiring (not done here - needs hooks/settings edits)

- Workspace shortcut: add the report to the Accounting workspace JSON
  (`erpnext/accounts/workspace/accounting/accounting.json`) under Financial
  Reports.
- A scheduled weekly digest (`hooks.py` `scheduler_events`) emailing the
  projected closing-balance curve to Accounts Managers, following the
  `process_deferred_accounting` background pattern.
- Company-level defaults (haircut %, delay days) on Accounts Settings once
  the knobs prove useful, so saved scenarios don't rely on filter defaults.
