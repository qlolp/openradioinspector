import SwiftUI

enum NativeDeviceStatus {
    static func title(_ state: String) -> String {
        switch state {
        case "running": return "Приём идёт"
        case "ready": return "Готов"
        case "disconnected", "stopped": return "Остановлен"
        case "error": return "Ошибка"
        case "degraded": return "Приём нестабилен"
        case "busy": return "Занят"
        case "draft": return "Подготовка"
        default: return state
        }
    }
}

struct NativeMeasurementStatus: View {
    @ObservedObject var model: RemoteDualModel

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            VStack(alignment: .leading, spacing: 4) {
                if !model.displayCurrent.isEmpty {
                    if model.measurementsStale || !model.coreReachable {
                        Label("Последние полученные данные · актуальность не подтверждена", systemImage: "clock.badge.exclamationmark")
                            .foregroundStyle(.orange)
                    }
                    if let date = model.lastMeasurementAt {
                        Text("Последнее измерение: \(max(0, Int(context.date.timeIntervalSince(date)))) с назад")
                    }
                }
                if model.spectrumPresentation.paused {
                    Text("Показ зафиксирован. Пауза графика не управляет приёмом.")
                        .foregroundStyle(.orange)
                }
            }.font(.caption)
        }
    }
}
