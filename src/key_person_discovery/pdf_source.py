from __future__ import annotations

from io import BytesIO
from urllib.parse import urlparse
from urllib.request import Request

from pypdf import PdfReader

from .models import CrawledPage
from .sources import _open_request


def extract_pdf_urls(
    urls: list[str],
    proxy_url: str,
    max_documents: int = 3,
    max_bytes: int = 15_000_000,
    max_pages: int = 60,
    timeout: float = 30,
) -> list[CrawledPage]:
    """Download bounded public PDFs and expose extracted text as evidence pages."""
    pages = []
    for url in urls[:max_documents]:
        try:
            pages.append(
                _extract_pdf(
                    url,
                    proxy_url=proxy_url,
                    max_bytes=max_bytes,
                    max_pages=max_pages,
                    timeout=timeout,
                )
            )
        except Exception as exc:
            pages.append(
                CrawledPage(
                    url=url,
                    markdown="",
                    error=str(exc),
                    provider="pypdf",
                    source_type="pdf_extract",
                )
            )
    return pages


def _extract_pdf(
    url: str,
    proxy_url: str,
    max_bytes: int,
    max_pages: int,
    timeout: float,
) -> CrawledPage:
    if urlparse(url).scheme not in {"http", "https"}:
        raise RuntimeError("PDF URL must use HTTP or HTTPS")
    request = Request(url, headers={"User-Agent": "key-person-discovery/0.2"})
    response = _open_request(request, timeout, proxy_url)
    with response:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise RuntimeError(f"PDF exceeds {max_bytes} bytes")
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RuntimeError(f"PDF exceeds {max_bytes} bytes")
    if b"%PDF-" not in data[:1024]:
        raise RuntimeError("response is not a PDF")

    reader = PdfReader(BytesIO(data), strict=True)
    if reader.is_encrypted and reader.decrypt("") == 0:
        raise RuntimeError("PDF is encrypted")

    extracted = []
    errors = []
    extracted_pages = 0
    total_characters = 0
    for index, page in enumerate(reader.pages[:max_pages], start=1):
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception as exc:
            errors.append(f"page {index}: {exc}")
            continue
        text = text.strip()
        if not text:
            continue
        remaining = 250_000 - total_characters
        if remaining <= 0:
            break
        text = text[:remaining]
        extracted.append(f"[PDF page {index}]\n{text}")
        extracted_pages += 1
        total_characters += len(text)

    markdown = "\n\n".join(extracted)
    if not markdown:
        errors.append("PDF contains no extractable text; OCR required")
    if len(reader.pages) > max_pages:
        errors.append(f"limited to first {max_pages} of {len(reader.pages)} pages")
    return CrawledPage(
        url=url,
        markdown=markdown,
        error="; ".join(errors),
        provider="pypdf",
        source_type="pdf_extract",
        page_count=extracted_pages,
    )
