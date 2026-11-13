"""Контракты облачного PR-бота без сети, токенов и ожидания реального CI."""

from __future__ import annotations

import base64
import copy
import io
import json
import urllib.error

import pytest

from scripts.public_release_github import (
    GitHubAPIError,
    GitHubClient,
    ReleasePullRequestError,
    create_and_merge_release_pr,
)


BASE = "a" * 40
HEAD = "b" * 40
MERGE = "c" * 40
REPO = "Donkiton/rem_card"
VERSION = "5.1.21"
BRANCH = f"automation/public-release-{VERSION}"
FILES = {"README.md": "Новый выпуск\n", "docs/public_release.json": '{"version":"5.1.21"}\n'}


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeGitHub:
    def __init__(self, repo=REPO):
        self.repo = repo
        self.required = "tests" if repo == REPO else "Сборка статического сайта"
        self.main = BASE
        self.ref = None
        self.existing = []
        self.signature = True
        self.head = HEAD
        self.changed_paths = list(FILES)
        self.main_files = {}
        self.head_files = dict(FILES)
        self.check_status = "completed"
        self.check_conclusion = "success"
        self.run_status = "completed"
        self.run_conclusion = "success"
        self.run_event = "pull_request"
        self.run_branch = BRANCH
        self.run_repo = repo
        self.run_pull_requests = []
        self.check_app = 15368
        self.mergeable = True
        self.mergeable_state = "clean"
        self.pr_head_override = None
        self.poll_main_override = None
        self.pr_file_override = None
        self.calls = []
        self.created = False
        self.create_count = 0
        self.check_reads = 0
        self.on_sleep = None

    def pr(self):
        return {
            "number": 187,
            "html_url": f"https://github.com/{self.repo}/pull/187",
            "state": "open", "draft": False, "user": {"type": "Bot"},
            "head": {"sha": self.pr_head_override or self.head, "ref": BRANCH,
                     "repo": {"full_name": self.repo}},
            "base": {"sha": BASE, "ref": "main", "repo": {"full_name": self.repo}},
            "mergeable": self.mergeable, "mergeable_state": self.mergeable_state,
        }

    def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        root = f"/repos/{self.repo}"
        if path == f"{root}/git/ref/heads/main":
            return {"object": {"sha": self.poll_main_override if self.created and self.poll_main_override else self.main}}
        if path.startswith(f"{root}/contents/"):
            path_name, ref = path[len(f"{root}/contents/"):].split("?ref=")
            contents = self.main_files if ref == BASE else self.head_files
            if path_name not in contents:
                raise GitHubAPIError("Файл не найден", 404)
            return {"type": "file", "encoding": "base64",
                    "content": base64.b64encode(contents[path_name].encode()).decode()}
        if path.startswith(f"{root}/pulls?state=all"):
            return copy.deepcopy(self.existing)
        if path == f"{root}/git/ref/heads/{BRANCH}":
            if self.ref is None:
                raise GitHubAPIError("Ветка не найдена", 404)
            return {"object": {"sha": self.ref}}
        if method == "POST" and path == f"{root}/git/refs":
            assert payload == {"ref": f"refs/heads/{BRANCH}", "sha": BASE}
            self.ref = BASE
            return {}
        if method == "POST" and path == "/graphql":
            self.create_count += 1
            self.ref = HEAD
            assert payload["variables"]["input"]["expectedHeadOid"] == BASE
            assert payload["variables"]["input"]["message"]["headline"].startswith("Обновить ")
            additions = payload["variables"]["input"]["fileChanges"]["additions"]
            assert {item["path"]: base64.b64decode(item["contents"]).decode() for item in additions} == FILES
            return {"data": {"createCommitOnBranch": {"commit": {"oid": HEAD}}}}
        if path == f"{root}/commits/{HEAD}":
            return {"commit": {"verification": {"verified": self.signature}}}
        if path == f"{root}/compare/{BASE}...{HEAD}":
            return {"status": "ahead", "files": [{"filename": p, "status": "modified"} for p in self.changed_paths]}
        if method == "POST" and path == f"{root}/pulls":
            self.created = True
            assert payload["base"] == "main"
            assert payload["head"] == BRANCH
            return self.pr()
        if method == "GET" and path == f"{root}/pulls/187":
            return self.pr()
        if path == f"{root}/pulls/187/files?per_page=100":
            paths = self.pr_file_override or self.changed_paths
            return [{"filename": p, "status": "modified"} for p in paths]
        if path == f"{root}/commits/{HEAD}/check-runs?filter=latest&per_page=100":
            self.check_reads += 1
            return {"total_count": 1, "check_runs": [{
                "id": 123, "name": self.required, "head_sha": HEAD,
                "status": self.check_status, "conclusion": self.check_conclusion,
                "app": {"id": self.check_app}, "check_suite": {"id": 456}, "pull_requests": [],
            }]}
        if path == f"{root}/commits/{HEAD}/status?per_page=100":
            return {"total_count": 0, "state": "pending", "statuses": []}
        if path == f"{root}/actions/runs?head_sha={HEAD}&per_page=100":
            return {"total_count": 1, "workflow_runs": [{
                "event": self.run_event, "head_sha": HEAD, "head_branch": self.run_branch,
                "head_repository": {"full_name": self.run_repo}, "check_suite_id": 456,
                "status": self.run_status, "conclusion": self.run_conclusion,
                "pull_requests": self.run_pull_requests,
            }]}
        if method == "PUT" and path == f"{root}/pulls/187/merge":
            assert payload["sha"] == HEAD
            assert payload["merge_method"] == "merge"
            assert payload["commit_title"].startswith("Слить PR №187:")
            return {"merged": True, "sha": MERGE}
        raise AssertionError(f"Неожиданный API-вызов: {method} {path}")

    def mutations(self):
        return [call for call in self.calls if call[0] != "GET"]

    def merges(self):
        return [call for call in self.calls if call[0] == "PUT"]


def run(fake, **overrides):
    clock = Clock()
    arguments = {
        "client": fake, "repo": fake.repo, "branch": BRANCH, "version": VERSION,
        "expected_base_sha": BASE, "files": dict(FILES), "allowed_paths": set(FILES),
        "check_timeout_s": 2, "poll_interval_s": 1,
        "_sleep": clock.sleep, "_monotonic": clock.monotonic,
    }
    arguments.update(overrides)
    return create_and_merge_release_pr(**arguments)


@pytest.mark.parametrize("repo", [REPO, "Donkiton/rem_site"])
def test_creates_signed_server_commit_waits_for_pr_ci_and_merges_exact_sha(repo):
    fake = FakeGitHub(repo)
    result = run(fake)
    assert result == {"changed": True, "number": 187, "url": f"https://github.com/{repo}/pull/187",
                      "head_sha": HEAD, "merge_sha": MERGE}
    assert fake.create_count == 1
    assert len(fake.merges()) == 1
    assert all("admin" not in path for _, path, _ in fake.calls)


def test_empty_change_set_is_read_only():
    fake = FakeGitHub()
    result = run(fake, files={})
    assert result["changed"] is False
    assert result["merge_sha"] == BASE
    assert not fake.mutations()


def test_already_current_materials_are_read_only_before_looking_up_old_pr():
    fake = FakeGitHub()
    fake.main_files = dict(FILES)
    result = run(fake)
    assert result["changed"] is False
    assert not fake.mutations()
    assert not any("/pulls" in path for _, path, _ in fake.calls)


@pytest.mark.parametrize("path", ["../README.md", "/README.md", "C:/README.md", "a\\b", ".github/workflows/x.yml", ".git/config", "docs//x", "docs/./x", "not-allowed.txt"])
def test_unsafe_or_unlisted_paths_are_rejected_before_network(path):
    fake = FakeGitHub()
    with pytest.raises(ValueError):
        run(fake, files={path: "x"})
    assert not fake.calls


@pytest.mark.parametrize("overrides", [
    {"version": "5.1.21-beta.1"}, {"version": "05.1.21"},
    {"branch": "main"}, {"repo": "Other/project"}, {"expected_base_sha": "main"},
    {"files": {"README.md": "x\0y"}}, {"files": {"README.md": "x" * 1_000_001}},
    {"check_timeout_s": 0}, {"poll_interval_s": float("nan")},
])
def test_invalid_input_cannot_create_branches_or_pull_requests(overrides):
    fake = FakeGitHub()
    with pytest.raises(ValueError):
        run(fake, **overrides)
    assert not fake.calls


def test_unsigned_api_commit_is_never_used_for_a_pr():
    fake = FakeGitHub()
    fake.signature = False
    with pytest.raises(ReleasePullRequestError, match="подпись"):
        run(fake)
    assert not any(path.endswith("/pulls") and method == "POST" for method, path, _ in fake.calls)
    assert not fake.merges()


def test_main_moving_before_preparation_is_read_only():
    fake = FakeGitHub()
    fake.main = "d" * 40
    with pytest.raises(ReleasePullRequestError, match="main изменилась"):
        run(fake)
    assert not fake.mutations()


def test_main_moving_while_ci_runs_never_merges():
    fake = FakeGitHub()
    fake.poll_main_override = "d" * 40
    with pytest.raises(ReleasePullRequestError, match="main изменилась"):
        run(fake)
    assert not fake.merges()


def test_changed_pr_head_is_never_merged():
    fake = FakeGitHub()
    original = fake.request

    def request(method, path, payload=None):
        result = original(method, path, payload)
        if method == "GET" and path.endswith("/pulls/187"):
            result["head"]["sha"] = "d" * 40
        return result

    fake.request = request
    with pytest.raises(ReleasePullRequestError, match="SHA PR изменился"):
        run(fake)
    assert not fake.merges()


def test_head_changing_after_successful_checks_is_still_not_merged():
    fake = FakeGitHub()
    original = fake.request
    reads = 0

    def request(method, path, payload=None):
        nonlocal reads
        result = original(method, path, payload)
        if method == "GET" and path.endswith("/pulls/187"):
            reads += 1
            if reads == 2:
                result["head"]["sha"] = "d" * 40
        return result

    fake.request = request
    with pytest.raises(ReleasePullRequestError, match="SHA PR изменился"):
        run(fake)
    assert fake.check_reads == 1
    assert not fake.merges()


def test_main_changing_after_successful_checks_is_still_not_merged():
    fake = FakeGitHub()
    original = fake.request

    def request(method, path, payload=None):
        result = original(method, path, payload)
        if method == "GET" and path.endswith("/git/ref/heads/main") and fake.check_reads:
            result["object"]["sha"] = "d" * 40
        return result

    fake.request = request
    with pytest.raises(ReleasePullRequestError, match="main изменилась"):
        run(fake)
    assert fake.check_reads == 1
    assert not fake.merges()


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required", "stale", "startup_failure"])
def test_failed_or_unapproved_ci_is_never_merged(conclusion):
    fake = FakeGitHub()
    fake.check_conclusion = conclusion
    with pytest.raises(ReleasePullRequestError, match="CI завершилась ошибкой"):
        run(fake)
    assert not fake.merges()


def test_pending_ci_times_out_without_merging():
    fake = FakeGitHub()
    fake.check_status = "queued"
    fake.check_conclusion = None
    with pytest.raises(ReleasePullRequestError, match="Не дождались"):
        run(fake)
    assert fake.check_reads >= 2
    assert not fake.merges()


def test_pending_ci_is_polled_until_the_real_pr_workflow_passes():
    fake = FakeGitHub()
    fake.check_status = "in_progress"
    fake.check_conclusion = None
    clock = Clock()

    def finish(seconds):
        clock.sleep(seconds)
        fake.check_status = "completed"
        fake.check_conclusion = "success"

    result = run(fake, _sleep=finish, _monotonic=clock.monotonic)
    assert result["changed"] is True
    assert fake.check_reads == 2
    assert len(fake.merges()) == 1


def test_failed_combined_commit_status_cannot_be_hidden_by_successful_check_run():
    fake = FakeGitHub()
    original = fake.request

    def request(method, path, payload=None):
        if path.endswith(f"/commits/{HEAD}/status?per_page=100"):
            fake.calls.append((method, path, payload))
            return {"total_count": 1, "state": "failure", "statuses": [{"state": "failure", "context": "extra-check"}]}
        return original(method, path, payload)

    fake.request = request
    with pytest.raises(ReleasePullRequestError, match="Статус CI завершился ошибкой"):
        run(fake)
    assert not fake.merges()


@pytest.mark.parametrize("field,value", [
    ("run_event", "workflow_dispatch"), ("run_branch", "some-other-branch"),
    ("run_repo", "outsider/rem_card"), ("run_pull_requests", [{"number": 188}]),
    ("check_app", 999), ("check_conclusion", "skipped"),
])
def test_required_check_needs_real_pr_workflow_provenance(field, value):
    fake = FakeGitHub()
    setattr(fake, field, value)
    with pytest.raises(ReleasePullRequestError, match="Не дождались"):
        run(fake)
    assert not fake.merges()


def test_successful_check_does_not_merge_while_its_workflow_still_runs():
    fake = FakeGitHub()
    fake.run_status = "in_progress"
    fake.run_conclusion = None
    with pytest.raises(ReleasePullRequestError, match="Не дождались"):
        run(fake)
    assert not fake.merges()


def test_existing_exact_bot_pr_is_reused_without_new_commit_or_pr():
    fake = FakeGitHub()
    fake.existing = [fake.pr()]
    result = run(fake)
    assert result["changed"] is True
    assert fake.create_count == 0
    assert len(fake.merges()) == 1
    assert not any(method == "POST" for method, _, _ in fake.calls)


def test_existing_pr_with_different_content_is_not_overwritten():
    fake = FakeGitHub()
    fake.existing = [fake.pr()]
    fake.head_files["README.md"] = "Чужая правка\n"
    with pytest.raises(ReleasePullRequestError, match="Содержимое существующей ветки"):
        run(fake)
    assert not fake.mutations()


def test_existing_user_pull_request_is_not_reused():
    fake = FakeGitHub()
    pr = fake.pr()
    pr["user"]["type"] = "User"
    fake.existing = [pr]
    with pytest.raises(ReleasePullRequestError, match="не принадлежит"):
        run(fake)
    assert not fake.mutations()


def test_existing_pr_with_extra_paths_is_not_merged():
    fake = FakeGitHub()
    fake.existing = [fake.pr()]
    fake.changed_paths.append("app/main.py")
    with pytest.raises(ReleasePullRequestError, match="другой набор файлов"):
        run(fake)
    assert not fake.mutations()


def test_interrupted_branch_without_pr_can_be_reused_only_with_exact_contents():
    fake = FakeGitHub()
    fake.ref = HEAD
    result = run(fake)
    assert result["changed"] is True
    assert fake.create_count == 0
    assert len(fake.merges()) == 1


def test_extra_file_added_after_pr_creation_stops_merge():
    fake = FakeGitHub()
    fake.pr_file_override = [*FILES, "app/main.py"]
    with pytest.raises(ReleasePullRequestError, match="Набор файлов PR изменился"):
        run(fake)
    assert not fake.merges()


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, length):
        return self.content[:length]


def test_public_read_does_not_send_empty_authorization_header(monkeypatch):
    def open_request(request, timeout):
        assert request.get_header("Authorization") is None
        assert timeout == 60
        return FakeResponse(b'{"ok":true}')

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    assert GitHubClient("").request("GET", "/repos/Donkiton/rem_card") == {"ok": True}
    with pytest.raises(GitHubAPIError, match="токен установки"):
        GitHubClient("").request("POST", "/graphql", {})


def test_token_is_not_forwarded_as_redirectable_header(monkeypatch):
    def open_request(request, timeout):
        assert "Authorization" not in request.headers
        assert request.unredirected_hdrs["Authorization"] == "Bearer temporary-app-token"
        return FakeResponse(b'{}')

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    assert GitHubClient("temporary-app-token").request("GET", "/user") == {}


def test_http_error_redacts_token_and_does_not_dump_response(monkeypatch):
    token = "temporary-app-token"
    body = json.dumps({"message": f"Rejected {token}", "private": "do-not-print"}).encode()

    def open_request(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://api.github.com", 403, "Forbidden", {}, io.BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    with pytest.raises(GitHubAPIError) as caught:
        GitHubClient(token).request("GET", "/user")
    assert caught.value.status == 403
    assert token not in str(caught.value)
    assert "do-not-print" not in str(caught.value)
    assert "[скрыто]" in str(caught.value)


def test_api_responses_are_size_limited(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse(b" " * 2_000_001))
    with pytest.raises(GitHubAPIError, match="размер"):
        GitHubClient("").request("GET", "/repos/Donkiton/rem_card")


@pytest.mark.parametrize("api_url", ["http://api.github.com", "https://token@api.github.com", "https://api.github.com?token=x"])
def test_invalid_api_origin_is_rejected(api_url):
    with pytest.raises(ValueError):
        GitHubClient("temporary-app-token", api_url)
