from __future__ import annotations

import hashlib
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal, NoReturn

from pypdf import PdfReader

from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage
from psrc.papers.models import PaperDocument, SourcePage

MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 200
MAX_TEXT = 2_000_000


def fail(code: ErrorCode, message: str, **details: object) -> NoReturn:
    raise ContractViolation(
        ContractError(
            run_id="paper.pipeline",
            stage=ErrorStage.DISCOVERY,
            code=code,
            message=message,
            details={"fallback_used": False, **details},
        )
    )


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


class _VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def ingest(path: Path, *, source_uri: str | None = None) -> PaperDocument:
    try:
        if path.stat().st_size > MAX_BYTES:
            fail(ErrorCode.PAPER_INPUT_INVALID, "Paper exceeds the 20 MiB input limit")
        raw = path.read_bytes()
        suffix = path.suffix.lower()
        media: Literal["application/pdf", "text/html", "text/plain"]
        if suffix == ".pdf":
            reader = PdfReader(path, strict=True)
            if reader.is_encrypted or len(reader.pages) > MAX_PAGES:
                fail(ErrorCode.PAPER_INPUT_INVALID, "Encrypted or oversized PDF is unsupported")
            texts = [page.extract_text() or "" for page in reader.pages]
            media = "application/pdf"
            parser = "pypdf-6.1.1"
        elif suffix in {".html", ".htm"}:
            html = _VisibleHTML()
            html.feed(raw.decode("utf-8"))
            texts = ["".join(html.parts)]
            media = "text/html"
            parser = "stdlib-html-v1"
        elif suffix in {".txt", ".md"}:
            texts = [raw.decode("utf-8")]
            media = "text/plain"
            parser = "utf8-v1"
        else:
            fail(ErrorCode.PAPER_INPUT_INVALID, "Expected PDF, HTML or UTF-8 text")
        if not any(normalize(t) for t in texts) or sum(map(len, texts)) > MAX_TEXT:
            fail(
                ErrorCode.PAPER_PARSE_FAILED, "Empty/scanned or oversized text; explicit OCR needed"
            )
        return PaperDocument(
            source_uri=source_uri or path.resolve().as_uri(),
            source_sha256=digest(raw),
            media_type=media,
            parser=parser,
            pages=tuple(
                SourcePage(number=i + 1, text=t, sha256=digest(t.encode()))
                for i, t in enumerate(texts)
            ),
        )
    except ContractViolation:
        raise
    except Exception as exc:
        fail(ErrorCode.PAPER_PARSE_FAILED, "Paper could not be parsed", cause=str(exc))
