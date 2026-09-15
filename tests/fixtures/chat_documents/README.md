# Synthetic document fixtures

`tests/test_chat_document_worker.py` creates all documents in pytest temporary
directories. No customer attachments, screenshots, or model-produced documents
are checked in.

The matrix covers Unicode TXT/Markdown, quoted CSV, inert HTML, DOCX paragraphs,
headers and tables, XLSX sheets/formulas/sparse coordinates, textual and scanned
PDFs, PNG/JPEG, and PPTX/legacy PPT slides with speaker notes and hidden slides.
Image fixtures include English and Chinese text rendered with an installed CJK
font. Archive fixtures contain traversal paths, links, nested archives, expansion
bombs, external entities and macros. PDF/OOXML image metadata tests enforce the
pixel bound before the native renderer/converter sees the image.

Base tests skip optional dependency and platform integrations. Install the optional
parser libraries and fixture tools to run format tests. On Linux with bubblewrap,
set `TEAMMEM_TEST_DOCUMENT_RUNTIME_ROOT` to an exported document runtime to run
native sandbox tests without installing LibreOffice/Tesseract on the host.
The runtime image recipe is `scripts/chat-document-runtime.Dockerfile`.
