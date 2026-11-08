"""Pure port-name identity checks, without importing or opening a transport."""

import re


def serial_port_identity(port: str) -> str:
    """Normalize native COM aliases only; preserve other paths exactly."""
    name = port[4:] if port.startswith("\\\\.\\") else port
    if re.fullmatch(r"COM[1-9][0-9]*", name, re.IGNORECASE | re.ASCII):
        return name.upper()
    return port
