"""Небольшой DOCX writer протоколов на стандартной библиотеке.

Поддерживает заголовки, статическое содержание, абзацы и таблицы. Не пытается
воспроизводить бинарные шаблоны `.doc` исходного комплекса: текст шаблона уже
нормализован ``ProtocolTemplate.render``.
"""

from __future__ import annotations

import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape

from .reporting import ProtocolTemplate, ReportContext

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_WIDTH_DXA = 9354


def _text(value: object) -> str:
    """Экранирует текст и удаляет кодпоинты, запрещённые в XML 1.0."""
    text = str(value)
    sanitized = "".join(
        char
        for char in text
        if ord(char) in (0x9, 0xA, 0xD)
        or 0x20 <= ord(char) <= 0xD7FF
        or 0xE000 <= ord(char) <= 0xFFFD
        or 0x10000 <= ord(char) <= 0x10FFFF
    )
    return escape(sanitized, {'"': "&quot;"})


def _paragraph(
    text: str = "",
    *,
    style: str | None = None,
    align: str | None = None,
    indent: int | None = None,
    tabs: bool = False,
    bookmark: tuple[int, str] | None = None,
) -> str:
    properties = []
    if style:
        properties.append(f'<w:pStyle w:val="{style}"/>')
    if tabs:
        properties.append('<w:tabs><w:tab w:val="right" w:leader="dot" w:pos="9000"/></w:tabs>')
    properties.append('<w:spacing w:after="120" w:line="300" w:lineRule="auto"/>')
    if indent is not None:
        properties.append(f'<w:ind w:left="{indent}"/>')
    if align:
        properties.append(f'<w:jc w:val="{align}"/>')
    ppr = f"<w:pPr>{''.join(properties)}</w:pPr>" if properties else ""
    bookmark_start = bookmark_end = ""
    if bookmark is not None:
        bookmark_id, bookmark_name = bookmark
        bookmark_start = (
            f'<w:bookmarkStart w:id="{bookmark_id}" w:name="{_text(bookmark_name)}"/>'
        )
        bookmark_end = f'<w:bookmarkEnd w:id="{bookmark_id}"/>'
    if not text:
        return f"<w:p>{ppr}{bookmark_start}{bookmark_end}</w:p>"
    return (
        f"<w:p>{ppr}{bookmark_start}<w:r>"
        f"<w:t xml:space=\"preserve\">{_text(text)}</w:t></w:r>{bookmark_end}</w:p>"
    )


def _cell(text: str, width: int, *, header: bool = False) -> str:
    shading = '<w:shd w:val="clear" w:color="auto" w:fill="EEF3F6"/>' if header else ""
    bold = "<w:b/>" if header else ""
    return (
        f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shading}</w:tcPr>'
        f'<w:p><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
        f'<w:r><w:rPr>{bold}</w:rPr><w:t xml:space="preserve">{_text(text)}</w:t></w:r></w:p></w:tc>'
    )


def _table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    columns = max(1, max(len(row) for row in rows))
    width = CONTENT_WIDTH_DXA // columns
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for _ in range(columns))
    borders = "".join(
        f'<w:{name} w:val="single" w:sz="4" w:space="0" w:color="D8E0E3"/>'
        for name in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    body = []
    for index, row in enumerate(rows):
        padded = row + [""] * (columns - len(row))
        cells = "".join(_cell(value, width, header=index == 0) for value in padded)
        body.append(f"<w:tr>{cells}</w:tr>")
    return (
        '<w:tbl><w:tblPr><w:tblW w:w="9354" w:type="dxa"/>'
        '<w:tblBorders>' + borders + '</w:tblBorders>'
        '<w:tblCellMar><w:top w:w="120" w:type="dxa"/><w:left w:w="120" w:type="dxa"/>'
        '<w:bottom w:w="120" w:type="dxa"/><w:right w:w="120" w:type="dxa"/></w:tblCellMar>'
        '</w:tblPr><w:tblGrid>' + grid + '</w:tblGrid>' + "".join(body) + '</w:tbl>'
    )


def _toc_entry(title: str, level: int, bookmark: str) -> str:
    properties = (
        f'<w:pPr><w:pStyle w:val="TOC{level}"/>'
        '<w:tabs><w:tab w:val="right" w:leader="dot" w:pos="9000"/></w:tabs>'
        f'<w:ind w:left="{(level - 1) * 360}"/></w:pPr>'
    )
    return (
        f'<w:p>{properties}<w:r><w:t xml:space="preserve">{_text(title)}</w:t></w:r>'
        '<w:r><w:tab/></w:r>'
        '<w:r><w:fldChar w:fldCharType="begin" w:dirty="true"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve"> PAGEREF {_text(bookmark)} \\h </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    )


def _toc(entries: list[tuple[int, str, str]]) -> str:
    result = [_paragraph("Содержание", style="Heading1")]
    for level, title, bookmark in entries:
        result.append(_toc_entry(title, level, bookmark))
    return "".join(result)


def _styles() -> str:
    heading = (
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
        '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
        '<w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/></w:pPr>'
        '<w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="SimSun" w:cs="Arial"/>'
        '<w:b/><w:sz w:val="30"/><w:szCs w:val="30"/></w:rPr></w:style>'
    )
    heading2 = heading.replace('w:styleId="Heading1"', 'w:styleId="Heading2"')
    heading2 = heading2.replace('w:val="heading 1"', 'w:val="heading 2"').replace('w:val="30"', 'w:val="26"')
    heading3 = heading.replace('w:styleId="Heading1"', 'w:styleId="Heading3"')
    heading3 = heading3.replace('w:val="heading 1"', 'w:val="heading 3"').replace('w:val="30"', 'w:val="24"')
    toc = (
        '<w:style w:type="paragraph" w:styleId="TOC1"><w:name w:val="toc 1"/>'
        '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
        '<w:pPr><w:tabs><w:tab w:val="right" w:leader="dot" w:pos="9000"/></w:tabs></w:pPr></w:style>'
    )
    toc2 = toc.replace('w:styleId="TOC1"', 'w:styleId="TOC2"').replace('w:val="toc 1"', 'w:val="toc 2"')
    toc3 = toc.replace('w:styleId="TOC1"', 'w:styleId="TOC3"').replace('w:val="toc 1"', 'w:val="toc 3"')
    return (
        f'<w:styles xmlns:w="{W_NS}">'
        '<w:docDefaults><w:rPrDefault><w:rPr>'
        '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="SimSun" w:cs="Times New Roman"/>'
        '<w:sz w:val="24"/><w:szCs w:val="24"/><w:lang w:val="ru-RU" w:eastAsia="ru-RU"/>'
        '</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>'
        '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:jc w:val="center"/></w:pPr>'
        '<w:rPr><w:b/><w:sz w:val="36"/><w:szCs w:val="36"/></w:rPr></w:style>'
        + heading + heading2 + heading3 + toc + toc2 + toc3 +
        '<w:style w:type="paragraph" w:styleId="Header"><w:name w:val="Header"/><w:basedOn w:val="Normal"/>'
        '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:jc w:val="center"/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Footer"><w:name w:val="Footer"/><w:basedOn w:val="Normal"/>'
        '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:jc w:val="center"/></w:pPr></w:style>'
        '</w:styles>'
    )


def _document(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}"><w:body>{body}'
        '<w:sectPr><w:headerReference w:type="default" r:id="rId2"/>'
        '<w:footerReference w:type="default" r:id="rId3"/>'
        '<w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1276" w:bottom="1440" w:left="1276" w:header="708" w:footer="708" w:gutter="0"/>'
        '<w:cols w:space="708"/><w:docGrid w:linePitch="360"/></w:sectPr>'
        '</w:body></w:document>'
    )


def _header(title: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:hdr xmlns:w="{W_NS}" xmlns:r="{R_NS}">{_paragraph(title, style="Header")}</w:hdr>'
    )


def _footer() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:ftr xmlns:w="{W_NS}" xmlns:r="{R_NS}">'
        '<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r>'
        '<w:fldChar w:fldCharType="begin"/><w:instrText xml:space="preserve"> PAGE </w:instrText>'
        '<w:fldChar w:fldCharType="separate"/><w:t>1</w:t><w:fldChar w:fldCharType="end"/>'
        '</w:r></w:p>'
        '</w:ftr>'
    )


def _parts(title: str) -> dict[str, str]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
            '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
            '<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
            '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
            '</Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{PKG_REL_NS}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            '</Relationships>'
        ),
        "word/_rels/document.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{PKG_REL_NS}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>'
            '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>'
            '</Relationships>'
        ),
        "word/settings.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:settings xmlns:w="{W_NS}"><w:updateFields w:val="true"/></w:settings>'
        ),
        "word/styles.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + _styles(),
        "word/header1.xml": _header(title),
        "word/footer1.xml": _footer(),
        "docProps/core.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f'<dc:title>{_text(title)}</dc:title><dc:creator>OpenRadioInspector</dc:creator>'
            '<cp:lastModifiedBy>OpenRadioInspector</cp:lastModifiedBy>'
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
            '</cp:coreProperties>'
        ),
        "docProps/app.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            '<Application>OpenRadioInspector</Application><DocSecurity>0</DocSecurity><ScaleCrop>false</ScaleCrop>'
            '<Company></Company><LinksUpToDate>false</LinksUpToDate><SharedDoc>false</SharedDoc>'
            '<HyperlinksChanged>false</HyperlinksChanged><AppVersion>0.2</AppVersion></Properties>'
        ),
    }


def _is_table_delimiter(row: list[str]) -> bool:
    """Распознаёт Markdown-разделитель `---`, включая маркеры выравнивания."""
    return bool(row) and all(
        cell and "-" in cell and not set(cell) - {"-", ":", " "}
        for cell in row
    )


def _markdown_blocks(text: str) -> Iterable[tuple[str, object]]:
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("|"):
            rows = []
            while index < len(lines) and lines[index].startswith("|"):
                row = [cell.strip() for cell in lines[index].strip("|").split("|")]
                if not _is_table_delimiter(row):
                    rows.append(row)
                index += 1
            yield "table", rows
            continue
        if line.startswith("### "):
            yield "heading3", line[4:].strip()
        elif line.startswith("## "):
            yield "heading2", line[3:].strip()
        elif line.startswith("# "):
            yield "heading1", line[2:].strip()
        elif line.strip():
            yield "paragraph", line
        index += 1


def write_docx_report(
    template: ProtocolTemplate, context: ReportContext, output: str | Path
) -> Path:
    """Создаёт DOCX протокол атомарно и возвращает путь к нему."""
    rendered = template.render(context)
    blocks = list(_markdown_blocks(rendered))
    headings = [
        (
            index,
            1 if kind == "heading1" else 2 if kind == "heading2" else 3,
            str(value),
        )
        for index, (kind, value) in enumerate(blocks)
        if kind in ("heading1", "heading2", "heading3")
    ]
    title = headings[0][2] if headings else "Протокол измерений"
    body = [_paragraph(title, style="Title")]
    body.append(
        _toc(
            [
                (level, heading, f"_ori_heading_{index + 1}")
                for index, level, heading in headings[1:]
            ]
        )
    )
    for index, (kind, value) in enumerate(blocks):
        if kind == "table":
            body.append(_table(value))  # type: ignore[arg-type]
        elif kind == "paragraph":
            body.append(_paragraph(str(value)))
        else:
            bookmark = (index + 1, f"_ori_heading_{index + 1}")
            body.append(
                _paragraph(
                    str(value), style=str(kind).capitalize(), bookmark=bookmark
                )
            )
    parts = _parts(title)
    parts["word/document.xml"] = _document("".join(body))
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=destination.name + ".",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as staged:
            temporary = Path(staged.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as package:
            for name, data in parts.items():
                package.writestr(name, data.encode("utf-8"))
        temporary.replace(destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination
