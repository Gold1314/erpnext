# Lease Accounting (ASC 842 / IFRS 16) — Design

Lessee-side lease accounting built on the repo's **pure-core + adapter**
doctrine (see `GAP_CLOSURE_BLUEPRINT.md` §2 and the precedents in
`erpnext/manufacturing/scheduling/` and `erpnext/accounts/forecasting/`).

```
erpnext/assets/leasing/            pure core (zero frappe, zero ambient time)
  models.py                        LeasePayment, LeaseTerms, AmortizationRow,
                                   LeaseSchedule, RemeasurementResult
  engine.py                        present_value, build_schedule, remeasure,
                                   classify_short_term, generate_payment_rows
  test_leasing.py                  stand-alone unit tests (no site needed)

erpnext/assets/doctype/
  lease_contract/                  submittable adapter doctype
  lease_contract_payment/          child: contractual payments (source of truth)
  lease_amortization_entry/        child: computed schedule + posting state

erpnext/assets/report/
  lease_liability_summary/         script report + ASC 842 maturity chart
```

## Engine conventions

**Monthly compounding.** The periodic discount rate is
`annual_rate_pct / 12 / 100`. Discount exponents are *whole calendar months*
between commencement and each payment's due date:

- `Beginning of Period` timing uses **floor** months — a payment due at
  commencement (month 0) is undiscounted, matching an annuity-due.
- `End of Period` timing uses **ceiling** months — a payment due on the last
  day of the first month is discounted one full period, matching an ordinary
  annuity. (Floor months would give it exponent 0 and misprice it.)

The amortization schedule anchors every payment to the *same* month tick as
its discount exponent (start-of-period application for Beginning timing,
end-of-period for End timing), so the roll-forward is exactly consistent with
the PV and the liability mathematically amortizes to zero.

**Rounding.** All monetary outputs are rounded to 2dp with deterministic
`ROUND_HALF_UP` (`engine.round2`, Decimal-backed). Per-period rounding drift
is absorbed by the **final row**: its interest is forced to
`payment − opening_liability` so the closing liability lands on exactly
`0.00`, and the final depreciation row books the remaining ROU carrying value
so the closing ROU also lands on exactly `0.00`. Unit tests assert both.

**ROU depreciation** is straight-line over the lease term months (or
`rou_useful_life_months` when shorter — engine supports it; the doctype uses
the lease term in v1).

**Short-term exemption** (`classify_short_term`): term ≤ 12 months
(ASC 842-20-25-2 / IFRS 16.5). Short-term contracts recognize *no* liability
and *no* ROU asset; each payment is expensed as incurred.

**Remeasurement** (`remeasure`) — v1 simplifications, deliberately narrow:

- The caller supplies `new_terms` describing the lease *from* the effective
  date (remaining payments, new rate); the engine returns a fresh schedule
  plus the signed delta between the carrying liability (closing liability of
  the last period ended before the effective date) and the new PV. That delta
  is the ROU adjustment amount per ASC 842-10-35-4 / IFRS 16.39.
- Not modeled in v1: partial terminations with P&L gain/loss, ROU floored at
  zero (excess to P&L), separate-contract tests for added underlying assets,
  and mid-period effective dates (the carrying value is read at the last
  completed period end). A `Lease Modification` doctype consuming this
  function is the planned v2.

**v1 scope notes:** `initial_rou = initial_liability` — initial direct
costs, incentives, prepaid/accrued rent and dismantling provisions are v2
fields that would adjust the ROU independently of the liability. Variable
payments, FX leases and finance/operating dual classification (US GAAP
operating leases with single lease cost) are also out of v1 scope; the
schedule implements the finance-lease/IFRS 16 single-model mechanics.

## Doctype orchestration (no new ledgers, no duplicated assets)

**The ROU asset is a standard `Asset`.** On submit of a non-short-term
Lease Contract, the controller creates one with:

- `asset_type = "Existing Asset"` — this repo's Asset schema (there is no
  `is_existing_asset` check field here). Existing Assets require **no
  purchase document** and post **no GL on submit**
  (`Asset.validate_make_gl_entry` returns False without a purchase document),
  which is exactly what we want: capitalization is booked by the
  commencement Journal Entry instead.
- `net_purchase_amount = initial_rou` (this schema's gross-value field),
  `purchase_date = available_for_use_date = commencement_date`,
  `calculate_depreciation = 1`, and one explicit `finance_books` row:
  Straight Line, `total_number_of_depreciations = term months`,
  `frequency_of_depreciation = 1`, salvage 0, depreciation start = last day
  of the commencement month (the Asset controller's own default).
- The asset is inserted **as a draft, not submitted**. The Asset controller
  generates the depreciation schedule on save and explicitly asks the user
  to "check, edit if needed, and submit" — auto-submitting would both bypass
  that review and fire an Asset Movement silently. The contract links it via
  `rou_asset` and tells the user to review + submit.
- Requirements this inherits from the assets module (surfaced as normal
  Asset validation errors): a fixed-asset non-stock Item (`asset_item`
  field), a Location, an Asset Category with fixed asset / accumulated
  depreciation / depreciation expense accounts for the company, and a cost
  center (on the contract or the company's `depreciation_cost_center`).

**Commencement Journal Entry** (submitted automatically):

    Dr  Fixed Asset account (from the Asset Category)   initial_rou
    Cr  Lease Liability account                          initial_liability

(equal amounts in v1). JE construction copies the
`deferred_revenue.book_revenue_via_journal_entry` style. JE lines carry **no**
`reference_type/reference_name` — that child field is a closed Select that
does not include Lease Contract — so the linkage lives on the contract
(`commencement_journal_entry`) and on each schedule row (`journal_entry`).

**Monthly posting** (`post_monthly_entries(until_date)` — whitelisted doc
method, idempotent per row via the `posted` flag): for every unposted row
with `period_end <= until_date`:

    Dr  Interest Expense        interest      (accretion)
    Dr  Lease Liability         principal     (liability reduction)
    Cr  Payment Account         payment       (bank/payable chosen on the contract)

Negative components flip sides automatically (e.g. an accretion-only period
where interest exceeds payment credits the liability net). **ROU depreciation
is never posted here** — the standard Asset owns its depreciation schedule
and posts it through the assets module; posting it from the lease too would
double-book. The final row's residual-absorbing interest keeps the GL
liability in exact agreement with the schedule.

**Short-term path:** on submit nothing is posted; `post_monthly_entries`
books `Dr Short Term Lease Expense / Cr Payment Account` per period payment.

**Cancellation:** blocked while any schedule row is posted (cancel those JEs
first); a draft ROU asset is deleted (with its draft depreciation schedules —
the `buying_controller.py` draft-asset cleanup precedent), a submitted one
must be cancelled by the user first; the commencement JE is auto-cancelled.

## Recommended follow-ups (not applied — hooks/workspace edits are out of file scope)

- `hooks.py` scheduler: a monthly job iterating submitted Lease Contracts and
  calling `post_monthly_entries()` (pattern: `process_deferred_accounting`).
- Assets workspace links for Lease Contract and Lease Liability Summary.
- v2: `Lease Modification` doctype over `engine.remeasure`, initial direct
  costs/incentives fields, finance-book-aware operating treatment (blueprint
  W1-1d), disclosure pack report.
