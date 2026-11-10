"""HTTP body budgets shared by the core and archive import clients."""

MAX_CONTROL_REQUEST_BYTES = 65_536
MAX_MEASUREMENT_REQUEST_BYTES = 16 * 1024 * 1024
REQUEST_BODY_TIMEOUT_SECONDS = 10.0
