// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.provide("erpnext.agent_policy");

const AGENT_RISK_HINTS = {
	READ: __("Read-only lookups. Safe to allow broadly; Frappe permissions still apply."),
	DRAFT_WRITE: __("Creates and edits drafts only (docstatus 0). Nothing posts until a human submits."),
	SUBMIT: __(
		"Denied by the shipped policy. An active Workflow on the doctype always wins over an allow rule here."
	),
	DESTRUCTIVE: __("No destructive tool exists on this surface. Keep this denied."),
};

frappe.ui.form.on("Agent Policy", {
	refresh(frm) {
		erpnext.agent_policy.set_indicator(frm);
		erpnext.agent_policy.show_headline(frm);

		if (!frm.is_new()) {
			frm.add_custom_button(__("Preview My Permissions"), () =>
				erpnext.agent_policy.show_policy_summary()
			);
		}
	},

	enabled(frm) {
		erpnext.agent_policy.set_indicator(frm);
	},

	is_default(frm) {
		erpnext.agent_policy.set_indicator(frm);
	},
});

frappe.ui.form.on("Agent Policy Rule", {
	risk_level(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const hint = AGENT_RISK_HINTS[row.risk_level];
		if (hint) frappe.show_alert({ message: hint, indicator: "blue" }, 7);
	},

	allow(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		// the two risk classes that should essentially never be ticked
		if (row.allow && ["SUBMIT", "DESTRUCTIVE"].includes(row.risk_level)) {
			frappe.msgprint({
				title: __("This Widens the Agent Boundary"),
				indicator: "orange",
				message: __(
					"You are allowing {0} on {1}. Agents are meant to create drafts and let humans approve them. If the doctype has an active Workflow, the agent will still be refused and told to use the workflow action.",
					[row.risk_level, row.doctype_pattern || "*"]
				),
			});
		}
	},
});

erpnext.agent_policy = {
	set_indicator(frm) {
		if (frm.doc.__islocal) return;

		if (!frm.doc.enabled) {
			frm.page.set_indicator(__("Disabled"), "grey");
		} else if (frm.doc.is_default) {
			frm.page.set_indicator(__("Enforced"), "green");
		} else {
			frm.page.set_indicator(__("Enabled (not default)"), "blue");
		}
	},

	show_headline(frm) {
		frm.dashboard.clear_headline();
		if (frm.doc.__islocal) return;

		if (frm.doc.enabled && frm.doc.is_default) {
			frm.dashboard.set_headline(
				__("This is the policy the agent surface enforces right now.")
			);
		} else {
			frm.dashboard.set_headline(
				__(
					"Not in force. The agent surface enforces the enabled policy marked <b>Is Default</b>, or the shipped safe default when there is none."
				)
			);
		}
	},

	show_policy_summary() {
		frappe.call({
			method: "erpnext.agent.api.get_policy_summary",
			callback(r) {
				if (!r.message) return;

				const summary = r.message;
				const rows = Object.keys(summary.risks || {}).map((risk) => {
					const entry = summary.risks[risk];
					const doctypes = entry.any_doctype_allowed
						? __("any doctype")
						: (entry.allowed_doctypes || []).join(", ") || __("none");
					return `<tr>
						<td><b>${frappe.utils.escape_html(risk)}</b></td>
						<td>${frappe.utils.escape_html(doctypes)}</td>
						<td>${entry.requires_approval ? __("yes") : __("no")}</td>
					</tr>`;
				});

				frappe.msgprint({
					title: __("What {0} May Do", [summary.user]),
					message: `<p class="text-muted">${__("Policy in force: {0}", [
						frappe.utils.escape_html(summary.policy_source || ""),
					])}</p>
					<table class="table table-bordered">
						<thead><tr>
							<th>${__("Risk")}</th>
							<th>${__("Allowed Doctypes")}</th>
							<th>${__("Needs Approval")}</th>
						</tr></thead>
						<tbody>${rows.join("")}</tbody>
					</table>`,
					wide: true,
				});
			},
		});
	},
};
