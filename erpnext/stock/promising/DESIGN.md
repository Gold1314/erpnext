# ATP Order Promising - Design

Status: **v1 implemented** - pure-core promise engine + Frappe loaders +
whitelisted API + "Get Promise Dates" button on draft Sales Orders + "Order
Promise Review" script report. Blueprint reference: `GAP_CLOSURE_BLUEPRINT.md`
W3 item 3b.

## 1. Problem

Sales Order delivery dates are user-entered; nothing checks them against
projected supply. ERPNext already knows everything needed to answer "when can
we actually ship N of this item?" - on-hand stock in `Bin`, hard reservations
in `Stock Reservation Entry`, inbound supply on open Purchase Orders and Work
Orders, and competing commitments on other open Sales Orders - but no code
assembles it into a promise. This module does exactly that, and re-checks
existing promises when supply slips.

## 2. Architecture

**Pure core, thin adapters** - modeled on `erpnext/manufacturing/scheduling/`
and `erpnext/accounts/forecasting/`:

```
                 ┌─────────────────────────────────────────────┐
                 │              promise engine                 │
   loaders.py ─▶ │  SupplyEvent[] + DemandEvent[] + request    │ ─▶ api.py
  (Bin, PO, WO,  │  ─▶ net committed demand ─▶ allocate        │    (SO button)
   SO demand)    │  ─▶ PromiseResult(date, allocation, gap)    │ ─▶ report
                 │  no frappe imports, no ambient time         │
                 └─────────────────────────────────────────────┘
```

- `models.py` / `engine.py`: **no frappe imports**. Dataclasses in, dataclass
  result out. `today` and `horizon_end` are passed in - no `today()` inside
  the core - so the engine is deterministic and unit-testable without a site
  (`python erpnext/stock/promising/test_promising.py`).
- `loaders.py`: all `frappe.qb` work. Returns event lists in **stock UOM**.
- `api.py`: whitelisted `get_promise_date` / `promise_sales_order` (read-only)
  and `apply_promise_dates` (the only write, draft Sales Orders only).
- `erpnext/stock/report/order_promise_review/`: re-promise report flagging
  committed dates that today's supply picture can no longer meet.

## 3. ATP semantics (precise)

The engine implements the "net committed demand first, then promise from the
remainder" formulation:

1. **Supply timeline.** Supply events are per (item, warehouse):
   - `On Hand` - free Bin stock, available `today`;
   - `Purchase Order` - pending PO row qty, available on the row's
     `schedule_date`;
   - `Work Order` - pending WO qty, available on `expected_delivery_date`
     (fallback `planned_end_date`).
   Supply dated **after `horizon_end`** (default today + 90 days) is dropped.
   Supply dated **before `today`** (overdue POs, late WOs) is clamped to
   `today` - it may arrive any moment, but nothing can be promised in the
   past.
2. **Commit existing demand.** Demand events (undelivered open submitted SO
   rows, sorted by `required_date`) greedily consume the **earliest-dated**
   remaining supply, one after another. A demand row consumes supply even
   when that supply arrives after its required date: the commitment exists
   whether or not it will be met on time, so a new request can never borrow
   those units. Demand is **not** horizon-filtered - a commitment due beyond
   the horizon still consumes supply. What survives is the *uncommitted*
   supply timeline.
3. **Allocate the request.** The requested qty is allocated from the
   uncommitted timeline, earliest supply first. The promise date is
   `max(latest allocated event's available_date, today, requested_date)` -
   i.e. the earliest date the full qty is on hand, never in the past, and
   never earlier than the customer asked for (no reason to promise early;
   the message reports the earliest availability separately).
4. **Shortfall.** If the uncommitted supply inside the horizon never reaches
   the requested qty: `fulfillable = False`, `promised_date = None`,
   `shortfall = qty - total uncommitted supply`, and the message says
   "insufficient planned supply within N-day horizon". The partial
   allocation found so far is still returned for diagnostics.
5. **Multi-line orders.** `promise_many` processes requests sequentially in
   list order against shared per-(item, warehouse) pools; each fulfilled
   request's allocation is subtracted before the next request is evaluated,
   so two lines of the same item never double-count one unit.
6. **Guards.** Requests with qty <= 0 return a non-fulfillable result with an
   explanatory message (no exception); supply/demand events with qty <= 0 are
   ignored. Float comparisons use an epsilon of 1e-9.

**Conservativeness note.** Greedy earliest-supply commitment is *more
conservative* than the classic cumulative-min ATP curve. Example: supply 10
today + 10 on day 10, one committed demand of 10 due day 30. The classic
forward-min balance never dips below 10, so 10 units could be promised today;
greedy netting instead lets the day-30 commitment claim today's stock and
promises the new request on day 10. Both formulations never over-promise;
greedy trades a slightly later date for a much simpler, allocation-traceable
model (every promised unit maps to a concrete supply event). This is the
"simpler correct approach" chosen deliberately for v1.

## 4. Verified fieldnames

Every fieldname queried by `loaders.py` / the report, checked against the
doctype JSONs in this repo:

| Doctype (JSON) | Fields used | Notes |
|---|---|---|
| `Bin` (`stock/doctype/bin/bin.json`) | `item_code`, `warehouse`, `actual_qty`, `reserved_stock` | `reserved_stock` = qty held by non-delivered Stock Reservation Entries (the Bin controller aggregates SRE `reserved_qty - delivered_qty` into it), so on-hand = `actual_qty - reserved_stock`. `reserved_qty` (soft SO commitment), `ordered_qty`, `planned_qty`, `projected_qty` exist but are deliberately **not** used - POs/WOs/SO demand enter as dated events instead of undated Bin aggregates. |
| `Purchase Order` (`buying/doctype/purchase_order/purchase_order.json`) | `name`, `status`, `company`, `schedule_date`, `docstatus` | Status options verified: excluded set = `Closed`, `On Hold`; `docstatus = 1` excludes Draft/Cancelled; fully-received rows drop out via the pending-qty condition. |
| `Purchase Order Item` (`buying/doctype/purchase_order_item/purchase_order_item.json`) | `parent`, `item_code`, `warehouse`, `qty`, `received_qty`, `conversion_factor`, `schedule_date`, `expected_delivery_date` | Row-level `schedule_date` ("Required By") confirmed; pending = `(qty - received_qty) * conversion_factor`, same math as the MRP report's PO supply query. |
| `Work Order` (`manufacturing/doctype/work_order/work_order.json`) | `name`, `status`, `company`, `production_item`, `fg_warehouse`, `qty`, `produced_qty`, `expected_delivery_date`, `planned_end_date` | Both date fields exist (`expected_delivery_date` Date, `planned_end_date` Datetime); `expected_delivery_date` preferred, `planned_end_date` fallback. Excluded statuses = `Stopped`, `Closed`, `Completed` (mirrors the MRP report; also admits `Submitted`/`Stock Reserved` variants, not just `Not Started`/`In Process`). |
| `Sales Order` (`selling/doctype/sales_order/sales_order.json`) | `name`, `status`, `company`, `customer`, `delivery_date`, `transaction_date` | Excluded status = `Closed`; `Completed` orders drop out via `qty > delivered_qty`; `On Hold` orders still commit demand. |
| `Sales Order Item` (`selling/doctype/sales_order_item/sales_order_item.json`) | `parent`, `item_code`, `warehouse`, `qty`, `delivered_qty`, `conversion_factor`, `delivery_date`, `idx` | Required date = child `delivery_date`, fallback parent `delivery_date`, then parent `transaction_date`. |

## 5. API surface

- `get_promise_date(item_code, warehouse, qty, company=None, requested_date=None, horizon_days=90)`
  - one-off ATP query, returns the `PromiseResult` as a dict including the
  allocation breakdown.
- `promise_sales_order(sales_order, horizon_days=90)` - draft Sales Orders
  only; runs `promise_many` over the item rows (excluding the order itself
  from committed demand), returns per-row dicts. **No writes.**
- `apply_promise_dates(sales_order, rows)` - draft only, requires write
  permission on Sales Order; sets child `delivery_date` per `idx` from the
  reviewed rows and parent `delivery_date` = max child date, then `doc.save()`.

UI: one "Get Promise Dates" button on draft Sales Orders showing a review
dialog with an "Apply Dates" primary action.

## 6. Order Promise Review report

Script report (module Stock, ref doctype Sales Order; roles Sales Manager /
Stock Manager / Sales User). For each open submitted SO item due in the filter
window it recomputes the ATP promise from today's picture, excluding the row's
own order from committed demand, and flags:

- **At Risk** - recomputed promise date > committed delivery date (supply
  slipped; overdue rows also land here since nothing can be promised in the
  past);
- **Shortfall** - not fulfillable within the horizon;
- **OK** - hidden by default (`only_at_risk` filter, default on).

Chart: donut of row counts by status. Computation is capped at **500 SO
items** per run (promise recompute is per-row); a `msgprint` announces
truncation.

## 7. v1 simplifications (documented, deliberate)

- **Demand = Sales Orders only.** Material Request demand, Production Plan
  raw-material demand and packed-item (product bundle) demand are ignored.
- **SRE / SO double counting is conservative.** Stock reserved via SRE against
  an SO is subtracted from on-hand *and* that SO's undelivered qty appears as
  demand, so such stock is counted against supply twice. This only ever makes
  promises later, never over-promises; netting SRE qty per SO out of demand
  is a follow-up.
- **Exact warehouse match** - no group-warehouse rollup (the MRP report's
  `get_descendants_of` pattern is the reference when adding it).
- **Stock UOM everywhere** in the engine; `shortfall` in API responses is in
  stock UOM even when the SO row uses another UOM.
- **No lead-time inflation, no partial-shipment proposals** - one date per
  requested qty; splitting a line into multiple shipments is a planner call.
- **CTP not included**: make-to-order items with no planned supply simply
  report a shortfall; capable-to-promise via the scheduling engine
  (`manufacturing/scheduling`) in dry-run mode is the designed follow-up
  (the two pure cores compose without new plumbing).

## 8. Follow-ups

1. Hook re-promising into supply-slip events (PO reschedule / WO delay) via
   `hooks.py` doc_events, notifying owners of At-Risk orders (this v1
   deliberately touches no hooks).
2. Include Material Request and Production Plan demand in
   `get_committed_demand`.
3. Net SRE-reserved qty out of the reserving SO's demand event.
4. `promised_date` as a persisted field on Sales Order Item (doctype JSON
   change) so promise vs. request is auditable.
5. CTP: when ATP shortfalls on a manufacturable item, call the scheduling
   engine dry-run for a finite-capacity completion date.
6. Group-warehouse support and a batch "re-promise all At Risk" action on the
   review report.
