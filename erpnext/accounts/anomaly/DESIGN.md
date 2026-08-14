# Accounting Anomaly & Duplicate Detection

Implements the W6 anomaly screen from `GAP_CLOSURE_BLUEPRINT.md` ("Anomaly &
duplicate detection: nightly screens (duplicate invoices, unusual
account/dimension combos, off-trend postings) feeding the
`ledger_health_monitor` surface that already exists").

## Architecture

Pure-core + adapter, per blueprint section 2 and the shape proven by
`erpnext/accounts/forecasting/` and `erpnext/manufacturing/scheduling/`:

| Layer | File | Frappe? | Role |
|---|---|---|---|
| Model | `models.py` | no | dataclasses: `InvoiceRecord`, `GLMovement`, `PostingRecord`, `Finding` |
| Engine | `engine.py` | no | five deterministic checks + fingerprinting/dedupe; stdlib math only (`difflib`, `math`) |
| Loaders | `loaders.py` | yes | all DB reads; converts rows to model objects, derives weekday/backdating pure-side |
| Scanner | `scanner.py` | yes | settings-gated orchestration, dedupe, `Anomaly Finding` inserts, whitelisted manual trigger |
| Surface | `Anomaly Finding` doctype, `Anomaly Detection Settings` single, `Anomaly Findings Summary` report | yes | triage workflow + reporting |

The engine has **zero frappe imports and no ambient time** - every check is a
pure function of its inputs, unit-tested without a site
(`python erpnext/accounts/anomaly/test_anomaly.py`, importlib bootstrap
borrowed from `erpnext/accounts/forecasting/test_engine.py`).

The scheduled entry point follows the pattern of
`erpnext.accounts.utils.run_ledger_health_checks`
(`erpnext/accounts/utils.py:2656`, wired as a `daily` job at
`erpnext/hooks.py:520`): read the settings single, bail out unless enabled,
then run per company and persist one log row per finding. On top of that
pattern the scanner wraps each company in try/except + `frappe.log_error`, so
one company's bad data cannot starve the rest of the nightly run.

## Checks and threshold rationale

### 1. `duplicate_invoice` — duplicate supplier invoices
Pairs of submitted Purchase Invoices with the same supplier, amounts within
**0.5%** and effective dates (bill date, falling back to posting date) within
**45 days**, where the bill numbers are equal after normalization (case,
spaces, punctuation, leading zeros in digit runs — `INV-001` == `inv 001` ==
`INV0001`), similar (difflib ratio >= **0.85**), or at least one is empty.

- equal/similar bill no -> **High** (double capture of the same supplier bill);
- empty bill no, amount+date only -> **Medium** (weaker evidence).
- Two different, dissimilar bill numbers are *not* flagged - a supplier
  legitimately issues many same-amount bills (rent, retainers).

45 days covers a typical monthly re-send/re-key cycle plus mail lag without
pairing genuinely recurring monthly invoices at the far end; 0.5% absorbs
rounding/FX noise while excluding materially different bills; 0.85 is
difflib's ratio for one substituted character in a ~7-char reference, the
classic typo distance. Each pair is emitted once in stable (date, name)
order — no (A,B)+(B,A) mirrors.

### 2. `account_outlier` — off-trend monthly movement
Latest month's net movement (debit − credit) per account vs the mean and
**population** standard deviation of at least **6** prior months. |z| >=
**3.0** flags (99.7% band for roughly normal history); |z| >= **4.5** is
High. Zero stddev (flat history) is skipped - any deviation from a constant
series is an infinite z and mostly means a dormant account waking up, which
check 3 covers better. Score = |z|.

### 3. `rare_combination` — unusual account/dimension pairs
A (account, cost center) combination seen in at most **1%** of the account's
postings, on accounts with at least **20** postings (below that, "1%" is one
posting of twenty — noise). Medium at <= 0.5%, Low between 0.5% and 1%.
Score = `rarity_pct − share_pct`, so rarer scores higher.

### 4. `suspicious_posting` — round / weekend / backdated JEs
Per manual Journal Entry, signals **stack** into one finding:
- round amount (whole multiple of 1000, >= **10,000**): +1.0 — estimates and
  fabricated entries cluster on round numbers; the floor keeps petty cash out;
- weekend posting (weekday 5/6): +1.0 — manual entries outside working days;
- backdated >= **14 days** (posting date vs record creation date): +1.5 —
  a fortnight allows normal month-end catch-up but not period re-opening.

Severity: Medium when backdated or when two or more signals coincide,
otherwise Low.

### 5. `benford_deviation` — first-digit distribution
Chi-square of observed first-digit frequencies of positive GL debit amounts
against Benford's law, per scope (the scanner runs one scope per GL voucher
type). Flags only when:

- **n >= 300** usable amounts — below that the test's false-alarm rate makes
  it operationally useless; and
- **chi2 > 20.09**, the critical value of the chi-square distribution at
  significance **0.01** for **8 degrees of freedom** (9 first-digit bins − 1).
  Standard table value chi2(0.99, df=8) = 20.090: an exceedance means the
  observed digits would arise from a Benford-conforming population less than
  1% of the time. The constant lives in
  `engine.BENFORD_CHI2_CRITICAL_0_01`.

One Medium finding per deviating scope, with the top three deviating digits
(largest chi-square contributions) in the details. Score = the statistic.

All thresholds are engine defaults mirrored as editable fields on
**Anomaly Detection Settings** (Single, System Manager only). When the master
`enabled` flag is on, the scan applies to **all companies** — per-company
scoping is deliberately out of v1 (the Ledger Health Monitor's company child
table can be retrofitted later if needed).

## Fingerprinting & re-scan behavior

`compute_fingerprint(finding)` = SHA-1 over `check_key | entity_type |
entity_name |` the *identity-carrying* detail keys per check
(`IDENTITY_DETAIL_KEYS`): invoice pair for duplicates, period for outliers,
dimension value for rare combos, flag set for suspicious postings, nothing
extra for Benford (the scope is the entity). Volatile values (scores, z,
percentages) are excluded, so a rescan re-identifies the same finding even
when the numbers drift.

`dedupe_against_known` drops findings whose fingerprint is already stored on
an Anomaly Finding in a **blocking status** — Open, Investigating,
Confirmed Issue or **False Positive** (a dismissed false positive must stay
quiet forever). **Resolved is not blocking**: a resolved anomaly that recurs
is a new fact and is raised again. The `AnomalyFinding.before_insert` guard
enforces the same rule at the document layer.

`Anomaly Finding` (autoname `ANOM-{#####}`, `track_changes`) carries the
triage workflow Open → Investigating → Confirmed Issue → False
Positive/Resolved; closure stamps `resolved_by`/`resolved_on` via the
controller, mirroring
`erpnext/accounts/doctype/sod_violation_log/sod_violation_log.py`.

## Field names verified against doctype JSONs

- **Purchase Invoice**: `supplier`, `bill_no`, `bill_date`, `grand_total`,
  `posting_date`, `docstatus`, `company`
- **GL Entry**: `account`, `posting_date`, `debit`, `credit`, `cost_center`,
  `voucher_type`, `voucher_no`, `company`, `is_cancelled`
- **Journal Entry**: `name`, `total_debit`, `posting_date`, `company`,
  `docstatus`, plus framework-standard `creation` / `owner`

## Deployment (not applied by this change)

Recommended scheduler wiring in `erpnext/hooks.py` (`scheduler_events` →
`"daily"`, next to `run_ledger_health_checks` in `daily_maintenance`):

```python
"erpnext.accounts.anomaly.scanner.run_scheduled_scan",
```

Optional workspace links (Accounting workspace): `Anomaly Finding` (list),
`Anomaly Detection Settings`, report `Anomaly Findings Summary`.
