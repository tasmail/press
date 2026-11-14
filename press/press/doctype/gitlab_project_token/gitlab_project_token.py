# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

from frappe.model.document import Document


class GitLabProjectToken(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		access_token: DF.Password
		expires_on: DF.Date
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		project_path: DF.Data
	# end: auto-generated types

	pass
