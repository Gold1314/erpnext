# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Semantic metric layer for ERPNext (GAP_CLOSURE_BLUEPRINT.md W7).

A governed catalog of business metrics with one definition each, so "DSO"
means the same thing in a report, an API call and an agent answer.

Layout mirrors ``erpnext/accounts/forecasting``:

- ``models.py`` / ``engine.py`` / ``registry.py``: **pure** - no frappe
  imports, no ambient clock. Unit-tested with
  ``python erpnext/analytics/test_analytics.py``.
- ``loaders.py``: every DB read lives here; each loader returns a dict of the
  named inputs the registry declares.
- ``api.py``: whitelisted entry points (``get_kpis``, ``snapshot_kpis``,
  ``get_metric_catalog``).
"""
