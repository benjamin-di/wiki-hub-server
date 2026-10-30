.PHONY: bootstrap check
bootstrap:
	uv sync --locked
check: bootstrap
	uv run --locked ruff check src tests examples
	uv run --locked pytest -n auto --maxprocesses=4 --junitxml=.artifacts/junit.xml
	git diff --check
