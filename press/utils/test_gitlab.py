# Copyright (c) 2026, Frappe and Contributors
# See license.txt
from __future__ import annotations

import json
from unittest.mock import patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from press.press.doctype.app.test_app import create_test_app
from press.press.doctype.app_source.app_source import AppSource
from press.utils import get_current_team, gitlab

GITLAB_URL = "https://git.example.test"
PROJECT_A = "erpnext/ua_prro"
PROJECT_B = "erpnext/ua_signer"
TOKEN_A = "token-a-0123456789abcdef"
TOKEN_B = "token-b-0123456789abcdef"


def configure_gitlab(*projects: tuple[str, str], expires_in_days: int = 30) -> None:
	"""Set the GitLab URL and one token row per `(project_path, token)`, as the Press Settings form does."""
	frappe.db.set_single_value("Press Settings", "gitlab_url", GITLAB_URL)
	frappe.db.delete("GitLab Project Token", {"parent": "Press Settings"})
	for index, (project_path, token) in enumerate(projects, start=1):
		frappe.get_doc(
			{
				"doctype": "GitLab Project Token",
				"parent": "Press Settings",
				"parenttype": "Press Settings",
				"parentfield": "gitlab_project_tokens",
				"idx": index,
				"project_path": project_path,
				"access_token": token,
				"expires_on": add_days(nowdate(), expires_in_days),
			}
		).insert(ignore_permissions=True)


def fake_response(status_code: int = 200, body=None, text: str = "", headers: dict | None = None):
	response = requests.Response()
	response.status_code = status_code
	response.encoding = "utf-8"
	response._content = (json.dumps(body) if body is not None else text).encode()
	response.headers.update(headers or {})
	return response


def create_gitlab_app_source(
	project_path: str = PROJECT_A,
	branch: str = "main",
	hooks: str = "",
	repository_url: str | None = None,
	**fields,
) -> AppSource:
	"""A GitLab App Source that makes no API call on insert. `hooks` is the text of the app's hooks.py."""
	app = create_test_app("ua_prro", "UA PRRO")
	with (
		patch.object(AppSource, "after_insert", new=lambda self: None),
		patch("press.utils.gitlab.raw_file", return_value=hooks),
	):
		return frappe.get_doc(
			{
				"doctype": "App Source",
				"app": app.name,
				"repository_url": repository_url or f"{GITLAB_URL}/{project_path}",
				"branch": branch,
				"team": get_current_team(),
				"versions": [{"version": "Version 14"}],
				**fields,
			}
		).insert()


class TestGitLabDetection(FrappeTestCase):
	def setUp(self):
		super().setUp()
		configure_gitlab((PROJECT_A, TOKEN_A))

	def tearDown(self):
		frappe.db.rollback()

	def test_provider_is_github_for_every_url_while_gitlab_url_is_not_set(self):
		frappe.db.set_single_value("Press Settings", "gitlab_url", "")
		self.assertEqual(gitlab.provider_for(f"{GITLAB_URL}/{PROJECT_A}"), "GitHub")

	def test_provider_is_gitlab_only_for_the_configured_host(self):
		self.assertEqual(gitlab.provider_for(f"{GITLAB_URL}/{PROJECT_A}"), "GitLab")
		self.assertEqual(gitlab.provider_for("https://github.com/frappe/erpnext"), "GitHub")

	def test_provider_is_github_for_hosts_that_only_look_like_the_gitlab_host(self):
		for url in (
			"https://git.example.test.evil.test/erpnext/ua_prro",
			"https://evil.test/git.example.test/erpnext/ua_prro",
			"https://git.example.test@evil.test/erpnext/ua_prro",
			"https://notgit.example.test/erpnext/ua_prro",
		):
			with self.subTest(url=url):
				self.assertEqual(gitlab.provider_for(url), "GitHub")

	def test_project_path_accepts_git_suffix_and_trailing_slash(self):
		self.assertEqual(gitlab.project_path_for(f"{GITLAB_URL}/{PROJECT_A}.git"), PROJECT_A)
		self.assertEqual(gitlab.project_path_for(f"{GITLAB_URL}/{PROJECT_A}/"), PROJECT_A)
		self.assertEqual(gitlab.project_path_for(f"{GITLAB_URL}/group/sub/project"), "group/sub/project")

	def test_project_path_refuses_urls_that_are_not_plain_https_project_urls(self):
		plain = "A GitLab repository URL must be a plain https URL"
		cases = {
			"http://git.example.test/erpnext/ua_prro": plain,
			"https://user:secret@git.example.test/erpnext/ua_prro": plain,
			"https://git.example.test/erpnext/ua_prro?private_token=x": plain,
			"https://git.example.test/erpnext/ua_prro#frag": plain,
			"https://git.example.test:8443/erpnext/ua_prro": (
				"The GitLab repository URL does not match the configured GitLab host"
			),
			"https://git.example.test/ua_prro": "Invalid GitLab project path: ua_prro",
			"https://git.example.test/erpnext/../ua_prro": "Invalid GitLab project path: erpnext/../ua_prro",
			"https://git.example.test/erpnext//ua_prro": "Invalid GitLab project path: erpnext//ua_prro",
		}
		for url, message in cases.items():
			with self.subTest(url=url), self.assertRaises(frappe.ValidationError) as raised:
				gitlab.project_path_for(url)
			self.assertEqual(str(raised.exception), message)

	def test_base_url_must_be_an_https_host_without_credentials_or_path(self):
		gitlab.validate_base_url("https://git.example.test")
		gitlab.validate_base_url("https://git.example.test:8443")
		for url in (
			"http://git.example.test",
			"https://user:pw@git.example.test",
			"https://git.example.test/path",
			"https://git.example.test?x=1",
		):
			with self.subTest(url=url), self.assertRaises(frappe.ValidationError):
				gitlab.validate_base_url(url)

	def test_only_a_system_manager_may_add_a_gitlab_source(self):
		gitlab.require_system_manager("Administrator", [])
		gitlab.require_system_manager("operator@example.test", ["System Manager"])
		with self.assertRaises(frappe.PermissionError) as raised:
			gitlab.require_system_manager("customer@example.test", ["Press User"])
		self.assertEqual(str(raised.exception), "Only a System Manager can add or change a GitLab source")

	def test_clone_and_commit_urls_hold_no_credential(self):
		self.assertEqual(gitlab.clone_url(PROJECT_A), f"{GITLAB_URL}/{PROJECT_A}")
		self.assertEqual(
			gitlab.commit_url(f"{GITLAB_URL}/{PROJECT_A}", "abc1234"),
			f"{GITLAB_URL}/{PROJECT_A}/-/commit/abc1234",
		)


class TestGitLabTokens(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_token_round_trip_returns_the_value_and_the_column_holds_no_secret(self):
		configure_gitlab((PROJECT_A, TOKEN_A))

		self.assertEqual(gitlab.token_for(PROJECT_A), TOKEN_A)
		stored = frappe.get_all(
			"GitLab Project Token", filters={"parent": "Press Settings"}, fields=["access_token"]
		)
		self.assertEqual([row.access_token for row in stored], ["*" * len(TOKEN_A)])

	def test_each_project_gets_its_own_token_when_two_projects_are_configured(self):
		configure_gitlab((PROJECT_A, TOKEN_A), (PROJECT_B, TOKEN_B))

		self.assertEqual(gitlab.token_for(PROJECT_A), TOKEN_A)
		self.assertEqual(gitlab.token_for(PROJECT_B), TOKEN_B)
		self.assertEqual(gitlab.token_for("Erpnext/UA_Prro"), TOKEN_A)

	def test_a_project_without_a_row_gets_no_token(self):
		configure_gitlab((PROJECT_A, TOKEN_A))

		with self.assertRaises(gitlab.GitLabError) as raised:
			gitlab.token_for(PROJECT_B)
		self.assertEqual(str(raised.exception), f"No GitLab token is configured for project {PROJECT_B}")

	def test_an_expired_token_is_refused(self):
		configure_gitlab((PROJECT_A, TOKEN_A), expires_in_days=-1)

		with self.assertRaises(gitlab.GitLabError) as raised:
			gitlab.token_for(PROJECT_A)
		self.assertEqual(str(raised.exception), f"The GitLab token of project {PROJECT_A} has expired")

	def test_settings_validation_refuses_duplicate_projects_and_missing_expiry(self):
		def row(path, expires_on):
			return frappe._dict(idx=1, project_path=path, expires_on=expires_on)

		with self.assertRaises(frappe.ValidationError) as raised:
			gitlab.validate_token_rows([row(PROJECT_A, nowdate()), row(PROJECT_A.upper(), nowdate())])
		self.assertEqual(str(raised.exception), f"GitLab project {PROJECT_A} is listed twice")

		with self.assertRaises(frappe.ValidationError) as raised:
			gitlab.validate_token_rows([row(PROJECT_A, None)])
		self.assertEqual(
			str(raised.exception), f"The token of GitLab project {PROJECT_A} needs an expiry date"
		)


class TestGitLabRedaction(FrappeTestCase):
	def setUp(self):
		super().setUp()
		configure_gitlab((PROJECT_A, TOKEN_A), (PROJECT_B, TOKEN_B))

	def tearDown(self):
		frappe.db.rollback()

	def test_redact_removes_every_configured_token_value(self):
		text = f"a {TOKEN_A} b {TOKEN_B} c {TOKEN_A}"
		self.assertEqual(gitlab.redact(text), "a [REDACTED] b [REDACTED] c [REDACTED]")

	def test_redact_removes_token_shaped_strings_that_are_not_configured(self):
		self.assertEqual(gitlab.redact("x glpat-abcdefghij0123456789 y"), "x [REDACTED] y")

	def test_redact_removes_auth_headers_and_url_credentials(self):
		text = "PRIVATE-TOKEN: hunter2hunter2 and https://oauth2:hunter2hunter2@git.example.test/a/b"
		redacted = gitlab.redact(text)
		self.assertNotIn("hunter2", redacted)
		self.assertIn("PRIVATE-TOKEN: [REDACTED]", redacted)
		self.assertIn("https://[REDACTED]@git.example.test/a/b", redacted)

	def test_redact_accepts_empty_input(self):
		self.assertEqual(gitlab.redact(None), "")
		self.assertEqual(gitlab.redact(""), "")


class TestGitLabApi(FrappeTestCase):
	def setUp(self):
		super().setUp()
		configure_gitlab((PROJECT_A, TOKEN_A), (PROJECT_B, TOKEN_B))

	def tearDown(self):
		frappe.db.rollback()

	def test_branch_call_uses_the_encoded_project_path_the_token_header_and_no_redirects(self):
		with patch("press.utils.gitlab.requests.get", return_value=fake_response(body={})) as get:
			gitlab.branch(PROJECT_A, "version-15")

		get.assert_called_once()
		self.assertEqual(
			get.call_args.args[0],
			f"{GITLAB_URL}/api/v4/projects/erpnext%2Fua_prro/repository/branches/version-15",
		)
		self.assertEqual(get.call_args.kwargs["headers"], {"PRIVATE-TOKEN": TOKEN_A})
		self.assertIs(get.call_args.kwargs["allow_redirects"], False)
		self.assertEqual(get.call_args.kwargs["timeout"], gitlab.API_TIMEOUT_SECONDS)

	def test_two_projects_at_once_each_send_only_their_own_token(self):
		with patch("press.utils.gitlab.requests.get", return_value=fake_response(body={})) as get:
			gitlab.branch(PROJECT_A, "main")
			gitlab.branch(PROJECT_B, "main")
			gitlab.branch(PROJECT_A, "main")

		sent = [call.kwargs["headers"]["PRIVATE-TOKEN"] for call in get.call_args_list]
		self.assertEqual(sent, [TOKEN_A, TOKEN_B, TOKEN_A])
		self.assertIn("erpnext%2Fua_signer", get.call_args_list[1].args[0])

	def test_a_redirect_is_not_followed_and_is_reported(self):
		redirect = fake_response(302, headers={"Location": "https://evil.test/steal"})
		with (
			patch("press.utils.gitlab.requests.get", return_value=redirect) as get,
			self.assertRaises(gitlab.GitLabHTTPError) as raised,
		):
			gitlab.branch(PROJECT_A, "main")

		self.assertEqual(get.call_count, 1)
		self.assertEqual(raised.exception.status_code, 302)
		self.assertEqual(str(raised.exception), "GitLab answered with a redirect; redirects are not followed")

	def test_error_answers_that_echo_the_token_are_redacted(self):
		echo = f"401 Unauthorized PRIVATE-TOKEN: {TOKEN_A} token {TOKEN_B}"
		for status in (401, 404):
			with self.subTest(status=status):
				with (
					patch("press.utils.gitlab.requests.get", return_value=fake_response(status, text=echo)),
					self.assertRaises(gitlab.GitLabHTTPError) as raised,
				):
					gitlab.branch(PROJECT_A, "main")
				self.assertEqual(raised.exception.status_code, status)
				self.assertNotIn(TOKEN_A, str(raised.exception))
				self.assertNotIn(TOKEN_B, str(raised.exception))

	def test_a_transport_error_names_only_the_exception_type(self):
		error = requests.ConnectionError(f"failed for {TOKEN_A}")
		with (
			patch("press.utils.gitlab.requests.get", side_effect=error),
			self.assertRaises(gitlab.GitLabError) as raised,
		):
			gitlab.branch(PROJECT_A, "main")
		self.assertEqual(str(raised.exception), "GitLab request failed: ConnectionError")

	def test_a_body_that_is_not_json_is_reported(self):
		with (
			patch("press.utils.gitlab.requests.get", return_value=fake_response(text="<html>login</html>")),
			self.assertRaises(gitlab.GitLabError) as raised,
		):
			gitlab.branch(PROJECT_A, "main")
		self.assertEqual(str(raised.exception), "GitLab answered with a body that is not JSON")

	def test_no_call_is_made_for_a_project_without_a_token(self):
		with patch("press.utils.gitlab.requests.get") as get, self.assertRaises(gitlab.GitLabError):
			gitlab.branch("erpnext/other", "main")
		get.assert_not_called()

	def test_commit_call_path_and_hash_check(self):
		with patch("press.utils.gitlab.requests.get", return_value=fake_response(body={})) as get:
			gitlab.commit(PROJECT_A, "a" * 40)
		self.assertTrue(get.call_args.args[0].endswith(f"/repository/commits/{'a' * 40}"))

		with patch("press.utils.gitlab.requests.get") as get, self.assertRaises(gitlab.GitLabError) as raised:
			gitlab.commit(PROJECT_A, "main; rm -rf /")
		self.assertEqual(str(raised.exception), "Invalid commit hash")
		get.assert_not_called()

	def test_raw_file_call_encodes_the_file_path_and_passes_the_ref(self):
		with patch("press.utils.gitlab.requests.get", return_value=fake_response(text="x = 1")) as get:
			content = gitlab.raw_file(PROJECT_A, "ua_prro/hooks.py", "main")

		self.assertEqual(content, "x = 1")
		self.assertTrue(get.call_args.args[0].endswith("/repository/files/ua_prro%2Fhooks.py/raw"))
		self.assertEqual(get.call_args.kwargs["params"], {"ref": "main"})

	def test_branches_follow_pages_and_put_version_branches_first(self):
		first = [{"name": f"feature-{index:03}"} for index in range(gitlab.PAGE_SIZE)]
		second = [{"name": "version-15"}, {"name": "main"}]
		with patch(
			"press.utils.gitlab.requests.get",
			side_effect=[fake_response(body=first), fake_response(body=second)],
		) as get:
			found = gitlab.branches(PROJECT_A)

		self.assertEqual(get.call_count, 2)
		self.assertEqual(get.call_args_list[1].kwargs["params"], {"per_page": gitlab.PAGE_SIZE, "page": 2})
		self.assertEqual(len(found), gitlab.PAGE_SIZE + 2)
		self.assertEqual(found[0], {"name": "version-15"})

	def test_branch_and_commit_answers_are_mapped_to_the_github_shape(self):
		info = {
			"id": "abc123",
			"message": "Fix it",
			"author_name": "Dev",
			"authored_date": "2026-11-10T12:00:00.000+02:00",
		}
		self.assertEqual(
			gitlab.to_github_shape({"name": "main", "commit": info}, by_commit=False),
			{
				"name": "main",
				"commit": {
					"sha": "abc123",
					"commit": {
						"message": "Fix it",
						"author": {"name": "Dev", "date": "2026-11-10T12:00:00.000+02:00"},
					},
				},
			},
		)
		self.assertEqual(
			gitlab.to_github_shape(info, by_commit=True),
			{
				"commit": {
					"message": "Fix it",
					"author": {"name": "Dev", "date": "2026-11-10T12:00:00.000+02:00"},
				}
			},
		)


class TestGitLabUpdateRefusals(FrappeTestCase):
	def setUp(self):
		super().setUp()
		configure_gitlab((PROJECT_A, TOKEN_A))

	def tearDown(self):
		frappe.db.rollback()

	def test_refuse_gitlab_sources_names_the_source_and_the_action(self):
		source = create_gitlab_app_source()

		with self.assertRaises(frappe.ValidationError) as raised:
			gitlab.refuse_gitlab_sources([source.name], "A patch build")
		self.assertEqual(
			str(raised.exception),
			f"A patch build is not supported for the GitLab source {source.name}. Build a new bench instead.",
		)

	def test_refuse_gitlab_sources_accepts_github_sources_and_empty_lists(self):
		gitlab.refuse_gitlab_sources([], "A patch build")
		gitlab.refuse_gitlab_sources([None], "A patch build")
		self.assertIsNone(gitlab.gitlab_source_among(["SRC-does-not-exist-.001"]))
