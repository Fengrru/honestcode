# Contributing to HonestCode

Thank you for your interest in making HonestCode better! This document
outlines how to contribute code, report issues, and request features.

## Getting Started

### Prerequisites

- Python >= 3.10
- Git
- (Optional) [pre-commit](https://pre-commit.com/) for automated linting

### Setup

1. Fork the repository on GitHub.

2. Clone your fork locally:
   ```bash
   git clone https://github.com/<your-username>/honestcode-mcp.git
   cd honestcode-mcp
   ```

3. Create a virtual environment and install in editable mode:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   pip install -e ".[dev]"
   ```

4. (Optional) Install pre-commit hooks:
   ```bash
   pre-commit install
   ```

## Development Workflow

1. Create a new branch for your change:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. Make your changes, including tests when applicable.

3. Run the test suite and lint checks:
   ```bash
   # Tests
   pytest

   # Lint
   ruff check honestcode tests scripts
   ruff format --check honestcode tests scripts

   # Or via pre-commit
   pre-commit run --all-files
   ```

4. Commit your changes with a clear, concise message.

5. Push to your fork and open a pull request against the `main` branch.

## Code Style

- We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.
- Target Python version is 3.10+.
- Keep functions focused and add docstrings for public APIs.
- Prefer `pathlib.Path` over string paths.
- Avoid bare `except:`; catch specific exceptions or mark intentional broad
  catches with `# noqa: BLE001`.

## Writing Tests

- Tests live in the `tests/` directory.
- Use `pytest` and the `tmp_path` fixture for filesystem fixtures.
- Aim to cover both happy paths and edge cases.
- If you fix a bug, add a regression test when possible.

## Pull Request Guidelines

- Keep PRs focused on a single change.
- Include a clear description of what changed and why.
- Ensure all CI checks pass.
- Reference related issues (e.g. "Closes #123").

## Reporting Issues

When reporting a bug, please include:

- A clear description of the problem.
- Steps to reproduce.
- Expected vs. actual behavior.
- Your operating system and Python version.
- Relevant logs or error messages.

## Feature Requests

Feature requests are welcome! Open an issue describing:

- The use case.
- The proposed API or behavior.
- Why it is useful for hallucination detection.

## Code of Conduct

Be respectful, constructive, and inclusive. We want HonestCode to be a
welcoming project for everyone.

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE).
