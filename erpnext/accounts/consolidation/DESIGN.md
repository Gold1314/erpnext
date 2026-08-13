# Consolidation with Eliminations and Minority Interest

Closes blueprint item **W1/1f** (see `GAP_CLOSURE_BLUEPRINT.md`): today
consolidation in ERPNext is only the *Consolidated Financial Statement*
report, which naively sums company columns after presentation-currency
conversion. This module adds an ownership register, elimination rules and a
computed, auditable consolidation run — following the **pure-core + adapter**
doctrine (section 2 of the blueprint).

## Layout

| Path | Role |
|---|---|
| `models.py` | Pure data model (dataclasses, sign convention, account-key merging). Zero frappe. |
| `engine.py` | Pure pipeline: group resolution → translation → eliminations → minority interest → merge. Zero frappe. |
| `loaders.py` | Frappe adapters: GL balances, exchange rates, ownership edges, elimination pairs, intercompany pair suggestions. |
| `test_consolidation.py` | Standalone unit tests (importlib bootstrap; run `python erpnext/accounts/consolidation/test_consolidation.py`). |
| `../doctype/consolidation_ownership/` | Ownership register (parent, subsidiary, %, method, effectivity). |
| `../doctype/elimination_rule/` (+ `elimination_rule_account`) | Intercompany account pairs that offset each other. |
| `../doctype/consolidation_run/` (+ `consolidation_adjustment_line`) | Submittable run that computes and freezes the adjustments. |
| `../report/consolidated_statement_with_eliminations/` | Script report rendering a run. |

## Sign convention

Every balance is **signed, debit-positive** (assets/expenses ≥ 0,
liabilities/equity/income ≤ 0 in the normal case). A full trial balance sums
to zero; the engine's invariants and tests rely on that single convention.

## Pipeline semantics and deliberate simplifications

1. **Group resolution** (`resolve_group`). Effective percents multiply down
   the chain (80% of A, A owns 60% of B ⇒ B is 48%) and sum across parallel
   ownership paths, capped at 100. Cycles raise `ValueError`. **Equity
   method** stakes are validated and reported but *excluded* from
   line-by-line consolidation with a warning — equity pickup (one-line
   "Investment in Associate" accretion) is a v2 feature. Descent stops below
   an equity edge.

2. **Currency translation** (`translate`). Balance-sheet accounts (root
   types Asset/Liability/Equity) translate at the **closing rate**, P&L
   (Income/Expense) at the **average rate**. The resulting per-company
   imbalance is plugged into a synthetic **"Currency Translation
   Adjustment"** equity line so every translated column still sums to zero.
   Precondition: local columns balance — the loader guarantees this by
   appending an **"Unclosed Prior Periods Profit / Loss"** equity line per
   company (the cumulative-BS vs. period-P&L gap; same idea as the existing
   report's "Unclosed Fiscal Years Profit / Loss" row).
   *Simplification:* the average rate is a simple mean of month-end rates in
   the period (fallback: closing rate, with a warning) — not a
   transaction-weighted average; equity is not kept at historical rates.

3. **Eliminations** (`eliminate`). Each Elimination Rule pair declares two
   accounts (in two companies) whose balances offset. The engine eliminates
   **min(|a|, |b|)** against *both* sides, so a pair's elimination lines
   always net to zero and can never unbalance the consolidated column. Any
   residual (|a| ≠ |b|, or same-sign balances) is emitted as an
   **out-of-balance exception showing both balances** — it is *never*
   silently plugged. Elimination happens after translation, so both sides
   are compared in the presentation currency.

4. **Minority interest** (`minority_interest`). For each Full-method
   subsidiary with effective percent < 100:
   `MI = (1 − pct) × (post-elimination equity total + current net income)`.
   *Documented simplification:* MI is computed on equity plus current net
   income only — no split of historical reserves into pre-/post-acquisition,
   no fair-value adjustments, no goodwill. In the consolidated column MI is
   presented as a **net-zero reclass within equity**: credit "Minority
   Interest", debit "Equity Attributable to Group (NCI Reclass)" — the
   balance sheet stays balanced while MI is visible.

5. **Account merging.** Consolidated amounts merge across companies by
   account name with the `" - ABBR"` company suffix stripped
   (longest-abbreviation-first so short abbrs cannot shadow longer ones).
   Synthetic engine lines carry no suffix and pass through unchanged.

## No GL posting — reporting layer only

A Consolidation Run posts **nothing** to the General Ledger, in any company,
ever. `compute()` (draft only) stores adjustment lines, exceptions and the
MI total on the document; **submit** merely freezes the run
(status *Finalized*). The statutory books of each group company remain
untouched — consolidation is a reporting overlay. (The blueprint's eventual
end state posts eliminations into a dedicated consolidation `finance_book`
from item 1d; that is explicitly out of scope here and owned by the
multi-GAAP workstream.)

## Report determinism

The *Consolidated Statement with Eliminations* report renders **fresh
translated company columns** (so the statement always reflects the books,
including a freshly computed CTA plug) combined with the **run's stored
elimination / minority-interest lines**. Storing the adjustments on the run
keeps them deterministic: what was computed, reviewed and finalized is
exactly what renders. If the books move after `compute()`, the stored
adjustments may no longer tie to the fresh balances — the visible drift is
the cue to recompute (or amend a finalized run). Stored CTA lines are
record-keeping only; using them *and* the fresh plug would double-count.

## Intercompany pair suggestions

`loaders.suggest_intercompany_pairs(parent)` (whitelisted, read permission on
Elimination Rule) proposes candidate pairs from internal parties
(`Customer.is_internal_customer` / `represents_company`,
`Supplier.is_internal_supplier` / `represents_company`, scoped by their
"Allowed To Transact With" child tables) mapped to the companies' default
receivable/payable accounts. Party Links are implied by internal parties.
It returns **suggestions only** — it never creates rules.

## Follow-ups (recommended, not implemented)

- **Equity method (v2):** one-line pickup — accrete the investor's share of
  the associate's profit onto "Investment in Associate" instead of skipping.
- **Historical reserves split:** pre-/post-acquisition equity, goodwill and
  fair-value adjustments for a true purchase-accounting MI.
- **Investment-vs-equity auto pairs:** derive `Investment vs Equity` rule
  pairs from the ownership register + share-capital accounts.
- **Finance-book posting:** once blueprint 1d ships, optionally post the
  stored adjustment lines into a dedicated consolidation finance book.
- **Workspace links:** add Consolidation Ownership / Elimination Rule /
  Consolidation Run / the report to the Accounting workspace JSON (left
  untouched here to keep the change additive).
