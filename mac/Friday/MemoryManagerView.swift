import SwiftUI

struct MemoryRecord: Identifiable, Equatable {
    let id: String
    let type: String
    let title: String
    let detail: String
    let source: String
    let confidence: Double
    let updatedAt: String

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String else { return nil }
        self.id = id
        type = value["type"] as? String ?? "memory"
        title = value["title"] as? String ?? id
        detail = value["detail"] as? String ?? ""
        source = value["source"] as? String ?? "unknown"
        confidence = value["confidence"] as? Double ?? 0
        updatedAt = value["updatedAt"] as? String
            ?? value["createdAt"] as? String
            ?? ""
    }
}

@MainActor
final class MemoryManagerModel: ObservableObject {
    @Published var memories: [MemoryRecord] = []
    @Published var filter = "all"
    @Published var search = ""
    @Published var alias = ""
    @Published var target = ""
    @Published var kind = "entity"
    @Published var isLoading = false
    @Published var error: String?

    func reload() async {
        guard let port = BootCoordinator.shared.servicePort else {
            error = "Friday's local service is not ready."
            return
        }
        isLoading = true
        defer { isLoading = false }
        do {
            let values = try await LocalServiceClient(port: port)
                .contextMemories(kind: filter, query: search)
            memories = values.compactMap(MemoryRecord.init)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    func saveReference() async {
        let cleanedAlias = alias.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanedTarget = target.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanedAlias.isEmpty, !cleanedTarget.isEmpty,
              let port = BootCoordinator.shared.servicePort else { return }
        do {
            try await LocalServiceClient(port: port).rememberReference(
                alias: cleanedAlias,
                target: cleanedTarget,
                kind: kind
            )
            alias = ""
            target = ""
            await reload()
        } catch {
            self.error = error.localizedDescription
        }
    }

    func forget(_ memory: MemoryRecord) async {
        guard let port = BootCoordinator.shared.servicePort else { return }
        do {
            try await LocalServiceClient(port: port)
                .forgetContextMemory(id: memory.id)
            memories.removeAll { $0.id == memory.id }
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct MemoryManagerView: View {
    @StateObject private var model = MemoryManagerModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            correctionEditor
            Divider()
            filters
            memoryList
        }
        .padding(20)
        .frame(minWidth: 680, minHeight: 480)
        .task { await model.reload() }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Friday's Memory")
                .font(.title2.weight(.semibold))
            Text("Everything here is stored locally. You can inspect, correct, or delete it.")
                .foregroundStyle(.secondary)
        }
    }

    private var correctionEditor: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Teach Friday a reference")
                .font(.headline)
            HStack(spacing: 8) {
                TextField("When I say...", text: $model.alias)
                TextField("I mean...", text: $model.target)
                Picker("Kind", selection: $model.kind) {
                    ForEach(
                        ["entity", "person", "project", "file", "event", "app"],
                        id: \.self
                    ) { Text($0.capitalized).tag($0) }
                }
                .frame(width: 110)
                Button("Remember") {
                    Task { await model.saveReference() }
                }
                .disabled(
                    model.alias.trimmingCharacters(in: .whitespaces).isEmpty
                        || model.target.trimmingCharacters(in: .whitespaces).isEmpty
                )
            }
        }
    }

    private var filters: some View {
        HStack {
            Picker("Type", selection: $model.filter) {
                Text("All").tag("all")
                Text("References").tag("reference")
                Text("Preferences").tag("preference")
                Text("Entities").tag("entity")
                Text("Relationships").tag("relationship")
                Text("Timeline").tag("event")
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 470)
            .onChange(of: model.filter) { _, _ in
                Task { await model.reload() }
            }

            TextField("Search", text: $model.search)
                .textFieldStyle(.roundedBorder)
                .onSubmit { Task { await model.reload() } }

            Button {
                Task { await model.reload() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .help("Refresh memories")
        }
    }

    @ViewBuilder
    private var memoryList: some View {
        if let error = model.error {
            ContentUnavailableView(
                "Could Not Load Memory",
                systemImage: "exclamationmark.triangle",
                description: Text(error)
            )
        } else if model.memories.isEmpty, !model.isLoading {
            ContentUnavailableView(
                "No Memories",
                systemImage: "brain",
                description: Text("Friday has not stored anything in this category yet.")
            )
        } else {
            List(model.memories) { memory in
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: icon(for: memory.type))
                        .foregroundStyle(.secondary)
                        .frame(width: 18)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(memory.title)
                            .font(.body.weight(.medium))
                        if !memory.detail.isEmpty {
                            Text(memory.detail)
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                        Text("\(memory.source) · \(Int(memory.confidence * 100))% confidence")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    }
                    Spacer()
                    Button(role: .destructive) {
                        Task { await model.forget(memory) }
                    } label: {
                        Image(systemName: "trash")
                    }
                    .buttonStyle(.borderless)
                    .help("Forget this memory")
                }
                .padding(.vertical, 4)
            }
            .overlay {
                if model.isLoading {
                    ProgressView()
                }
            }
        }
    }

    private func icon(for type: String) -> String {
        switch type {
        case "reference": "quote.bubble"
        case "preference": "slider.horizontal.3"
        case "relationship": "point.3.connected.trianglepath.dotted"
        case "entity": "circle.hexagongrid"
        case "event": "clock.arrow.circlepath"
        default: "brain"
        }
    }
}
