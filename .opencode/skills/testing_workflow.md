# Skill: Testing & Regression Prevention Workflow

## Objective
Ensure code quality by enforcing a strict testing lifecycle (TDD) and regression prevention before any code changes are committed. This skill ensures Little Mester analyzes existing tests, writes new ones up-front, and validates the entire suite.

## Trigger
This skill is active during all development tasks involving code modification, feature addition, or refactoring.

## Workflow Steps

### 1. Pre-Change Analysis
- **Identify Scope**: Determine which files/modules are affected by the requested change.
- **Audit Existing Tests**: Check for existing unit and integration tests in the target area.
- **Gap Analysis**: Identify missing coverage for the proposed changes. Explicitly state what is currently tested vs. what needs testing.

### 2. Test Strategy Selection (Workflow Choice)
Before writing code, Little Mester must select the appropriate testing strategy based on the task context:
- **Strategy A: Strict TDD (Recommended)**
  - Write failing unit tests first.
  - Write failing integration tests if the feature touches external systems or APIs.
  - Run tests to confirm failure.
- **Strategy B: Regression Focus**
  - Identify high-risk areas for regression in existing code.
  - Write specific regression tests for those areas.
  - Proceed with implementation.

### 3. Implementation (TDD Cycle)
1. **Write Tests**: Create unit and integration tests that define the expected behavior.
   - *Constraint*: Must include both Unit (logic) and Integration (system/API) tests where applicable.
2. **Run & Fail**: Execute the test suite to ensure new tests fail as expected.
3. **Implement Code**: Write the minimum code required to pass the tests.
4. **Run & Pass**: Execute the full test suite (unit + integration) to ensure success.

### 4. Regression Check
- Run the *entire* relevant test suite, not just new tests.
- Verify no existing functionality is broken.
- If regressions are found, fix them immediately before committing.

### 5. Commitment
- Commit the code and the associated tests together.
- Ensure commit message references the tests added/modified.

## Decision Matrix: Unit vs Integration
| Feature Type | Required Tests |
| :--- | :--- |
| Pure Logic / Algorithms | Unit Tests |
| API Endpoints | Integration Tests + Unit Tests (for logic) |
| Database Interactions | Integration Tests (with mock or test DB) |
| UI Components | Integration Tests (E2E) + Unit Tests (logic) |

## Execution Rules
- **NO** code changes are committed without passing tests.
- **NO** feature is considered "done" until regression checks pass.
- Always prioritize **Integration Tests** for system stability and **Unit Tests** for logic correctness.
- If a test cannot be written, explicitly state why and propose a workaround or manual verification step.
