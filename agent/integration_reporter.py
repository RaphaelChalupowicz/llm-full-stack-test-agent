"""
Orchestrates Jira issue creation and Bitbucket PR creation after a test
generation run succeeds or fails.

Success flow:
  1. Commit the passing test to a new Bitbucket branch.
  2. Create a Jira issue ("Tests added" / Done-ready).
  3. Open a Bitbucket PR referencing the Jira key.
  4. Attach the PR URL as a remote link on the Jira issue.

Failure flow:
  1. Commit the last (failing) test attempt to a new branch.
  2. Create a Jira issue ("Needs manual work") with the LLM analysis.
  3. Open a *draft* Bitbucket PR with the LLM analysis in the description.
  4. Attach the draft PR URL as a remote link on the Jira issue.

Both Jira and Bitbucket are optional — if a client is None the corresponding
steps are skipped.  Failures in either integration are caught and printed as
warnings so they never break the main agent loop.
"""

import os
import re

from jira_client import JiraClient, adf_doc, text_to_adf_nodes
from jira_client import _adf_heading, _adf_paragraph, _adf_code_block
from bitbucket_client import BitbucketClient


def _file_to_branch_slug(file_relative: str) -> str:
    """
    'src/components/auth/LoginForm.tsx' → 'components-auth-loginform'

    Args:
        file_relative (str): The relative path to the file.
    Returns:
        str: A slug suitable for use in branch names.
    """

    path = file_relative.replace("\\", "/")
    if path.startswith("src/"):
        path = path[4:]
    path = path.rsplit(".", 1)[0]  # strip extension
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower())
    slug = slug.strip("-")
    return slug[:60]  # keep branch names reasonable


def _truncate(text: str, max_chars: int = 2500) -> str:
    """
    Truncate text to a maximum number of characters, appending a note about omitted characters.

    Args:
        text (str): The text to truncate.
        max_chars (int): The maximum number of characters to keep.
    Returns:
        str: The truncated text with an omission note if truncation occurred.
    """

    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n\n… ({omitted} characters omitted)"


def _safe(fn, label: str, verbose: bool):
    """
    Run *fn()* and convert exceptions to printed warnings.
    Args:
        fn: A no argument function that performs an integration action (e.g. create Jira issue).
        label: A short label describing the action, used in warning messages.
        verbose: A boolean indicating whether to print verbose output.
    Returns:
        The result of *fn()* if it succeeds, or None if an exception occurs.
    """

    try:
        return fn()
    except Exception as exc:
        print(f"         [Integration] ⚠️  {label}: {exc}")
        return None

class IntegrationReporter:
    """
    Handles Jira issue creation and Bitbucket PR creation for test generation results.
    """

    def __init__(
        self,
        *,
        jira: JiraClient | None,
        bitbucket: BitbucketClient | None,
        verbose: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.jira = jira
        self.bitbucket = bitbucket
        self.verbose = verbose
        self.dry_run = dry_run

    @property
    def enabled(self) -> bool:
        return self.jira is not None or self.bitbucket is not None

    def report_success(
        self,
        *,
        file_relative: str,
        strategy: str,
        test_code: str,
        test_path_in_repo: str,
        epic_key: str | None = None,
        framework_label: str = "Jest",
        generator_label: str = "LLM React Test Generator",
        domain_label: str = "frontend",
    ) -> tuple[str | None, str | None]:
        """
        Create a Jira issue (done) + Bitbucket PR for a file where auto-generation produced a passing test.

        Args:
            file_relative: The relative path to the source file for which the test was generated.
            strategy: The generation strategy used (e.g. "initial", "repair-1", etc.).
            test_code: The full code of the generated test.
            test_path_in_repo: The path to the test file within the repository.
            epic_key: An optional Jira epic key to link the issue to.
            framework_label: A label describing the test framework, used in messages.
            generator_label: A label describing the generator, used in messages.
            domain_label: A label describing the domain, used for Jira issue labeling.
        Returns:
            tuple[str | None, str | None]: The Jira issue key and PR URL if created, or (None, None).
        """

        slug = _file_to_branch_slug(file_relative)
        branch = f"test/auto/{slug}"
        jira_key: str | None = None
        pr_url: str | None = None

        if self.dry_run:
            print(f"         [DRY-RUN] Would commit to branch '{branch}'")
            print(f"         [DRY-RUN] Would create Jira issue (success) for {file_relative}")
            print(f"         [DRY-RUN] Would open Bitbucket PR from '{branch}'")
            return None, None

        # 1. Create Jira issue first, so downstream artifacts can carry issue key
        if self.jira:
            description = adf_doc(
                _adf_heading("Automatically Generated Tests", 2),
                _adf_paragraph(
                    f"{generator_label} successfully created a "
                    f"passing {framework_label} test for '{file_relative}'."
                ),
                _adf_heading("What Changed", 2),
                _adf_paragraph(
                    f"A new generated test was added/updated at '{test_path_in_repo}'."
                ),
                _adf_heading("Test Details", 2),
                _adf_paragraph(f"Strategy: {strategy}"),
                _adf_paragraph(f"Source file: {file_relative}"),
                _adf_paragraph(f"Test saved to: {test_path_in_repo}"),
                _adf_heading("Generated Test (preview)", 2),
                _adf_code_block(_truncate(test_code, 2000)),
            )
            jira_key = _safe(
                lambda: self.jira.create_issue(
                    summary=f"[AutoTest] \u2705 Tests added for {file_relative}",
                    description_adf=description,
                    labels=["auto-test", "test-coverage", domain_label],
                    epic_key=epic_key,
                ),
                "Jira issue creation",
                self.verbose,
            )
            if jira_key and self.verbose:
                print(f"         [Integration] Created Jira issue {jira_key}")

        # 2. Commit test file to Bitbucket
        if jira_key:
            branch = f"test/auto/{jira_key.lower()}-{slug}"

        if self.bitbucket:
            commit_msg = (
                f"{jira_key}: test(auto): add generated test for {file_relative}"
                if jira_key
                else f"test(auto): add generated test for {file_relative}"
            )
            _safe(
                lambda: self.bitbucket.commit_file(
                    branch=branch,
                    file_path=test_path_in_repo,
                    content=test_code,
                    message=commit_msg,
                ),
                "Bitbucket commit",
                self.verbose,
            )
            if self.verbose:
                print(f"         [Integration] Committed test to branch '{branch}'")

        # 3. Create Bitbucket PR
        if self.bitbucket:
            pr_desc = (
                f"## \u2705 Automatically Generated Tests\n\n"
                f"**File:** `{file_relative}`  \n"
                f"**Strategy:** {strategy}  \n"
                f"**Framework:** {framework_label}  \n"
                f"**Test path:** `{test_path_in_repo}`\n\n"
                f"Generated by "
                f"[{generator_label}]"
                f"(https://github.com/RaphaelChalupowicz/llm-react-test-generator).\n"
            )
            if jira_key:
                pr_desc += f"\n**Jira:** {jira_key}\n"

            pr = _safe(
                lambda: self.bitbucket.create_pull_request(
                    title=(
                        f"[{jira_key}] [AutoTest] Add generated tests for "
                        f"{os.path.basename(file_relative)}"
                        if jira_key
                        else f"[AutoTest] Add generated tests for "
                             f"{os.path.basename(file_relative)}"
                    ),
                    description=pr_desc,
                    source_branch=branch,
                    draft=False,
                ),
                "Bitbucket PR creation",
                self.verbose,
            )
            if pr:
                pr_url = pr["url"]
                if self.verbose:
                    print(f"         [Integration] Created Bitbucket PR: {pr_url}")

        # 4. Link PR to Jira issue
        if self.jira and jira_key and pr_url:
            _safe(
                lambda: self.jira.add_remote_link(
                    jira_key,
                    url=pr_url,
                    title=f"Bitbucket PR: {os.path.basename(file_relative)}",
                ),
                "Jira remote link",
                self.verbose,
            )
            # Fallback visibility: always add a Jira comment with the PR URL.
            _safe(
                lambda: self.jira.add_comment(
                    jira_key,
                    adf_doc(
                        _adf_paragraph(
                            f"Bitbucket PR created: {pr_url}"
                        )
                    ),
                ),
                "Jira PR comment",
                self.verbose,
            )

        self._print_integration_summary(jira_key, pr_url, draft=False)
        return jira_key, pr_url

    def report_failure(
        self,
        *,
        file_relative: str,
        strategy: str,
        test_code: str,
        test_path_in_repo: str,
        last_error: str,
        failure_type: str,
        llm_analysis: str,
        epic_key: str | None = None,
        framework_label: str = "Jest",
        generator_label: str = "LLM React Test Generator",
        domain_label: str = "frontend",
    ) -> tuple[str | None, str | None]:
        """
        Create a Jira issue (needs-work) + draft Bitbucket PR for a file where auto-generation failed to produce a passing test.

        Args:
            file_relative: The relative path to the source file for which the test was generated.
            strategy: The generation strategy used (e.g. "initial", "repair-1", etc.).
            test_code: The full code of the generated test.
            test_path_in_repo: The path to the test file within the repository.
            last_error: The error message from the last test attempt.
            failure_type: The type of failure that occurred.
            llm_analysis: The analysis provided by the LLM for the failure.
            epic_key: An optional Jira epic key to link the issue to.
            framework_label: A label describing the test framework, used in messages.
            generator_label: A label describing the generator, used in messages.
            domain_label: A label describing the domain, used for Jira issue labeling.
        Returns:
            tuple[str | None, str | None]: The Jira issue key and PR URL if created, or (None, None).
        """

        slug = _file_to_branch_slug(file_relative)
        branch = f"test/needs-work/{slug}"
        jira_key: str | None = None
        pr_url: str | None = None

        if self.dry_run:
            print(f"         [DRY-RUN] Would commit failing test to branch '{branch}'")
            print(f"         [DRY-RUN] Would create Jira issue (needs-work) for {file_relative}")
            print(f"         [DRY-RUN] Would open draft Bitbucket PR from '{branch}'")
            return None, None

        # 1. Create Jira issue first, so downstream artifacts can carry issue key
        if self.jira:
            analysis_nodes = text_to_adf_nodes(_truncate(llm_analysis, 2500))
            description = adf_doc(
                _adf_heading("Automatic Test Generation Failed", 2),
                _adf_paragraph(
                    f"{generator_label} could not produce a passing "
                    f"{framework_label} test for '{file_relative}' after all repair attempts."
                ),
                _adf_heading("What Changed", 2),
                _adf_paragraph(
                    f"A generated test attempt was saved to '{test_path_in_repo}' "
                    f"using strategy '{strategy}', but it still failed."
                ),
                _adf_heading("Failure Details", 2),
                _adf_paragraph(f"Failure type: {failure_type}"),
                _adf_heading(f"Last {framework_label} Error", 2),
                _adf_code_block(_truncate(last_error, 1000), language=""),
                _adf_heading("Last Test Attempt (preview)", 2),
                _adf_code_block(_truncate(test_code, 1500)),
                _adf_heading("Approach To Fix", 2),
                *analysis_nodes,
                _adf_heading("Developer Action Required", 2),
                _adf_paragraph(
                    "Please review the draft PR, apply the recommendations above, "
                    "fix the test manually, and mark this issue as Done when complete."
                ),
            )
            jira_key = _safe(
                lambda: self.jira.create_issue(
                    summary=f"[AutoTest] \u26a0\ufe0f Manual test needed for {file_relative}",
                    description_adf=description,
                    labels=["auto-test", "needs-work", domain_label],
                    epic_key=epic_key,
                ),
                "Jira issue creation",
                self.verbose,
            )
            if jira_key and self.verbose:
                print(f"         [Integration] Created Jira issue {jira_key} (needs-work)")

        # 2. Commit last test attempt to Bitbucket
        if jira_key:
            branch = f"test/needs-work/{jira_key.lower()}-{slug}"

        if self.bitbucket:
            commit_msg = (
                f"{jira_key}: test(needs-work): add failing test attempt for {file_relative}"
                if jira_key
                else f"test(needs-work): add failing test attempt for {file_relative}"
            )
            _safe(
                lambda: self.bitbucket.commit_file(
                    branch=branch,
                    file_path=test_path_in_repo,
                    content=test_code,
                    message=commit_msg,
                ),
                "Bitbucket commit",
                self.verbose,
            )
            if self.verbose:
                print(f"         [Integration] Committed failing test to branch '{branch}'")

        # 3. Create draft Bitbucket PR
        if self.bitbucket:
            pr_desc = (
                f"## \u26a0\ufe0f Needs Manual Work\n\n"
                f"**File:** `{file_relative}`  \n"
                f"**Strategy:** {strategy}  \n"
                f"**Framework:** {framework_label}  \n"
                f"**Failure type:** `{failure_type}`\n\n"
                f"---\n\n"
                f"## What Changed\n\n"
                f"A generated test attempt was added/updated at `{test_path_in_repo}` but still fails.\n\n"
                f"---\n\n"
                f"## LLM Analysis\n\n"
                f"{_truncate(llm_analysis, 2000)}\n\n"
                f"---\n\n"
                f"## Last {framework_label} Error\n\n"
                f"```\n{_truncate(last_error, 800)}\n```\n\n"
                f"---\n\n"
                f"Generated by "
                f"[{generator_label}]"
                f"(https://github.com/RaphaelChalupowicz/llm-react-test-generator).\n"
            )
            if jira_key:
                pr_desc += f"\n**Jira:** {jira_key}\n"

            pr = _safe(
                lambda: self.bitbucket.create_pull_request(
                    title=(
                        f"[{jira_key}] [AutoTest] NEEDS WORK: test for "
                        f"{os.path.basename(file_relative)}"
                        if jira_key
                        else f"[AutoTest] NEEDS WORK: test for "
                             f"{os.path.basename(file_relative)}"
                    ),
                    description=pr_desc,
                    source_branch=branch,
                    draft=True,
                ),
                "Bitbucket draft PR creation",
                self.verbose,
            )
            if pr:
                pr_url = pr["url"]
                if self.verbose:
                    print(f"         [Integration] Created draft Bitbucket PR: {pr_url}")

        # 4. Link draft PR to Jira issue
        if self.jira and jira_key and pr_url:
            _safe(
                lambda: self.jira.add_remote_link(
                    jira_key,
                    url=pr_url,
                    title=f"Draft Bitbucket PR (needs work): "
                          f"{os.path.basename(file_relative)}",
                ),
                "Jira remote link",
                self.verbose,
            )
            # Fallback visibility: always add a Jira comment with the PR URL.
            _safe(
                lambda: self.jira.add_comment(
                    jira_key,
                    adf_doc(
                        _adf_paragraph(
                            f"Draft Bitbucket PR created: {pr_url}"
                        )
                    ),
                ),
                "Jira PR comment",
                self.verbose,
            )

        self._print_integration_summary(jira_key, pr_url, draft=True)
        return jira_key, pr_url

    def _print_integration_summary(
        self, jira_key: str | None, pr_url: str | None, draft: bool
    ) -> None:
        """
        Print a summary of the integration results.

        Args:
            jira_key: The Jira issue key if a Jira issue was created, or None.
            pr_url: The Bitbucket PR URL if a PR was created, or None.
            draft: A boolean indicating whether the PR is a draft (for failures).
        """
        
        parts = []
        if jira_key:
            parts.append(f"Jira: {jira_key}")
        if pr_url:
            label = "Draft PR" if draft else "PR"
            parts.append(f"{label}: {pr_url}")
        if parts:
            print(f"         \U0001f517 Integration: {' | '.join(parts)}")
