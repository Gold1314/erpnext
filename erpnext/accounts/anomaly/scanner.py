# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Orchestrates the pure anomaly checks over live data and persists findings.

Follows the settings-gated scheduled-check pattern of
``erpnext.accounts.utils.run_ledger_health_checks`` (erpnext/accounts/utils.py,
wired as a ``daily`` job in erpnext/hooks.py): read the monitor settings
single, bail out unless enabled, then run each enabled check per company and
save one log row per finding. On top of that pattern, per-company errors are
swallowed and logged so one company's bad data cannot starve the others.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import cint, flt, now_datetime

from erpnext.accounts.anomaly import engine, loaders
from erpnext.accounts.anomaly.models import (
	CHECK_ACCOUNT_OUTLIER,
	CHECK_BENFORD_DEVIATION,
	CHECK_DUPLICATE_INVOICE,
	CHECK_RARE_COMBINATION,
	CHECK_SUSPICIOUS_POSTING,
	Finding,
)

#: Baseline posting-date lookback for the duplicate-invoice check. The effective
#: lookback is widened to the configured pairing window when that is larger.
DUPLICATE_LOOKBACK_DAYS = 90


def run_scheduled_scan():
	"""Daily scheduler entry point (recommended hooks.py wiring:
	``erpnext.accounts.anomaly.scanner.run_scheduled_scan`` in the ``daily``
	scheduler_events list, next to
	``erpnext.accounts.utils.run_ledger_health_checks``)."""
	settings = frappe.get_single("Anomaly Detection Settings")
	if not settings.enabled:
		return

	for company in frappe.get_all("Company", pluck="name"):
		try:
			run_anomaly_scan(company)
		except Exception:
			frappe.log_error(
				title=f"Anomaly scan failed for {company}",
				message=frappe.get_traceback(),
			)


@frappe.whitelist()
def run_scan_now(company: str | None = None):
	"""Whitelisted wrapper so the scan can be triggered from the UI."""
	frappe.only_for(("System Manager", "Accounts Manager"))
	return run_anomaly_scan(company)


def run_anomaly_scan(company: str | None = None) -> dict:
	"""Run all enabled checks for ``company`` (or every company when None),
	dedupe against known fingerprints and insert Anomaly Finding docs.

	Returns per-check new-finding counts, e.g.
	``{"duplicate_invoice": 2, ..., "total": 5, "companies": ["_TC"]}``.
	"""
	settings = frappe.get_single("Anomaly Detection Settings")
	counts = dict.fromkeys(
		(
			CHECK_DUPLICATE_INVOICE,
			CHECK_ACCOUNT_OUTLIER,
			CHECK_RARE_COMBINATION,
			CHECK_SUSPICIOUS_POSTING,
			CHECK_BENFORD_DEVIATION,
		),
		0,
	)
	result = {**counts, "total": 0, "companies": []}

	if not settings.enabled:
		return result

	companies = [company] if company else frappe.get_all("Company", pluck="name")

	for name in companies:
		findings = collect_findings(name, settings)
		findings = engine.dedupe_against_known(findings, loaders.get_known_fingerprints(name))

		detected_on = now_datetime()
		for finding in findings:
			insert_finding(name, finding, detected_on)
			result[finding.check_key] += 1
			result["total"] += 1

		result["companies"].append(name)

	return result


def collect_findings(company: str, settings) -> list[Finding]:
	"""Run every enabled check for one company with the settings' thresholds."""
	findings: list[Finding] = []

	if settings.enable_duplicate_invoices:
		duplicate_days_window = cint(settings.duplicate_days_window) or 45
		# The posting lookback must cover the pairing window, otherwise a duplicate
		# re-entered today can never be paired with an original that has already
		# aged out of the lookback.
		findings += engine.find_duplicate_invoices(
			loaders.get_invoices(company, days=max(DUPLICATE_LOOKBACK_DAYS, duplicate_days_window)),
			days_window=duplicate_days_window,
		)

	if settings.enable_account_outliers:
		findings += engine.find_account_outliers(
			loaders.get_gl_movements(company),
			z_threshold=flt(settings.z_threshold) or 3.0,
		)

	if settings.enable_rare_combinations:
		combo_counts, total_by_account = loaders.get_combo_counts(company)
		findings += engine.find_rare_combinations(
			combo_counts,
			total_by_account,
			rarity_pct=flt(settings.rarity_pct) or 1.0,
		)

	if settings.enable_suspicious_postings:
		findings += engine.find_suspicious_postings(
			loaders.get_postings(company),
			round_amount_threshold=flt(settings.round_amount_threshold) or 10000,
			backdate_days_threshold=cint(settings.backdate_days_threshold) or 14,
		)

	if settings.enable_benford:
		min_n = cint(settings.benford_min_n) or 300
		for voucher_type in loaders.get_benford_voucher_types(company):
			findings += engine.find_benford_deviation(
				loaders.get_amounts_for_benford(company, voucher_type=voucher_type),
				min_n=min_n,
				scope=voucher_type,
			)

	return findings


def insert_finding(company: str, finding: Finding, detected_on) -> None:
	doc = frappe.get_doc(
		{
			"doctype": "Anomaly Finding",
			"company": company,
			"check_key": finding.check_key,
			"severity": finding.severity,
			"entity_type": finding.entity_type,
			"entity_name": finding.entity_name,
			"message": finding.message,
			"score": flt(finding.score),
			"details": json.dumps(finding.details, indent=2, default=str),
			"fingerprint": engine.compute_fingerprint(finding),
			"detected_on": detected_on,
			"status": "Open",
		}
	)

	# link back to the source record when the entity is a real document
	if frappe.db.exists("DocType", finding.entity_type) and frappe.db.exists(
		finding.entity_type, finding.entity_name
	):
		doc.reference_doctype = finding.entity_type
		doc.reference_name = finding.entity_name

	doc.insert(ignore_permissions=True)
