# Revenue Management (ASC 606 / IFRS 15) — v1 Design

Pure-core + adapter implementation of the five-step revenue model, following
the doctrine in `GAP_CLOSURE_BLUEPRINT.md` (§2, W1 item 1c): a frappe-free
engine (`erpnext/accounts/revenue/`) behind thin doctype adapters
(`Revenue Contract`, `SSP Price`) that post through standard Journal Entries
in the same style as the existing deferred-revenue machinery
(`erpnext/accounts/deferred_revenue.py`).

## The five steps, mapped

| ASC 606 step | Where it lives |
|---|---|
| 1. Identify the contract | `Revenue Contract` doctype (optionally loaded from a Sales Order / Sales Invoice) |
| 2. Identify performance obligations | `Revenue Contract Obligation` child rows → `models.ObligationInput` |
| 3. Determine the transaction price | Σ stated amounts, computed in `validate()` |
| 4. Allocate the transaction price | `engine.allocate` — relative-SSP allocation |
| 5. Recognize revenue | `engine.build_recognition_plan` → `Revenue Recognition Entry` rows → `post_recognition()` JEs |

The whole trail (SSP basis on `SSP Price`, allocation %, per-period plan,
posted JE links) is visible on the submitted contract — that is the
auditor-facing five-step evidence.

## Pure core (`models.py`, `engine.py`)

Zero frappe imports, no ambient time (every date is a parameter). Tested by
`test_revenue.py`, runnable without a site:

```
python erpnext/accounts/revenue/test_revenue.py
```

### Allocation (`engine.allocate`)

`allocated_i = transaction_price × ssp_i / Σ ssp`, rounded half-up to 2
decimals; the rounding residual is pushed into the **largest** allocation so
the allocations sum to the price exactly (tested). Rules:

- an obligation with `ssp = 0` receives 0 (it is a freebie relative to the
  priced obligations);
- if **all** obligations have `ssp = 0`, allocation falls back to
  `stated_amount` proportions — the contract's own stated prices are then the
  best available estimate of relative standalone value (a pragmatic v1 stand-in
  for the "residual approach" of ASC 606-10-32-34(c));
- if there is no basis at all (all SSPs and stated amounts are zero) a
  non-zero price is refused (`ValueError`).

### Recognition plan (`engine.build_recognition_plan`)

- **Point in Time** → a single row dated on `satisfied_date`. If the event has
  not happened yet, the row carries `None` dates and the key is flagged in
  `ContractPlan.event_pending_keys`; such rows are never posted by a
  time-based run until the obligation is marked satisfied.
- **Over Time** → straight-line by calendar month between `start_date` and
  `end_date`, with day-proration for partial first/last months (the same
  proration idea as `deferred_revenue.calculate_monthly_amount`): each month
  segment weighs `days_covered / days_in_month`, so full months get equal
  amounts and edge months get proportionally less. The rounding residual lands
  in the **final** period so each obligation's rows total exactly its
  allocated amount (tested).

Invariant asserted on submit: Σ plan rows = transaction price.

### Contract modification (`engine.modification`)

- **Prospective** (≈ ASC 606-10-25-13(a), separate-contract-like treatment):
  only the unrecognized remainder (`new_price − Σ recognized`) is reallocated
  across the caller-supplied *remaining* obligations (service windows starting
  at the modification date). Already-recognized revenue is never restated;
  `catch_up_by_key` is empty. Refuses a new price below recognized-to-date.
- **Cumulative Catch-up** (≈ ASC 606-10-25-13(b)): the full new price is
  reallocated, a full plan re-derived, and per key
  `catch_up = new cumulative-to-date (rows with period_end ≤ as_of) −
  recognized-to-date` — can be negative (clawback). `as_of` is required
  because the engine has no ambient clock.

**Documented v1 simplifications** vs. the full standard:

- no contract-combination rules (ASC 606-10-25-9) — one Revenue Contract is
  one unit of account; combining multiple SO/SIs is the operator's judgment;
- no distinct-goods test for whether a modification is a *separate contract*
  (ASC 606-10-25-12) — the operator chooses the method;
- no variable consideration, constraints, significant financing component,
  or refund liabilities — the transaction price is the fixed stated total;
- over-time progress is time-elapsed only (no cost-to-cost / units-delivered
  input methods yet);
- `original_plan` is accepted for API stability/audit context; v1 math needs
  only the recognized-to-date amounts.
- **UI note:** `modification` is engine-only in v1 (tested in
  `test_revenue.py`); the `Revenue Contract` doctype deliberately ships no
  modification button — wiring it (amend-style flow that re-derives the plan
  and posts the catch-up JE) is the documented follow-up.

## Journal entry design and the deferral assumption

`Revenue Contract.post_recognition(until_date)` posts, per due unposted plan
row, **one submitted Journal Entry** (voucher type `Deferred Revenue`,
mirroring `deferred_revenue.book_revenue_via_journal_entry`):

```
Dr  Deferred Revenue (contract-level liability account)   amount
Cr  obligation.income_account                              amount
```

with the contract's cost center and a `user_remark` referencing contract +
obligation + period. Point-in-time rows post only after
`mark_obligation_satisfied` stamps the event date (which also dates the plan
row); over-time rows post on their `period_end`.

**Deferral assumption (keeps v1 decoupled and safe):** this module assumes
billing credits the deferred revenue account — either the standard ERPNext
deferred flow (`enable_deferred_revenue` on Sales Invoice Items pointing at
the same account) or a manual entry. Revenue recognition here only moves value
*out of* deferral into income; it never touches the invoice itself. If the
invoice posts straight to income instead, the operator must not also run this
module for those amounts (double-count risk) — v1 does not reconcile the two.

Other v1 accounting simplifications:

- amounts are in **company currency** (`base_net_amount` from the source);
  multi-currency contracts are out of scope;
- the JE carries no `reference_type/reference_name` because `Journal Entry
  Account.reference_type` is a closed select list that does not include
  `Revenue Contract`; traceability is via the `journal_entry` link on the plan
  row plus the user remark;
- no accounting-dimension fan-out beyond cost center yet.

## Doctypes

- **SSP Price** — standalone selling price catalog per item (× optional
  company × optional `valid_from`), with the estimation `basis`
  (Observable / Adjusted Market / Expected Cost Plus / Residual) and notes.
  `get_ssp(item_code, company, date)` resolves the record with the latest
  `valid_from` on or before the date; a company-specific record beats a
  company-agnostic one. Duplicates on (item, company, valid_from) are blocked.
- **Revenue Contract** (submittable, `REVCON-#####`) — obligations +
  generated plan; `load_from_source()` pulls Sales Order / Sales Invoice
  items (verified fields: `item_code`, `description`, `base_net_amount`,
  and on SI items also `income_account`, `service_start_date`,
  `service_end_date`, `enable_deferred_revenue`, `deferred_revenue_account`;
  SO items have no income account or service dates, so the company default
  income account and Point in Time method are used, with a msgprint).
  Cancel is blocked while posted rows exist.
- **Revenue Contract Obligation / Revenue Recognition Entry** — child tables;
  plan rows are engine output, read-only, keyed by obligation row idx.

## Status lifecycle

`Draft` → (submit) `Active` → (all plan rows posted) `Completed`;
(cancel, only with nothing posted) `Cancelled`.

## Follow-ups (not in v1)

- modification UI + catch-up JE posting (engine ready);
- scheduler wrapper for monthly auto `post_recognition` (recommend a
  `process_deferred_accounting`-style monthly job);
- event triggers from Delivery Note submission → `mark_obligation_satisfied`;
- input-method progress measures; variable consideration; multi-currency;
- workspace shortcuts/links for SSP Price, Revenue Contract and the
  Revenue Recognition Status report.
