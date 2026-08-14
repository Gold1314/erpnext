# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure domain model for the bank reconciliation copilot.

No frappe imports here - these dataclasses are plain Python so the ranking
engine can be unit-tested without a site (see ``erpnext/accounts/forecasting``
and ``erpnext/manufacturing/scheduling`` for the pattern this module follows).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

#: component keys, in the order they are reported in ScoredSuggestion.features
AMOUNT = "amount_score"
DATE = "date_score"
REFERENCE = "reference_score"
PARTY = "party_score"
HISTORY = "history_score"

COMPONENTS = (AMOUNT, DATE, REFERENCE, PARTY, HISTORY)

#: default component weights (must sum to 1.0)
DEFAULT_WEIGHTS: dict[str, float] = {
	AMOUNT: 0.35,
	REFERENCE: 0.25,
	PARTY: 0.20,
	DATE: 0.10,
	HISTORY: 0.10,
}


@dataclass(frozen=True)
class TxnFeatures:
	"""The bank-statement side of a match.

	``amount`` is the transaction's *unallocated* amount (always positive -
	direction is resolved by the loader, mirroring how the reconciliation
	tool's ``check_matching`` uses ``transaction.unallocated_amount``).
	"""

	amount: float
	date: datetime.date | None
	description: str | None = None
	reference_number: str | None = None
	#: the party already stamped on the transaction (rule engine / user), or
	#: the statement's counterparty name (``bank_party_name``) as a fallback
	party_hint: str | None = None


@dataclass(frozen=True)
class CandidateVoucher:
	"""One unreconciled-side voucher that could clear the transaction."""

	voucher_type: str
	voucher_name: str
	#: the amount the voucher can still clear against a bank transaction
	#: (paid amount minus existing bank-transaction allocations)
	amount: float
	date: datetime.date | None
	party: str | None = None
	party_name: str | None = None
	reference_no: str | None = None
	bill_no: str | None = None
	currency: str | None = None
	#: invoice outstanding amount, when the voucher is an invoice - enables
	#: the partial-payment carve-out in the amount score
	outstanding: float | None = None


@dataclass
class MatchHistory:
	"""Aggregated evidence from previously **Reconciled** bank transactions.

	Built by ``loaders.get_match_history`` from the ``payment_entries`` child
	table (Bank Transaction Payments) of reconciled transactions.
	"""

	#: (party, voucher_type) -> number of prior reconciliations
	party_voucher_counts: dict[tuple[str, str], int] = field(default_factory=dict)
	#: normalized description prefix -> (party, count) - the dominant party
	#: historically matched for statements that start with this text
	description_party: dict[str, tuple[str, int]] = field(default_factory=dict)


@dataclass
class ScoredSuggestion:
	candidate: CandidateVoucher
	#: weighted total, 0-100
	score: float
	#: component scores (keys = COMPONENTS), each 0-100
	features: dict[str, float]
	#: human-readable summary of the top contributing components
	explanation: str
	#: conservative flag for the UI - never triggers a write by itself
	auto_match_eligible: bool
