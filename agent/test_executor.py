import os
import shutil
import subprocess
import sys


def resolve_command(command: list[str]) -> list[str]:
    """
    Ensures the test command resolves correctly across platforms.
    """
    if not command:
        return command

    first = command[0]

    if os.name == "nt" and first == "npm":
        npm_cmd = shutil.which("npm.cmd") or shutil.which("npm")
        if npm_cmd:
            return [npm_cmd] + command[1:]

    return command


def run_single_test(
    project_root: str,
    test_command: list[str],
    test_relative_path: str,
    timeout_seconds: int = 120,
) -> tuple[bool, str]:
    """
    Executes a single test file using the project's configured test command.
    This function intentionally runs tests WITHOUT coverage.
    
    Returns:
        (passed: bool, output: str)
    """
    # Ensure command works on current OS (especially Windows)
    command = resolve_command(test_command) + [
        "--watchAll=false",
        "--runTestsByPath",
        test_relative_path.replace("\\", "/"),
    ]

    # Force CI mode to disable interactive behaviors
    env = os.environ.copy()
    env["CI"] = "true"

    try:
        result = subprocess.run(
            command,
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,
            env=env,
        )
    except FileNotFoundError:
        # npm / node / runner is not found
        return (
            False,
            "INFRA_ERROR: Could not find test command executable.\n"
            f"Python executable: {sys.executable}\n"
            f"Resolved command: {command}\n"
            f"PATH: {os.environ.get('PATH', '')}",
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return (
            False,
            "INFRA_ERROR: Test timed out.\n\n"
            f"STDOUT:\n{stdout}\n\n"
            f"STDERR:\n{stderr}",
        )

    # Combine output streams for downstream failure analysis
    output = (result.stdout or "") + "\n" + (result.stderr or "")

    # Return success status based on exit code
    return result.returncode == 0, output