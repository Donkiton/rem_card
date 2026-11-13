"""Подписанные PR публичных материалов с проверкой точного SHA и штатного CI."""

from __future__ import annotations

import base64
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


class GitHubAPIError(RuntimeError):
    """Ошибка API без вывода токена или полного тела HTTP-ответа."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ReleasePullRequestError(RuntimeError):
    """Слияние остановлено; созданный PR остаётся для диагностики."""


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com"):
        if not isinstance(token, str):
            raise ValueError("Токен должен быть строкой; пустая строка разрешает только публичное чтение")
        parsed = urllib.parse.urlsplit(api_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("GitHub API должен использовать HTTPS без данных входа в URL")
        if parsed.query or parsed.fragment:
            raise ValueError("Недопустимый базовый URL GitHub API")
        self._token = token.strip()
        self._api_url = api_url.rstrip("/")

    def request(self, method: str, path: str, payload: Any = None) -> Any:
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("Недопустимый метод GitHub API")
        if not self._token and method != "GET":
            raise GitHubAPIError("Для изменения GitHub требуется токен установки GitHub App")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ValueError("Ожидался относительный путь GitHub API")
        if "\r" in path or "\n" in path or "\\" in path:
            raise ValueError("Недопустимый путь GitHub API")
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._api_url + path,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "remcard-public-release-sync",
            },
        )
        if self._token:
            # urllib не переносит этот заголовок при HTTP redirect на другой URL.
            request.add_unredirected_header("Authorization", f"Bearer {self._token}")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            message = ""
            try:
                error = json.loads(exc.read(16_384).decode("utf-8"))
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    detail = error["message"]
                    if self._token:
                        detail = detail.replace(self._token, "[скрыто]")
                    message = ": " + detail[:500]
            except (ValueError, UnicodeError):
                pass
            safe_path = path.split("?")[0]
            if self._token:
                safe_path = safe_path.replace(self._token, "[скрыто]")
            raise GitHubAPIError(f"GitHub API {method} {safe_path}: HTTP {exc.code}{message}", exc.code) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitHubAPIError(f"GitHub API недоступен ({type(exc).__name__}); повторите запуск") from None
        if not raw:
            return None
        if len(raw) > 2_000_000:
            raise GitHubAPIError("Ответ GitHub API превышает допустимый размер")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise GitHubAPIError("GitHub API вернул некорректный JSON") from None
        if path == "/graphql" and isinstance(result, dict) and result.get("errors"):
            raise GitHubAPIError("GitHub GraphQL отклонил создание коммита; проверьте разрешения и SHA")
        return result


_VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
_REQUIRED_CHECKS = {
    "Donkiton/rem_card": "tests",
    "Donkiton/rem_site": "Сборка статического сайта",
}
_GITHUB_ACTIONS_APP_ID = 15368
_MAX_FILE_BYTES = 1_000_000
_MAX_TOTAL_BYTES = 8_000_000
_BAD_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required", "stale", "startup_failure"}
_GOOD_CONCLUSIONS = {"success", "neutral", "skipped"}
_COMMIT_MUTATION = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid } }
}
"""


def _valid_path(path: str) -> bool:
    return (
        isinstance(path, str) and 0 < len(path) < 250
        and all(_PATH_SEGMENT.fullmatch(part) and part not in {".", ".."}
                and part.lower() not in {".git", ".github"} for part in path.split("/"))
    )


def _validate_arguments(repo, branch, version, expected_base_sha, files, allowed_paths,
                        check_timeout_s, poll_interval_s) -> None:
    if repo not in _REQUIRED_CHECKS:
        raise ValueError("Автоматизация разрешена только для rem_card и rem_site")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError("Ожидалась стабильная версия MAJOR.MINOR.PATCH")
    if branch != f"automation/public-release-{version}":
        raise ValueError("Недопустимая ветка автоматического обновления")
    if not isinstance(expected_base_sha, str) or not _SHA.fullmatch(expected_base_sha):
        raise ValueError("Не задан точный SHA исходной main")
    if not isinstance(files, dict) or not isinstance(allowed_paths, (set, frozenset)):
        raise ValueError("Нужны словарь файлов и явный набор разрешённых путей")
    if any(not _valid_path(path) for path in allowed_paths):
        raise ValueError("Некорректный разрешённый путь")
    total = 0
    for path, content in files.items():
        if not _valid_path(path) or path not in allowed_paths:
            raise ValueError(f"Автоматизация не имеет права изменять путь: {path!r}")
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("Разрешены только текстовые UTF-8 файлы")
        size = len(content.encode("utf-8"))
        if size > _MAX_FILE_BYTES:
            raise ValueError(f"Слишком большой публичный файл: {path}")
        total += size
    if total > _MAX_TOTAL_BYTES:
        raise ValueError("Слишком большой набор публичных файлов")
    for value in (check_timeout_s, poll_interval_s):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("Таймаут и интервал проверки должны быть положительными числами")


def _main_sha(client, repo: str, expected: str) -> str:
    data = client.request("GET", f"/repos/{repo}/git/ref/heads/main")
    sha = data.get("object", {}).get("sha") if isinstance(data, dict) else None
    if sha != expected:
        raise ReleasePullRequestError("main изменилась после подготовки файлов; повторите генерацию от новой main")
    return sha


def _get_ref(client, repo: str, branch: str) -> str | None:
    try:
        result = client.request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
    except GitHubAPIError as exc:
        if exc.status == 404:
            return None
        raise
    sha = result.get("object", {}).get("sha") if isinstance(result, dict) else None
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ReleasePullRequestError("Не удалось проверить SHA ветки автоматизации")
    return sha


def _signed_commit(client, repo: str, sha: str) -> None:
    commit = client.request("GET", f"/repos/{repo}/commits/{sha}")
    verification = commit.get("commit", {}).get("verification", {}) if isinstance(commit, dict) else {}
    if verification.get("verified") is not True:
        raise ReleasePullRequestError("GitHub не подтвердил подпись коммита; PR нельзя сливать")


def _file_text(client, repo: str, sha: str, path: str) -> str:
    result = client.request("GET", f"/repos/{repo}/contents/{path}?ref={sha}")
    if not isinstance(result, dict) or result.get("type") != "file" or result.get("encoding") != "base64":
        raise ReleasePullRequestError(f"Не удалось проверить текст файла {path}")
    try:
        return base64.b64decode(result["content"], validate=False).decode("utf-8")
    except (KeyError, ValueError, UnicodeError):
        raise ReleasePullRequestError(f"Некорректное содержимое файла {path}") from None


def _assert_contents(client, repo: str, sha: str, files: dict[str, str]) -> None:
    if any(_file_text(client, repo, sha, path) != value for path, value in files.items()):
        raise ReleasePullRequestError("Содержимое существующей ветки отличается от подготовленных материалов")


def _already_current(client, repo: str, sha: str, files: dict[str, str]) -> bool:
    for path, value in files.items():
        try:
            if _file_text(client, repo, sha, path) != value:
                return False
        except GitHubAPIError as exc:
            if exc.status == 404:
                return False
            raise
    return True


def _assert_pr(pr: dict, repo: str, branch: str, head_sha: str | None = None) -> str:
    head = pr.get("head", {})
    if (pr.get("user", {}).get("type") != "Bot" or pr.get("base", {}).get("ref") != "main"
            or pr.get("base", {}).get("repo", {}).get("full_name") != repo
            or head.get("ref") != branch or head.get("repo", {}).get("full_name") != repo
            or pr.get("draft") is True):
        raise ReleasePullRequestError("Существующий PR не принадлежит ожидаемой автоматизации")
    sha = head.get("sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha) or (head_sha is not None and sha != head_sha):
        raise ReleasePullRequestError("SHA PR изменился; автоматическое слияние остановлено")
    return sha


def _changed_paths(client, repo: str, number: int) -> set[str]:
    result = client.request("GET", f"/repos/{repo}/pulls/{number}/files?per_page=100")
    if not isinstance(result, list) or len(result) >= 100:
        raise ReleasePullRequestError("Не удалось проверить полный набор файлов PR")
    if any(item.get("status") not in {"added", "modified"} for item in result):
        raise ReleasePullRequestError("Автоматический PR не должен удалять или переименовывать файлы")
    paths = {item.get("filename") for item in result}
    if len(paths) != len(result):
        raise ReleasePullRequestError("Некорректный набор файлов PR")
    return paths


def _prepare_pr(client, repo, branch, version, expected_base_sha, files) -> dict:
    owner = repo.split("/")[0]
    existing = client.request(
        "GET", f"/repos/{repo}/pulls?state=all&base=main&head={owner}:{branch}&per_page=100"
    )
    if not isinstance(existing, list) or len(existing) > 1:
        raise ReleasePullRequestError("Найден неоднозначный набор PR для версии")
    if existing:
        pr = existing[0]
        head_sha = _assert_pr(pr, repo, branch)
        if pr.get("state") == "closed":
            if not pr.get("merged_at"):
                raise ReleasePullRequestError("PR этой версии закрыт без слияния; требуется проверка")
            _assert_contents(client, repo, expected_base_sha, files)
            return {"already_merged": True, "pr": pr, "head_sha": head_sha}
        if _changed_paths(client, repo, pr["number"]) != set(files):
            raise ReleasePullRequestError("Существующий PR содержит другой набор файлов")
        _assert_contents(client, repo, head_sha, files)
        _signed_commit(client, repo, head_sha)
        return {"pr": pr, "head_sha": head_sha}

    head_sha = _get_ref(client, repo, branch)
    if head_sha is None:
        client.request("POST", f"/repos/{repo}/git/refs", {
            "ref": f"refs/heads/{branch}", "sha": expected_base_sha,
        })
        head_sha = expected_base_sha
    label = "README и руководства" if repo.endswith("/rem_card") else "сайт"
    title = f"Обновить {label} для Ремкарты {version}"
    if head_sha == expected_base_sha:
        _main_sha(client, repo, expected_base_sha)
        result = client.request("POST", "/graphql", {"query": _COMMIT_MUTATION, "variables": {
            "input": {
                "branch": {"repositoryNameWithOwner": repo, "branchName": branch},
                "expectedHeadOid": expected_base_sha,
                "message": {"headline": title},
                "fileChanges": {"additions": [
                    {"path": path, "contents": base64.b64encode(text.encode("utf-8")).decode("ascii")}
                    for path, text in sorted(files.items())
                ]},
            },
        }})
        try:
            head_sha = result["data"]["createCommitOnBranch"]["commit"]["oid"]
        except (KeyError, TypeError):
            raise ReleasePullRequestError("GitHub не вернул SHA подписанного коммита") from None
        if not isinstance(head_sha, str) or not _SHA.fullmatch(head_sha):
            raise ReleasePullRequestError("GitHub вернул некорректный SHA коммита")
    else:
        comparison = client.request("GET", f"/repos/{repo}/compare/{expected_base_sha}...{head_sha}")
        compared = comparison.get("files", []) if isinstance(comparison, dict) else []
        if (not isinstance(comparison, dict) or comparison.get("status") != "ahead" or len(compared) >= 300
                or {item.get("filename") for item in compared} != set(files)
                or any(item.get("status") not in {"added", "modified"} for item in compared)):
            raise ReleasePullRequestError("Ветка без PR содержит посторонние изменения или устаревшую main")
        _assert_contents(client, repo, head_sha, files)
    _signed_commit(client, repo, head_sha)
    _main_sha(client, repo, expected_base_sha)
    pr = client.request("POST", f"/repos/{repo}/pulls", {
        "title": title, "head": branch, "base": "main",
        "body": (
            f"Автоматическое обновление публичных материалов для опубликованной версии **{version}**.\n\n"
            "Данные получены из опубликованного релиза и его исходного коммита. "
            "Слияние выполняется только после штатных проверок GitHub Actions.\n\n"
            "Изменяемые файлы:\n" + "\n".join(f"- `{path}`" for path in sorted(files))
        ),
    })
    _assert_pr(pr, repo, branch, head_sha)
    return {"pr": pr, "head_sha": head_sha}


def _checks_ready(client, repo, head_sha, branch, number) -> bool:
    checks = client.request("GET", f"/repos/{repo}/commits/{head_sha}/check-runs?filter=latest&per_page=100")
    statuses = client.request("GET", f"/repos/{repo}/commits/{head_sha}/status?per_page=100")
    runs = client.request("GET", f"/repos/{repo}/actions/runs?head_sha={head_sha}&per_page=100")
    if (not isinstance(checks, dict) or not isinstance(checks.get("check_runs"), list)
            or not isinstance(statuses, dict) or not isinstance(statuses.get("statuses"), list)
            or not isinstance(runs, dict) or not isinstance(runs.get("workflow_runs"), list)):
        raise ReleasePullRequestError("Не удалось проверить полный ответ CI")
    if (checks.get("total_count", len(checks["check_runs"])) > len(checks["check_runs"])
            or statuses.get("total_count", len(statuses["statuses"])) > len(statuses["statuses"])
            or runs.get("total_count", len(runs["workflow_runs"])) > len(runs["workflow_runs"])):
        raise ReleasePullRequestError("Слишком много записей CI для однозначной автоматической проверки")
    ready = True
    for status in statuses["statuses"]:
        if status.get("state") in {"failure", "error"}:
            raise ReleasePullRequestError(f"Статус CI завершился ошибкой: {status.get('context', 'без имени')}")
        if status.get("state") != "success":
            ready = False
    pr_runs = {}
    for run in runs["workflow_runs"]:
        pull_requests = run.get("pull_requests", [])
        if (run.get("event") == "pull_request" and run.get("head_sha") == head_sha
                and run.get("head_branch") == branch
                and run.get("head_repository", {}).get("full_name") == repo
                and (not pull_requests or any(pr.get("number") == number for pr in pull_requests))):
            pr_runs[run.get("check_suite_id")] = run
    required_ready = False
    for check in checks["check_runs"]:
        if check.get("head_sha") != head_sha:
            raise ReleasePullRequestError("Проверка CI относится к другому SHA")
        conclusion = check.get("conclusion")
        if conclusion in _BAD_CONCLUSIONS:
            raise ReleasePullRequestError(f"Проверка CI завершилась ошибкой: {check.get('name', 'без имени')}")
        if check.get("status") != "completed" or conclusion not in _GOOD_CONCLUSIONS:
            ready = False
        if check.get("name") != _REQUIRED_CHECKS[repo] or check.get("app", {}).get("id") != _GITHUB_ACTIONS_APP_ID:
            continue
        run = pr_runs.get(check.get("check_suite", {}).get("id"))
        if run and run.get("status") == "completed" and run.get("conclusion") == "success" and conclusion == "success":
            required_ready = True
        elif run and run.get("conclusion") in _BAD_CONCLUSIONS:
            raise ReleasePullRequestError("Обязательный PR workflow завершился ошибкой")
    return ready and required_ready


def create_and_merge_release_pr(
    client: GitHubClient, repo: str, branch: str, version: str,
    expected_base_sha: str, files: dict[str, str], allowed_paths: set[str],
    check_timeout_s: float = 1800, poll_interval_s: float = 20,
    *, _sleep: Callable[[float], None] = time.sleep,
    _monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Создать один проверенный PR и слить его без обхода правил репозитория.

    ``files`` содержит только изменяемые файлы, полностью вычисленные от
    ``expected_base_sha``. Любое движение main/head требует новой генерации.
    При ошибке созданная ветка и PR остаются для диагностики и повторного запуска.
    """
    _validate_arguments(repo, branch, version, expected_base_sha, files, allowed_paths,
                        check_timeout_s, poll_interval_s)
    _main_sha(client, repo, expected_base_sha)
    if not files or _already_current(client, repo, expected_base_sha, files):
        return {"changed": False, "number": None, "url": None,
                "head_sha": expected_base_sha, "merge_sha": expected_base_sha}
    prepared = _prepare_pr(client, repo, branch, version, expected_base_sha, files)
    pr, head_sha = prepared["pr"], prepared["head_sha"]
    number = pr["number"]
    if prepared.get("already_merged"):
        return {"changed": False, "number": number, "url": pr["html_url"],
                "head_sha": head_sha, "merge_sha": expected_base_sha}
    deadline = _monotonic() + check_timeout_s
    while True:
        _main_sha(client, repo, expected_base_sha)
        current = client.request("GET", f"/repos/{repo}/pulls/{number}")
        _assert_pr(current, repo, branch, head_sha)
        if current.get("state") != "open":
            raise ReleasePullRequestError("Состояние PR изменилось; автоматическое слияние остановлено")
        if current.get("base", {}).get("sha") != expected_base_sha:
            raise ReleasePullRequestError("База PR изменилась; повторите генерацию")
        if _changed_paths(client, repo, number) != set(files):
            raise ReleasePullRequestError("Набор файлов PR изменился; автоматическое слияние остановлено")
        checks_ready = _checks_ready(client, repo, head_sha, branch, number)
        if checks_ready and current.get("mergeable") is True and current.get("mergeable_state") == "clean":
            # Повторная сверка непосредственно перед мутацией; merge API также
            # атомарно проверяет head SHA и самостоятельно применяет ruleset.
            _main_sha(client, repo, expected_base_sha)
            latest = client.request("GET", f"/repos/{repo}/pulls/{number}")
            _assert_pr(latest, repo, branch, head_sha)
            if latest.get("state") != "open" or latest.get("base", {}).get("sha") != expected_base_sha:
                raise ReleasePullRequestError("PR изменился перед слиянием")
            result = client.request("PUT", f"/repos/{repo}/pulls/{number}/merge", {
                "sha": head_sha, "merge_method": "merge",
                "commit_title": f"Слить PR №{number}: обновить публичные материалы для {version}",
                "commit_message": "Автоматическое обновление после успешных штатных проверок GitHub Actions.",
            })
            if not isinstance(result, dict) or result.get("merged") is not True or not _SHA.fullmatch(str(result.get("sha", ""))):
                raise ReleasePullRequestError("GitHub не подтвердил успешное слияние PR")
            return {"changed": True, "number": number, "url": pr["html_url"],
                    "head_sha": head_sha, "merge_sha": result["sha"]}
        if current.get("mergeable") is False or current.get("mergeable_state") == "dirty":
            raise ReleasePullRequestError("PR имеет конфликт; автоматическое слияние остановлено")
        remaining = deadline - _monotonic()
        if remaining <= 0:
            raise ReleasePullRequestError(f"Не дождались успешных проверок PR №{number}; PR оставлен открытым")
        _sleep(min(poll_interval_s, remaining))
