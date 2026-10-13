"""DOCX -> PDF conversion (Gotenberg / LibreOffice) and checks on the resulting PDF.

The converter is an internal HTTP service (PDF_CONVERTER_URL, e.g. http://pdf:3000). Only the filled .docx
is sent to it; nothing is sent anywhere else. Every PDF is checked before it is stored.
"""
import io
import secrets
import zipfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from django.conf import settings
from pypdf import PdfReader
from pypdf.errors import PdfReadError

A4_POINTS = (595.3, 841.9)
MAX_PDF_BYTES = 20 * 1024 * 1024


class ConversionError(Exception):
    pass


def _is_docx(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return "word/document.xml" in zf.namelist()
    except zipfile.BadZipFile:
        return False


def convert_docx_to_pdf(docx_bytes):
    if not _is_docx(docx_bytes):
        # The converter would happily turn arbitrary text into a PDF; only filled Word documents are sent.
        raise ConversionError("Only Word (.docx) documents can be converted.")
    base = (settings.PDF_CONVERTER_URL or "").rstrip("/")
    if not base:
        raise ConversionError("The PDF converter is not configured (PDF_CONVERTER_URL).")
    boundary = "----gradtwin" + secrets.token_hex(12)
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"document.docx\"\r\n"
            "Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n"
            ).encode() + docx_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{base}/forms/libreoffice/convert", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=settings.PDF_CONVERTER_TIMEOUT) as resp:
            data = resp.read(MAX_PDF_BYTES + 1)
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        raise ConversionError(f"The PDF converter rejected the document (HTTP {exc.code}).") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ConversionError("The PDF converter is unavailable. Try again later.") from None
    if len(data) > MAX_PDF_BYTES:
        raise ConversionError("The converted PDF is unexpectedly large.")
    if "application/pdf" not in ctype or not data.startswith(b"%PDF-"):
        raise ConversionError("The converter did not return a PDF.")
    return data


@dataclass
class PdfInfo:
    page_count: int
    page_sizes: list
    fonts: set
    images_per_page: list
    text: str
    sha256: str = ""
    problems: list = field(default_factory=list)


def inspect_pdf(pdf_bytes):
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = list(reader.pages)
        sizes = [(round(float(p.mediabox.width), 1), round(float(p.mediabox.height), 1)) for p in pages]
        fonts, images, text = set(), [], []
        for p in pages:
            resources = p.get("/Resources") or {}
            for font in (resources.get("/Font") or {}).values():
                base = font.get_object().get("/BaseFont")
                if base:
                    fonts.add(str(base).split("+")[-1].lstrip("/"))
            images.append(len(p.images))
            text.append(p.extract_text() or "")
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise ConversionError(f"The generated PDF could not be read ({type(exc).__name__}).") from None
    return PdfInfo(page_count=len(pages), page_sizes=sizes, fonts=fonts, images_per_page=images,
                   text="\n".join(text))


def _normalise(text):
    return " ".join(text.replace(" ", " ").split())


def check_pdf(info, *, expected_values, min_images):
    """Problems with a generated certificate PDF (empty list = OK)."""
    problems = []
    if info.page_count != 1:
        problems.append(f"Expected 1 page, got {info.page_count}.")
    for w, h in info.page_sizes:
        if abs(w - A4_POINTS[0]) > 2 or abs(h - A4_POINTS[1]) > 2:
            problems.append(f"Page size {w}×{h} pt is not A4.")
    if sum(info.images_per_page[:1]) < min_images:
        problems.append(f"Expected at least {min_images} image(s) (letterhead, signature/seal) on the page, "
                        f"found {sum(info.images_per_page[:1])}.")
    if not info.fonts:
        problems.append("No fonts are embedded or referenced in the PDF.")
    text = _normalise(info.text)
    if "{{" in text or "}}" in text:
        problems.append("Unfilled placeholder text appears in the PDF.")
    for value in expected_values:
        if value and _normalise(value) not in text:
            problems.append("A filled value is missing from the PDF text (possible layout overflow).")
            break
    return problems
