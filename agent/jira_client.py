"""
Jira REST API client for creating issues and attaching remote links.

Authentication: HTTP Basic auth (email + API token).
Description format: Atlassian Document Format (ADF).
"""

import requests
from requests.auth import HTTPBasicAuth


def _adf_text(text: str) -> dict:
    return {"type": "text", "text": text}


def _adf_paragraph(text: str) -> dict:
    return {"type": "paragraph", "content": [_adf_text(text)]}


def _adf_heading(text: str, level: int = 2) -> dict:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [_adf_text(text)],
    }


def _adf_code_block(code: str, language: str = "typescript") -> dict:
    return {
        "type": "codeBlock",
        "attrs": {"language": language},
        "content": [_adf_text(code)],
    }


def adf_doc(*nodes: dict) -> dict:
    """Build an ADF document from a sequence of block nodes."""
    return {"version": 1, "type": "doc", "content": list(nodes)}


def text_to_adf_nodes(text: str) -> list[dict]:
    """
    Very simple plain-text / light-markdown → ADF converter.
    Handles headings (# lines) and paragraphs (blank-line separated).
    Skips empty sections.
    """
    nodes: list[dict] = []
    # Split on blank lines to get sections
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        first = lines[0]
        if first.startswith("#"):
            level = min(len(first) - len(first.lstrip("#")), 6)
            heading_text = first.lstrip("# ").strip()
            nodes.append(_adf_heading(heading_text, level))
            rest = "\n".join(lines[1:]).strip()
            if rest:
                nodes.append(_adf_paragraph(rest))
        else:
            nodes.append(_adf_paragraph(block))
    return nodes or [_adf_paragraph(text)]


class JiraClient:
    """
    Thin wrapper around the Jira REST API v3.
    All methods raise requests.HTTPError on non-2xx responses.
    """

    def __init__(
        self,
        *,
        url: str,
        email: str,
        token: str,
        project_key: str,
    ) -> None:
        self._base = url.rstrip("/") + "/rest/api/3"
        self._auth = HTTPBasicAuth(email, token)
        self._project_key = project_key
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }


    def create_issue(
        self,
        *,
        summary: str,
        description_adf: dict,
        labels: list[str] | None = None,
        issue_type: str = "Task",
        epic_key: str | None = None,
    ) -> str:
        """
        Create a Jira issue and return its key (e.g. 'SHOP-42').

        epic_key: if provided, sets the 'parent' field (works for
                  team-managed / next-gen projects).  Classic projects
                  may need a custom epic-link field - see JIRA_EPIC_FIELD.
        """
        fields: dict = {
            "project": {"key": self._project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
            "description": description_adf,
        }
        if labels:
            fields["labels"] = labels
        if epic_key:
            fields["parent"] = {"key": epic_key}

        resp = requests.post(
            f"{self._base}/issue",
            headers=self._headers,
            auth=self._auth,
            json={"fields": fields},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["key"]

    def add_remote_link(
        self,
        issue_key: str,
        *,
        url: str,
        title: str,
    ) -> None:
        """Attach an external URL (e.g. a Bitbucket PR) to a Jira issue."""
        payload = {
            "globalId": f"link-{url}",
            "application": {
                "type": "com.atlassian.bitbucket",
                "name": "Bitbucket",
            },
            "relationship": "pull request",
            "object": {
                "url": url,
                "title": title,
                "summary": title,
                "icon": {
                    "url16x16": "https://bitbucket.org/favicon.ico",
                    "title": "Bitbucket",
                },
                "status": {
                    "resolved": False,
                    "icon": {
                        "url16x16": "https://bitbucket.org/favicon.ico",
                        "title": "Open",
                        "link": url,
                    },
                },
            },
        }
        resp = requests.post(
            f"{self._base}/issue/{issue_key}/remotelink",
            headers=self._headers,
            auth=self._auth,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()

    def add_comment(self, issue_key: str, body_adf: dict) -> None:
        """Add an ADF-formatted comment to an existing Jira issue."""
        resp = requests.post(
            f"{self._base}/issue/{issue_key}/comment",
            headers=self._headers,
            auth=self._auth,
            json={"body": body_adf},
            timeout=30,
        )
        resp.raise_for_status()
