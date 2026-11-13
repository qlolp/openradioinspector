"""Literal compiler output formats and pin-confusion regression cases."""

NATIVE_APPLE_SWIFT_OUTPUT = (
    "swift-driver version: 1.168.6 Apple Swift version 6.4.2 "
    "(swiftlang-6.4.2.1.9 clang-2100.4.1.1)\n"
    "Target: arm64-apple-macosx27.0.0\n"
)

SUPPORTED_SWIFT_OUTPUTS = (
    ("native-apple-driver", NATIVE_APPLE_SWIFT_OUTPUT),
    (
        "linux-release",
        "Swift version 6.4.2 (swift-6.4.2-RELEASE)\n"
        "Target: x86_64-unknown-linux-gnu\n",
    ),
    (
        "plain-apple",
        "Apple Swift version 6.4.2 (swiftlang-6.4.2.1.9 clang-2100.4.1.1)\n",
    ),
    ("plain-swift", "Swift version 6.4.2\n"),
    (
        "independent-driver-version",
        "swift-driver version: 6.4.1 Apple Swift version 6.4.2\n",
    ),
)

UNSUPPORTED_SWIFT_OUTPUTS = (
    ("empty", ""),
    ("old-major", "Apple Swift version 5.10.1\n"),
    ("old-patch", "Swift version 6.4.1\n"),
    ("new-patch", "Apple Swift version 6.4.3\n"),
    ("extra-digit", "Swift version 6.4.20\n"),
    ("extra-component", "Swift version 6.4.2.1\n"),
    ("dev-suffix", "Swift version 6.4.2-dev\n"),
    ("build-suffix", "Apple Swift version 6.4.2+dev\n"),
    ("development-snapshot", "Swift version 6.4.2 development snapshot\n"),
    ("driver-only-pin", "swift-driver version: 6.4.2\n"),
    (
        "driver-pin-wrong-compiler",
        "swift-driver version: 6.4.2 Apple Swift version 6.4.1\n",
    ),
    (
        "metadata-pin-wrong-compiler",
        "Apple Swift version 6.4.1 (swiftlang-6.4.2.1.9 clang-2100.4.1.1)\n",
    ),
    ("sdk-metadata-only", "SDK metadata: swiftlang-6.4.2.1.9 clang-2100.4.1.1\n"),
    ("unrelated-prefix", "SDK metadata: Swift version 6.4.2\n"),
    ("unrelated-suffix", "Swift version 6.4.2 SDK metadata\n"),
    ("indented-declaration", " Swift version 6.4.2\n"),
    (
        "conflicting-declarations",
        "Swift version 6.4.2\nApple Swift version 6.4.1\n",
    ),
    (
        "conflicting-driver-declarations",
        NATIVE_APPLE_SWIFT_OUTPUT
        + "swift-driver version: 6.4.2 Apple Swift version 6.4.3\n",
    ),
    ("duplicate-declarations", "Swift version 6.4.2\nSwift version 6.4.2\n"),
    (
        "same-line-conflict",
        "Swift version 6.4.2 (metadata Swift version 6.4.1)\n",
    ),
    (
        "extra-metadata-declaration",
        NATIVE_APPLE_SWIFT_OUTPUT + "SDK metadata: Swift version 6.4.2\n",
    ),
)
