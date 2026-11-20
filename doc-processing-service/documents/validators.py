"""Upload validation. The file type is decided from its magic bytes, never from the client's
declared Content-Type or file extension, and the file is then parsed to make sure it is not corrupt."""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf
from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from PIL import Image

from .exceptions import FileTooLarge, InvalidDocument, UnsupportedFileType


@dataclass(frozen=True)
class FileType:
    mime: str
    extension: str

    @property
    def is_pdf(self) -> bool:
        return self.mime == "application/pdf"


PDF = FileType("application/pdf", ".pdf")
PNG = FileType("image/png", ".png")
JPEG = FileType("image/jpeg", ".jpg")
TIFF = FileType("image/tiff", ".tiff")

_SIGNATURES: tuple[tuple[bytes, FileType], ...] = (
    (b"%PDF-", PDF),
    (b"\x89PNG\r\n\x1a\n", PNG),
    (b"\xff\xd8\xff", JPEG),
    (b"II*\x00", TIFF),
    (b"MM\x00*", TIFF),
)


def detect_file_type(header: bytes) -> FileType | None:
    for signature, file_type in _SIGNATURES:
        if header.startswith(signature):
            return file_type
    return None


def validate_upload(upload: UploadedFile | None) -> FileType:
    if upload is None:
        raise InvalidDocument("No file uploaded. Send the document as multipart/form-data in the 'file' field.")
    if upload.size == 0:
        raise InvalidDocument("Uploaded file is empty.")
    if upload.size > settings.MAX_UPLOAD_SIZE:
        raise FileTooLarge(f"File exceeds the maximum allowed size of {settings.MAX_UPLOAD_SIZE} bytes.")

    upload.seek(0)
    file_type = detect_file_type(upload.read(16))
    upload.seek(0)
    if file_type is None:
        raise UnsupportedFileType()

    try:
        if file_type.is_pdf:
            _check_pdf(upload)
        else:
            _check_image(upload)
    finally:
        upload.seek(0)
    return file_type


def _check_pdf(upload: UploadedFile) -> None:
    try:
        doc = pymupdf.open(stream=upload.read(), filetype="pdf")
    except Exception as exc:
        raise InvalidDocument("File is not a valid PDF.") from exc
    with doc:
        if doc.needs_pass:
            raise InvalidDocument("Password-protected PDFs are not supported.")
        if doc.page_count == 0:
            raise InvalidDocument("PDF has no pages.")


def _check_image(upload: UploadedFile) -> None:
    try:
        with Image.open(upload) as image:
            image.verify()
    except Exception as exc:
        raise InvalidDocument("File is not a valid image.") from exc
