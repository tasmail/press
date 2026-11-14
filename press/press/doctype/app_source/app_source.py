# Copyright (c) 2020, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

import frappe
import requests
from frappe.model.document import Document
from frappe.model.naming import make_autoname

from press.api.github import (
	GithubFetchError,
	get_access_token,
	get_auth_headers,
	parse_frappe_version_from_pyproject,
)
from press.api.github import app as get_repo_app_info
from press.overrides import get_permission_query_conditions_for_doctype
from press.press.doctype.app.app import parse_frappe_version
from press.utils import get_current_team, gitlab, log_error

REQUIRED_APPS_PATTERN = re.compile(r"^\s*(?!#)\s*required_apps\s*=\s*\[(.*?)\]", re.DOTALL | re.MULTILINE)

if TYPE_CHECKING:
	from press.press.doctype.app_release.app_release import AppRelease


class AppSource(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from press.press.doctype.app_source_version.app_source_version import AppSourceVersion
		from press.press.doctype.required_apps.required_apps import RequiredApps

		app: DF.Link
		app_title: DF.Data
		branch: DF.Data
		enabled: DF.Check
		frappe: DF.Check
		github_installation_id: DF.Data | None
		last_github_poll_failed: DF.Check
		last_github_response: DF.Code | None
		last_synced: DF.Datetime | None
		provider: DF.Literal["GitHub", "GitLab"]
		public: DF.Check
		repository: DF.Data | None
		repository_owner: DF.Data | None
		repository_path: DF.Data | None
		repository_url: DF.Data
		required_apps: DF.Table[RequiredApps]
		team: DF.Link
		uninstalled: DF.Check
		versions: DF.Table[AppSourceVersion]
	# end: auto-generated types

	dashboard_fields = ("repository_owner", "repository", "branch")

	@property
	def project_path(self) -> str:
		"""The GitLab project path of a GitLab source."""
		return self.repository_path or ""

	def set_required_apps(self, match: str):
		# In the format frappe/erpnext
		apps = match.replace("'", "").replace('"', "").replace(" ", "").split(",")
		added_required_apps = [app.repository_url for app in self.required_apps]

		for app in apps:
			try:
				owner, repo = app.split("/")
			except ValueError:
				owner, repo = "frappe", app

			repository_url = f"https://github.com/{owner}/{repo}"
			if repository_url not in added_required_apps:
				self.append("required_apps", {"repository_url": repository_url})

	def validate_dependent_apps(self):
		if self.provider == "GitLab":
			return self.validate_dependent_apps_from_gitlab()

		hooks_uri = f"{self.repository_owner}/{self.repository}/{self.branch}/{self.app}/hooks.py"
		raw_content_url = (
			f"https://{get_access_token(self.github_installation_id)}@raw.githubusercontent.com/"
			if self.github_installation_id
			else "https://raw.githubusercontent.com/"
		)
		uri = raw_content_url + hooks_uri

		try:
			response = requests.get(uri, timeout=10)
			if not response.ok:
				return None

			self.add_required_apps_from(response.text)

		except Exception as e:
			frappe.log_error(f"Error fetching hooks.py: {e}", "App Source Dependency Check")
			return None  # Continue with the save process even if validation fails
		return None

	def validate_dependent_apps_from_gitlab(self):
		try:
			hooks = gitlab.raw_file(self.project_path, f"{self.app}/hooks.py", self.branch)
		except gitlab.GitLabError as e:
			frappe.log_error(f"Error fetching hooks.py: {e}", "App Source Dependency Check")
			return  # Continue with the save process even if validation fails

		self.add_required_apps_from(hooks)

	def add_required_apps_from(self, hooks_source: str):
		required_apps = [
			required_app for required_app in REQUIRED_APPS_PATTERN.findall(hooks_source) if required_app
		]
		if required_apps:
			self.set_required_apps(match=required_apps[0])

	def autoname(self):
		series = f"SRC-{self.app}-.###"
		self.name = make_autoname(series)

	def after_insert(self):
		self.create_release()

	def on_update(self):
		self.create_release()

	def validate(self):
		self.set_provider()
		self.validate_source_signature()
		self.validate_duplicate_versions()

	def add_version(self, version):
		self.append("versions", {"version": version})
		self.save()

	@frappe.whitelist()
	def sync_versions(self):
		"""Replace `versions` with the Frappe versions the repo's pyproject.toml currently supports."""
		if self.app == "frappe":
			return
		app_info = self.get_app_info()
		supported_versions = parse_frappe_version(
			app_info.get("frappe_version"), app_info.get("title"), ease_versioning_constrains=True
		)
		ordered_versions = frappe.get_all(
			"Frappe Version",
			{"name": ("in", list(supported_versions))},
			pluck="name",
			order_by="number",
		)
		self.set("versions", [{"version": version} for version in ordered_versions])
		self.save()

	def get_app_info(self) -> dict:
		if self.provider == "GitLab":
			pyproject = gitlab.raw_file(self.project_path, "pyproject.toml", self.branch)
			return {
				"title": self.app_title,
				"frappe_version": parse_frappe_version_from_pyproject(pyproject, self.repository),
			}

		return get_repo_app_info(
			owner=self.repository_owner,
			repository=self.repository,
			branch=self.branch,
			installation=self.github_installation_id,
		)

	def set_provider(self):
		self.provider = gitlab.provider_for(self.repository_url)
		if self.provider == "GitLab":
			self.validate_gitlab_source()

	def validate_gitlab_source(self):
		gitlab.require_system_manager(frappe.session.user, frappe.get_roles())
		self.repository_path = gitlab.project_path_for(self.repository_url)
		self.repository_url = gitlab.clone_url(self.project_path)
		self.repository_owner, _, self.repository = self.repository_path.rpartition("/")

		if self.github_installation_id:
			frappe.throw("A GitLab source cannot have a GitHub installation", frappe.ValidationError)
		if self.public:
			frappe.throw("A GitLab source is always private", frappe.ValidationError)
		self.validate_gitlab_token()

	def validate_gitlab_token(self):
		try:
			gitlab.token_for(self.project_path)
		except gitlab.GitLabError as e:
			frappe.throw(str(e), frappe.ValidationError)

	def validate_source_signature(self):
		# Don't allow multiple sources with same signature
		if frappe.db.exists(
			"App Source",
			{
				"name": ("!=", self.name),
				"app": self.app,
				"repository_url": self.repository_url,
				"branch": self.branch,
				"team": self.team,
				"enabled": self.enabled,
			},
		):
			frappe.throw(
				f"Already added {(self.repository_url, self.branch)} for {self.app}",
				frappe.ValidationError,
			)

	def validate_duplicate_versions(self):
		# Don't allow versions to be added multiple times
		versions = set()
		for row in self.versions:
			if row.version in versions:
				frappe.throw(f"Version {row.version} can be added only once", frappe.ValidationError)
			versions.add(row.version)

	def before_save(self):
		if self.provider == "GitLab":
			self.validate_dependent_apps()
			return

		# Assumes repository_url looks like https://github.com/frappe/erpnext
		self.repository_url = self.repository_url.removesuffix(".git")

		_, self.repository_owner, self.repository = self.repository_url.rsplit("/", 2)
		self.validate_dependent_apps()
		# self.create_release()

	@frappe.whitelist()
	def create_release(
		self,
		force: bool = False,
		commit_hash: str | None = None,
	):
		if self.last_github_poll_failed and not force:
			return None

		_commit_hash, commit_info, ok = self.get_commit_info(
			commit_hash,
		)

		if not ok:
			return None

		try:
			return self._create_release(
				_commit_hash,
				commit_info,
			)
		except Exception:
			log_error("Create Release Error", doc=self)

	def _create_release(self, commit_hash: str, commit_info: dict) -> str:
		releases = frappe.get_all(
			"App Release",
			{
				"app": self.app,
				"source": self.name,
				"hash": commit_hash,
			},
			pluck="name",
			limit=1,
		)
		if len(releases) > 0:
			# No need to create a new release
			return releases[0]

		return self.create_release_from_commit_info(
			commit_hash,
			commit_info,
		).name

	def create_release_from_commit_info(
		self,
		commit_hash: str,
		commit_info: dict,
	):
		app_release: "AppRelease" = frappe.get_doc(
			{
				"doctype": "App Release",
				"app": self.app,
				"source": self.name,
				"hash": commit_hash,
				"team": self.team,
				"message": commit_info.get("message"),
				"author": commit_info.get("author", {}).get("name"),
				"timestamp": get_timestamp_from_commit_info(commit_info),
			}
		).insert(ignore_permissions=True)
		return app_release

	def get_commit_info(self, commit_hash: str | None = None) -> tuple[str, dict, bool]:
		"""
		If `commit_hash` is not provided, `commit_info` is of the latest commit
		on the branch pointed to by `self.hash`.
		"""
		if self.provider == "GitLab":
			return self.get_gitlab_commit_info(commit_hash)

		if (response := self.poll_github(commit_hash)).ok:
			self.set_poll_succeeded()
		else:
			self.set_poll_failed(response)
			self.db_update()
			return ("", {}, False)

		# Will cause recursion of db.save is used
		self.db_update()

		return unpack_commit_data(response.json(), commit_hash)

	def get_gitlab_commit_info(self, commit_hash: str | None = None) -> tuple[str, dict, bool]:
		failure = None
		try:
			data = self.poll_gitlab(commit_hash)
		except gitlab.GitLabError as error:
			failure = error

		if failure:
			self.set_gitlab_poll_failed(failure)
			self.db_update()
			return ("", {}, False)

		self.set_poll_succeeded()
		self.db_update()
		return unpack_commit_data(data, commit_hash)

	def poll_gitlab(self, commit_hash: str | None = None) -> dict:
		"""Latest branch commit, or the given commit, in the shape that GitHub answers."""
		if commit_hash:
			return gitlab.to_github_shape(gitlab.commit(self.project_path, commit_hash), by_commit=True)

		return gitlab.to_github_shape(gitlab.branch(self.project_path, self.branch), by_commit=False)

	def set_gitlab_poll_failed(self, error: gitlab.GitLabError):
		"""The error text is already redacted, so it can be stored and logged."""
		status_code = getattr(error, "status_code", 0)
		self.last_github_response = str(error)
		self.last_github_poll_failed = True
		self.last_synced = frappe.utils.now()
		self.uninstalled = False

		if status_code != 404:
			log_error(
				"Create Release Error",
				response_status_code=status_code,
				response_text=str(error),
				doc=self,
			)

	def poll_github(self, commit_hash: str | None = None) -> requests.Response:
		headers = self.get_auth_headers()
		url = f"https://api.github.com/repos/{self.repository_owner}/{self.repository}"

		if commit_hash:
			# page and per_page set to reduce unnecessary diff info
			url = f"{url}/commits/{commit_hash}?page=1&per_page=1"
		else:
			url = f"{url}/branches/{self.branch}"

		return requests.get(url, headers=headers)

	def set_poll_succeeded(self):
		self.last_github_response = ""
		self.last_github_poll_failed = False
		self.last_synced = frappe.utils.now()
		self.uninstalled = False

	def set_poll_failed(self, response):
		self.last_github_response = response.text or ""
		self.last_github_poll_failed = True
		self.last_synced = frappe.utils.now()

		"""
		If poll fails with 404 after updating the `github_installation_id` it
		*probably* means that FC hasn't been granted access to this particular
		app by the user.

		In this case the App Source is in an uninstalled state.
		"""
		self.uninstalled = response.status_code == 404

		if response.status_code != 404:
			log_error(
				"Create Release Error",
				response_status_code=response.status_code,
				response_text=response.text,
				doc=self,
			)

	def get_auth_headers(self) -> dict:
		return get_auth_headers(self.github_installation_id)

	def get_access_token(self) -> str | None:
		if self.provider == "GitLab":
			return None

		if self.github_installation_id:
			return get_access_token(self.github_installation_id)

		return frappe.get_value("Press Settings", None, "github_access_token")

	def get_repo_url(self) -> str:
		if self.provider == "GitLab":
			return gitlab.clone_url(self.project_path)

		if not self.github_installation_id:
			return self.repository_url

		token = get_access_token(self.github_installation_id)
		if token is None:
			# Do not edit without updating deploy_notifications.py
			raise GithubFetchError("App installation token could not be fetched", self.app)

		return f"https://x-access-token:{token}@github.com/{self.repository_owner}/{self.repository}"


def create_app_source(
	app: str, repository_url: str, branch: str, versions: list[str], required_apps: list[str]
) -> AppSource:
	team = get_current_team()

	app_source = frappe.get_doc(
		{
			"doctype": "App Source",
			"app": app,
			"repository_url": repository_url,
			"branch": branch,
			"team": team,
			"versions": [{"version": version} for version in versions],
			"required_apps": [{"repository_url": required_app} for required_app in required_apps],
		}
	)

	app_source.save()

	return app_source


get_permission_query_conditions = get_permission_query_conditions_for_doctype("App Source")


def unpack_commit_data(data: dict, commit_hash: str | None) -> tuple[str, dict, bool]:
	if commit_hash:
		return (commit_hash, data.get("commit", {}), True)

	commit_hash = data.get("commit", {}).get("sha", "")
	commit_info = data.get("commit", {}).get("commit", {})
	return (commit_hash, commit_info, True)


def get_timestamp_from_commit_info(commit_info: dict) -> str | None:
	timestamp_str = commit_info.get("author", {}).get("date")
	if not timestamp_str:
		return None

	timestamp_str = timestamp_str.replace("Z", "+00:00")
	return datetime.fromisoformat(timestamp_str).strftime("%Y-%m-%d %H:%M:%S")
