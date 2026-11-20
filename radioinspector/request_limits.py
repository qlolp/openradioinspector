"""Shared request size and pagination limits."""

MAX_CONTROL_REQUEST_BYTES = 65_536
MAX_MEASUREMENT_REQUEST_BYTES = 16 * 1024 * 1024
REQUEST_BODY_TIMEOUT_SECONDS = 10.0

# Границы SQL-страницы списка кандидатов.
MAX_PAGE_LIMIT = 5_000
MAX_PAGE_OFFSET = (1 << 63) - 1


def validate_candidate_window(limit: int | None, offset: int) -> None:
    if limit is not None and (type(limit) is not int or not 1 <= limit <= MAX_PAGE_LIMIT):
        raise ValueError(f"limit должен быть целым числом от 1 до {MAX_PAGE_LIMIT}")
    if type(offset) is not int or not 0 <= offset <= MAX_PAGE_OFFSET:
        raise ValueError(f"offset должен быть целым числом от 0 до {MAX_PAGE_OFFSET}")
