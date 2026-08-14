# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Consolidated Statement with Eliminations.

Renders a stored Consolidation Run:

- **Company columns** are rebuilt from fresh GL balances translated at fresh
  rates (including each company's Currency Translation Adjustment plug), so
  the statement always reflects the books.
- **Eliminations** and **Minority Interest** columns come from the run's
  *stored* adjustment lines. The stored run keeps the adjustments
  deterministic — what was computed, reviewed and (optionally) finalized is
  exactly what is shown; if the books moved since the run was computed, the
  elimination columns may no longer tie to the fresh balances, which is the
  cue to recompute the run.
- Stored CTA lines are record-keeping only: the CTA shown in each company
  column is the freshly computed plug (using them both would double-count).
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt

from erpnext.accounts.consolidation import engine, loaders
from erpnext.accounts.consolidation.models import ROOT_TYPES, strip_company_abbr

ELIMINATIONS_FIELD = "eliminations"
MINORITY_INTEREST_FIELD = "minority_interest"
CONSOLIDATED_FIELD = "consolidated"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.get("consolidation_run"):
		return [], []

	run = frappe.get_doc("Consolidation Run", filters.consolidation_run)
	report_date = run.report_date or run.to_date

	edges = loaders.get_ownership_edges(run.parent_company, report_date)
	try:
		members = engine.resolve_group(run.parent_company, edges)
	except ValueError as e:
		frappe.throw(str(e), title=_("Invalid Ownership Structure"))

	companies = [run.parent_company] + [m.company for m in members.values() if m.method == engine.METHOD_FULL]
	abbrs = loaders.get_company_abbrs(companies)

	balances = loaders.get_balances(companies, run.from_date, run.to_date, report_date)
	rates, _rate_warnings = loaders.get_rates(
		companies, run.presentation_currency, report_date, run.from_date, run.to_date
	)
	translated, _cta = engine.translate(balances, rates)

	# company columns (fresh, translated, merged account keys)
	columns_by_company = {company: defaultdict(float) for company in companies}
	root_type_by_key = {}
	for bal in translated:
		key = strip_company_abbr(bal.account, abbrs)
		columns_by_company[bal.company][key] += bal.balance
		if bal.root_type:
			root_type_by_key.setdefault(key, bal.root_type)

	# adjustment columns (stored on the run — deterministic)
	eliminations = defaultdict(float)
	minority = defaultdict(float)
	for line in run.adjustment_lines:
		amount = flt(line.debit) - flt(line.credit)  # back to signed, debit-positive
		key = strip_company_abbr(line.account or "", abbrs)
		if line.line_type == "Elimination":
			eliminations[key] += amount
			root_type_by_key.setdefault(key, _account_root_type(line.account))
		elif line.line_type == "Minority Interest":
			minority[key] += amount
			root_type_by_key.setdefault(key, "Equity")
		# CTA lines: record-keeping only, already in the fresh company columns

	data = build_rows(companies, columns_by_company, eliminations, minority, root_type_by_key, run)
	columns = get_columns(companies, run)
	chart = get_chart(data, run)
	message = _build_message(run)

	return columns, data, message, chart


def _account_root_type(account):
	if account and frappe.db.exists("Account", account):
		return frappe.get_cached_value("Account", account, "root_type")
	return "Equity"


def build_rows(companies, columns_by_company, eliminations, minority, root_type_by_key, run):
	keys_by_root = defaultdict(set)
	for key, root in root_type_by_key.items():
		keys_by_root[root if root in ROOT_TYPES else "Equity"].add(key)

	currency = run.presentation_currency
	data = []

	for root_type in ROOT_TYPES:
		keys = sorted(keys_by_root.get(root_type, ()))
		if not keys:
			continue

		data.append(
			{
				"account": _(root_type),
				"is_section_header": 1,
				"currency": currency,
			}
		)

		totals = defaultdict(float)
		for key in keys:
			row = {"account": key, "indent": 1, "currency": currency}
			consolidated = 0.0
			has_value = False

			for company in companies:
				amount = flt(columns_by_company.get(company, {}).get(key, 0.0), 2)
				row[company] = amount
				consolidated += amount
				totals[company] += amount
				if amount:
					has_value = True

			for fieldname, source in (
				(ELIMINATIONS_FIELD, eliminations),
				(MINORITY_INTEREST_FIELD, minority),
			):
				amount = flt(source.get(key, 0.0), 2)
				row[fieldname] = amount
				consolidated += amount
				totals[fieldname] += amount
				if amount:
					has_value = True

			row[CONSOLIDATED_FIELD] = flt(consolidated, 2)
			totals[CONSOLIDATED_FIELD] += flt(consolidated, 2)

			if has_value:
				data.append(row)

		total_row = {
			"account": _("Total {0}").format(_(root_type)),
			"is_total_row": 1,
			"currency": currency,
		}
		for fieldname in [*companies, ELIMINATIONS_FIELD, MINORITY_INTEREST_FIELD, CONSOLIDATED_FIELD]:
			total_row[fieldname] = flt(totals[fieldname], 2)
		data.append(total_row)
		data.append({})

	return data


def get_columns(companies, run):
	columns = [
		{
			"fieldname": "account",
			"label": _("Account"),
			"fieldtype": "Data",
			"width": 300,
		},
		{
			"fieldname": "currency",
			"label": _("Currency"),
			"fieldtype": "Link",
			"options": "Currency",
			"hidden": 1,
		},
	]

	for company in companies:
		columns.append(
			{
				"fieldname": company,
				"label": f"{company} ({run.presentation_currency})",
				"fieldtype": "Currency",
				"options": "currency",
				"width": 150,
			}
		)

	columns.extend(
		[
			{
				"fieldname": ELIMINATIONS_FIELD,
				"label": _("Eliminations"),
				"fieldtype": "Currency",
				"options": "currency",
				"width": 140,
			},
			{
				"fieldname": MINORITY_INTEREST_FIELD,
				"label": _("Minority Interest"),
				"fieldtype": "Currency",
				"options": "currency",
				"width": 140,
			},
			{
				"fieldname": CONSOLIDATED_FIELD,
				"label": _("Consolidated ({0})").format(run.presentation_currency),
				"fieldtype": "Currency",
				"options": "currency",
				"width": 160,
			},
		]
	)

	return columns


def get_chart(data, run):
	labels = []
	values = []
	for row in data:
		if row.get("is_total_row"):
			labels.append(row["account"])
			values.append(abs(flt(row.get(CONSOLIDATED_FIELD))))

	if not labels:
		return None

	return {
		"data": {
			"labels": labels,
			"datasets": [
				{"name": _("Consolidated ({0})").format(run.presentation_currency), "values": values}
			],
		},
		"type": "bar",
		"fieldtype": "Currency",
		"options": "currency",
		"currency": run.presentation_currency,
	}


def _build_message(run):
	parts = []
	if run.status == "Computed":
		parts.append(
			_("Adjustments from Consolidation Run {0} (Computed, not yet finalized).").format(run.name)
		)
	elif run.status == "Finalized":
		parts.append(_("Adjustments from finalized Consolidation Run {0}.").format(run.name))
	if run.exceptions:
		parts.append(_("The run has exceptions/warnings — open {0} for details.").format(run.name))
	parts.append(
		_(
			"Company columns are fresh translated balances; elimination and minority-interest columns are the run's stored adjustments."
		)
	)
	return " ".join(parts)
