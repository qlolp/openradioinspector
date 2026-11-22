import SwiftUI

struct NativeConsoleActions {
    let observe: () -> Void
    let archive: () -> Void
    let settings: () -> Void
    let prepare: (() -> Void)?
}

private struct NativeConsoleActionsKey: FocusedValueKey {
    typealias Value = NativeConsoleActions
}

extension FocusedValues {
    var nativeConsoleActions: NativeConsoleActions? {
        get { self[NativeConsoleActionsKey.self] }
        set { self[NativeConsoleActionsKey.self] = newValue }
    }
}

struct NativeConsoleCommands: Commands {
    @FocusedValue(\.nativeConsoleActions) private var actions
    var body: some Commands {
        CommandGroup(after: .newItem) {
            Button("Открыть полевой выезд…") { NativeFieldOpenDelegate.chooseDocument() }
                .keyboardShortcut("o")
        }
        CommandGroup(replacing: .appSettings) {
            Button("Настройки наблюдения…") { actions?.settings() }
                .keyboardShortcut(",").disabled(actions == nil)
        }
        CommandMenu("Наблюдение") {
            Button("Пульт наблюдения") { actions?.observe() }.keyboardShortcut("1").disabled(actions == nil)
            Button("Архив") { actions?.archive() }.keyboardShortcut("2").disabled(actions == nil)
            Divider()
            Button("Подготовить наблюдение…") { actions?.prepare?() }
                .keyboardShortcut("n", modifiers: [.command, .shift]).disabled(actions?.prepare == nil)
        }
    }
}
