"""
Bitbucket REST API client for committing files and opening pull requests.

Authentication: HTTP Basic auth (username + app password / access token).
"""

import os
import requests
from requests.auth import HTTPBasicAuth


_BITBUCKET_API = "https://api.bitbucket.org/2.0"


class BitbucketClient:
    """
    Client for interacting with the Bitbucket REST API, focused on committing files and creating pull requests.
    """

    def __init__(
        self,
        *,
        workspace: str,
        repo_slug: str,
        username: str,
        token: str,
        default_branch: str = "main",
        default_reviewers: list[str] | None = None,
    ) -> None:
        self._workspace = workspace
        self._repo_slug = repo_slug
        self._auth = HTTPBasicAuth(username, token)
        self._default_branch = default_branch
        self._default_reviewers: list[str] = default_reviewers or []
        self._base = f"{_BITBUCKET_API}/repositories/{workspace}/{repo_slug}"

    def commit_file(
        self,
        *,
        branch: str,
        file_path: str,
        content: str,
        message: str,
    ) -> None:
        """
        Commit a single file to *branch*.

        Uses the Bitbucket multipart source endpoint.  If *branch* does not
        yet exist it is created from the repository's default branch
        automatically by Bitbucket.

        file_path is the path inside the repository, e.g.
            'tests/generated/components/Button.test.tsx'

        Args:
            branch: The name of the branch to commit to.
            file_path: The path inside the repository.
            content: The content of the file to commit.
            message: The commit message.
        """

        resp = requests.post(
            f"{self._base}/src",
            auth=self._auth,
            files={
                "message": (None, message),
                "branch": (None, branch),
                file_path: (
                    os.path.basename(file_path),
                    content.encode("utf-8"),
                    "text/plain",
                ),
            },
            timeout=60,
        )
        resp.raise_for_status()

    def create_pull_request(
        self,
        *,
        title: str,
        description: str,
        source_branch: str,
        target_branch: str | None = None,
        draft: bool = False,
        reviewers: list[str] | None = None,
    ) -> dict:
        """
        Create a pull request and return a dict with 'id', 'url', 'title'.

        reviewers: list of Bitbucket account UUIDs, e.g. ['{some-uuid}'].
                   Merged with default_reviewers set at construction time.
                   Obtain a user's UUID from
                   GET /2.0/users/{account_id_or_username}.
        
        Args:
            title: The title of the pull request.
            description: The description of the pull request.
            source_branch: The name of the branch to create the pull request from.
            target_branch: The name of the branch to create the pull request to. If not provided, the default branch is used.
            draft: Whether to create the pull request as a draft.
            reviewers: A list of Bitbucket account UUIDs to review the pull request.
        Returns:
            A dict containing the 'id', 'url', and 'title' of the created pull request.
        """

        target = target_branch or self._default_branch
        # Merge constructor defaults with call-site overrides (deduplicate)
        all_reviewers = list({*self._default_reviewers, *(reviewers or [])})
        payload: dict = {
            "title": title,
            "description": description,
            "source": {"branch": {"name": source_branch}},
            "destination": {"branch": {"name": target}},
        }
        if draft:
            payload["draft"] = True
        if all_reviewers:
            payload["reviewers"] = [{"uuid": uid} for uid in all_reviewers]

        resp = requests.post(
            f"{self._base}/pullrequests",
            auth=self._auth,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "id": data["id"],
            "url": data["links"]["html"]["href"],
            "title": data["title"],
        }
