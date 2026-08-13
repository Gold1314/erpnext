# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Whitelisted entry points for the semantic metric layer.

Three calls, one shared pipeline (loaders -> engine -> thresholds):

- :func:`get_kpis` - compute the pack for a company/period, graded and trended.
- :func:`snapshot_kpis` - persist that result as ``Metric Snapshot`` rows
  (idempotent per company + metric + period).
- :func:`get_metric_catalog` - the governed catalog itself, as plain dicts, so
  a UI or an agent can discover what a metric means before quoting it.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import get_first_day, get_last_day, getdate, now_datetime, today

from erpnext.analytics import engine, loaders, registry
from erpnext.analytics.models import COUNT, Threshold

READ_ROLES = ("System Manager", "Accounts Manager", "Accounts User", "Auditor")
WRITE_ROLES = ("System Manager", "Accounts Manager")


def _resolve_period(period_start=None, period_end=None) -> tuple:
	"""Default to the current calendar month; validate ordering."""
	end = getdate(period_end) if period_end else get_last_day(today())
	start = getdate(period_start) if period_start else get_first_day(end)
	if start > end:
		frappe.throw(_("Period Start Date cannot be after Period End Date."))
	return start, end


def _threshold_from_row(row, spec) -> Threshold | None:
	"""Read one Metric Definition row's bounds.

	Frappe ``Float`` columns are ``NOT NULL DEFAULT 0``, so a stored ``0``
	cannot be told apart from "left blank". Two rules resolve that, and they
	are the same two documented on the Metric Definition form:

	1. a row whose three bounds are all ``0`` configures nothing - the
	   registry's shipped default applies (this is how ``working_capital``
	   keeps its "negative is Red" hard bound);
	2. in any other row, ``0`` is a real bound only for ``Count`` metrics,
	   where "zero open findings" is the natural target; for money, ratio,
	   percent and days metrics ``0`` means unset.
	"""
	bounds = (row.green_min, row.amber_min, row.red_max)
	if not any(bounds):
		return None

	zero_is_a_bound = spec.unit == COUNT

	def bound(value):
		if value is None:
			return None
		if value == 0 and not zero_is_a_bound:
			return None
		return value

	return Threshold(
		key=spec.key,
		green_min=bound(row.green_min),
		amber_min=bound(row.amber_min),
		red_max=bound(row.red_max),
	)


def _thresholds(company: str) -> tuple[dict, set]:
	"""Resolved thresholds per metric, and the set of disabled metrics.

	A Metric Definition scoped to ``company`` wins over a global (company-less)
	one, which in turn wins over the registry default - so the board is useful
	before anyone presses "Install Default Metrics".
	"""
	resolved = {spec.key: spec.default_threshold for spec in registry.ALL_SPECS}
	disabled: set = set()

	if not frappe.db.table_exists("Metric Definition"):
		return resolved, disabled

	rows = frappe.get_all(
		"Metric Definition",
		filters={"company": ("in", [company, "", None])},
		fields=["metric_key", "company", "enabled", "green_min", "amber_min", "red_max"],
		# company-scoped rows are read last so they overwrite the global ones
		order_by="company asc",
	)
	for row in rows:
		spec = registry.get_spec(row.metric_key)
		if not spec:
			continue
		resolved[spec.key] = _threshold_from_row(row, spec) or spec.default_threshold
		if row.enabled:
			disabled.discard(spec.key)
		else:
			disabled.add(spec.key)

	return resolved, disabled


def compute_kpis(company: str, period_start, period_end, keys=None) -> list[dict]:
	"""Shared pipeline behind :func:`get_kpis` (no permission checks).

	Loads inputs for the period and the immediately preceding equal-length
	period, computes every requested metric on both, grades the current value
	against the resolved thresholds and attaches the trend.
	"""
	specs = registry.get_specs(keys)
	thresholds, disabled = _thresholds(company)
	if not keys:
		specs = [spec for spec in specs if spec.key not in disabled]

	inputs, warnings = loaders.collect_inputs(company, period_start, period_end)
	prior_start, prior_end = loaders.prior_period(period_start, period_end)
	prior_inputs, _prior_warnings = loaders.collect_inputs(company, prior_start, prior_end)

	current_values = engine.compute_many(
		specs, shared_inputs=inputs, company=company, period_start=period_start, period_end=period_end
	)
	prior_values = engine.compute_many(
		specs,
		shared_inputs=prior_inputs,
		company=company,
		period_start=prior_start,
		period_end=prior_end,
	)

	results = []
	for spec, value, prior in zip(specs, current_values, prior_values, strict=True):
		threshold = thresholds.get(spec.key)
		movement = engine.trend([prior, value], spec)
		results.append(
			{
				"key": spec.key,
				"label": spec.label,
				"unit": spec.unit,
				"grain": spec.grain,
				"direction": spec.direction,
				"category": spec.category,
				"value": value.value,
				"status": engine.evaluate_threshold(spec, value.value, threshold),
				"prior_value": prior.value,
				"change": movement["change"],
				"pct_change": movement["pct_change"],
				"direction_is_good": movement["direction_is_good"],
				"sparkline": movement["sparkline"],
				"formula_text": spec.formula_text,
				"description": spec.description,
				"inputs_used": value.inputs_used,
				"warnings": value.warnings + warnings,
				"period_start": str(getdate(period_start)),
				"period_end": str(getdate(period_end)),
				"prior_period_start": str(getdate(prior_start)),
				"prior_period_end": str(getdate(prior_end)),
			}
		)

	return results


@frappe.whitelist()
def get_kpis(company: str, period_start=None, period_end=None, keys=None) -> list[dict]:
	"""The KPI pack for one company and period, graded and trended.

	``keys`` may be a list or a JSON string; omitting it returns every enabled
	metric. The period defaults to the current calendar month.
	"""
	frappe.only_for(READ_ROLES)
	if not company:
		frappe.throw(_("Company is required."))
	frappe.has_permission("Company", doc=company, throw=True)

	if isinstance(keys, str):
		keys = frappe.parse_json(keys) if keys.strip().startswith("[") else [keys]

	start, end = _resolve_period(period_start, period_end)
	return compute_kpis(company, start, end, keys)


@frappe.whitelist()
def snapshot_kpis(company: str, period_start=None, period_end=None, keys=None) -> dict:
	"""Persist the pack as ``Metric Snapshot`` rows.

	Idempotent per company + metric_key + period: an existing snapshot for the
	same triple is updated in place rather than duplicated, so re-running a
	month never doubles the history.
	"""
	frappe.only_for(WRITE_ROLES)
	if not company:
		frappe.throw(_("Company is required."))
	frappe.has_permission("Company", doc=company, throw=True)

	start, end = _resolve_period(period_start, period_end)
	rows = compute_kpis(company, start, end, keys)
	computed_on = now_datetime()

	created, updated = 0, 0
	for row in rows:
		existing = frappe.db.get_value(
			"Metric Snapshot",
			{
				"company": company,
				"metric_key": row["key"],
				"period_start": start,
				"period_end": end,
			},
			"name",
		)
		payload = {
			"label": row["label"],
			"value": row["value"],
			"unit": row["unit"],
			"status": row["status"],
			"computed_on": computed_on,
			"inputs_json": json.dumps(row["inputs_used"], default=str, indent=1),
			"warnings": "\n".join(row["warnings"])[:1000] or None,
		}

		if existing:
			doc = frappe.get_doc("Metric Snapshot", existing)
			doc.update(payload)
			doc.save(ignore_permissions=True)
			updated += 1
		else:
			doc = frappe.get_doc(
				{
					"doctype": "Metric Snapshot",
					"company": company,
					"metric_key": row["key"],
					"period_start": start,
					"period_end": end,
					**payload,
				}
			)
			doc.insert(ignore_permissions=True)
			created += 1

	return {
		"company": company,
		"period_start": str(start),
		"period_end": str(end),
		"created": created,
		"updated": updated,
		"metrics": len(rows),
	}


@frappe.whitelist()
def get_metric_catalog(keys=None) -> list[dict]:
	"""The governed catalog as plain dicts - definitions, not numbers."""
	if isinstance(keys, str):
		keys = frappe.parse_json(keys) if keys.strip().startswith("[") else [keys]
	return registry.catalog_as_dicts(keys)


def snapshot_all_companies(period_start=None, period_end=None) -> dict:
	"""Scheduler entry point: snapshot every enabled company for the period.

	Not whitelisted - intended for a monthly ``scheduler_events`` hook (see
	DESIGN.md; the hook is documented, not applied).
	"""
	start, end = _resolve_period(period_start, period_end)
	summary = {}
	for company in frappe.get_all("Company", pluck="name"):
		try:
			summary[company] = snapshot_kpis(company, start, end)
		except Exception:
			frappe.log_error(title=f"KPI snapshot failed for {company}", message=frappe.get_traceback())
			summary[company] = {"error": True}
	return summary
