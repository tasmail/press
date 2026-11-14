# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""GitLab source driver: read-only API calls and credential hygiene for one self-hosted GitLab.

The token of a project lives in Press Settings (child table `GitLab Project Token`). It travels only in the
`PRIVATE-TOKEN` header of an API call. Clone URLs are plain; the build and Press hosts hold the clone credential.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

import frappe
import requests
from frappe.utils import getdate, nowdate
from frappe.utils.password import get_decrypted_password

if TYPE_CHECKING:
	from collections.abc import Iterable

REDACTED = "[REDACTED]"
API_TIMEOUT_SECONDS = 20
RESPONSE_TEXT_LIMIT = 2000
PAGE_SIZE = 100
MAX_PAGES = 20
PROJECT_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)+$")
COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
TOKEN_PATTERN = re.compile(r"\bgl[a-z]{1,4}t-[A-Za-z0-9_-]{8,}")
HEADER_PATTERN = re.compile(
	r"(?i)\b(private-token|job-token|authorization)(\s*[:=]\s*)(?:bearer\s+|basic\s+)?\S+"
)
URL_CREDENTIAL_PATTERN = re.compile(r"(?<=://)[^/\s@]+@")


class GitLabError(Exception):
	"""Raised with a message that has already been redacted."""


class GitLabHTTPError(GitLabError):
	def __init__(self, status_code: int, text: str):
		super().__init__(text)
		self.status_code = status_code


def base_url() -> str | None:
	url = frappe.db.get_single_value("Press Settings", "gitlab_url")
	return url.strip().rstrip("/") if url else None


def validate_base_url(url: str) -> None:
	parts = urlsplit(url.strip())
	if parts.scheme != "https" or not parts.hostname:
		frappe.throw("GitLab URL must be an https URL", frappe.ValidationError)
	if parts.username or parts.password or parts.query or parts.fragment or parts.path.strip("/"):
		frappe.throw(
			"GitLab URL must hold only the scheme, the host and an optional port", frappe.ValidationError
		)


def provider_for(repository_url: str) -> str:
	"""`GitLab` when the URL points to the configured GitLab host, otherwise `GitHub`."""
	configured = base_url()
	if not configured:
		return "GitHub"
	host = (urlsplit(repository_url).hostname or "").lower()
	return "GitLab" if host == (urlsplit(configured).hostname or "").lower() else "GitHub"


def project_path_for(repository_url: str) -> str:
	"""The project path of a GitLab repository URL. Refuses every URL that is not a plain https project URL."""
	configured = urlsplit(base_url() or "")
	parts = urlsplit(repository_url)
	if parts.scheme != "https" or parts.username or parts.password or parts.query or parts.fragment:
		frappe.throw("A GitLab repository URL must be a plain https URL", frappe.ValidationError)
	if parts.netloc.lower() != configured.netloc.lower():
		frappe.throw(
			"The GitLab repository URL does not match the configured GitLab host", frappe.ValidationError
		)
	path = parts.path.strip("/").removesuffix(".git")
	if not PROJECT_PATH_PATTERN.match(path) or ".." in path.split("/"):
		frappe.throw(f"Invalid GitLab project path: {path}", frappe.ValidationError)
	return path


def clone_url(project_path: str) -> str:
	"""Plain clone URL, without any credential."""
	return f"{base_url()}/{project_path}"


def commit_url(repository_url: str, commit_hash: str) -> str:
	return f"{repository_url}/-/commit/{commit_hash}"


def require_system_manager(user: str, roles: Iterable[str]) -> None:
	if user != "Administrator" and "System Manager" not in roles:
		frappe.throw("Only a System Manager can add or change a GitLab source", frappe.PermissionError)


def project_token_rows() -> list[frappe._dict]:
	return frappe.get_all(
		"GitLab Project Token",
		filters={"parenttype": "Press Settings", "parent": "Press Settings"},
		fields=["name", "project_path", "expires_on"],
	)


def _token_value(row_name: str) -> str | None:
	return get_decrypted_password("GitLab Project Token", row_name, "access_token", raise_exception=False)


def configured_tokens() -> list[str]:
	return [token for row in project_token_rows() if (token := _token_value(row.name))]


def token_for(project_path: str) -> str:
	"""The token of exactly this project. A token is never used for another project."""
	wanted = project_path.strip("/").lower()
	for row in project_token_rows():
		if (row.project_path or "").strip("/").lower() != wanted:
			continue
		if row.expires_on and getdate(row.expires_on) < getdate(nowdate()):
			raise GitLabError(f"The GitLab token of project {project_path} has expired")
		if token := _token_value(row.name):
			return token
		raise GitLabError(f"The GitLab token of project {project_path} is empty")
	raise GitLabError(f"No GitLab token is configured for project {project_path}")


def validate_token_rows(rows) -> None:
	seen = set()
	for row in rows:
		path = (row.project_path or "").strip("/").lower()
		if not PROJECT_PATH_PATTERN.match(path):
			frappe.throw(f"Invalid GitLab project path in row {row.idx}", frappe.ValidationError)
		if path in seen:
			frappe.throw(f"GitLab project {path} is listed twice", frappe.ValidationError)
		seen.add(path)
		if not row.expires_on or not isinstance(getdate(row.expires_on), date):
			frappe.throw(f"The token of GitLab project {path} needs an expiry date", frappe.ValidationError)


def redact(text: str | None, tokens: Iterable[str] | None = None) -> str:
	"""Remove every configured token, token-shaped string, auth header and URL credential from `text`."""
	text = text or ""
	for token in configured_tokens() if tokens is None else tokens:
		if token:
			text = text.replace(token, REDACTED)
	text = TOKEN_PATTERN.sub(REDACTED, text)
	text = HEADER_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)
	return URL_CREDENTIAL_PATTERN.sub(f"{REDACTED}@", text)


def api_get(project_path: str, endpoint: str = "", params: dict | None = None) -> requests.Response:
	"""GET a project endpoint. No redirect is followed, and only a 2xx answer is returned."""
	configured = base_url()
	if not configured:
		raise GitLabError("GitLab URL is not configured in Press Settings")
	url = f"{configured}/api/v4/projects/{quote(project_path, safe='')}{endpoint}"
	if urlsplit(url).netloc.lower() != urlsplit(configured).netloc.lower():
		raise GitLabError("Refusing to send a GitLab token to another host")
	token = token_for(project_path)
	try:
		response = requests.get(
			url,
			headers={"PRIVATE-TOKEN": token},
			params=params,
			timeout=API_TIMEOUT_SECONDS,
			allow_redirects=False,
		)
	except requests.RequestException as exc:
		raise GitLabError(f"GitLab request failed: {type(exc).__name__}") from None
	return _checked(response, token)


def _checked(response: requests.Response, token: str) -> requests.Response:
	if 300 <= response.status_code < 400:
		raise GitLabHTTPError(
			response.status_code, "GitLab answered with a redirect; redirects are not followed"
		)
	if not response.ok:
		text = redact(response.text, [token, *configured_tokens()])[:RESPONSE_TEXT_LIMIT]
		raise GitLabHTTPError(response.status_code, text)
	return response


def _json(response: requests.Response):
	try:
		return response.json()
	except ValueError:
		raise GitLabError("GitLab answered with a body that is not JSON") from None


def branch(project_path: str, name: str) -> dict:
	return _json(api_get(project_path, f"/repository/branches/{quote(name, safe='')}"))


def commit(project_path: str, commit_hash: str) -> dict:
	if not COMMIT_PATTERN.match(commit_hash):
		raise GitLabError("Invalid commit hash")
	return _json(api_get(project_path, f"/repository/commits/{commit_hash}"))


def raw_file(project_path: str, file_path: str, ref: str) -> str:
	endpoint = f"/repository/files/{quote(file_path, safe='')}/raw"
	return api_get(project_path, endpoint, {"ref": ref}).text


def branches(project_path: str) -> list[dict]:
	"""All branches, `version-*` first like the GitHub listing."""
	found: list[dict] = []
	for page in range(1, MAX_PAGES + 1):
		chunk = _json(api_get(project_path, "/repository/branches", {"per_page": PAGE_SIZE, "page": page}))
		found.extend({"name": item["name"]} for item in chunk)
		if len(chunk) < PAGE_SIZE:
			break
	found.sort(key=lambda item: (0 if item["name"].startswith("version-") else 1, item["name"]))
	return found


def to_github_shape(data: dict, by_commit: bool) -> dict:
	"""Map a GitLab branch (or commit) answer to the GitHub shape that App Source reads."""
	info = data if by_commit else data.get("commit", {})
	commit_info = {
		"message": info.get("message"),
		"author": {"name": info.get("author_name"), "date": info.get("authored_date")},
	}
	if by_commit:
		return {"commit": commit_info}
	return {"name": data.get("name"), "commit": {"sha": info.get("id", ""), "commit": commit_info}}


def gitlab_source_among(source_names: Iterable[str]) -> str | None:
	names = [name for name in source_names if name]
	if not names:
		return None
	rows = frappe.get_all("App Source", {"name": ("in", names), "provider": "GitLab"}, pluck="name", limit=1)
	return rows[0] if rows else None


def refuse_gitlab_sources(source_names: Iterable[str], action: str) -> None:
	if source := gitlab_source_among(source_names):
		frappe.throw(
			f"{action} is not supported for the GitLab source {source}. Build a new bench instead.",
			frappe.ValidationError,
		)
