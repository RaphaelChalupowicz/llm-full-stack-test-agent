"""
Tracks per file generation results across runs so the agent can resume
without reprocessing files that have already been successfully tested.

The tracker persists results to .progress.json inside the generated tests
output directory (tests/generated/).
"""

import json
import os
from datetime import datetime, timezone

_PROGRESS_FILE = ".progress.json"


def _load(path: str) -> dict:
    """
    Loads progress data from a JSON file.
    Args:
        path (str): The path to the JSON file.
    Returns:
        dict: The loaded progress data.
    """

    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(path: str, data: dict) -> None:
    """
    Saves progress data to a JSON file.
    Args:
        path (str): The path to the JSON file.
        data (dict): The progress data to save.
    """

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


class ProgressTracker:
    """Persists per file generation results to disk for crash recovery."""

    def __init__(self, tests_output_dir: str) -> None:
        """
        Initializes the progress tracker.
        Args:
            tests_output_dir (str): The directory where generated tests will be saved.
        """

        self._path = os.path.join(tests_output_dir, _PROGRESS_FILE)
        self._data = _load(self._path)


    def get_status(self, file_relative: str) -> str | None:
        """
        Returns the status of a file.
        Args:
            file_relative (str): The relative path to the file.
        Returns:
            str | None: The status of the file or None if not found.
        """

        return self._data.get(file_relative, {}).get("status")


    def mark(
        self,
        file_relative: str,
        status: str,
        jira_key: str | None = None,
        pr_url: str | None = None,
    ) -> None:
        """
        Record result for a file. status is one of: 'pass', 'fail', 'skip'.
        Args:
            file_relative (str): The relative path to the file.
            status (str): The status of the file.
            jira_key (str | None): The Jira issue key for the file, or None.
            pr_url (str | None): The PR URL for the file, or None.
        """

        entry: dict = {
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if jira_key:
            entry["jira_key"] = jira_key
        if pr_url:
            entry["pr_url"] = pr_url
        self._data[file_relative] = entry
        _save(self._path, self._data)


    def reset(self) -> None:
        """
        Clear all stored progress.
        """

        self._data = {}
        if os.path.exists(self._path):
            os.remove(self._path)


    @property
    def path(self) -> str:
        return self._path
