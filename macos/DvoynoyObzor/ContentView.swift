import SwiftUI

struct ContentView: View {
    @StateObject private var receiver = ArModel()
    @StateObject private var analyzer = UltraModel()

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Двойной обзор")
                    .font(.title.weight(.semibold))
                Text("Два прибора в одном окне. Цифру разбирает приёмник. Слева отсчёт S-метра, справа дБм.")
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            Divider()
            HSplitView {
                ArPane(model: receiver)
                    .frame(minWidth: 460)
                UltraPane(model: analyzer) { hertz in
                    let units = hertz >= 0 ? (hertz + 5) / 10 : 0
                    receiver.stopScanAndSetFrequency(units: units)
                }
                .frame(minWidth: 460)
            }
        }
        .frame(minWidth: 980, minHeight: 700)
    }
}
