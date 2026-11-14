import SwiftUI
import SpektrKomandy

/// SwiftUI's selection Hashable value must use the contract's byte identity.
/// Writes preserve the original String used by existing models and API calls.
@MainActor
extension Binding where Value == String {
    var exactID: Binding<PlatformExactID> {
        Binding<PlatformExactID>(get: { PlatformExactID(wrappedValue) }, set: { wrappedValue = $0.rawValue })
    }
}
