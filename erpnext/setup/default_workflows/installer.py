# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Installer for the shipped approval-workflow content pack.

Creates Frappe ``Workflow`` records (plus the ``Workflow State`` and
``Workflow Action Master`` masters they reference) from the plain-dict
definitions in ``definitions.py``. Idempotent: a workflow whose
``workflow_name`` already exists on the site is skipped.

Usage (from a bench):

    bench --site <site> execute erpnext.setup.default_workflows.installer.install_all
    bench --site <site> execute erpnext.setup.default_workflows.installer.install_workflow \
        --kwargs '{"slug": "purchase-order-approval", "approval_threshold": 50000}'
"""

import copy

import frappe
from frappe import _
from frappe.utils import flt, sbool

from erpnext.setup.default_workflows.definitions import (
	DEFAULT_APPROVAL_THRESHOLD,
	THRESHOLD_PLACEHOLDER,
	WORKFLOW_ACTIONS,
	WORKFLOW_DEFINITIONS,
	WORKFLOW_STATES,
)


@frappe.whitelist()
def get_available_workflows():
	"""Return the shipped workflow slugs with metadata and install status."""
	out = []
	for slug, definition in WORKFLOW_DEFINITIONS.items():
		out.append(
			{
				"slug": slug,
				"name": definition["workflow_name"],
				"document_type": definition["document_type"],
				"description": definition.get("description", ""),
				"installed": bool(frappe.db.exists("Workflow", definition["workflow_name"])),
			}
		)
	return out


@frappe.whitelist()
def install_workflow(slug, approval_threshold=None, activate=True):
	"""Install one shipped workflow by slug.

	:param slug: key in ``WORKFLOW_DEFINITIONS``
	:param approval_threshold: overrides ``DEFAULT_APPROVAL_THRESHOLD`` in
	        condition strings that carry the ``{approval_threshold}`` placeholder
	:param activate: pass False (or 0) to install with ``is_active = 0`` so
	        the workflow can be staged and switched on later
	:returns: dict with the outcome (``created`` / ``skipped``)
	"""
	frappe.only_for("System Manager")

	definition = _get_definition(slug)
	workflow_name = definition["workflow_name"]

	if frappe.db.exists("Workflow", workflow_name):
		return {
			"slug": slug,
			"workflow": workflow_name,
			"status": "skipped",
			"message": _("Workflow {0} already exists").format(workflow_name),
		}

	_create_masters(definition)

	threshold = flt(approval_threshold) if approval_threshold is not None else DEFAULT_APPROVAL_THRESHOLD

	doc = frappe.get_doc(_build_workflow_dict(definition, threshold, sbool(activate)))
	doc.insert(ignore_permissions=True)

	return {"slug": slug, "workflow": workflow_name, "status": "created"}


@frappe.whitelist()
def install_all(approval_threshold=None, activate=True):
	"""Install every shipped workflow. Existing workflows are skipped."""
	frappe.only_for("System Manager")

	return [
		install_workflow(slug, approval_threshold=approval_threshold, activate=activate)
		for slug in WORKFLOW_DEFINITIONS
	]


@frappe.whitelist()
def uninstall_workflow(slug):
	"""Deactivate a shipped workflow (sets ``is_active = 0``).

	The Workflow record is deliberately NOT deleted: documents on the site
	may already carry its states in their ``workflow_state`` field.
	"""
	frappe.only_for("System Manager")

	definition = _get_definition(slug)
	workflow_name = definition["workflow_name"]

	if not frappe.db.exists("Workflow", workflow_name):
		return {
			"slug": slug,
			"workflow": workflow_name,
			"status": "skipped",
			"message": _("Workflow {0} is not installed").format(workflow_name),
		}

	workflow = frappe.get_doc("Workflow", workflow_name)
	workflow.is_active = 0
	workflow.save(ignore_permissions=True)

	return {"slug": slug, "workflow": workflow_name, "status": "deactivated"}


def _get_definition(slug):
	definition = WORKFLOW_DEFINITIONS.get(slug)
	if not definition:
		frappe.throw(
			_("Unknown workflow slug {0}. Available: {1}").format(
				slug, ", ".join(WORKFLOW_DEFINITIONS)
			)
		)
	return definition


def _create_masters(definition):
	"""Create the Workflow State / Workflow Action Master records the
	definition references, guarded by ``frappe.db.exists``."""
	styles = {row["workflow_state_name"]: row.get("style") or "" for row in WORKFLOW_STATES}

	for row in definition["states"]:
		state = row["state"]
		if not frappe.db.exists("Workflow State", state):
			frappe.get_doc(
				{
					"doctype": "Workflow State",
					"workflow_state_name": state,
					"style": styles.get(state, ""),
				}
			).insert(ignore_permissions=True)

	shipped_actions = {row["workflow_action_name"] for row in WORKFLOW_ACTIONS}
	referenced_actions = {row["action"] for row in definition["transitions"]}
	for action in sorted(shipped_actions | referenced_actions):
		if not frappe.db.exists("Workflow Action Master", action):
			frappe.get_doc(
				{"doctype": "Workflow Action Master", "workflow_action_name": action}
			).insert(ignore_permissions=True)


def _build_workflow_dict(definition, threshold, activate):
	"""Turn a plain definition dict into a Workflow doc dict, substituting
	the approval threshold into condition templates."""
	definition = copy.deepcopy(definition)

	workflow = {
		"doctype": "Workflow",
		"workflow_name": definition["workflow_name"],
		"document_type": definition["document_type"],
		"workflow_state_field": definition.get("workflow_state_field", "workflow_state"),
		"is_active": 1 if activate else 0,
		"send_email_alert": definition.get("send_email_alert", 0),
		"states": [],
		"transitions": [],
	}

	for row in definition["states"]:
		state = {
			"state": row["state"],
			"doc_status": row["doc_status"],
			"allow_edit": row["allow_edit"],
		}
		if row.get("update_field"):
			state["update_field"] = row["update_field"]
			state["update_value"] = row.get("update_value")
		workflow["states"].append(state)

	for row in definition["transitions"]:
		transition = {
			"state": row["state"],
			"action": row["action"],
			"next_state": row["next_state"],
			"allowed": row["allowed"],
			"allow_self_approval": row.get("allow_self_approval", 0),
		}
		condition = row.get("condition")
		if condition:
			if THRESHOLD_PLACEHOLDER in condition:
				# str(flt(...)) yields a plain python literal (no locale
				# separators), keeping the condition safe_eval-able.
				condition = condition.replace(THRESHOLD_PLACEHOLDER, str(flt(threshold)))
			transition["condition"] = condition
		workflow["transitions"].append(transition)

	return workflow
