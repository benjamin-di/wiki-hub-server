.PHONY: bootstrap check
bootstrap:
	uv sync --locked
check: bootstrap
	uv run --locked ruff check src tests examples
	uv run --locked pytest --junitxml=.artifacts/junit.xml
	git diff --check
