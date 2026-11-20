from fastapi import HTTPException, status


class InvalidDocument(HTTPException):
    def __init__(self, detail: str = "Invalid document.") -> None:
        super().__init__(status.HTTP_400_BAD_REQUEST, detail)


class UnsupportedFileType(HTTPException):
    def __init__(self, detail: str = "Unsupported file type. Allowed types: PDF, PNG, JPEG, TIFF.") -> None:
        super().__init__(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail)


class FileTooLarge(HTTPException):
    def __init__(self, detail: str = "File is too large.") -> None:
        super().__init__(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail)


class NotFound(HTTPException):
    def __init__(self, detail: str = "Not found.") -> None:
        super().__init__(status.HTTP_404_NOT_FOUND, detail)
