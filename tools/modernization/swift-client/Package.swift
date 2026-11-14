// swift-tools-version: 6.4
import PackageDescription
let package = Package(name: "ORIClientInterop", platforms: [.macOS(.v14)],
    dependencies: [.package(path: "../../../macos/SpektrKomandy")],
    targets: [.executableTarget(name: "ORIClientInterop", dependencies: [
        .product(name: "SpektrKomandy", package: "SpektrKomandy")])])
