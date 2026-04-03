# LLM React Test Generator

An autonomous agent that generates **working tests** for React + TypeScript frontend using Jest and React Testing Library, with an LLM-driven repair loop.

---

## What it does

Given a React project, the agent:

1. Detects project setup (Jest or Vitest, Router, Redux)
2. Tries to read coverage data (coverage-summary.json)
3. If coverage is missing → falls back to scanning source files from **src**
4. Uses an LLM to decide if each file is worth testing
5. Generates a Jest / React Testing Library test using an LLM model
6. Runs tests locally
7. If the test fails → analyzes the error and fixes it (deterministic AND LLM repair loop)
8. Retries until the test passes or max attempts are reached

The result is passing generated tests for low-coverage files with minimal manual work.

---

## Why this is different

Most AI tools generate tests once and stop.

This Agent builds a **full lifecycle**:

```
generate -> run -> fail -> classify -> fix -> retry -> pass
```

---

## Architecture

```
agent/
├── main.py                         # Orchestrates the full run and bootstrap commands
├── project_analyzer.py             # Detects test runner and project capabilities
├── coverage_reader.py              # Reads coverage gap + source scan fallback logic
├── strategy_selector.py            # Chooses testing strategy by file path and type
├── test_generator.py               # LLM generation and LLM repair loop
├── test_executor.py                # Runs generated tests
├── failure_classifier.py           # Classifies errors and provides deterministic hints/fixes
├── coverage_executor.py            # Runs project coverage command when needed
└── bootstrap_jest.py               # Bootstraps Jest in projects without test setup

(target project (--project /path/to/react-app) )
├── src/                            # (front end cmps)
├── package.json
├── coverage/coverage-summary.json  # Optional
└── tests/generated/                # generated tests
    └── .progress.json              # progress cache (auto-created)
```

---

## Requirements

- Python 3.10+
- Node.js (with npm)
- A React project (TypeScript preferred)
- Coverage report (`coverage-summary.json`) - **optional**

---

## Setup

```bash
1. clone project

git clone https://github.com/RaphaelChalupowicz/llm-full-stack-test-agent.git
cd llm-full-stack-test-agent

2. Create and activate a Python virtual environment

python -m venv venv

Windows:
venv\Scripts\activate
macOS/Linux:
source venv/bin/activate

3. Install dependencies

pip install -r requirements.txt

4. Copy environment template

cp .env.example .env

4. Edit .env and set OPENAI_API_KEY
```

You can tune runtime behavior without code changes:

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5.4-mini
DEFAULT_COVERAGE_PATH=coverage/coverage-summary.json
DEFAULT_MAX_FILES=2
MAX_FIX_ATTEMPTS=3
TEST_TIMEOUT_SECONDS=120
COVERAGE_THRESHOLD=80
COVERAGE_SKIP_PATTERNS=types/,assets/,.d.ts,main.tsx,vite-env,index.css,rootReducer.ts,store.ts
```

---

## Usage

1. Bootstrap a project (if needed)

```bash
python agent/main.py bootstrap --project /path/to/react-app
```

This will:

- install Jest + dependencies
- patch config (original `package.json` is backed up as `package.json.bak`)
- create test setup files

2. Run the agent

```bash
python agent/main.py run --project /path/to/react-app
```

`run` arguments:

| Flag          | Default                          | Description                                                 |
| ------------- | -------------------------------- | ----------------------------------------------------------- |
| `--project`   | _(required)_                     | Path to the target frontend project                         |
| `--coverage`  | `coverage/coverage-summary.json` | Coverage file path relative to project root                 |
| `--max-files` | `2`                              | Maximum number of low-coverage files to process per run     |
| `--verbose`   | off                              | Print full LLM prompts and responses (useful for debugging) |

---

## Example Output

Analyzing project: /path/to/frontend

=== PROJECT PROFILE ===
test_runner: jest
test_command: [npm, test, --]
uses_router: True
uses_redux: True

=== FOUND 31 COVERAGE GAPS ===

GENERATING - src/pages/Collection.tsx
Strategy: page
Saved -> tests/generated/pages/Collection.test.tsx
Running test (attempt 1/3)...
FAIL - tests/generated/pages/Collection.test.tsx
Failure type: react_invalid_element
Repair changed the file.
Running test (attempt 2/3)...
PASS - tests/generated/pages/Collection.test.tsx

=== RUN SUMMARY ===
Processed: 1
Passing generated tests: 1
Failed/skipped: 0

---

## Strategy detection

| Path pattern                     | Strategy      | Approach                                          |
| -------------------------------- | ------------- | ------------------------------------------------- |
| `src/pages/`                     | `page`        | Shell/integration-lite, MemoryRouter, child mocks |
| `src/components/`                | `component`   | RTL render, user interaction assertions           |
| `src/redux/thunks/`              | `redux_thunk` | Real store via configureStore, fulfilled/rejected |
| `src/redux/slices/`              | `redux_slice` | Pure reducer/action tests                         |
| `src/services/`                  | `service`     | axios mocking, request/response behavior          |
| `src/hooks/`, `use*.ts(x)`       | `util`        | Custom hook tests                                 |
| `src/contexts/`                  | `component`   | Context provider rendering                        |
| `src/utils/`, `helpers/`, `lib/` | `util`        | Pure input/output tests                           |

---

## Failure classifier

| Failure type             | Trigger                            | Repair action                         |
| ------------------------ | ---------------------------------- | ------------------------------------- |
| `react_invalid_element`  | "Element type is invalid"          | Fix default export mocking pattern    |
| `redux_state_missing`    | "cannot destructure...useSelector" | Add missing slice state or mock child |
| `missing_router_context` | "basename", "useContext"           | Wrap in MemoryRouter                  |
| `fragile_call_count`     | "toHaveBeenCalledTimes"            | Relax to toHaveBeenCalled()           |
| `dom_query_failure`      | "unable to find element"           | Fix selector to match rendered DOM    |
| `ambiguous_query`        | "found multiple elements"          | Use getByRole or getAllByText         |
| `import_error`           | "Cannot find module"               | Fix relative import paths             |
| `act_warning`            | "not wrapped in act"               | Add act() or waitFor() wrapping       |
| `null_reference`         | TypeError on null/undefined        | Fix mock shape or await async data    |
| `ts_compile`             | TypeScript compile error           | Fix type annotations                  |

---

## Current limitations

- Optimized for React + TypeScript projects
- Vitest support is partial. Jest is the primary flow
- Some complex integration heavy components may still require manual test refinement

---
