"""Upload generated chart pages using GitHub's Contents API."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

_API_ROOT = "https://api.github.com"
_REQUEST_TIMEOUT_SECONDS = 20
_CHART_DIRECTORY = "charts"
_STOCK_ID_PATTERN = re.compile(r"^\d{4,6}$")
_DAILY_JSON_PATH_PATTERN = re.compile(r"^data/daily/(\d{4}-\d{2}-\d{2})\.json$")
_RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)
_RETRY_DELAYS_SECONDS = (2.0, 4.0, 8.0)
_CONFLICT_RETRY_DELAYS_SECONDS = (1.5, 3.0, 5.0)


class GitHubUploadError(RuntimeError):
    """Raised when GitHub cannot accept or publish a chart page."""


def _request_with_retry(method, url: str, **kwargs) -> requests.Response:
    """Execute a requests call with exponential backoff on 429/5xx or transient network errors."""
    last_exc: Exception | None = None
    for delay in (*_RETRY_DELAYS_SECONDS, None):
        try:
            response = method(url, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            if delay is None:
                raise GitHubUploadError(f"GitHub API 網路連線失敗：{exc}") from exc
            logger.warning("GitHub API network error (%s), retrying in %.1fs...", exc, delay)
            time.sleep(delay)
            continue

        if response.status_code in _RETRYABLE_STATUS_CODES and delay is not None:
            logger.warning(
                "GitHub API HTTP %s for %s, retrying in %.1fs...",
                response.status_code,
                url,
                delay,
            )
            time.sleep(delay)
            continue
        return response

    raise GitHubUploadError(f"GitHub API 呼叫失敗：{last_exc}")  # pragma: no cover


def upload_to_github(
    file_path: str,
    stock_id: str,
    *,
    filename_suffix: str = "",
) -> str:
    """Create or update a chart file in the configured GitHub Pages repository."""
    token = os.getenv("GITHUB_TOKEN", "").strip()
    repository = os.getenv("GITHUB_REPO", "").strip()
    branch = os.getenv("GITHUB_BRANCH", "gh-pages").strip() or "gh-pages"
    if not token:
        raise GitHubUploadError("GITHUB_TOKEN 尚未設定。")

    repository_parts = repository.split("/")
    if len(repository_parts) != 2 or not all(repository_parts):
        raise GitHubUploadError("GITHUB_REPO 格式錯誤，應為 username/repo_name。")

    normalized_stock_id = stock_id.strip()
    if not _STOCK_ID_PATTERN.fullmatch(normalized_stock_id):
        raise GitHubUploadError("股票代號必須是 4 至 6 位數字。")

    chart_path = Path(file_path)
    expected_filename = f"{normalized_stock_id}{filename_suffix}.html"
    if not chart_path.is_file() or chart_path.name != expected_filename:
        raise GitHubUploadError(f"找不到有效的圖表檔案：{expected_filename}")

    owner, repo = repository_parts
    remote_path = f"{_CHART_DIRECTORY}/{expected_filename}"
    encoded_path = quote(remote_path, safe="/")
    contents_url = f"{_API_ROOT}/repos/{owner}/{repo}/contents/{encoded_path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "discord-stock-chart-bot",
    }

    current_file = _request_with_retry(
        requests.get,
        contents_url,
        headers=headers,
        params={"ref": branch},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    file_sha: str | None = None
    if current_file.status_code == requests.codes.ok:
        file_sha = current_file.json().get("sha")
        if not file_sha:
            raise GitHubUploadError("GitHub 未回傳既有檔案的 SHA，無法更新檔案。")
    elif current_file.status_code != requests.codes.not_found:
        raise GitHubUploadError(
            f"無法查詢 GitHub 儲存庫（HTTP {current_file.status_code}）："
            f"{current_file.text[:500]}"
        )

    payload: dict[str, str] = {
        "message": f"Update stock chart {normalized_stock_id}",
        "content": base64.b64encode(chart_path.read_bytes()).decode("ascii"),
        "branch": branch,
    }
    if file_sha:
        payload["sha"] = file_sha

    response = _request_with_retry(
        requests.put,
        contents_url,
        headers=headers,
        json=payload,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code not in (requests.codes.created, requests.codes.ok):
        raise GitHubUploadError(
            f"GitHub 上傳失敗（HTTP {response.status_code}）：{response.text[:500]}"
        )

    pages_path = f"/{repo}/{remote_path}"
    if repo.lower() == f"{owner}.github.io".lower():
        pages_path = f"/{remote_path}"
    return f"https://{owner}.github.io{pages_path}"


def get_github_pages_base_url(repository: str | None = None) -> str | None:
    """回傳 GitHub Pages 戰情總部根網址（不含尾端斜線）。"""
    repo_raw = (repository if repository is not None else os.getenv("GITHUB_REPO", "")).strip()
    parts = repo_raw.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    owner, repo = parts
    if repo.lower() == f"{owner}.github.io".lower():
        return f"https://{owner}.github.io"
    return f"https://{owner}.github.io/{repo}"


def _compute_git_blob_sha(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()  # noqa: S324 - Git blob SHA-1 spec


def _normalize_remote_path(path: str) -> str:
    return path.strip().lstrip("/").replace("\\", "/")


def sync_portal_batch(
    files: Mapping[str, bytes | str],
    *,
    delete_paths: Sequence[str] = (),
    keep_daily_dates: set[str] | None = None,
    commit_message: str = "Update quant portal data",
    max_conflict_retries: int = 3,
    token: str | None = None,
    repository: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    """透過 GitHub Git Trees API 以單次 Commit 批次上傳戰情總部資料並清理過期每日 JSON。

    安全鐵律（SASD v6.2.0 §5.1~5.3）：
    - 嚴禁刪除 `charts/` 下任何個股戰情室 HTML（確保舊 Discord 連結與 Sheet 快取網址永久有效）。
    - 自動比對遠端 Blob SHA，跳過內容未變動的檔案（如未改版的 `index.html` 或未更新之歷史 JSON）。
    - 更新 `refs/heads/{branch}` 若遇 HTTP 422（併發 non-fast-forward 衝突），自動重新拉取最新 ref 重建 commit，最多重試 `max_conflict_retries` 次。
    """
    resolved_token = (token if token is not None else os.getenv("GITHUB_TOKEN", "")).strip()
    resolved_repo = (repository if repository is not None else os.getenv("GITHUB_REPO", "")).strip()
    resolved_branch = (
        branch if branch is not None else os.getenv("GITHUB_BRANCH", "gh-pages")
    ).strip() or "gh-pages"

    if not resolved_token:
        raise GitHubUploadError("GITHUB_TOKEN 尚未設定。")
    repo_parts = resolved_repo.split("/")
    if len(repo_parts) != 2 or not all(repo_parts):
        raise GitHubUploadError("GITHUB_REPO 格式錯誤，應為 username/repo_name。")

    owner, repo = repo_parts
    base_api = f"{_API_ROOT}/repos/{owner}/{repo}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {resolved_token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "discord-quant-portal-publisher",
    }

    normalized_files: dict[str, bytes] = {}
    for raw_path, raw_content in files.items():
        norm_path = _normalize_remote_path(raw_path)
        if not norm_path:
            continue
        data = raw_content.encode("utf-8") if isinstance(raw_content, str) else bytes(raw_content)
        normalized_files[norm_path] = data

    explicit_deletes = {
        _normalize_remote_path(p)
        for p in delete_paths
        if _normalize_remote_path(p) and not _normalize_remote_path(p).startswith(f"{_CHART_DIRECTORY}/")
    }

    portal_url = get_github_pages_base_url(resolved_repo)
    attempts = max(1, int(max_conflict_retries))

    for attempt in range(1, attempts + 1):
        # 1. 取得目標分支 HEAD commit SHA
        ref_url = f"{base_api}/git/ref/heads/{quote(resolved_branch, safe='/')}"
        ref_resp = _request_with_retry(
            requests.get, ref_url, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS
        )
        if ref_resp.status_code != requests.codes.ok:
            raise GitHubUploadError(
                f"無法讀取 GitHub 分支 {resolved_branch}（HTTP {ref_resp.status_code}）："
                f"{ref_resp.text[:500]}"
            )
        head_commit_sha = str((ref_resp.json().get("object") or {}).get("sha") or "").strip()
        if not head_commit_sha:
            raise GitHubUploadError("GitHub 未回傳分支 HEAD Commit SHA。")

        # 2. 取得 HEAD commit 對應之 base_tree SHA
        commit_resp = _request_with_retry(
            requests.get,
            f"{base_api}/git/commits/{head_commit_sha}",
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if commit_resp.status_code != requests.codes.ok:
            raise GitHubUploadError(
                f"無法讀取 Commit {head_commit_sha}（HTTP {commit_resp.status_code}）："
                f"{commit_resp.text[:500]}"
            )
        base_tree_sha = str((commit_resp.json().get("tree") or {}).get("sha") or "").strip()
        if not base_tree_sha:
            raise GitHubUploadError("GitHub 未回傳 base_tree SHA。")

        # 3. 遞迴讀取遠端檔案樹，用於比對未變更檔案與找出超過 30 交易日之舊每日 JSON
        tree_resp = _request_with_retry(
            requests.get,
            f"{base_api}/git/trees/{base_tree_sha}",
            headers=headers,
            params={"recursive": "1"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        remote_blobs: dict[str, str] = {}
        if tree_resp.status_code == requests.codes.ok:
            for entry in tree_resp.json().get("tree") or []:
                if entry.get("type") == "blob" and entry.get("path") and entry.get("sha"):
                    remote_blobs[str(entry["path"])] = str(entry["sha"])
        else:
            logger.warning(
                "無法讀取遠端樹狀索引（HTTP %s），將略過增量比對與舊檔清理。",
                tree_resp.status_code,
            )

        # 計算要刪除的路徑（僅限顯式指定且非 charts/，以及 data/daily/ 下不在保留清單之舊 JSON）
        to_delete: list[str] = []
        delete_set = set(explicit_deletes)
        if keep_daily_dates is not None:
            for remote_path in remote_blobs:
                if remote_path.startswith(f"{_CHART_DIRECTORY}/"):
                    continue
                m = _DAILY_JSON_PATH_PATTERN.fullmatch(remote_path)
                if m and m.group(1) not in keep_daily_dates and remote_path not in normalized_files:
                    delete_set.add(remote_path)
        for path in sorted(delete_set):
            if path.startswith(f"{_CHART_DIRECTORY}/"):
                continue
            if path in remote_blobs and path not in normalized_files:
                to_delete.append(path)

        # 4. 建立有變更檔案的 Git Blob（未變更者直接跳過）
        tree_entries: list[dict[str, Any]] = []
        uploaded_paths: list[str] = []
        skipped_paths: list[str] = []

        for remote_path, data in sorted(normalized_files.items()):
            local_sha = _compute_git_blob_sha(data)
            if remote_blobs.get(remote_path) == local_sha:
                skipped_paths.append(remote_path)
                continue
            blob_resp = _request_with_retry(
                requests.post,
                f"{base_api}/git/blobs",
                headers=headers,
                json={
                    "content": base64.b64encode(data).decode("ascii"),
                    "encoding": "base64",
                },
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            if blob_resp.status_code not in (requests.codes.created, requests.codes.ok):
                raise GitHubUploadError(
                    f"建立 Git Blob 失敗 {remote_path}（HTTP {blob_resp.status_code}）："
                    f"{blob_resp.text[:500]}"
                )
            blob_sha = str(blob_resp.json().get("sha") or "").strip()
            if not blob_sha:
                raise GitHubUploadError(f"GitHub 未回傳 {remote_path} 的 Blob SHA。")
            tree_entries.append(
                {
                    "path": remote_path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha,
                }
            )
            uploaded_paths.append(remote_path)

        for del_path in to_delete:
            tree_entries.append(
                {
                    "path": del_path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": None,
                }
            )

        if not tree_entries:
            return {
                "commit_sha": head_commit_sha,
                "uploaded": [],
                "deleted": [],
                "skipped_unchanged": skipped_paths,
                "portal_url": portal_url,
                "no_changes": True,
            }

        # 5. 建立新 Tree
        new_tree_resp = _request_with_retry(
            requests.post,
            f"{base_api}/git/trees",
            headers=headers,
            json={"base_tree": base_tree_sha, "tree": tree_entries},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if new_tree_resp.status_code not in (requests.codes.created, requests.codes.ok):
            raise GitHubUploadError(
                f"建立 Git Tree 失敗（HTTP {new_tree_resp.status_code}）：{new_tree_resp.text[:500]}"
            )
        new_tree_sha = str(new_tree_resp.json().get("sha") or "").strip()
        if not new_tree_sha:
            raise GitHubUploadError("GitHub 未回傳新 Tree SHA。")

        # 6. 建立單一 Commit
        new_commit_resp = _request_with_retry(
            requests.post,
            f"{base_api}/git/commits",
            headers=headers,
            json={
                "message": commit_message,
                "tree": new_tree_sha,
                "parents": [head_commit_sha],
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if new_commit_resp.status_code not in (requests.codes.created, requests.codes.ok):
            raise GitHubUploadError(
                f"建立 Git Commit 失敗（HTTP {new_commit_resp.status_code}）："
                f"{new_commit_resp.text[:500]}"
            )
        new_commit_sha = str(new_commit_resp.json().get("sha") or "").strip()
        if not new_commit_sha:
            raise GitHubUploadError("GitHub 未回傳新 Commit SHA。")

        # 7. 更新分支 Ref（遇 422 non-fast-forward 衝突自動重試）
        patch_resp = _request_with_retry(
            requests.patch,
            f"{base_api}/git/refs/heads/{quote(resolved_branch, safe='/')}",
            headers=headers,
            json={"sha": new_commit_sha, "force": False},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if patch_resp.status_code == requests.codes.ok:
            return {
                "commit_sha": new_commit_sha,
                "uploaded": uploaded_paths,
                "deleted": to_delete,
                "skipped_unchanged": skipped_paths,
                "portal_url": portal_url,
                "no_changes": False,
            }

        if patch_resp.status_code == requests.codes.unprocessable_entity and attempt < attempts:
            delay = _CONFLICT_RETRY_DELAYS_SECONDS[min(attempt - 1, len(_CONFLICT_RETRY_DELAYS_SECONDS) - 1)]
            logger.warning(
                "GitHub branch ref conflict (HTTP 422) on attempt %d/%d; retrying in %.1fs...",
                attempt,
                attempts,
                delay,
            )
            time.sleep(delay)
            continue

        raise GitHubUploadError(
            f"更新 GitHub 分支 {resolved_branch} 失敗（HTTP {patch_resp.status_code}）："
            f"{patch_resp.text[:500]}"
        )

    raise GitHubUploadError("GitHub 批次發布重試次數已耗盡。")  # pragma: no cover
