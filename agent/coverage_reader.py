import json
import os


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_csv(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if not value:
        return default
    parsed = [item.strip() for item in value.split(",") if item.strip()]
    return parsed or default


COVERAGE_THRESHOLD = _env_int("COVERAGE_THRESHOLD", 80)

SKIP_PATTERNS = _env_csv(
    "COVERAGE_SKIP_PATTERNS",
    [
        "types/",
        "assets/",
        ".d.ts",
        "main.tsx",
        "vite-env",
        "index.css",
        "rootReducer.ts",
        "store.ts",
    ],
)


def normalize_path(abs_path: str, project_root: str) -> str:
    """
    Normalizes a file path to be relative to the project root.
    Args:
        abs_path (str): The absolute file path.
        project_root (str): The project root path.
    Returns:
        str: The normalized relative file path.
    """

    abs_path = abs_path.replace("\\", "/")
    project_root = project_root.replace("\\", "/")
    if abs_path.startswith(project_root):
        rel = abs_path[len(project_root):].lstrip("/")
        return rel
    return abs_path


def should_skip(file_path: str) -> bool:
    """
    Determines if a file should be skipped based on configured skip patterns.
    Args:
        file_path (str): The file path to check.
    Returns:
        bool: True if the file should be skipped, False otherwise.
    """

    normalized = file_path.replace("\\", "/")
    return any(pattern in normalized for pattern in SKIP_PATTERNS)


def coverage_is_empty(coverage_json_path: str) -> bool:
    """
    Determines if the coverage report is effectively empty, meaning it has no per-file entries or all files are marked as 0% covered. This can happen when tests haven't been run or coverage data is missing.
    Args:
        coverage_json_path (str): The file path to the coverage-summary.json report.
    Returns:
        bool: True if coverage is empty or missing, False if there are real files with coverage
    """

    if not os.path.exists(coverage_json_path):
        return True

    with open(coverage_json_path, encoding="utf-8") as f:
        data = json.load(f)

    real_files = [k for k in data.keys() if k != "total"]
    if real_files:
        return False

    total = data.get("total", {})
    lines = total.get("lines", {})
    pct = lines.get("pct")
    total_lines = lines.get("total", 0)

    return pct == "Unknown" or total_lines == 0


def discover_source_files(project_root: str) -> list[str]:
    """
    Discovers all source files in the project root.
    Args:
        project_root (str): The project root path.
    Returns:
        list[str]: A list of relative file paths.
    """

    src_root = os.path.join(project_root, "src")
    if not os.path.isdir(src_root):
        return []

    discovered = []

    for root, _, files in os.walk(src_root):
        for file in files:
            if not file.endswith((".ts", ".tsx", ".js", ".jsx")):
                continue

            full_path = os.path.join(root, file)
            rel = normalize_path(full_path, project_root)

            if should_skip(rel):
                continue

            discovered.append(rel)

    return discovered


def get_coverage_gaps(coverage_json_path: str, project_root: str) -> list[dict]:
    """
    Gets the list of coverage gaps based on the coverage report and project files.
    Args:
        coverage_json_path (str): The file path to the coverage-summary.json report.
        project_root (str): The project root path.
    Returns:
        list[dict]: A list of dictionaries representing the coverage gaps.
    """
    
    if coverage_is_empty(coverage_json_path):
        print("Coverage missing or empty → using source scan fallback")

        files = discover_source_files(project_root)
        gaps = []

        for rel in files:
            gaps.append(
                {
                    "file_relative": rel,
                    "file_absolute": os.path.join(project_root, rel),
                    "coverage_pct": 0,
                    "lines_total": 0,
                    "lines_covered": 0,
                    "discovered_without_coverage": True,
                }
            )

        gaps.sort(key=lambda x: x["file_relative"])
        return gaps

    # Normal coverage flow
    with open(coverage_json_path, encoding="utf-8") as f:
        data = json.load(f)

    gaps = []
    for raw_path, stats in data.items():
        if raw_path == "total":
            continue

        relative_path = normalize_path(raw_path, project_root)

        if should_skip(relative_path):
            continue

        pct = stats["lines"]["pct"]
        if pct == "Unknown":
            pct = 0

        if pct < COVERAGE_THRESHOLD:
            gaps.append(
                {
                    "file_relative": relative_path,
                    "file_absolute": os.path.join(project_root, relative_path),
                    "coverage_pct": pct,
                    "lines_total": stats["lines"]["total"],
                    "lines_covered": stats["lines"]["covered"],
                    "discovered_without_coverage": False,
                }
            )

    gaps.sort(key=lambda x: x["lines_total"], reverse=True)
    return gaps