// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "SpektrKomandy",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "SpektrKomandy", targets: ["SpektrKomandy"]),
    ],
    targets: [
        .target(name: "SpektrKomandy"),
        .testTarget(
            name: "SpektrKomandyTests",
            dependencies: ["SpektrKomandy"]
        ),
    ]
)
