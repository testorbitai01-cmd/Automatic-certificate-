"""Defensive inspection of uploaded Word (.docx) templates.

Nothing here modifies the template. It checks the file is a safe, ordinary Word document,
extracts paragraph text (body, headers, footers, text boxes) and validates `{{ placeholder }}`
usage against the approved registry. Untrusted XML is never resolved: any DOCTYPE/ENTITY is refused.
"""
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from .placeholders import REGISTRY, allowed_for

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
HYPERLINK_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
MAIN_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"

MAX_ENTRIES = 500
MAX_TOTAL_UNCOMPRESSED = 60 * 1024 * 1024
MAX_PART_UNCOMPRESSED = 20 * 1024 * 1024
MAX_RATIO = 200  # compressed-to-uncompressed ratio above this (for parts > 1 MB) suggests a zip bomb

VALID_PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")
ANY_OPEN = re.compile(r"\{\{(.{0,60}?)(\}\}|$)", re.S)
TEXT_PART = re.compile(r"^word/(document|header\d*|footer\d*)\.xml$")
TEMPLATE_SYNTAX = ("{{", "}}", "{%", "%}", "{#", "#}")
# Archive/decompression failures for entries whose directory looks valid.
READ_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError, ValueError)


class TemplateFileError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Inspection:
    parts: list = field(default_factory=list)       # [(part_label, [paragraph_text, ...])]
    placeholders: list = field(default_factory=list)  # sorted unique names
    image_count: int = 0

    def as_json(self):
        return {"placeholders": self.placeholders, "image_count": self.image_count,
                "paragraphs": sum(len(p) for _, p in self.parts)}


def _part_label(name):
    base = name.rsplit("/", 1)[1].removesuffix(".xml")
    return "Body" if base == "document" else base.rstrip("0123456789").capitalize()


def _safe_xml(data, name):
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise TemplateFileError([f"{name} contains a DOCTYPE/ENTITY declaration, which Word documents never need."])
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise TemplateFileError([f"{name} is not valid XML; the file may be damaged."]) from None


def _paragraph_texts(root):
    """Text of each paragraph in document order, without duplicating nested text-box paragraphs."""
    parent = {child: p for p in root.iter() for child in p}
    texts = {}
    order = []
    for el in root.iter():
        if el.tag == f"{W}p":
            texts[el] = []
            order.append(el)
            continue
        if el.tag not in (f"{W}t", f"{W}tab", f"{W}br", f"{W}cr"):
            continue
        p = parent.get(el)
        while p is not None and p.tag != f"{W}p":
            p = parent.get(p)
        if p is None:
            continue
        texts[p].append(el.text or "" if el.tag == f"{W}t" else ("\t" if el.tag == f"{W}tab" else "\n"))
    return ["".join(texts[p]) for p in order if "".join(texts[p]).strip()]


def _read(zf, info):
    if info.file_size > MAX_PART_UNCOMPRESSED:
        raise TemplateFileError([f"{info.filename} is unusually large."])
    if info.file_size > 1024 * 1024 and info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
        raise TemplateFileError(["The file is compressed in a suspicious way (possible zip bomb)."])
    try:
        return zf.read(info)
    except READ_ERRORS:
        raise TemplateFileError(["The file is damaged and cannot be read. Save it again from Word."]) from None


def _unvalidated_text(root, *, skip_paragraph_text):
    """All text that is NOT validated paragraph text: other element text (field codes, deleted text,
    comments, footnotes...) and every attribute value (alt text, field instructions, link targets...).
    docxtpl renders the whole XML through Jinja, so template syntax here must be refused."""
    chunks = []
    for el in root.iter():
        if not (skip_paragraph_text and el.tag == f"{W}t") and el.text:
            chunks.append(el.text)
        chunks.extend(el.attrib.values())
    return "".join(chunks)


def inspect_docx(fileobj, cert_type):
    """Validate a .docx template and return an Inspection. Raises TemplateFileError listing every problem."""
    try:
        zf = zipfile.ZipFile(fileobj)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError):
        raise TemplateFileError(["This is not a valid Word (.docx) file."]) from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_TOTAL_UNCOMPRESSED:
            raise TemplateFileError(["The file is too large or complex to be a certificate template."])
        if any(i.flag_bits & 0x1 for i in infos):
            raise TemplateFileError(["Encrypted or password-protected documents are not accepted."])
        for info in infos:  # zip-bomb check on every entry (from the directory; nothing is decompressed)
            if info.file_size > MAX_PART_UNCOMPRESSED or (
                    info.file_size > 1024 * 1024 and info.file_size > MAX_RATIO * max(info.compress_size, 1)):
                raise TemplateFileError(["The file is compressed in a suspicious way (possible zip bomb) "
                                         "or contains an unusually large part."])
        names = {i.filename for i in infos}
        if any(n.startswith("/") or ".." in n.split("/") for n in names):
            raise TemplateFileError(["The file contains invalid internal paths."])
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise TemplateFileError(["This is not a Word document (.docx)."])

        errors = []
        content_types = _read(zf, zf.getinfo("[Content_Types].xml"))
        _safe_xml(content_types, "[Content_Types].xml")
        if b"macroEnabled" in content_types or "word/vbaProject.bin" in names:
            errors.append("Macro-enabled documents are not accepted. Save the template as a plain .docx.")
        elif MAIN_CONTENT_TYPE.encode() not in content_types:
            errors.append("This is not a standard Word document (.docx).")
        if any(n.startswith(("word/activeX/", "word/embeddings/")) for n in names):
            errors.append("Embedded objects or ActiveX controls are not accepted in templates.")

        for info in infos:
            if info.filename.endswith(".rels"):
                root = _safe_xml(_read(zf, info), info.filename)
                for rel in root.iter(f"{REL_NS}Relationship"):
                    if rel.get("TargetMode") == "External" and rel.get("Type") != HYPERLINK_REL:
                        errors.append("The document loads external content (linked images, remote templates or "
                                      "objects). Embed images in the file instead.")
                        break
            if info.filename.endswith((".xml", ".rels")):
                # Template syntax is allowed only in validated paragraph text of body/headers/footers.
                root = _safe_xml(_read(zf, info), info.filename)
                hidden = _unvalidated_text(root, skip_paragraph_text=bool(TEXT_PART.match(info.filename)))
                if any(token in hidden for token in TEMPLATE_SYNTAX):
                    errors.append(f"Template syntax was found outside the main text ({info.filename}: e.g. "
                                  "comments, footnotes, field codes, tracked changes or image descriptions). "
                                  "Placeholders may only appear in the visible text of the body, headers and "
                                  "footers; remove the rest and accept all tracked changes.")

        inspection = Inspection(image_count=sum(1 for n in names if n.startswith("word/media/")))
        text_parts = sorted((n for n in names if TEXT_PART.match(n)),
                            key=lambda n: (0 if "header" in n else 1 if "document" in n else 2, n))
        for name in text_parts:
            paragraphs = _paragraph_texts(_safe_xml(_read(zf, zf.getinfo(name)), name))
            inspection.parts.append((_part_label(name), paragraphs))

    errors.extend(_placeholder_errors(inspection, cert_type))
    if errors:
        raise TemplateFileError(list(dict.fromkeys(errors)))
    return inspection


def _placeholder_errors(inspection, cert_type):
    errors, found = [], set()
    allowed = allowed_for(cert_type)
    for label, paragraphs in inspection.parts:
        for text in paragraphs:
            if "{%" in text or "%}" in text or "{#" in text or "#}" in text:
                errors.append(f"{label}: template logic ({{% %}} or {{# #}}) is not allowed; "
                              "use only approved {{ placeholders }}.")
            stripped = VALID_PLACEHOLDER.sub("", text)
            for match in ANY_OPEN.finditer(stripped):
                errors.append(f"{label}: malformed placeholder “{{{{{match.group(1).strip()[:40]}”. "
                              "Use the form {{ placeholder_name }} with an approved name.")
            if "}}" in stripped:
                errors.append(f"{label}: a closing “}}}}” has no matching “{{{{”.")
            for name in VALID_PLACEHOLDER.findall(text):
                if name not in REGISTRY:
                    errors.append(f"“{{{{ {name} }}}}” is not an approved placeholder.")
                elif name not in allowed:
                    errors.append(f"“{{{{ {name} }}}}” cannot be used in this certificate type.")
                else:
                    found.add(name)
    if not found and not errors:
        errors.append("No placeholders were found. This looks like a completed document, not a template: "
                      "replace the student-specific text with approved {{ placeholders }}.")
    inspection.placeholders = sorted(found)
    return errors


def preview(inspection, values):
    """Paragraphs split into (text, placeholder_name|None) segments with sample values substituted.

    Values are returned as plain strings; the HTML template escapes them.
    """
    parts = []
    for label, paragraphs in inspection.parts:
        rendered = []
        for text in paragraphs:
            segments, pos = [], 0
            for m in VALID_PLACEHOLDER.finditer(text):
                if m.start() > pos:
                    segments.append((text[pos:m.start()], None))
                segments.append((values.get(m.group(1), ""), m.group(1)))
                pos = m.end()
            if pos < len(text):
                segments.append((text[pos:], None))
            rendered.append(segments)
        parts.append((label, rendered))
    return parts
