# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure domain model for the accounting anomaly-detection engine.

No frappe imports here - these dataclasses are plain Python so the engine can
be unit-tested without a site (see ``erpnext/accounts/forecasting`` and
``erpnext/manufacturing/scheduling`` for the pattern this module follows).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

# Severities
HIGH = "High"
MEDIUM = "Medium"
LOW = "Low"
SEVERITIES = (HIGH, MEDIUM, LOW)

# Check keys (stored on Anomaly Finding.check_key)
CHECK_DUPLICATE_INVOICE = "duplicate_invoice"
CHECK_ACCOUNT_OUTLIER = "account_outlier"
CHECK_RARE_COMBINATION = "rare_combination"
CHECK_SUSPICIOUS_POSTING = "suspicious_posting"
CHECK_BENFORD_DEVIATION = "benford_deviation"

CHECK_KEYS = (
	CHECK_DUPLICATE_INVOICE,
	CHECK_ACCOUNT_OUTLIER,
	CHECK_RARE_COMBINATION,
	CHECK_SUSPICIOUS_POSTING,
	CHECK_BENFORD_DEVIATION,
)


@dataclass
class InvoiceRecord:
	"""One submitted supplier invoice, as needed for duplicate screening."""

	name: str
	supplier: str
	bill_no: str | None
	bill_date: datetime.date | None
	amount: float
	posting_date: datetime.date

	@property
	def effective_date(self) -> datetime.date:
		"""The supplier's bill date when captured, else our posting date."""
		return self.bill_date or self.posting_date


@dataclass
class GLMovement:
	"""Aggregated GL movement of one account in one calendar month.

	``period`` is ``YYYY-MM`` so plain string sorting is chronological.
	"""

	account: str
	period: str
	total_debit: float = 0.0
	total_credit: float = 0.0
	entry_count: int = 0

	@property
	def net(self) -> float:
		return self.total_debit - self.total_credit


@dataclass
class PostingRecord:
	"""One posting (voucher-level) screened for suspicious traits.

	``weekday`` follows :meth:`datetime.date.weekday` - Monday=0 .. Sunday=6.
	``is_backdated_days`` is how many days the posting date lies before the
	record's creation date (0 when not backdated); the loader derives it so
	the engine never touches ambient time.
	"""

	voucher_type: str
	voucher_no: str
	account: str | None
	amount: float
	posting_date: datetime.date
	weekday: int
	created_by: str | None = None
	is_backdated_days: int = 0


@dataclass
class Finding:
	"""One anomaly raised by a check.

	``score`` is check-specific but always "higher = more anomalous";
	``details`` must stay JSON-serializable (strings/numbers/lists/dicts).
	"""

	check_key: str
	severity: str
	entity_type: str
	entity_name: str
	message: str
	score: float
	details: dict = field(default_factory=dict)
