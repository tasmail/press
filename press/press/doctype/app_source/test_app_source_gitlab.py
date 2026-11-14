# Copyright (c) 2026, Frappe and Contributors
# See license.txt
from __future__ import annotations

import json
import subprocess
from unittest.mock import Mock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase
from frappe.utils import nowdate

from press.api.github import _get_pyproject_from_commit, branches, get_dependant_apps_with_versions
from press.press.doctype.app.test_app import create_test_app
from press.press.doctype.app_release.app_release import AppRelease
from press.press.doctype.app_release.test_app_release import create_test_app_release
from press.press.doctype.app_source.app_source import AppSource
from press.press.doctype.app_source.test_app_source import create_test_app_source
from press.press.doctype.release_group.release_group import can_run_patch_build
from press.press.doctype.release_group.test_release_group import create_test_release_group
from press.utils.test_gitlab import (
	GITLAB_URL,
	PROJECT_A,
	PROJECT_B,
	TOKEN_A,
	TOKEN_B,
	configure_gitlab,
	create_gitlab_app_source,
	fake_response,
)

COMMIT_HASH = "0123456789abcdef0123456789abcdef01234567"
BRANCH_ANSWER = {
	"name": "main",
	"commit": {
		"id": COMMIT_HASH,
		"message": "Add the receipt",
		"author_name": "Dev Eloper",
		"authored_date": "2026-11-10T12:00:00.000+00:00",
	},
}
HOOKS_WITH_REQUIRED_APPS = 'app_name = "ua_prro"\nrequired_apps = ["erpnext"]\n'
API_BASE = f"{GITLAB_URL}/api/v4/projects/erpnext%2Fua_prro"


def all_error_logs() -> str:
	return json.dumps(frappe.get_all("Error Log", fields=["method", "error"]), default=str)


def source_as_text(source: AppSource) -> str:
	source.reload()
	return json.dumps(source.as_dict(), default=str)


class TestGitLabAppSource(FrappeTestCase):
	def setUp(self):
		super().setUp()
		configure_gitlab((PROJECT_A, TOKEN_A), (PROJECT_B, TOKEN_B))

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_source_gets_the_gitlab_provider_the_project_path_and_a_plain_url(self):
		source = create_gitlab_app_source(repository_url=f"{GITLAB_URL}/{PROJECT_A}.git")

		self.assertEqual(source.provider, "GitLab")
		self.assertEqual(source.repository_path, PROJECT_A)
		self.assertEqual(source.repository_url, f"{GITLAB_URL}/{PROJECT_A}")
		self.assertEqual(source.repository_owner, "erpnext")
		self.assertEqual(source.repository, "ua_prro")

	def test_source_is_refused_when_the_project_has_no_token(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			create_gitlab_app_source("erpnext/other")
		self.assertEqual(str(raised.exception), "No GitLab token is configured for project erpnext/other")

	def test_source_is_refused_with_a_github_installation(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			create_gitlab_app_source(github_installation_id="12345")
		self.assertEqual(str(raised.exception), "A GitLab source cannot have a GitHub installation")

	def test_source_is_refused_when_public(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			create_gitlab_app_source(public=1)
		self.assertEqual(str(raised.exception), "A GitLab source is always private")

	def test_source_is_refused_for_urls_that_are_not_plain_https_project_urls(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			create_gitlab_app_source(repository_url=f"http://git.example.test/{PROJECT_A}")
		self.assertEqual(str(raised.exception), "A GitLab repository URL must be a plain https URL")

	def test_source_is_refused_for_a_user_who_is_not_a_system_manager(self):
		denial = frappe.PermissionError("Only a System Manager can add or change a GitLab source")
		with (
			patch("press.utils.gitlab.require_system_manager", side_effect=denial) as require,
			self.assertRaises(frappe.PermissionError) as raised,
		):
			create_gitlab_app_source()

		require.assert_called_once()
		self.assertEqual(str(raised.exception), "Only a System Manager can add or change a GitLab source")
		self.assertFalse(frappe.db.exists("App Source", {"repository_url": f"{GITLAB_URL}/{PROJECT_A}"}))

	def test_hooks_py_of_the_project_adds_the_required_apps(self):
		source = create_gitlab_app_source(hooks=HOOKS_WITH_REQUIRED_APPS)

		self.assertEqual(
			[row.repository_url for row in source.required_apps], ["https://github.com/frappe/erpnext"]
		)

	def test_hooks_py_is_fetched_from_the_gitlab_api_for_the_branch(self):
		hooks = fake_response(text=HOOKS_WITH_REQUIRED_APPS)
		with (
			patch.object(AppSource, "after_insert", new=lambda self: None),
			patch("press.utils.gitlab.requests.get", return_value=hooks) as get,
		):
			create_gitlab_app_source_with_real_api()

		self.assertEqual(get.call_args.args[0], f"{API_BASE}/repository/files/ua_prro%2Fhooks.py/raw")
		self.assertEqual(get.call_args.kwargs["params"], {"ref": "main"})
		self.assertEqual(get.call_args.kwargs["headers"], {"PRIVATE-TOKEN": TOKEN_A})

	def test_a_failed_hooks_fetch_does_not_block_the_save(self):
		with (
			patch.object(AppSource, "after_insert", new=lambda self: None),
			patch("press.utils.gitlab.requests.get", return_value=fake_response(404, text="404")),
		):
			source = create_gitlab_app_source_with_real_api()

		self.assertEqual(len(source.required_apps), 0)

	def test_clone_url_is_plain_and_no_access_token_is_handed_out(self):
		source = create_gitlab_app_source()

		self.assertEqual(source.get_repo_url(), f"{GITLAB_URL}/{PROJECT_A}")
		self.assertNotIn("@", source.get_repo_url())
		self.assertIsNone(source.get_access_token())

	def test_commit_link_uses_the_gitlab_path(self):
		source = create_gitlab_app_source()
		release = create_test_app_release(source, COMMIT_HASH)

		self.assertEqual(release.get_commit_link(), f"{GITLAB_URL}/{PROJECT_A}/-/commit/{COMMIT_HASH}")


class TestGitLabPolling(FrappeTestCase):
	def setUp(self):
		super().setUp()
		configure_gitlab((PROJECT_A, TOKEN_A), (PROJECT_B, TOKEN_B))
		self.source = create_gitlab_app_source()

	def tearDown(self):
		frappe.db.rollback()

	def test_poll_creates_a_release_from_the_branch_head(self):
		with patch("press.utils.gitlab.requests.get", return_value=fake_response(body=BRANCH_ANSWER)) as get:
			release_name = self.source.create_release()

		self.assertEqual(get.call_args.args[0], f"{API_BASE}/repository/branches/main")
		release = frappe.get_doc("App Release", release_name)
		self.assertEqual(release.hash, COMMIT_HASH)
		self.assertEqual(release.message, "Add the receipt")
		self.assertEqual(release.author, "Dev Eloper")
		self.assertFalse(frappe.db.get_value("App Source", self.source.name, "last_github_poll_failed"))

	def test_poll_by_commit_reads_the_commit_endpoint(self):
		commit = BRANCH_ANSWER["commit"]
		with patch("press.utils.gitlab.requests.get", return_value=fake_response(body=commit)) as get:
			commit_hash, commit_info, ok = self.source.get_commit_info(COMMIT_HASH)

		self.assertEqual(get.call_args.args[0], f"{API_BASE}/repository/commits/{COMMIT_HASH}")
		self.assertEqual((commit_hash, ok), (COMMIT_HASH, True))
		self.assertEqual(commit_info["message"], "Add the receipt")
		self.assertEqual(commit_info["author"]["name"], "Dev Eloper")

	def test_the_scheduled_poll_dispatches_gitlab_sources_to_the_gitlab_api(self):
		from press.press.doctype.app.app import poll_new_releases

		def answer(url, **kwargs):
			if url.startswith(GITLAB_URL):
				return fake_response(body=BRANCH_ANSWER)
			raise requests.ConnectionError("no network in tests")

		with patch("press.utils.gitlab.requests.get", side_effect=answer):
			poll_new_releases()

		self.assertTrue(frappe.db.exists("App Release", {"source": self.source.name, "hash": COMMIT_HASH}))

	def test_a_404_marks_the_poll_failed_and_no_field_or_log_holds_a_token(self):
		body = f"404 Project Not Found PRIVATE-TOKEN: {TOKEN_A} {TOKEN_B}"
		with patch("press.utils.gitlab.requests.get", return_value=fake_response(404, text=body)):
			self.assertIsNone(self.source.create_release())

		self.source.reload()
		self.assertTrue(self.source.last_github_poll_failed)
		self.assertFalse(self.source.uninstalled)
		self.assertIn("404 Project Not Found", self.source.last_github_response)
		self.assert_no_token_anywhere()

	def test_a_401_marks_the_poll_failed_logs_it_and_no_field_or_log_holds_a_token(self):
		body = f"401 Unauthorized token={TOKEN_A} PRIVATE-TOKEN: {TOKEN_B}"
		with patch("press.utils.gitlab.requests.get", return_value=fake_response(401, text=body)):
			self.assertIsNone(self.source.create_release())

		self.source.reload()
		self.assertTrue(self.source.last_github_poll_failed)
		self.assertIn("401 Unauthorized", self.source.last_github_response)
		self.assertIn("Create Release Error", all_error_logs())
		self.assert_no_token_anywhere()

	def test_a_failed_poll_is_skipped_until_it_is_forced_again(self):
		with patch("press.utils.gitlab.requests.get", return_value=fake_response(401, text="nope")):
			self.source.create_release()

		with patch("press.utils.gitlab.requests.get") as get:
			self.assertIsNone(self.source.create_release())
		get.assert_not_called()

		with patch("press.utils.gitlab.requests.get", return_value=fake_response(body=BRANCH_ANSWER)):
			release_name = self.source.create_release(force=True)

		self.assertEqual(frappe.db.get_value("App Release", release_name, "hash"), COMMIT_HASH)
		self.source.reload()
		self.assertFalse(self.source.last_github_poll_failed)
		self.assertEqual(self.source.last_github_response or "", "")

	def test_a_redirect_is_not_followed_and_fails_the_poll(self):
		redirect = fake_response(302, headers={"Location": "https://evil.test/steal"})
		with patch("press.utils.gitlab.requests.get", return_value=redirect) as get:
			self.assertIsNone(self.source.create_release())

		self.assertEqual(get.call_count, 1)
		self.assertIs(get.call_args.kwargs["allow_redirects"], False)
		self.source.reload()
		self.assertTrue(self.source.last_github_poll_failed)
		self.assertEqual(
			self.source.last_github_response, "GitLab answered with a redirect; redirects are not followed"
		)

	def test_a_network_error_fails_the_poll_without_the_exception_text(self):
		error = requests.ConnectionError(f"boom {TOKEN_A}")
		with patch("press.utils.gitlab.requests.get", side_effect=error):
			self.assertIsNone(self.source.create_release())

		self.source.reload()
		self.assertTrue(self.source.last_github_poll_failed)
		self.assertEqual(self.source.last_github_response, "GitLab request failed: ConnectionError")
		self.assert_no_token_anywhere()

	def test_two_sources_of_two_projects_poll_with_their_own_tokens(self):
		other = create_gitlab_app_source(PROJECT_B)
		with patch("press.utils.gitlab.requests.get", return_value=fake_response(body=BRANCH_ANSWER)) as get:
			self.source.get_commit_info()
			other.get_commit_info()

		sent = [(call.args[0], call.kwargs["headers"]["PRIVATE-TOKEN"]) for call in get.call_args_list]
		self.assertEqual(
			sent,
			[
				(f"{API_BASE}/repository/branches/main", TOKEN_A),
				(f"{GITLAB_URL}/api/v4/projects/erpnext%2Fua_signer/repository/branches/main", TOKEN_B),
			],
		)

	def assert_no_token_anywhere(self):
		haystack = source_as_text(self.source) + all_error_logs()
		self.assertNotIn(TOKEN_A, haystack)
		self.assertNotIn(TOKEN_B, haystack)


class TestGitLabClone(FrappeTestCase):
	def setUp(self):
		super().setUp()
		configure_gitlab((PROJECT_A, TOKEN_A))
		self.source = create_gitlab_app_source()
		self.release = create_test_app_release(self.source, COMMIT_HASH)
		self.commands: list[str] = []

	def tearDown(self):
		frappe.db.rollback()

	def record(self, fail_fetch_with: bytes | None = None):
		def run(command: str):
			self.commands.append(command)
			if fail_fetch_with is not None and command.startswith("git fetch"):
				raise subprocess.CalledProcessError(128, command, output=fail_fetch_with)
			return ""

		return patch.object(AppRelease, "run", side_effect=run)

	def test_clone_commands_use_the_plain_url_and_no_credential(self):
		with self.record():
			self.release._clone_repo()

		self.assertIn(f"git remote add origin {GITLAB_URL}/{PROJECT_A}", self.commands)
		self.assertIn(f"git fetch --depth 1 origin {COMMIT_HASH}", self.commands)
		joined = "\n".join(self.commands)
		self.assertNotIn("@", joined)
		self.assertNotIn(TOKEN_A, joined)

	def test_a_failed_clone_raises_a_redacted_error_and_logs_no_token(self):
		output = (
			f"fatal: unable to access 'https://oauth2:{TOKEN_A}@git.example.test/x': PRIVATE-TOKEN: {TOKEN_A}"
		)
		with self.record(fail_fetch_with=output.encode()), self.assertRaises(Exception) as raised:
			self.release._clone_repo()

		self.assertEqual(raised.exception.args, ("Repository could not be fetched", "ua_prro"))
		self.assertTrue(raised.exception.__suppress_context__)
		logs = all_error_logs()
		self.assertIn("App Release Clone Error", logs)
		self.assertIn("[REDACTED]", logs)
		self.assertNotIn(TOKEN_A, logs)
		self.assertNotIn(TOKEN_A, str(raised.exception))

	def test_a_failed_release_command_logs_the_redacted_command_and_output(self):
		error = subprocess.CalledProcessError(1, "git", output=f"PRIVATE-TOKEN: {TOKEN_A} {TOKEN_A}".encode())
		with (
			patch("press.press.doctype.app_release.app_release.run", side_effect=error),
			patch("press.press.doctype.app_release.app_release.log_error") as log_error,
			patch.object(AppRelease, "cleanup"),
			self.assertRaises(subprocess.CalledProcessError),
		):
			self.release.run(f"git remote add origin https://oauth2:{TOKEN_A}@git.example.test/x")

		kwargs = log_error.call_args.kwargs
		self.assertNotIn(TOKEN_A, kwargs["command"])
		self.assertNotIn(TOKEN_A, kwargs["output"])
		self.assertIn("[REDACTED]", kwargs["output"])


class TestGitLabApiPaths(FrappeTestCase):
	def setUp(self):
		super().setUp()
		configure_gitlab((PROJECT_A, TOKEN_A))
		self.source = create_gitlab_app_source()

	def tearDown(self):
		frappe.db.rollback()

	def test_branches_of_a_gitlab_source_come_from_the_gitlab_api(self):
		with patch(
			"press.utils.gitlab.requests.get",
			return_value=fake_response(body=[{"name": "main"}, {"name": "version-15"}]),
		) as get:
			found = branches("erpnext", "ua_prro", app_source=self.source.name)

		self.assertEqual(found, [{"name": "version-15"}, {"name": "main"}])
		self.assertEqual(get.call_args.args[0], f"{API_BASE}/repository/branches")

	def test_branch_validation_returns_the_github_shape_and_redacts_errors(self):
		from press.api.bench import validate_gitlab_branch

		with patch("press.utils.gitlab.requests.get", return_value=fake_response(body=BRANCH_ANSWER)):
			answer = validate_gitlab_branch(self.source, "main")
		self.assertEqual(answer["name"], "main")
		self.assertEqual(answer["commit"]["sha"], COMMIT_HASH)

		body = f"404 Branch Not Found {TOKEN_A}"
		with (
			patch("press.utils.gitlab.requests.get", return_value=fake_response(404, text=body)),
			self.assertRaises(frappe.ValidationError) as raised,
		):
			validate_gitlab_branch(self.source, "missing")
		self.assertIn("Error validating branch from GitLab: 404 Branch Not Found", str(raised.exception))
		self.assertNotIn(TOKEN_A, str(raised.exception))

	def test_pyproject_is_read_at_the_commit_from_the_gitlab_api(self):
		pyproject = '[tool.bench.frappe-dependencies]\nfrappe = ">=15.0.0-dev,<16.0.0-dev"\n'
		with patch("press.utils.gitlab.requests.get", return_value=fake_response(text=pyproject)) as get:
			parsed = _get_pyproject_from_commit(self.source.name, COMMIT_HASH)

		self.assertEqual(parsed["tool"]["bench"]["frappe-dependencies"]["frappe"], ">=15.0.0-dev,<16.0.0-dev")
		self.assertEqual(get.call_args.args[0], f"{API_BASE}/repository/files/pyproject.toml/raw")
		self.assertEqual(get.call_args.kwargs["params"], {"ref": COMMIT_HASH})

	def test_dependencies_of_a_commit_are_read_through_the_gitlab_api(self):
		pyproject = (
			'[project]\nrequires-python = ">=3.10"\n'
			'[tool.bench.frappe-dependencies]\nfrappe = ">=15.0.0-dev,<16.0.0-dev"\n'
		)
		with patch("press.utils.gitlab.requests.get", return_value=fake_response(text=pyproject)):
			dependencies = get_dependant_apps_with_versions(self.source.name, COMMIT_HASH, cache=False)

		self.assertEqual(dependencies["python_version"], ">=3.10")
		self.assertEqual(dependencies["frappe_dependencies"], {"frappe": ">=15.0.0-dev,<16.0.0-dev"})

	def test_a_missing_pyproject_is_reported_like_on_github(self):
		with (
			patch("press.utils.gitlab.requests.get", return_value=fake_response(404, text="404")),
			self.assertRaises(frappe.ValidationError) as raised,
		):
			_get_pyproject_from_commit(self.source.name, COMMIT_HASH)
		self.assertEqual(str(raised.exception), "Pyproject not found at this commit")

	def test_sync_versions_reads_pyproject_of_the_branch_from_gitlab(self):
		pyproject = '[tool.bench.frappe-dependencies]\nfrappe = ">=14.0.0-dev,<16.0.0-dev"\n'
		with (
			patch("press.utils.gitlab.requests.get", return_value=fake_response(text=pyproject)) as get,
			patch(
				"press.press.doctype.app_source.app_source.parse_frappe_version", return_value=["Version 14"]
			) as parse,
			patch("press.press.doctype.app_source.app_source.get_repo_app_info") as github_info,
		):
			self.source.sync_versions()

		github_info.assert_not_called()
		parse.assert_called_once_with(">=14.0.0-dev,<16.0.0-dev", "UA PRRO", ease_versioning_constrains=True)
		self.assertEqual(get.call_args.kwargs["params"], {"ref": "main"})

	def test_the_app_release_difference_of_a_gitlab_source_is_a_migrate_without_a_github_call(self):
		from press.press.doctype.app_release.test_app_release import create_test_app_release

		old = create_test_app_release(self.source)
		new = create_test_app_release(self.source)
		difference = frappe.get_doc(
			{
				"doctype": "App Release Difference",
				"app": self.source.app,
				"source": self.source.name,
				"source_release": old.name,
				"destination_release": new.name,
			}
		).insert()

		with patch("press.press.doctype.app_release_difference.app_release_difference.Github") as github:
			difference.set_deploy_type()

		github.assert_not_called()
		self.assertEqual(
			frappe.db.get_value("App Release Difference", difference.name, "deploy_type"), "Migrate"
		)

	def test_the_clone_info_of_the_mcp_tool_is_plain_private_and_has_no_expiry(self):
		from press.mcp.tools.codebase import _app_clone_info

		release = create_test_app_release(self.source, COMMIT_HASH)
		info = _app_clone_info(self.source.app, self.source.name, release.name, COMMIT_HASH)

		self.assertEqual(info["provider"], "GitLab")
		self.assertTrue(info["private"])
		self.assertEqual(info["clone_url"], f"{GITLAB_URL}/{PROJECT_A}")
		self.assertIsNone(info["clone_url_expires_in_minutes"])


def create_gitlab_app_source_with_real_api() -> AppSource:
	"""Insert a GitLab source so that its hooks.py is fetched through `gitlab.raw_file`."""
	from press.press.doctype.app.test_app import create_test_app
	from press.utils import get_current_team

	app = create_test_app("ua_prro", "UA PRRO")
	return frappe.get_doc(
		{
			"doctype": "App Source",
			"app": app.name,
			"repository_url": f"{GITLAB_URL}/{PROJECT_A}",
			"branch": "main",
			"team": get_current_team(),
			"versions": [{"version": "Version 14"}],
		}
	).insert()


class TestGitLabSourceBenchRefusals(FrappeTestCase):
	def setUp(self):
		super().setUp()
		configure_gitlab((PROJECT_A, TOKEN_A))
		self.source = create_gitlab_app_source()
		self.refusal = (
			"{action} is not supported for the GitLab source "
			f"{self.source.name}. Build a new bench instead."
		)

	def tearDown(self):
		frappe.db.rollback()

	def fake_apps(self):
		return [frappe._dict(app="ua_prro", source=self.source.name)]

	def test_a_patch_build_is_refused_for_a_candidate_with_a_gitlab_source(self):
		from types import SimpleNamespace

		from press.press.doctype.deploy_candidate.deploy_candidate import DeployCandidate

		candidate = SimpleNamespace(apps=self.fake_apps(), create_build=Mock())
		with (
			patch("press.press.doctype.deploy_candidate.deploy_candidate.is_suspended", return_value=False),
			self.assertRaises(frappe.ValidationError) as raised,
		):
			DeployCandidate.trigger_patch_deploy(candidate)

		self.assertEqual(str(raised.exception), self.refusal.format(action="A patch build"))
		candidate.create_build.assert_not_called()

	def test_the_patch_app_updates_of_a_build_are_refused_for_a_gitlab_source(self):
		from types import SimpleNamespace

		from press.press.doctype.deploy_candidate_build.deploy_candidate_build import DeployCandidateBuild

		build = SimpleNamespace(candidate=SimpleNamespace(apps=self.fake_apps()))
		with self.assertRaises(frappe.ValidationError) as raised:
			DeployCandidateBuild._get_patch_app_updates(build, SimpleNamespace(apps=[]))

		self.assertEqual(str(raised.exception), self.refusal.format(action="A patch build"))

	def test_an_in_place_update_is_refused_when_the_update_is_validated(self):
		from types import SimpleNamespace

		from press.press.doctype.bench_update.bench_update import BenchUpdate

		update = SimpleNamespace(apps=self.fake_apps(), sites=[])
		with self.assertRaises(frappe.ValidationError) as raised:
			BenchUpdate.validate_inplace_update(update)

		self.assertEqual(str(raised.exception), self.refusal.format(action="An in place update"))

	def test_an_in_place_update_is_refused_on_the_bench_before_any_job_is_created(self):
		from types import SimpleNamespace

		from press.press.doctype.bench.bench import Bench

		bench = SimpleNamespace(set_self_and_site_status=Mock(), save=Mock())
		with self.assertRaises(frappe.ValidationError) as raised:
			Bench.update_inplace(bench, self.fake_apps(), [])

		self.assertEqual(str(raised.exception), self.refusal.format(action="An in place update"))
		bench.set_self_and_site_status.assert_not_called()
		bench.save.assert_not_called()

	def test_the_framework_branch_of_a_gitlab_source_is_refused(self):
		from press.press.doctype.release_group.release_group import ReleaseGroup

		with self.assertRaises(frappe.ValidationError) as raised:
			ReleaseGroup._validate_frappe_branch_matches_bench_version(Mock(), self.source, "version-15")
		self.assertEqual(str(raised.exception), "The Frappe framework from a GitLab source is not supported.")


class TestGitLabReleaseGroup(FrappeTestCase):
	def setUp(self):
		super().setUp()
		configure_gitlab((PROJECT_A, TOKEN_A))
		frappe.db.set_single_value("Press Settings", "allow_patch_builds", 1)
		self.frappe_app = create_test_app()
		self.frappe_source = create_test_app_source("Version 14", self.frappe_app)
		self.gitlab_source = create_gitlab_app_source()
		self.group = create_test_release_group(
			[self.frappe_app, frappe._dict(name="ua_prro")],
			app_sources=[self.frappe_source.name, self.gitlab_source.name],
		)

	def tearDown(self):
		frappe.db.rollback()

	def test_a_patch_build_is_not_possible_for_a_group_with_a_gitlab_source(self):
		with patch(
			"press.press.doctype.release_group.release_group._get_previous_candidate", return_value=None
		) as previous_candidate:
			self.assertFalse(can_run_patch_build(self.group.name))

		previous_candidate.assert_not_called()

	def test_a_patch_build_check_still_reaches_the_candidate_lookup_for_a_github_only_group(self):
		group = create_test_release_group([self.frappe_app], app_sources=[self.frappe_source.name])
		with patch(
			"press.press.doctype.release_group.release_group._get_previous_candidate", return_value=None
		) as previous_candidate:
			self.assertFalse(can_run_patch_build(group.name))

		previous_candidate.assert_called_once()

	@patch.object(AppSource, "create_release", create_test_app_release)
	@patch.object(AppSource, "sync_versions", new=Mock())
	def test_branch_change_keeps_the_gitlab_provider_and_the_source_private(self):
		with patch("press.utils.gitlab.raw_file", return_value=""):
			self.group.change_app_branch("ua_prro", "version-15")

		self.group.reload()
		new_source = frappe.get_doc("App Source", self.group.apps[1].source)
		self.assertNotEqual(new_source.name, self.gitlab_source.name)
		self.assertEqual(new_source.provider, "GitLab")
		self.assertEqual(new_source.branch, "version-15")
		self.assertEqual(new_source.repository_path, PROJECT_A)
		self.assertEqual(new_source.repository_url, f"{GITLAB_URL}/{PROJECT_A}")
		self.assertFalse(new_source.public)
		self.assertFalse(new_source.github_installation_id)


class TestPressSettingsGitLab(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_settings_refuse_a_gitlab_url_that_is_not_a_plain_https_host(self):
		settings = frappe.get_doc("Press Settings")
		settings.gitlab_url = "http://git.example.test"

		with self.assertRaises(frappe.ValidationError) as raised:
			settings.validate_gitlab()
		self.assertEqual(str(raised.exception), "GitLab URL must be an https URL")

	def test_settings_refuse_two_rows_for_one_project(self):
		settings = frappe.get_doc("Press Settings")
		settings.gitlab_url = GITLAB_URL
		for _ in range(2):
			settings.append(
				"gitlab_project_tokens",
				{"project_path": PROJECT_A, "access_token": TOKEN_A, "expires_on": nowdate()},
			)

		with self.assertRaises(frappe.ValidationError) as raised:
			settings.validate_gitlab()
		self.assertEqual(str(raised.exception), f"GitLab project {PROJECT_A} is listed twice")
