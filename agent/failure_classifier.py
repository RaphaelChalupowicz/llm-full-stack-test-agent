import re


def extract_relevant_jest_error(output: str) -> str:
    lines = output.splitlines()

    # Prefer the detailed failure section starting at first bullet failure
    bullet_index = next((i for i, line in enumerate(lines) if line.strip().startswith("● ")), None)
    if bullet_index is not None:
        return "\n".join(lines[max(0, bullet_index - 20): bullet_index + 80])

    fail_index = next((i for i, line in enumerate(lines) if "FAIL" in line), None)
    if fail_index is not None:
        return "\n".join(lines[fail_index:fail_index + 80])

    return "\n".join(lines[-80:])


def classify_failure(error_snippet: str) -> str:
    lowered = error_snippet.lower()

    if "element type is invalid" in lowered:
        return "react_invalid_element"

    if "cannot destructure property" in lowered and "useselector" in lowered:
        return "redux_state_missing"

    if "basename" in lowered or "usecontext" in lowered:
        return "missing_router_context"

    if "tohavebeencalledtimes" in lowered or "received number of calls" in lowered:
        return "fragile_call_count"

    if "unable to find an element with the role" in lowered or "testinglibraryelementerror" in lowered:
        return "dom_query_failure"

    if "found multiple elements with the text" in lowered:
        return "ambiguous_query"

    if "cannot find module" in lowered or "module not found" in lowered:
        return "import_error"

    if "not wrapped in act" in lowered or "warning: an update to" in lowered:
        return "act_warning"

    if "typeerror" in lowered and (
        "cannot read properties of null" in lowered
        or "cannot read properties of undefined" in lowered
        or "is not a function" in lowered
    ):
        return "null_reference"

    if "error ts" in lowered or "typescript" in lowered:
        return "ts_compile"

    return "generic"


def build_repair_hint(failure_type: str) -> str:
    hints = {
        "react_invalid_element": (
            "Likely cause: a mocked React component is undefined due to incorrect default export mocking.\n"
            "Ensure mocks for default-exported components use:\n"
            "{ __esModule: true, default: () => <div /> }\n"
            "Check default vs named imports.\n"
        ),
        "redux_state_missing": (
            "Likely cause: a real child component is using useSelector for a Redux slice missing in test state.\n"
            "Either add the missing slice state or mock the child component.\n"
        ),
        "missing_router_context": (
            "Likely cause: the component tree requires React Router context.\n"
            "Wrap render in MemoryRouter and avoid incomplete router hook spying.\n"
        ),
        "fragile_call_count": (
            "Likely cause: the test asserts an exact call count that is too brittle.\n"
            "Relax to toHaveBeenCalled() unless exact count is essential.\n"
        ),
        "dom_query_failure": (
            "Likely cause: the test is querying the DOM with the wrong selector.\n"
            "Use the rendered DOM snapshot as source of truth.\n"
        ),
        "ambiguous_query": (
            "Likely cause: the test uses getByText for text that appears multiple times.\n"
            "Prefer getByRole with an accessible name, or use getAllByText if intentional.\n"
        ),
        "import_error": (
            "Likely cause: an import path is incorrect or the module is not installed.\n"
            "Verify that all relative import paths are correct from the test file location.\n"
            "For internal src/ imports, count the folder depth and prefix with the correct number of '../'.\n"
            "Do not import modules that are not available (e.g. unused utilities or private internals).\n"
        ),
        "act_warning": (
            "Likely cause: a state update inside the component is not wrapped in act().\n"
            "Wrap async interactions and renders with await act(async () => { ... }).\n"
            "Or use waitFor() to wait for asynchronous DOM updates before asserting.\n"
        ),
        "null_reference": (
            "Likely cause: a variable or mock is undefined/null when the test accesses it.\n"
            "Check that all mocks return the expected shape and that async data is awaited.\n"
            "If mocking a module, ensure the mock factory returns the correct structure.\n"
        ),
    }
    return hints.get(failure_type, "")


def apply_local_failure_fix(current_test_code: str, failure_type: str) -> str | None:
    updated = current_test_code

    if failure_type == "fragile_call_count":
        updated = re.sub(
            r"\.toHaveBeenCalledTimes\(\s*1\s*\)",
            ".toHaveBeenCalled()",
            updated,
        )
        if updated != current_test_code:
            return updated

    return None
