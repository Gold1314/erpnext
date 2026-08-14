# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Model-agnostic LLM provider abstraction.

Data sovereignty is the design constraint: no vendor is hard-wired. The
:class:`OpenAICompatibleProvider` speaks the de-facto standard chat
completions dialect, which covers OpenAI itself *and* every self-hosted
gateway that mimics it — vLLM, Ollama (``/v1``), LiteLLM, llama.cpp server,
LM Studio, Azure-style proxies. :class:`AnthropicProvider` speaks the
Anthropic Messages API. Adding a provider means one subclass with a payload
builder — nothing upstream changes.

frappe-light by design
----------------------
Only :func:`get_provider` touches frappe (it reads the **AI Settings**
single). Everything else — the payload/header builders, the response-shape
extractors, :class:`MockProvider` — is pure and unit-testable without a
site. The HTTP transport is the ``requests`` library, the same pattern the
rest of ERPNext uses for outbound calls (see
``erpnext/accounts/doctype/currency_exchange_settings``); frappe's
``make_post_request`` helper is not used anywhere in this app.

Tests never hit the network: they exercise the pure builders/extractors and
:class:`MockProvider` only.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod

import requests

ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_API_VERSION = "2023-06-01"

#: name of the forced tool used to obtain schema-constrained JSON from the
#: Anthropic Messages API (which has no OpenAI-style ``response_format``)
ANTHROPIC_JSON_TOOL_NAME = "emit_extraction"

DEFAULT_TIMEOUT = 120


class ProviderError(Exception):
	"""The provider was reachable but the call failed (HTTP error, bad shape)."""


class ProviderNotConfigured(Exception):
	"""AI Settings is missing or incomplete; carries a user-facing message."""


# ------------------------------------------------------------- pure builders


def build_openai_payload(
	model: str,
	system_prompt: str,
	user_content: str,
	response_json_schema: dict | None = None,
	temperature: float = 0.0,
	max_tokens: int = 4000,
) -> dict:
	"""Chat-completions request body (OpenAI dialect).

	When a JSON schema is given, ``response_format`` uses the structured
	``json_schema`` mode; servers that ignore it still get the schema
	discipline from the system prompt, so degradation is graceful.
	"""
	payload = {
		"model": model,
		"messages": [
			{"role": "system", "content": system_prompt},
			{"role": "user", "content": user_content},
		],
		"temperature": temperature,
		"max_tokens": max_tokens,
	}
	if response_json_schema:
		payload["response_format"] = {
			"type": "json_schema",
			"json_schema": {
				"name": "invoice_extraction",
				"strict": True,
				"schema": response_json_schema,
			},
		}
	return payload


def build_openai_headers(api_key: str | None) -> dict:
	"""Bearer auth; the key is optional because self-hosted servers
	(vLLM/Ollama without auth) commonly run keyless."""
	headers = {"Content-Type": "application/json"}
	if api_key:
		headers["Authorization"] = f"Bearer {api_key}"
	return headers


def extract_openai_text(response: dict) -> str:
	"""``choices[0].message.content`` — with a named error when absent."""
	try:
		content = response["choices"][0]["message"]["content"]
	except (KeyError, IndexError, TypeError) as e:
		raise ProviderError(f"Unexpected chat-completions response shape: {e!r}") from e
	if content is None:
		raise ProviderError("The model returned an empty message (content is null)")
	return content


def build_anthropic_payload(
	model: str,
	system_prompt: str,
	user_content: str,
	response_json_schema: dict | None = None,
	temperature: float = 0.0,
	max_tokens: int = 4000,
) -> dict:
	"""Messages-API request body.

	Anthropic has no ``response_format``; the equivalent discipline is a
	**forced tool call** whose ``input_schema`` is our extraction schema —
	the model must emit arguments that validate against it, which is exactly
	the JSON document we want.
	"""
	payload = {
		"model": model,
		"system": system_prompt,
		"messages": [{"role": "user", "content": user_content}],
		"temperature": temperature,
		"max_tokens": max_tokens,
	}
	if response_json_schema:
		payload["tools"] = [
			{
				"name": ANTHROPIC_JSON_TOOL_NAME,
				"description": "Emit the structured invoice extraction result.",
				"input_schema": response_json_schema,
			}
		]
		payload["tool_choice"] = {"type": "tool", "name": ANTHROPIC_JSON_TOOL_NAME}
	return payload


def build_anthropic_headers(api_key: str) -> dict:
	return {
		"Content-Type": "application/json",
		"x-api-key": api_key or "",
		"anthropic-version": ANTHROPIC_API_VERSION,
	}


def extract_anthropic_text(response: dict) -> str:
	"""First ``tool_use`` block (forced-tool mode) or concatenated text blocks."""
	blocks = response.get("content")
	if not isinstance(blocks, list):
		raise ProviderError("Unexpected Messages API response shape: no content blocks")

	for block in blocks:
		if block.get("type") == "tool_use":
			return json.dumps(block.get("input") or {})

	text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
	if not text:
		raise ProviderError("The model returned no usable text or tool_use block")
	return text


# ------------------------------------------------------------------ providers


class LLMProvider(ABC):
	"""One method. Everything an intake flow needs from a model."""

	@abstractmethod
	def complete(
		self,
		system_prompt: str,
		user_content: str,
		response_json_schema: dict | None = None,
		temperature: float = 0.0,
		max_tokens: int = 4000,
	) -> str:
		"""Return the model's raw text output (ideally a JSON document)."""


class MockProvider(LLMProvider):
	"""Deterministic provider for tests and demos.

	``responses`` is either a list of canned strings (returned in order,
	repeating the last one when exhausted) or a callable
	``(system_prompt, user_content) -> str``. Every call is recorded on
	``self.calls`` so tests can assert on the prompts that were sent.
	"""

	def __init__(self, responses):
		self.responses = responses
		self.calls: list[dict] = []
		self._cursor = 0

	def complete(
		self,
		system_prompt: str,
		user_content: str,
		response_json_schema: dict | None = None,
		temperature: float = 0.0,
		max_tokens: int = 4000,
	) -> str:
		self.calls.append(
			{
				"system_prompt": system_prompt,
				"user_content": user_content,
				"response_json_schema": response_json_schema,
				"temperature": temperature,
				"max_tokens": max_tokens,
			}
		)
		if callable(self.responses):
			return self.responses(system_prompt, user_content)
		if not self.responses:
			raise ProviderError("MockProvider has no canned responses")
		index = min(self._cursor, len(self.responses) - 1)
		self._cursor += 1
		return self.responses[index]


class OpenAICompatibleProvider(LLMProvider):
	"""Any server speaking the ``/chat/completions`` dialect.

	Covers OpenAI, Azure-style proxies and — the sovereignty case — fully
	self-hosted vLLM, Ollama, LiteLLM and llama.cpp deployments where the
	invoice text never leaves the operator's infrastructure.
	"""

	def __init__(self, base_url: str, api_key: str | None, model: str, timeout: int = DEFAULT_TIMEOUT):
		self.base_url = (base_url or "").rstrip("/")
		self.api_key = api_key
		self.model = model
		self.timeout = timeout

	@property
	def endpoint(self) -> str:
		return f"{self.base_url}/chat/completions"

	def complete(
		self,
		system_prompt: str,
		user_content: str,
		response_json_schema: dict | None = None,
		temperature: float = 0.0,
		max_tokens: int = 4000,
	) -> str:
		payload = build_openai_payload(
			self.model, system_prompt, user_content, response_json_schema, temperature, max_tokens
		)
		try:
			response = requests.post(
				self.endpoint,
				json=payload,
				headers=build_openai_headers(self.api_key),
				timeout=self.timeout,
			)
			response.raise_for_status()
		except requests.exceptions.RequestException as e:
			raise ProviderError(f"LLM request to {self.endpoint} failed: {e}") from e
		return extract_openai_text(response.json())


class AnthropicProvider(LLMProvider):
	def __init__(
		self,
		api_key: str,
		model: str,
		base_url: str = ANTHROPIC_DEFAULT_BASE_URL,
		timeout: int = DEFAULT_TIMEOUT,
	):
		self.api_key = api_key
		self.model = model
		self.base_url = (base_url or ANTHROPIC_DEFAULT_BASE_URL).rstrip("/")
		self.timeout = timeout

	@property
	def endpoint(self) -> str:
		return f"{self.base_url}/v1/messages"

	def complete(
		self,
		system_prompt: str,
		user_content: str,
		response_json_schema: dict | None = None,
		temperature: float = 0.0,
		max_tokens: int = 4000,
	) -> str:
		payload = build_anthropic_payload(
			self.model, system_prompt, user_content, response_json_schema, temperature, max_tokens
		)
		try:
			response = requests.post(
				self.endpoint,
				json=payload,
				headers=build_anthropic_headers(self.api_key),
				timeout=self.timeout,
			)
			response.raise_for_status()
		except requests.exceptions.RequestException as e:
			raise ProviderError(f"LLM request to {self.endpoint} failed: {e}") from e
		return extract_anthropic_text(response.json())


# --------------------------------------------------------- frappe adapter


def get_provider() -> LLMProvider:
	"""Build the configured provider from the **AI Settings** single.

	The only function in this module that imports frappe. Raises
	:class:`ProviderNotConfigured` (already translated) when settings are
	missing, so callers can surface it verbatim.
	"""
	import frappe
	from frappe import _

	settings = frappe.get_cached_doc("AI Settings")
	provider_type = (settings.provider_type or "").strip()
	model = (settings.model or "").strip()

	if not provider_type or not model:
		raise ProviderNotConfigured(
			_("No AI provider is configured. Set Provider Type and Model in AI Settings.")
		)

	api_key = settings.get_password("api_key", raise_exception=False)
	timeout = int(settings.request_timeout or DEFAULT_TIMEOUT)

	if provider_type == "Anthropic":
		if not api_key:
			raise ProviderNotConfigured(_("Set the API Key in AI Settings to use the Anthropic provider."))
		return AnthropicProvider(
			api_key=api_key,
			model=model,
			base_url=settings.base_url or ANTHROPIC_DEFAULT_BASE_URL,
			timeout=timeout,
		)

	if provider_type == "OpenAI Compatible":
		if not settings.base_url:
			raise ProviderNotConfigured(
				_(
					"Set the Base URL in AI Settings (e.g. https://api.openai.com/v1, or your "
					"self-hosted vLLM / Ollama / LiteLLM endpoint)."
				)
			)
		return OpenAICompatibleProvider(
			base_url=settings.base_url, api_key=api_key, model=model, timeout=timeout
		)

	raise ProviderNotConfigured(_("Unknown provider type {0} in AI Settings.").format(provider_type))
