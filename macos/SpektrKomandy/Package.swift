// swift-tools-version: 6.4
import PackageDescription

let package = Package(
    name: "SpektrKomandy",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "SpektrKomandy", targets: ["SpektrKomandy"]),
    ],
    targets: [
        .target(name: "SpektrKomandy", swiftSettings: [.swiftLanguageMode(.v6)]),
        .testTarget(
            name: "SpektrKomandyTests",
            dependencies: ["SpektrKomandy"],
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
    ]
)
