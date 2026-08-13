# Reconciliation Copilot — Design

Implements the W6 "Reconciliation copilot" item of `GAP_CLOSURE_BLUEPRINT.md`:
a candidate **ranking** engine for bank matching with explanations, layered on
top of ERPNext's existing rule-based reconciliation — it re-ranks the same
voucher universe the Bank Reconciliation Tool already queries, and delegates
every write back to the tool. Nothing existing was modified.

## Architecture (pure-core + adapter, per blueprint §2)

```
models.py    pure dataclasses (TxnFeatures, CandidateVoucher, MatchHistory,
             ScoredSuggestion) — zero frappe
engine.py    pure scoring/ranking — zero frappe, no ambient time (callers
             pass `today`)
loaders.py   frappe adapters: transaction features, candidate queries
             (mirrors of the tool's), match history
api.py       whitelisted endpoints for the /banking SPA
report/reconciliation_suggestions/   script report over the same engine
test_recon_copilot.py   site-less unit tests (importlib bootstrap, same as
             erpnext/accounts/forecasting/test_engine.py)
```

## What we mirror / delegate to (exact citations)

All references are into
`erpnext/accounts/doctype/bank_reconciliation_tool/bank_reconciliation_tool.py`
(BRT) and `erpnext/accounts/doctype/bank_transaction/bank_transaction.py` (BT).

| Copilot code | Mirrors / delegates to | Why |
|---|---|---|
| `loaders._get_payment_entry_rows` | mirrors BRT `get_pe_matching_query` (BRT:1319–1378): `docstatus = 1`, `payment_type IN (Receive/Pay by direction, "Internal Transfer")`, `clearance_date IS NULL`, `paid_to`/`paid_from` = bank GL account, `base_paid_amount_after_tax` as paid amount | same PE universe as the tool |
| `loaders._get_journal_entry_rows` | mirrors BRT `get_je_matching_query` (BRT:1381–1447): `docstatus = 1`, `voucher_type != "Opening Entry"`, `clearance_date IS NULL`, JE Account row on the bank GL account, `SUM(credit/debit_in_account_currency)` by direction, `Max(cheque_no)` as reference | same JE universe |
| `loaders._get_sales_invoice_rows` (deposits only) | mirrors BRT `get_si_matching_query` (BRT:1450–1491): POS rows (`Sales Invoice Payment`) with `clearance_date IS NULL`, `sip.account` = bank GL account, invoice `currency` = bank account currency | same SI universe; deposit-direction gate mirrors BRT `get_matching_queries` (BRT:1259) |
| `loaders._get_purchase_invoice_rows` (withdrawals only) | mirrors BRT `get_pi_matching_query` (BRT:1494–1527): `is_paid = 1`, `clearance_date IS NULL`, `cash_bank_account` = bank GL account, currency match | same PI universe; withdrawal-direction gate mirrors BRT:1263 |
| `loaders.get_candidates` post-processing | **delegates** to BRT `subtract_allocations` (BRT:1105–1117) | identical allocation-adjusted remaining amount as `get_linked_payments` (BRT:1076–1102) |
| `loaders.get_transaction_features` — amount = `unallocated_amount` | mirrors BRT `check_matching`'s `common_filters.amount = transaction.unallocated_amount` (BRT:1143–1151) | rank against what is still open, not the gross amount |
| `api.suggest_bulk` / report transaction filters | mirror BRT `get_bank_transactions` (BRT:50–89): `docstatus = 1`, `unallocated_amount > 0`, optional date range, ordered by date | same definition of "unreconciled" |
| `api.accept_suggestion` | **delegates** to BRT `reconcile_vouchers` (BRT:1061–1072, `@frappe.whitelist(methods=["POST"])`, signature `(bank_transaction_name, vouchers, is_new_voucher=False)` with `vouchers` = JSON list of `{payment_doctype, payment_name, amount}`) which runs BT `add_payment_entries` (BT:153) → `allocate_payment_entries` (BT:174) | one write path for reconciliation, ever |
| `api.accept_suggestion` amount | mirrors BRT `start_auto_reconcile` (BRT:994–1036, esp. 1014–1023): pass the voucher's allocation-adjusted `paid_amount`; over-allocation is clipped by BT `allocate_payment_entries` (BT:174–186) | same semantics as the tool's own auto flow |

Non-goals mirrored deliberately: the tool's `get_bt_matching_query`
(BRT:1275, transfer counterparts) is served by the existing
`search_for_transfer_transaction` (BRT:901) flow and is not re-ranked; no
Loan doctypes exist in this codebase's matching queries (lending ships its
own `get_matching_queries` hook, BRT:1193, which we intentionally do not
duplicate).

## Scoring model (engine.py)

Component scores, each 0–100 (weights: amount .35, reference .25, party .20,
date .10, history .10; custom weights are normalized to sum 1):

- **amount_score** — exact within 0.005 → 100; else `100 × (1 − |Δ|/txn.amount)`
  floored at 0. *Partial-payment carve-out*: candidate `outstanding ≥
  txn.amount` and the transaction pays that outstanding exactly (within
  0.005) → 90. It scores below a full exact match on purpose: paying off a
  remainder is strong but not conclusive.
- **date_score** — `100 − 5 × |days|`, floor 0. A candidate dated **after**
  the bank transaction gets an extra flat −10 (books usually precede the
  bank; future-dated vouchers are slightly suspicious, not disqualifying).
- **reference_score** — candidate `reference_no`/`bill_no` equal to the
  transaction's `reference_number` (alphanumeric-collapsed), or found (≥4
  alphanumeric chars) inside the description/reference → 100; else Jaccard
  overlap × 100 over alphanumeric tokens ≥3 chars, case-insensitive, pure
  stopword tokens ("payment", "transfer", "neft", …) ignored.
- **party_score** — normalized party-hint equality → 100; else best
  `difflib.SequenceMatcher` ratio of the normalized party/party_name against
  sliding token windows (size k−1…k+1) over the description × 100.
- **history_score** — prior reconciliations of the same (party,
  voucher_type) from `MatchHistory` with diminishing returns:
  `min(count, 5)/5 × 100` — five confirmations saturate the signal; a
  description-prefix hit (first 24 collapsed chars) pointing at the same
  party feeds the same curve.

`explanation` lists the top three contributing components by weighted
contribution, in words ("exact amount; reference INV-0042 found in
description; matched this payer 7 times before").

**Determinism**: results are sorted score-descending, then voucher_name
ascending — equal scores never shuffle between runs.

**Zero-amount guard**: a transaction with `unallocated_amount ≤ 0` scores 0
on the amount component (no division), other components still rank.

## Auto-match policy (conservative, UI-flag only)

`auto_match_eligible` = `score ≥ 95` **and** amount exactly equal (the
90-point partial-payment carve-out does **not** qualify) **and**
(`reference_score ≥ 90` **or** `party_score ≥ 90`).

Rationale: total ≥ 95 alone can be reached by amount+date+history without any
identity evidence; requiring an exact amount plus a near-certain reference or
party signal keeps false-positive one-click matches out of the ledger.

**There is intentionally no endpoint that writes a match without a human.**
`auto_match_eligible` only tells the UI it may render a prominent one-click
"Accept" affordance; the write still goes through `api.accept_suggestion` →
the tool's `reconcile_vouchers`. (ERPNext's existing
`auto_reconcile_vouchers` (BRT:960) remains the only unattended matcher, and
it only acts on exact reference matches.)

## History (loaders.get_match_history)

Reads submitted Bank Transactions with `status = "Reconciled"` on the bank
account within `lookback_days` (default 365) and their `payment_entries`
child rows (**Bank Transaction Payments**, verified fields:
`payment_document`, `payment_entry`, `allocated_amount`, `clearance_date`,
`reconciliation_type`). Party per voucher: Payment Entry → `party`, Journal
Entry → `pay_to_recd_from`, Sales Invoice → `customer`, Purchase Invoice →
`supplier`. Description prefixes are normalized with the engine's own
`description_prefix` so build and lookup always agree.

## Caps (documented)

- `loaders.MAX_ROWS_PER_QUERY = 500` per voucher query, most recent first —
  the tool's own UI narrows by date; we bound the request thread instead.
- `api.suggest_bulk`: ≤ 200 transactions (oldest first, same ordering as BRT
  `get_bank_transactions`), ≤ 20 suggestions per transaction; the response
  carries `capped` + `total_unreconciled` so clients can page by date. This
  mirrors the spirit of BRT `auto_reconcile_vouchers` (BRT:971) pushing >10
  transactions to background batches — heavier runs belong in a job.
- Report: ≤ 200 transactions with an on-screen truncation message.

## API surface (api.py, all whitelisted)

- `GET  …recon_copilot.api.suggest(bank_transaction, limit=10)` — ranked
  suggestion dicts (voucher_type/name, score, per-component features,
  explanation, auto_match_eligible). Read-only.
- `GET  …recon_copilot.api.suggest_bulk(bank_account, from_date=None,
  to_date=None, limit_per_txn=5, max_txns=200)` — `{transactions: {txn:
  [suggestions]}, capped, total_unreconciled}`. Read-only.
- `POST …recon_copilot.api.accept_suggestion(bank_transaction, voucher_type,
  voucher_name)` — re-validates the voucher against the mirrored candidate
  queries, then delegates to BRT `reconcile_vouchers`.
- `GET  …recon_copilot.api.get_score_weights()` — `engine.explain_weights`
  for the UI legend.

## Recommended banking-SPA wiring (not applied)

The /banking SPA already calls BRT endpoints. To adopt the copilot:

1. In the transaction detail pane, call `suggest(txn)` and render the ranked
   list with score chips + `explanation`; use `get_score_weights()` for a
   "why these scores" legend.
2. Render a highlighted one-click "Accept" button when
   `auto_match_eligible`; on click call `accept_suggestion(...)` (never a
   background auto-write).
3. In the list view, prefetch `suggest_bulk(bank_account, from, to)` to badge
   transactions Strong/Good/Weak.
4. Desk users get the same picture from the **Reconciliation Suggestions**
   report (Accounts Manager / Accounts User).

No new hooks are required (the API is addressed by dotted path). Optional
future hook: an app-extensible `get_recon_candidate_loaders` hook mirroring
BRT's `get_matching_queries` hook (BRT:1193) so lending/other apps can feed
extra candidate types into the ranker.
