# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from erpnext.analytics import registry
from erpnext.analytics.models import HIGHER_IS_BETTER, LOWER_IS_BETTER


class MetricDefinition(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amber_min: DF.Float
		category: DF.Data | None
		company: DF.Link | None
		description: DF.SmallText | None
		direction: DF.Data | None
		enabled: DF.Check
		formula_text: DF.SmallText | None
		green_min: DF.Float
		label: DF.Data | None
		metric_key: DF.Data
		notes: DF.SmallText | None
		red_max: DF.Float
		unit: DF.Data | None
	# end: auto-generated types

	def validate(self):
		spec = self.get_spec()
		self.sync_from_registry(spec)
		self.validate_threshold_order(spec)

	def get_spec(self):
		"""The catalog entry this definition governs. Unknown keys are rejected."""
		spec = registry.get_spec((self.metric_key or "").strip())
		if not spec:
			frappe.throw(
				_("{0} is not a metric in the catalog. Known metrics: {1}").format(
					frappe.bold(self.metric_key), ", ".join(sorted(registry.METRICS))
				),
				title=_("Unknown Metric"),
			)
		return spec

	def sync_from_registry(self, spec):
		"""The registry is the single source of truth for what a metric *is*.

		Only the thresholds, the enabled flag and the notes belong to the user;
		unit, direction, category and the formula always come from
		``erpnext/analytics/registry.py``.
		"""
		self.metric_key = spec.key
		self.unit = spec.unit
		self.direction = spec.direction
		self.category = spec.category
		self.formula_text = spec.formula_text
		self.description = spec.description
		if not self.label:
			self.label = spec.label

	def validate_threshold_order(self, spec):
		"""Green must be at least as demanding as Amber, per direction."""
		# Compare against None, not truthiness: zero is a real boundary for
		# Count metrics (green_min = 0 open violations), and treating it as
		# "unset" would let an inverted Green/Amber pair through unchecked.
		if self.green_min is None or self.amber_min is None:
			return

		if spec.direction == HIGHER_IS_BETTER and self.green_min < self.amber_min:
			frappe.throw(
				_(
					"{0} is higher_is_better, so the Green Boundary ({1}) must be at least the Amber Boundary ({2})."
				).format(spec.label, self.green_min, self.amber_min)
			)

		if spec.direction == LOWER_IS_BETTER and self.green_min > self.amber_min:
			frappe.throw(
				_(
					"{0} is lower_is_better, so the Green Boundary ({1}) must be at most the Amber Boundary ({2})."
				).format(spec.label, self.green_min, self.amber_min)
			)


@frappe.whitelist()
def install_default_metrics():
	"""Create one Metric Definition per catalog entry.

	Existing definitions are left untouched (thresholds are a customer's
	judgement, not ours). Shipped default bands are written only where the
	right answer is uncontroversial - metrics whose bands are industry- or
	policy-specific are created with no bands at all, which reports as
	``Unknown`` rather than pretending to grade them.
	"""
	frappe.only_for(("System Manager", "Accounts Manager"))

	created, skipped = [], []
	for spec in registry.ALL_SPECS:
		if frappe.db.exists("Metric Definition", spec.key):
			skipped.append(spec.key)
			continue

		threshold = spec.default_threshold
		doc = frappe.get_doc(
			{
				"doctype": "Metric Definition",
				"metric_key": spec.key,
				"label": spec.label,
				"enabled": 1,
				"green_min": threshold.green_min if threshold else None,
				"amber_min": threshold.amber_min if threshold else None,
				"red_max": threshold.red_max if threshold else None,
			}
		)
		doc.insert(ignore_permissions=True)
		created.append(spec.key)

	return {"created": created, "skipped": skipped, "catalog_size": len(registry.ALL_SPECS)}
