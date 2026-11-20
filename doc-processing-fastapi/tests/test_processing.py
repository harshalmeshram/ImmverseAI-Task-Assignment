"""Unit tests for LocalDocumentProcessor. Tesseract is mocked so tests don't need it installed."""

import pytesseract
import pytest
from PIL import Image

from app.processing import LocalDocumentProcessor, PermanentProcessingError, ProcessingError

from .conftest import make_pdf


@pytest.fixture
def processor():
    return LocalDocumentProcessor(ocr_language="eng")


@pytest.fixture
def fake_ocr(monkeypatch):
    calls = []

    def image_to_string(image, lang):
        calls.append(image)
        return "OCR TEXT"

    monkeypatch.setattr(pytesseract, "image_to_string", image_to_string)
    return calls


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_pdf_text_layer_is_extracted_without_ocr(processor, tmp_path, fake_ocr):
    path = write(tmp_path, "a.pdf", make_pdf("Quarterly report"))
    assert "Quarterly report" in processor.process(path, "application/pdf")
    assert fake_ocr == []


def test_pdf_page_without_text_falls_back_to_ocr(processor, tmp_path, fake_ocr):
    path = write(tmp_path, "scan.pdf", make_pdf(text=""))
    assert processor.process(path, "application/pdf") == "OCR TEXT"
    assert len(fake_ocr) == 1


def test_image_is_ocrd(processor, tmp_path, fake_ocr):
    path = tmp_path / "img.png"
    Image.new("RGB", (20, 20), "white").save(path)
    assert processor.process(str(path), "image/png") == "OCR TEXT"


def test_missing_file_is_permanent_error(processor, tmp_path):
    with pytest.raises(PermanentProcessingError):
        processor.process(str(tmp_path / "gone.pdf"), "application/pdf")


def test_corrupt_pdf_is_permanent_error(processor, tmp_path):
    path = write(tmp_path, "bad.pdf", b"%PDF-1.7 garbage")
    with pytest.raises(PermanentProcessingError):
        processor.process(path, "application/pdf")


def test_ocr_engine_unavailable_is_retryable(processor, tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "image_to_string", missing)
    path = tmp_path / "img.png"
    Image.new("RGB", (20, 20)).save(path)

    with pytest.raises(ProcessingError) as excinfo:
        processor.process(str(path), "image/png")
    assert excinfo.value.retryable is True


def test_clean_text_strips_trailing_spaces_and_extra_blank_lines():
    from app.processing import clean_text

    raw = "Appraisal Letter \r\nTo,  \n\n\n\nName: A   \n  indented stays\n\n"
    assert clean_text(raw) == "Appraisal Letter\nTo,\n\nName: A\n  indented stays"
