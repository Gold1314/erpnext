# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt

from erpnext.buying.sourcing.models import SCENARIO_NAMES


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.get("sourcing_event"):
		frappe.throw(_("Select a Sourcing Event."))

	event = frappe.get_doc("Sourcing Event", filters.sourcing_event)
	event.check_permission("read")

	# ``compute_award_analysis`` -> ``get_bids_for_comparison`` throws while
	# the event is sealed and still open, so the sealed rule is enforced in
	# exactly one place.
	analysis = event.compute_award_analysis()

	data = get_data(analysis, filters)
	return get_columns(), data, get_message(analysis), get_chart(analysis)


def get_columns():
	return [
		{
			"fieldname": "item_code",
			"label": _("Item"),
			"fieldtype": "Link",
			"options": "Item",
			"width": 160,
		},
		{
			"fieldname": "supplier",
			"label": _("Supplier"),
			"fieldtype": "Link",
			"options": "Supplier",
			"width": 160,
		},
		{
			"fieldname": "bid",
			"label": _("Bid"),
			"fieldtype": "Link",
			"options": "Sourcing Event Bid",
			"width": 110,
		},
		{"fieldname": "qty", "label": _("Qty"), "fieldtype": "Float", "width": 90},
		{
			"fieldname": "unit_price",
			"label": _("Unit Price"),
			"fieldtype": "Currency",
			"options": "currency",
			"width": 110,
		},
		{
			"fieldname": "amount",
			"label": _("Amount"),
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
		{"fieldname": "lead_time_days", "label": _("Lead Time"), "fieldtype": "Int", "width": 90},
		{"fieldname": "weighted_score", "label": _("Weighted Score"), "fieldtype": "Float", "width": 120},
		{"fieldname": "rank", "label": _("Rank"), "fieldtype": "Int", "width": 70},
		{"fieldname": "scenarios", "label": _("Winning In"), "fieldtype": "Data", "width": 220},
		{
			"fieldname": "currency",
			"label": _("Currency"),
			"fieldtype": "Link",
			"options": "Currency",
			"width": 90,
			"hidden": 1,
		},
	]


def get_data(analysis, filters):
	scores = {row["supplier"]: row for row in analysis["scores"]}
	winners = get_scenario_winners(analysis)
	data = []

	for line in analysis["lines"]:
		supplier = line["supplier"]
		if filters.get("supplier") and supplier != filters.supplier:
			continue

		score = scores.get(supplier, {})
		won_in = winners.get((line["item_code"], supplier), [])
		data.append(
			{
				"item_code": line["item_code"],
				"supplier": supplier,
				"bid": line["bid"],
				"qty": flt(line["qty"]),
				"unit_price": flt(line["unit_price"]),
				"amount": flt(line["amount"]),
				"lead_time_days": line["lead_time_days"],
				"weighted_score": score.get("weighted_total", 0.0),
				"rank": score.get("rank", 0),
				"scenarios": ", ".join(_(name) for name in won_in),
				"currency": analysis.get("currency"),
			}
		)

	data.sort(key=lambda row: (row["item_code"], row["rank"] or 999, row["supplier"]))
	return data


def get_scenario_winners(analysis):
	winners = {}
	for scenario in analysis["scenarios"]:
		for award in scenario["awards"]:
			winners.setdefault((award["item_code"], award["supplier"]), []).append(scenario["name"])
	return winners


def get_message(analysis):
	"""Scenario summary rendered above the rows."""
	currency = analysis.get("currency") or ""
	by_name = {scenario["name"]: scenario for scenario in analysis["scenarios"]}

	rows = []
	for name in SCENARIO_NAMES:
		scenario = by_name.get(name)
		if not scenario:
			continue
		gaps = ", ".join(scenario["coverage_gaps"]) or "-"
		suppliers = sorted({award["supplier"] for award in scenario["awards"]})
		rows.append(
			"<tr><td>{name}</td><td class='text-right'>{cost}</td><td>{lines}</td>"
			"<td>{suppliers}</td><td>{gaps}</td></tr>".format(
				name=_(name),
				cost=frappe.utils.fmt_money(scenario["total_cost"], currency=currency),
				lines=len(scenario["awards"]),
				suppliers=", ".join(suppliers) or "-",
				gaps=gaps,
			)
		)

	excluded = analysis.get("excluded_suppliers") or []
	excluded_html = (
		"<p class='text-danger'>{}</p>".format(
			_("Excluded for missing qualification: {0}").format(", ".join(excluded))
		)
		if excluded
		else ""
	)

	weights = analysis.get("weights") or {}
	weights_html = _("Weights - price {0}, lead time {1}, scorecard {2}, qualification {3}").format(
		flt(weights.get("price")),
		flt(weights.get("lead_time")),
		flt(weights.get("scorecard")),
		flt(weights.get("qualification")),
	)

	return """{excluded}<p class="text-muted">{weights}</p>
		<table class="table table-bordered"><thead><tr>
		<th>{scenario}</th><th class="text-right">{total}</th><th>{lines}</th>
		<th>{suppliers}</th><th>{gaps}</th></tr></thead>
		<tbody>{rows}</tbody></table>""".format(
		excluded=excluded_html,
		weights=weights_html,
		scenario=_("Scenario"),
		total=_("Total Cost"),
		lines=_("Lines Awarded"),
		suppliers=_("Suppliers"),
		gaps=_("Coverage Gaps"),
		rows="".join(rows),
	)


def get_chart(analysis):
	by_name = {scenario["name"]: scenario for scenario in analysis["scenarios"]}
	names = [name for name in SCENARIO_NAMES if name in by_name]

	return {
		"data": {
			"labels": [_(name) for name in names],
			"datasets": [
				{
					"name": _("Total Cost"),
					"values": [flt(by_name[name]["total_cost"]) for name in names],
				}
			],
		},
		"type": "bar",
		"colors": ["#5e64ff"],
	}
