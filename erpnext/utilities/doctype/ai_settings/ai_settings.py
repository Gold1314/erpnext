# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Connection settings for the AI document intake layer.

Deliberately dumb: it stores *which* endpoint to talk to; the provider
construction logic lives in :func:`erpnext.ai.providers.get_provider` so it
stays testable without a site.
"""

from frappe.model.document import Document


class AISettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Password | None
		base_url: DF.Data | None
		model: DF.Data | None
		provider_type: DF.Literal["OpenAI Compatible", "Anthropic"]
		request_timeout: DF.Int
	# end: auto-generated types

	pass
