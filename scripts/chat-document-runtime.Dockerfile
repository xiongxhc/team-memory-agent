# Build from the repository root with --target runtime (production dependencies)
# or --target test (synthetic fixture tools and parser integration tests).
# Export the resulting stopped container into a new operator-owned directory;
# BubblewrapSandbox(runtime_root=...) runs it natively without Docker privileges.
# The parser never receives the Docker socket or host mounts.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    bubblewrap=0.12.0-1~deb13u1 \
    libreoffice-impress=4:25.2.3-2+deb13u6 \
    tesseract-ocr=5.5.0-1+b1 \
    tesseract-ocr-eng=1:4.1.0-2 \
    tesseract-ocr-chi-sim=1:4.1.0-2 \
    fonts-noto-cjk=1:20240730+repack1-1 \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/worker && /opt/worker/bin/pip install --no-cache-dir \
    pypdf==6.18.1 pypdfium2==5.13.0 Pillow==12.3.0 defusedxml==0.7.1 olefile==0.47

FROM runtime AS test
RUN /opt/worker/bin/pip install --no-cache-dir \
    pytest==9.1.1 python-docx==1.2.0 python-pptx==1.0.2 openpyxl==3.1.5 reportlab==5.0.1
WORKDIR /proof
RUN mkdir -p teammem/chat tests && touch teammem/__init__.py teammem/chat/__init__.py
COPY teammem/chat/document_worker.py teammem/chat/document_worker.py
COPY tests/test_chat_document_worker.py tests/test_chat_document_worker.py
CMD ["/opt/worker/bin/python", "-m", "pytest", "-q", "tests/test_chat_document_worker.py"]
