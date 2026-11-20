import SpektrKomandy

/// Engineering-only selection. Production clients continue capabilities negotiation.
enum NativeAcceptanceTransport {
    static func resolve(_ requested:String?,offered:PlatformMeasurementTransport) throws -> PlatformMeasurementTransport {
        switch requested ?? "auto" {
        case "auto":return offered
        case "sse":return .serverSentEvents
        case "binary-v1":
            guard offered == .binaryV1 else {throw PlatformError.invalidResponse("Выбран binary-v1, но узел не подтвердил его поддержку. Автоматического перехода на SSE нет.")}
            return .binaryV1
        default:throw PlatformError.invalidCommand("Неизвестный транспорт инженерного профиля")
        }
    }
}
