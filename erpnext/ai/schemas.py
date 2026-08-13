# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Extraction JSON schemas and pure validation/coercion.

The LLM's output is *untrusted input*, exactly like the XML the EDI inbound
module parses. This module turns raw model text into a normalized, typed
payload plus explicit ``errors`` (unusable) and ``warnings`` (usable, but a
human should look) — the same stage-don't-discard doctrine as
``erpnext.edi.inbound``. No frappe import anywhere.

Repairs are deliberately narrow: markdown fences, prose around the JSON
object, and trailing commas — the three failure modes every JSON-emitting
model actually exhibits. Anything beyond that is an error, not a guess.
"""

from __future__ import annotations

import datetime
import json
import re

#: Σ(lines) vs net_total, and net+tax vs grand_total: below this the numbers
#: are considered consistent (same tolerance as erpnext.edi.inbound.api)
TOTALS_TOLERANCE = 0.02

#: default confidence floor (0-1); fields scoring below it go to needs_review
DEFAULT_CONFIDENCE_THRESHOLD = 0.8

TOP_LEVEL_FIELDS = (
	"supplier_name",
	"supplier_vat",
	"invoice_number",
	"invoice_date",
	"due_date",
	"currency",
	"net_total",
	"tax_total",
	"grand_total",
)

#: fields whose absence makes the extraction reviewable but not unusable
OPTIONAL_FIELDS = ("supplier_vat", "due_date", "tax_total")

INVOICE_EXTRACTION_SCHEMA = {
	"type": "object",
	"additionalProperties": False,
	"properties": {
		"supplier_name": {"type": ["string", "null"], "description": "Legal name of the party issuing the invoice"},
		"supplier_vat": {"type": ["string", "null"], "description": "VAT / tax identifier of the issuer"},
		"invoice_number": {"type": ["string", "null"], "description": "The supplier's own invoice number, verbatim"},
		"invoice_date": {"type": ["string", "null"], "description": "Issue date, ISO 8601 (YYYY-MM-DD) preferred"},
		"due_date": {"type": ["string", "null"], "description": "Payment due date, ISO 8601 preferred"},
		"currency": {"type": ["string", "null"], "description": "ISO 4217 currency code, e.g. EUR"},
		"net_total": {"type": ["number", "string", "null"], "description": "Total before tax"},
		"tax_total": {"type": ["number", "string", "null"], "description": "Total tax amount"},
		"grand_total": {"type": ["number", "string", "null"], "description": "Total payable including tax"},
		"lines": {
			"type": "array",
			"items": {
				"type": "object",
				"additionalProperties": False,
				"properties": {
					"description": {"type": ["string", "null"]},
					"qty": {"type": ["number", "string", "null"]},
					"unit_price": {"type": ["number", "string", "null"]},
					"amount": {"type": ["number", "string", "null"], "description": "Net line total"},
					"tax_rate": {"type": ["number", "string", "null"], "description": "Tax rate percent for this line"},
				},
				"required": ["description", "qty", "unit_price", "amount", "tax_rate"],
			},
		},
		"taxes": {
			"type": "array",
			"items": {
				"type": "object",
				"additionalProperties": False,
				"properties": {
					"rate": {"type": ["number", "string", "null"], "description": "Tax rate percent"},
					"amount": {"type": ["number", "string", "null"], "description": "Tax amount at this rate"},
				},
				"required": ["rate", "amount"],
			},
		},
		"field_confidences": {
			"type": "object",
			"description": "Per-field confidence 0-1 for each top-level field, honestly assessed",
			"additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
		},
	},
	"required": [
		"supplier_name",
		"supplier_vat",
		"invoice_number",
		"invoice_date",
		"due_date",
		"currency",
		"net_total",
		"tax_total",
		"grand_total",
		"lines",
		"taxes",
		"field_confidences",
	],
}


# ------------------------------------------------------------- JSON repairs

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n?|\n?```\s*$", re.MULTILINE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def parse_llm_json(raw_text: str) -> tuple[dict | None, list[str]]:
	"""``json.loads`` with the three repairs models actually need.

	1. strip markdown code fences (```json ... ```),
	2. cut leading/trailing prose down to the outermost ``{...}``,
	3. remove trailing commas before ``}`` / ``]``.

	Returns ``(data, errors)``; ``data`` is None when nothing parseable
	remains. The *original* text is what callers should persist for audit.
	"""
	errors: list[str] = []
	if not raw_text or not raw_text.strip():
		return None, ["The model returned an empty response"]

	text = _FENCE_RE.sub("", raw_text).strip()

	start, end = text.find("{"), text.rfind("}")
	if start == -1 or end <= start:
		return None, ["The model response contains no JSON object"]
	text = text[start : end + 1]

	for attempt in (text, _TRAILING_COMMA_RE.sub(r"\1", text)):
		try:
			data = json.loads(attempt)
			break
		except json.JSONDecodeError as e:
			last_error = str(e)
	else:
		return None, [f"The model response is not valid JSON: {last_error}"]

	if not isinstance(data, dict):
		return None, ["The model response is valid JSON but not an object"]
	return data, errors


# ---------------------------------------------------------------- coercion

_MONTHS = {
	"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
	"jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")
_DMY_RE = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$")
_TEXTUAL_RE = re.compile(r"^(\d{1,2})\.?\s+([A-Za-z]{3,})\.?,?\s+(\d{4})$")
_TEXTUAL_US_RE = re.compile(r"^([A-Za-z]{3,})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$")


def parse_date(value, field: str = "date") -> tuple[str | None, list[str]]:
	"""Coerce a date in the formats suppliers actually print to ISO 8601.

	Supported: ``YYYY-MM-DD``, ``DD/MM/YYYY``, ``MM/DD/YYYY`` (also with
	``.`` or ``-`` separators), ``DD Mon YYYY`` and ``Mon DD, YYYY``.

	``xx/yy/YYYY`` with both parts <= 12 is genuinely ambiguous; the
	day-first reading is used (the majority convention outside the US) and a
	warning says so, so the field lands in review rather than silently wrong.
	"""
	warnings: list[str] = []
	if value is None or value == "":
		return None, warnings
	if isinstance(value, datetime.date):
		return value.isoformat(), warnings

	text = str(value).strip()

	m = _ISO_RE.match(text)
	if m:
		year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
		return _checked_date(year, month, day, field, text, warnings)

	m = _DMY_RE.match(text)
	if m:
		first, second, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
		if first > 12 and second <= 12:
			day, month = first, second
		elif second > 12 and first <= 12:
			day, month = second, first
		else:
			day, month = first, second  # day-first convention
			warnings.append(
				f"{field}: '{text}' is ambiguous (day/month both <= 12); read as day-first "
				f"{year:04d}-{second:02d}-{first:02d} — verify"
			)
		return _checked_date(year, month, day, field, text, warnings)

	m = _TEXTUAL_RE.match(text)
	if m:
		day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
		month = _MONTHS.get(month_name[:3].lower())
		if month:
			return _checked_date(year, month, day, field, text, warnings)

	m = _TEXTUAL_US_RE.match(text)
	if m:
		month_name, day, year = m.group(1), int(m.group(2)), int(m.group(3))
		month = _MONTHS.get(month_name[:3].lower())
		if month:
			return _checked_date(year, month, day, field, text, warnings)

	warnings.append(f"{field}: could not parse '{text}' as a date; left blank")
	return None, warnings


def _checked_date(year: int, month: int, day: int, field: str, text: str, warnings: list[str]):
	try:
		return datetime.date(year, month, day).isoformat(), warnings
	except ValueError:
		warnings.append(f"{field}: '{text}' is not a valid calendar date; left blank")
		return None, warnings


_AMOUNT_CLEAN_RE = re.compile(r"[^\d,.\-]")


def parse_amount(value, field: str = "amount") -> tuple[float | None, list[str]]:
	"""Coerce an amount that may carry currency symbols and locale separators.

	Handles ``1,234.56`` (anglo), ``1.234,56`` (continental — warned, since
	the reading flips the digits' meaning), lone-comma decimals (``42,50``)
	and dot-only thousands groups (``1.234.567``).
	"""
	warnings: list[str] = []
	if value is None or value == "":
		return None, warnings
	if isinstance(value, int | float):
		return float(value), warnings

	text = _AMOUNT_CLEAN_RE.sub("", str(value).strip())
	if not text or text in ("-", ".", ","):
		warnings.append(f"{field}: could not parse '{value}' as a number; left blank")
		return None, warnings

	has_comma, has_dot = "," in text, "." in text
	try:
		if has_comma and has_dot:
			if text.rfind(",") > text.rfind("."):
				# 1.234,56 — continental: dot groups thousands, comma is decimal
				normalized = text.replace(".", "").replace(",", ".")
				warnings.append(
					f"{field}: '{value}' read as continental format (decimal comma) = {normalized}"
				)
			else:
				normalized = text.replace(",", "")  # 1,234.56
			return float(normalized), warnings

		if has_comma:
			parts = text.split(",")
			if len(parts) == 2 and len(parts[1]) != 3:
				normalized = text.replace(",", ".")  # 42,50 — decimal comma
				warnings.append(f"{field}: '{value}' read with a decimal comma = {normalized}")
				return float(normalized), warnings
			return float(text.replace(",", "")), warnings  # 1,234 / 1,234,567

		if has_dot:
			parts = text.split(".")
			if len(parts) > 2:
				return float(text.replace(".", "")), warnings  # 1.234.567 — dot thousands
			return float(text), warnings

		return float(text), warnings
	except ValueError:
		warnings.append(f"{field}: could not parse '{value}' as a number; left blank")
		return None, warnings


# --------------------------------------------------------------- validation


def validate_extraction(
	raw_json_text: str,
	confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
	tolerance: float = TOTALS_TOLERANCE,
) -> tuple[dict | None, list[str], list[str]]:
	"""Raw model output -> normalized payload + errors + warnings.

	Returns ``(data, errors, warnings)``:

	* ``data`` — normalized dict (ISO dates, float amounts, ``lines`` and
	  ``taxes`` lists, ``field_confidences``, and ``needs_review`` — the
	  list of top-level fields that are missing or below the confidence
	  floor). ``None`` only when the text was not parseable JSON at all.
	* ``errors`` — the extraction is unusable as-is (no lines, no JSON).
	* ``warnings`` — usable but human attention wanted (ambiguous dates,
	  locale-coerced amounts, totals that do not cross-check).
	"""
	data, errors = parse_llm_json(raw_json_text)
	if data is None:
		return None, errors, []

	warnings: list[str] = []
	needs_review: list[str] = []
	result: dict = {}

	confidences = data.get("field_confidences")
	if not isinstance(confidences, dict):
		confidences = {}
		warnings.append("The model returned no field_confidences; every field is flagged for review")

	# --- top-level scalars
	for field in ("supplier_name", "supplier_vat", "invoice_number", "currency"):
		value = data.get(field)
		result[field] = str(value).strip() if value not in (None, "") else None
	if result.get("currency"):
		result["currency"] = result["currency"].upper()

	for field in ("invoice_date", "due_date"):
		value, date_warnings = parse_date(data.get(field), field)
		result[field] = value
		warnings.extend(date_warnings)
		if date_warnings:
			needs_review.append(field)

	for field in ("net_total", "tax_total", "grand_total"):
		value, amount_warnings = parse_amount(data.get(field), field)
		result[field] = value
		warnings.extend(amount_warnings)
		if amount_warnings:
			needs_review.append(field)

	# --- missing-field detection
	for field in TOP_LEVEL_FIELDS:
		if result.get(field) is None and field not in OPTIONAL_FIELDS:
			warnings.append(f"{field} is missing from the extraction")
			if field not in needs_review:
				needs_review.append(field)

	# --- lines
	raw_lines = data.get("lines")
	lines: list[dict] = []
	if not isinstance(raw_lines, list) or not raw_lines:
		errors.append("The extraction contains no invoice lines")
	else:
		for index, raw in enumerate(raw_lines, start=1):
			if not isinstance(raw, dict):
				errors.append(f"Line {index} is not an object")
				continue
			line = {"description": (str(raw.get("description")).strip() if raw.get("description") else None)}
			for key in ("qty", "unit_price", "amount", "tax_rate"):
				value, amount_warnings = parse_amount(raw.get(key), f"line {index} {key}")
				line[key] = value
				warnings.extend(amount_warnings)
			if line["amount"] is None and line["qty"] is not None and line["unit_price"] is not None:
				line["amount"] = round(line["qty"] * line["unit_price"], 2)
			lines.append(line)
	result["lines"] = lines

	# --- taxes
	raw_taxes = data.get("taxes")
	taxes: list[dict] = []
	if isinstance(raw_taxes, list):
		for index, raw in enumerate(raw_taxes, start=1):
			if not isinstance(raw, dict):
				continue
			rate, rate_warnings = parse_amount(raw.get("rate"), f"tax {index} rate")
			amount, amount_warnings = parse_amount(raw.get("amount"), f"tax {index} amount")
			warnings.extend(rate_warnings + amount_warnings)
			if rate is not None or amount is not None:
				taxes.append({"rate": rate, "amount": amount})
	result["taxes"] = taxes

	# --- totals cross-checks (warning, not error: the supplier's figures win)
	line_sum = sum(line["amount"] for line in lines if line["amount"] is not None)
	if lines and result.get("net_total") is not None and abs(line_sum - result["net_total"]) > tolerance:
		warnings.append(
			f"Sum of lines ({line_sum:.2f}) does not match net_total ({result['net_total']:.2f})"
		)
		if "net_total" not in needs_review:
			needs_review.append("net_total")

	if result.get("net_total") is not None and result.get("grand_total") is not None:
		tax_total = result.get("tax_total") or 0.0
		expected = result["net_total"] + tax_total
		if abs(expected - result["grand_total"]) > tolerance:
			warnings.append(
				f"net_total + tax_total ({expected:.2f}) does not match grand_total "
				f"({result['grand_total']:.2f})"
			)
			if "grand_total" not in needs_review:
				needs_review.append("grand_total")

	# --- confidence floor
	for field in TOP_LEVEL_FIELDS:
		if result.get(field) is None:
			continue  # absence handled above
		try:
			confidence = float(confidences.get(field)) if confidences.get(field) is not None else None
		except (TypeError, ValueError):
			confidence = None
		if confidence is None:
			if field not in needs_review:
				needs_review.append(field)
		elif confidence < confidence_threshold:
			warnings.append(f"{field}: confidence {confidence:.2f} is below the threshold {confidence_threshold:.2f}")
			if field not in needs_review:
				needs_review.append(field)

	result["field_confidences"] = {
		key: value for key, value in confidences.items() if isinstance(value, int | float)
	}
	result["needs_review"] = needs_review
	return result, errors, warnings
