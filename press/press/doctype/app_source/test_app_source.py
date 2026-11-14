# Copyright (c) 2020, Frappe and Contributors
# See license.txt
from __future__ import annotations

import typing
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from press.press.doctype.app_release.test_app_release import create_test_app_release
from press.press.doctype.app_source.app_source import AppSource
from press.press.doctype.team.test_team import create_test_team
from press.utils import get_current_team

if typing.TYPE_CHECKING:
	from press.press.doctype.app.app import App


@patch.object(AppSource, "create_release", create_test_app_release)
def create_test_app_source(
	version: str,
	app: App,
	repository_url=None,
	branch: str = "master",
	team: str | None = None,
) -> AppSource:
	"""
	Create test app source for app with given version.

	Also creates app release without github api call.
	"""
	if not repository_url:
		repository_url = "https://github.com/frappe/erpnext"
	team = team or get_current_team()
	return app.add_source(repository_url=repository_url, branch=branch, frappe_version=version, team=team)


class TestAppSource(FrappeTestCase):
	def create_app(self, name: str, title: str):
		app: App = frappe.get_doc({"doctype": "App", "name": name, "title": title})
		app.insert(ignore_if_duplicate=True)
		return app

	@patch.object(AppSource, "after_insert", new=Mock())
	def test_validate_dependant_apps(self):
		team_name = create_test_team().name
		app: App = self.create_app("hrms", "HRMS")
		source = app.add_source(
			frappe_version="Nightly",
			repository_url="https://github.com/frappe/hrms",
			branch="develop",
			team=team_name,
		)

		for req_app in source.required_apps:
			self.assertEqual("https://github.com/frappe/erpnext", req_app.repository_url)

	@patch.object(AppSource, "after_insert", new=Mock())
	def test_sync_versions_replaces_versions_with_what_the_repo_currently_supports(self):
		team_name = create_test_team().name
		app: App = self.create_app("hrms", "HRMS")
		source = app.add_source(
			frappe_version="Version 14",
			repository_url="https://github.com/frappe/hrms",
			branch="develop",
			team=team_name,
		)
		source.append("versions", {"version": "Version 13"})
		source.save()

		with patch(
			"press.press.doctype.app_source.app_source.get_repo_app_info",
			return_value={"frappe_version": "Version 15", "title": "HRMS"},
		):
			source.sync_versions()

		self.assertEqual([row.version for row in source.versions], ["Version 15"])


class TestGitHubSourcesStayOnTheGitHubPath(FrappeTestCase):
	"""With a GitLab host configured, GitHub sources must behave exactly as before."""

	def setUp(self):
		super().setUp()
		from press.utils.test_gitlab import PROJECT_A, TOKEN_A, configure_gitlab

		configure_gitlab((PROJECT_A, TOKEN_A))
		hooks = {"ok": False, "status_code": 404, "text": ""}
		with patch("press.press.doctype.app_source.app_source.requests.get", return_value=Mock(**hooks)):
			app = frappe.get_doc({"doctype": "App", "name": "hrms", "title": "HRMS"}).insert(
				ignore_if_duplicate=True
			)
			with patch.object(AppSource, "after_insert", new=Mock()):
				self.source = app.add_source(
					frappe_version="Nightly",
					repository_url="https://github.com/frappe/hrms",
					branch="develop",
					team=create_test_team().name,
				)

	def tearDown(self):
		frappe.db.rollback()

	def test_source_keeps_the_github_provider_and_the_github_fields(self):
		self.assertEqual(self.source.provider, "GitHub")
		self.assertEqual(self.source.repository_url, "https://github.com/frappe/hrms")
		self.assertEqual(self.source.repository_owner, "frappe")
		self.assertEqual(self.source.repository, "hrms")
		self.assertFalse(self.source.repository_path)

	def test_hooks_py_is_still_read_from_raw_githubusercontent(self):
		with patch(
			"press.press.doctype.app_source.app_source.requests.get",
			return_value=Mock(ok=True, text='required_apps = ["frappe/erpnext"]'),
		) as get:
			self.source.validate_dependent_apps()

		get.assert_called_once_with(
			"https://raw.githubusercontent.com/frappe/hrms/develop/hrms/hooks.py", timeout=10
		)
		self.assertEqual(
			[row.repository_url for row in self.source.required_apps], ["https://github.com/frappe/erpnext"]
		)

	def test_poll_urls_and_headers_are_the_github_ones(self):
		headers = {"Authorization": "token abc"}
		with (
			patch("press.press.doctype.app_source.app_source.get_auth_headers", return_value=headers),
			patch("press.press.doctype.app_source.app_source.requests.get") as get,
		):
			self.source.poll_github()
			self.source.poll_github("abc1234")

		self.assertEqual(
			[call.args[0] for call in get.call_args_list],
			[
				"https://api.github.com/repos/frappe/hrms/branches/develop",
				"https://api.github.com/repos/frappe/hrms/commits/abc1234?page=1&per_page=1",
			],
		)
		self.assertTrue(all(call.kwargs == {"headers": headers} for call in get.call_args_list))

	def test_commit_info_is_read_from_the_github_answer(self):
		answer = Mock(ok=True)
		answer.json.return_value = {
			"commit": {"sha": "abc", "commit": {"message": "m", "author": {"name": "A"}}}
		}
		with patch.object(AppSource, "poll_github", return_value=answer):
			self.assertEqual(
				self.source.get_commit_info(), ("abc", {"message": "m", "author": {"name": "A"}}, True)
			)

	def test_a_github_404_still_marks_the_source_uninstalled(self):
		answer = Mock(ok=False, status_code=404, text="Not Found")
		with patch.object(AppSource, "poll_github", return_value=answer):
			self.assertEqual(self.source.get_commit_info(), ("", {}, False))

		self.source.reload()
		self.assertTrue(self.source.last_github_poll_failed)
		self.assertTrue(self.source.uninstalled)
		self.assertEqual(self.source.last_github_response, "Not Found")

	def test_repository_url_is_plain_without_an_installation_and_carries_the_token_with_one(self):
		self.assertEqual(self.source.get_repo_url(), "https://github.com/frappe/hrms")

		self.source.github_installation_id = "42"
		with patch("press.press.doctype.app_source.app_source.get_access_token", return_value="ghs_secret"):
			self.assertEqual(
				self.source.get_repo_url(), "https://x-access-token:ghs_secret@github.com/frappe/hrms"
			)

	def test_commit_link_and_branch_list_stay_on_github(self):
		from press.api.github import branches
		from press.press.doctype.app_release.test_app_release import create_test_app_release

		release = create_test_app_release(self.source, "abc1234")
		self.assertEqual(release.get_commit_link(), "https://github.com/frappe/hrms/commit/abc1234")

		answer = Mock(ok=True, status_code=200, links={})
		answer.json.return_value = [{"name": "develop"}, {"name": "version-15"}]
		with patch("press.api.github.requests.get", return_value=answer) as get:
			found = branches("frappe", "hrms", app_source=self.source.name)

		self.assertEqual(found, [{"name": "version-15"}, {"name": "develop"}])
		self.assertEqual(get.call_args.args[0], "https://api.github.com/repos/frappe/hrms/branches")

	def test_the_release_difference_of_a_github_source_still_asks_github(self):
		from press.press.doctype.app_release.test_app_release import create_test_app_release

		difference = frappe.get_doc(
			{
				"doctype": "App Release Difference",
				"app": self.source.app,
				"source": self.source.name,
				"source_release": create_test_app_release(self.source).name,
				"destination_release": create_test_app_release(self.source).name,
			}
		).insert()

		with patch("press.press.doctype.app_release_difference.app_release_difference.Github") as github:
			github.return_value.get_repo.side_effect = Exception("no access")
			difference.set_deploy_type()

		github.assert_called_once()
		self.assertEqual(
			frappe.db.get_value("App Release Difference", difference.name, "deploy_type"), "Migrate"
		)

	def test_a_github_clone_failure_keeps_its_two_outcomes(self):
		import subprocess

		from press.press.doctype.app_release.app_release import AppRelease
		from press.press.doctype.app_release.test_app_release import create_test_app_release

		release = create_test_app_release(self.source, "abc1234")

		def failing(output: bytes):
			def run(command: str):
				if command.startswith("git fetch"):
					raise subprocess.CalledProcessError(128, command, output=output)
				return ""

			return patch.object(AppRelease, "run", side_effect=run)

		with failing(b"Repository not found."), self.assertRaises(Exception) as raised:
			release._clone_repo()
		self.assertEqual(raised.exception.args, ("Repository could not be fetched", "hrms"))

		with failing(b"fatal: something else"), self.assertRaises(subprocess.CalledProcessError):
			release._clone_repo()
