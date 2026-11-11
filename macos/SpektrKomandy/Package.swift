// swift-tools-version: 6.4
import PackageDescription

let package = Package(
    name: "SpektrKomandy",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "SpektrKomandy", targets: ["SpektrKomandy"]),
    ],
    dependencies: [
        .package(path: "../../contracts/protobuf/swift-stream"),
        .package(path: "../../contracts/generated/swift"),
    ],
    targets: [
        .target(name: "SpektrKomandy", dependencies: [
            .product(name: "ORIMeasurementStream", package: "swift-stream"),
            .product(name: "ORIContracts", package: "swift"),
        ], swiftSettings: [.swiftLanguageMode(.v6)]),
        .testTarget(
            name: "SpektrKomandyTests",
            dependencies: ["SpektrKomandy",
                .product(name: "ORIMeasurementStream", package: "swift-stream"),
                .product(name: "ORIContracts", package: "swift"),
            ],
            resources: [.copy("Fixtures")],
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
    ]
)
