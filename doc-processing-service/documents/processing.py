"""Text extraction, isolated behind the `DocumentProcessor` interface.

The worker only depends on `DocumentProcessor.process()`, so the local PyMuPDF/Tesseract
implementation can be swapped (e.g. for a hosted OCR service) via the DOCUMENT_PROCESSOR setting.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

import pymupdf
import pytesseract
from django.conf import settings
from django.utils.module_loading import import_string
from PIL import Image, ImageSequence

logger = logging.getLogger(__name__)


class ProcessingError(Exception):
    """Processing failed; the job may succeed if retried (e.g. OCR engine/service unavailable)."""

    retryable = True


class PermanentProcessingError(ProcessingError):
    """Processing can never succeed for this input (corrupt or missing file); do not retry."""

    retryable = False


class DocumentProcessor(ABC):
    @abstractmethod
    def process(self, file_path: str, content_type: str) -> str:
        """Return the text extracted from the file. Raise ProcessingError on failure."""


class LocalDocumentProcessor(DocumentProcessor):
    """PDF: use the embedded text layer, OCR-ing only pages that have none. Images: OCR."""

    def __init__(self, ocr_language: str | None = None, ocr_dpi: int = 200) -> None:
        self.ocr_language = ocr_language or settings.OCR_LANGUAGE
        self.ocr_dpi = ocr_dpi

    def process(self, file_path: str, content_type: str) -> str:
        if not Path(file_path).is_file():
            raise PermanentProcessingError(f"File not found: {file_path}")
        if content_type == "application/pdf":
            return self._process_pdf(file_path)
        if content_type.startswith("image/"):
            return self._process_image(file_path)
        raise PermanentProcessingError(f"Unsupported content type: {content_type}")

    def _process_pdf(self, file_path: str) -> str:
        try:
            doc = pymupdf.open(file_path)
        except Exception as exc:
            raise PermanentProcessingError(f"Cannot open PDF: {exc}") from exc

        pages: list[str] = []
        with doc:
            for page in doc:
                text = page.get_text().strip()
                if not text:  # scanned page without a text layer
                    pix = page.get_pixmap(dpi=self.ocr_dpi)
                    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    text = self._ocr(image)
                pages.append(text)
        return "\n\n".join(pages).strip()

    def _process_image(self, file_path: str) -> str:
        try:
            with Image.open(file_path) as image:
                # Multi-page TIFFs have several frames.
                return "\n\n".join(self._ocr(frame.convert("RGB")) for frame in ImageSequence.Iterator(image)).strip()
        except ProcessingError:
            raise
        except Exception as exc:
            raise PermanentProcessingError(f"Cannot read image: {exc}") from exc

    def _ocr(self, image: Image.Image) -> str:
        try:
            return pytesseract.image_to_string(image, lang=self.ocr_language).strip()
        except pytesseract.TesseractNotFoundError as exc:
            raise ProcessingError("OCR engine (tesseract) is not available") from exc
        except pytesseract.TesseractError as exc:
            raise ProcessingError(f"OCR failed: {exc}") from exc


def get_processor() -> DocumentProcessor:
    return import_string(settings.DOCUMENT_PROCESSOR)()
