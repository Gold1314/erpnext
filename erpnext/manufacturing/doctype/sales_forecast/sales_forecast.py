# Copyright (c) 2025, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.utils import add_to_date, cint, flt

from erpnext.manufacturing.forecasting.models import DEFAULT_SEASON_LENGTH

DEFAULT_HISTORY_MONTHS = 24


class SalesForecast(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.manufacturing.doctype.sales_forecast_item.sales_forecast_item import SalesForecastItem

		amended_from: DF.Link | None
		based_on: DF.Literal["Sales Order", "Sales Invoice", "Delivery Note"]
		company: DF.Link
		demand_number: DF.Int
		forecast_model: DF.Literal[
			"Auto",
			"Holt-Winters Additive",
			"Holt-Winters Multiplicative",
			"Holt Linear",
			"Exponential Smoothing",
			"Seasonal Naive",
			"Croston",
			"Croston SBA",
		]
		frequency: DF.Literal["Weekly", "Monthly"]
		from_date: DF.Date
		generation_method: DF.Literal["Manual", "Statistical"]
		history_from_date: DF.Date | None
		history_to_date: DF.Date | None
		items: DF.Table[SalesForecastItem]
		naming_series: DF.Literal["SF.YY.-.######"]
		parent_warehouse: DF.Link
		posting_date: DF.Date | None
		season_length: DF.Int
		selected_items: DF.TableMultiSelect[SalesForecastItem]
		status: DF.Literal["Planned", "MPS Generated", "Cancelled"]
	# end: auto-generated types

	def on_discard(self):
		self.db_set("status", "Cancelled")

	def generate_manual_demand(self):
		forecast_demand = []
		for row in self.selected_items:
			item_details = frappe.db.get_value(
				"Item", row.item_code, ["item_name", "stock_uom as uom"], as_dict=True
			)

			for index in range(self.demand_number):
				if self.frequency == "Monthly":
					delivery_date = add_to_date(self.from_date, months=index + 1)
				else:
					delivery_date = add_to_date(self.from_date, weeks=index + 1)

				forecast_demand.append(
					{
						"item_code": row.item_code,
						"delivery_date": delivery_date,
						"item_name": item_details.item_name,
						"uom": item_details.uom,
						"demand_qty": 1.0,
					}
				)

		for demand in forecast_demand:
			self.append("items", demand)

	def _delivery_date(self, index):
		"""Same date convention as manual generation: from_date + n periods."""
		if self.frequency == "Monthly":
			return add_to_date(self.from_date, months=index + 1)
		return add_to_date(self.from_date, weeks=index + 1)

	def get_statistical_defaults(self):
		"""Resolve the statistical-run parameters, applying documented defaults."""
		return frappe._dict(
			based_on=self.get("based_on") or "Sales Order",
			history_from_date=self.get("history_from_date")
			or add_to_date(self.from_date, months=-DEFAULT_HISTORY_MONTHS),
			history_to_date=self.get("history_to_date") or self.from_date,
			season_length=cint(self.get("season_length")) or DEFAULT_SEASON_LENGTH[self.frequency],
			forecast_model=self.get("forecast_model") or "Auto",
		)

	def generate_statistical_demand(self):
		from erpnext.manufacturing.forecasting import backtest, loaders

		settings = self.get_statistical_defaults()
		horizon = cint(self.demand_number)

		item_codes = [row.item_code for row in self.selected_items if row.item_code]
		if not item_codes:
			return

		series_map = loaders.get_demand_history(
			company=self.company,
			from_date=settings.history_from_date,
			to_date=settings.history_to_date,
			based_on=settings.based_on,
			periodicity=self.frequency,
			item_code=item_codes,
			warehouse=self.parent_warehouse,
		)

		item_details = self.get_item_details(item_codes)
		skipped = []

		for item_code in item_codes:
			series = series_map.get(item_code)
			history = series.values() if series else []

			if len(history) < 3 or not any(history):
				skipped.append(item_code)
				continue

			if settings.forecast_model == "Auto":
				fit, _results = backtest.select_champion(
					history, season_length=settings.season_length, horizon=horizon
				)
			else:
				fit = backtest.fit_named_model(
					history,
					settings.forecast_model,
					season_length=settings.season_length,
					horizon=horizon,
				)

			details = item_details.get(item_code, frappe._dict())
			mape = fit.metrics.get("mape")
			bias = fit.metrics.get("bias")

			for index in range(horizon):
				self.append(
					"items",
					{
						"item_code": item_code,
						"item_name": details.get("item_name"),
						"uom": details.get("uom"),
						"delivery_date": self._delivery_date(index),
						"demand_qty": flt(fit.forecast[index], 6),
						"forecast_model": fit.model_name,
						"mape": flt(mape, 2) if mape is not None else None,
						"bias": flt(bias, 4) if bias is not None else None,
					},
				)

		if skipped:
			frappe.msgprint(
				_(
					"No statistical forecast was generated for the following items because they have insufficient demand history between {0} and {1}: {2}"
				).format(
					frappe.bold(settings.history_from_date),
					frappe.bold(settings.history_to_date),
					", ".join(frappe.bold(item) for item in skipped),
				),
				title=_("Items Skipped"),
				indicator="orange",
			)

	def get_item_details(self, item_codes):
		details = frappe.get_all(
			"Item",
			filters={"name": ("in", item_codes)},
			fields=["name", "item_name", "stock_uom as uom"],
		)
		return {row.name: row for row in details}

	@frappe.whitelist()
	def generate_demand(self):
		self.set("items", [])
		if self.get("generation_method") == "Statistical":
			self.generate_statistical_demand()
		else:
			self.generate_manual_demand()


@frappe.whitelist()
def create_mps(source_name: str, target_doc: str | dict | Document | None = None):
	def postprocess(source, doc):
		doc.naming_series = "MPS.YY.-.######"

	doc = get_mapped_doc(
		"Sales Forecast",
		source_name,
		{
			"Sales Forecast": {
				"doctype": "Master Production Schedule",
				"validation": {"docstatus": ["=", 1]},
				"field_map": {
					"name": "sales_forecast",
					"from_date": "from_date",
				},
			},
		},
		target_doc,
		postprocess,
	)

	return doc
