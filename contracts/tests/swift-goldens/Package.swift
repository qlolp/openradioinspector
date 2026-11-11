// swift-tools-version: 6.4
import PackageDescription
let package = Package(name: "ORIContractTests", dependencies: [.package(path: "../../generated/swift")], targets: [.testTarget(name: "ContractTests", dependencies: [.product(name: "ORIContracts", package: "swift")])])
