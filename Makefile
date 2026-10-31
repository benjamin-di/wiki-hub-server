JUNIT_XML ?= .artifacts/junit.xml

.PHONY: bootstrap check check-browser
bootstrap:
	uv sync --locked
check: bootstrap
	uv run --locked ruff check src tests examples
	uv run --locked pytest -n auto --maxprocesses=4 --junitxml=$(JUNIT_XML)
	git diff --check
check-browser: bootstrap
	WIKI_TEST_BROWSER=chromium uv run --locked pytest tests/test_web_browser.py
