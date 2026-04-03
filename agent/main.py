import argparse
import importlib.util
import os
import sys
from pathlib import Path
from strategy_selector import select_strategy
from test_generator import (
    should_generate_test,
    generate_test,
    fix_test_after_failure,
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

# exit early if OpenAI library is not installed, since it's required for the agent to run at all
def validate_api_key() -> None:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key or api_key == "your_api_key_here":
        print("ERROR: OPENAI_API_KEY is not configured.")
        print("Set it in your .env file or as an environment variable.")
        print("Example: OPENAI_API_KEY=sk-...")
        sys.exit(1)


# ensure the output directory for generated tests exists, and return its path
def ensure_output_dir(project_root: str) -> str:
    output_dir = os.path.join(project_root, "tests", "generated")
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


# return the text content of a file given its path
def file_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# save the generated test code to the specified output path
def save_test(output_path: str, test_code: str) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(test_code)


# given a source file path, return the corresponding test file path
def get_test_filename(file_rel: str) -> str:
    relative_no_src = file_rel.replace("src/", "", 1)
    base, ext = os.path.splitext(relative_no_src)
    return f"{base}.test{ext}"



def process_one_gap(
    *,
    gap: dict,
    project_root: str,
    test_command: list[str],
    tests_output_dir: str,
    verbose: bool = False,
) -> bool:
    file_rel = gap["file_relative"]
    file_abs = gap["file_absolute"]

    # select a testing strategy based on the file location and name
    strategy = select_strategy(file_rel)

    # determine if it is worth generating a test for a file and get the reason for the decision
    worth_it, reason = should_generate_test(file_abs, file_rel, verbose=verbose)

    if not worth_it:
        print(f"✗ SKIP - {file_rel}")
        print(f"         Strategy: {strategy}")
        print(f"         Reason: {reason}\n")
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
        print(f"         Saved -> {test_relative_path_for_runner}")
    except Exception as err:
        print(f"         FAILED DURING GENERATION - {file_rel}")
        print(f"         Error: {err}\n")
        return False

    unchanged_count = 0

    # attempt to run the test and fix it if it fails
    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        print(f"         Running test (attempt {attempt}/{MAX_FIX_ATTEMPTS})...")

        # run the test and capture whether it passed and its output
        passed, output = run_single_test(
            project_root=project_root,
            test_command=test_command,
            test_relative_path=test_relative_path_for_runner,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )
        
        if "INFRA_ERROR:" in output:
            print(f"         INFRA ERROR - {test_relative_path_for_runner}")
            print(output)
            return False

        if passed:
            print(f"         PASS - {test_relative_path_for_runner}\n")
            return True

        print(f"         FAIL - {test_relative_path_for_runner}")

        error_snippet = extract_relevant_jest_error(output) # extract the most relevant portion of the Jest error output for classification and repair
        failure_type = classify_failure(error_snippet) # classify the failure to determine the most likely cause and best repair approach
        repair_hint = build_repair_hint(failure_type) # build a repair hint based on the failure type to guide the LLM in fixing the test

        print(f"         Failure type: {failure_type}")
        print("\n--- ERROR SENT TO REPAIR LOOP ---")
        print(error_snippet)
        print("---------------------------------\n")

        try:
            before_fix = file_text(output_path)

            local_fix = apply_local_failure_fix(before_fix, failure_type) # attempt to apply a local, deterministic fix based on the failure type
            if local_fix is not None and local_fix != before_fix:
                save_test(output_path, local_fix)
                print("         Applied local deterministic fix.")
                continue

            if unchanged_count >= 1:
                print("         Forcing simplified repair...")
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
                print("         Repair returned identical code.")
            else:
                unchanged_count = 0
                print("         Repair changed the file.")

        except Exception as err:
            print(f"         FAILED DURING REPAIR - {file_rel}")
            print(f"         Error: {err}\n")
            return False

    print(f"         GAVE UP AFTER {MAX_FIX_ATTEMPTS} ATTEMPTS - {file_rel}\n")
    return False


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

    # define command line arguments for the "bootstrap" command
    bootstrap_parser = subparsers.add_parser("bootstrap")
    bootstrap_parser.add_argument(
        "--project",
        required=True,
        help="Path to the target React/TypeScript project",
    )

    args = parser.parse_args()

    # if the command is "bootstrap" run the Jest bootstrapping process and exit
    if args.command == "bootstrap":
        project_root = os.path.abspath(args.project)
        bootstrap_jest_project(project_root)
        return

    validate_api_key()

    project_root = os.path.abspath(args.project)
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

    # get the list of coverage gaps from the coverage summary or source scan
    gaps = get_coverage_gaps(coverage_json_path, project_root)
    print(f"\n=== FOUND {len(gaps)} COVERAGE GAPS ===\n")

    if not gaps:
        print("No eligible source files or coverage gaps were found.")
        return

    passed_count = 0
    attempted_count = 0

    # process each gap and generate a test for it
    for gap in gaps:
        if attempted_count >= args.max_files:
            break

        attempted_count += 1
        # process one gap and generate/fix a test for it
        test_passed = process_one_gap(
            gap=gap,
            project_root=project_root,
            test_command=profile["test_command"],
            tests_output_dir=tests_output_dir,
            verbose=args.verbose,
        )
        if test_passed:
            passed_count += 1

    print("=== RUN SUMMARY ===")
    print(f"Processed: {attempted_count}")
    print(f"Passing generated tests: {passed_count}")
    print(f"Failed/skipped: {attempted_count - passed_count}")


if __name__ == "__main__":
    main()
