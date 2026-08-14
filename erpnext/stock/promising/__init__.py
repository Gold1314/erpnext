# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""ATP (available-to-promise) order promising.

Pure-core promise engine (``models.py`` + ``engine.py``, no frappe imports)
behind thin Frappe adapters (``loaders.py`` for queries, ``api.py`` for
whitelisted endpoints). Blueprint reference: ``GAP_CLOSURE_BLUEPRINT.md`` W3
item 3b; architecture modeled on ``erpnext/manufacturing/scheduling/`` and
``erpnext/accounts/forecasting/``. See ``DESIGN.md`` for the exact ATP
semantics.
"""
