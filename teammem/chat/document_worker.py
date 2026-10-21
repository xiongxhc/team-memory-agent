"""Bounded document extraction. Production callers MUST use parse_attachment.

The private extraction functions are the worker entry point, not a fallback for a
missing sandbox. The worker only receives an admitted file and a sanitized job;
session authorization, download budgets and artifact retention belong to its caller.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from typing import Mapping
import zipfile


DEFAULT_LIMITS = {
    "max_file_bytes": 30 * 1024**2,
    "max_uncompressed_bytes_per_file": 256 * 1024**2,
    "max_uncompressed_bytes_per_request": 512 * 1024**2,
    "max_archive_entries_per_file": 10_000,
    "max_archive_expansion_ratio": 100,
    "max_document_pages_or_slides": 200,
    "max_visual_pages_per_request": 20,
    "max_decoded_image_pixels": 20_000_000,
    "max_extracted_characters_per_file": 2_000_000,
    "parse_timeout_seconds": 60,
    "max_sheet_rows": 10_000,
    "max_sheet_columns": 256,
    "max_sheet_cells": 100_000,
}
MIMES = {
    ".pdf": {"application/pdf"},
    ".ppt": {"application/vnd.ms-powerpoint"},
    ".pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".html": {"text/html"}, ".htm": {"text/html"},
    ".csv": {"text/csv", "application/csv", "text/plain"},
    ".txt": {"text/plain"}, ".md": {"text/markdown", "text/plain"},
    ".png": {"image/png"}, ".jpg": {"image/jpeg"}, ".jpeg": {"image/jpeg"},
}
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


class DocumentError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _limits(job):
    given = job.get("limits") or {}
    result = dict(DEFAULT_LIMITS)
    for key in result:
        if key in given:
            value = given[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise DocumentError("invalid_limits", f"Invalid {key}")
            result[key] = min(result[key], value)
    return result


def _source(job, limits):
    name = job.get("filename", "")
    if not isinstance(name, str) or not name or len(name) > 255 or name in {".", ".."} or any(c in name for c in "/\\\x00") or any(ord(c) < 32 for c in name):
        raise DocumentError("unsafe_path", "Filename must be a plain basename")
    extension = Path(name).suffix.lower()
    allowed = (job.get("limits") or {}).get("allowed_extensions", list(MIMES))
    if extension not in MIMES or extension not in allowed:
        raise DocumentError("unsupported_format", "Unsupported document format")
    path = Path(job["path"])
    try:
        info = path.lstat()
    except OSError as error:
        raise DocumentError("missing_file", "Attachment file is unavailable") from error
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise DocumentError("unsafe_path", "Attachment must be a regular file, not a link")
    if info.st_size > limits["max_file_bytes"]:
        raise DocumentError("file_limit", "Attachment exceeds allowed bytes")
    mime = (job.get("mime_type") or "").split(";", 1)[0].strip().lower()
    if mime and mime != "application/octet-stream" and mime not in MIMES[extension]:
        raise DocumentError("type_mismatch", "Declared MIME does not match filename")
    return path, name, extension


class _Result:
    def __init__(self, job, output):
        self.job, self.output = job, Path(output)
        self.limits = _limits(job)
        self.path, self.name, self.ext = _source(job, self.limits)
        self.deadline = min(float(job.get("deadline", float("inf"))), time.monotonic() + self.limits["parse_timeout_seconds"])
        self.remaining_visual = max(0, min(int(job.get("visual_pages_remaining", 20)), self.limits["max_visual_pages_per_request"]))
        self.expanded_limit = min(self.limits["max_uncompressed_bytes_per_file"], int(job.get("expanded_bytes_remaining", self.limits["max_uncompressed_bytes_per_request"])))
        self.data = {"filename": self.name, "fragments": [], "images": [], "coverage": {
            "complete": True, "processed": [], "omitted": [], "warnings": [],
            "pages_or_slides": None, "characters": 0, "expanded_bytes": 0, "visual_pages": 0,
        }}
        self.check()
        self.output.mkdir(parents=True, exist_ok=True)

    @property
    def coverage(self):
        return self.data["coverage"]

    def check(self):
        if time.monotonic() >= self.deadline:
            raise DocumentError("timeout", "Document request deadline exceeded")

    def omit(self, locator):
        self.coverage["complete"] = False
        if locator not in self.coverage["omitted"] and len(self.coverage["omitted"]) < 1000:
            self.coverage["omitted"].append(locator)

    def warn(self, message):
        if message not in self.coverage["warnings"]:
            self.coverage["warnings"].append(message)

    def add(self, text, location, kind="text"):
        self.check()
        text = str(text).strip()
        if not text:
            return
        locator = f"{self.name}: {location}"
        if len(self.data["fragments"]) >= 20_000:
            self.omit(f"{self.name}: text after 20000 fragments (output limit)")
            return
        remaining = int(self.limits["max_extracted_characters_per_file"] - self.coverage["characters"])
        if len(text) > remaining:
            self.omit(locator + " (text truncated)")
        text = text[:remaining]
        if text:
            self.data["fragments"].append({"text": text, "locator": locator, "kind": kind})
            self.coverage["characters"] += len(text)
            if len(self.coverage["processed"]) < 1000:
                self.coverage["processed"].append(locator)

    def pages(self, count):
        if count > self.limits["max_document_pages_or_slides"]:
            raise DocumentError("page_limit", "Document has too many pages or slides")
        self.coverage["pages_or_slides"] = count

    def image(self, image, location):
        self.check()
        if image.width * image.height > self.limits["max_decoded_image_pixels"]:
            raise DocumentError("pixel_limit", "Decoded image exceeds allowed pixels")
        if not self.remaining_visual:
            self.omit(f"{self.name}: {location} (visual content)")
            return None
        target = self.output / f"visual-{len(self.data['images']) + 1}.png"
        image.convert("RGB").save(target, "PNG")
        _output_size(self.output, self.expanded_limit - self.coverage["expanded_bytes"])
        self.remaining_visual -= 1
        self.coverage["visual_pages"] += 1
        self.data["images"].append({"path": str(target), "locator": f"{self.name}: {location}", "kind": "visual"})
        return target


def _xml(data):
    from defusedxml.ElementTree import fromstring
    return fromstring(data)


def _archive(path, result):
    """Stream every member through admission before any OOXML parser sees it."""
    archive = zipfile.ZipFile(path)
    try:
        entries = archive.infolist()
        if len(entries) > result.limits["max_archive_entries_per_file"]:
            raise DocumentError("archive_limit", "Archive has too many entries")
        total = 0
        names = set()
        for info in entries:
            result.check()
            part = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            if info.filename in names or part.is_absolute() or ".." in part.parts or "\\" in info.filename or ":" in info.filename or "\x00" in info.filename:
                raise DocumentError("unsafe_archive", "Unsafe archive member path")
            names.add(info.filename)
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                raise DocumentError("unsafe_archive", "Archive links are forbidden")
            if info.flag_bits & 1:
                raise DocumentError("encrypted", "Password-protected archive is unsupported")
            if info.file_size > max(1, info.compress_size) * result.limits["max_archive_expansion_ratio"]:
                raise DocumentError("archive_limit", "Archive expansion ratio exceeds limit")
            if total + info.file_size > result.expanded_limit:
                raise DocumentError("archive_limit", "Expanded archive bytes exceed request/file limit")
            if part.suffix.lower() in {".zip", ".rar", ".7z", ".gz", ".tar", ".docx", ".pptx", ".xlsx", ".jar"}:
                raise DocumentError("unsafe_archive", "Nested archives are forbidden")
            if "vbaproject" in info.filename.lower() or "/embeddings/" in info.filename.lower():
                raise DocumentError("active_content", "Embedded executable objects/macros are unsupported")
            first = True
            with archive.open(info) as source:
                while chunk := source.read(65536):
                    result.check()
                    if first and chunk.startswith((b"PK\x03\x04", b"7z\xbc\xaf\x27\x1c", b"Rar!", b"\x1f\x8b")):
                        raise DocumentError("unsafe_archive", "Nested archives are forbidden")
                    first = False
                    total += len(chunk)
                    if total > result.expanded_limit:
                        raise DocumentError("archive_limit", "Expanded archive bytes exceed limit")
            if part.suffix.lower() == ".rels":
                root = _xml(archive.read(info))
                if any(e.get("TargetMode") == "External" for e in root):
                    result.warn("External references were ignored; no links were fetched or refreshed.")
            if part.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"}:
                from PIL import Image
                with archive.open(info) as source, Image.open(source) as image:
                    _pixels(image.width, image.height, result)
        result.coverage["expanded_bytes"] += total
        if result.coverage["expanded_bytes"] > result.expanded_limit:
            raise DocumentError("archive_limit", "Conversion expanded bytes exceed request/file limit")
        return archive
    except BaseException:
        archive.close()
        raise


def _relationship(archive, base, rel_id):
    part = PurePosixPath(base)
    rels = str(part.parent / "_rels" / (part.name + ".rels"))
    if rels not in archive.namelist():
        return None
    for element in _xml(archive.read(rels)):
        if element.get("Id") != rel_id or element.get("TargetMode") == "External":
            continue
        target = element.get("Target", "")
        import posixpath
        resolved = posixpath.normpath(posixpath.join(str(part.parent), target)) if not target.startswith("/") else target.lstrip("/")
        if resolved.startswith("../") or resolved not in archive.namelist():
            raise DocumentError("unsafe_archive", "Invalid document relationship")
        return resolved
    return None


def _docx(archive, result):
    body = _xml(archive.read("word/document.xml")).find("w:body", NS)
    if body is None:
        raise DocumentError("malformed", "DOCX body is missing")
    paragraph = table = 0
    for child in body:
        if child.tag == "{" + NS["w"] + "}p":
            paragraph += 1
            result.add("".join(t.text or "" for t in child.findall(".//w:t", NS)), f"paragraph {paragraph}")
        elif child.tag == "{" + NS["w"] + "}tbl":
            table += 1
            for row_index, row in enumerate(child.findall("w:tr", NS), 1):
                for column, cell in enumerate(row.findall("w:tc", NS), 1):
                    result.add(" ".join(t.text or "" for t in cell.findall(".//w:t", NS)), f"table {table}, row {row_index}, column {column}")
    if any(name.startswith("word/media/") for name in archive.namelist()):
        result.omit(f"{result.name}: embedded document images (structural extraction only)")
    for name in archive.namelist():
        if re.fullmatch(r"word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml", name):
            for index, paragraph in enumerate(_xml(archive.read(name)).findall(".//w:p", NS), 1):
                result.add("".join(t.text or "" for t in paragraph.findall(".//w:t", NS)), f"{PurePosixPath(name).stem}, paragraph {index}")


def _xlsx(archive, result):
    shared = []
    if "xl/sharedStrings.xml" in archive.namelist():
        shared = ["".join(n.text or "" for n in item.findall(".//s:t", NS)) for item in _xml(archive.read("xl/sharedStrings.xml"))]
    workbook = _xml(archive.read("xl/workbook.xml"))
    total = 0
    for sheet in workbook.findall("s:sheets/s:sheet", NS):
        path = _relationship(archive, "xl/workbook.xml", sheet.get("{" + NS["r"] + "}id"))
        if not path:
            raise DocumentError("malformed", "Missing sheet relationship")
        for cell in _xml(archive.read(path)).findall(".//s:sheetData/s:row/s:c", NS):
            result.check()
            reference = cell.get("r", "")
            match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", reference)
            if not match:
                raise DocumentError("malformed", "Invalid sheet cell reference")
            column = 0
            for char in match[1]:
                column = column * 26 + ord(char) - 64
            total += 1
            if int(match[2]) > result.limits["max_sheet_rows"] or column > result.limits["max_sheet_columns"] or total > result.limits["max_sheet_cells"]:
                raise DocumentError("sheet_limit", "Spreadsheet exceeds bounded sheet rows, columns or cells")
            value = cell.findtext("s:v", default="", namespaces=NS)
            formula = cell.findtext("s:f", default=None, namespaces=NS)
            if cell.get("t") == "s":
                value = shared[int(value)]
            elif cell.get("t") == "inlineStr":
                value = "".join(t.text or "" for t in cell.findall(".//s:t", NS))
            if formula is not None:
                value = f"={formula}; stored cached value: {value or '(absent)'}"
                result.warn("Formulas are not executed; stored cached values may be absent or stale.")
            result.add(value, f"{sheet.get('name', 'sheet')}!{reference}")
    if any(name.startswith(("xl/charts/", "xl/media/")) for name in archive.namelist()):
        result.omit(f"{result.name}: spreadsheet charts/images (stored cell values only)")


def _select(texts, result):
    question = str(result.job.get("question", "")).lower()
    terms = set(re.findall(r"\w+", question))
    requested = {int(number) - 1 for number in re.findall(r"(?:pages?\s*|slides?\s*|第\s*)(\d+)", question)}
    ranked = sorted(range(len(texts)), key=lambda i: (i not in requested, -sum(term in texts[i].lower() for term in terms), bool(texts[i].strip()), i))
    return set(ranked[:result.remaining_visual])


def _pixels(width, height, result):
    if not 0 < int(width) * int(height) <= result.limits["max_decoded_image_pixels"]:
        raise DocumentError("pixel_limit", "Embedded image exceeds allowed decoded pixels")


def _pdf_image_limits(page, reader, result):
    """Check XObjects, masks and inline rasters before PDFium decodes them."""
    from pypdf.generic import ContentStream
    pending, visited = [page], set()
    while pending:
        result.check()
        node = pending.pop().get_object()
        if id(node) in visited:
            continue
        visited.add(id(node))
        if len(visited) > 10_000:
            raise DocumentError("image_limit", "PDF image/resource traversal exceeds limit")
        if node.get("/Subtype") == "/Image":
            _pixels(node.get("/Width", 0), node.get("/Height", 0), result)
            mask = node.get("/SMask")
            if mask is not None and hasattr(mask.get_object(), "get"):
                pending.append(mask)
            continue
        resources = node.get("/Resources")
        if resources is not None:
            objects = resources.get_object().get("/XObject")
            if objects is not None:
                pending.extend(objects.get_object().values())
        stream = node if node.get("/Subtype") == "/Form" else node.get("/Contents")
        if stream is not None:
            for operands, operator in ContentStream(stream, reader).operations:
                if operator == b"INLINE IMAGE":
                    settings = operands["settings"]
                    _pixels(settings.get("/W", settings.get("/Width", 0)), settings.get("/H", settings.get("/Height", 0)), result)


def _pdf(path, result, location="page", extract=True):
    from pypdf import PdfReader
    import pypdfium2
    reader = PdfReader(str(path), strict=True)
    if reader.is_encrypted:
        raise DocumentError("encrypted", "Password-protected/encrypted PDF is unsupported")
    if not extract and result.coverage["pages_or_slides"] != len(reader.pages):
        raise DocumentError("conversion_failed", "Converted slide mapping differs from original presentation")
    result.pages(len(reader.pages))
    texts = []
    for index, page in enumerate(reader.pages, 1):
        result.check()
        text = page.extract_text() or ""
        texts.append(text)
        if extract:
            result.add(text, f"{location} {index}")
    selected = _select(texts, result)
    for index in selected:
        _pdf_image_limits(reader.pages[index], reader, result)
    pdf = pypdfium2.PdfDocument(str(path))
    try:
        for i in range(len(pdf)):
            result.check()
            where = f"{location} {i + 1}"
            if i not in selected:
                result.omit(f"{result.name}: {where} (visual content" + (" and OCR)" if not texts[i].strip() else ")"))
                continue
            page = pdf[i]
            width, height = page.get_size()
            if not width > 0 or not height > 0 or width * height * 4 > result.limits["max_decoded_image_pixels"]:
                raise DocumentError("pixel_limit", "Rendered page exceeds allowed pixels")
            bitmap = page.render(scale=2)
            try:
                image_path = result.image(bitmap.to_pil(), where)
            finally:
                bitmap.close(); page.close()
            if extract and not texts[i].strip() and image_path:
                _ocr(image_path, result, where)
    finally:
        pdf.close()


def _run(command, result):
    result.check()
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=max(.001, result.deadline - time.monotonic()), check=False)
    except subprocess.TimeoutExpired as error:
        raise DocumentError("timeout", "Document conversion/OCR deadline exceeded") from error
    if completed.returncode:
        raise DocumentError("conversion_failed", "Document conversion/OCR failed")


def _ocr(path, result, location):
    executable = shutil.which("tesseract")
    if not executable:
        result.omit(f"{result.name}: {location} OCR (Tesseract unavailable)")
        result.warn("OCR unavailable; the selected image is visual evidence, not extracted text.")
        return
    base = result.output / "ocr"
    try:
        _run([executable, str(path), str(base), "-l", "eng+chi_sim", "--psm", "3"], result)
        text = base.with_suffix(".txt").read_text(encoding="utf-8")
        result.add(text, location, "ocr")
        result.warn("OCR uses English and simplified Chinese; recognition may contain errors.")
        if not text.strip():
            result.omit(f"{result.name}: {location} OCR (no reliable text recognized)")
    except DocumentError as error:
        if error.code == "timeout":
            raise
        result.omit(f"{result.name}: {location} OCR failed")
        result.warn("OCR failed or required language data is unavailable; image evidence remains available.")
    finally:
        base.with_suffix(".txt").unlink(missing_ok=True)


def _convert(path, result, extension):
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if not executable:
        raise DocumentError("dependency_unavailable", "LibreOffice is required for presentation conversion/rendering")
    scratch = result.output / ".conversion" / extension
    scratch.mkdir(parents=True, exist_ok=True)
    profile = result.output / ".profile"
    profile.mkdir(exist_ok=True)
    # Highest macro security; automatic document link updates disabled. The
    # surrounding sandbox also removes the network and all source host paths.
    (profile / "user").mkdir(exist_ok=True)
    (profile / "user" / "registrymodifications.xcu").write_text('<oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item><item oor:path="/org.openoffice.Office.Common/Load"><prop oor:name="UpdateDocMode" oor:op="fuse"><value>0</value></prop></item></oor:items>')
    conversion = extension
    if extension == "pdf":
        conversion = 'pdf:impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"true"},"ExportNotesPages":{"type":"boolean","value":"false"}}'
    _run([executable, "-env:UserInstallation=" + profile.as_uri(), "--headless", "--nologo", "--nodefault", "--norestore", "--nofirststartwizard", "--convert-to", conversion, "--outdir", str(scratch), str(path)], result)
    target = scratch / (path.stem + "." + extension)
    if not target.is_file() or target.is_symlink():
        raise DocumentError("conversion_failed", "Presentation conversion produced no valid output")
    _output_size(result.output, result.expanded_limit)
    return target


def _pptx(path, result):
    with _archive(path, result) as archive:
        presentation = _xml(archive.read("ppt/presentation.xml"))
        ids = presentation.findall("p:sldIdLst/p:sldId", NS)
        result.pages(len(ids))
        for index, slide_id in enumerate(ids, 1):
            slide = _relationship(archive, "ppt/presentation.xml", slide_id.get("{" + NS["r"] + "}id"))
            if not slide:
                raise DocumentError("malformed", "Missing presentation slide")
            text = "\n".join("".join(t.text or "" for t in paragraph.findall(".//a:t", NS)) for paragraph in _xml(archive.read(slide)).findall(".//a:p", NS))
            result.add(text, f"slide {index}")
            relpath = str(PurePosixPath(slide).parent / "_rels" / (PurePosixPath(slide).name + ".rels"))
            if relpath in archive.namelist():
                for relation in _xml(archive.read(relpath)):
                    if relation.get("Type", "").endswith("/notesSlide"):
                        notes = _relationship(archive, slide, relation.get("Id"))
                        if notes:
                            root = _xml(archive.read(notes))
                            for shape in root.findall(".//p:sp", NS):
                                placeholder = shape.find("p:nvSpPr/p:nvPr/p:ph", NS)
                                if placeholder is not None and placeholder.get("type") == "body":
                                    result.add("\n".join(t.text or "" for t in shape.findall(".//a:t", NS)), f"slide {index}, speaker notes", "notes")
        if result.remaining_visual:
            try:
                rendered = _convert(path, result, "pdf")
                _pdf(rendered, result, "slide", extract=False)
            except DocumentError as error:
                if error.code != "dependency_unavailable":
                    raise
                result.warn(str(error))
                for index in range(1, len(ids) + 1):
                    result.omit(f"{result.name}: slide {index} (visual rendering unavailable)")
        else:
            for index in range(1, len(ids) + 1):
                result.omit(f"{result.name}: slide {index} (visual content)")


class _InertHTML(HTMLParser):
    def __init__(self, result):
        super().__init__(convert_charrefs=True)
        self.result, self.blocked, self.table, self.row, self.column = result, [], 0, 0, 0
        self.location = "text"

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "iframe", "object", "noscript", "template"}:
            self.blocked.append(tag)
        if self.blocked:
            return
        if tag == "table":
            self.table += 1; self.row = 0
        if tag == "tr":
            self.row += 1; self.column = 0
        if tag in {"td", "th"}:
            self.column += 1
            self.location = f"table {self.table}, row {self.row}, column {self.column}"
        elif re.fullmatch("h[1-6]", tag):
            self.location = f"heading at line {self.getpos()[0]}"
        elif tag in {"p", "div", "li"}:
            self.location = f"text at line {self.getpos()[0]}"

    def handle_endtag(self, tag):
        if self.blocked and tag == self.blocked[-1]:
            self.blocked.pop()

    def handle_data(self, data):
        if not self.blocked:
            self.result.add(data, self.location)


def _parse_document(job: Mapping, output: Path) -> dict:
    """Worker-only parser; tests may call with synthetic trusted fixtures."""
    result = _Result(job, output)
    try:
        with result.path.open("rb") as source:
            head = source.read(16)
        ext = result.ext
        if ext == ".pdf":
            if not head.startswith(b"%PDF-"):
                raise DocumentError("type_mismatch", "PDF signature does not match")
            _pdf(result.path, result)
        elif ext in {".pptx", ".docx", ".xlsx"}:
            if not head.startswith(b"PK\x03\x04"):
                raise DocumentError("type_mismatch", "OOXML signature does not match (encrypted files unsupported)")
            if ext == ".pptx":
                _pptx(result.path, result)
            else:
                with _archive(result.path, result) as archive:
                    (_docx if ext == ".docx" else _xlsx)(archive, result)
        elif ext == ".ppt":
            if not head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
                raise DocumentError("type_mismatch", "Legacy PPT signature does not match")
            import olefile
            with olefile.OleFileIO(str(result.path)) as ole:
                if ole.exists("EncryptedPackage") or not ole.exists("PowerPoint Document"):
                    raise DocumentError("encrypted", "Encrypted or invalid legacy PPT is unsupported")
            _pptx(_convert(result.path, result, "pptx"), result)
        elif ext in {".png", ".jpg", ".jpeg"}:
            from PIL import Image
            with Image.open(result.path) as image:
                if image.format != ("PNG" if ext == ".png" else "JPEG"):
                    raise DocumentError("type_mismatch", "Image signature does not match")
                if image.width * image.height > result.limits["max_decoded_image_pixels"]:
                    raise DocumentError("pixel_limit", "Decoded image exceeds allowed pixels")
                image.load()
                result.image(image, "image 1")
        else:
            text = result.path.read_bytes().decode("utf-8-sig")
            if "\x00" in text or any(ord(c) < 9 for c in text):
                raise DocumentError("type_mismatch", "Text contains binary content")
            if ext in {".html", ".htm"}:
                parser = _InertHTML(result); parser.feed(text); parser.close()
                result.warn("HTML scripts, styles and embedded resources were discarded; links were not fetched.")
            elif ext == ".csv":
                csv.field_size_limit(int(result.limits["max_extracted_characters_per_file"]))
                cells = 0
                for row_number, row in enumerate(csv.reader(io.StringIO(text)), 1):
                    if row_number > result.limits["max_sheet_rows"] or len(row) > result.limits["max_sheet_columns"]:
                        raise DocumentError("sheet_limit", "CSV exceeds row/column limit")
                    cells += len(row)
                    if cells > result.limits["max_sheet_cells"]:
                        raise DocumentError("sheet_limit", "CSV exceeds bounded cells")
                    for column, value in enumerate(row, 1):
                        result.add(value, f"row {row_number}, column {column}")
            else:
                for number, line in enumerate(text.splitlines(), 1):
                    result.add(line, f"line {number}")
        result.check()
        result.coverage["expanded_bytes"] += _output_size(result.output, result.expanded_limit - result.coverage["expanded_bytes"])
        return result.data
    except DocumentError:
        raise
    except ImportError as error:
        raise DocumentError("dependency_unavailable", "Optional document parser dependency is unavailable") from error
    except Exception as error:
        raise DocumentError("malformed", "Malformed or unsupported document content") from error
    finally:
        for directory in (".conversion", ".profile"):
            shutil.rmtree(result.output / directory, ignore_errors=True)


def _output_size(root, maximum):
    total = entries = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            entry = Path(directory) / name
            try:
                info = entry.lstat()
            except FileNotFoundError:
                # LibreOffice removes temporary profile/cache entries while the
                # parent monitors the directory. A vanished entry uses no space.
                continue
            entries += 1
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise DocumentError("unsafe_output", "Worker produced an unsafe output link/file")
            total += info.st_size if stat.S_ISREG(info.st_mode) else 0
            if total > maximum or entries > 10_000:
                raise DocumentError("output_limit", "Worker conversion/output bytes exceed limit")
    return total


class BubblewrapSandbox:
    """Linux-only runner. Never exposes the caller's HOME or environment.

    output_root must be a per-attachment application-owned directory; generated
    image paths are returned beneath it and must follow attachment retention.
    """
    def __init__(self, output_root: Path, *, python_executable=None, bubblewrap=None, cancel_event=None, runtime_root: Path | None = None):
        self.output_root = Path(output_root)
        self.runtime_root = Path(runtime_root).resolve() if runtime_root is not None else None
        self.python = str(python_executable or ("/opt/worker/bin/python" if self.runtime_root else sys.executable))
        self.bubblewrap = bubblewrap or shutil.which("bwrap")
        self.cancel_event = cancel_event

    def _runtime_path(self, logical):
        if not self.runtime_root:
            return Path(logical)
        virtual = PurePosixPath(logical)
        if not virtual.is_absolute() or ".." in virtual.parts:
            raise DocumentError("unsafe_runtime", "Invalid runtime path")
        source = self.runtime_root / str(virtual).lstrip("/")
        if not source.resolve().is_relative_to(self.runtime_root):
            raise DocumentError("unsafe_runtime", "A runtime mount escapes its configured root")
        return source

    def _command(self, inputs, output):
        if sys.platform != "linux" or not self.bubblewrap or not Path(self.bubblewrap).is_file():
            raise DocumentError("sandbox_unavailable", "Linux bubblewrap isolation is unavailable; parsing refused")
        command = [self.bubblewrap, "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv"]
        for directory in ("/usr", "/bin", "/lib", "/lib64"):
            source = self._runtime_path(directory)
            if source.exists():
                command += ["--ro-bind", str(source), directory]
        for path in ("/etc/fonts", "/etc/ld.so.cache", "/etc/libreoffice"):
            source = self._runtime_path(path)
            if source.exists():
                command += ["--ro-bind", str(source), path]
        # A dedicated worker venv is the only caller-selected runtime exposure.
        prefix = Path(self.python).absolute().parent.parent
        executable = self.python
        if prefix != Path("/usr"):
            command += ["--ro-bind", str(self._runtime_path(prefix)), "/runtime"]
            executable = "/runtime/bin/" + Path(self.python).name
        command += ["--proc", "/proc", "--dev", "/dev", "--size", "268435456", "--tmpfs", "/tmp", "--dir", "/tmp/home",
                    "--setenv", "HOME", "/tmp/home", "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "LANG", "C.UTF-8",
                    "--setenv", "OMP_THREAD_LIMIT", "1", "--ro-bind", str(inputs), "/input",
                    "--bind", str(output), "/output", "--ro-bind", str(Path(__file__).resolve()), "/worker.py",
                    "--chdir", "/tmp", executable, "-I", "/worker.py"]
        return command

    def check_runtime(self):
        """Verify libraries, converter linkage and OCR data in the actual sandbox."""
        return self.run({"limits": {"parse_timeout_seconds": 10}, "deadline": time.monotonic() + 10}, _runtime_check=True)

    def run(self, job, *, _runtime_check=False):
        limits = _limits(job)
        path, name, extension = (None, "runtime-check", "") if _runtime_check else _source(job, limits)
        deadline = min(float(job.get("deadline", float("inf"))), time.monotonic() + limits["parse_timeout_seconds"])
        self.output_root.mkdir(parents=True, exist_ok=True)
        artifacts = Path(tempfile.mkdtemp(prefix="document-", dir=self.output_root))
        process = None
        try:
            with tempfile.TemporaryDirectory(prefix="teammem-document-input-") as folder:
                inputs = Path(folder)
                command = self._command(inputs, artifacts)
                # O_NOFOLLOW closes the admission-to-open symlink race.
                if path is not None:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, "rb") as source, (inputs / ("source" + extension)).open("wb") as target:
                        total = 0
                        while chunk := source.read(65536):
                            total += len(chunk)
                            if total > limits["max_file_bytes"]:
                                raise DocumentError("file_limit", "Attachment bytes exceeded during copy")
                            target.write(chunk)
                payload = {"operation": "check_runtime" if _runtime_check else "parse", "path": "/input/source" + extension, "filename": name,
                           "mime_type": job.get("mime_type"), "question": str(job.get("question", ""))[:8000],
                           "limits": limits, "deadline": deadline,
                           "visual_pages_remaining": job.get("visual_pages_remaining", 20),
                           "expanded_bytes_remaining": job.get("expanded_bytes_remaining", limits["max_uncompressed_bytes_per_request"])}
                (inputs / "job.json").write_text(json.dumps(payload), encoding="utf-8")
                maximum = min(limits["max_uncompressed_bytes_per_file"], payload["expanded_bytes_remaining"])
                with (artifacts / ".stderr").open("wb") as stderr:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr, env={}, start_new_session=True)
                    while process.poll() is None:
                        if self.cancel_event is not None and self.cancel_event.is_set():
                            raise DocumentError("cancelled", "Document parsing was cancelled")
                        if time.monotonic() >= deadline:
                            raise DocumentError("timeout", "Document request deadline exceeded")
                        _output_size(artifacts, maximum)
                        time.sleep(.02)
                _output_size(artifacts, maximum)
                response_path = artifacts / "result.json"
                if process.returncode != 0 or not response_path.is_file():
                    raise DocumentError("sandbox_failed", "Isolated document worker failed; no fallback was used")
                if response_path.stat().st_size > 16 * 1024**2:
                    raise DocumentError("output_limit", "Worker response exceeds output limit")
                response = json.loads(response_path.read_text())
                if "error" in response:
                    raise DocumentError(response["error"]["code"], response["error"]["message"])
                for image in response.get("images", []):
                    relative = PurePosixPath(image["path"]).relative_to("/output")
                    if ".." in relative.parts:
                        raise DocumentError("unsafe_output", "Invalid worker image path")
                    host_path = artifacts / str(relative)
                    if not host_path.is_file() or host_path.is_symlink():
                        raise DocumentError("unsafe_output", "Missing/unsafe worker image")
                    image["path"] = str(host_path)
                response_path.unlink(); (artifacts / ".stderr").unlink()
                return response
        except BaseException:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            shutil.rmtree(artifacts, ignore_errors=True)
            raise


def parse_attachment(job: Mapping, sandbox: BubblewrapSandbox | None = None) -> dict:
    if sandbox is None:
        raise DocumentError("sandbox_unavailable", "An explicitly configured document sandbox is required")
    return sandbox.run(job)


def _check_runtime_inside(deadline):
    import importlib
    dependencies = ["defusedxml", "pypdf", "pypdfium2", "PIL", "olefile"]
    try:
        for name in dependencies:
            importlib.import_module("PIL.Image" if name == "PIL" else name)
    except (ImportError, OSError) as error:
        raise DocumentError("dependency_unavailable", "Document runtime Python dependencies are unavailable") from error
    binaries = {}
    languages = []
    for binary, option in [("libreoffice", "--version"), ("tesseract", "--version"), ("tesseract", "--list-langs")]:
        executable = shutil.which(binary)
        if not executable:
            raise DocumentError("dependency_unavailable", f"Document runtime is missing {binary}")
        try:
            result = subprocess.run([executable, option], capture_output=True, text=True,
                                    timeout=max(.001, deadline - time.monotonic()), check=False)
        except subprocess.TimeoutExpired as error:
            raise DocumentError("timeout", "Document runtime check deadline exceeded") from error
        if result.returncode or len(result.stdout) + len(result.stderr) > 65536:
            raise DocumentError("dependency_unavailable", f"Document runtime could not execute {binary}")
        if option == "--list-langs":
            languages = sorted(set(result.stdout.splitlines()) & {"eng", "chi_sim"})
            if set(languages) != {"eng", "chi_sim"}:
                raise DocumentError("dependency_unavailable", "Document runtime needs eng and chi_sim OCR languages")
        else:
            binaries[binary] = (result.stdout or result.stderr).splitlines()[0][:200]
    return {"dependencies": dependencies, "binaries": binaries, "languages": languages}


def _main():
    # Limits apply inside the child only, never to the live chat/collector process.
    job = json.loads(Path("/input/job.json").read_text())
    limits = _limits(job)
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_FSIZE, (int(limits["max_uncompressed_bytes_per_file"]),) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        result = _check_runtime_inside(job["deadline"]) if job.get("operation") == "check_runtime" else _parse_document(job, Path("/output"))
    except DocumentError as error:
        result = {"error": {"code": error.code, "message": str(error)}}
    Path("/output/result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    _main()
