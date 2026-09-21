.PHONY: bootstrap verify verify-container schema packages
bootstrap:
	uv sync --frozen --extra dev --extra adapters

verify: bootstrap
	uv run --no-sync python scripts/verify.py --fetch

verify-container:
	python scripts/verify-container.py

schema:
	uv run --no-sync psrc schema export --output schemas/generated

packages:
	uv run --no-sync psrc package export --output strategies
