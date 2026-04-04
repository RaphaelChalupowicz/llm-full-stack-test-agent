"""
LLM-based test generation and repair for C# source files.
Generates xUnit 2.x tests (with Moq for mocking) using the OpenAI API.
"""

import os
import re
from openai import (
    OpenAI,
    AuthenticationError,
    RateLimitError,
    APIConnectionError,
    APIStatusError,
)
from csharp_parser import parse_csharp_file, classify_csharp_file


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
GENERATE_MAX_TOKENS = _env_int("CSHARP_MAX_TOKENS_GENERATE", _env_int("OPENAI_MAX_TOKENS_GENERATE", 2600))
REPAIR_MAX_TOKENS = _env_int("CSHARP_MAX_TOKENS_REPAIR", _env_int("OPENAI_MAX_TOKENS_REPAIR", 3200))
GENERATE_TEMPERATURE = _env_float("OPENAI_TEMPERATURE_GENERATE", 0.1)
REPAIR_TEMPERATURE = _env_float("OPENAI_TEMPERATURE_REPAIR", 0)


def _call_llm(client: OpenAI, **kwargs):
    """
    Wrapper around chat.completions.create with structured error handling.

    Args:
        client (OpenAI): An instance of the OpenAI API client.
        **kwargs: The keyword arguments to pass to chat.com
    Returns:
        The response from the OpenAI API.
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
    Create and return an OpenAI API client instance.
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
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", text)
    text = re.sub(r"\n```$", "", text)
    return text.strip()


def infer_test_namespace(file_relative: str, project_namespace: str) -> str:
    """
    Derive a sensible test namespace from the source file path.

    E.g. ``Controllers/UserController.cs`` + ``MyApp``
    → ``MyApp.GeneratedTests.Controllers``

    Args:
        file_relative (str): The relative path of the source file within the project.
        project_namespace (str): The root namespace of the main project, used as a prefix for the test namespace.
    Returns:
        str: The inferred namespace for the generated test class.
    """

    parts = file_relative.replace("\\", "/").split("/")
    # Drop the filename itself
    folders = parts[:-1]
    if folders:
        return f"{project_namespace}.GeneratedTests.{'.'.join(folders)}"
    return f"{project_namespace}.GeneratedTests"


def infer_test_class_name(file_relative: str) -> str:
    """
    Derive the test class name from the source filename.
    ``Controllers/UserController.cs`` → ``UserControllerTests``

    Args:
        file_relative (str): The relative path of the source file within the project.
    Returns:
        str: The inferred test class name.
    """

    base = os.path.basename(file_relative)
    stem = base.replace(".cs", "").replace(".CS", "")
    return f"{stem}Tests"


def build_system_prompt(strategy: str) -> str:
    """
    Build the system prompt for the LLM based on the testing strategy.

    Args:
        strategy (str): The testing strategy category (e.g. "controller", "service", "utility").
    Returns:
        str: The system prompt to guide the LLM in generating appropriate tests.
    """

    base = (
        "You are a senior C# backend engineer. "
        "You write clean, well-structured xUnit 2.x unit tests for .NET projects. "
        "Return ONLY the raw C# test file content — no explanation, no markdown fences.\n\n"
        "General rules:\n"
        "- Use xUnit attributes: [Fact] for simple tests, [Theory] + [InlineData] for parameterised tests\n"
        "- Follow the Arrange / Act / Assert pattern with blank lines between sections\n"
        "- Use Moq to mock interfaces and abstract classes\n"
        "- Use meaningful, descriptive test method names (e.g. MethodName_StateUnderTest_ExpectedBehavior)\n"
        "- Do not test private/internal methods directly\n"
        "- Prefer testing observable behaviour over implementation details\n"
        "- Emit only the 'using' directives that are actually required by the generated code\n"
        "- Prefer FluentAssertions-style assertions if the project already uses them, "
        "otherwise fall back to plain xUnit Assert.*\n"
        "- When a method is async, the test method must also be async Task\n"
        "- If the class under test depends on ILogger<T>, mock it with Mock<ILogger<T>>()\n"
        "- Do not reference Microsoft.AspNetCore.Mvc.Testing unless you add it to the test project\n"
    )

    strategy_rules = {
        "controller": (
            "\nController testing rules:\n"
            "- Instantiate the controller directly (not via WebApplicationFactory)\n"
            "- Mock all service/repository dependencies with Moq\n"
            "- Assert the IActionResult type (OkObjectResult, NotFoundResult, BadRequestResult, etc.)\n"
            "- Assert the Value property where appropriate\n"
            "- Test both happy-path and error/not-found paths\n"
        ),
        "service": (
            "\nService testing rules:\n"
            "- Mock all repository and external-service dependencies\n"
            "- Test business logic branches (null inputs, empty collections, exceptions)\n"
            "- Verify mock interactions with .Verify() when side-effects matter\n"
        ),
        "repository": (
            "\nRepository testing rules:\n"
            "- Use an in-memory DbContext (Microsoft.EntityFrameworkCore.InMemory) when possible\n"
            "- Seed minimal data needed for each test\n"
            "- Do not connect to a real database\n"
        ),
        "middleware": (
            "\nMiddleware testing rules:\n"
            "- Use DefaultHttpContext to construct a minimal pipeline\n"
            "- Verify that the next delegate is (or is not) called as expected\n"
        ),
        "validator": (
            "\nValidator testing rules:\n"
            "- Test both valid and invalid inputs\n"
            "- Cover all validation rules declared in the validator\n"
        ),
        "handler": (
            "\nHandler testing rules:\n"
            "- Test with realistic command/query objects\n"
            "- Mock infrastructure dependencies\n"
            "- Assert both return values and side-effects\n"
        ),
        "utility": (
            "\nUtility / helper testing rules:\n"
            "- Write focused, pure input/output tests\n"
            "- Cover edge cases: null, empty string, boundary values\n"
        ),
        "model": (
            "\nModel / DTO testing rules:\n"
            "- Test any custom validation attributes or business-rule methods present\n"
            "- Skip if the class is a plain data-bag with no logic\n"
        ),
    }

    return base + strategy_rules.get(strategy, "")


def build_user_prompt(
    *,
    file_relative: str,
    source_code: str,
    strategy: str,
    metadata: dict,
    test_namespace: str,
    test_class_name: str,
    project_namespace: str,
) -> str:
    """
    Build the user prompt for the LLM based on the file and testing context.
    Args:
        file_relative (str): The file path relative to the project root, e.g. "Controllers/UserController.cs".
        source_code (str): The full source code of the file to generate tests for.
        strategy (str): The testing strategy category (e.g. "controller", "service", "utility").
        metadata (dict): Metadata about the file, including namespace, classes, and methods.
        test_namespace (str): The namespace for the generated test class.
        test_class_name (str): The name of the generated test class.
        project_namespace (str): The root namespace of the main project, used as a prefix for the test namespace.
    Returns:
        str: The user prompt to guide the LLM in generating the test file.
    """

    namespace = metadata.get("namespace", "")
    classes = metadata.get("classes", [])
    methods = metadata.get("public_methods", [])

    context_lines = [f"Source file: {file_relative}"]
    if namespace:
        context_lines.append(f"Namespace: {namespace}")
    if classes:
        context_lines.append(f"Classes: {', '.join(classes)}")
    if methods:
        context_lines.append(f"Public methods: {', '.join(methods[:20])}")
    context_lines.append(f"Test namespace: {test_namespace}")
    context_lines.append(f"Test class name: {test_class_name}")
    if project_namespace:
        context_lines.append(f"Main project namespace: {project_namespace}")

    context_block = "\n".join(context_lines)

    return (
        f"{context_block}\n\n"
        f"Source code:\n```csharp\n{source_code}\n```\n\n"
        f"Generate a complete xUnit test file for the class(es) above. "
        f"The test class must be named '{test_class_name}' "
        f"and placed in namespace '{test_namespace}'."
    )


def cleanup_generated_csharp_test(code: str) -> str:
    """
    Clean up the raw code generated by the LLM.

    Args:
        code (str): The raw C# code generated by the LLM, which may contain markdown formatting or extraneous whitespace.
    Returns:
        str: The cleaned C# code.
    """

    code = strip_code_fences(code)
    # Remove stray markdown emphasis inside code
    code = re.sub(r"\*\*(.*?)\*\*", r"\1", code)
    # Collapse runs of 3+ blank lines to 2
    code = re.sub(r"\n{3,}", "\n\n", code)
    return code.strip() + "\n"


def should_generate_csharp_test(
    file_absolute: str,
    file_relative: str,
    verbose: bool = False,
) -> tuple[bool, str]:
    """
    Ask the LLM whether a C# source file is worth writing tests for.
    
    Args:
        file_absolute (str): The absolute path to the C# source file.
        file_relative (str): The file path relative to the project root.
        verbose (bool): Whether to print detailed information about the LLM's decision.
    Returns:
        tuple[bool, str]: A tuple containing whether to generate tests and the reason for the decision.
    """

    with open(file_absolute, encoding="utf-8", errors="replace") as f:
        source = f.read()

    client = get_client()

    messages = [
        {
            "role": "system",
            "content": (
                "You are a senior C# backend engineer reviewing whether a file is worth writing unit tests for.\n"
                "Respond in this exact format:\n"
                "VERDICT: YES or NO\n"
                "REASON: one sentence explanation\n\n"
                "Say NO if the file is:\n"
                "- A plain POCO / DTO / record with no logic\n"
                "- A pure interface or abstract class with no implementation\n"
                "- A constants file\n"
                "- A migration or EF Core model snapshot\n"
                "- Less than 5 meaningful lines of logic\n"
                "- Program.cs or Startup.cs bootstrapping code\n\n"
                "Say YES if the file contains:\n"
                "- Real business logic or calculations\n"
                "- Controller action methods\n"
                "- Service methods that coordinate logic\n"
                "- Repository queries with filtering or mapping\n"
                "- Middleware logic\n"
                "- Validation rules\n"
                "- Error handling"
            ),
        },
        {
            "role": "user",
            "content": f"File: {file_relative}\n\nSource:\n{source}",
        },
    ]

    if verbose:
        print("\n--- LLM CLASSIFY PROMPT (C#) ---")
        for m in messages:
            print(f"[{m['role']}]\n{m['content']}\n")
        print("--------------------------------\n")

    response = _call_llm(
        client,
        model=MODEL,
        messages=messages,
        temperature=0,
        max_completion_tokens=CLASSIFY_MAX_TOKENS,
    )

    content = extract_text(response)

    if verbose:
        print(f"\n--- LLM CLASSIFY RESPONSE (C#) ---\n{content}\n----------------------------------\n")

    lines = content.splitlines()
    verdict_line = next((l for l in lines if l.upper().startswith("VERDICT:")), "VERDICT: NO")
    reason_line = next((l for l in lines if l.upper().startswith("REASON:")), "REASON: Unknown")

    should_test = "YES" in verdict_line.upper()
    reason = reason_line.split(":", 1)[-1].strip()

    return should_test, reason


def generate_csharp_test(
    *,
    file_absolute: str,
    file_relative: str,
    project_namespace: str,
    verbose: bool = False,
) -> str:
    """
    Generate a complete xUnit test file for the given C# source file.
    
    Args:
        file_absolute (str): The absolute path to the C# source file.
        file_relative (str): The file path relative to the project root.
        project_namespace (str): The namespace of the project.
        verbose (bool): Whether to print detailed information about the LLM's decision.
    Returns:
        str: The generated C# test file content.
    """
    
    metadata = parse_csharp_file(file_absolute)
    strategy = classify_csharp_file(file_relative)
    test_namespace = infer_test_namespace(
        file_relative, project_namespace or metadata.get("namespace", "GeneratedTests")
    )
    test_class_name = infer_test_class_name(file_relative)

    system_prompt = build_system_prompt(strategy)
    user_prompt = build_user_prompt(
        file_relative=file_relative,
        source_code=metadata["source"],
        strategy=strategy,
        metadata=metadata,
        test_namespace=test_namespace,
        test_class_name=test_class_name,
        project_namespace=project_namespace,
    )

    client = get_client()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    if verbose:
        print("\n--- LLM GENERATE PROMPT (C#) ---")
        for m in messages:
            print(f"[{m['role']}]\n{m['content']}\n")
        print("---------------------------------\n")

    response = _call_llm(
        client,
        model=MODEL,
        messages=messages,
        temperature=GENERATE_TEMPERATURE,
        max_completion_tokens=GENERATE_MAX_TOKENS,
    )

    raw = extract_text(response)

    if verbose:
        print(f"\n--- LLM GENERATE RESPONSE (C#) ---\n{raw}\n-----------------------------------\n")

    return cleanup_generated_csharp_test(raw)


def fix_csharp_test(
    *,
    file_absolute: str,
    file_relative: str,
    current_test_code: str,
    build_error_output: str,
    project_namespace: str,
    verbose: bool = False,
) -> str:
    """
    Ask the LLM to fix a failing C# test given the compiler/test error output.
    
    Args:
        file_absolute (str): The absolute path to the C# source file.
        file_relative (str): The file path relative to the project root.
        current_test_code (str): The current content of the test file.
        build_error_output (str): The error output from the build/test process.
        project_namespace (str): The namespace of the project.
        verbose (bool): Whether to print detailed information about the LLM's decision.
    Returns:
        str: The fixed C# test file content.
    """

    metadata = parse_csharp_file(file_absolute)

    messages = [
        {
            "role": "system",
            "content": (
                "You are a senior C# backend engineer. "
                "Fix the provided xUnit test file so that it compiles and all tests pass. "
                "Return ONLY the complete fixed C# test file — no explanation, no markdown fences.\n\n"
                "Rules:\n"
                "- Do not remove tests; fix them\n"
                "- Correct any wrong using directives\n"
                "- Fix any Moq setup errors\n"
                "- If a method is async, the test must also be async Task\n"
                "- Do not add external packages that are not already available in a standard "
                "xUnit + Moq project\n"
                "- If the error is about a missing member, adjust the test to use the correct API\n"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Source file: {file_relative}\n\n"
                f"Source code:\n```csharp\n{metadata['source']}\n```\n\n"
                f"Current failing test file:\n```csharp\n{current_test_code}\n```\n\n"
                f"Build / test error output:\n```\n{build_error_output}\n```\n\n"
                "Please return the complete fixed test file."
            ),
        },
    ]

    if verbose:
        print("\n--- LLM REPAIR PROMPT (C#) ---")
        for m in messages:
            print(f"[{m['role']}]\n{m['content']}\n")
        print("------------------------------\n")

    client = get_client()
    response = _call_llm(
        client,
        model=MODEL,
        messages=messages,
        temperature=REPAIR_TEMPERATURE,
        max_completion_tokens=REPAIR_MAX_TOKENS,
    )

    raw = extract_text(response)

    if verbose:
        print(f"\n--- LLM REPAIR RESPONSE (C#) ---\n{raw}\n--------------------------------\n")

    return cleanup_generated_csharp_test(raw)


def explain_csharp_test_failure(
    *,
    file_absolute: str,
    file_relative: str,
    test_code: str,
    error_output: str,
    verbose: bool = False,
) -> str:
    """
    Ask the LLM to provide a human-readable explanation of why a C# test failed.
    
    Args:
        file_absolute (str): The absolute path to the C# source file.
        file_relative (str): The file path relative to the project root.
        test_code (str): The content of the test file.
        error_output (str): The error output from the test run.
        verbose (bool): Whether to print detailed information about the LLM's decision.
    Returns:
        str: A human-readable explanation of the test failure.
    """
    metadata = parse_csharp_file(file_absolute)

    messages = [
        {
            "role": "system",
            "content": (
                "You are a senior C# backend engineer. "
                "Analyse the provided test failure and give a concise explanation:\n"
                "1. Root cause of the failure\n"
                "2. What needs to change in the test or source code\n"
                "3. Any architectural observations\n"
                "Be brief and actionable."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Source file: {file_relative}\n\n"
                f"Source code:\n```csharp\n{metadata['source']}\n```\n\n"
                f"Test file:\n```csharp\n{test_code}\n```\n\n"
                f"Error output:\n```\n{error_output}\n```"
            ),
        },
    ]

    if verbose:
        print("\n--- LLM EXPLAIN PROMPT (C#) ---")
        for m in messages:
            print(f"[{m['role']}]\n{m['content']}\n")
        print("-------------------------------\n")

    client = get_client()
    response = _call_llm(
        client,
        model=MODEL,
        messages=messages,
        temperature=0,
        max_completion_tokens=_env_int("OPENAI_MAX_TOKENS_EXPLAIN", 1500),
    )

    raw = extract_text(response)

    if verbose:
        print(f"\n--- LLM EXPLAIN RESPONSE (C#) ---\n{raw}\n---------------------------------\n")

    return raw