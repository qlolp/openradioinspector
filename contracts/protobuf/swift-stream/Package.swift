// swift-tools-version: 6.4
import PackageDescription
let package = Package(
    name: "ORIMeasurementStream", platforms: [.macOS(.v14)],
    products: [.library(name: "ORIMeasurementStream", targets: ["ORIMeasurementStream"])],
    dependencies: [.package(path: "../../generated/swift")],
    targets: [
        .target(name: "ORIMeasurementStream", dependencies: [.product(name: "ORIContracts", package: "swift")]),
        .testTarget(name: "ORIMeasurementStreamTests", dependencies: ["ORIMeasurementStream", .product(name: "ORIContracts", package: "swift")])
    ]
)
