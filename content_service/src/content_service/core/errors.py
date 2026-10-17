from fastapi import HTTPException, status


def not_found(code: str = "PostNotFound") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=code)


def bad_request(code: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=code)


def forbidden(code: str = "Forbidden") -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=code)


def conflict(code: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=code)
