# Supplier Qualification + Sourcing Events

Implements blueprint item **W5** (`GAP_CLOSURE_BLUEPRINT.md`): supplier
onboarding/qualification with questionnaire templates, document expiry and risk
tiers, plus `Sourcing Event` RFx with sealed bids and side-by-side award
scenarios scored the way the Supplier Scorecard engine scores criteria
(weighted components normalized to 0-100).

Follows the repo's pure-core + adapter doctrine (blueprint section 2; same
shape as `erpnext/stock/wms/` and `erpnext/accounts/closing/`):

| File | Role |
|---|---|
| `models.py` | frappe-free dataclasses (`QuestionnaireAnswer`, `QualificationResult`, `DocumentRequirement`, `DocumentStatus`, `BidLine`, `AwardScore`, `AwardScenario`) + status/tier/scenario constants |
| `engine.py` | frappe-free logic: `score_questionnaire`, `evaluate_documents` / `documents_ok` / `count_expiring`, `score_bids`, `build_award_scenarios` |
| `test_sourcing.py` | pure unit tests, runnable without a site: `python erpnext/buying/sourcing/test_sourcing.py` (32 tests) |

Doctypes (module **Buying**): `Supplier Qualification Template`
(+ `Supplier Qualification Template Item`), `Supplier Qualification`
(+ `Supplier Qualification Answer`), the shared child
`Supplier Document Requirement`, `Sourcing Event` (+ `Sourcing Event Supplier`,
`Sourcing Event Item`) and `Sourcing Event Bid` (+ `Sourcing Event Bid Item`).
Reports: `Supplier Qualification Status`, `Sourcing Event Award Analysis`.

No existing file was edited. Hooks and workspace links that *should* follow are
listed in section 6.

## 1. Qualification scoring

`score_questionnaire(answers, pass_threshold_pct, risk_bands)`:

```
percent = 100 x Σ(clamp(score, 0, max_score) x weight) / Σ(max_score x weight)
passed  = percent >= pass_threshold AND no failed knockout
```

* rows with `weight <= 0` or `max_score <= 0` drop out of the ratio but are
  still evaluated for knockouts - a pure compliance gate can be scoreless;
* **any** failed knockout sets `passed = False` regardless of the score, lists
  the `question_key` in `knockout_failures`, and pins the risk tier to the
  lowest band (a supplier that fails a sanctions check is never "Low risk");
* `risk_bands` is `[(min_pct, tier), ...]`; the engine sorts them descending
  and appends a `(0, High)` catch-all if the caller did not provide one.
  Boundaries are **inclusive** (exactly 85% is Low with the defaults). The
  template supplies the bands from `low_risk_threshold` /
  `medium_risk_threshold`.

`Supplier Qualification.validate` runs this plus `evaluate_documents`, writes
`total_score` / `max_score` / `score_percent` / `risk_tier` /
`knockout_failures` and keeps `status = Draft`; `on_submit` sets **Qualified**
only when the questionnaire passed *and* all **mandatory** documents are OK,
otherwise **Rejected**. `on_cancel` returns the status to Draft.

## 2. Document expiry

`evaluate_documents(docs, as_of, expiring_within_days=30)`:

| condition | status |
|---|---|
| no attachment | `Missing` (wins over any expiry data) |
| attached, no expiry captured | `Valid`, `days_to_expiry = None` |
| expiry < `as_of` | `Expired` (negative days) |
| `0 <= days <= expiring_within_days` | `Expiring` (inclusive at both ends - expiring *today* is Expiring, not Expired) |
| otherwise | `Valid` |

`documents_ok(statuses, mandatory_only=False)` is False when anything is
`Expired` or `Missing`; the report and the submit gate use
`mandatory_only=True` so an optional bank letter never blocks qualification.
The engine never reads the clock - the controller passes
`getdate(nowdate())`.

## 3. Bid scoring

`score_bids(bid_lines, weights, scorecard_by_supplier, qualification_by_supplier)`
returns ranked `AwardScore` rows.

Price and lead time are normalized **per item** and then aggregated per
supplier as a **qty-weighted mean** over the lines that supplier bid.
Suppliers rarely bid the same basket, so a raw basket total would simply
reward whoever bid fewest lines.

* `price_score = 100 x best_price / this_price` per item. A line priced 0 or
  less is a data error, not a winning bid: it is excluded from the best-price
  search, scores 0, and is not awardable in any scenario.
* `lead_time_score = 100 x (best_lead + 1) / (this_lead + 1)` per item.
  Negative lead times are floored at 0 and `LEAD_TIME_FLOOR_DAYS = 1.0` is the
  zero guard: all-zero lead times score 100 each, and a 0-vs-2-day race scores
  100 vs 33.3 instead of the degenerate 100 vs 0 a bare ratio would give.
* scorecard and qualification arrive already on 0-100 and are clamped there. A
  supplier missing from either map scores **0 for that component but is still
  ranked** - "no scorecard yet" must not remove a bidder from the comparison.
* `weighted_total = Σ(weight x component) / Σ(weight)`; zero-weight components
  drop out. With every weight 0 the totals are 0 - `Sourcing Event.validate`
  rejects that setup before it can happen.
* ranks are 1-based on `weighted_total` descending, **ties broken by supplier
  name ascending**, so the same inputs always produce the same order (and the
  same award proposal) regardless of row order.

## 4. Award scenarios

`build_award_scenarios(bid_lines, award_scores, item_codes)` returns exactly
three proposals over the event's basket (`item_codes` = the Sourcing Event
items, so items nobody bid surface as `coverage_gaps` rather than silently
disappearing):

| Scenario | Per-item winner | Tie-break |
|---|---|---|
| **Lowest Price** | cheapest valid line | supplier name |
| **Best Weighted** | highest `weighted_total` | cheaper line, then supplier name |
| **Single Supplier** | the supplier covering the most items | cheapest covered basket, then supplier name |

Each scenario carries `total_cost = Σ(qty x unit_price)` and its
`coverage_gaps`. Single Supplier awards only what that supplier bid; the rest
is its gap list - which is exactly the trade-off a category manager wants to
see next to the cheapest split award.

## 5. Sealed bids and the award flow (design decisions)

**Sealed bids are enforced in one place.**
`Sourcing Event.get_bids_for_comparison()` throws when `sealed_until_close` is
set and the status is not `Closed`/`Awarded`. `compute_award_analysis()` and
the `Sourcing Event Award Analysis` report both go through it, so there is no
second code path that could leak values early - and no stored field that could
either.

**Closing.** `close_event(force)` moves Open -> Closed. Before `close_date` it
requires `force`, and force is restricted to **Purchase Manager / System
Manager** (`frappe.only_for`) so a buyer cannot cut a sealed event short alone.
Bids are rejected by `Sourcing Event Bid.validate` once the event is
Closed/Awarded/Cancelled, before `open_date`, or after `close_date`; one
submitted bid per supplier per event (the throw names the existing bid), and
bidders must be on the invited list when any supplier is flagged invited.

**Nothing is cached.** `compute_award_analysis()` recomputes live. The analysis
is a function of the bids, the event weights *and* each supplier's current
scorecard standing and qualification - all of which move independently of this
document. A cached copy would go stale silently and would also mean storing
sealed values in a field. The report reads the same method.

**Awards create draft Supplier Quotations.** `award_to(supplier, item_codes)`
maps the winning bid lines to a **draft** `Supplier Quotation` (same posture as
the portal mapper `create_supplier_quotation`), then sets the event to
`Awarded`. `item_codes` restricts the award so a split award is just repeated
calls, one per winner. Buyer review of taxes/terms/pricing happens on the
quotation, and the existing Supplier Quotation -> Purchase Order flow takes it
from there.

**`require_qualification`** removes suppliers without a valid, submitted,
unexpired qualification from the analysis entirely (they are reported in
`excluded_suppliers`) instead of merely scoring them 0.

**`event_type`**: `RFQ` and `RFP` are scored by this engine. `Reverse Auction`
is accepted as a value but has no live-bidding loop in v1 - the follow-up is a
bid-round child table plus a decrementing-price validation, both of which fit
under `Sourcing Event Bid` without touching the engine.

## 6. Verified fieldnames

Every field this feature reads from an existing doctype, checked against its
JSON:

| Doctype | Field | Type / note |
|---|---|---|
| `Supplier` | `supplier_name` | Data - fetched into bid / event-supplier / qualification rows |
| `Supplier` | `disabled` | Check - used in `link_filters` and `set_query` |
| `Supplier` | `supplier_group` | Link (Supplier Group) - qualification-status report filter |
| `Supplier` | `email_id` | **Read Only**, fetched from `supplier_primary_contact.email_id` - source of `Sourcing Event Supplier.contact_email` |
| `Supplier` | `on_hold`, `hold_type`, `warn_pos`, `is_internal_supplier` | exist; **not** touched by this change (the PO-gating hook in section 7 is where they would combine) |
| `Supplier Scorecard` | *name* | `autoname: field:supplier` - one scorecard per supplier |
| `Supplier Scorecard` | `supplier_score` | **Data** holding a 0-100 number (`calculate_total_score` -> `round(100.0 * total/max, 1)`) - read via `frappe.db.get_value("Supplier Scorecard", {"supplier": ...}, "supplier_score")` and `flt()`-ed |
| `Supplier Scorecard` | `status` | Data - the standing name (`apply_standing`), shown for context |
| `Supplier Quotation` | `supplier`, `company`, `transaction_date`, `currency`, `items` | all `reqd`; `conversion_rate` and `status` are also reqd and are filled by `set_missing_values` |
| `Supplier Quotation` | `valid_till` | Date - receives the bid's `valid_until` |
| `Supplier Quotation Item` | `item_code`, `qty`, `uom`, `stock_uom`, `conversion_factor`, `base_rate`, `base_amount` | reqd; the last four are resolved by `set_missing_values` / `calculate_taxes_and_totals` |
| `Supplier Quotation Item` | `item_name`, `description`, `rate`, `lead_time_days`, `expected_delivery_date` | written from the bid line + event item (there is **no** `schedule_date` on this child - `expected_delivery_date` is the equivalent) |
| `Request for Quotation Supplier` | `supplier`, `email_id`, `quote_status` | shape mirrored by `Sourcing Event Supplier` (`contact_email`, `responded`, `qualification_status`) |
| `Item` | `item_name`, `stock_uom`, `description` | `fetch_from` sources on `Sourcing Event Item` / `Sourcing Event Bid Item` |

Child `Currency` fields (`unit_price`, `amount`, `total_amount`) use
`options: "currency"`, the same parent-currency reference
`Supplier Quotation Item.rate` uses.

## 7. Follow-ups (recommended, not applied - no existing file was edited)

1. **Daily expiry sweep** - register in `erpnext/hooks.py`:

   ```python
   scheduler_events = {
       "daily": [
           "erpnext.buying.doctype.supplier_qualification.supplier_qualification.expire_qualifications",
       ],
   }
   ```

   `expire_qualifications()` is already idempotent and hook-free (it uses
   `frappe.db.set_value`). Until it is registered, the qualification-status
   report still shows lapsed rows as Expired because it recomputes
   `days_to_expiry` at read time.

2. **PO gating on qualification** - a `Purchase Order` `validate` hook (or a
   `Buying Settings` flag `block_po_without_qualification`) calling
   `get_supplier_qualification(self.supplier)` and throwing when it is missing
   or `is_qualified` is False. Deliberately not applied: it changes an
   existing, heavily used transaction and belongs behind a setting.

3. **Supplier form button** - add to `supplier.js`:
   *Qualification* -> new `Supplier Qualification` prefilled with the supplier,
   and an indicator showing the latest `status` / `risk_tier` from
   `get_supplier_qualification`. A `Supplier` dashboard entry
   (`supplier_dashboard.py`) listing `Supplier Qualification` and
   `Sourcing Event Bid` is the companion change.

4. **Workspace links** - in `erpnext/buying/workspace/buying/buying.json`, add
   under the *Buying* card: `Sourcing Event`, `Sourcing Event Bid`; under
   *Supplier Scorecard*/settings: `Supplier Qualification`,
   `Supplier Qualification Template`; and to the *Key Reports* card the two
   query reports `Supplier Qualification Status` and
   `Sourcing Event Award Analysis` (`link_type: "Report"`,
   `is_query_report: 1`, `dependencies: "Sourcing Event"`).

5. **Supplier portal for bidding** - reuse the RFQ portal plumbing
   (`erpnext/templates/pages/rfq.py` + `Portal Menu Item`) so invited
   suppliers submit `Sourcing Event Bid` themselves; the sealed rule already
   holds on the buyer side, and a portal write path only needs the same
   `Portal User` check the RFQ page performs.

6. **Reverse auction** - bid rounds on `Sourcing Event Bid` plus a
   monotonic-decrease validation; the scoring engine needs no change.
