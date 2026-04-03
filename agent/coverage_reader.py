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
    abs_path = abs_path.replace("\\", "/")
    project_root = project_root.replace("\\", "/")
    if abs_path.startswith(project_root):
        rel = abs_path[len(project_root):].lstrip("/")
        return rel
    return abs_path


def should_skip(file_path: str) -> bool:
    normalized = file_path.replace("\\", "/")
    return any(pattern in normalized for pattern in SKIP_PATTERNS)


def coverage_is_empty(coverage_json_path: str) -> bool:
    """
    Returns True when coverage-summary.json is missing or contains only an
    empty/Unknown total section with no per file entries.
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
    Fallback mode:
    If coverage-summary.json has no per file entries, scan src/ and treat
    relevant source files as 0% covered candidates.
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
    Reads real coverage gaps when coverage-summary.json contains per file entries.
    If the coverage file is missing or empty,
    falls back to scanning src/ and treating files as 0% coverage gaps.
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