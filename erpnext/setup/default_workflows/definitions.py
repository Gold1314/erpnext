# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Workflow record definitions for the shipped approval-workflow content pack.

Each entry in ``WORKFLOW_DEFINITIONS`` is a plain python dict that mirrors the
Frappe ``Workflow`` doctype schema (see ``frappe.workflow.doctype.workflow``;
the exact field structure is the one used by
``erpnext/selling/doctype/sales_order/test_sales_order.py::make_sales_order_workflow``):

- top level: ``workflow_name``, ``document_type``, ``workflow_state_field``,
  ``is_active``, ``send_email_alert``
- ``states`` rows (Workflow Document State): ``state``, ``doc_status``
  (string: "0" draft, "1" submitted, "2" cancelled), ``allow_edit`` (a Role
  name), optional ``update_field``/``update_value``
- ``transitions`` rows (Workflow Transition): ``state``, ``action``,
  ``next_state``, ``allowed`` (a Role name), ``allow_self_approval``,
  optional ``condition`` (python expression evaluated by ``frappe.safe_eval``
  with ``doc`` in context)

Conditions may contain the literal placeholder ``{approval_threshold}``;
the installer substitutes it with ``DEFAULT_APPROVAL_THRESHOLD`` (or an
override passed at install time) before the record is created. Definitions
in this module are pure data — no frappe import — so they can be validated
without a running site.
"""

# Default monetary threshold above which a Purchase Order submitted by a
# Purchase User is routed to "Pending Approval" instead of being approved
# directly. Interpolated into condition strings at install time; override
# via install_workflow(slug, approval_threshold=...) in installer.py.
DEFAULT_APPROVAL_THRESHOLD = 10000

# Placeholder token used inside condition strings.
THRESHOLD_PLACEHOLDER = "{approval_threshold}"

# Workflow State master records required by the definitions below.
# Schema: workflow_state_name, style (frappe.workflow.doctype.workflow_state).
WORKFLOW_STATES = [
	{"workflow_state_name": "Draft", "style": ""},
	{"workflow_state_name": "Pending Approval", "style": "Warning"},
	{"workflow_state_name": "Approved", "style": "Success"},
	{"workflow_state_name": "Rejected", "style": "Danger"},
]

# Workflow Action Master records required by the definitions below.
# Schema: workflow_action_name (frappe.workflow.doctype.workflow_action_master).
WORKFLOW_ACTIONS = [
	{"workflow_action_name": "Submit"},
	{"workflow_action_name": "Approve"},
	{"workflow_action_name": "Reject"},
	{"workflow_action_name": "Review"},
]


WORKFLOW_DEFINITIONS = {
	"purchase-order-approval": {
		"workflow_name": "Purchase Order Approval",
		"document_type": "Purchase Order",
		"description": (
			"Purchase Orders above the approval threshold require Purchase Manager "
			"approval; orders at or below the threshold, and any order submitted by "
			"a Purchase Manager, are approved directly."
		),
		"workflow_state_field": "workflow_state",
		"is_active": 1,
		"send_email_alert": 0,
		"states": [
			{"state": "Draft", "doc_status": "0", "allow_edit": "Purchase User"},
			# Edit locked for the requester while awaiting approval.
			{"state": "Pending Approval", "doc_status": "0", "allow_edit": "Purchase Manager"},
			{"state": "Approved", "doc_status": "1", "allow_edit": "Purchase Manager"},
			{"state": "Rejected", "doc_status": "0", "allow_edit": "Purchase User"},
		],
		"transitions": [
			# Threshold branch: above the threshold the Purchase User's submit
			# routes to Pending Approval ...
			{
				"state": "Draft",
				"action": "Submit",
				"next_state": "Pending Approval",
				"allowed": "Purchase User",
				"allow_self_approval": 1,
				"condition": "doc.grand_total > {approval_threshold}",
			},
			# ... at or below the threshold it is approved directly.
			{
				"state": "Draft",
				"action": "Submit",
				"next_state": "Approved",
				"allowed": "Purchase User",
				"allow_self_approval": 1,
				"condition": "doc.grand_total <= {approval_threshold}",
			},
			# Purchase Managers may approve directly regardless of amount.
			{
				"state": "Draft",
				"action": "Submit",
				"next_state": "Approved",
				"allowed": "Purchase Manager",
				"allow_self_approval": 1,
			},
			{
				"state": "Pending Approval",
				"action": "Approve",
				"next_state": "Approved",
				"allowed": "Purchase Manager",
				"allow_self_approval": 0,
			},
			{
				"state": "Pending Approval",
				"action": "Reject",
				"next_state": "Rejected",
				"allowed": "Purchase Manager",
				"allow_self_approval": 0,
			},
			{
				"state": "Rejected",
				"action": "Review",
				"next_state": "Draft",
				"allowed": "Purchase User",
				"allow_self_approval": 1,
			},
		],
	},
	"journal-entry-approval": {
		"workflow_name": "Journal Entry Approval",
		"document_type": "Journal Entry",
		"description": (
			"Every Journal Entry requires Accounts Manager approval before it is "
			"submitted (four-eyes: the entry's author cannot approve it)."
		),
		"workflow_state_field": "workflow_state",
		"is_active": 1,
		"send_email_alert": 0,
		"states": [
			{"state": "Draft", "doc_status": "0", "allow_edit": "Accounts User"},
			{"state": "Pending Approval", "doc_status": "0", "allow_edit": "Accounts Manager"},
			{"state": "Approved", "doc_status": "1", "allow_edit": "Accounts Manager"},
			{"state": "Rejected", "doc_status": "0", "allow_edit": "Accounts User"},
		],
		"transitions": [
			{
				"state": "Draft",
				"action": "Submit",
				"next_state": "Pending Approval",
				"allowed": "Accounts User",
				"allow_self_approval": 1,
			},
			{
				"state": "Pending Approval",
				"action": "Approve",
				"next_state": "Approved",
				"allowed": "Accounts Manager",
				"allow_self_approval": 0,
			},
			{
				"state": "Pending Approval",
				"action": "Reject",
				"next_state": "Rejected",
				"allowed": "Accounts Manager",
				"allow_self_approval": 0,
			},
			{
				"state": "Rejected",
				"action": "Review",
				"next_state": "Draft",
				"allowed": "Accounts User",
				"allow_self_approval": 1,
			},
		],
	},
	"sales-invoice-discount-approval": {
		"workflow_name": "Sales Invoice Discount Approval",
		"document_type": "Sales Invoice",
		"description": (
			"Sales Invoices carrying more than 10% additional discount or any flat "
			"discount amount require Sales Manager approval; invoices within the "
			"discount limit are submitted directly by the Sales User."
		),
		"workflow_state_field": "workflow_state",
		"is_active": 1,
		"send_email_alert": 0,
		"states": [
			{"state": "Draft", "doc_status": "0", "allow_edit": "Sales User"},
			{"state": "Pending Approval", "doc_status": "0", "allow_edit": "Sales Manager"},
			{"state": "Approved", "doc_status": "1", "allow_edit": "Sales Manager"},
			{"state": "Rejected", "doc_status": "0", "allow_edit": "Sales User"},
		],
		"transitions": [
			# Discount within limit: direct submit by the Sales User.
			{
				"state": "Draft",
				"action": "Submit",
				"next_state": "Approved",
				"allowed": "Sales User",
				"allow_self_approval": 1,
				"condition": "doc.additional_discount_percentage <= 10 and doc.discount_amount <= 0",
			},
			# Discount exceeds limit: route to Sales Manager.
			{
				"state": "Draft",
				"action": "Submit",
				"next_state": "Pending Approval",
				"allowed": "Sales User",
				"allow_self_approval": 1,
				"condition": "doc.additional_discount_percentage > 10 or doc.discount_amount > 0",
			},
			{
				"state": "Pending Approval",
				"action": "Approve",
				"next_state": "Approved",
				"allowed": "Sales Manager",
				"allow_self_approval": 0,
			},
			{
				"state": "Pending Approval",
				"action": "Reject",
				"next_state": "Rejected",
				"allowed": "Sales Manager",
				"allow_self_approval": 0,
			},
			{
				"state": "Rejected",
				"action": "Review",
				"next_state": "Draft",
				"allowed": "Sales User",
				"allow_self_approval": 1,
			},
		],
	},
	"material-request-approval": {
		"workflow_name": "Material Request Approval",
		"document_type": "Material Request",
		"description": (
			"Every Material Request requires Stock Manager approval before it is submitted."
		),
		"workflow_state_field": "workflow_state",
		"is_active": 1,
		"send_email_alert": 0,
		"states": [
			{"state": "Draft", "doc_status": "0", "allow_edit": "Stock User"},
			{"state": "Pending Approval", "doc_status": "0", "allow_edit": "Stock Manager"},
			{"state": "Approved", "doc_status": "1", "allow_edit": "Stock Manager"},
			{"state": "Rejected", "doc_status": "0", "allow_edit": "Stock User"},
		],
		"transitions": [
			{
				"state": "Draft",
				"action": "Submit",
				"next_state": "Pending Approval",
				"allowed": "Stock User",
				"allow_self_approval": 1,
			},
			{
				"state": "Pending Approval",
				"action": "Approve",
				"next_state": "Approved",
				"allowed": "Stock Manager",
				"allow_self_approval": 0,
			},
			{
				"state": "Pending Approval",
				"action": "Reject",
				"next_state": "Rejected",
				"allowed": "Stock Manager",
				"allow_self_approval": 0,
			},
			{
				"state": "Rejected",
				"action": "Review",
				"next_state": "Draft",
				"allowed": "Stock User",
				"allow_self_approval": 1,
			},
		],
	},
}
