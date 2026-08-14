# WMS-lite: Bin Locations + Cycle Counting

Implements blueprint item **W3/3a** (`GAP_CLOSURE_BLUEPRINT.md`): a `Storage
Location` tree per warehouse promoted into a real stock-ledger dimension via
the existing Inventory Dimension machinery, a pure putaway/pick/ABC engine,
and a `Cycle Count Program` that turns velocity classes into draft Stock
Reconciliation count sheets.

Follows the repo's pure-core + adapter doctrine (section 2 of the blueprint;
same shape as `erpnext/stock/promising/` and `erpnext/accounts/forecasting/`):

| File | Role |
|---|---|
| `models.py` | frappe-free dataclasses (`LocationInfo`, `PutawaySuggestion`, `PickSuggestion`) + location-type/ABC constants |
| `engine.py` | frappe-free logic: `suggest_putaway`, `suggest_picks`, `classify_abc`, `next_count_due` / `is_count_due` |
| `loaders.py` | all DB access: locations, per-location stock (via the dimension column), item velocity from SLE |
| `api.py` | whitelisted endpoints: `setup_wms_dimension`, `suggest_putaway_location`, `suggest_pick_locations` |
| `test_wms.py` | pure unit tests, runnable without a site: `python erpnext/stock/wms/test_wms.py` |

Doctypes: `Storage Location` (tree), `Cycle Count Program` (+ child
`Cycle Count Program Class`), `Cycle Count Log`. Report:
`Cycle Count Summary` (Script Report over the logs).

## 1. Storage Location as an Inventory Dimension

The `inventory_dimension` doctype was built to promote any non-child doctype
into a stock-ledger dimension by generating **custom fields** on every
inventory document plus `Stock Ledger Entry` / `Stock Closing Balance` - no
doctype JSON edits. `api.setup_wms_dimension()` (System Manager, idempotent)
creates:

```
Inventory Dimension
  dimension_name        = "Storage Location"   (also the record name: autoname field:dimension_name)
  reference_document    = "Storage Location"
  apply_to_all_doctypes = 1
  reqd                  = 0                     (locations opt-in per row)
  validate_negative_stock = 0
```

Controller behavior verified in `inventory_dimension.py`:

- `before_save -> set_source_and_target_fieldname()` derives **both**
  `source_fieldname` and `target_fieldname` as `scrub(dimension_name)` -> the
  SLE column is **`storage_location`** (transfer fields
  `to_storage_location` / `from_storage_location` appear on Stock Entry
  Detail etc.).
- `before_save -> reset_value()` clears `document_type` / `condition` /
  `istable` / `mandatory_depends_on` and `set_type_of_transaction()` forces
  `type_of_transaction = "Both"` when `apply_to_all_doctypes` is set - so the
  installer does not set them.
- `on_update -> add_custom_fields()` generates the fields; `on_trash`
  removes them.
- `validate_reference_document()` forbids child tables and
  Batch/Serial No/Warehouse/Item - Storage Location qualifies.

Loaders never re-derive the column name; they read `target_fieldname` back
from the Inventory Dimension record and raise a clear
`WMSDimensionNotInstalled` error (pointing at the installer) when it is
absent. `get_locations` degrades gracefully instead (current qty 0) so
putaway suggestions still work pre-installation.

## 2. Engine semantics

**Putaway** (`suggest_putaway(locations, incoming_qty)`): candidates sort by
`(finite-capacity first, type rank, pick_sequence, code)` where the type rank
prefers Bin > Pick Face > Bulk > structural (Zone/Aisle/Rack) > Staging > QC.
Finite locations contribute `capacity_qty - current_qty`; `capacity_qty = 0`
means unlimited and is used only as a last resort. Returns suggestions plus
the unallocated remainder (non-zero only when everything finite is full and
no unlimited location exists).

**Picking** (`suggest_picks(locations_with_stock, required_qty)`): walk the
pick path - `pick_sequence` ascending, largest on-hand qty as tiebreak, code
for determinism - consuming until satisfied; returns suggestions plus
shortfall.

**ABC** (`classify_abc(items_with_velocity, a_pct=0.8, b_pct=0.95)`): rank by
consumption value descending (item code tiebreak -> deterministic); an item's
class comes from the cumulative share *before* it (A while < 80%, B while
< 95%, else C). The "before" convention keeps the top item A even when it
alone exceeds the boundary. Zero/negative velocity is always C; zero total
velocity -> everything C.

**Cycle-count math** (`next_count_due(last_counted, frequency_days, as_of)`):
never-counted items are due at `as_of`; otherwise `last_counted +
frequency_days`.

## 3. Cycle Count Program

- `classify_items()` - universe = items with non-zero Bin balance under the
  warehouse + items with outgoing SLE in the lookback window; velocity =
  `sum(-actual_qty * valuation_rate)` of non-cancelled outgoing SLE rows.
  Results persist as **one `Cycle Count Log` row per (program, item)**
  (status `Classified`), updated in place on re-classification - a dedicated
  child table was rejected because thousands of item rows do not belong on
  the program document.
- `generate_counts(as_of)` - logs due (`next_due_on <= as_of`, not already
  `Scheduled`) batch per class into **draft Stock Reconciliations**
  (`purpose = "Stock Reconciliation"`, at most `items_per_count` rows per
  document); touched logs flip to `Scheduled` with the document link.
- `sync_counts()` - submitted sheet -> log `Counted` (dates roll forward from
  the sheet's `posting_date`); cancelled/deleted sheet -> back to
  `Classified`.
- Programs are restricted to **leaf warehouses** (one program per leaf) so
  count-sheet rows always carry a postable warehouse; group-warehouse fan-out
  is a follow-up.

### Draft-reconciliation design choice

`StockReconciliation.validate` is strict even on draft save (verified in
`stock_reconciliation.py`): `validate_data` rejects rows with neither `qty`
nor `valuation_rate`, and `remove_items_with_no_change` throws
`EmptyStockReconciliationItemsError` when every row matches the current
balance - so neither "blank qty" nor "prefill expected qty" survives a normal
`insert()`. Count sheets are therefore inserted with
`flags.ignore_validate = True` (the established erpnext pattern for
programmatic drafts, e.g. `bulk_transaction.py`, `buying_controller.py`) and
**blank qty**, which also matches counting discipline: the counter should
record what is physically there, not confirm a suggested number. Mandatory
checks still run on insert (`purpose`, `company`, `naming_series`, row
`item_code`/`warehouse` are all set); the full validation runs when the
counter saves/submits. Note: `validate_inventory_dimension` blocks dimension
values on reconciliation rows that carry a `current_qty` (dimensions are for
opening entries only there), so count sheets stay at item+warehouse grain -
location-level counting needs the Stock Entry-based flow (follow-up below).

## 4. Verified fieldnames

| Doctype | Fields used | Verified in |
|---|---|---|
| Inventory Dimension | `dimension_name`, `reference_document`, `apply_to_all_doctypes`, `reqd`, `validate_negative_stock`, `source_fieldname`, `target_fieldname`, `type_of_transaction`, `document_type`, `istable`, `condition`, `mandatory_depends_on` | `inventory_dimension.json` / `.py` |
| Stock Ledger Entry | `item_code`, `warehouse`, `actual_qty`, `valuation_rate`, `posting_date`, `company`, `is_cancelled` (+ generated `storage_location`) | `stock_ledger_entry.json` |
| Stock Reconciliation | `naming_series`, `purpose`, `company`, `posting_date`, `posting_time`, `set_posting_time`, `set_warehouse`, `items`, `docstatus` | `stock_reconciliation.json` / `.py` |
| Stock Reconciliation Item | `item_code`, `warehouse`, `qty`, `valuation_rate`, `current_qty` | `stock_reconciliation_item.json` |
| Warehouse | `company`, `is_group`, `lft`, `rgt`, `disabled` | `warehouse.json` |
| Bin | `item_code`, `warehouse`, `actual_qty` | `bin.json` (also used by promising loaders) |

## 5. Follow-ups (recommended, not applied - no existing files were edited)

1. **Pick List integration**: order `pick_list.set_item_locations` output by
   `Storage Location.pick_sequence` and stamp the row's location; the
   `suggest_pick_locations` endpoint is the ready-made source.
2. **Putaway Rule bridging**: extend `apply_putaway_rule` to resolve to
   locations after the warehouse is chosen (`suggest_putaway_location` is the
   drop-in second stage), and reuse Putaway Rule's per-item capacity where a
   location holds a single item.
3. **Handheld PWA** (banking-SPA stack): scan-driven receive/putaway/pick/
   count against `api.py` + existing `scan_barcode` endpoints - half the
   perceived WMS value per the blueprint.
4. **Scheduler hooks**: nightly `classify_items` + `generate_counts` +
   `sync_counts` for enabled programs (needs a `hooks.py` entry, deliberately
   not edited here); alternatively an `on_submit`/`on_cancel` hook on Stock
   Reconciliation to sync logs immediately.
5. **Location-level count sheets**: a Stock Entry ("Material Transfer")
   based recount flow per location, since Stock Reconciliation refuses
   dimension values on non-opening rows.
6. **Workspace/dashboard links**: add Storage Location, Cycle Count Program
   and the report to the Stock workspace JSON.
7. **Capacity UOM conversion**: `capacity_uom` is stored but v1 interprets
   `capacity_qty` in stock UOM across items; per-UOM conversion needs item
   context.
