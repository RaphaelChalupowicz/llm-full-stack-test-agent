"""
Scans a directory for C# source files and extracts lightweight metadata
(namespace, class names, public method signatures) to provide context to the
LLM test-generation prompt.
"""

import os
import re

# Directories that are never worth scanning
_IGNORED_DIRS = {
    "obj",
    "bin",
    ".vs",
    ".git",
    "node_modules",
    "GeneratedTests",
    "Migrations",
    "Properties",
}

# File-name fragments that indicate a file is already a test or generated code
_TEST_FILE_PATTERNS = [
    r"\.tests?\.cs$",
    r"tests?\.cs$",
    r"\.specs?\.cs$",
    r"spec\.cs$",
    r"designer\.cs$",
    r"\.g\.cs$",
    r"AssemblyInfo\.cs$",
]

_TEST_RE = re.compile("|".join(_TEST_FILE_PATTERNS), re.IGNORECASE)

# Regex patterns for lightweight C# parsing
_NAMESPACE_RE = re.compile(
    r"^\s*(?:namespace\s+([\w.]+)(?:\s*\{|;))",
    re.MULTILINE,
)
_CLASS_RE = re.compile(
    r"^\s*(?:public|internal|protected|private)?\s*"
    r"(?:static\s+|abstract\s+|sealed\s+|partial\s+)*"
    r"(?:class|record|struct|interface)\s+([\w<>]+)",
    re.MULTILINE,
)
_METHOD_RE = re.compile(
    r"^\s*(?:public|protected)\s+"
    r"(?:static\s+|async\s+|virtual\s+|override\s+|abstract\s+)*"
    r"(?:[\w<>\[\]?,\s]+?)\s+"
    r"([\w]+)\s*\(",
    re.MULTILINE,
)


def _is_test_file(path: str) -> bool:
    return bool(_TEST_RE.search(os.path.basename(path)))


def _has_ignored_segment(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return any(part in _IGNORED_DIRS for part in parts)


def find_csharp_files(project_root: str) -> list[dict]:
    """
    Recursively find all C# source files under *project_root* that are
    candidates for test generation (not tests, not generated, not obj/bin).

    Returns a list of dicts::

        {
            "file_relative": "Controllers/UserController.cs",
            "file_absolute": "/abs/path/Controllers/UserController.cs",
        }
    """
    
    project_root = os.path.abspath(project_root)
    results: list[dict] = []

    for dirpath, dirnames, filenames in os.walk(project_root):
        # Prune ignored directories in-place to skip them entirely
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]

        for fname in filenames:
            if not fname.endswith(".cs"):
                continue

            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, project_root).replace("\\", "/")

            if _has_ignored_segment(rel):
                continue
            if _is_test_file(rel):
                continue

            results.append(
                {
                    "file_relative": rel,
                    "file_absolute": full,
                }
            )

    results.sort(key=lambda x: x["file_relative"])
    return results


def parse_csharp_file(file_absolute: str) -> dict:
    """
    Lightly parse a C# file to extract:

    - ``namespace``      - the declared namespace (or empty string)
    - ``classes``        - list of class / record / struct / interface names
    - ``public_methods`` - list of public method names

    This is intentionally lightweight (regex-based, not a full AST) because
    the full source is also sent to the LLM.

    Args:
        file_absolute (str): The absolute path to the C# file to parse.
    Returns:
        dict: A dictionary containing the parsed namespace, classes,
            public methods, and the full source code of the file.
    """

    with open(file_absolute, encoding="utf-8", errors="replace") as f:
        source = f.read()

    namespace_match = _NAMESPACE_RE.search(source)
    namespace = namespace_match.group(1).strip() if namespace_match else ""

    classes = _CLASS_RE.findall(source)
    # Strip generic type parameters for display
    classes = [re.sub(r"<.*?>", "", c) for c in classes]

    # Collect public methods; filter out likely property accessors (get/set/init)
    raw_methods = _METHOD_RE.findall(source)
    skip_names = {"get", "set", "init", "add", "remove", "value"}
    public_methods = [m for m in raw_methods if m not in skip_names]

    return {
        "namespace": namespace,
        "classes": classes,
        "public_methods": public_methods,
        "source": source,
    }


# Directories to skip when searching for project .csproj files
_CSPROJ_SKIP_DIRS = {
    "obj",
    "bin",
    ".vs",
    ".git",
    "node_modules",
    "GeneratedTests",
    "Migrations",
    "Properties",
    "wwwroot",
}


def _looks_like_test_csproj(csproj_name: str) -> bool:
    """
    Heuristic to determine if a .csproj file is likely a test project based on its name.
    Args:
        csproj_name (str): The name of the .csproj file.
    Returns:
        bool: True if the file name suggests it's a test project, False otherwise.
    """

    lower = csproj_name.lower()
    return any(
        tok in lower
        for tok in (".tests.", ".test.", ".specs.", ".spec.", "generatedtests")
    )


def find_all_project_csproj_files(project_root: str) -> list[str]:
    """
    Return the absolute paths of all non-test ``.csproj`` files that belong
    to the solution rooted at *project_root*.

    Search order:

    1. Directly inside *project_root* (common for single-project layouts)
    2. Immediate subdirectories one level deep, skipping build / generated dirs
       (common for multi-project solutions where ``--project`` points at the
       solution root)

    Test project ``.csproj`` files (detected by name heuristic) are excluded.

    Args:
        project_root (str): The root directory to search for .csproj files.
    Returns:
        list[str]: A list of absolute paths to non-test .csproj files.
    """

    found: list[str] = []

    # 1. Root-level .csproj files
    try:
        for fname in sorted(os.listdir(project_root)):
            if fname.endswith(".csproj") and not _looks_like_test_csproj(fname):
                found.append(os.path.join(project_root, fname))
    except PermissionError:
        pass

    if found:
        return found

    # 2. One level deep in subdirectories
    try:
        for entry in sorted(os.scandir(project_root), key=lambda e: e.name):
            if not entry.is_dir() or entry.name in _CSPROJ_SKIP_DIRS:
                continue
            try:
                for fname in sorted(os.listdir(entry.path)):
                    if fname.endswith(".csproj") and not _looks_like_test_csproj(fname):
                        found.append(os.path.join(entry.path, fname))
            except PermissionError:
                continue
    except PermissionError:
        pass

    return found


def detect_csharp_project_file(project_root: str) -> str | None:
    """
    Return the path of the primary ``.csproj`` for *project_root*, or ``None``.

    Delegates to :func:`find_all_project_csproj_files` and returns the first
    result (which is the most direct / most prominent match).

    Args:
        project_root (str): The root directory of the C# project.
    Returns:
        str | None: The absolute path to the primary .csproj file, or None if not found.
    """

    files = find_all_project_csproj_files(project_root)
    return files[0] if files else None


def detect_target_framework(project_root: str) -> str:
    """
    Read ``<TargetFramework>`` from the project's ``.csproj``.
    Falls back to ``net8.0`` when undetectable.
    Args:
        project_root (str): The root directory of the C# project.
    Returns:
        The target framework string, e.g. "net8.0".
    """

    csproj = detect_csharp_project_file(project_root)
    if csproj is None:
        return "net8.0"

    with open(csproj, encoding="utf-8", errors="replace") as f:
        content = f.read()

    match = re.search(r"<TargetFramework>(.*?)</TargetFramework>", content)
    if match:
        return match.group(1).strip()

    # Multiple frameworks — take the first one
    multi = re.search(r"<TargetFrameworks>(.*?)</TargetFrameworks>", content)
    if multi:
        return multi.group(1).split(";")[0].strip()

    return "net8.0"


def classify_csharp_file(file_relative: str) -> str:
    """
    Infer a broad category for a C# file based on its path/name.
    Used to tailor the LLM prompt strategy.

    Args:
        file_relative (str): The file path relative to the project root, e.g. "Controllers/UserController.cs".
    Returns:
        str: A category label such as "controller", "service", "repository", "middleware", "utility", "validator", "handler", "model", or "generic".
    """

    path = file_relative.replace("\\", "/").lower()
    name = os.path.basename(path)

    if "controller" in name:
        return "controller"
    if "service" in name:
        return "service"
    if "repository" in name or "repo" in name:
        return "repository"
    if "middleware" in name:
        return "middleware"
    if "helper" in name or "util" in name or "extension" in name:
        return "utility"
    if "validator" in name:
        return "validator"
    if "handler" in name:
        return "handler"

    # Directory-based hints
    if "/controllers/" in path:
        return "controller"
    if "/services/" in path:
        return "service"
    if "/repositories/" in path or "/data/" in path:
        return "repository"
    if "/middleware/" in path:
        return "middleware"
    if "/validators/" in path:
        return "validator"
    if "/handlers/" in path:
        return "handler"
    if "/helpers/" in path or "/utils/" in path or "/extensions/" in path:
        return "utility"
    if "/models/" in path or "/entities/" in path or "/dtos/" in path:
        return "model"

    return "generic"
