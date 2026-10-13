"""Fill approved `{{ placeholder }}` slots in a validated .docx without any template engine.

The template's XML is parsed with lxml (entities, DTDs and network access disabled) so Word's namespaces
are preserved exactly. Each placeholder is located across Word's text runs and replaced with plain text in
the run where it starts (keeping that run's formatting). Values are inserted as XML text — never parsed or
evaluated — so student input cannot become markup or template code. All other parts are copied unchanged.
"""
import io
import re
import zipfile

from lxml import etree

from apps.doctemplates.docx import TEXT_PART, VALID_PLACEHOLDER

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
# Characters not allowed in XML 1.0 (control characters other than tab/newline/carriage return).
INVALID_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False,
                         remove_blank_text=False)


class FillError(Exception):
    pass


def clean_value(value):
    return INVALID_XML_CHARS.sub("", str(value))


def _own_text_nodes(paragraph):
    """w:t nodes belonging to this paragraph (not to paragraphs nested in its text boxes)."""
    nodes = []
    for t in paragraph.iter(f"{W}t"):
        p = t.getparent()
        while p is not None and p.tag != f"{W}p":
            p = p.getparent()
        if p is paragraph:
            nodes.append(t)
    return nodes


def _fill_paragraph(paragraph, values, used):
    nodes = _own_text_nodes(paragraph)
    if not nodes:
        return
    texts = [n.text or "" for n in nodes]
    joined = "".join(texts)
    matches = list(VALID_PLACEHOLDER.finditer(joined))
    if not matches:
        return
    starts, pos = [], 0  # start offset of each node in `joined`
    for t in texts:
        starts.append(pos)
        pos += len(t)

    def locate(offset):
        for i in range(len(nodes) - 1, -1, -1):
            if starts[i] <= offset:
                return i, offset - starts[i]
        return 0, 0

    for m in reversed(matches):  # right to left keeps earlier offsets valid
        name = m.group(1)
        if name not in values:
            raise FillError(f"No value for placeholder “{name}”.")
        used.add(name)
        value = clean_value(values[name])
        si, so = locate(m.start())
        ei, eo = locate(m.end() - 1)
        eo += 1
        if si == ei:
            texts[si] = texts[si][:so] + value + texts[si][eo:]
        else:
            texts[si] = texts[si][:so] + value
            for k in range(si + 1, ei):
                texts[k] = ""
            texts[ei] = texts[ei][eo:]
        for k in range(si, ei + 1):
            nodes[k].text = texts[k]
            nodes[k].set(XML_SPACE, "preserve")
    if VALID_PLACEHOLDER.search("".join(n.text or "" for n in nodes)):
        raise FillError("A placeholder could not be filled.")


def fill_docx(template_bytes, values):
    """Return the filled .docx bytes. Raises FillError if any placeholder is unknown or left unfilled."""
    used = set()
    try:
        src = zipfile.ZipFile(io.BytesIO(template_bytes))
    except zipfile.BadZipFile as exc:
        raise FillError("The template file is not a valid .docx.") from exc
    out = io.BytesIO()
    with src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info)
            if TEXT_PART.match(info.filename):
                try:
                    root = etree.fromstring(data, PARSER)
                except etree.XMLSyntaxError as exc:
                    raise FillError(f"{info.filename} is not valid XML.") from exc
                for p in root.iter(f"{W}p"):
                    _fill_paragraph(p, values, used)
                data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
                if b"{{" in data or b"}}" in data:
                    raise FillError("Template syntax remains in the filled document.")
            dst.writestr(info, data)
    return out.getvalue(), used
