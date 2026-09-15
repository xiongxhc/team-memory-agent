"""Synthetic document fixtures: no customer documents or remote resources."""
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading
import time
import zipfile

import pytest


@pytest.fixture
def worker():
    from teammem.chat import document_worker
    return document_worker


def _sandbox(worker, output, **kwargs):
    return worker.BubblewrapSandbox(output, runtime_root=os.environ.get("TEAMMEM_TEST_DOCUMENT_RUNTIME_ROOT"), **kwargs)


def parse(worker, tmp_path, name, data, **kwargs):
    source = tmp_path / name
    source.write_bytes(data)
    output = tmp_path / "out"
    output.mkdir(exist_ok=True)
    return worker._parse_document({"path": str(source), "filename": name, **kwargs}, output)


def zipped(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return stream.getvalue()


def test_unicode_text_preserves_source_lines(worker, tmp_path):
    result = parse(worker, tmp_path, "notes.md", "# Holiday\n你好，团队\n".encode())
    assert result["fragments"][1] == {"text": "你好，团队", "locator": "notes.md: line 2", "kind": "text"}
    assert result["coverage"]["complete"]


def test_text_character_limit_discloses_truncation(worker, tmp_path):
    result = parse(worker, tmp_path, "notes.txt", b"abcdefghij\nmore", limits={"max_extracted_characters_per_file": 5})
    assert sum(len(f["text"]) for f in result["fragments"]) == 5
    assert not result["coverage"]["complete"]
    assert result["coverage"]["omitted"]


def test_csv_cells_have_original_row_and_column(worker, tmp_path):
    result = parse(worker, tmp_path, "a.csv", b'name,value\n"two\nlines",42\n')
    assert any(f["text"] == "42" and f["locator"] == "a.csv: row 2, column 2" for f in result["fragments"])


def test_html_is_inert_and_keeps_table_locations(worker, tmp_path):
    result = parse(worker, tmp_path, "a.html", b'<h1>Report</h1><script>steal()</script><style>SECRET</style><iframe src="http://127.0.0.1/"></iframe><table><tr><td>Revenue</td><td>42</td></tr></table>')
    text = " ".join(f["text"] for f in result["fragments"])
    assert "Report" in text and "Revenue" in text and "42" in text
    assert "steal" not in text and "SECRET" not in text
    assert any("table 1" in f["locator"] for f in result["fragments"])


@pytest.mark.parametrize("name,data,code", [
    ("bad.pdf", b"not a pdf", "type_mismatch"),
    ("bad.docx", b"not a zip", "type_mismatch"),
    ("script.exe", b"MZ", "unsupported_format"),
    ("bad.txt", b"hello\x00binary", "type_mismatch"),
])
def test_signature_and_allowlist_fail_closed(worker, tmp_path, name, data, code):
    with pytest.raises(worker.DocumentError) as error:
        parse(worker, tmp_path, name, data)
    assert error.value.code == code


def test_mime_mismatch_rejected(worker, tmp_path):
    with pytest.raises(worker.DocumentError, match="MIME"):
        parse(worker, tmp_path, "a.txt", b"text", mime_type="application/pdf")


def test_source_symlink_and_unsafe_original_name_rejected(worker, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("private")
    link = tmp_path / "link.txt"
    link.symlink_to(source)
    for path, name in [(link, "link.txt"), (source, "../source.txt")]:
        with pytest.raises(worker.DocumentError):
            worker._parse_document({"path": str(path), "filename": name}, tmp_path / "out")


@pytest.mark.parametrize("entries,limits", [
    ({"../outside": b"oops"}, {}),
    ({"nested.zip": b"PK\x03\x04"}, {}),
    ({"word/document.xml": b"x" * 20000}, {}),
    ({"a": b"a", "b": b"b"}, {"max_archive_entries_per_file": 1}),
    ({"word/document.xml": b"123456"}, {"max_uncompressed_bytes_per_file": 5}),
])
def test_unsafe_archives_rejected_before_document_library(worker, tmp_path, entries, limits):
    with pytest.raises(worker.DocumentError):
        parse(worker, tmp_path, "bad.docx", zipped(entries), limits=limits)


def test_archive_symlink_rejected(worker, tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        member = zipfile.ZipInfo("word/document.xml")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(member, "/etc/passwd")
    with pytest.raises(worker.DocumentError, match="link"):
        parse(worker, tmp_path, "a.docx", stream.getvalue())


def test_docx_paragraph_table_citations_never_invent_pages(worker, tmp_path):
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_heading("Status", 1)
    doc.add_paragraph("你好 team")
    doc.add_table(rows=1, cols=2).cell(0, 1).text = "done"
    stream = io.BytesIO(); doc.save(stream)
    result = parse(worker, tmp_path, "report.docx", stream.getvalue())
    assert any(f["text"] == "done" and "table 1, row 1, column 2" in f["locator"] for f in result["fragments"])
    assert all("page" not in f["locator"] for f in result["fragments"])


def test_docx_header_is_extracted_with_structural_citation(worker, tmp_path):
    docx = pytest.importorskip("docx")
    doc = docx.Document(); doc.sections[0].header.paragraphs[0].text = "Important header"
    doc.add_paragraph("Body")
    stream = io.BytesIO(); doc.save(stream)
    result = parse(worker, tmp_path, "report.docx", stream.getvalue())
    assert any(f["text"] == "Important header" and "header" in f["locator"] for f in result["fragments"])


def test_explicit_question_page_selects_that_page(worker, tmp_path):
    source = tmp_path / "a.txt"; source.write_text("test")
    result = worker._Result({"path": str(source), "filename": "a.txt", "question": "Please explain page 3", "visual_pages_remaining": 1}, tmp_path / "out")
    assert worker._select(["", "", ""], result) == {2}


def test_xlsx_sheets_formulas_and_missing_cache_disclosed(worker, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook(); book.active.title = "Costs"
    book.active["A1"] = 12; book.active["B1"] = "=A1*2"
    book.create_sheet("中文")["C3"] = "完成"
    stream = io.BytesIO(); book.save(stream)
    result = parse(worker, tmp_path, "book.xlsx", stream.getvalue())
    assert any(f["text"] == "完成" and "中文!C3" in f["locator"] for f in result["fragments"])
    assert any("=A1*2" in f["text"] for f in result["fragments"])
    assert any("cached" in warning for warning in result["coverage"]["warnings"])


def test_sparse_xlsx_coordinate_limit_before_traversal(worker, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook(); book.active["XFD1048576"] = "far away"
    stream = io.BytesIO(); book.save(stream)
    with pytest.raises(worker.DocumentError, match="sheet"):
        parse(worker, tmp_path, "sparse.xlsx", stream.getvalue())


def test_pptx_notes_are_separate_and_original_slide_cited(worker, tmp_path):
    pptx = pytest.importorskip("pptx")
    deck = pptx.Presentation(); slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "计划 Plan"
    slide.notes_slide.notes_text_frame.text = "Speaker-only evidence"
    stream = io.BytesIO(); deck.save(stream)
    result = parse(worker, tmp_path, "deck.pptx", stream.getvalue(), visual_pages_remaining=0)
    assert any(f["text"] == "Speaker-only evidence" and f["kind"] == "notes" and f["locator"] == "deck.pptx: slide 1, speaker notes" for f in result["fragments"])
    assert any("计划" in f["text"] and f["locator"] == "deck.pptx: slide 1" for f in result["fragments"])
    assert result["coverage"]["omitted"]


def test_pdf_text_and_bounded_selected_visual_pages(worker, tmp_path):
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    pytest.importorskip("pypdfium2")
    stream = io.BytesIO(); doc = canvas.Canvas(stream)
    for label in ["Ordinary page", "Revenue chart", "Last page"]:
        doc.drawString(100, 700, label); doc.showPage()
    doc.save()
    result = parse(worker, tmp_path, "report.pdf", stream.getvalue(), question="revenue chart", visual_pages_remaining=1)
    assert any("Revenue" in f["text"] and f["locator"] == "report.pdf: page 2" for f in result["fragments"])
    assert len(result["images"]) == 1
    assert result["images"][0]["locator"] == "report.pdf: page 2"
    assert Path(result["images"][0]["path"]).is_file()
    assert not result["coverage"]["complete"]


def test_encrypted_pdf_rejected(worker, tmp_path):
    pypdf = pytest.importorskip("pypdf")
    doc = pypdf.PdfWriter(); doc.add_blank_page(100, 100); doc.encrypt("secret")
    stream = io.BytesIO(); doc.write(stream)
    with pytest.raises(worker.DocumentError, match="encrypted"):
        parse(worker, tmp_path, "secret.pdf", stream.getvalue())


def test_pdf_page_count_rejected(worker, tmp_path):
    pypdf = pytest.importorskip("pypdf")
    doc = pypdf.PdfWriter()
    for _ in range(3): doc.add_blank_page(100, 100)
    stream = io.BytesIO(); doc.write(stream)
    with pytest.raises(worker.DocumentError, match="pages"):
        parse(worker, tmp_path, "long.pdf", stream.getvalue(), limits={"max_document_pages_or_slides": 2})


def test_converted_presentation_count_must_preserve_slide_mapping(worker, tmp_path):
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("pypdfium2")
    source = tmp_path / "a.txt"; source.write_text("test")
    result = worker._Result({"path": str(source), "filename": "a.txt"}, tmp_path / "out")
    result.pages(2)
    doc = pypdf.PdfWriter(); doc.add_blank_page(100, 100)
    converted = tmp_path / "converted.pdf"
    with converted.open("wb") as output: doc.write(output)
    with pytest.raises(worker.DocumentError, match="slide mapping"):
        worker._pdf(converted, result, "slide", extract=False)


@pytest.mark.parametrize("ext,fmt", [("png", "PNG"), ("jpg", "JPEG")])
def test_image_decode_and_pixel_budget(worker, tmp_path, ext, fmt):
    Image = pytest.importorskip("PIL.Image")
    stream = io.BytesIO(); Image.new("RGB", (20, 20)).save(stream, fmt)
    result = parse(worker, tmp_path, "photo." + ext, stream.getvalue())
    assert result["images"][0]["locator"] == "photo." + ext + ": image 1"
    with pytest.raises(worker.DocumentError, match="pixels"):
        parse(worker, tmp_path, "photo." + ext, stream.getvalue(), limits={"max_decoded_image_pixels": 100})


def test_pdf_embedded_image_pixels_checked_before_render(worker, tmp_path):
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("pypdfium2")
    from pypdf.generic import DictionaryObject, NameObject, NumberObject, DecodedStreamObject
    writer = pypdf.PdfWriter(); page = writer.add_blank_page(100, 100)
    image = DecodedStreamObject(); image.set_data(b"\0\0\0")
    image.update({NameObject("/Type"):NameObject("/XObject"),NameObject("/Subtype"):NameObject("/Image"),NameObject("/Width"):NumberObject(10000),NameObject("/Height"):NumberObject(10000),NameObject("/ColorSpace"):NameObject("/DeviceRGB"),NameObject("/BitsPerComponent"):NumberObject(8)})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"):DictionaryObject({NameObject("/Im1"):writer._add_object(image)})})
    content = DecodedStreamObject(); content.set_data(b"q 100 0 0 100 0 0 cm /Im1 Do Q")
    page[NameObject("/Contents")] = writer._add_object(content)
    stream = io.BytesIO(); writer.write(stream)
    with pytest.raises(worker.DocumentError, match="pixels"):
        parse(worker, tmp_path, "huge-image.pdf", stream.getvalue())


def test_ooxml_embedded_image_pixels_checked_before_converter(worker, tmp_path):
    Image = pytest.importorskip("PIL.Image")
    stream = io.BytesIO(); Image.new("RGB", (20, 20)).save(stream, "PNG")
    data = zipped({"ppt/media/image.png":stream.getvalue()})
    with pytest.raises(worker.DocumentError, match="pixels"):
        parse(worker, tmp_path, "huge-image.pptx", data, limits={"max_decoded_image_pixels":100})


def test_request_expansion_and_deadline_pass_through(worker, tmp_path):
    with pytest.raises(worker.DocumentError, match="deadline"):
        parse(worker, tmp_path, "a.txt", b"text", deadline=time.monotonic() - 1)
    with pytest.raises(worker.DocumentError, match="bytes"):
        parse(worker, tmp_path, "a.txt", b"too big", limits={"max_file_bytes": 2})


def test_rendered_images_charge_shared_expanded_budget(worker, tmp_path):
    Image = pytest.importorskip("PIL.Image")
    stream = io.BytesIO(); Image.new("RGB", (20, 20), "red").save(stream, "PNG")
    result = parse(worker, tmp_path, "a.png", stream.getvalue())
    assert result["coverage"]["expanded_bytes"] >= Path(result["images"][0]["path"]).stat().st_size > 0


def test_xml_entities_rejected_without_reading_host_file(worker, tmp_path):
    xml = b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>'
    with pytest.raises(worker.DocumentError):
        parse(worker, tmp_path, "bad.docx", zipped({"word/document.xml": xml}))


def test_archive_expanded_request_budget_rejected(worker, tmp_path):
    with pytest.raises(worker.DocumentError, match="bytes"):
        parse(worker, tmp_path, "bad.docx", zipped({"word/document.xml": b"123456"}), expanded_bytes_remaining=5)


def test_external_xlsx_links_are_disclosed_not_executed(worker, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook(); book.active["A1"] = "=HYPERLINK(\"https://example.invalid/\",\"x\")"
    stream = io.BytesIO(); book.save(stream)
    result = parse(worker, tmp_path, "link.xlsx", stream.getvalue())
    assert "HYPERLINK" in result["fragments"][0]["text"]
    assert "not executed" in result["coverage"]["warnings"][0]


def test_untrusted_macro_archive_rejected(worker, tmp_path):
    with pytest.raises(worker.DocumentError, match="macros"):
        parse(worker, tmp_path, "bad.pptx", zipped({"ppt/vbaProject.bin": b"active"}))


def test_sparse_csv_cell_total_limit(worker, tmp_path):
    with pytest.raises(worker.DocumentError, match="cells"):
        parse(worker, tmp_path, "a.csv", b"a,b\nc,d\n", limits={"max_sheet_cells": 3})


def test_too_many_text_fragments_disclose_omitted_remainder(worker, tmp_path):
    result = parse(worker, tmp_path, "many.txt", b"x\n" * 25000)
    assert len(result["fragments"]) <= 20000
    assert not result["coverage"]["complete"]


def test_symlink_parent_rejected(worker, tmp_path):
    real = tmp_path / "real"; real.mkdir(); (real / "a.txt").write_text("text")
    alias = tmp_path / "alias"; alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(worker.DocumentError, match="link"):
        worker._parse_document({"path": str(alias / "a.txt"), "filename": "a.txt"}, tmp_path / "out")


def test_sandbox_unavailable_never_falls_back(worker, tmp_path):
    source = tmp_path / "a.txt"; source.write_text("test")
    sandbox = worker.BubblewrapSandbox(tmp_path / "artifacts", bubblewrap="/definitely/missing/bwrap")
    with pytest.raises(worker.DocumentError) as error:
        worker.parse_attachment({"path": str(source), "filename": "a.txt"}, sandbox)
    assert error.value.code == "sandbox_unavailable"


def test_bundled_runtime_mounts_only_selected_runtime_directories(worker, tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    for directory in ["usr", "bin", "lib", "opt/worker/bin"]:
        (runtime / directory).mkdir(parents=True)
    monkeypatch.setattr(worker.sys, "platform", "linux")
    runner = worker.BubblewrapSandbox(tmp_path / "out", runtime_root=runtime, bubblewrap=sys.executable)
    command = runner._command(tmp_path / "input", tmp_path / "out")
    assert str(runtime / "usr") in command
    assert str(runtime / "opt/worker") in command
    assert "/runtime/bin/python" in command
    assert str(runtime) not in command


def test_bundled_runtime_cannot_escape_its_root(worker, tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"; runtime.mkdir()
    (runtime / "usr").symlink_to("/usr", target_is_directory=True)
    monkeypatch.setattr(worker.sys, "platform", "linux")
    runner = worker.BubblewrapSandbox(tmp_path / "out", runtime_root=runtime, bubblewrap=sys.executable)
    with pytest.raises(worker.DocumentError, match="runtime"):
        runner._command(tmp_path / "input", tmp_path / "out")


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="requires Linux bubblewrap")
def test_runtime_check_verifies_dependencies_binaries_and_languages(worker, tmp_path):
    if not os.environ.get("TEAMMEM_TEST_DOCUMENT_RUNTIME_ROOT") and not shutil.which("libreoffice"):
        pytest.skip("requires complete document runtime")
    result = _sandbox(worker, tmp_path / "runtime-probe").check_runtime()
    assert set(result["dependencies"]) == {"defusedxml", "pypdf", "pypdfium2", "PIL", "olefile"}
    assert set(result["binaries"]) == {"libreoffice", "tesseract"}
    assert {"eng", "chi_sim"} <= set(result["languages"])


@pytest.mark.parametrize("missing_module", ["pypdf", "PIL.Image"])
def test_runtime_check_refuses_missing_dependency(worker, monkeypatch, missing_module):
    import importlib
    original = importlib.import_module
    def missing(name, *args, **kwargs):
        if name == missing_module:
            raise ImportError("synthetic missing dependency")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(worker.DocumentError, match="dependencies"):
        worker._check_runtime_inside(time.monotonic() + 10)


def test_runner_timeout_kills_descendants_and_removes_artifacts(worker, tmp_path, monkeypatch):
    source = tmp_path / "a.txt"; source.write_text("test")
    marker = tmp_path / "late-child-output"
    child = "import time; from pathlib import Path; time.sleep(.5); Path(" + repr(str(marker)) + ").write_text('late')"
    program = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(5)"
    sandbox = worker.BubblewrapSandbox(tmp_path / "artifacts")
    monkeypatch.setattr(sandbox, "_command", lambda *_: [sys.executable, "-c", program])
    with pytest.raises(worker.DocumentError) as error:
        sandbox.run({"path": str(source), "filename": "a.txt", "deadline": time.monotonic() + .15})
    assert error.value.code == "timeout"
    time.sleep(.6)
    assert not marker.exists()
    assert list((tmp_path / "artifacts").iterdir()) == []


def test_runner_cancel_removes_artifacts(worker, tmp_path, monkeypatch):
    source = tmp_path / "a.txt"; source.write_text("test")
    cancelled = threading.Event(); cancelled.set()
    sandbox = worker.BubblewrapSandbox(tmp_path / "artifacts", cancel_event=cancelled)
    monkeypatch.setattr(sandbox, "_command", lambda *_: [sys.executable, "-c", "import time; time.sleep(10)"])
    with pytest.raises(worker.DocumentError) as error:
        sandbox.run({"path": str(source), "filename": "a.txt"})
    assert error.value.code == "cancelled"
    assert list((tmp_path / "artifacts").iterdir()) == []


def test_runner_crash_never_returns_partial_output(worker, tmp_path, monkeypatch):
    source = tmp_path / "a.txt"; source.write_text("test")
    sandbox = worker.BubblewrapSandbox(tmp_path / "artifacts")
    monkeypatch.setattr(sandbox, "_command", lambda *_: [sys.executable, "-c", "raise RuntimeError('crash')"])
    with pytest.raises(worker.DocumentError) as error:
        sandbox.run({"path": str(source), "filename": "a.txt"})
    assert error.value.code == "sandbox_failed"
    assert list((tmp_path / "artifacts").iterdir()) == []


def test_runner_conversion_output_bomb_is_stopped(worker, tmp_path, monkeypatch):
    source = tmp_path / "a.txt"; source.write_text("test")
    sandbox = worker.BubblewrapSandbox(tmp_path / "artifacts")
    def command(inputs, outputs):
        program = "import time; from pathlib import Path; Path(" + repr(str(outputs / "huge")) + ").write_bytes(b'x'*10000); time.sleep(5)"
        return [sys.executable, "-c", program]
    monkeypatch.setattr(sandbox, "_command", command)
    with pytest.raises(worker.DocumentError) as error:
        sandbox.run({"path": str(source), "filename": "a.txt", "expanded_bytes_remaining": 500})
    assert error.value.code == "output_limit"
    assert list((tmp_path / "artifacts").iterdir()) == []


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="requires Linux bubblewrap")
@pytest.mark.parametrize("kind", ["txt", "csv", "html", "docx", "xlsx", "pdf", "pptx", "png"])
def test_linux_sandbox_real_parser_round_trip(worker, tmp_path, kind):
    source = tmp_path / ("a." + kind)
    if kind == "docx":
        document = pytest.importorskip("docx").Document(); document.add_paragraph("hello 团队"); document.save(source)
    elif kind == "xlsx":
        book = pytest.importorskip("openpyxl").Workbook(); book.active["A1"] = "hello 团队"; book.save(source)
    elif kind == "pptx":
        deck = pytest.importorskip("pptx").Presentation(); deck.slides.add_slide(deck.slide_layouts[1]).shapes.title.text = "hello 团队"; deck.save(source)
    elif kind == "pdf":
        doc = pytest.importorskip("reportlab.pdfgen.canvas").Canvas(str(source)); doc.drawString(100, 700, "hello"); doc.save()
    elif kind == "png":
        pytest.importorskip("PIL.Image").new("RGB", (30, 30), "red").save(source)
    else:
        source.write_text("<p>hello 团队</p>" if kind == "html" else "hello 团队")
    result = worker.parse_attachment({"path": str(source), "filename": "original." + kind}, _sandbox(worker, tmp_path / "artifacts"))
    if kind != "png":
        assert any("hello" in f["text"] for f in result["fragments"])
    if kind in {"png", "pdf", "pptx"}:
        assert result["images"]
    assert all(f["locator"].startswith("original." + kind) for f in result["fragments"] + result["images"])


@pytest.mark.skipif(not shutil.which("tesseract") and not os.environ.get("TEAMMEM_TEST_DOCUMENT_RUNTIME_ROOT"), reason="requires Tesseract eng and chi_sim")
def test_scanned_english_and_chinese_pdf_ocr(worker, tmp_path):
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    ImageFont = pytest.importorskip("PIL.ImageFont")
    font_paths = ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "/System/Library/Fonts/PingFang.ttc"]
    if os.environ.get("TEAMMEM_TEST_DOCUMENT_RUNTIME_ROOT"):
        font_paths.insert(0, os.environ["TEAMMEM_TEST_DOCUMENT_RUNTIME_ROOT"] + "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    font_path = next((path for path in font_paths if Path(path).exists()), None)
    if font_path is None:
        pytest.skip("requires CJK font for synthetic scan")
    picture = Image.new("RGB", (1400, 400), "white")
    ImageDraw.Draw(picture).text((60, 100), "Team status 团队计划", font=ImageFont.truetype(font_path, 65), fill="black")
    stream = io.BytesIO(); picture.save(stream, "PDF")
    result = parse(worker, tmp_path, "scan.pdf", stream.getvalue())
    if sys.platform == "linux" and shutil.which("bwrap"):
        result = worker.parse_attachment({"path": str(tmp_path / "scan.pdf"), "filename": "scan.pdf"}, _sandbox(worker, tmp_path / "isolated-scan"))
    ocr = " ".join(f["text"] for f in result["fragments"] if f["kind"] == "ocr")
    assert "Team" in ocr and "团队" in ocr
    assert result["images"][0]["locator"] == "scan.pdf: page 1"
    assert any("recognition" in warning for warning in result["coverage"]["warnings"])


@pytest.mark.skipif(not (shutil.which("libreoffice") or shutil.which("soffice") or os.environ.get("TEAMMEM_TEST_DOCUMENT_RUNTIME_ROOT")), reason="requires LibreOffice")
def test_pptx_render_and_legacy_ppt_keep_original_slide_locators(worker, tmp_path):
    pptx = pytest.importorskip("pptx")
    deck = pptx.Presentation(); slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "Original Plan"
    slide.notes_slide.notes_text_frame.text = "Hidden note"
    hidden = deck.slides.add_slide(deck.slide_layouts[1]); hidden.shapes.title.text = "Hidden Slide"; hidden._element.set("show", "0")
    third = deck.slides.add_slide(deck.slide_layouts[1]); third.shapes.title.text = "Third Slide"
    stream = io.BytesIO(); deck.save(stream)
    pptx_result = parse(worker, tmp_path, "modern.pptx", stream.getvalue())
    if os.environ.get("TEAMMEM_TEST_DOCUMENT_RUNTIME_ROOT"):
        pptx_result = worker.parse_attachment({"path": str(tmp_path / "modern.pptx"), "filename": "modern.pptx"}, _sandbox(worker, tmp_path / "isolated-modern"))
    assert pptx_result["images"][0]["locator"] == "modern.pptx: slide 1"
    assert len(pptx_result["images"]) == 3
    # Only fixture-generation code runs LibreOffice outside the sandbox, against
    # this test's own generated trusted presentation.
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if os.environ.get("TEAMMEM_TEST_DOCUMENT_RUNTIME_ROOT"):
        command = _sandbox(worker, tmp_path)._command(tmp_path, tmp_path)
        command[-3:] = ["/usr/bin/libreoffice", "-env:UserInstallation=file:///tmp/fixture-profile", "--headless", "--convert-to", "ppt", "--outdir", "/output", "/input/modern.pptx"]
    else:
        command = [executable, "-env:UserInstallation=" + (tmp_path / "fixture-profile").as_uri(), "--headless", "--convert-to", "ppt", "--outdir", str(tmp_path), str(tmp_path / "modern.pptx")]
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    (tmp_path / "original.ppt").write_bytes((tmp_path / "modern.ppt").read_bytes())
    if sys.platform == "linux" and shutil.which("bwrap"):
        legacy = worker.parse_attachment({"path": str(tmp_path / "original.ppt"), "filename": "original.ppt"}, _sandbox(worker, tmp_path / "isolated-legacy"))
    else:
        legacy = parse(worker, tmp_path, "original.ppt", (tmp_path / "modern.ppt").read_bytes())
    assert legacy["images"][0]["locator"] == "original.ppt: slide 1"
    assert any(f["kind"] == "notes" and f["locator"] == "original.ppt: slide 1, speaker notes" for f in legacy["fragments"])


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="requires Linux bubblewrap")
def test_linux_sandbox_denies_host_file_network_and_secrets(worker, tmp_path):
    sentinel = tmp_path / "host-secret"; sentinel.write_text("secret")
    inputs = tmp_path / "input"; inputs.mkdir()
    output = tmp_path / "output"; output.mkdir()
    sandbox = _sandbox(worker, output)
    command = sandbox._command(inputs, output)
    code = "import os,socket; assert not os.path.exists(" + repr(str(sentinel)) + "); assert 'TEAMMEM_TEST_SECRET' not in os.environ; s=socket.socket(); s.settimeout(.2); r=s.connect_ex(('1.1.1.1',443)); assert r != 0"
    command[-3:] = [command[-3], "-c", code]
    result = subprocess.run(command, env={"TEAMMEM_TEST_SECRET": "secret"}, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
