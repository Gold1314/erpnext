# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Cycle Count Program - ABC classification + count-sheet generation.

Orchestrates the pure WMS engine (``erpnext/stock/wms/engine.py``) against
existing doctypes: velocity comes from the Stock Ledger (via
``wms.loaders.get_item_velocity``), classification results persist as one
Cycle Count Log row per (program, item), and due counts materialize as
**draft Stock Reconciliations** the counters complete and submit.

Draft-count design choice (verified against
``stock_reconciliation.py::validate``): a draft whose rows carry no ``qty``
fails ``validate_data`` ("Please specify either Quantity or Valuation Rate"),
and prefilling ``qty`` with the expected balance fails
``remove_items_with_no_change`` ("None of the items have any change"). So
count sheets are inserted with ``flags.ignore_validate = True`` (the standard
erpnext pattern for programmatic drafts, e.g. bulk_transaction.py) and blank
``qty`` - the counter records the physical count, and the full validation
runs on their save/submit as usual.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate, nowdate, nowtime

from erpnext.stock.wms import engine, loaders

#: shipped cadence defaults (days between counts per ABC class)
DEFAULT_FREQUENCY_DAYS = {"A": 30, "B": 90, "C": 365}
DEFAULT_ITEMS_PER_COUNT = 20

STOCK_RECO_NAMING_SERIES = "MAT-RECO-.YYYY.-"


class CycleCountProgram(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.stock.doctype.cycle_count_program_class.cycle_count_program_class import (
			CycleCountProgramClass,
		)

		class_settings: DF.Table[CycleCountProgramClass]
		company: DF.Link
		enabled: DF.Check
		last_classified_on: DF.Date | None
		lookback_days: DF.Int
		program_name: DF.Data
		warehouse: DF.Link
	# end: auto-generated types

	def validate(self):
		self.validate_company_and_warehouse()
		self.set_missing_class_settings()
		self.validate_class_settings()

		if cint(self.lookback_days) <= 0:
			self.lookback_days = 90

	def validate_company_and_warehouse(self):
		details = frappe.db.get_value("Warehouse", self.warehouse, ["company", "is_group"], as_dict=True)
		if not details:
			frappe.throw(_("Warehouse {0} not found.").format(self.warehouse))

		if details.company != self.company:
			frappe.throw(
				_("Warehouse {0} does not belong to Company {1}.").format(
					frappe.bold(self.warehouse), frappe.bold(self.company)
				),
				title=_("Invalid Warehouse"),
			)

		if details.is_group:
			frappe.throw(
				_(
					"Cycle Count Programs run against a leaf warehouse; {0} is a group. Create one"
					" program per leaf warehouse."
				).format(frappe.bold(self.warehouse)),
				title=_("Group Warehouse"),
			)

	def set_missing_class_settings(self):
		existing = {row.abc_class for row in self.class_settings}
		for abc_class, frequency in DEFAULT_FREQUENCY_DAYS.items():
			if abc_class not in existing:
				self.append(
					"class_settings",
					{
						"abc_class": abc_class,
						"count_frequency_days": frequency,
						"items_per_count": DEFAULT_ITEMS_PER_COUNT,
					},
				)

	def validate_class_settings(self):
		seen = set()
		for row in self.class_settings:
			if row.abc_class in seen:
				frappe.throw(
					_("Row #{0}: duplicate settings for class {1}.").format(row.idx, row.abc_class),
					title=_("Duplicate Class"),
				)
			seen.add(row.abc_class)

			if cint(row.count_frequency_days) <= 0:
				frappe.throw(
					_("Row #{0}: Count Frequency (Days) must be greater than zero.").format(row.idx),
					title=_("Invalid Frequency"),
				)

			if cint(row.items_per_count) <= 0:
				row.items_per_count = DEFAULT_ITEMS_PER_COUNT

	def get_class_settings_map(self) -> dict:
		return {row.abc_class: row for row in self.class_settings}

	def get_frequency_by_class(self) -> dict[str, int]:
		frequency = dict(DEFAULT_FREQUENCY_DAYS)
		for row in self.class_settings:
			frequency[row.abc_class] = cint(row.count_frequency_days)
		return frequency

	@frappe.whitelist()
	def classify_items(self) -> dict:
		"""Velocity-rank the warehouse's items into A/B/C and persist to Cycle Count Log.

		Universe = items with a non-zero Bin balance under the warehouse plus
		items with outgoing movement in the lookback window (zero-velocity
		stocked items classify as "C"). One log row per (program, item) is
		created or updated in place; an open "Scheduled" row keeps its status
		and linked count sheet, only its class/velocity/due date refresh.
		"""
		self.check_permission("write")
		today = getdate(nowdate())

		velocity = loaders.get_item_velocity(self.company, self.warehouse, cint(self.lookback_days) or 90)
		universe: dict[str, float] = {item: 0.0 for item in loaders.get_stocked_items(self.warehouse)}
		universe.update(velocity)

		if not universe:
			return {"classified": 0, "message": _("No stocked or moving items found for this warehouse.")}

		classes = engine.classify_abc(universe)
		frequency = self.get_frequency_by_class()

		counts = {"A": 0, "B": 0, "C": 0}
		for item_code in sorted(classes):
			abc_class = classes[item_code]
			counts[abc_class] += 1

			existing = frappe.db.get_value(
				"Cycle Count Log",
				{"program": self.name, "item_code": item_code},
				["name", "last_counted_on", "status"],
				as_dict=True,
			)

			last_counted = getdate(existing.last_counted_on) if existing and existing.last_counted_on else None
			next_due = engine.next_count_due(last_counted, frequency[abc_class], today)

			if existing:
				frappe.db.set_value(
					"Cycle Count Log",
					existing.name,
					{
						"abc_class": abc_class,
						"velocity_value": flt(universe[item_code]),
						"next_due_on": next_due,
						"warehouse": self.warehouse,
					},
				)
			else:
				frappe.get_doc(
					{
						"doctype": "Cycle Count Log",
						"program": self.name,
						"item_code": item_code,
						"warehouse": self.warehouse,
						"abc_class": abc_class,
						"velocity_value": flt(universe[item_code]),
						"next_due_on": next_due,
						"status": "Classified",
					}
				).insert()

		self.db_set("last_classified_on", today)

		return {
			"classified": len(classes),
			"by_class": counts,
			"message": _("Classified {0} items: {1} A, {2} B, {3} C.").format(
				len(classes), counts["A"], counts["B"], counts["C"]
			),
		}

	@frappe.whitelist()
	def generate_counts(self, as_of=None) -> dict:
		"""Create draft Stock Reconciliation count sheets for items whose count is due.

		Due = log's ``next_due_on`` on or before ``as_of`` and not already
		"Scheduled". Items batch per class, at most ``items_per_count`` rows
		per document. Each touched log flips to "Scheduled" with a link to
		its count sheet.
		"""
		self.check_permission("write")

		if not self.enabled:
			frappe.throw(_("Cycle Count Program {0} is disabled.").format(self.name))

		as_of = getdate(as_of or nowdate())
		settings = self.get_class_settings_map()

		created: list[str] = []
		scheduled_items = 0

		for abc_class in ("A", "B", "C"):
			setting = settings.get(abc_class)
			batch_size = cint(setting.items_per_count) if setting else DEFAULT_ITEMS_PER_COUNT
			batch_size = batch_size or DEFAULT_ITEMS_PER_COUNT

			due_logs = frappe.get_all(
				"Cycle Count Log",
				filters={
					"program": self.name,
					"abc_class": abc_class,
					"status": ("!=", "Scheduled"),
					"next_due_on": ("<=", as_of),
				},
				fields=["name", "item_code", "warehouse"],
				order_by="next_due_on asc, item_code asc",
			)

			for start in range(0, len(due_logs), batch_size):
				batch = due_logs[start : start + batch_size]
				reconciliation = self._make_draft_count_sheet(batch, as_of)
				created.append(reconciliation)

				for log in batch:
					frappe.db.set_value(
						"Cycle Count Log",
						log.name,
						{"status": "Scheduled", "stock_reconciliation": reconciliation},
					)
				scheduled_items += len(batch)

		return {
			"stock_reconciliations": created,
			"scheduled_items": scheduled_items,
			"message": _("Created {0} draft count sheet(s) covering {1} item(s).").format(
				len(created), scheduled_items
			)
			if created
			else _("No counts are due."),
		}

	def _make_draft_count_sheet(self, logs, as_of) -> str:
		"""One draft Stock Reconciliation with blank qty rows for the counter.

		Inserted with ``flags.ignore_validate`` (see module docstring): the
		expected quantity is deliberately left for the counter to establish,
		and Stock Reconciliation's own validation runs on their save/submit.
		"""
		doc = frappe.new_doc("Stock Reconciliation")
		doc.naming_series = STOCK_RECO_NAMING_SERIES
		doc.purpose = "Stock Reconciliation"
		doc.company = self.company
		doc.set_posting_time = 1
		doc.posting_date = as_of
		doc.posting_time = nowtime()
		doc.set_warehouse = self.warehouse

		for log in logs:
			doc.append(
				"items",
				{
					"item_code": log.item_code,
					"warehouse": log.warehouse or self.warehouse,
				},
			)

		doc.flags.ignore_validate = True
		doc.insert()
		return doc.name

	@frappe.whitelist()
	def sync_counts(self) -> dict:
		"""Reconcile "Scheduled" logs against their count sheets' docstatus.

		Submitted sheet -> "Counted" (last/next dates roll forward from the
		sheet's posting date); cancelled or deleted sheet -> back to
		"Classified" so the item becomes due again. Draft sheets are left
		alone. Intended to be called from the form (or a scheduler hook -
		see DESIGN.md follow-ups).
		"""
		self.check_permission("write")

		frequency = self.get_frequency_by_class()
		scheduled = frappe.get_all(
			"Cycle Count Log",
			filters={"program": self.name, "status": "Scheduled", "stock_reconciliation": ("is", "set")},
			fields=["name", "stock_reconciliation", "abc_class"],
		)

		reco_cache: dict[str, frappe._dict | None] = {}
		counted = reverted = 0

		for log in scheduled:
			if log.stock_reconciliation not in reco_cache:
				reco_cache[log.stock_reconciliation] = frappe.db.get_value(
					"Stock Reconciliation",
					log.stock_reconciliation,
					["docstatus", "posting_date"],
					as_dict=True,
				)

			reconciliation = reco_cache[log.stock_reconciliation]

			if reconciliation and reconciliation.docstatus == 1:
				counted_on = getdate(reconciliation.posting_date)
				frappe.db.set_value(
					"Cycle Count Log",
					log.name,
					{
						"status": "Counted",
						"last_counted_on": counted_on,
						"next_due_on": engine.next_count_due(
							counted_on, frequency.get(log.abc_class or "C", 365), counted_on
						),
					},
				)
				counted += 1
			elif reconciliation is None or reconciliation.docstatus == 2:
				frappe.db.set_value(
					"Cycle Count Log",
					log.name,
					{"status": "Classified", "stock_reconciliation": None},
				)
				reverted += 1

		return {
			"counted": counted,
			"reverted": reverted,
			"message": _("{0} item(s) marked Counted, {1} reverted to Classified.").format(
				counted, reverted
			),
		}
