import os
import re
from openai import (
    OpenAI,
    AuthenticationError,
    RateLimitError,
    APIConnectionError,
    APIStatusError,
)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
CLASSIFY_MAX_TOKENS = _env_int("OPENAI_MAX_TOKENS_CLASSIFY", 120)
GENERATE_MAX_TOKENS = _env_int("OPENAI_MAX_TOKENS_GENERATE", 2600)
REPAIR_MAX_TOKENS = _env_int("OPENAI_MAX_TOKENS_REPAIR", 3200)
EXPLAIN_MAX_TOKENS = _env_int("OPENAI_MAX_TOKENS_EXPLAIN", 1500)
GENERATE_TEMPERATURE = _env_float("OPENAI_TEMPERATURE_GENERATE", 0.1)
REPAIR_TEMPERATURE = _env_float("OPENAI_TEMPERATURE_REPAIR", 0)


def _call_llm(client: OpenAI, **kwargs):
    """
    Calls the OpenAI LLM with the specified parameters.
    Args:
        client (OpenAI): The OpenAI client instance.
        **kwargs: The keyword arguments for the LLM call.
    Returns:
        The response from the LLM.
    """

    try:
        return client.chat.completions.create(**kwargs)
    except AuthenticationError:
        raise RuntimeError(
            "OpenAI authentication failed. "
            "Check that OPENAI_API_KEY is set correctly in your .env file."
        )
    except RateLimitError:
        raise RuntimeError(
            "OpenAI rate limit exceeded. Wait a moment and retry, "
            "or reduce DEFAULT_MAX_FILES to process fewer files per run."
        )
    except APIConnectionError as exc:
        raise RuntimeError(f"Could not connect to the OpenAI API: {exc}") from exc
    except APIStatusError as exc:
        raise RuntimeError(
            f"OpenAI API returned an error (HTTP {exc.status_code}): {exc.message}"
        ) from exc


def get_client() -> OpenAI:
    """
    Creates and returns an OpenAI client instance.
    Returns:
        OpenAI: The initialized OpenAI client.
    """

    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    organization = os.getenv("OPENAI_ORGANIZATION")

    kwargs: dict = {}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    if organization:
        kwargs["organization"] = organization

    return OpenAI(**kwargs)


def extract_text(response) -> str:
    content = response.choices[0].message.content
    return (content or "").strip()


def strip_code_fences(text: str) -> str:
    """
    Strips markdown code fences from the given text.
    Args:
        text (str): The input text that may contain markdown code fences.
    Returns:
        str: The text with code fences removed.
    """

    text = text.strip()
    text = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", text)
    text = re.sub(r"\n```$", "", text)
    return text.strip()


def compute_relative_import(file_relative: str) -> str:
    """
    Compute relative import path from:
      tests/generated/<mirrored structure>.test.ts(x)
    to:
      src/<original file>
    """

    stripped = file_relative.replace("src/", "", 1)
    folder_depth = stripped.count("/")
    ups = "../" * (folder_depth + 2)
    no_ext = file_relative.rsplit(".", 1)[0]
    return f"{ups}{no_ext}"


def compute_generated_test_relpath(file_relative: str) -> str:
    """
    Computes the relative path for the generated test file.
    Args:
        file_relative (str): The relative path to the original file.
    Returns:
        str: The relative path to the generated test file.
    """

    stripped = file_relative.replace("src/", "", 1)
    base, ext = stripped.rsplit(".", 1)
    return f"tests/generated/{base}.test.{ext}"


def cleanup_generated_test(code: str, file_relative: str) -> str:
    """
    Cleans up the generated test code by fixing import paths and removing unnecessary boilerplate.
    Args:
        code (str): The generated test code.
        file_relative (str): The relative path to the original file.
    Returns:
        str: The cleaned-up test code.
    """

    code = strip_code_fences(code)

    correct_import = compute_relative_import(file_relative)
    filename_no_ext = os.path.basename(file_relative).rsplit(".", 1)[0]

    # Fix guessed import path of file under test
    code = re.sub(
        rf'from\s+[\'"][^\'"]*{re.escape(filename_no_ext)}[\'"]',
        f"from '{correct_import}'",
        code,
        count=1,
    )

    # Remove redux-thunk imports
    code = re.sub(
        r'^\s*import\s+thunk\s+from\s+[\'"]redux-thunk[\'"];\s*\n?',
        "",
        code,
        flags=re.MULTILINE,
    )

    # Remove bad thunk middleware pattern
    code = re.sub(
        r"middleware:\s*\(\s*getDefaultMiddleware\s*\)\s*=>\s*getDefaultMiddleware\(\)\.concat\(thunk\),?",
        "",
        code,
        flags=re.DOTALL,
    )

    # Fix React import for projects without esModuleInterop
    code = re.sub(
        r"^import React from 'react';",
        "import * as React from 'react';",
        code,
        flags=re.MULTILINE,
    )

    # Relax brittle exact call count assertions
    code = re.sub(
        r"\.toHaveBeenCalledTimes\(\s*1\s*\)",
        ".toHaveBeenCalled()",
        code,
    )

    # Clean excessive spacing
    code = re.sub(r"\n{3,}", "\n\n", code)

    return code.strip() + "\n"


def should_generate_test(file_absolute: str, file_relative: str, verbose: bool = False) -> tuple[bool, str]:
    """
    Determines whether a test should be generated for the given file.
    Args:
        file_absolute (str): The absolute path to the file.
        file_relative (str): The relative path to the file.
        verbose (bool): Whether to print verbose output.
    Returns:
        tuple[bool, str]: A tuple containing a boolean indicating whether to generate a test, and a reason for the decision.
    """

    with open(file_absolute, encoding="utf-8") as f:
        source = f.read()

    client = get_client()

    messages = [
            {
                "role": "system",
                "content": (
                    "You are a senior frontend engineer reviewing whether a file is worth writing tests for. "
                    "You must respond in this exact format:\n"
                    "VERDICT: YES or NO\n"
                    "REASON: one sentence explanation\n\n"
                    "Say NO if the file is any of these:\n"
                    "- A trivial wrapper with no logic\n"
                    "- A pure types/interfaces file\n"
                    "- A constants/assets file\n"
                    "- A config file\n"
                    "- Less than 5 meaningful lines of logic\n\n"
                    "Say YES if the file has:\n"
                    "- Real business logic\n"
                    "- Conditional rendering\n"
                    "- User interactions\n"
                    "- Redux thunks/slices with meaningful behavior\n"
                    "- API calls or error handling\n"
                    "- Auth/permission logic"
                ),
            },
            {
                "role": "user",
                "content": f"File: {file_relative}\n\nSource:\n{source}",
            },
        ]

    if verbose:
        print("\n--- LLM CLASSIFY PROMPT ---")
        for msg in messages:
            print(f"[{msg['role']}]\n{msg['content']}\n")
        print("----------------------------\n")

    response = _call_llm(
        client,
        model=MODEL,
        messages=messages,
        temperature=0,
        max_completion_tokens=CLASSIFY_MAX_TOKENS,
    )

    content = extract_text(response)

    if verbose:
        print(f"\n--- LLM CLASSIFY RESPONSE ---\n{content}\n-----------------------------\n")

    lines = content.splitlines()
    verdict_line = next((line for line in lines if line.startswith("VERDICT:")), "VERDICT: NO")
    reason_line = next((line for line in lines if line.startswith("REASON:")), "REASON: Unknown")

    should_test = "YES" in verdict_line.upper()
    reason = reason_line.replace("REASON:", "").strip()

    return should_test, reason


def build_system_prompt(strategy: str) -> str:
    """
    Builds the system prompt for the LLM based on the testing strategy.
    Args:
        strategy (str): The testing strategy (e.g., 'page', 'component', 'redux_thunk', etc.).
    Returns:
        str: The system prompt.
    """
    
    base = (
        "You are a senior frontend engineer. "
        "You write clean, realistic Jest + React Testing Library tests for React TypeScript projects. "
        "Return ONLY raw test file content. No explanation. No markdown.\n\n"
        "Critical rules:\n"
        "- Never import redux-thunk\n"
        "- Never use `import React from 'react'` - this project does not have esModuleInterop enabled\n"
        "- Always use `import * as React from 'react'` instead\n"
        "- If using configureStore from @reduxjs/toolkit, rely on default middleware only\n"
        "- Do not add middleware: getDefaultMiddleware().concat(thunk)\n"
        "- Keep mocks minimal\n"
        "- Avoid testing implementation details\n"
        "- Avoid exact call count assertions like toHaveBeenCalledTimes(1) unless absolutely required\n"
        "- Prefer toHaveBeenCalled() or toHaveBeenCalledWith(...)\n"
        "- When testing user interactions (button clicks, input changes, form submissions, dropdown selections), "
        "always use fireEvent or userEvent - never skip the interaction and jump straight to assertions\n"
        "- Import fireEvent from '@testing-library/react' or userEvent from '@testing-library/user-event'\n"
        "- Prefer fireEvent.click(element) for simple clicks\n"
        "- Prefer userEvent.type(input, 'text') for typing into inputs\n"
        "- Never assert on the result of an interaction without actually firing the interaction first\n"
    )

    strategy_rules = {
        "page": (
            "\nPage testing rules:\n"
            "- Prefer shell/integration-lite tests\n"
            "- Use MemoryRouter when router features are present\n"
            "- Prefer lightweight child component mocks for complex child trees\n"
            "- If mocking default-exported React components, use:\n"
            "  { __esModule: true, default: () => <div /> }\n"
            "- Do not invent UI text that is not present in the source\n"
            "- Do not assume third-party pagination DOM unless visible in the source\n"
        ),
        "component": (
            "\nComponent testing rules:\n"
            "- Use RTL render and interaction-based assertions\n"
            "- Focus on visible behavior and user interactions\n"
            "- If mocking child components, keep them valid React components\n"
        ),
        "redux_thunk": (
            "\nRedux thunk testing rules:\n"
            "- Dispatch thunks through a real store created with configureStore\n"
            "- Prefer asserting using thunk.fulfilled.match(result) and thunk.rejected.match(result)\n"
            "- Use jest.spyOn for service mocks\n"
        ),
        "redux_slice": (
            "\nRedux slice testing rules:\n"
            "- Test reducer behavior directly\n"
            "- Prefer pure reducer/action tests over rendering\n"
        ),
        "service": (
            "\nService testing rules:\n"
            "- Mock axios or external calls directly\n"
            "- Assert request/response behavior and error handling\n"
        ),
        "util": (
            "\nUtility testing rules:\n"
            "- Write direct input/output tests\n"
            "- Prefer pure function tests with clear edge cases\n"
        ),
    }

    return base + strategy_rules.get(strategy, "")


def build_user_prompt(
    *,
    file_relative: str,
    source_code: str,
    strategy: str,
) -> str:
    """
    Builds the user prompt for the LLM based on the file and testing strategy.
    Args:
        file_relative (str): The relative path to the file being tested.
        source_code (str): The source code of the file being tested.
        strategy (str): The testing strategy (e.g., 'page', 'component', 'redux_thunk', etc.).
    Returns:
        str: The user prompt.
    """

    correct_import = compute_relative_import(file_relative)
    stripped = file_relative.replace("src/", "", 1)
    folder_depth = stripped.count("/")
    ups = "../" * (folder_depth + 2)
    test_path = compute_generated_test_relpath(file_relative)

    strategy_specific = {
        "redux_thunk": (
            "If the file contains Redux createAsyncThunk logic:\n"
            "- Create a store with configureStore({ reducer: (state = {}) => state })\n"
            "- Do NOT import redux-thunk\n"
            "- Dispatch thunks through the store\n"
            "- Assert service calls and fulfilled/rejected result shape\n"
        ),
        "page": (
            "If the page renders complex child components:\n"
            "- Prefer lightweight mocks for child components that are not the main subject of the test\n"
            "- If router is used, wrap with MemoryRouter\n"
            "- If Redux child components require unrelated slice state, prefer mocking those children instead of expanding store state too much\n"
        ),
        "component": (
            "Prefer user-facing assertions and real interactions over implementation-detail assertions.\n"
        ),
        "service": (
            "Mock network dependencies directly and assert returned values and thrown errors.\n"
        ),
        "redux_slice": (
            "Prefer reducer tests over render tests.\n"
        ),
        "util": (
            "Prefer simple input/output assertions and edge case coverage.\n"
        ),
    }.get(strategy, "")

    return (
        f"Write a complete Jest + React Testing Library test file.\n"
        f"File being tested: {file_relative}\n"
        f"Detected strategy: {strategy}\n"
        f"Test file location: {test_path}\n\n"

        f"IMPORTANT - import rules:\n"
        f"- Import the file under test EXACTLY like this: from '{correct_import}'\n"
        f"- For all other internal src imports, go up {folder_depth + 2} levels first, then into src/\n"
        f"- Base prefix for internal imports: '{ups}src/'\n\n"

        f"IMPORTANT - testing libraries:\n"
        f"- Use @testing-library/react\n"
        f"- Use @testing-library/jest-dom\n"
        f"- Mock axios only if needed\n\n"

        f"{strategy_specific}\n"
        f"Return ONLY the raw test file code.\n\n"
        f"Source:\n{source_code}"
    )


def generate_test(
    file_absolute: str,
    file_relative: str,
    strategy: str,
    verbose: bool = False,
) -> str:
    """
    Generates a test file for the given source file and testing strategy.
    Args:
        file_absolute (str): The absolute path to the source file.
        file_relative (str): The relative path to the source file.
        strategy (str): The testing strategy (e.g., 'page', 'component', 'redux_thunk', etc.).
        verbose (bool): Whether to print verbose output.
    Returns:
        str: The generated test code.
    """

    with open(file_absolute, encoding="utf-8") as f:
        source_code = f.read()

    client = get_client()

    system_prompt = build_system_prompt(strategy)
    user_prompt = build_user_prompt(
        file_relative=file_relative,
        source_code=source_code,
        strategy=strategy,
    )

    if verbose:
        print("\n--- LLM GENERATE PROMPT ---")
        print(f"[system]\n{system_prompt}\n")
        print(f"[user]\n{user_prompt}\n")
        print("----------------------------\n")

    response = _call_llm(
        client,
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=GENERATE_TEMPERATURE,
        max_completion_tokens=GENERATE_MAX_TOKENS,
    )

    raw = extract_text(response)

    if verbose:
        print(f"\n--- LLM GENERATE RESPONSE ---\n{raw}\n-----------------------------\n")

    return cleanup_generated_test(raw, file_relative)


def fix_test_after_failure(
    *,
    file_absolute: str,
    file_relative: str,
    current_test_code: str,
    jest_error_output: str,
    failure_type: str,
    repair_hint: str,
    verbose: bool = False,
) -> str:
    """
    Fixes a failing Jest + TypeScript test.
    Args:
        file_absolute (str): The absolute path to the file under test.
        file_relative (str): The relative path to the file under test.
        current_test_code (str): The current test code.
        jest_error_output (str): The error output from Jest.
        failure_type (str): The type of failure.
        repair_hint (str): A hint for repairing the test.
        verbose (bool): Whether to print verbose output.
    Returns:
        str: The fixed test code.
    """

    with open(file_absolute, encoding="utf-8") as f:
        source_code = f.read()

    client = get_client()
    correct_import = compute_relative_import(file_relative)
    test_path = compute_generated_test_relpath(file_relative)

    system_prompt = (
        "You are fixing a failing Jest + TypeScript test.\n"
        "Return ONLY the corrected full test file content.\n"
        "No markdown. No explanation.\n\n"

        "CRITICAL RULES:\n"
        "- The current test is failing - you MUST modify it\n"
        "- Do NOT return identical code\n"
        "- If unsure, simplify the test to make it pass\n"
        "- Prefer correctness over completeness\n"
        "- You may remove or rewrite broken assertions\n"
        "- Use the Jest error output as the source of truth\n"
        "- When testing user interactions (button clicks, input changes, form submissions, dropdown selections), "
        "always use fireEvent or userEvent - never skip the interaction and jump straight to assertions\n"
        "- Import fireEvent from '@testing-library/react' or userEvent from '@testing-library/user-event'\n"
        "- Prefer fireEvent.click(element) for simple clicks\n"
        "- Prefer userEvent.type(input, 'text') for typing into inputs\n"
        "- Never assert on the result of an interaction without actually firing the interaction first\n\n"

        "Technical rules:\n"
        "- Never import redux-thunk\n"
        "- Use configureStore with default middleware only\n"
        "- Avoid exact call count assertions like toHaveBeenCalledTimes(1)\n"
        "- Prefer stable assertions over brittle ones\n"
        "- Keep mocks minimal and correct\n\n"

        "Failure-specific guidance:\n"
        "- If the Jest error says 'Element type is invalid', assume a React component import or mock is undefined\n"
        "- For default-exported mocked React components, prefer:\n"
        "  { __esModule: true, default: () => <div /> }\n"
        "- If the error indicates missing router context, wrap renders in MemoryRouter\n"
        "- If the error indicates missing Redux slice state in child components, either add the missing slice state or mock the child component\n"
        "- If the failure is a Testing Library query failure, use the rendered DOM snapshot as source of truth\n"
    )

    user_prompt = (
        f"File under test: {file_relative}\n"
        f"Expected import path for file under test: from '{correct_import}'\n"
        f"Generated test file path: {test_path}\n"
        f"Failure type: {failure_type}\n\n"
        f"Additional context:\n{repair_hint}\n\n"
        f"Source file under test:\n{source_code}\n\n"
        f"Current failing test:\n{current_test_code}\n\n"
        f"Jest error output:\n{jest_error_output}"
    )

    if verbose:
        print("\n--- LLM REPAIR PROMPT ---")
        print(f"[system]\n{system_prompt}\n")
        print(f"[user]\n{user_prompt}\n")
        print("-------------------------\n")

    response = _call_llm(
        client,
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=REPAIR_TEMPERATURE,
        max_completion_tokens=REPAIR_MAX_TOKENS,
    )

    raw = extract_text(response)

    if verbose:
        print(f"\n--- LLM REPAIR RESPONSE ---\n{raw}\n---------------------------\n")

    return cleanup_generated_test(raw, file_relative)


def explain_test_failure(
    *,
    file_absolute: str,
    file_relative: str,
    test_code: str,
    jest_error: str,
    failure_type: str,
    verbose: bool = False,
) -> str:
    """
    Provides a detailed explanation for why a generated test is failing, along with actionable guidance for fixing it.

    Args:
        file_absolute: The absolute path to the source file under test.
        file_relative: The relative path to the source file under test.
        test_code: The code of the generated test that failed.
        jest_error: The Jest error output.
        failure_type: A classification of the failure type.
        verbose: A boolean indicating whether to print verbose output.
    Returns:
        str: A markdown-formatted string explaining the failure and providing guidance for fixing it.
    """

    with open(file_absolute, encoding="utf-8") as f:
        source_code = f.read()

    client = get_client()

    user_content = (
        f"A Jest test was automatically generated for `{file_relative}` but "
        f"failed to pass after all repair attempts.\n\n"
        f"**Source file under test:**\n```tsx\n{source_code}\n```\n\n"
        f"**Last generated test (that failed):**\n```tsx\n{test_code}\n```\n\n"
        f"**Jest error output:**\n```\n{jest_error}\n```\n\n"
        f"**Failure classification:** {failure_type}\n\n"
        f"Please provide:\n"
        f"1. **Root cause** — why is this test failing?\n"
        f"2. **Key challenges** — what makes this file hard to test automatically?\n"
        f"3. **Recommended approach** — step-by-step guide for a developer to "
        f"write a passing test manually\n"
        f"4. **Code patterns** — specific mocks, utilities, or techniques to use\n"
        f"5. **Minimum viable test** — a minimal test case likely to pass as a "
        f"starting point\n\n"
        f"Be specific, practical, and concise.  A developer should be able to act "
        f"on this within 30 minutes."
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are a senior React/TypeScript testing expert helping a developer "
                "understand why an automatically generated test failed and how to fix it. "
                "Use markdown formatting (headings, code blocks) for readability."
            ),
        },
        {"role": "user", "content": user_content},
    ]

    if verbose:
        print("\n--- LLM EXPLAIN FAILURE PROMPT ---")
        print(f"[user]\n{user_content}\n")
        print("----------------------------------\n")

    response = _call_llm(
        client,
        model=MODEL,
        messages=messages,
        temperature=0.2,
        max_completion_tokens=EXPLAIN_MAX_TOKENS,
    )

    analysis = extract_text(response)

    if verbose:
        print(f"\n--- LLM EXPLAIN FAILURE RESPONSE ---\n{analysis}\n------------------------------------\n")

    return analysis
