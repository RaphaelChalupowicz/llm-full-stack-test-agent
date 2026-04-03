import os
import shutil
import subprocess
import sys


def resolve_command(command: list[str]) -> list[str]:
    """
    Ensures the test command resolves correctly across platforms.
    Args:
        command (list[str]): The original command list to resolve.
    Returns:
        list[str]: The resolved command list, with "npm" replaced by the full path to npm.cmd on Windows if necessary.
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
    Runs a single test file using the specified test command, ensuring cross-platform compatibility and handling timeouts and command resolution.
    Args:
        project_root (str): The root directory of the project where the test should be run.
        test_command (list[str]): The base command to run the test (e.g. ["npm", "test"]).
        test_relative_path (str): The relative path to the test file to run.
        timeout_seconds (int): The maximum time to allow for the test command to run before timing out.
    Returns:
        tuple[bool, str]: A tuple containing a boolean indicating test success, and a string
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