from rest_framework import status
from rest_framework.exceptions import APIException


class InvalidDocument(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Invalid document."
    default_code = "invalid_document"


class UnsupportedFileType(APIException):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    default_detail = "Unsupported file type. Allowed types: PDF, PNG, JPEG, TIFF."
    default_code = "unsupported_file_type"


class FileTooLarge(APIException):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_detail = "File is too large."
    default_code = "file_too_large"
