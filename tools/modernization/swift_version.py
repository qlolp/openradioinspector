"""Recognize the pinned compiler declaration, independently of Swift's driver."""

import re


_DECLARATION = re.compile(r"\bSwift[ \t]+version\b", re.IGNORECASE)
_PINNED_DECLARATION = re.compile(
    r"(?:swift-driver version: [0-9]+(?:\.[0-9]+)+ Apple |(?:Apple )?)"
    r"Swift version 6\.4\.2(?: \([^()\r\n]+\))?"
)


def is_pinned_swift_output(output):
    """Accept one complete stable 6.4.2 declaration in a supported banner format."""
    declarations = [line for line in output.splitlines() if _DECLARATION.search(line)]
    return (
        len(declarations) == 1
        and len(_DECLARATION.findall(declarations[0])) == 1
        and _PINNED_DECLARATION.fullmatch(declarations[0]) is not None
    )
