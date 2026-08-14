# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint, flt
from frappe.utils.nestedset import NestedSet


class StorageLocation(NestedSet):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		capacity_qty: DF.Float
		capacity_uom: DF.Link | None
		disabled: DF.Check
		is_group: DF.Check
		lft: DF.Int
		location_code: DF.Data
		location_name: DF.Data | None
		location_type: DF.Literal["Zone", "Aisle", "Rack", "Bin", "Staging", "QC", "Bulk", "Pick Face"]
		old_parent: DF.Link | None
		parent_storage_location: DF.Link | None
		pick_sequence: DF.Int
		rgt: DF.Int
		warehouse: DF.Link | None
	# end: auto-generated types

	nsm_parent_field = "parent_storage_location"

	def validate(self):
		self.validate_leaf_has_warehouse()
		self.validate_warehouse_matches_parent()
		self.validate_pick_sequence_and_capacity()

	def validate_leaf_has_warehouse(self):
		if not self.is_group and not self.warehouse:
			frappe.throw(
				_("Leaf storage location {0} must be linked to a Warehouse.").format(
					frappe.bold(self.location_code)
				),
				title=_("Warehouse Missing"),
			)

	def validate_warehouse_matches_parent(self):
		if not self.parent_storage_location or not self.warehouse:
			return

		parent_warehouse = frappe.db.get_value(
			"Storage Location", self.parent_storage_location, "warehouse"
		)
		if parent_warehouse and parent_warehouse != self.warehouse:
			frappe.throw(
				_("Storage Location {0} must belong to Warehouse {1}, same as its parent {2}.").format(
					frappe.bold(self.location_code),
					frappe.bold(parent_warehouse),
					frappe.bold(self.parent_storage_location),
				),
				title=_("Warehouse Mismatch"),
			)

	def validate_pick_sequence_and_capacity(self):
		if cint(self.pick_sequence) < 0:
			frappe.throw(_("Pick Sequence cannot be negative."), title=_("Invalid Pick Sequence"))

		if flt(self.capacity_qty) < 0:
			frappe.throw(_("Capacity Qty cannot be negative."), title=_("Invalid Capacity"))
