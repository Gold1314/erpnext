# Multi-GAAP Finance-Book Adjustments (`erpnext/accounts/multibook`)

Implements GAP_CLOSURE_BLUEPRINT.md **W1 item 1d** in its SAFE v1 form. Per the
blueprint's own risk note ("This is the highest-risk item in W1"), this module
does **not** intercept the GL map. Instead it generates **book-specific
adjustment Journal Entries** tagged with a `finance_book`, so that the
existing book-filtered reports show policy-adjusted numbers with zero changes
to any posting path. Doctrine (blueprint section 2): pure-core + adapter,
orchestrate existing doctypes (Journal Entry does the posting), ledger-adjacent
lives in `erpnext/accounts`.

## 1. The report semantics this module targets (quoted)

`finance_book` already exists on GL Entry and Journal Entry, and every
book-aware report resolves a "book view" the same way.

**General Ledger** — `erpnext/accounts/report/general_ledger/general_ledger.py:313-332`
(`get_conditions`):

```python
if filters.get("include_default_book_entries"):
	if filters.get("finance_book"):
		if filters.get("company_fb") and cstr(filters.get("finance_book")) != cstr(
			filters.get("company_fb")
		):
			frappe.throw(
				_("To use a different finance book, please uncheck 'Include Default FB Entries'")
			)
		else:
			conditions.append("(finance_book in (%(finance_book)s, '') OR finance_book IS NULL)")
	else:
		conditions.append("(finance_book in (%(company_fb)s, '') OR finance_book IS NULL)")
else:
	if filters.get("finance_book"):
		conditions.append("(finance_book in (%(finance_book)s, '') OR finance_book IS NULL)")
	else:
		conditions.append("(finance_book in ('') OR finance_book IS NULL)")

if not filters.get("show_cancelled_entries"):
	conditions.append("is_cancelled = 0")
```

(The DuckDB path repeats the identical predicate at
`general_ledger.py:1067-1083`.)

**Financial statements** — `erpnext/accounts/report/financial_statements.py:821-837`
(`get_gl_entries` query builder):

```python
if filters.get("include_default_book_entries"):
	company_fb = frappe.get_cached_value("Company", filters.company, "default_finance_book")

	if filters.finance_book and company_fb and cstr(filters.finance_book) != cstr(company_fb):
		frappe.throw(
			_("To use a different finance book, please uncheck 'Include Default FB Entries'")
		)

	query = query.where(
		(gl_entry.finance_book.isin([cstr(filters.finance_book), cstr(company_fb), ""]))
		| (gl_entry.finance_book.isnull())
	)
else:
	query = query.where(
		(gl_entry.finance_book.isin([cstr(filters.finance_book), ""]))
		| (gl_entry.finance_book.isnull())
	)
```

`erpnext/accounts/utils.py:204-229` (`get_balance_on`) uses the same
`is_cancelled=0` condition and the same book semantics through its
`finance_book` / `include_default_fb_balances` parameters.

**Consequences the design is built on:**

1. **"Book B view" = entries tagged B _plus_ all untagged entries.**
   Even with `include_default_book_entries` unchecked, the General Ledger
   condition is `finance_book in (B, '') OR finance_book IS NULL` — untagged
   ("common") entries are *always* included in a book's view.
2. **"Default book view" (no book selected) = untagged entries only**
   (`finance_book in ('') OR finance_book IS NULL`).
3. **An entry tagged B is invisible to every other view** — other books and
   the no-book view alike.
4. Cancelled ledger rows are excluded with `is_cancelled = 0`, not by
   docstatus (GL Entry is submittable, but every report and `get_balance_on`
   filter on `is_cancelled`).

Therefore: posting one Journal Entry with `finance_book = B` layers a
policy delta on top of the shared numbers **only** in book B's reports.
That is the entire mechanism of this module.

## 2. Components

| Piece | Path | Role |
|---|---|---|
| Pure model | `multibook/models.py` | `PolicyRule`, `AccountMovement`, `AdjustmentLine` dataclasses; rule-type constants. Zero frappe. |
| Pure engine | `multibook/engine.py` | `build_adjustment_lines`, `balance_lines`, `periods_overlap`, `validate_rule`, `round2`. Zero frappe. |
| Loaders | `multibook/loaders.py` | All DB access: `get_policy_rules`, `get_account_movements`. |
| Rule doctype | `accounting_policy_rule/` | One policy delta definition per company + finance book. |
| Run doctype | `finance_book_adjustment/` (+ child `finance_book_adjustment_line/`) | Submittable run document: compute -> preview -> submit posts one book-tagged JE. |
| Report | `report/finance_book_comparison/` | Side-by-side per-account balances of two book views + delta. |
| Tests | `multibook/test_multibook.py` | Pure stand-alone suite (importlib bootstrap, 21 tests). |

## 3. Rule semantics (engine contract)

Movements are **net movements** per account over the adjustment period:
`SUM(debit) - SUM(credit)` in company currency, **positive = net debit**.

- **Reclassify** — move `percentage`% of `source_account`'s period movement to
  `target_account` inside the book. Net-debit movement `M > 0` with fraction
  `p`: **Credit source `round2(M x p)`, Debit target `round2(M x p)`**;
  net-credit movement is mirrored (Debit source, Credit target). Example:
  local-GAAP rent expense reclassified to ROU depreciation under IFRS 16.
- **Exclude** — negate `percentage`% of `source_account`'s period movement in
  the book, offset to `target_account`. Mechanically identical to Reclassify
  (the sign-reversing entry on source, opposite side on target); the intent
  differs — target is the balancing offset (e.g. a "GAAP adjustment" equity
  account), so the source account simply shows less/none of the movement in
  this book.
- **Manual Amount** — fixed entry: Debit `manual_debit_account` /
  Credit `manual_credit_account` of `manual_amount`.

**Consolidation:** duplicate accounts across all rules are summed and
*netted* (debit total minus credit total keeps only the larger side), so each
account appears at most once in the JE; zero lines are dropped;
`AdjustmentLine.source_rules` accumulates every contributing rule (the child
row's `source_rule` link is set only when exactly one rule contributed — the
JE remark always names the rules).

**Money:** every line amount is rounded **half-up to 2dp at line level**
(`Decimal(str(x))`-based, so binary-float noise cannot flip the rounding);
consolidation sums Decimals. `balance_lines` then asserts
`sum(debit) == sum(credit)` at 2dp: a residual up to one cent is absorbed into
the largest line; anything larger raises `ValueError` (surfaced as a throw by
the doctype adapter). v1 posts in **company currency only** — submitting an
adjustment whose accounts are denominated in another currency is blocked.

## 4. Finance Book Adjustment lifecycle

```
Draft --compute()--> Computed --submit--> Posted --cancel--> Cancelled
```

- `compute()` (whitelisted, draft only): loads enabled rules for
  company + finance book, loads movements for the rules' source accounts over
  `from_date..to_date`, runs the engine, fills the read-only preview grid and
  totals, status `Computed`. Changing company/book/period on a draft clears
  computed lines (stale-input protection).
- `on_submit`: creates and submits **one** Journal Entry —
  `voucher_type = "Journal Entry"` (no better-suited option exists in
  `journal_entry.json`'s `voucher_type` Select), `finance_book` set,
  `posting_date` (defaulted to `to_date`, may not precede it), account rows
  from the computed lines with `debit`/`credit` +
  `debit_in_account_currency`/`credit_in_account_currency`,
  `account_currency`, optional `cost_center`, and a `user_remark` naming the
  adjustment, book, period and contributing rules. Link stored in
  `journal_entry`, status `Posted`.
- `on_cancel`: cancels the linked Journal Entry (if still submitted), status
  `Cancelled` (pattern: `lease_contract.py` / asset depreciation JE
  cancellation).

### Re-run / compounding protection (two independent layers)

1. **Movement base = untagged entries only.** `loaders.get_account_movements`
   filters `(finance_book = '' OR finance_book IS NULL)` — the common layer
   that every book view shares (quoted semantics above). Adjustment JEs are
   tagged with a book, so **an adjustment can never feed the base of a later
   computation**: cancel-and-redo, recompute, or adjusting the next period
   never compounds earlier adjustments. This is strictly stronger than only
   excluding *this* book's entries: entries tagged with *other* books are
   invisible in this book's view too, so they must not shift its base either.
2. **One submitted adjustment per company + book + day.** `validate()` rejects
   a document whose `from_date..to_date` overlaps (inclusive, via the pure
   `engine.periods_overlap`) any *other submitted* Finance Book Adjustment for
   the same company + finance book, naming the conflicting document. So a
   period's movement can be adjusted at most once per book.

## 5. Finance Book Comparison report

Script Report over GL Entry (roles: Accounts Manager, Accounts User,
Auditor). For `finance_book_1` (required) and `finance_book_2` (optional) it
computes per-account balances in each **book view**, mirroring the quoted
General Ledger predicate verbatim (`finance_book_comparison.py::get_book_condition`):

- book selected: `(gle.finance_book IN (%(book)s, '') OR gle.finance_book IS NULL)`
- book 2 empty -> "default book view": `(gle.finance_book IN ('') OR gle.finance_book IS NULL)`

Balance definition: Income/Expense rows show the net movement inside
`from_date..to_date`; Asset/Liability/Equity rows show the closing balance as
of `to_date`. Balances carry their natural sign (credit-normal root types are
negated); `delta = book_1 - book_2`. Rows are grouped under bold root-type
header rows; the chart plots the top 10 accounts by `abs(delta)`.
Selecting the same book twice is rejected. `is_cancelled = 0` throughout.

## 6. Verified fieldnames

| Doctype | Fields used | Verified against |
|---|---|---|
| GL Entry | `account`, `debit`, `credit`, `posting_date`, `company`, `finance_book`, `is_cancelled` | `accounts/doctype/gl_entry/gl_entry.json` |
| Journal Entry | `voucher_type` (Select, "Journal Entry" option), `company`, `posting_date`, `finance_book` (Link Finance Book), `user_remark`, `accounts` (Table Journal Entry Account) | `accounts/doctype/journal_entry/journal_entry.json` |
| Journal Entry Account | `account`, `debit`, `debit_in_account_currency`, `credit`, `credit_in_account_currency`, `account_currency`, `cost_center`, `user_remark` | `accounts/doctype/journal_entry_account/journal_entry_account.json` |
| Account | `company`, `is_group`, `root_type`, `account_name`, `disabled` | `accounts/doctype/account/account.json` |
| Finance Book | autoname `field:finance_book_name` | `accounts/doctype/finance_book/finance_book.json` |

## 7. v1 limitations / v2 direction

- Company-currency accounts only (multi-currency adjustment JEs need exchange
  rates and `multi_currency` handling).
- Movements are period *net* movements per account — no per-voucher or
  per-dimension splitting (a single optional cost center is stamped on the JE).
- Rules apply at computation time; editing a rule after posting does not
  restate history (cancel + amend the adjustment to re-run).
- The blueprint's full 1d (book-expansion inside the GL map, balance assertion
  per book behind a company opt-in flag, reposting tooling) remains the v2
  path; everything here — rules, engine, comparison report — carries over.

## 8. Not applied here (recommended follow-ups)

- No `hooks.py` change is required for the doctypes/report to function (they
  are standard module content discovered via `modules.txt`'s Accounts module).
- Workspace: add "Accounting Policy Rule", "Finance Book Adjustment" and the
  "Finance Book Comparison" report to
  `erpnext/accounts/workspace/accounts/accounts.json` (Financial Reports /
  Multi-GAAP card) in a follow-up, since editing existing files was out of
  scope for this change.
