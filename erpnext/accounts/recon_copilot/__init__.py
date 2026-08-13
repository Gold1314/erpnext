# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Bank reconciliation copilot (GAP_CLOSURE_BLUEPRINT.md, W6).

A suggestion *ranking* engine that sits on top of the existing rule-based
candidate matching in
``erpnext/accounts/doctype/bank_reconciliation_tool/bank_reconciliation_tool.py``.

Layout (pure-core + adapter doctrine, see ``erpnext/accounts/forecasting``):

- ``models.py`` / ``engine.py``   pure Python, zero frappe, no ambient time
- ``loaders.py``                  frappe adapters (DB access only)
- ``api.py``                      whitelisted endpoints for the /banking SPA
- ``test_recon_copilot.py``       site-less unit tests for the pure core
"""
