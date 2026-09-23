# Verification receipts

`verification-summary.json` is the compact, reviewable receipt committed with the source. It identifies the exact verification input tree and summarizes the local host and strict-container gates. `release/` contains the corresponding complete verification and acceptance JSON documents; every published file has a SHA-256 in the summary.

Full logs, JUnit, coverage, run bundles, paper evidence and HTML reports are generated under `reports/generated/` and `reports/strict/`. They are excluded from Git because they are large and environment-specific. `python scripts/publish-evidence.py` accepts only two passing receipts for the current input tree and rejects acceptance documents containing absolute local paths before refreshing the committed portable evidence. The GitHub workflow uploads the complete directories as per-commit artifacts for Linux, Windows, macOS and the strict Linux container.

The strict launcher invalidates prior success before any Docker operation. Its image, verification, and acceptance receipts share one `attempt_id`; the publisher rejects mixed attempts. Remote platform results and downloadable evidence are recorded by the workflow run for each commit.
