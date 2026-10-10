"""Upload generated chart pages using GitHub's Contents API."""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from urllib.parse import quote

import requests

_API_ROOT = "https://api.github.com"
_REQUEST_TIMEOUT_SECONDS = 20
_CHART_DIRECTORY = "charts"
_STOCK_ID_PATTERN = re.compile(r"^\d{4,6}$")


class GitHubUploadError(RuntimeError):
    """Raised when GitHub cannot accept or publish a chart page."""


def upload_to_github(file_path: str, stock_id: str) -> str:
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
    expected_filename = f"{normalized_stock_id}.html"
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

    current_file = requests.get(
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

    response = requests.put(
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
