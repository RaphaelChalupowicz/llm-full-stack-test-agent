import json
import os
import shutil
import subprocess
import sys


JEST_DEV_DEPENDENCIES = [
    "jest",
    "ts-jest",
    "@types/jest",
    "jest-environment-jsdom",
    "@testing-library/react",
    "@testing-library/jest-dom",
    "identity-obj-proxy",
]


def resolve_command(command: list[str]) -> list[str]:
    """
    Resolves the given command list by replacing "npm" with the full path to npm.cmd or npm on Windows systems, ensuring correct command execution. Returns the possibly modified command list.
    Args:
        command (list[str]): The original command list to resolve.
    """

    if not command:
        return command

    first = command[0]

    if os.name == "nt" and first == "npm":
        npm_cmd = shutil.which("npm.cmd") or shutil.which("npm")
        if npm_cmd:
            return [npm_cmd] + command[1:]

    return command


def run_command(command: list[str], cwd: str) -> None:
    """
    Executes the given command as a subprocess in the specified cwd directory, raising a RuntimeError if the command fails or is not found.
    Args:
        command (list[str]): The command to execute, as a list of strings.
        cwd (str): The directory in which to execute the command.
    """

    resolved = resolve_command(command)

    try:
        result = subprocess.run(
            resolved,
            cwd=cwd,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Could not find npm executable.\n"
            f"Python executable: {sys.executable}\n"
            f"Resolved command: {resolved}\n"
            f"PATH: {os.environ.get('PATH', '')}"
        )

    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(resolved)}")


def install_jest_dependencies(project_root: str) -> None:
    """
    Installs Jest development dependencies in the specified project_root directory by running an npm install command.
    Args:
        project_root (str): The root directory of the project where Jest dependencies should be installed.
    """

    print("Installing Jest dependencies...")
    run_command(["npm", "install", "--save-dev", *JEST_DEV_DEPENDENCIES], cwd=project_root)


def load_package_json(project_root: str) -> tuple[str, dict]:
    """
    Loads the package.json file from the specified project root.
    Args:
        project_root (str): The root directory of the project.
    Returns:
        tuple[str, dict]: A tuple containing the path to the package.json file and its contents as a dictionary.
    """

    package_json_path = os.path.join(project_root, "package.json")
    with open(package_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return package_json_path, data


def save_package_json(package_json_path: str, data: dict) -> None:
    """
    Saves the updated package.json data to the specified path.
    Args:
        package_json_path (str): The path to the package.json file.
        data (dict): The updated package.json data.
    """

    with open(package_json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def patch_package_json_for_jest(project_root: str) -> None:
    """
    Updates the package.json in the given project_root to configure Jest for testing, including backing up the original file, setting up test scripts, and adding Jest-specific settings.
    Args:
        project_root (str): The root directory of the project whose package.json should be patched for
    """

    print("Patching package.json for Jest...")
    package_json_path, data = load_package_json(project_root)

    backup_path = package_json_path + ".bak"
    if not os.path.exists(backup_path):
        with open(package_json_path, "r", encoding="utf-8") as src, open(
            backup_path, "w", encoding="utf-8"
        ) as dst:
            dst.write(src.read())
        print(f"  Backed up original package.json -> {backup_path}")

    scripts = data.setdefault("scripts", {})
    scripts.setdefault("test", "jest")
    scripts["coverage"] = "jest --coverage --passWithNoTests"

    data["jest"] = {
        "testEnvironment": "jsdom",
        "coverageReporters": ["json-summary", "text"],
        "collectCoverageFrom": [
            "<rootDir>/src/**/*.{js,jsx,ts,tsx}",
            "!<rootDir>/src/**/*.d.ts",
            "!<rootDir>/src/main.tsx",
            "!<rootDir>/src/vite-env.d.ts",
        ],
        "transform": {
            "^.+\\.tsx?$": [
                "ts-jest",
                {
                    "tsconfig": {
                        "jsx": "react-jsx",
                        "types": ["jest", "@testing-library/jest-dom"],
                    }
                },
            ]
        },
        "moduleNameMapper": {
            "\\.(css|less|scss|sass)$": "identity-obj-proxy",
            "\\.(gif|ttf|eot|svg|png|jpg|jpeg)$": "<rootDir>/tests/fileMock.ts",
        },
        "setupFilesAfterEnv": ["<rootDir>/tests/setupTests.ts"],
    }

    save_package_json(package_json_path, data)


def create_bootstrap_files(project_root: str) -> None:
    """
    Creates a tests directory under the given project_root and generates three TypeScript test bootstrap files: setupTests.ts, fileMock.ts, and bootstrap.test.ts with default content for initializing a test environment.
    Args:
        project_root (str): The root directory of the project where the tests directory and bootstrap files should be created.
    """

    print("Creating test bootstrap files...")
    tests_dir = os.path.join(project_root, "tests")
    os.makedirs(tests_dir, exist_ok=True)

    setup_tests_path = os.path.join(tests_dir, "setupTests.ts")
    file_mock_path = os.path.join(tests_dir, "fileMock.ts")
    bootstrap_test_path = os.path.join(tests_dir, "bootstrap.test.ts")

    with open(setup_tests_path, "w", encoding="utf-8") as f:
        f.write('import "@testing-library/jest-dom";\n')

    with open(file_mock_path, "w", encoding="utf-8") as f:
        f.write('const fileMock = "test-file-stub";\nexport default fileMock;\n')

    with open(bootstrap_test_path, "w", encoding="utf-8") as f:
        f.write('test("bootstrap", () => {\n  expect(true).toBe(true);\n});\n')


def bootstrap_jest_project(project_root: str) -> None:
    """
    Initializes a Jest testing environment in the specified project_root by installing dependencies, updating package.json, and creating necessary bootstrap files.
    Args:
        project_root (str): The root directory of the project to bootstrap Jest in.
    """

    install_jest_dependencies(project_root)
    patch_package_json_for_jest(project_root)
    create_bootstrap_files(project_root)
    print("Jest bootstrap completed successfully.")
