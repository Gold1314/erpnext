# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure domain model for multi-GAAP finance-book adjustments.

No frappe imports here - these dataclasses are plain Python so the engine can
be unit-tested without a site (see ``erpnext/accounts/forecasting`` and
``erpnext/accounts/revenue`` for the pattern this module follows).

The safe v1 design (per GAP_CLOSURE_BLUEPRINT.md W1 1d risk note): the GL map
is NOT intercepted. Policy rules generate one book-tagged adjustment Journal
Entry per period, so book-filtered reports - whose semantics are
``finance_book IN (B, '') OR finance_book IS NULL`` (see DESIGN.md for the
quoted report code) - show policy-adjusted numbers on top of the common
(untagged) entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Rule types (must match the ``rule_type`` Select options of the
# ``Accounting Policy Rule`` doctype).
RECLASSIFY = "Reclassify"
EXCLUDE = "Exclude"
MANUAL_AMOUNT = "Manual Amount"
RULE_TYPES = (RECLASSIFY, EXCLUDE, MANUAL_AMOUNT)


@dataclass
class AccountMovement:
	"""Net movement of one account over the adjustment period.

	``net_movement`` is ``SUM(debit) - SUM(credit)`` in company currency:
	**positive = net debit**, negative = net credit.
	"""

	account: str
	net_movement: float


@dataclass
class PolicyRule:
	"""Pure mirror of one enabled ``Accounting Policy Rule`` row.

	- :data:`RECLASSIFY`: move ``percentage``% of ``source_account``'s period
	  movement to ``target_account`` inside the book.
	- :data:`EXCLUDE`: negate ``percentage``% of ``source_account``'s period
	  movement inside the book, offsetting to ``target_account`` (typically a
	  "GAAP adjustment" equity account so the entry balances).
	- :data:`MANUAL_AMOUNT`: fixed ``manual_amount``, debiting
	  ``manual_debit_account`` and crediting ``manual_credit_account``.
	"""

	name: str
	rule_type: str
	source_account: str | None = None
	target_account: str | None = None
	percentage: float = 100.0
	manual_amount: float = 0.0
	manual_debit_account: str | None = None
	manual_credit_account: str | None = None
	description: str = ""


@dataclass
class AdjustmentLine:
	"""One computed Journal Entry line (company currency, 2dp).

	Exactly one of ``debit`` / ``credit`` is non-zero. ``source_rules`` lists
	the names of every :class:`PolicyRule` that contributed to this line -
	consolidation sums duplicate accounts across rules, so a line can carry
	more than one rule.
	"""

	account: str
	debit: float = 0.0
	credit: float = 0.0
	source_rules: tuple[str, ...] = field(default_factory=tuple)
