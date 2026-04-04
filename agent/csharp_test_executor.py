"""
Builds and executes C# xUnit tests inside the GeneratedTests project using
the `dotnet` CLI.
"""

import os
import re
import subprocess
import sys


def _run_dotnet(
    args: list[str],
    cwd: str,
    timeout_seconds: int = 120,
    extra_env: dict | None = None,
) -> tuple[int, str]:
    """
    Run a ``dotnet`` command and return ``(returncode, combined_output)``.
    Never raises on non-zero exit - callers decide how to handle failures.

    Args:
        args (list[str]): The arguments to pass to the dotnet CLI, e.g. ["build"] or ["test", "--filter", "FullyQualifiedName~MyTestClass"].
        cwd (str): The working directory in which to execute the command.
        timeout_seconds (int): The maximum time to wait for the command to complete before killing it.
        extra_env (dict | None): Additional environment variables to set for the command, merged with the default environment.
    Returns:
        tuple[int, str]: A tuple of (returncode, combined_output) where returncode is the exit code of the command and combined_output is the combined stdout and stderr.
    """

    env = os.environ.copy()
    # Suppress the dotnet welcome/telemetry banner
    env["DOTNET_NOLOGO"] = "1"
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    if extra_env:
        env.update(extra_env)

    command = ["dotnet"] + args

    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,
            env=env,
        )
    except FileNotFoundError:
        msg = (
            "INFRA_ERROR: 'dotnet' CLI not found on PATH.\n"
            f"Python executable: {sys.executable}\n"
            f"PATH: {os.environ.get('PATH', '')}"
        )
        return 1, msg
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"")
        stderr = (exc.stderr or b"")
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return 1, (
            f"INFRA_ERROR: dotnet command timed out after {timeout_seconds}s.\n\n"
            f"STDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
        )

    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    return result.returncode, combined


def build_test_project(
    test_project_dir: str,
    timeout_seconds: int = 120,
) -> tuple[bool, str]:
    """
    Run ``dotnet build`` on the GeneratedTests project.
    
    Args:
        test_project_dir (str): The path to the GeneratedTests project directory.
        timeout_seconds (int): The maximum time to wait for the build to complete.
    Returns:
        tuple[bool, str]: A tuple of (success, output) where success is True if the build passed.
    """

    code, output = _run_dotnet(
        ["build", "--nologo", "-v", "minimal"],
        cwd=test_project_dir,
        timeout_seconds=timeout_seconds,
    )
    return code == 0, output


def run_csharp_test(
    test_project_dir: str,
    test_class_name: str,
    timeout_seconds: int = 120,
) -> tuple[bool, str]:
    """
    Build then execute a single xUnit test class inside the GeneratedTests project.

    Uses ``--filter "FullyQualifiedName~<test_class_name>"`` to target only the
    newly generated test class, mirroring the per-file approach used by the
    Jest runner.

    Args:
        test_project_dir: The path to the GeneratedTests project directory.
        test_class_name: The name of the test class to execute.
        timeout_seconds: The maximum time to wait for the test to complete.
    Returns:
        tuple[bool, str]: A tuple of (success, output) where success is True if the test passed,
    """
    # Build first - surface compile errors before trying to run
    build_ok, build_output = build_test_project(
        test_project_dir, timeout_seconds=timeout_seconds
    )
    if not build_ok:
        return False, build_output

    code, run_output = _run_dotnet(
        [
            "test",
            "--nologo",
            "--no-build",
            "--filter",
            f"FullyQualifiedName~{test_class_name}",
        ],
        cwd=test_project_dir,
        timeout_seconds=timeout_seconds,
    )

    combined = build_output + "\n" + run_output
    return code == 0, combined


_PROJECT_REF_MISSING_RE = re.compile(
    r"referenced project .+ does not exist",
    re.IGNORECASE,
)


def is_project_reference_error(output: str) -> bool:
    """
    Return ``True`` when the build output indicates a broken
    ``<ProjectReference>`` - e.g. the placeholder ``../YourProject.csproj``
    was never replaced with a real project path.

    This is an infrastructure problem that the LLM cannot fix by editing
    the test ``.cs`` file; it must be resolved by repairing
    ``GeneratedTests.csproj``.
    
    Args:
        output (str): The combined stdout/stderr from a dotnet build run.
    Returns:
        bool: True if the output indicates a missing project reference, False otherwise.
    """
    return bool(_PROJECT_REF_MISSING_RE.search(output))


def extract_relevant_dotnet_error(output: str) -> str:
    """
    Trim the dotnet build / test output to the most relevant error lines
    (similar to ``extract_relevant_jest_error`` for the JS runner).

    Args:
        output (str): The full stdout/stderr from a dotnet build or test run.
    Returns:
        str: A snippet of the error message that is most relevant for failure classification.
    """

    lines = output.splitlines()

    # Build errors look like: "   error CS0246: …"
    error_indices = [
        i for i, l in enumerate(lines)
        if re.search(r"\berror\b", l, re.IGNORECASE)
        and not l.strip().startswith("//")
    ]

    if error_indices:
        first = max(0, error_indices[0] - 5)
        last = min(len(lines), error_indices[-1] + 20)
        return "\n".join(lines[first:last])

    # Test failures look like "Failed  MethodName"
    fail_indices = [
        i for i, l in enumerate(lines)
        if re.search(r"^\s*(Failed|FAIL|✗|X )", l)
    ]
    if fail_indices:
        first = max(0, fail_indices[0] - 3)
        return "\n".join(lines[first: first + 60])

    # Fall back to last 80 lines
    return "\n".join(lines[-80:])
