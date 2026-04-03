import os
import shutil
import subprocess
import sys


def resolve_command(command: list[str]) -> list[str]:
    if not command:
        return command

    first = command[0]

    if os.name == "nt" and first == "npm":
        npm_cmd = shutil.which("npm.cmd") or shutil.which("npm")
        if npm_cmd:
            return [npm_cmd] + command[1:]

    return command


def run_coverage(
    project_root: str,
    coverage_command: list[str],
    timeout_seconds: int = 300,
) -> tuple[bool, str]:
    """
    Runs the project coverage command to generate coverage-summary.json.
    """
    command = resolve_command(coverage_command)

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
        return (
            False,
            "INFRA_ERROR: Could not find coverage command executable.\n"
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
        return False, (
            "INFRA_ERROR: Coverage command timed out.\n\n"
            f"STDOUT:\n{stdout}\n\n"
            f"STDERR:\n{stderr}"
        )

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    return result.returncode == 0, output
