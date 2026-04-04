import argparse
import os
import sys
from pathlib import Path
from csharp_bootstrap import GENERATED_TESTS_DIR, bootstrap_csharp_test_project, repair_project_reference
from csharp_parser import classify_csharp_file, detect_csharp_project_file, find_csharp_files
from csharp_test_executor import extract_relevant_dotnet_error, is_project_reference_error, run_csharp_test
from csharp_test_generator import explain_csharp_test_failure, fix_csharp_test, generate_csharp_test, infer_test_class_name, should_generate_csharp_test
from progress_tracker import ProgressTracker
from strategy_selector import select_strategy
from test_generator import (
    should_generate_test,
    generate_test,
    fix_test_after_failure,
    explain_test_failure,
)
from test_executor import run_single_test
from failure_classifier import (
    extract_relevant_jest_error,
    classify_failure,
    build_repair_hint,
    apply_local_failure_fix,
)
from bootstrap_jest import bootstrap_jest_project
from project_analyzer import analyze_project
from coverage_reader import get_coverage_gaps

from dotenv import load_dotenv


# load .env from repository root first, then local fallback
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
load_dotenv()


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


MAX_FIX_ATTEMPTS = _env_int("MAX_FIX_ATTEMPTS", 3)
DEFAULT_MAX_FILES = _env_int("DEFAULT_MAX_FILES", 2)
DEFAULT_COVERAGE_PATH = os.getenv("DEFAULT_COVERAGE_PATH", "coverage/coverage-summary.json")
TEST_TIMEOUT_SECONDS = _env_int("TEST_TIMEOUT_SECONDS", 120)


def validate_api_key() -> None:
    """
    Validates that the OpenAI API key is configured.
    """

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key or api_key == "your_api_key_here":
        print("ERROR: OPENAI_API_KEY is not configured.")
        print("Set it in your .env file or as an environment variable.")
        print("Example: OPENAI_API_KEY=sk-...")
        sys.exit(1)


def _build_integration_reporter(
    *,
    verbose: bool = False,
    dry_run: bool = False,
    no_integration: bool = False,
    csharp_mode: bool = False,
):
    """
    Build an IntegrationReporter from environment variables.

    Returns None if no_integration is True.
    Returns a reporter with whichever clients are fully configured.
    Either Jira or Bitbucket may be absent independently.

    Args:
        verbose (bool): Whether to print verbose output.
        dry_run (bool): Whether to perform a dry run.
        no_integration (bool): Whether to disable integration.
        csharp_mode (bool): Whether to run in C# mode.

    Returns:
        IntegrationReporter or None: The built integration reporter or None if disabled.
    """

    if no_integration:
        return None

    from integration_reporter import IntegrationReporter

    jira_client = None
    bitbucket_client = None

    def _env_prefixed(name: str) -> str:
        """Prefer C#-scoped env vars when running run-csharp, else shared vars."""
        if csharp_mode:
            return os.getenv(f"CSHARP_{name}") or os.getenv(name, "")
        return os.getenv(name, "")

    jira_url = _env_prefixed("JIRA_URL")
    jira_email = _env_prefixed("JIRA_EMAIL")
    jira_token = _env_prefixed("JIRA_TOKEN")
    jira_project = _env_prefixed("JIRA_PROJECT_KEY")

    if all([jira_url, jira_email, jira_token, jira_project]):
        from jira_client import JiraClient
        jira_client = JiraClient(
            url=jira_url,
            email=jira_email,
            token=jira_token,
            project_key=jira_project,
        )
        mode = " (C#)" if csharp_mode else ""
        print(f"Jira integration{mode}: enabled (project {jira_project})")
    else:
        if csharp_mode:
            print(
                "Jira integration (C#): disabled "
                "(CSHARP_JIRA_* or shared JIRA_* vars not fully set)"
            )
        else:
            print("Jira integration: disabled (JIRA_URL/EMAIL/TOKEN/PROJECT_KEY not fully set)")

    bb_workspace = _env_prefixed("BITBUCKET_WORKSPACE")
    bb_repo = _env_prefixed("BITBUCKET_REPO_SLUG")
    bb_user = _env_prefixed("BITBUCKET_USERNAME")
    bb_token = _env_prefixed("BITBUCKET_TOKEN")
    bb_branch = _env_prefixed("BITBUCKET_DEFAULT_BRANCH") or "main"
    bb_reviewers_raw = _env_prefixed("BITBUCKET_REVIEWERS")
    bb_reviewers = [r.strip() for r in bb_reviewers_raw.split(",") if r.strip()]

    if all([bb_workspace, bb_repo, bb_user, bb_token]):
        from bitbucket_client import BitbucketClient
        bitbucket_client = BitbucketClient(
            workspace=bb_workspace,
            repo_slug=bb_repo,
            username=bb_user,
            token=bb_token,
            default_branch=bb_branch,
            default_reviewers=bb_reviewers,
        )
        mode = " (C#)" if csharp_mode else ""
        print(
            f"Bitbucket integration{mode}: enabled "
            f"({bb_workspace}/{bb_repo}, branch → {bb_branch})"
        )
    else:
        if csharp_mode:
            print(
                "Bitbucket integration (C#): disabled "
                "(CSHARP_BITBUCKET_* or shared BITBUCKET_* vars not fully set)"
            )
        else:
            print(
                "Bitbucket integration: disabled "
                "(BITBUCKET_WORKSPACE/REPO_SLUG/USERNAME/TOKEN not fully set)"
            )

    reporter = IntegrationReporter(
        jira=jira_client,
        bitbucket=bitbucket_client,
        verbose=verbose,
        dry_run=dry_run,
    )
    return reporter if reporter.enabled else None


def ensure_output_dir(project_root: str) -> str:
    """
    Ensures the output directory for generated tests exists, and returns its path.
    Args:
        project_root (str): The project root path.
    Returns:
        str: The path to the output directory.
    """

    output_dir = os.path.join(project_root, "tests", "generated")
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


def file_text(path: str) -> str:
    """
    Returns the text content of a file given its path.
    Args:
        path (str): The file path.
    Returns:
        str: The file content.
    """

    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def save_test(output_path: str, test_code: str) -> None:
    """
    Saves the generated test code to the specified output path.
    Args:
        output_path (str): The file path where the test code will be saved.
        test_code (str): The test code to save.
    """

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(test_code)


def get_test_filename(file_rel: str) -> str:
    """
    Returns the filename for the corresponding test file.
    Args:
        file_rel (str): The relative path to the source file.
    Returns:
        str: The relative path to the test file.
    """

    relative_no_src = file_rel.replace("src/", "", 1)
    base, ext = os.path.splitext(relative_no_src)
    return f"{base}.test{ext}"

def _run_success_integration(
    *,
    reporter,
    file_rel: str,
    file_abs: str,
    strategy: str,
    output_path: str,
    test_relative_path_for_runner: str,
    jira_epic_key: str | None,
    framework_label: str = "Jest",
    generator_label: str = "LLM Test Generator",
    domain_label: str = "frontend",
) -> tuple[str | None, str | None]:
    """Call reporter.report_success() if integration is active."""
    if reporter is None or not reporter.enabled:
        return None, None
    try:
        test_code = file_text(output_path)
        return reporter.report_success(
            file_relative=file_rel,
            strategy=strategy,
            test_code=test_code,
            test_path_in_repo=test_relative_path_for_runner,
            epic_key=jira_epic_key,
            framework_label=framework_label,
            generator_label=generator_label,
            domain_label=domain_label,
        )
    except Exception as exc:
        print(f"         [Integration] ⚠️  Success reporting failed: {exc}")
        return None, None


def _run_failure_integration(
    *,
    reporter,
    file_rel: str,
    file_abs: str,
    strategy: str,
    output_path: str,
    test_relative_path_for_runner: str,
    last_error_snippet: str,
    last_failure_type: str,
    jira_epic_key: str | None,
    verbose: bool,
    framework_label: str = "Jest",
    generator_label: str = "LLM Test Generator",
    domain_label: str = "frontend",
) -> tuple[str | None, str | None]:
    """
    Call explain_test_failure() then reporter.report_failure().
    Always prints the LLM analysis to stdout.
    """
    test_code = file_text(output_path) if os.path.exists(output_path) else ""

    # Always ask the LLM to explain the failure — useful even without integration
    try:
        analysis = explain_test_failure(
            file_absolute=file_abs,
            file_relative=file_rel,
            test_code=test_code,
            jest_error=last_error_snippet,
            failure_type=last_failure_type,
            verbose=verbose,
        )
        print("\n=== LLM FAILURE ANALYSIS ===")
        print(analysis)
        print("============================\n")
    except Exception as exc:
        print(f"         [LLM] ⚠️  Could not generate failure analysis: {exc}")
        analysis = f"(analysis unavailable: {exc})"

    if reporter is None or not reporter.enabled:
        return None, None

    try:
        return reporter.report_failure(
            file_relative=file_rel,
            strategy=strategy,
            test_code=test_code,
            test_path_in_repo=test_relative_path_for_runner,
            last_error=last_error_snippet,
            failure_type=last_failure_type,
            llm_analysis=analysis,
            epic_key=jira_epic_key,
            framework_label=framework_label,
            generator_label=generator_label,
            domain_label=domain_label,
        )
    except Exception as exc:
        print(f"         [Integration] ⚠️  Failure reporting failed: {exc}")
        return None, None

def process_one_gap(
    *,
    gap: dict,
    project_root: str,
    test_command: list[str],
    tests_output_dir: str,
    tracker: ProgressTracker,
    reporter=None,
    jira_epic_key: str | None = None,
    verbose: bool = False,
) -> bool:
    """
    Processes a single coverage gap by generating a test for it.
    Args:
        gap (dict): The coverage gap information.
        project_root (str): The project root path.
        test_command (list[str]): The test command to run.
        tests_output_dir (str): The directory where generated tests will be saved.
        tracker (ProgressTracker): The progress tracker instance.
        reporter: The integration reporter instance, or None to disable reporting.
        jira_epic_key (str | None): An optional Jira epic key to link created issues to.
        verbose (bool, optional): Whether to print verbose output. Defaults to False.
    Returns:
        bool: True if the test was generated and passed, False otherwise.
    """

    file_rel = gap["file_relative"]
    file_abs = gap["file_absolute"]

    # Skip files that were already handled in a previous run
    existing_status = tracker.get_status(file_rel)
    if existing_status == "pass":
        print(f"↷ ALREADY PASSED (skipping) — {file_rel}\n")
        return True
    if existing_status == "skip":
        print(f"↷ ALREADY SKIPPED (skipping) — {file_rel}\n")
        return False

    # select a testing strategy based on the file location and name
    strategy = select_strategy(file_rel)

    # determine if it is worth generating a test for a file and get the reason for the decision
    worth_it, reason = should_generate_test(file_abs, file_rel, verbose=verbose)

    if not worth_it:
        print(f"✗ SKIP - {file_rel}")
        print(f"         Strategy: {strategy}")
        print(f"         Reason: {reason}\n")
        tracker.mark(file_rel, "skip")
        return False
    else:
        print(f"✓ GENERATING - {file_rel}")
        print(f"         Strategy: {strategy}")
        print(f"         Reason: {reason}")

    if gap.get("discovered_without_coverage"):
        print("         Coverage source: fallback source scan")

    test_filename = get_test_filename(file_rel) # determine the output test file name based on the source file path
    output_path = os.path.join(tests_output_dir, test_filename) # determine the full output path for the generated test file
    test_relative_path_for_runner = os.path.join("tests", "generated", test_filename).replace("\\", "/") # determine the test file path relative to the project root, for use in test commands and reporting

    try:
        test_code = generate_test(
            file_absolute=file_abs,
            file_relative=file_rel,
            strategy=strategy,
            verbose=verbose,
        )
        save_test(output_path, test_code)
        print(f"        Saved -> {test_relative_path_for_runner}")
    except Exception as err:
        print(f"        ✗ FAILED DURING GENERATION - {file_rel}")
        print(f"        Error: {err}\n")
        tracker.mark(file_rel, "fail")
        return False

    unchanged_count = 0

    # attempt to run the test and fix it if it fails
    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        print(f"        Running test (attempt {attempt}/{MAX_FIX_ATTEMPTS})...")

        # run the test and capture whether it passed and its output
        passed, output = run_single_test(
            project_root=project_root,
            test_command=test_command,
            test_relative_path=test_relative_path_for_runner,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )
        
        if "INFRA_ERROR:" in output:
            print(f"        ✗ INFRA ERROR - {test_relative_path_for_runner}")
            print(output)
            tracker.mark(file_rel, "fail")
            return False

        if passed:
            print(f"        ✓ PASS - {test_relative_path_for_runner}\n")
            jira_key, pr_url = _run_success_integration(
                reporter=reporter,
                file_rel=file_rel,
                file_abs=file_abs,
                strategy=strategy,
                output_path=output_path,
                test_relative_path_for_runner=test_relative_path_for_runner,
                jira_epic_key=jira_epic_key,
            )
            tracker.mark(file_rel, "pass", jira_key=jira_key, pr_url=pr_url)
            return True

        print(f"        ✗ FAIL - {test_relative_path_for_runner}")

        error_snippet = extract_relevant_jest_error(output) # extract the most relevant portion of the Jest error output for classification and repair
        failure_type = classify_failure(error_snippet) # classify the failure to determine the most likely cause and best repair approach
        repair_hint = build_repair_hint(failure_type) # build a repair hint based on the failure type to guide the LLM in fixing the test
        
        # Keep last known error for failure reporting
        last_error_snippet = error_snippet
        last_failure_type = failure_type

        print(f"         Failure type: {failure_type}")
        print("\n--- ERROR SENT TO REPAIR LOOP ---")
        print(error_snippet)
        print("---------------------------------\n")

        try:
            before_fix = file_text(output_path)

            local_fix = apply_local_failure_fix(before_fix, failure_type) # attempt to apply a local, deterministic fix based on the failure type
            if local_fix is not None and local_fix != before_fix:
                save_test(output_path, local_fix)
                print("        ✓ Applied local deterministic fix.")
                continue

            if unchanged_count >= 1:
                print("        Forcing simplified repair...")
                repair_hint += "\nIMPORTANT: simplify the test to make it pass.\n"

            # attempt to fix the test using the LLM. providing the original code the Jest error output the failure classification and a repair hint to guide the fix
            fixed_test_code = fix_test_after_failure(
                file_absolute=file_abs,
                file_relative=file_rel,
                current_test_code=before_fix,
                jest_error_output=error_snippet,
                failure_type=failure_type,
                repair_hint=repair_hint,
                verbose=verbose,
            )

            save_test(output_path, fixed_test_code)
            after_fix = file_text(output_path)

            if before_fix == after_fix:
                unchanged_count += 1
                print("        ✗ Repair returned identical code.")
            else:
                unchanged_count = 0
                print("        ✓ Repair changed the file.")

        except Exception as err:
            print(f"        ✗ FAILED DURING REPAIR - {file_rel}")
            print(f"        Error: {err}\n")
            tracker.mark(file_rel, "fail")
            return False

    print(f"        ✗ GAVE UP AFTER {MAX_FIX_ATTEMPTS} ATTEMPTS - {file_rel}\n")
    jira_key, pr_url = _run_failure_integration(
        reporter=reporter,
        file_rel=file_rel,
        file_abs=file_abs,
        strategy=strategy,
        output_path=output_path,
        test_relative_path_for_runner=test_relative_path_for_runner,
        last_error_snippet=last_error_snippet,
        last_failure_type=last_failure_type,
        jira_epic_key=jira_epic_key,
        verbose=verbose,
    )
    tracker.mark(file_rel, "fail", jira_key=jira_key, pr_url=pr_url)
    return False


# ---------------------------------------------------------------------------
# C# backend test-generation pipeline
# ---------------------------------------------------------------------------

    
def _csharp_detect_project_namespace(project_root: str) -> str:
    """
    Detect the main project namespace by reading the RootNamespace from the primary .csproj file, or falling back to the .csproj filename or project root name.

    Args:
        project_root (str): The root directory of the C# project.
    Returns:
        str: The detected project namespace.
    """

    import re as _re
    csproj = detect_csharp_project_file(project_root)
    if csproj:
        with open(csproj, encoding="utf-8", errors="replace") as f:
            content = f.read()
        m = _re.search(r"<RootNamespace>(.*?)</RootNamespace>", content)
        if m:
            return m.group(1).strip()
        return os.path.splitext(os.path.basename(csproj))[0]
    return os.path.basename(os.path.abspath(project_root))


def _get_csharp_test_output_path(
    file_rel: str,
    test_project_dir: str,
) -> str:
    """
    Mirror the source file path inside the GeneratedTests directory.
    e.g. Controllers/UserController.cs ->
         <test_project_dir>/Controllers/UserControllerTests.cs
    Args:
        file_rel (str): The file path relative to the project root, e.g. "Controllers/UserController.cs".
        test_project_dir (str): The directory where test files are stored.
    Returns:
        str: The output path for the generated C# test file.
    """

    parts = file_rel.replace("\\", "/").split("/")
    folder_parts = parts[:-1]
    stem = os.path.splitext(parts[-1])[0]
    test_filename = f"{stem}Tests.cs"
    return os.path.join(test_project_dir, *folder_parts, test_filename)


def process_one_csharp_file(
    *,
    cs_file: dict,
    project_root: str,
    test_project_dir: str,
    project_namespace: str,
    tracker: ProgressTracker,
    reporter=None,
    jira_epic_key: str | None = None,
    verbose: bool = False,
) -> bool:
    """
    Generate, build, and run xUnit tests for a single C# source file.
    
    Args:
        cs_file (dict): A dictionary containing the relative and absolute paths of the C# source file.
        project_root (str): The root directory of the C# project.
        test_project_dir (str): The directory where test files are stored.
        project_namespace (str): The namespace of the main project.
        tracker (ProgressTracker): A tracker to monitor the progress of test generation and execution.
        reporter: The reporter to use for failure integration.
        jira_epic_key (str | None): The Jira epic key for the tests.
        verbose (bool): Whether to print verbose output.

    Returns:
        bool: True on success (all tests pass).
    """

    file_rel = cs_file["file_relative"]
    file_abs = cs_file["file_absolute"]
    strategy = classify_csharp_file(file_rel)
    test_class_name = infer_test_class_name(file_rel)

    existing_status = tracker.get_status(file_rel)
    if existing_status == "pass":
        print(f"↷ ALREADY PASSED (skipping) — {file_rel}\n")
        return True
    if existing_status == "skip":
        print(f"↷ ALREADY SKIPPED (skipping) — {file_rel}\n")
        return False

    worth_it, reason = should_generate_csharp_test(file_abs, file_rel, verbose=verbose)
    if not worth_it:
        print(f"↷ SKIP — {file_rel}")
        print(f"         Strategy: {strategy}")
        print(f"         Reason: {reason}\n")
        tracker.mark(file_rel, "skip")
        return False

    print(f"✓ GENERATING — {file_rel}")
    print(f"         Strategy: {strategy}")
    print(f"         Reason: {reason}")

    output_path = _get_csharp_test_output_path(file_rel, test_project_dir)
    output_rel = os.path.relpath(output_path, project_root).replace("\\", "/")

    try:
        test_code = generate_csharp_test(
            file_absolute=file_abs,
            file_relative=file_rel,
            project_namespace=project_namespace,
            verbose=verbose,
        )
        save_test(output_path, test_code)
        print(f"        ✓ SAVED — {output_rel}")
    except Exception as e:
        print(f"        ✗ FAILED DURING GENERATION — {file_rel}")
        print(f"         Error: {e}\n")
        tracker.mark(file_rel, "fail")
        return False

    unchanged_count = 0
    last_output = ""

    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        print(f"         Building & running test (attempt {attempt}/{MAX_FIX_ATTEMPTS})...")

        passed, output = run_csharp_test(
            test_project_dir=test_project_dir,
            test_class_name=test_class_name,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )
        last_output = output

        if "INFRA_ERROR:" in output:
            print(f"         INFRA ERROR — {output_rel}")
            print(output)
            tracker.mark(file_rel, "fail")
            return False

        if passed:
            print(f"        ✓ PASS — {output_rel}\n")
            jira_key, pr_url = _run_success_integration(
                reporter=reporter,
                file_rel=file_rel,
                file_abs=file_abs,
                strategy=strategy,
                output_path=output_path,
                test_relative_path_for_runner=output_rel,
                jira_epic_key=jira_epic_key,
                framework_label="xUnit/.NET",
                generator_label="LLM Test Generator (C#)",
                domain_label="backend",
            )
            tracker.mark(file_rel, "pass", jira_key=jira_key, pr_url=pr_url)
            return True

        print(f"        ✗ FAIL — {output_rel}")

        # --- Infrastructure guard: broken <ProjectReference> in GeneratedTests.csproj ---
        # This cannot be fixed by editing the test .cs file; the LLM repair loop
        # would just spin producing identical output forever.  Attempt to auto-repair
        # GeneratedTests.csproj and retry without consuming an LLM attempt.
        if is_project_reference_error(output):
            print(
                "         Broken <ProjectReference> detected in GeneratedTests.csproj — "
                "attempting auto-repair..."
            )
            if repair_project_reference(test_project_dir, project_root):
                print("         ProjectReference(s) repaired. Retrying build...")
                continue  # re-run build without counting this as an LLM attempt
            else:
                csproj_hint = os.path.join(test_project_dir, f"{GENERATED_TESTS_DIR}.csproj")
                print(
                    f"         INFRA ERROR — Could not locate a real .csproj to reference.\n"
                    f"         Please add a <ProjectReference> to {csproj_hint} manually,\n"
                    f"         then delete the progress entry for '{file_rel}' and re-run."
                )
                tracker.mark(file_rel, "fail")
                return False
        # ---------------------------------------------------------------------------------

        error_snippet = extract_relevant_dotnet_error(output)

        print("\n--- ERROR SENT TO REPAIR LOOP ---")
        print(error_snippet)
        print("---------------------------------\n")

        try:
            before_fix = file_text(output_path)

            fixed_test_code = fix_csharp_test(
                file_absolute=file_abs,
                file_relative=file_rel,
                current_test_code=before_fix,
                build_error_output=error_snippet,
                project_namespace=project_namespace,
                verbose=verbose,
            )

            save_test(output_path, fixed_test_code)
            after_fix = file_text(output_path)

            if before_fix == after_fix:
                unchanged_count += 1
                print("        ✗ Repair returned identical code.")
            else:
                unchanged_count = 0
                print("        ✓ Repair changed the file.")

        except Exception as err:
            print(f"        ✗ FAILED DURING REPAIR — {file_rel}")
            print(f"        Error: {err}\n")
            tracker.mark(file_rel, "fail")
            return False

    print(f"        ✗ GAVE UP AFTER {MAX_FIX_ATTEMPTS} ATTEMPTS — {file_rel}")

    analysis = ""
    last_error = extract_relevant_dotnet_error(last_output)
    test_code_on_disk = file_text(output_path) if os.path.exists(output_path) else ""
    try:
        analysis = explain_csharp_test_failure(
            file_absolute=file_abs,
            file_relative=file_rel,
            test_code=test_code_on_disk,
            error_output=last_error,
            verbose=verbose,
        )
        print("\n=== LLM FAILURE ANALYSIS ===")
        print(analysis)
        print("============================\n")
    except Exception as exc:
        print(f"        [LLM] Could not generate failure analysis: {exc}")
        analysis = f"(analysis unavailable: {exc})"

    jira_key = None
    pr_url = None
    if reporter is not None and reporter.enabled:
        try:
            jira_key, pr_url = reporter.report_failure(
                file_relative=file_rel,
                strategy=strategy,
                test_code=test_code_on_disk,
                test_path_in_repo=output_rel,
                last_error=last_error,
                failure_type="dotnet_test_failure",
                llm_analysis=analysis,
                epic_key=jira_epic_key,
                framework_label="xUnit/.NET",
                generator_label="LLM Test Generator (C#)",
                domain_label="backend",
            )
        except Exception as exc:
            print(f"         [Integration] ⚠️  Failure reporting failed: {exc}")

    tracker.mark(file_rel, "fail", jira_key=jira_key, pr_url=pr_url)
    return False


def run_csharp_command(args) -> None:
    """
    Entry point for the run-csharp subcommand.
    
    Args:
        args: The command line arguments.
    """

    # validate API key before doing any work
    validate_api_key()

    project_root = os.path.abspath(args.project)

    if not os.path.isdir(project_root):
        print(f"ERROR: --project path does not exist or is not a directory: {project_root}")
        sys.exit(1)

    print(f"C# project: {project_root}\n")

    jira_epic_key = os.getenv("CSHARP_JIRA_EPIC_KEY") or os.getenv("JIRA_EPIC_KEY") or None

    # bootstrap the GeneratedTests project, repairing broken references if it already exists
    test_project_dir = bootstrap_csharp_test_project(project_root)

    tracker = ProgressTracker(test_project_dir)
    if args.reset_progress:
        tracker.reset()
        print("\nProgress cache cleared. All files will be re-processed.\n")
    else:
        print(f"\nProgress file: {tracker.path}")

    project_namespace = _csharp_detect_project_namespace(project_root)
    print(f"Main project namespace: {project_namespace}\n")

    print()
    reporter = _build_integration_reporter(
        verbose=args.verbose,
        dry_run=args.dry_run,
        no_integration=args.no_integration,
        csharp_mode=True,
    )
    if args.dry_run:
        print("DRY-RUN mode: no external API calls will be made.\n")

    cs_files = find_csharp_files(project_root)
    print(f"=== FOUND {len(cs_files)} C# SOURCE FILES ===\n")

    if not cs_files:
        print("No eligible C# source files found.")
        return

    passed_count = 0
    attempted_count = 0
    integration_summary: list[dict] = []

    for cs_file in cs_files:
        file_rel = cs_file["file_relative"]

        existing_status = tracker.get_status(file_rel)
        if existing_status in ("pass", "skip"):
            label = "ALREADY PASSED" if existing_status == "pass" else "ALREADY SKIPPED"
            print(f"{label} (skipping) — {file_rel}\n")
            continue

        if attempted_count >= args.max_files:
            break

        attempted_count += 1
        result = process_one_csharp_file(
            cs_file=cs_file,
            project_root=project_root,
            test_project_dir=test_project_dir,
            project_namespace=project_namespace,
            tracker=tracker,
            reporter=reporter,
            jira_epic_key=jira_epic_key,
            verbose=args.verbose,
        )
        if result:
            passed_count += 1

        jira_key = tracker.get_jira_key(file_rel)
        pr_url = tracker.get_pr_url(file_rel)
        if jira_key or pr_url:
            integration_summary.append({
                "file": file_rel,
                "status": "pass" if result else "fail",
                "jira_key": jira_key,
                "pr_url": pr_url,
            })

    print("=== C# RUN SUMMARY ===")
    print(f"Processed:               {attempted_count}")
    print(f"Passing generated tests: {passed_count}")
    print(f"Failed/skipped:          {attempted_count - passed_count}")

    if integration_summary:
        print("\n=== C# INTEGRATION SUMMARY ===")
        for item in integration_summary:
            icon = "✅" if item["status"] == "pass" else "⚠️"
            parts = [f"{icon} {item['file']}"]
            if item["jira_key"]:
                parts.append(f"Jira: {item['jira_key']}")
            if item["pr_url"]:
                label = "PR" if item["status"] == "pass" else "Draft PR"
                parts.append(f"{label}: {item['pr_url']}")
            print("  " + " | ".join(parts))


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    # define all command line arguments for the "run" command
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument(
        "--project",
        required=True,
        help="Path to the target React/TypeScript project",
    )
    run_parser.add_argument(
        "--coverage",
        default=DEFAULT_COVERAGE_PATH,
        help="Relative path to coverage-summary.json from project root",
    )
    run_parser.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help="Maximum number of low-coverage files to process in one run",
    )
    run_parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Print full LLM prompts and responses for debugging",
    )
    run_parser.add_argument(
        "--reset-progress",
        action="store_true",
        default=False,
        help="Clear saved progress and reprocess all files from scratch",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Simulate Jira/Bitbucket integration: log what would be created "
            "without making any external API calls"
        ),
    )
    run_parser.add_argument(
        "--no-integration",
        action="store_true",
        default=False,
        help="Disable Jira and Bitbucket integration even if credentials are configured",
    )

    # define command line arguments for the "bootstrap" command
    bootstrap_parser = subparsers.add_parser("bootstrap")
    bootstrap_parser.add_argument(
        "--project",
        required=True,
        help="Path to the target React/TypeScript project",
    )

    # C# backend command
    run_csharp_parser = subparsers.add_parser(
        "run-csharp",
        help="Generate xUnit tests for a C# backend project",
    )
    run_csharp_parser.add_argument(
        "--project",
        required=True,
        help="Path to the folder containing the C# source files",
    )
    run_csharp_parser.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help="Maximum number of C# files to process in one run",
    )
    run_csharp_parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Print full LLM prompts and responses for debugging",
    )
    run_csharp_parser.add_argument(
        "--reset-progress",
        action="store_true",
        default=False,
        help="Clear saved progress and re-process all files from scratch",
    )
    run_csharp_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Simulate Jira/Bitbucket integration: log what would be created "
            "without making any external API calls"
        ),
    )
    run_csharp_parser.add_argument(
        "--no-integration",
        action="store_true",
        default=False,
        help="Disable Jira and Bitbucket integration even if credentials are configured",
    )

    args = parser.parse_args()

    # if the command is "bootstrap" run the Jest bootstrapping process and exit
    if args.command == "bootstrap":
        project_root = os.path.abspath(args.project)
        bootstrap_jest_project(project_root)
        return
    
    # if the command is "run-csharp" run the C# test generation process and exit
    if args.command == "run-csharp":
        run_csharp_command(args)
        return

    # for the "run" command, execute the main test generation pipeline
    validate_api_key()

    project_root = os.path.abspath(args.project)
    jira_epic_key = os.getenv("JIRA_EPIC_KEY") or None

    # sanity check the project path before doing any work
    if not os.path.isdir(project_root):
        print(f"ERROR: --project path does not exist or is not a directory: {project_root}")
        sys.exit(1)
    
    coverage_json_path = os.path.join(project_root, args.coverage)

    print(f"Analyzing project: {project_root}\n")
    profile = analyze_project(project_root)

    print("=== PROJECT PROFILE ===")
    for key, value in profile.items():
        print(f"{key}: {value}")

    if not profile.get("test_command"):
        print("\nERROR: Could not determine a usable test command for this project.")
        print("This project does not appear to have Jest or Vitest configured.")
        print("Please install and configure a supported test runner first.")
        return

    tests_output_dir = ensure_output_dir(project_root)

    tracker = ProgressTracker(tests_output_dir)
    if args.reset_progress:
        tracker.reset()
        print("\nProgress cache cleared. All files will be re-processed.\n")
    else:
        print(f"\nProgress file: {tracker.path}")

    # Build integration reporter (optional)
    print()
    reporter = _build_integration_reporter(
        verbose=args.verbose,
        dry_run=args.dry_run,
        no_integration=args.no_integration,
    )
    if args.dry_run:
        print("DRY-RUN mode: no external API calls will be made.\n")

    # get the list of coverage gaps from the coverage summary or source scan
    gaps = get_coverage_gaps(coverage_json_path, project_root)
    print(f"\n=== FOUND {len(gaps)} COVERAGE GAPS ===\n")

    if not gaps:
        print("No eligible source files or coverage gaps were found.")
        return

    passed_count = 0
    attempted_count = 0
    integration_summary: list[dict] = []

    # process each gap and generate a test for it
    for gap in gaps:
        file_rel = gap["file_relative"]

        # Already handled in a previous run — notify but don't consume quota
        existing_status = tracker.get_status(file_rel)
        if existing_status in ("pass", "skip"):
            label = "ALREADY PASSED" if existing_status == "pass" else "ALREADY SKIPPED"
            print(f"↷ {label} (skipping) — {file_rel}\n")
            continue

        if attempted_count >= args.max_files:
            break

        attempted_count += 1
        # process one gap and generate/fix a test for it
        test_passed = process_one_gap(
            gap=gap,
            project_root=project_root,
            test_command=profile["test_command"],
            tests_output_dir=tests_output_dir,
            tracker=tracker,
            reporter=reporter,
            jira_epic_key=jira_epic_key,
            verbose=args.verbose,
        )
        if test_passed:
            passed_count += 1

        # Collect integration links for the end-of-run summary
        jira_key = tracker.get_jira_key(file_rel)
        pr_url = tracker.get_pr_url(file_rel)
        if jira_key or pr_url:
            integration_summary.append({
                "file": file_rel,
                "status": "pass" if test_passed else "fail",
                "jira_key": jira_key,
                "pr_url": pr_url,
            })

    print("=== RUN SUMMARY ===")
    print(f"Processed: {attempted_count}")
    print(f"Passing generated tests: {passed_count}")
    print(f"Failed/skipped: {attempted_count - passed_count}")

    if integration_summary:
        print("\n=== INTEGRATION SUMMARY ===")
        for item in integration_summary:
            icon = "✅" if item["status"] == "pass" else "⚠️"
            parts = [f"{icon} {item['file']}"]
            if item["jira_key"]:
                parts.append(f"Jira: {item['jira_key']}")
            if item["pr_url"]:
                label = "PR" if item["status"] == "pass" else "Draft PR"
                parts.append(f"{label}: {item['pr_url']}")
            print("  " + " | ".join(parts))


if __name__ == "__main__":
    main()
