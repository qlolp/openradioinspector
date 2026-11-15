// swift-tools-version: 6.4
import PackageDescription

// Local app implementation and monitoring controls shared by the launcher and tests.
let package = Package(
    name: "DvoynoyObzorKit",
    platforms: [.macOS(.v14)],
    products: [.library(name: "DvoynoyObzorSession", targets: ["DvoynoyObzorSession"])],
    dependencies: [.package(path: "SpektrKomandy")],
    targets: [
        .target(name: "DvoynoyObzorSession", dependencies: ["SpektrKomandy"],
                path: "DvoynoyObzor", exclude: ["DvoynoyObzorApp.swift"],
                swiftSettings: [.swiftLanguageMode(.v6)]),
        .testTarget(name: "DvoynoyObzorSessionTests", dependencies: ["DvoynoyObzorSession"],
                    path: "Tests", swiftSettings: [.swiftLanguageMode(.v5)])
    ]
)
