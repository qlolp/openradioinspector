import Foundation
import SpektrKomandy
import SwiftUI

struct PortFields<Speed: View>: View {
    @Binding var path: String
    var connected: Bool
    var ports: [String]
    var onRefresh: () -> Void
    @ViewBuilder var speed: () -> Speed

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField("/dev/cu…", text: $path)
                .textFieldStyle(.roundedBorder)
                .disabled(connected)
            HStack {
                Menu("Найденные порты") {
                    if ports.isEmpty {
                        Button("Порты cu.* не найдены") {}
                            .disabled(true)
                    } else {
                        ForEach(ports, id: \.self) { port in
                            Button(port) { path = port }
                        }
                    }
                }
                .disabled(connected)
                Button("Обновить", action: onRefresh)
                    .disabled(connected)
                Spacer()
            }
            speed()
        }
    }
}

struct ErrorLine: View {
    var text: String?

    var body: some View {
        if let text, !text.isEmpty {
            Text(text)
                .font(.callout)
                .foregroundStyle(.red)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

struct ReplyLog: View {
    var text: String

    var body: some View {
        Text(text.isEmpty ? "—" : text)
            .font(.system(.caption, design: .monospaced))
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(8)
            .background(Color.primary.opacity(0.04))
            .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

func parseUserNumber(_ text: String) -> Double? {
    let normalized = text
        .trimmingCharacters(in: .whitespacesAndNewlines)
        .replacingOccurrences(of: ",", with: ".")
    if normalized.isEmpty { return nil }
    return Double(normalized)
}

func describeSession(_ error: Error) -> String {
    if let problem = error as? SessionProblem {
        return problem.errorDescription ?? "Ошибка сеанса."
    }
    let text = error.localizedDescription
    if text.isEmpty {
        return "Ошибка сеанса."
    }
    return text
}

func tracesFolder() throws -> URL {
    guard let base = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first else {
        throw SessionProblem.cannotWriteTrace("нет каталога документов")
    }
    let folder = base.appendingPathComponent("Двойной обзор", isDirectory: true)
    do {
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    } catch {
        throw SessionProblem.cannotWriteTrace(error.localizedDescription)
    }
    return folder
}

func clipReply(_ text: String, limit: Int) -> String {
    let flat = text.replacingOccurrences(of: "\r", with: " ").replacingOccurrences(of: "\n", with: " ")
    if flat.count <= limit { return flat }
    return String(flat.prefix(limit)) + "…"
}
