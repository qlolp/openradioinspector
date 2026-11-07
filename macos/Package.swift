// swift-tools-version: 5.9
import PackageDescription

// macOS-only regression tests for the app's session and transport code.
let package = Package(
    name: "DvoynoyObzorChecks",
    platforms: [.macOS(.v14)],
    dependencies: [.package(path: "SpektrKomandy")],
    targets: [
        .target(name: "DvoynoyObzorSession", dependencies: ["SpektrKomandy"],
                path: "DvoynoyObzor", exclude: ["DvoynoyObzorApp.swift"]),
        .testTarget(name: "DvoynoyObzorSessionTests", dependencies: ["DvoynoyObzorSession"],
                    path: "Tests")
    ]
)
