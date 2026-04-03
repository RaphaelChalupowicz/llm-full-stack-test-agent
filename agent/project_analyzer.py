import json
import os


def analyze_project(project_root: str) -> dict:
    package_json_path = os.path.join(project_root, "package.json")
    src_path = os.path.join(project_root, "src")

    # Start with a basic profile of the project based on package.json and directory structure
    profile = {
        "project_root": project_root,
        "has_src": os.path.isdir(src_path),
        "test_runner": "unknown",
        "test_command": None,
        "coverage_command": None,
        "uses_router": False,
        "uses_redux": False,
        "source_dirs": {
            "pages": [],
            "components": [],
            "services": [],
            "redux": [],
        },
    }

    if not os.path.exists(package_json_path):
        return profile

    with open(package_json_path, "r", encoding="utf-8") as f:
        package_data = json.load(f)

    deps = {
        **package_data.get("dependencies", {}),
        **package_data.get("devDependencies", {}),
    }
    scripts = package_data.get("scripts", {})

    if "react-router-dom" in deps:
        profile["uses_router"] = True

    if "react-redux" in deps or "@reduxjs/toolkit" in deps:
        profile["uses_redux"] = True

    # Detect source directories
    candidate_dirs = {
        "pages": ["src/pages"],
        "components": ["src/components"],
        "services": ["src/services"],
        "redux": ["src/redux"],
    }

    for dir_type, dir_paths in candidate_dirs.items():
        for dir_path in dir_paths:
            if os.path.isdir(os.path.join(project_root, dir_path)):
                profile["source_dirs"][dir_type].append(dir_path)

    # Detect test runner + commands
    if "vitest" in deps or "vitest" in scripts.get("test", ""):
        profile["test_runner"] = "vitest"

        if "test:nocoverage" in scripts:
            profile["test_command"] = ["npm", "run", "test:nocoverage", "--"]
        elif "test" in scripts:
            profile["test_command"] = ["npm", "test", "--"]
        else:
            profile["test_command"] = ["npx", "vitest", "run"]

        if "coverage" in scripts:
            profile["coverage_command"] = ["npm", "run", "coverage"]
        elif "test:coverage" in scripts:
            profile["coverage_command"] = ["npm", "run", "test:coverage"]
        elif "test" in scripts:
            profile["coverage_command"] = ["npm", "test", "--", "--coverage"]
        else:
            profile["coverage_command"] = ["npx", "vitest", "run", "--coverage"]

    elif "jest" in deps or "ts-jest" in deps or "jest" in scripts.get("test", ""):
        profile["test_runner"] = "jest"

        if "test:nocoverage" in scripts:
            profile["test_command"] = ["npm", "run", "test:nocoverage", "--"]
        elif "test" in scripts:
            profile["test_command"] = ["npm", "test", "--"]
        else:
            profile["test_command"] = ["npx", "jest"]

        if "coverage" in scripts:
            profile["coverage_command"] = ["npm", "run", "coverage"]
        elif "test:coverage" in scripts:
            profile["coverage_command"] = ["npm", "run", "test:coverage"]
        elif "test" in scripts:
            profile["coverage_command"] = ["npm", "test", "--", "--coverage"]
        else:
            profile["coverage_command"] = ["npx", "jest", "--coverage", "--passWithNoTests"]

    return profile