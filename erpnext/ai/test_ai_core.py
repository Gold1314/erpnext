# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure AI intake core.

``providers.py`` (builders/extractors/MockProvider), ``schemas.py`` and
``prompts.py`` have no module-level frappe dependency, but importing them
through the ``erpnext`` package would pull in ``erpnext/__init__.py`` (which
imports frappe). So when run as a plain file -

	python erpnext/ai/test_ai_core.py

- the modules are loaded directly from their file paths under their canonical
names (same bootstrap as ``erpnext/edi/inbound/test_inbound.py``).

No test touches the network: HTTP-bearing providers are exercised only
through their pure payload/header builders; everything end-to-end runs on
:class:`MockProvider`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_module(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	sys.modules[name] = module
	spec.loader.exec_module(module)
	return module


try:
	from erpnext.ai import prompts, providers, schemas
except Exception:
	# stand-alone run: register stub packages so absolute imports resolve
	# without importing erpnext/__init__.py (which needs frappe)
	for _pkg in ("erpnext", "erpnext.ai"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	providers = _load_module("erpnext.ai.providers", _HERE / "providers.py")
	schemas = _load_module("erpnext.ai.schemas", _HERE / "schemas.py")
	prompts = _load_module("erpnext.ai.prompts", _HERE / "prompts.py")


# ---------------------------------------------------------------- fixtures


def make_extraction(**overrides) -> dict:
	"""A fully consistent extraction: 2 lines, 19% tax, all sums add up."""
	data = {
		"supplier_name": "Muster & Söhne GmbH",
		"supplier_vat": "DE123456789",
		"invoice_number": "RE-2026-0042",
		"invoice_date": "2026-07-14",
		"due_date": "2026-08-13",
		"currency": "EUR",
		"net_total": 300.0,
		"tax_total": 57.0,
		"grand_total": 357.0,
		"lines": [
			{"description": "Widget A", "qty": 10, "unit_price": 20.0, "amount": 200.0, "tax_rate": 19},
			{"description": "Widget B", "qty": 4, "unit_price": 25.0, "amount": 100.0, "tax_rate": 19},
		],
		"taxes": [{"rate": 19, "amount": 57.0}],
		"field_confidences": {
			"supplier_name": 0.98,
			"supplier_vat": 0.97,
			"invoice_number": 0.99,
			"invoice_date": 0.95,
			"due_date": 0.9,
			"currency": 0.99,
			"net_total": 0.96,
			"tax_total": 0.96,
			"grand_total": 0.97,
		},
	}
	data.update(overrides)
	return data


def extraction_json(**overrides) -> str:
	return json.dumps(make_extraction(**overrides), ensure_ascii=False)


# ---------------------------------------------------------- payload builders


class TestOpenAIPayload(unittest.TestCase):
	def test_payload_shape(self):
		payload = providers.build_openai_payload(
			"qwen2.5-72b", "SYS", "USER", temperature=0.1, max_tokens=1234
		)
		self.assertEqual(payload["model"], "qwen2.5-72b")
		self.assertEqual(
			payload["messages"],
			[{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}],
		)
		self.assertEqual(payload["temperature"], 0.1)
		self.assertEqual(payload["max_tokens"], 1234)
		self.assertNotIn("response_format", payload)

	def test_json_schema_response_format(self):
		payload = providers.build_openai_payload(
			"gpt-x", "SYS", "USER", response_json_schema=schemas.INVOICE_EXTRACTION_SCHEMA
		)
		fmt = payload["response_format"]
		self.assertEqual(fmt["type"], "json_schema")
		self.assertTrue(fmt["json_schema"]["strict"])
		self.assertIs(fmt["json_schema"]["schema"], schemas.INVOICE_EXTRACTION_SCHEMA)

	def test_headers(self):
		self.assertEqual(
			providers.build_openai_headers("sk-abc")["Authorization"], "Bearer sk-abc"
		)
		# keyless self-hosted servers (vLLM/Ollama) must not get an auth header
		self.assertNotIn("Authorization", providers.build_openai_headers(None))

	def test_extract_text(self):
		response = {"choices": [{"message": {"content": '{"ok": 1}'}}]}
		self.assertEqual(providers.extract_openai_text(response), '{"ok": 1}')
		with self.assertRaises(providers.ProviderError):
			providers.extract_openai_text({"choices": []})
		with self.assertRaises(providers.ProviderError):
			providers.extract_openai_text({"choices": [{"message": {"content": None}}]})

	def test_endpoint_normalization(self):
		provider = providers.OpenAICompatibleProvider("http://localhost:11434/v1/", None, "llama3")
		self.assertEqual(provider.endpoint, "http://localhost:11434/v1/chat/completions")


class TestAnthropicPayload(unittest.TestCase):
	def test_payload_shape(self):
		payload = providers.build_anthropic_payload("claude-x", "SYS", "USER", max_tokens=2000)
		self.assertEqual(payload["system"], "SYS")
		self.assertEqual(payload["messages"], [{"role": "user", "content": "USER"}])
		self.assertEqual(payload["max_tokens"], 2000)
		self.assertNotIn("tools", payload)

	def test_forced_tool_for_json_schema(self):
		payload = providers.build_anthropic_payload(
			"claude-x", "SYS", "USER", response_json_schema=schemas.INVOICE_EXTRACTION_SCHEMA
		)
		self.assertEqual(len(payload["tools"]), 1)
		self.assertIs(payload["tools"][0]["input_schema"], schemas.INVOICE_EXTRACTION_SCHEMA)
		self.assertEqual(
			payload["tool_choice"],
			{"type": "tool", "name": providers.ANTHROPIC_JSON_TOOL_NAME},
		)

	def test_headers(self):
		headers = providers.build_anthropic_headers("sk-ant-xyz")
		self.assertEqual(headers["x-api-key"], "sk-ant-xyz")
		self.assertEqual(headers["anthropic-version"], providers.ANTHROPIC_API_VERSION)

	def test_extract_text_from_tool_use(self):
		response = {"content": [{"type": "tool_use", "input": {"invoice_number": "X-1"}}]}
		self.assertEqual(
			json.loads(providers.extract_anthropic_text(response)), {"invoice_number": "X-1"}
		)

	def test_extract_text_from_text_blocks(self):
		response = {"content": [{"type": "text", "text": "{\"a\":"}, {"type": "text", "text": " 1}"}]}
		self.assertEqual(providers.extract_anthropic_text(response), '{"a": 1}')
		with self.assertRaises(providers.ProviderError):
			providers.extract_anthropic_text({"content": []})


class TestMockProvider(unittest.TestCase):
	def test_determinism_and_recording(self):
		mock = providers.MockProvider(["one", "two"])
		self.assertEqual(mock.complete("S", "U1"), "one")
		self.assertEqual(mock.complete("S", "U2"), "two")
		# exhausted -> keeps returning the last canned response, deterministically
		self.assertEqual(mock.complete("S", "U3"), "two")
		self.assertEqual([call["user_content"] for call in mock.calls], ["U1", "U2", "U3"])

	def test_callable_responses(self):
		mock = providers.MockProvider(lambda system, user: f"echo:{user}")
		self.assertEqual(mock.complete("S", "abc"), "echo:abc")
		self.assertEqual(mock.complete("S", "abc"), "echo:abc")


# ------------------------------------------------------------------ schemas


class TestValidateExtraction(unittest.TestCase):
	def test_happy_path(self):
		data, errors, warnings = schemas.validate_extraction(extraction_json())
		self.assertEqual(errors, [])
		self.assertEqual(warnings, [])
		self.assertEqual(data["needs_review"], [])
		self.assertEqual(data["invoice_number"], "RE-2026-0042")
		self.assertEqual(data["invoice_date"], "2026-07-14")
		self.assertEqual(data["grand_total"], 357.0)
		self.assertEqual(len(data["lines"]), 2)
		self.assertEqual(data["taxes"], [{"rate": 19.0, "amount": 57.0}])

	def test_markdown_fence_stripped(self):
		raw = "```json\n" + extraction_json() + "\n```"
		data, errors, _warnings = schemas.validate_extraction(raw)
		self.assertEqual(errors, [])
		self.assertEqual(data["invoice_number"], "RE-2026-0042")

	def test_prose_around_json_stripped(self):
		raw = "Here is the extraction you asked for:\n" + extraction_json() + "\nLet me know!"
		data, errors, _warnings = schemas.validate_extraction(raw)
		self.assertEqual(errors, [])
		self.assertEqual(data["supplier_vat"], "DE123456789")

	def test_trailing_comma_repaired(self):
		raw = '{"supplier_name": "X", "lines": [{"description": "a", "qty": 1, "unit_price": 2, "amount": 2, "tax_rate": 0,},], "field_confidences": {},}'
		data, errors, _warnings = schemas.validate_extraction(raw)
		self.assertIsNotNone(data)
		self.assertNotIn("The extraction contains no invoice lines", errors)
		self.assertEqual(data["lines"][0]["amount"], 2.0)

	def test_unparseable_json_is_error(self):
		data, errors, warnings = schemas.validate_extraction("the dog ate the invoice")
		self.assertIsNone(data)
		self.assertTrue(errors)
		self.assertEqual(warnings, [])

	def test_missing_lines_is_error(self):
		data, errors, _warnings = schemas.validate_extraction(extraction_json(lines=[]))
		self.assertIsNotNone(data)  # staged, not discarded
		self.assertIn("The extraction contains no invoice lines", errors)

	def test_totals_mismatch_warns_not_errors(self):
		data, errors, warnings = schemas.validate_extraction(extraction_json(grand_total=999.99))
		self.assertEqual(errors, [])
		self.assertTrue(any("grand_total" in warning for warning in warnings))
		self.assertIn("grand_total", data["needs_review"])

	def test_line_sum_mismatch_warns(self):
		data, errors, warnings = schemas.validate_extraction(
			extraction_json(net_total=250.0, grand_total=307.0)
		)
		self.assertEqual(errors, [])
		self.assertTrue(any("Sum of lines" in warning for warning in warnings))
		self.assertIn("net_total", data["needs_review"])

	def test_totals_within_tolerance_pass(self):
		data, _errors, warnings = schemas.validate_extraction(extraction_json(grand_total=357.01))
		self.assertFalse(any("grand_total" in warning for warning in warnings))
		self.assertNotIn("grand_total", data["needs_review"])

	def test_confidence_floor_flagging(self):
		confidences = make_extraction()["field_confidences"]
		confidences["supplier_vat"] = 0.4
		data, _errors, warnings = schemas.validate_extraction(
			extraction_json(field_confidences=confidences)
		)
		self.assertIn("supplier_vat", data["needs_review"])
		self.assertTrue(any("below the threshold" in warning for warning in warnings))
		# tighter threshold flags more fields
		data_strict, _e, _w = schemas.validate_extraction(
			extraction_json(), confidence_threshold=0.99
		)
		self.assertIn("invoice_date", data_strict["needs_review"])

	def test_missing_required_field_flagged(self):
		data, errors, warnings = schemas.validate_extraction(extraction_json(invoice_number=None))
		self.assertEqual(errors, [])
		self.assertIn("invoice_number", data["needs_review"])
		self.assertTrue(any("invoice_number is missing" in warning for warning in warnings))

	def test_string_amounts_coerced(self):
		data, errors, _warnings = schemas.validate_extraction(
			extraction_json(net_total="300.00", tax_total="57.00", grand_total="357.00")
		)
		self.assertEqual(errors, [])
		self.assertEqual(data["grand_total"], 357.0)


class TestDateParsing(unittest.TestCase):
	def test_iso(self):
		self.assertEqual(schemas.parse_date("2026-07-14"), ("2026-07-14", []))

	def test_day_first_unambiguous(self):
		value, warnings = schemas.parse_date("25/12/2025")
		self.assertEqual(value, "2025-12-25")
		self.assertEqual(warnings, [])

	def test_month_first_unambiguous(self):
		value, warnings = schemas.parse_date("12/25/2025")
		self.assertEqual(value, "2025-12-25")
		self.assertEqual(warnings, [])

	def test_ambiguous_warns_and_reads_day_first(self):
		value, warnings = schemas.parse_date("03/04/2025")
		self.assertEqual(value, "2025-04-03")
		self.assertTrue(warnings and "ambiguous" in warnings[0])

	def test_textual(self):
		self.assertEqual(schemas.parse_date("5 Mar 2026")[0], "2026-03-05")
		self.assertEqual(schemas.parse_date("14 July 2026")[0], "2026-07-14")
		self.assertEqual(schemas.parse_date("Mar 5, 2026")[0], "2026-03-05")

	def test_dotted_separator(self):
		self.assertEqual(schemas.parse_date("31.01.2026")[0], "2026-01-31")

	def test_garbage_warns_and_blanks(self):
		value, warnings = schemas.parse_date("sometime soon", "due_date")
		self.assertIsNone(value)
		self.assertTrue(warnings and "due_date" in warnings[0])

	def test_invalid_calendar_date(self):
		value, warnings = schemas.parse_date("2026-02-31")
		self.assertIsNone(value)
		self.assertTrue(warnings)


class TestAmountParsing(unittest.TestCase):
	def test_plain(self):
		self.assertEqual(schemas.parse_amount(357), (357.0, []))
		self.assertEqual(schemas.parse_amount("357.5"), (357.5, []))

	def test_anglo_thousands(self):
		value, warnings = schemas.parse_amount("1,234.56")
		self.assertEqual(value, 1234.56)
		self.assertEqual(warnings, [])

	def test_continental_warns(self):
		value, warnings = schemas.parse_amount("1.234,56", "net_total")
		self.assertEqual(value, 1234.56)
		self.assertTrue(warnings and "continental" in warnings[0])

	def test_decimal_comma_warns(self):
		value, warnings = schemas.parse_amount("42,50")
		self.assertEqual(value, 42.5)
		self.assertTrue(warnings and "decimal comma" in warnings[0])

	def test_comma_thousands_only(self):
		self.assertEqual(schemas.parse_amount("1,234")[0], 1234.0)

	def test_dot_thousands_groups(self):
		self.assertEqual(schemas.parse_amount("1.234.567")[0], 1234567.0)

	def test_currency_symbols_stripped(self):
		self.assertEqual(schemas.parse_amount("€ 1,234.56")[0], 1234.56)
		self.assertEqual(schemas.parse_amount("$99.00")[0], 99.0)

	def test_garbage_warns_and_blanks(self):
		value, warnings = schemas.parse_amount("N/A", "tax_total")
		self.assertIsNone(value)
		self.assertTrue(warnings and "tax_total" in warnings[0])


# ------------------------------------------------------------------ prompts


class TestPrompts(unittest.TestCase):
	def test_system_prompt_doctrine(self):
		self.assertIn("NEVER invent", prompts.SYSTEM_PROMPT_INVOICE)
		self.assertIn("null", prompts.SYSTEM_PROMPT_INVOICE)
		self.assertIn("field_confidences", prompts.SYSTEM_PROMPT_INVOICE)

	def test_user_content_fences_document(self):
		content = prompts.build_user_content("INVOICE #42 ...")
		self.assertIn("===== DOCUMENT START =====", content)
		self.assertIn("INVOICE #42 ...", content)
		self.assertIn("untrusted document content", content)

	def test_hints_included_but_subordinate(self):
		content = prompts.build_user_content(
			"text", {"company_currency": "EUR", "supplier_candidates": ["Muster & Söhne GmbH"]}
		)
		self.assertIn('"company_currency": "EUR"', content)
		self.assertIn("Muster & Söhne GmbH", content)
		self.assertIn("document\ntext always wins".replace("\n", " "), content.replace("\n", " "))

	def test_no_hints_no_context_block(self):
		self.assertNotIn("Context from the accounting system", prompts.build_user_content("text"))


# ------------------------------------------------- end-to-end on the mock


class TestEndToEndWithMock(unittest.TestCase):
	def test_full_pipeline(self):
		"""Prompt -> MockProvider -> validate: the exact path api.py drives."""
		mock = providers.MockProvider(["```json\n" + extraction_json() + "\n```"])
		user_content = prompts.build_user_content("RE-2026-0042 ...", {"company_currency": "EUR"})
		raw = mock.complete(
			prompts.SYSTEM_PROMPT_INVOICE,
			user_content,
			response_json_schema=schemas.INVOICE_EXTRACTION_SCHEMA,
		)
		data, errors, warnings = schemas.validate_extraction(raw)
		self.assertEqual(errors, [])
		self.assertEqual(warnings, [])
		self.assertEqual(data["supplier_name"], "Muster & Söhne GmbH")
		self.assertEqual(mock.calls[0]["response_json_schema"], schemas.INVOICE_EXTRACTION_SCHEMA)


if __name__ == "__main__":
	unittest.main(verbosity=2)
