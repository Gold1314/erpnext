frappe.ui.form.on("Sales Forecast", {
	refresh(frm) {
		frm.trigger("set_query_filters");
		frm.trigger("set_custom_buttons");
		frm.trigger("toggle_statistical_fields");
		frm.set_df_property("items", "cannot_add_rows", true);
	},

	generation_method(frm) {
		frm.trigger("toggle_statistical_fields");
	},

	frequency(frm) {
		frm.trigger("toggle_statistical_fields");
	},

	toggle_statistical_fields(frm) {
		const statistical = frm.doc.generation_method === "Statistical";
		["based_on", "forecast_model", "season_length", "history_from_date", "history_to_date"].forEach(
			(field) => frm.toggle_display(field, statistical)
		);

		if (statistical && !frm.doc.season_length) {
			frm.set_df_property(
				"season_length",
				"description",
				__("Leave blank to use {0} periods per season for {1} frequency.", [
					frm.doc.frequency === "Weekly" ? 52 : 12,
					frm.doc.frequency,
				])
			);
		}
	},

	set_query_filters(frm) {
		frm.set_query("parent_warehouse", (doc) => {
			return {
				filters: {
					is_group: 1,
					company: doc.company,
				},
			};
		});

		frm.set_query("item_code", "items", () => {
			return {
				filters: {
					disabled: 0,
					is_stock_item: 1,
				},
			};
		});
	},

	generate_demand(frm) {
		frm.call({
			method: "generate_demand",
			doc: frm.doc,
			freeze: true,
			callback: function (r) {
				frm.reload_doc();
			},
		});
	},

	set_custom_buttons(frm) {
		if (frm.doc.docstatus === 1 && frm.doc.status === "Planned") {
			frm.add_custom_button(__("Create MPS"), () => {
				frappe.model.open_mapped_doc({
					method: "erpnext.manufacturing.doctype.sales_forecast.sales_forecast.create_mps",
					frm: frm,
				});
			}).addClass("btn-primary");
		}
	},
});
