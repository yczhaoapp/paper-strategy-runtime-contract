#!/usr/bin/env python3
"""Online preparation only. Verification consumes immutable, hash-pinned local PDFs."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

HOSTS = {
    "arxiv.org",
    "math.nyu.edu",
    "www.cis.upenn.edu",
    "www.ma.imperial.ac.uk",
    "www.nber.org",
}
LIMIT = 20 * 1024 * 1024


def allowed(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in HOSTS
        or parts.username
        or parts.password
        or parts.port not in {None, 443}
    ):
        raise ValueError("paper fetch only accepts approved public academic HTTPS hosts")


class RedirectPolicy(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(registry: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    document = json.loads(registry.read_text(encoding="utf-8"))
    sources = document.get("sources", [])
    if document.get("version") != "1.0" or not sources:
        raise ValueError("paper source registry is empty or invalid")
    for source in sources:
        paper_id = source["source_id"]
        if not paper_id.replace("_", "").replace("-", "").isalnum():
            raise ValueError("unsafe paper identifier")
        target = output / f"{paper_id}.pdf"
        expected = source["source_sha256"]
        if target.is_file():
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError(f"cached source hash mismatch: {target}; refusing replacement")
            print(f"verified cached source: {paper_id}")
            continue
        url = source["source_url"]
        allowed(url)
        opener = urllib.request.build_opener(RedirectPolicy())
        with opener.open(url, timeout=60) as response:
            payload = response.read(LIMIT + 1)
        if len(payload) > LIMIT or not payload.startswith(b"%PDF-"):
            raise ValueError(f"invalid PDF response: {paper_id}")
        if hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError(
                f"upstream source changed: {paper_id}; explicit recipe review required"
            )
        temporary = target.with_suffix(".partial")
        temporary.write_bytes(payload)
        temporary.replace(target)
        print(f"fetched and verified: {paper_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=Path("papers/source-registry.json"))
    parser.add_argument("--output", type=Path, default=Path("papers/sources"))
    args = parser.parse_args()
    try:
        fetch(args.registry, args.output)
    except Exception as exc:
        print(json.dumps({"status": "failed", "code": "PAPER_FETCH_FAILED", "message": str(exc)}))
        sys.exit(1)
