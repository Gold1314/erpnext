# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure data model for the consolidation engine.

No frappe imports here — these types are shared between the pure engine
(``engine.py``) and the Frappe adapters (``loaders.py``, the Consolidation
Run controller and the report).

Sign convention
---------------
Every balance is **signed, debit-positive**: assets and expenses are
normally positive, liabilities / equity / income normally negative. A
company's full trial balance therefore sums to zero. All engine math and
the invariants tested in ``test_consolidation.py`` rely on this single
convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BALANCE_SHEET_ROOT_TYPES = ("Asset", "Liability", "Equity")
PROFIT_AND_LOSS_ROOT_TYPES = ("Income", "Expense")
ROOT_TYPES = BALANCE_SHEET_ROOT_TYPES + PROFIT_AND_LOSS_ROOT_TYPES

METHOD_FULL = "Full"
METHOD_EQUITY = "Equity"

# Synthetic account keys emitted by the engine. They carry no company
# abbreviation suffix, so account-key merging leaves them untouched.
CTA_ACCOUNT = "Currency Translation Adjustment"
MINORITY_INTEREST_ACCOUNT = "Minority Interest"
MI_RECLASS_ACCOUNT = "Equity Attributable to Group (NCI Reclass)"
UNCLOSED_PRIOR_PERIODS_ACCOUNT = "Unclosed Prior Periods Profit / Loss"


@dataclass(frozen=True)
class CompanyBalance:
	"""One account balance of one company (signed, debit-positive)."""

	company: str
	account: str
	root_type: str
	balance: float


@dataclass(frozen=True)
class OwnershipEdge:
	"""A direct ownership stake: ``parent`` owns ``percent`` % of ``subsidiary``."""

	parent: str
	subsidiary: str
	percent: float  # 0 < percent <= 100
	method: str = METHOD_FULL  # METHOD_FULL or METHOD_EQUITY


@dataclass(frozen=True)
class GroupMember:
	"""A subsidiary resolved into the consolidation group.

	``percent`` is the *effective* group share (ownership multiplied down
	the chain and summed across parallel ownership paths, capped at 100).
	"""

	company: str
	percent: float
	method: str


@dataclass(frozen=True)
class EliminationPair:
	"""Two accounts (in two companies) whose balances offset each other."""

	company_a: str
	account_a: str
	company_b: str
	account_b: str
	rule: str = ""


@dataclass(frozen=True)
class TranslationRates:
	"""Per-company translation rates into the presentation currency."""

	closing_rate: float = 1.0
	average_rate: float = 1.0


@dataclass(frozen=True)
class EliminationLine:
	"""A single-sided elimination adjustment (signed, debit-positive)."""

	company: str
	account: str
	amount: float
	rule: str = ""


@dataclass
class ConsolidationResult:
	"""Everything the adapters need to persist / render a consolidation."""

	parent: str
	companies: list[str] = field(default_factory=list)
	# company -> merged account key -> translated amount (pre-elimination)
	columns: dict[str, dict[str, float]] = field(default_factory=dict)
	elimination_lines: list[EliminationLine] = field(default_factory=list)
	exceptions: list[str] = field(default_factory=list)
	warnings: list[str] = field(default_factory=list)
	# company -> CTA plug (signed, debit-positive; part of that company's column)
	cta_by_company: dict[str, float] = field(default_factory=dict)
	# company -> minority interest, credit-positive
	minority_interest_by_company: dict[str, float] = field(default_factory=dict)
	minority_interest_total: float = 0.0
	# merged account key -> consolidated amount (after eliminations + MI split)
	consolidated: dict[str, float] = field(default_factory=dict)
	# merged account key -> root type, for grouping in reports
	root_type_by_key: dict[str, str] = field(default_factory=dict)


def strip_company_abbr(account: str, abbrs) -> str:
	"""Strip a trailing ``" - ABBR"`` company suffix from an account name.

	ERPNext account names are ``"<account_name> - <company abbr>"``; the same
	economic account in two companies differs only by that suffix, so merging
	strips it. Longest abbreviation wins, so an abbr that is a suffix of
	another (e.g. ``"C"`` and ``"ABC"``) cannot shadow it.
	"""
	for abbr in sorted({a for a in abbrs if a}, key=len, reverse=True):
		suffix = f" - {abbr}"
		if account.endswith(suffix):
			return account[: -len(suffix)]
	return account
