"""Write a synthetic TEST template (.docx) for trying the upload/preview workflow.

It contains no Gradtwin wording, branding, signature or seal: just a "TEST TEMPLATE – NOT FOR ISSUE"
heading and one line per approved placeholder. Upload it with "authentic Gradtwin template" unticked.
"""
import io
import zipfile
from xml.sax.saxutils import escape

from django.core.management.base import BaseCommand, CommandError

from apps.core.choices import CertificateType
from apps.doctemplates.placeholders import allowed_for

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>'
)
ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'officeDocument" Target="word/document.xml"/></Relationships>'
)


def build_docx(paragraphs):
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>' for text in paragraphs)
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body>{body}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("word/document.xml", document)
    return buf.getvalue()


def demo_paragraphs(cert_type):
    lines = ["TEST TEMPLATE – NOT FOR ISSUE", f"Certificate type: {CertificateType(cert_type).label}",
             "This file only demonstrates placeholders. It is not a Gradtwin document."]
    lines += [f"{p.label}: {{{{ {name} }}}}" for name, p in sorted(allowed_for(cert_type).items())]
    return lines


class Command(BaseCommand):
    help = "Write a synthetic TEST template .docx (no Gradtwin content) listing every approved placeholder."

    def add_arguments(self, parser):
        parser.add_argument("cert_type", choices=CertificateType.values)
        parser.add_argument("output", help="Path of the .docx file to write")

    def handle(self, *args, cert_type, output, **options):
        if not output.lower().endswith(".docx"):
            raise CommandError("The output file must end in .docx")
        with open(output, "wb") as fh:
            fh.write(build_docx(demo_paragraphs(cert_type)))
        self.stdout.write(self.style.SUCCESS(f"Wrote test template for {cert_type} to {output}"))
