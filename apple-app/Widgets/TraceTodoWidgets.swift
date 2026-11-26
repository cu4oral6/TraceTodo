import SwiftUI
import WidgetKit

// MARK: - Lightweight Codable Models & Networking (Zero AppIntents Metadata Dependency)

struct WidgetTaskRow: Identifiable, Hashable {
    let id: String
    let title: String
    let quadrant: String
    let priorityScore: Double
    let dueLabel: String
    let version: Int
    let isOverdue: Bool
    let subtaskProgress: String

    var shortCode: String {
        String(quadrant.prefix(2))
    }

    var tintColor: Color {
        switch quadrant {
        case "Q1_do_first": return Color(red: 0.90, green: 0.22, blue: 0.27)
        case "Q2_schedule": return Color(red: 0.20, green: 0.48, blue: 0.96)
        case "Q3_delegate": return Color(red: 0.92, green: 0.56, blue: 0.08)
        default: return Color(red: 0.42, green: 0.48, blue: 0.58)
        }
    }
}

private struct RawMatrixDTO: Decodable {
    let summaryCounts: RawSummaryDTO
    let sortedTasks: [RawTaskDTO]

    enum CodingKeys: String, CodingKey {
        case summaryCounts = "summary_counts"
        case sortedTasks = "sorted_tasks"
    }
}

private struct RawSummaryDTO: Decodable {
    let totalTodo: Int
    let totalDone: Int
    let overdue: Int
    let q1: Int
    let q2: Int
    let q3: Int
    let q4: Int

    enum CodingKeys: String, CodingKey {
        case totalTodo = "total_todo"
        case totalDone = "total_done"
        case overdue
        case q1 = "Q1_do_first"
        case q2 = "Q2_schedule"
        case q3 = "Q3_delegate"
        case q4 = "Q4_eliminate"
    }
}

private struct RawTaskDTO: Decodable {
    let memoryId: String
    let title: String
    let version: Int
    let payload: RawPayloadDTO

    enum CodingKeys: String, CodingKey {
        case memoryId = "memory_id"
        case title
        case version
        case payload
    }
}

private struct RawSubDTO: Decodable {
    let done: Bool
}

private struct RawPayloadDTO: Decodable {
    let taskStatus: String?
    let quadrant: String?
    let priorityScore: Double?
    let dueAt: String?
    let isOverdue: Bool?
    let subtasks: [RawSubDTO]?

    enum CodingKeys: String, CodingKey {
        case taskStatus = "task_status"
        case quadrant
        case priorityScore = "priority_score"
        case dueAt = "due_at"
        case isOverdue = "is_overdue"
        case subtasks
    }
}

enum WidgetAPI {
    static func resolveSharedConfig() -> (base: String, space: String, token: String) {
        let shared = SharedWidgetBridge.loadConfig()
        let base = shared.serverURL
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let effBase = base.isEmpty ? SharedWidgetBridge.cloudServerURL : base
        let space = shared.spaceId.trimmingCharacters(in: .whitespacesAndNewlines)
        let token = shared.authToken.trimmingCharacters(in: .whitespacesAndNewlines)
        return (effBase, space.isEmpty ? "default" : space, token)
    }

    static func decodeEntry(
        from data: Data,
        base: String,
        space: String,
        token: String
    ) throws -> TraceTodoWidgetEntry {
        let dto = try JSONDecoder().decode(RawMatrixDTO.self, from: data)
        let filtered = dto.sortedTasks.filter { t in
            t.payload.taskStatus != "done" && t.payload.taskStatus != "canceled"
        }.map { t -> WidgetTaskRow in
            let subs = t.payload.subtasks ?? []
            let prog = subs.isEmpty ? "" : "\(subs.filter(\.done).count)/\(subs.count)"
            let due = t.payload.dueAt.map { String($0.prefix(10)) } ?? ""
            return WidgetTaskRow(
                id: t.memoryId,
                title: t.title,
                quadrant: t.payload.quadrant ?? "Q4_eliminate",
                priorityScore: t.payload.priorityScore ?? 0,
                dueLabel: due,
                version: t.version,
                isOverdue: t.payload.isOverdue ?? false,
                subtaskProgress: prog
            )
        }
        return TraceTodoWidgetEntry(
            date: Date(),
            serverURL: base,
            spaceId: space,
            authToken: token,
            q1Count: dto.summaryCounts.q1,
            q2Count: dto.summaryCounts.q2,
            q34Count: dto.summaryCounts.q3 + dto.summaryCounts.q4,
            totalTodo: dto.summaryCounts.totalTodo,
            totalDone: dto.summaryCounts.totalDone,
            overdueCount: dto.summaryCounts.overdue,
            tasks: filtered,
            needsLogin: false,
            isOffline: false
        )
    }

    private static func cachedFallbackEntry(
        base: String,
        space: String,
        token: String,
        needsLogin: Bool = false
    ) -> TraceTodoWidgetEntry {
        if let cachedData = SharedWidgetBridge.loadMatrixSnapshotData(),
           let entry = try? decodeEntry(from: cachedData, base: base, space: space, token: token) {
            return entry
        }
        return .placeholder(base: base, space: space, needsLogin: needsLogin, isOffline: !needsLogin)
    }

    static func fetchEntry() async -> TraceTodoWidgetEntry {
        let resolved = resolveSharedConfig()
        let base = resolved.base
        let space = resolved.space
        let cleanTok = resolved.token

        // First check if we already have an instant snapshot written by the main app AND no network token yet
        guard var comps = URLComponents(string: "\(base)/api/tasks/matrix") else {
            return cachedFallbackEntry(base: base, space: space, token: cleanTok)
        }
        var qItems: [URLQueryItem] = [
            URLQueryItem(name: "sort_by", value: "priority"),
            URLQueryItem(name: "include_done", value: "false")
        ]
        // Crucial: when authenticated with Bearer token, omit `space_id=default` so the backend automatically resolves `current_user.space_id` (avoids HTTP 403 for normal users and empty default space for admins)
        if cleanTok.isEmpty || (!space.isEmpty && space != "default") {
            qItems.append(URLQueryItem(name: "space_id", value: space.isEmpty ? "default" : space))
        }
        comps.queryItems = qItems

        guard let url = comps.url else {
            return cachedFallbackEntry(base: base, space: space, token: cleanTok)
        }
        var req = URLRequest(url: url)
        req.timeoutInterval = 8
        if !cleanTok.isEmpty {
            req.setValue("Bearer \(cleanTok)", forHTTPHeaderField: "Authorization")
        }

        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            if let http = resp as? HTTPURLResponse {
                if http.statusCode == 401 || http.statusCode == 403 {
                    // Retry once without explicit space_id if 403 occurred on a stale spaceId
                    if http.statusCode == 403, !cleanTok.isEmpty {
                        var retryComps = URLComponents(string: "\(base)/api/tasks/matrix")
                        retryComps?.queryItems = [
                            URLQueryItem(name: "sort_by", value: "priority"),
                            URLQueryItem(name: "include_done", value: "false")
                        ]
                        if let retryURL = retryComps?.url {
                            var retryReq = URLRequest(url: retryURL)
                            retryReq.timeoutInterval = 8
                            retryReq.setValue("Bearer \(cleanTok)", forHTTPHeaderField: "Authorization")
                            if let (rData, rResp) = try? await URLSession.shared.data(for: retryReq),
                               let rHttp = rResp as? HTTPURLResponse,
                               (200...299).contains(rHttp.statusCode),
                               let decoded = try? decodeEntry(from: rData, base: base, space: "我的空间", token: cleanTok) {
                                SharedWidgetBridge.saveMatrixSnapshotData(rData)
                                return decoded
                            }
                        }
                    }
                    return cachedFallbackEntry(base: base, space: space, token: cleanTok, needsLogin: true)
                }
                guard (200...299).contains(http.statusCode) else {
                    return cachedFallbackEntry(base: base, space: space, token: cleanTok)
                }
            }
            SharedWidgetBridge.saveMatrixSnapshotData(data)
            return try decodeEntry(from: data, base: base, space: space, token: cleanTok)
        } catch {
            return cachedFallbackEntry(base: base, space: space, token: cleanTok)
        }
    }
}

// MARK: - Timeline Entry & Static Provider

struct TraceTodoWidgetEntry: TimelineEntry {
    let date: Date
    let serverURL: String
    let spaceId: String
    let authToken: String
    let q1Count: Int
    let q2Count: Int
    let q34Count: Int
    let totalTodo: Int
    let totalDone: Int
    let overdueCount: Int
    let tasks: [WidgetTaskRow]
    let needsLogin: Bool
    let isOffline: Bool

    static func placeholder(
        base: String = SharedWidgetBridge.cloudServerURL,
        space: String = "default",
        needsLogin: Bool = false,
        isOffline: Bool = false
    ) -> TraceTodoWidgetEntry {
        let isEmptyState = needsLogin || isOffline
        return TraceTodoWidgetEntry(
            date: Date(),
            serverURL: base,
            spaceId: space,
            authToken: "",
            q1Count: isEmptyState ? 0 : 2,
            q2Count: isEmptyState ? 0 : 1,
            q34Count: 0,
            totalTodo: isEmptyState ? 0 : 3,
            totalDone: isEmptyState ? 0 : 1,
            overdueCount: 0,
            tasks: isEmptyState ? [] : [
                WidgetTaskRow(id: "demo1", title: "准备学术会议论文汇报", quadrant: "Q1_do_first", priorityScore: 123, dueLabel: "本周六", version: 1, isOverdue: false, subtaskProgress: ""),
                WidgetTaskRow(id: "demo2", title: "整理阶段复盘笔记", quadrant: "Q1_do_first", priorityScore: 121, dueLabel: "今日", version: 1, isOverdue: false, subtaskProgress: "1/3"),
                WidgetTaskRow(id: "demo3", title: "阅读深度专业书籍", quadrant: "Q2_schedule", priorityScore: 88, dueLabel: "", version: 1, isOverdue: false, subtaskProgress: "")
            ],
            needsLogin: needsLogin,
            isOffline: isOffline
        )
    }
}

struct TraceTodoStaticProvider: TimelineProvider {
    func placeholder(in context: Context) -> TraceTodoWidgetEntry {
        .placeholder()
    }

    func getSnapshot(in context: Context, completion: @escaping (TraceTodoWidgetEntry) -> Void) {
        if context.isPreview {
            if let cached = SharedWidgetBridge.loadMatrixSnapshotData(),
               let entry = try? WidgetAPI.decodeEntry(from: cached, base: SharedWidgetBridge.cloudServerURL, space: "default", token: "") {
                completion(entry)
                return
            }
            completion(.placeholder())
            return
        }
        Task {
            let entry = await WidgetAPI.fetchEntry()
            completion(entry)
        }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<TraceTodoWidgetEntry>) -> Void) {
        Task {
            let entry = await WidgetAPI.fetchEntry()
            let mins = entry.needsLogin ? 5 : 15
            let nextRefresh = Calendar.current.date(byAdding: .minute, value: mins, to: Date()) ?? Date().addingTimeInterval(Double(mins * 60))
            completion(Timeline(entries: [entry], policy: .after(nextRefresh)))
        }
    }
}

// MARK: - Widget Views

struct TraceTodoWidgetEntryView: View {
    @Environment(\.widgetFamily) private var family
    let entry: TraceTodoWidgetEntry

    var body: some View {
        switch family {
        case .systemSmall:
            smallView
        case .systemMedium:
            mediumView
        case .systemLarge:
            largeView
        #if os(iOS)
        case .accessoryRectangular:
            accessoryRectView
        case .accessoryCircular:
            accessoryCircularView
        case .accessoryInline:
            accessoryInlineView
        #endif
        default:
            mediumView
        }
    }

    @ViewBuilder
    private var emptyOrStatusPrompt: some View {
        if entry.needsLogin {
            Link(destination: URL(string: "tracetodo://login")!) {
                VStack(alignment: .leading, spacing: 4) {
                    Label("点击打开 App 登录同步", systemImage: "person.crop.circle.badge.plus")
                        .font(.caption.bold())
                        .foregroundStyle(Color(red: 0.23, green: 0.50, blue: 0.96))
                    Text("登录后四象限待办将自动实时显示在桌面")
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
        } else if entry.isOffline {
            Link(destination: URL(string: "tracetodo://refresh")!) {
                VStack(alignment: .leading, spacing: 4) {
                    Label("轻点打开 有迹 同步数据", systemImage: "arrow.triangle.2.circlepath")
                        .font(.caption.bold())
                        .foregroundStyle(.orange)
                    Text("首次添加后打开主应用即可缓存最新待办")
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
        } else {
            Label("今日待办已全部清空 🎉", systemImage: "checkmark.seal.fill")
                .font(.caption.bold())
                .foregroundStyle(.green)
        }
    }

    private var smallView: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                HStack(spacing: 4) {
                    Image(systemName: "flame.fill")
                        .font(.caption2.bold())
                        .foregroundStyle(.white)
                    Text("Q1 \(entry.q1Count) · 待办 \(entry.totalTodo)")
                        .font(.system(size: 10.5, weight: .bold))
                        .foregroundStyle(.white)
                }
                .padding(.horizontal, 7)
                .padding(.vertical, 3)
                .background(
                    LinearGradient(
                        colors: [Color(red: 0.24, green: 0.51, blue: 0.96), Color(red: 0.52, green: 0.35, blue: 0.96)],
                        startPoint: .leading,
                        endPoint: .trailing
                    )
                )
                .clipShape(Capsule())

                Spacer()

                Link(destination: URL(string: "tracetodo://refresh")!) {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.secondary)
                }
            }

            if entry.tasks.isEmpty {
                Spacer(minLength: 2)
                emptyOrStatusPrompt
                Spacer(minLength: 2)
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(entry.tasks.prefix(3)) { task in
                        HStack(spacing: 5) {
                            RoundedRectangle(cornerRadius: 2)
                                .fill(task.tintColor)
                                .frame(width: 3, height: 14)
                            Text(task.title)
                                .font(.system(size: 12, weight: .medium))
                                .lineLimit(1)
                        }
                    }
                }
                Spacer(minLength: 0)
            }

            HStack {
                Text("有迹")
                    .font(.system(size: 9.5, weight: .semibold))
                    .foregroundStyle(.tertiary)
                Spacer()
                Link(destination: URL(string: "tracetodo://new")!) {
                    Image(systemName: "plus.circle.fill")
                        .font(.system(size: 14))
                        .foregroundStyle(Color(red: 0.42, green: 0.34, blue: 0.96))
                }
            }
        }
        .widgetURL(URL(string: entry.needsLogin ? "tracetodo://login" : "tracetodo://overview"))
    }

    private var mediumView: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("TODO")
                        .font(.system(size: 10, weight: .bold))
                        .tracking(1)
                        .foregroundStyle(.white.opacity(0.85))
                    Spacer()
                    Link(destination: URL(string: "tracetodo://refresh")!) {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 11, weight: .bold))
                            .foregroundStyle(.white.opacity(0.9))
                    }
                }

                Text("\(entry.totalTodo)")
                    .font(.system(size: 30, weight: .bold, design: .rounded))
                    .foregroundStyle(.white)

                Spacer(minLength: 2)

                VStack(alignment: .leading, spacing: 3) {
                    Text("Q1 急事: \(entry.q1Count)")
                    Text("Q2 规划: \(entry.q2Count)")
                }
                .font(.system(size: 10.5, weight: .semibold))
                .foregroundStyle(.white.opacity(0.92))

                HStack(spacing: 5) {
                    Link(destination: URL(string: "tracetodo://new")!) {
                        Label("新建", systemImage: "plus")
                            .font(.system(size: 10, weight: .bold))
                            .padding(.horizontal, 7)
                            .padding(.vertical, 3)
                            .background(Color.white.opacity(0.22))
                            .foregroundStyle(.white)
                            .clipShape(Capsule())
                    }
                    Link(destination: URL(string: "tracetodo://focus")!) {
                        Image(systemName: "timer")
                            .font(.system(size: 10.5, weight: .bold))
                            .padding(4)
                            .background(Color.white.opacity(0.22))
                            .foregroundStyle(.white)
                            .clipShape(Circle())
                    }
                }
            }
            .padding(12)
            .frame(width: 112)
            .frame(maxHeight: .infinity)
            .background(
                LinearGradient(
                    colors: [Color(red: 0.23, green: 0.50, blue: 0.96), Color(red: 0.51, green: 0.34, blue: 0.97)],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )
            )
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))

            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("优先待办")
                        .font(.system(size: 11.5, weight: .bold))
                        .foregroundStyle(.secondary)
                    Spacer()
                    if entry.overdueCount > 0 {
                        Text("逾期 \(entry.overdueCount)")
                            .font(.system(size: 10, weight: .bold))
                            .foregroundStyle(.red)
                    }
                }

                if entry.tasks.isEmpty {
                    Spacer(minLength: 2)
                    emptyOrStatusPrompt
                    Spacer(minLength: 2)
                } else {
                    ForEach(entry.tasks.prefix(3)) { task in
                        interactiveTaskRow(task)
                    }
                    Spacer(minLength: 0)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .widgetURL(URL(string: entry.needsLogin ? "tracetodo://login" : "tracetodo://overview"))
    }

    private var largeView: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                VStack(alignment: .leading, spacing: 1) {
                    Text("有迹 · 四象限工作台")
                        .font(.system(size: 14, weight: .bold))
                    Text("已完成 \(entry.totalDone) · 待办 \(entry.totalTodo)")
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Link(destination: URL(string: "tracetodo://focus")!) {
                    Image(systemName: "timer.circle.fill")
                        .font(.title3)
                        .foregroundStyle(Color(red: 0.92, green: 0.44, blue: 0.14))
                }

                Link(destination: URL(string: "tracetodo://refresh")!) {
                    Image(systemName: "arrow.clockwise.circle.fill")
                        .font(.title3)
                        .foregroundStyle(Color(red: 0.24, green: 0.51, blue: 0.96))
                }

                Link(destination: URL(string: "tracetodo://new")!) {
                    Image(systemName: "plus.circle.fill")
                        .font(.title3)
                        .foregroundStyle(Color(red: 0.51, green: 0.34, blue: 0.97))
                }
            }

            HStack(spacing: 8) {
                statPill(title: "Q1 急事", value: entry.q1Count, color: Color(red: 0.93, green: 0.26, blue: 0.34))
                statPill(title: "Q2 规划", value: entry.q2Count, color: Color(red: 0.22, green: 0.49, blue: 0.96))
                statPill(title: "Q3/Q4", value: entry.q34Count, color: Color(red: 0.08, green: 0.72, blue: 0.65))
                statPill(title: "总待办", value: entry.totalTodo, color: Color(red: 0.49, green: 0.33, blue: 0.96))
            }

            Divider()

            if entry.tasks.isEmpty {
                Spacer()
                emptyOrStatusPrompt
                    .frame(maxWidth: .infinity, alignment: .leading)
                Spacer()
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(entry.tasks.prefix(6)) { task in
                        interactiveTaskRow(task, showDetails: true)
                    }
                }
                Spacer(minLength: 0)
            }
        }
        .widgetURL(URL(string: entry.needsLogin ? "tracetodo://login" : "tracetodo://list"))
    }

    #if os(iOS)
    private var accessoryRectView: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Label("有迹", systemImage: "checkmark.circle.fill")
                    .font(.caption2.bold())
                Spacer()
                Text("Q1:\(entry.q1Count) 总:\(entry.totalTodo)")
                    .font(.caption2)
            }
            if let top = entry.tasks.first {
                Text("[\(top.shortCode)] \(top.title)")
                    .font(.caption)
                    .lineLimit(1)
            } else {
                Text(entry.needsLogin ? "打开 App 登录同步" : (entry.isOffline ? "轻点同步待办" : "全部待办已清空 🎉"))
                    .font(.caption)
            }
        }
        .widgetURL(URL(string: entry.needsLogin ? "tracetodo://login" : "tracetodo://overview"))
    }

    private var accessoryCircularView: some View {
        ZStack {
            AccessoryWidgetBackground()
            VStack(spacing: 0) {
                Text("\(entry.totalTodo)")
                    .font(.system(size: 18, weight: .bold, design: .rounded))
                    .widgetAccentable()
                Text("待办")
                    .font(.system(size: 8.5, weight: .medium))
            }
        }
        .widgetURL(URL(string: "tracetodo://overview"))
    }

    private var accessoryInlineView: some View {
        Group {
            if let first = entry.tasks.first {
                Label("Q1:\(entry.q1Count) · \(first.title)", systemImage: "checkmark.circle")
            } else {
                Label("今日待办 \(entry.totalTodo) 项", systemImage: "checkmark.seal")
            }
        }
        .widgetURL(URL(string: "tracetodo://overview"))
    }
    #endif

    private func interactiveTaskRow(_ task: WidgetTaskRow, showDetails: Bool = false) -> some View {
        let completeURL = URL(string: "tracetodo://complete?id=\(task.id.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? task.id)&v=\(task.version)")!
        return HStack(spacing: 7) {
            Link(destination: completeURL) {
                Image(systemName: "circle")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(task.tintColor)
            }

            VStack(alignment: .leading, spacing: 1) {
                Text(task.title)
                    .font(.system(size: 12.5, weight: .semibold))
                    .lineLimit(1)
                    .foregroundStyle(.primary)

                if showDetails {
                    HStack(spacing: 6) {
                        Text(task.shortCode)
                            .font(.system(size: 9.5, weight: .bold))
                            .foregroundStyle(task.tintColor)
                        if !task.dueLabel.isEmpty {
                            Text(task.dueLabel)
                                .font(.system(size: 9.5))
                                .foregroundStyle(task.isOverdue ? .red : .secondary)
                        }
                        if !task.subtaskProgress.isEmpty {
                            Text("📋 \(task.subtaskProgress)")
                                .font(.system(size: 9.5))
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }

            Spacer(minLength: 4)

            if !showDetails {
                Text(task.shortCode)
                    .font(.system(size: 9.5, weight: .bold))
                    .padding(.horizontal, 5)
                    .padding(.vertical, 2)
                    .background(task.tintColor.opacity(0.14))
                    .foregroundStyle(task.tintColor)
                    .clipShape(Capsule())
            } else {
                Text("\(Int(task.priorityScore))分")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 3)
    }

    private func statPill(title: String, value: Int, color: Color) -> some View {
        VStack(spacing: 1) {
            Text("\(value)")
                .font(.system(size: 15, weight: .bold, design: .rounded))
                .foregroundStyle(.white)
            Text(title)
                .font(.system(size: 9.5, weight: .semibold))
                .foregroundStyle(.white.opacity(0.9))
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 6)
        .background(color)
        .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
    }
}

struct TraceTodoWidget: Widget {
    let kind: String = "TraceTodoWidget"

    #if os(iOS)
    private let families: [WidgetFamily] = [
        .systemSmall,
        .systemMedium,
        .systemLarge,
        .accessoryRectangular,
        .accessoryCircular,
        .accessoryInline
    ]
    #else
    private let families: [WidgetFamily] = [
        .systemSmall,
        .systemMedium,
        .systemLarge
    ]
    #endif

    var body: some WidgetConfiguration {
        StaticConfiguration(
            kind: kind,
            provider: TraceTodoStaticProvider()
        ) { entry in
            TraceTodoWidgetContainer(entry: entry)
        }
        .configurationDisplayName("有迹 四象限待办")
        .description("实时展示今日四象限待办，自动跟随主应用账号同步。")
        .supportedFamilies(families)
    }
}

private struct TraceTodoWidgetContainer: View {
    @Environment(\.widgetFamily) private var family
    let entry: TraceTodoWidgetEntry

    var body: some View {
        #if os(iOS)
        switch family {
        case .accessoryCircular, .accessoryRectangular, .accessoryInline:
            TraceTodoWidgetEntryView(entry: entry)
                .containerBackground(.clear, for: .widget)
        default:
            TraceTodoWidgetEntryView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
        }
        #else
        TraceTodoWidgetEntryView(entry: entry)
            .containerBackground(.fill.tertiary, for: .widget)
        #endif
    }
}

// MARK: - Countdown & Anniversary Widget (StaticConfiguration)

struct WidgetCountdownRow: Identifiable, Hashable {
    let id: String
    let title: String
    let icon: String
    let headerBanner: String
    let displayNumber: String
    let unitLabel: String
    let badgeLabel: String
    let displaySolarDate: String
    let lunarLabel: String
    let colorTheme: String

    var bannerGradient: LinearGradient {
        let colors: [Color]
        switch colorTheme {
        case "crimson":
            colors = [Color(red: 0.86, green: 0.15, blue: 0.15), Color(red: 0.96, green: 0.25, blue: 0.37)]
        case "lavender":
            colors = [Color(red: 0.49, green: 0.23, blue: 0.93), Color(red: 0.65, green: 0.55, blue: 0.98)]
        case "pink":
            colors = [Color(red: 0.86, green: 0.15, blue: 0.47), Color(red: 0.96, green: 0.45, blue: 0.71)]
        case "mint":
            colors = [Color(red: 0.02, green: 0.59, blue: 0.41), Color(red: 0.20, green: 0.83, blue: 0.60)]
        case "peach":
            colors = [Color(red: 0.92, green: 0.35, blue: 0.05), Color(red: 0.98, green: 0.57, blue: 0.24)]
        default:
            colors = [Color(red: 0.01, green: 0.52, blue: 0.78), Color(red: 0.22, green: 0.74, blue: 0.97)]
        }
        return LinearGradient(colors: colors, startPoint: .topLeading, endPoint: .bottomTrailing)
    }
}

private struct RawCountdownListDTO: Decodable {
    let todayLunar: String?
    let items: [RawCountdownItemDTO]

    enum CodingKeys: String, CodingKey {
        case todayLunar = "today_lunar"
        case items
    }
}

private struct RawCountdownItemDTO: Decodable {
    let countdownId: String
    let title: String
    let icon: String?
    let headerBanner: String?
    let displayNumber: String?
    let unitLabel: String?
    let badgeLabel: String?
    let displaySolarDate: String?
    let lunarLabel: String?
    let colorTheme: String?

    enum CodingKeys: String, CodingKey {
        case countdownId = "countdown_id"
        case title, icon
        case headerBanner = "header_banner"
        case displayNumber = "display_number"
        case unitLabel = "unit_label"
        case badgeLabel = "badge_label"
        case displaySolarDate = "display_solar_date"
        case lunarLabel = "lunar_label"
        case colorTheme = "color_theme"
    }
}

struct TraceCountdownEntry: TimelineEntry {
    let date: Date
    let todayLunar: String
    let items: [WidgetCountdownRow]

    static func placeholder() -> TraceCountdownEntry {
        TraceCountdownEntry(
            date: Date(),
            todayLunar: "丙午马年 · 农历冬月",
            items: [
                WidgetCountdownRow(
                    id: "cd1",
                    title: "春节团圆",
                    icon: "🧧",
                    headerBanner: "距离 春节团圆 还有",
                    displayNumber: "76",
                    unitLabel: "天",
                    badgeLabel: "还有",
                    displaySolarDate: "2027-02-06",
                    lunarLabel: "农历正月初一",
                    colorTheme: "crimson"
                ),
                WidgetCountdownRow(
                    id: "cd2",
                    title: "里程碑发布",
                    icon: "🚀",
                    headerBanner: "距离 里程碑发布 还有",
                    displayNumber: "1",
                    unitLabel: "天",
                    badgeLabel: "还有",
                    displaySolarDate: "明日",
                    lunarLabel: "置顶目标",
                    colorTheme: "lavender"
                )
            ]
        )
    }
}

struct TraceCountdownStaticProvider: TimelineProvider {
    func placeholder(in context: Context) -> TraceCountdownEntry {
        .placeholder()
    }

    func getSnapshot(in context: Context, completion: @escaping (TraceCountdownEntry) -> Void) {
        if context.isPreview {
            if let cached = SharedWidgetBridge.loadCountdownsSnapshotData(),
               let parsed = try? Self.decodeCountdownData(cached),
               !parsed.items.isEmpty {
                completion(parsed)
                return
            }
            completion(.placeholder())
            return
        }
        Task {
            let entry = await Self.fetchCountdownEntry()
            completion(entry)
        }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<TraceCountdownEntry>) -> Void) {
        Task {
            let entry = await Self.fetchCountdownEntry()
            let nextRefresh = Calendar.current.date(byAdding: .minute, value: 30, to: Date()) ?? Date().addingTimeInterval(1800)
            completion(Timeline(entries: [entry], policy: .after(nextRefresh)))
        }
    }

    static func decodeCountdownData(_ data: Data) throws -> TraceCountdownEntry {
        let dto = try JSONDecoder().decode(RawCountdownListDTO.self, from: data)
        let rows = dto.items.map { it in
            WidgetCountdownRow(
                id: it.countdownId,
                title: it.title,
                icon: it.icon ?? "⏳",
                headerBanner: it.headerBanner ?? it.title,
                displayNumber: it.displayNumber ?? "0",
                unitLabel: it.unitLabel ?? "",
                badgeLabel: it.badgeLabel ?? "还有",
                displaySolarDate: it.displaySolarDate ?? "",
                lunarLabel: it.lunarLabel ?? "",
                colorTheme: it.colorTheme ?? "crimson"
            )
        }
        return TraceCountdownEntry(date: Date(), todayLunar: dto.todayLunar ?? "", items: rows)
    }

    static func fetchCountdownEntry() async -> TraceCountdownEntry {
        let resolved = WidgetAPI.resolveSharedConfig()
        let base = resolved.base
        let space = resolved.space
        let token = resolved.token

        if var comps = URLComponents(string: "\(base)/api/countdowns/list") {
            if token.isEmpty || (!space.isEmpty && space != "default") {
                comps.queryItems = [URLQueryItem(name: "space_id", value: space.isEmpty ? "default" : space)]
            }
            if let url = comps.url {
                var req = URLRequest(url: url)
                req.timeoutInterval = 8
                if !token.isEmpty {
                    req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                }
                if let (data, resp) = try? await URLSession.shared.data(for: req),
                   let http = resp as? HTTPURLResponse,
                   (200...299).contains(http.statusCode),
                   let parsed = try? decodeCountdownData(data),
                   !parsed.items.isEmpty {
                    SharedWidgetBridge.saveCountdownsSnapshotData(data)
                    return parsed
                }
            }
        }

        if let cached = SharedWidgetBridge.loadCountdownsSnapshotData(),
           let parsed = try? decodeCountdownData(cached),
           !parsed.items.isEmpty {
            return parsed
        }
        return .placeholder()
    }
}

struct TraceCountdownWidgetEntryView: View {
    @Environment(\.widgetFamily) private var family
    let entry: TraceCountdownEntry

    var body: some View {
        switch family {
        case .systemSmall:
            smallCard
        case .systemMedium:
            mediumWall
        #if os(iOS)
        case .accessoryCircular:
            lockCircular
        case .accessoryRectangular:
            lockRectangular
        case .accessoryInline:
            lockInline
        #endif
        default:
            smallCard
        }
    }

    private var primaryItem: WidgetCountdownRow {
        entry.items.first ?? TraceCountdownEntry.placeholder().items[0]
    }

    private var smallCard: some View {
        let item = primaryItem
        return VStack(spacing: 0) {
            HStack {
                Text("\(item.icon) \(item.headerBanner)")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(.white)
                    .lineLimit(1)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .background(item.bannerGradient)
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))

            Spacer(minLength: 4)

            HStack(alignment: .firstTextBaseline, spacing: 3) {
                Text(item.displayNumber)
                    .font(.system(size: 38, weight: .heavy, design: .rounded))
                    .monospacedDigit()
                if !item.unitLabel.isEmpty {
                    Text(item.unitLabel)
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(.secondary)
                }
            }

            Spacer(minLength: 4)

            VStack(spacing: 1) {
                Text(item.displaySolarDate)
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
                if !item.lunarLabel.isEmpty {
                    Text(item.lunarLabel)
                        .font(.system(size: 9.5))
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .widgetURL(URL(string: "tracetodo://countdown"))
    }

    private var mediumWall: some View {
        let top = primaryItem
        let others = Array(entry.items.dropFirst().prefix(2))
        return HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text("\(top.icon) \(top.title)")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(.white)
                    .lineLimit(1)
                Text(top.badgeLabel)
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.85))
                Spacer(minLength: 2)
                HStack(alignment: .firstTextBaseline, spacing: 2) {
                    Text(top.displayNumber)
                        .font(.system(size: 32, weight: .heavy, design: .rounded))
                        .foregroundStyle(.white)
                    Text(top.unitLabel)
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(.white.opacity(0.9))
                }
                Text(top.lunarLabel.isEmpty ? top.displaySolarDate : top.lunarLabel)
                    .font(.system(size: 9.5, weight: .medium))
                    .foregroundStyle(.white.opacity(0.88))
                    .lineLimit(1)
            }
            .padding(11)
            .frame(width: 126)
            .frame(maxHeight: .infinity, alignment: .leading)
            .background(top.bannerGradient)
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))

            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("⏳ 倒数纪念日")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(.secondary)
                    Spacer()
                    Link(destination: URL(string: "tracetodo://countdown")!) {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.secondary)
                    }
                }

                if others.isEmpty {
                    Spacer()
                    Text(entry.todayLunar.isEmpty ? "在 App 日历页添加更多公农历纪念日" : entry.todayLunar)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Spacer()
                } else {
                    ForEach(others) { row in
                        HStack {
                            VStack(alignment: .leading, spacing: 1) {
                                Text("\(row.icon) \(row.title)")
                                    .font(.system(size: 12, weight: .semibold))
                                    .lineLimit(1)
                                Text(row.lunarLabel.isEmpty ? row.displaySolarDate : row.lunarLabel)
                                    .font(.system(size: 9.5))
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                            Spacer(minLength: 4)
                            VStack(alignment: .trailing, spacing: 0) {
                                Text("\(row.displayNumber)\(row.unitLabel)")
                                    .font(.system(size: 14, weight: .heavy, design: .rounded))
                                Text(row.badgeLabel)
                                    .font(.system(size: 9))
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .padding(.vertical, 2)
                    }
                    Spacer(minLength: 0)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .widgetURL(URL(string: "tracetodo://countdown"))
    }

    #if os(iOS)
    private var lockCircular: some View {
        let item = primaryItem
        return ZStack {
            AccessoryWidgetBackground()
            VStack(spacing: 0) {
                Text(item.displayNumber)
                    .font(.system(size: 17, weight: .heavy, design: .rounded))
                    .widgetAccentable()
                Text(item.unitLabel.isEmpty ? item.icon : "\(item.badgeLabel)\(item.unitLabel)")
                    .font(.system(size: 8, weight: .medium))
            }
        }
        .widgetURL(URL(string: "tracetodo://countdown"))
    }

    private var lockRectangular: some View {
        let item = primaryItem
        return VStack(alignment: .leading, spacing: 2) {
            Text("\(item.icon) \(item.title)")
                .font(.caption.bold())
                .lineLimit(1)
            Text("\(item.badgeLabel) \(item.displayNumber) \(item.unitLabel)")
                .font(.system(size: 13, weight: .heavy, design: .rounded))
                .widgetAccentable()
            Text("\(item.displaySolarDate) · \(item.lunarLabel)")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .widgetURL(URL(string: "tracetodo://countdown"))
    }

    private var lockInline: some View {
        let item = primaryItem
        return Text("\(item.icon) \(item.title) · \(item.badgeLabel)\(item.displayNumber)\(item.unitLabel)")
            .widgetURL(URL(string: "tracetodo://countdown"))
    }
    #endif
}

struct TraceCountdownWidget: Widget {
    let kind: String = "TraceCountdownWidget"

    #if os(iOS)
    private let families: [WidgetFamily] = [
        .systemSmall,
        .systemMedium,
        .accessoryCircular,
        .accessoryRectangular,
        .accessoryInline
    ]
    #else
    private let families: [WidgetFamily] = [
        .systemSmall,
        .systemMedium
    ]
    #endif

    var body: some WidgetConfiguration {
        StaticConfiguration(
            kind: kind,
            provider: TraceCountdownStaticProvider()
        ) { entry in
            TraceCountdownWidgetContainer(entry: entry)
        }
        .configurationDisplayName("有迹 倒数纪念日")
        .description("桌面与锁屏展示公农历倒数日与正数纪念日（春节、生日、周年纪念）。")
        .supportedFamilies(families)
    }
}

private struct TraceCountdownWidgetContainer: View {
    @Environment(\.widgetFamily) private var family
    let entry: TraceCountdownEntry

    var body: some View {
        #if os(iOS)
        switch family {
        case .accessoryCircular, .accessoryRectangular, .accessoryInline:
            TraceCountdownWidgetEntryView(entry: entry)
                .containerBackground(.clear, for: .widget)
        default:
            TraceCountdownWidgetEntryView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
        }
        #else
        TraceCountdownWidgetEntryView(entry: entry)
            .containerBackground(.fill.tertiary, for: .widget)
        #endif
    }
}

// MARK: - Memo Notes Widget (StaticConfiguration)

struct WidgetMemoRow: Identifiable, Hashable {
    let id: String
    let title: String
    let excerpt: String
    let pinned: Bool
    let tags: [String]
    let updatedLabel: String
}

private struct RawWidgetMemoListDTO: Decodable {
    let total: Int?
    let pinnedCount: Int?
    let memos: [RawWidgetMemoItemDTO]

    enum CodingKeys: String, CodingKey {
        case total
        case pinnedCount = "pinned_count"
        case memos
    }
}

private struct RawWidgetMemoItemDTO: Decodable {
    let noteId: String
    let title: String?
    let content: String
    let pinned: Bool?
    let tags: [String]?
    let updatedAt: String?

    enum CodingKeys: String, CodingKey {
        case noteId = "note_id"
        case title, content, pinned, tags
        case updatedAt = "updated_at"
    }
}

struct TraceMemoEntry: TimelineEntry {
    let date: Date
    let totalCount: Int
    let pinnedCount: Int
    let memos: [WidgetMemoRow]
    let needsLogin: Bool

    static func stripMarkdown(_ raw: String) -> String {
        raw
            .replacingOccurrences(of: #"(?m)^#{1,6}\s*"#, with: "", options: .regularExpression)
            .replacingOccurrences(of: #"(?m)^[-*•]\s*(\[[ xX]\]\s*)?"#, with: "• ", options: .regularExpression)
            .replacingOccurrences(of: #"(?m)^>\s*"#, with: "", options: .regularExpression)
            .replacingOccurrences(of: "**", with: "")
            .replacingOccurrences(of: "`", with: "")
            .components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
            .joined(separator: " ")
    }

    static func placeholder(needsLogin: Bool = false) -> TraceMemoEntry {
        if needsLogin {
            return TraceMemoEntry(date: Date(), totalCount: 0, pinnedCount: 0, memos: [], needsLogin: true)
        }
        return TraceMemoEntry(
            date: Date(),
            totalCount: 4,
            pinnedCount: 2,
            memos: [
                WidgetMemoRow(id: "m1", title: "阅读与灵感清单", excerpt: "深度工作、双链记忆检索设计要点与下阶段实验想法", pinned: true, tags: ["灵感", "读书"], updatedLabel: "今日"),
                WidgetMemoRow(id: "m2", title: "出差报销凭证备忘", excerpt: "高铁票、住宿水单已拍照识别 OCR，周五统一提交财务", pinned: true, tags: ["财务"], updatedLabel: "昨日"),
                WidgetMemoRow(id: "m3", title: "周会核心纪要", excerpt: "确认客户端 v1.4.7 桌面组件矩阵升级与实时 Markdown 渲染", pinned: false, tags: ["会议"], updatedLabel: "本周")
            ],
            needsLogin: false
        )
    }
}

struct TraceMemoStaticProvider: TimelineProvider {
    func placeholder(in context: Context) -> TraceMemoEntry {
        .placeholder()
    }

    func getSnapshot(in context: Context, completion: @escaping (TraceMemoEntry) -> Void) {
        if context.isPreview {
            if let cached = SharedWidgetBridge.loadMemosSnapshotData(),
               let parsed = try? Self.decodeMemoData(cached) {
                completion(parsed)
                return
            }
            completion(.placeholder())
            return
        }
        Task {
            completion(await Self.fetchMemoEntry())
        }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<TraceMemoEntry>) -> Void) {
        Task {
            let entry = await Self.fetchMemoEntry()
            let mins = entry.needsLogin ? 5 : 20
            let nextRefresh = Calendar.current.date(byAdding: .minute, value: mins, to: Date()) ?? Date().addingTimeInterval(Double(mins * 60))
            completion(Timeline(entries: [entry], policy: .after(nextRefresh)))
        }
    }

    static func decodeMemoData(_ data: Data) throws -> TraceMemoEntry {
        let dto = try JSONDecoder().decode(RawWidgetMemoListDTO.self, from: data)
        let rows = dto.memos.map { m -> WidgetMemoRow in
            let cleanBody = TraceMemoEntry.stripMarkdown(m.content)
            let rawTitle = (m.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let displayTitle: String
            let displayExcerpt: String
            if !rawTitle.isEmpty {
                displayTitle = rawTitle
                displayExcerpt = cleanBody
            } else {
                let firstLine = m.content.components(separatedBy: .newlines).first { !$0.trimmingCharacters(in: .whitespaces).isEmpty } ?? "随记便签"
                displayTitle = String(TraceMemoEntry.stripMarkdown(firstLine).prefix(22))
                displayExcerpt = cleanBody
            }
            let upd = m.updatedAt.map { String($0.prefix(10)) } ?? ""
            return WidgetMemoRow(
                id: m.noteId,
                title: displayTitle.isEmpty ? "随记便签" : displayTitle,
                excerpt: displayExcerpt,
                pinned: m.pinned ?? false,
                tags: m.tags ?? [],
                updatedLabel: upd
            )
        }
        let pinCnt = dto.pinnedCount ?? rows.filter(\.pinned).count
        return TraceMemoEntry(
            date: Date(),
            totalCount: dto.total ?? rows.count,
            pinnedCount: pinCnt,
            memos: rows,
            needsLogin: false
        )
    }

    static func fetchMemoEntry() async -> TraceMemoEntry {
        let resolved = WidgetAPI.resolveSharedConfig()
        let base = resolved.base
        let space = resolved.space
        let token = resolved.token

        if var comps = URLComponents(string: "\(base)/api/memos/list") {
            var qItems = [URLQueryItem(name: "limit", value: "6")]
            if token.isEmpty || (!space.isEmpty && space != "default") {
                qItems.append(URLQueryItem(name: "space_id", value: space.isEmpty ? "default" : space))
            }
            comps.queryItems = qItems
            if let url = comps.url {
                var req = URLRequest(url: url)
                req.timeoutInterval = 8
                if !token.isEmpty {
                    req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                }
                if let (data, resp) = try? await URLSession.shared.data(for: req),
                   let http = resp as? HTTPURLResponse {
                    if (200...299).contains(http.statusCode),
                       let parsed = try? decodeMemoData(data) {
                        SharedWidgetBridge.saveMemosSnapshotData(data)
                        return parsed
                    }
                    if http.statusCode == 401 || http.statusCode == 403 {
                        if let cached = SharedWidgetBridge.loadMemosSnapshotData(),
                           let parsed = try? decodeMemoData(cached) {
                            return parsed
                        }
                        return .placeholder(needsLogin: true)
                    }
                }
            }
        }

        if let cached = SharedWidgetBridge.loadMemosSnapshotData(),
           let parsed = try? decodeMemoData(cached) {
            return parsed
        }
        return .placeholder(needsLogin: token.isEmpty)
    }
}

struct TraceMemoWidgetEntryView: View {
    @Environment(\.widgetFamily) private var family
    let entry: TraceMemoEntry

    private let warmGradient = LinearGradient(
        colors: [Color(red: 0.93, green: 0.44, blue: 0.14), Color(red: 0.85, green: 0.25, blue: 0.52)],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    var body: some View {
        switch family {
        case .systemSmall:
            smallMemoView
        case .systemMedium:
            mediumMemoView
        case .systemLarge:
            largeMemoView
        #if os(iOS)
        case .accessoryRectangular:
            lockMemoView
        #endif
        default:
            mediumMemoView
        }
    }

    private var smallMemoView: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                HStack(spacing: 3) {
                    Image(systemName: "pin.fill")
                        .font(.system(size: 9.5, weight: .bold))
                    Text("备忘 \(entry.totalCount)")
                        .font(.system(size: 10.5, weight: .bold))
                }
                .foregroundStyle(.white)
                .padding(.horizontal, 7)
                .padding(.vertical, 3)
                .background(warmGradient)
                .clipShape(Capsule())

                Spacer()

                Link(destination: URL(string: "tracetodo://notes")!) {
                    Image(systemName: "square.and.pencil")
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(Color(red: 0.88, green: 0.36, blue: 0.28))
                }
            }

            if entry.needsLogin {
                Spacer(minLength: 2)
                Text("🔐 打开 有迹 登录同步随记与置顶灵感")
                    .font(.system(size: 11.5, weight: .semibold))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 2)
            } else if let top = entry.memos.first {
                Text(top.title)
                    .font(.system(size: 12.5, weight: .bold))
                    .lineLimit(1)
                Text(top.excerpt.isEmpty ? "轻点查看完整 Markdown 随记" : top.excerpt)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
                Spacer(minLength: 0)
                if let firstTag = top.tags.first {
                    Text("#\(firstTag)")
                        .font(.system(size: 9.5, weight: .semibold))
                        .foregroundStyle(Color(red: 0.88, green: 0.36, blue: 0.28))
                }
            } else {
                Spacer(minLength: 2)
                Text("暂无随记备忘，轻点随时记录灵感与照片 OCR")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 2)
            }
        }
        .widgetURL(URL(string: entry.needsLogin ? "tracetodo://login" : "tracetodo://notes"))
    }

    private var mediumMemoView: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 5) {
                HStack {
                    Text("MEMOS")
                        .font(.system(size: 9.5, weight: .bold))
                        .tracking(0.8)
                        .foregroundStyle(.white.opacity(0.88))
                    Spacer()
                    Image(systemName: "pin.fill")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(.white.opacity(0.9))
                }

                Text("\(entry.totalCount)")
                    .font(.system(size: 28, weight: .heavy, design: .rounded))
                    .foregroundStyle(.white)

                Spacer(minLength: 2)

                Text("📌 置顶 \(entry.pinnedCount) 条")
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.92))

                Link(destination: URL(string: "tracetodo://notes")!) {
                    Label("记随记", systemImage: "square.and.pencil")
                        .font(.system(size: 10, weight: .bold))
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3.5)
                        .background(Color.white.opacity(0.22))
                        .foregroundStyle(.white)
                        .clipShape(Capsule())
                }
            }
            .padding(11)
            .frame(width: 110)
            .frame(maxHeight: .infinity, alignment: .leading)
            .background(warmGradient)
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))

            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("置顶与最新备忘")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(.secondary)
                    Spacer()
                    Link(destination: URL(string: "tracetodo://refresh")!) {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 10.5, weight: .semibold))
                            .foregroundStyle(.secondary)
                    }
                }

                if entry.needsLogin {
                    Spacer()
                    Text("🔐 轻点打开应用登录同步随记备忘")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                } else if entry.memos.isEmpty {
                    Spacer()
                    Text("✨ 还没有备忘录，点击左侧「记随记」写下第一篇")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                } else {
                    ForEach(entry.memos.prefix(3)) { memo in
                        VStack(alignment: .leading, spacing: 1.5) {
                            HStack(spacing: 4) {
                                if memo.pinned {
                                    Image(systemName: "pin.fill")
                                        .font(.system(size: 9))
                                        .foregroundStyle(Color(red: 0.90, green: 0.38, blue: 0.20))
                                }
                                Text(memo.title)
                                    .font(.system(size: 12, weight: .semibold))
                                    .lineLimit(1)
                                Spacer(minLength: 2)
                                if let tag = memo.tags.first {
                                    Text("#\(tag)")
                                        .font(.system(size: 9, weight: .bold))
                                        .foregroundStyle(Color(red: 0.88, green: 0.36, blue: 0.28))
                                }
                            }
                            if !memo.excerpt.isEmpty && memo.excerpt != memo.title {
                                Text(memo.excerpt)
                                    .font(.system(size: 10.5))
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                        }
                        .padding(.vertical, 1.5)
                    }
                    Spacer(minLength: 0)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .widgetURL(URL(string: entry.needsLogin ? "tracetodo://login" : "tracetodo://notes"))
    }

    private var largeMemoView: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                VStack(alignment: .leading, spacing: 1) {
                    Text("📌 有迹 · 随记备忘录")
                        .font(.system(size: 14, weight: .bold))
                    Text("共 \(entry.totalCount) 条备忘 · 已置顶 \(entry.pinnedCount) 条")
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Link(destination: URL(string: "tracetodo://refresh")!) {
                    Image(systemName: "arrow.clockwise.circle.fill")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                }
                Link(destination: URL(string: "tracetodo://notes")!) {
                    Image(systemName: "square.and.pencil.circle.fill")
                        .font(.title3)
                        .foregroundStyle(Color(red: 0.90, green: 0.38, blue: 0.22))
                }
            }

            Divider()

            if entry.memos.isEmpty {
                Spacer()
                Text(entry.needsLogin ? "🔐 打开 有迹 登录后即可在桌面浏览置顶备忘" : "✨ 暂无备忘录，轻点右上角随时记录 Markdown 灵感或票据照片")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(entry.memos.prefix(5)) { memo in
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 5) {
                                if memo.pinned {
                                    Text("置顶")
                                        .font(.system(size: 9, weight: .bold))
                                        .padding(.horizontal, 5)
                                        .padding(.vertical, 1.5)
                                        .background(Color(red: 0.92, green: 0.40, blue: 0.20).opacity(0.16))
                                        .foregroundStyle(Color(red: 0.90, green: 0.36, blue: 0.18))
                                        .clipShape(Capsule())
                                }
                                Text(memo.title)
                                    .font(.system(size: 12.5, weight: .bold))
                                    .lineLimit(1)
                                Spacer(minLength: 4)
                                ForEach(memo.tags.prefix(2), id: \.self) { t in
                                    Text("#\(t)")
                                        .font(.system(size: 9.5, weight: .semibold))
                                        .foregroundStyle(.secondary)
                                }
                            }
                            if !memo.excerpt.isEmpty {
                                Text(memo.excerpt)
                                    .font(.system(size: 11))
                                    .foregroundStyle(.secondary)
                                    .lineLimit(2)
                            }
                        }
                        .padding(8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.primary.opacity(0.04))
                        .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
                    }
                }
                Spacer(minLength: 0)
            }
        }
        .widgetURL(URL(string: entry.needsLogin ? "tracetodo://login" : "tracetodo://notes"))
    }

    #if os(iOS)
    private var lockMemoView: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Label("随记备忘", systemImage: "pin.fill")
                    .font(.caption2.bold())
                Spacer()
                Text("\(entry.totalCount)条")
                    .font(.caption2)
            }
            if let top = entry.memos.first {
                Text(top.title)
                    .font(.caption.bold())
                    .lineLimit(1)
                Text(top.excerpt)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            } else {
                Text("轻点记录灵感备忘")
                    .font(.caption)
            }
        }
        .widgetURL(URL(string: "tracetodo://notes"))
    }
    #endif
}

struct TraceMemoWidget: Widget {
    let kind: String = "TraceMemoWidget"

    #if os(iOS)
    private let families: [WidgetFamily] = [
        .systemSmall,
        .systemMedium,
        .systemLarge,
        .accessoryRectangular
    ]
    #else
    private let families: [WidgetFamily] = [
        .systemSmall,
        .systemMedium,
        .systemLarge
    ]
    #endif

    var body: some WidgetConfiguration {
        StaticConfiguration(
            kind: kind,
            provider: TraceMemoStaticProvider()
        ) { entry in
            #if os(iOS)
            TraceMemoWidgetContainer(entry: entry)
            #else
            TraceMemoWidgetEntryView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
            #endif
        }
        .configurationDisplayName("有迹 随记备忘录")
        .description("在桌面随时查看置顶灵感、读书清单与最新随记便签，一键打开速记。")
        .supportedFamilies(families)
    }
}

#if os(iOS)
private struct TraceMemoWidgetContainer: View {
    @Environment(\.widgetFamily) private var family
    let entry: TraceMemoEntry

    var body: some View {
        switch family {
        case .accessoryRectangular:
            TraceMemoWidgetEntryView(entry: entry)
                .containerBackground(.clear, for: .widget)
        default:
            TraceMemoWidgetEntryView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
        }
    }
}
#endif

// MARK: - Today Calendar & Schedule Timeline Widget (StaticConfiguration)

struct WidgetCalendarDayDot: Identifiable, Hashable {
    let id: String
    let dayNumber: Int
    let shortWeekday: String
    let isToday: Bool
    let hasEvents: Bool
    let isOffDay: Bool
    let subLabel: String
}

struct WidgetScheduleRow: Identifiable, Hashable {
    let id: String
    let timeBadge: String
    let title: String
    let isDone: Bool
    let isAllDay: Bool
    let colorTheme: String

    var accentColor: Color {
        switch colorTheme {
        case "crimson", "peach": return Color(red: 0.91, green: 0.30, blue: 0.28)
        case "mint": return Color(red: 0.06, green: 0.67, blue: 0.50)
        case "lavender", "pink": return Color(red: 0.55, green: 0.34, blue: 0.95)
        default: return Color(red: 0.14, green: 0.54, blue: 0.95)
        }
    }
}

private struct RawWidgetCalOverviewDTO: Decodable {
    let todayDate: String?
    let periodLabel: String?
    let days: [RawWidgetCalDayDTO]
    let unscheduledTasks: [RawWidgetCalUnscheduledDTO]?

    enum CodingKeys: String, CodingKey {
        case todayDate = "today_date"
        case periodLabel = "period_label"
        case days
        case unscheduledTasks = "unscheduled_tasks"
    }
}

private struct RawWidgetCalDayDTO: Decodable {
    let date: String
    let month: Int
    let day: Int
    let weekdayLabel: String?
    let isToday: Bool?
    let isWeekend: Bool?
    let focusMinutes: Int?
    let allDayTasks: [RawWidgetCalAllDayDTO]?
    let timedBlocks: [RawWidgetCalTimedDTO]?
    let lunarShort: String?
    let solarTerm: String?
    let festivals: [String]?
    let calendarSubLabel: String?
    let holidayStatus: String?
    let isOffDay: Bool?

    enum CodingKeys: String, CodingKey {
        case date, month, day
        case weekdayLabel = "weekday_label"
        case isToday = "is_today"
        case isWeekend = "is_weekend"
        case focusMinutes = "focus_minutes"
        case allDayTasks = "all_day_tasks"
        case timedBlocks = "timed_blocks"
        case lunarShort = "lunar_short"
        case solarTerm = "solar_term"
        case festivals
        case calendarSubLabel = "calendar_sub_label"
        case holidayStatus = "holiday_status"
        case isOffDay = "is_off_day"
    }
}

private struct RawWidgetCalAllDayDTO: Decodable {
    let memoryId: String
    let title: String
    let isDone: Bool?
    let colorTheme: String?

    enum CodingKeys: String, CodingKey {
        case memoryId = "memory_id"
        case title
        case isDone = "is_done"
        case colorTheme = "color_theme"
    }
}

private struct RawWidgetCalTimedDTO: Decodable {
    let memoryId: String
    let title: String
    let isDone: Bool?
    let colorTheme: String?
    let timeRangeLabel: String?
    let startMinute: Int?

    enum CodingKeys: String, CodingKey {
        case memoryId = "memory_id"
        case title
        case isDone = "is_done"
        case colorTheme = "color_theme"
        case timeRangeLabel = "time_range_label"
        case startMinute = "start_minute"
    }
}

private struct RawWidgetCalUnscheduledDTO: Decodable {
    let memoryId: String
    enum CodingKeys: String, CodingKey { case memoryId = "memory_id" }
}

struct TraceCalendarEntry: TimelineEntry {
    let date: Date
    let monthNumber: Int
    let dayNumber: Int
    let weekdayLabel: String
    let lunarLabel: String
    let holidayStatus: String
    let isOffDay: Bool
    let focusMinutes: Int
    let unscheduledCount: Int
    let weekDays: [WidgetCalendarDayDot]
    let todaySchedules: [WidgetScheduleRow]
    let needsLogin: Bool

    static func placeholder(needsLogin: Bool = false) -> TraceCalendarEntry {
        let cal = Calendar.current
        let now = Date()
        let m = cal.component(.month, from: now)
        let d = cal.component(.day, from: now)
        let wdNames = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
        let wdIdx = max(0, min(6, cal.component(.weekday, from: now) - 1))
        let isTodayWknd = wdIdx == 0 || wdIdx == 6
        if needsLogin {
            return TraceCalendarEntry(
                date: now,
                monthNumber: m,
                dayNumber: d,
                weekdayLabel: wdNames[wdIdx],
                lunarLabel: "",
                holidayStatus: "",
                isOffDay: isTodayWknd,
                focusMinutes: 0,
                unscheduledCount: 0,
                weekDays: [],
                todaySchedules: [],
                needsLogin: true
            )
        }
        return TraceCalendarEntry(
            date: now,
            monthNumber: m,
            dayNumber: d,
            weekdayLabel: wdNames[wdIdx],
            lunarLabel: "十月十七",
            holidayStatus: "",
            isOffDay: isTodayWknd,
            focusMinutes: 50,
            unscheduledCount: 2,
            weekDays: [
                WidgetCalendarDayDot(id: "w1", dayNumber: max(1, d - 1), shortWeekday: "一", isToday: false, hasEvents: true, isOffDay: false, subLabel: "十六"),
                WidgetCalendarDayDot(id: "w2", dayNumber: d, shortWeekday: "二", isToday: true, hasEvents: true, isOffDay: isTodayWknd, subLabel: "十七"),
                WidgetCalendarDayDot(id: "w3", dayNumber: d + 1, shortWeekday: "三", isToday: false, hasEvents: true, isOffDay: false, subLabel: "十八"),
                WidgetCalendarDayDot(id: "w4", dayNumber: d + 2, shortWeekday: "四", isToday: false, hasEvents: false, isOffDay: false, subLabel: "十九"),
                WidgetCalendarDayDot(id: "w5", dayNumber: d + 3, shortWeekday: "五", isToday: false, hasEvents: true, isOffDay: false, subLabel: "二十"),
                WidgetCalendarDayDot(id: "w6", dayNumber: d + 4, shortWeekday: "六", isToday: false, hasEvents: false, isOffDay: true, subLabel: "廿一"),
                WidgetCalendarDayDot(id: "w7", dayNumber: d + 5, shortWeekday: "日", isToday: false, hasEvents: false, isOffDay: true, subLabel: "廿二")
            ],
            todaySchedules: [
                WidgetScheduleRow(id: "s1", timeBadge: "09:30–11:00", title: "核心课题方案评审", isDone: false, isAllDay: false, colorTheme: "sky"),
                WidgetScheduleRow(id: "s2", timeBadge: "14:00–15:30", title: "深度代码重构与联调", isDone: false, isAllDay: false, colorTheme: "lavender"),
                WidgetScheduleRow(id: "s3", timeBadge: "全天", title: "提交周度进度汇总", isDone: false, isAllDay: true, colorTheme: "mint")
            ],
            needsLogin: false
        )
    }
}

struct TraceCalendarStaticProvider: TimelineProvider {
    func placeholder(in context: Context) -> TraceCalendarEntry {
        .placeholder()
    }

    func getSnapshot(in context: Context, completion: @escaping (TraceCalendarEntry) -> Void) {
        if context.isPreview {
            if let cached = SharedWidgetBridge.loadCalendarSnapshotData(),
               let parsed = try? Self.decodeCalendarData(cached) {
                completion(parsed)
                return
            }
            completion(.placeholder())
            return
        }
        Task {
            completion(await Self.fetchCalendarEntry())
        }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<TraceCalendarEntry>) -> Void) {
        Task {
            let entry = await Self.fetchCalendarEntry()
            let mins = entry.needsLogin ? 5 : 15
            let nextRefresh = Calendar.current.date(byAdding: .minute, value: mins, to: Date()) ?? Date().addingTimeInterval(Double(mins * 60))
            completion(Timeline(entries: [entry], policy: .after(nextRefresh)))
        }
    }

    static func decodeCalendarData(_ data: Data) throws -> TraceCalendarEntry {
        let dto = try JSONDecoder().decode(RawWidgetCalOverviewDTO.self, from: data)
        let weekDots: [WidgetCalendarDayDot] = dto.days.prefix(7).map { d in
            let wdRaw = d.weekdayLabel ?? ""
            let shortWd = String(wdRaw.suffix(1))
            let cnt = (d.timedBlocks?.count ?? 0) + (d.allDayTasks?.count ?? 0)
            let hol = d.holidayStatus ?? ""
            let isOff = d.isOffDay ?? (hol == "rest" || ((d.isWeekend ?? false) && hol != "work"))
            let sub = (d.calendarSubLabel ?? "").isEmpty ? (d.lunarShort ?? "") : (d.calendarSubLabel ?? "")
            return WidgetCalendarDayDot(
                id: d.date,
                dayNumber: d.day,
                shortWeekday: shortWd.isEmpty ? "\(d.day)" : shortWd,
                isToday: d.isToday ?? false,
                hasEvents: cnt > 0,
                isOffDay: isOff,
                subLabel: sub
            )
        }

        let todayBucket = dto.days.first(where: { $0.isToday == true }) ?? dto.days.first
        let cal = Calendar.current
        let now = Date()
        let monthNum = todayBucket?.month ?? cal.component(.month, from: now)
        let dayNum = todayBucket?.day ?? cal.component(.day, from: now)
        let wdLabel = todayBucket?.weekdayLabel ?? "今日"
        let focusMins = todayBucket?.focusMinutes ?? 0
        let todayHol = todayBucket?.holidayStatus ?? ""
        let wdIdx = max(0, min(6, cal.component(.weekday, from: now) - 1))
        let localWknd = wdIdx == 0 || wdIdx == 6
        let todayIsOff = todayBucket?.isOffDay ?? (todayHol == "rest" || ((todayBucket?.isWeekend ?? localWknd) && todayHol != "work"))
        let lunarShort = todayBucket?.lunarShort ?? ""
        let subLbl = todayBucket?.calendarSubLabel ?? ""
        let solarTerm = todayBucket?.solarTerm ?? ""
        let firstFest = todayBucket?.festivals?.first ?? ""
        let extra = !firstFest.isEmpty ? firstFest : (!solarTerm.isEmpty ? solarTerm : "")
        let lunarDisplay: String = {
            if !lunarShort.isEmpty && !extra.isEmpty {
                return "\(lunarShort) · \(extra)"
            }
            if !lunarShort.isEmpty { return lunarShort }
            return subLbl
        }()

        var schedules: [WidgetScheduleRow] = []
        if let timed = todayBucket?.timedBlocks {
            let sortedTimed = timed.sorted { ($0.startMinute ?? 0) < ($1.startMinute ?? 0) }
            for tb in sortedTimed {
                let rangeStr = (tb.timeRangeLabel ?? "日程").replacingOccurrences(of: " - ", with: "–")
                schedules.append(
                    WidgetScheduleRow(
                        id: "t-\(tb.memoryId)",
                        timeBadge: rangeStr,
                        title: tb.title,
                        isDone: tb.isDone ?? false,
                        isAllDay: false,
                        colorTheme: tb.colorTheme ?? "sky"
                    )
                )
            }
        }
        if let allDay = todayBucket?.allDayTasks {
            for ad in allDay {
                schedules.append(
                    WidgetScheduleRow(
                        id: "a-\(ad.memoryId)",
                        timeBadge: "全天",
                        title: ad.title,
                        isDone: ad.isDone ?? false,
                        isAllDay: true,
                        colorTheme: ad.colorTheme ?? "mint"
                    )
                )
            }
        }

        return TraceCalendarEntry(
            date: now,
            monthNumber: monthNum,
            dayNumber: dayNum,
            weekdayLabel: wdLabel,
            lunarLabel: lunarDisplay,
            holidayStatus: todayHol,
            isOffDay: todayIsOff,
            focusMinutes: focusMins,
            unscheduledCount: dto.unscheduledTasks?.count ?? 0,
            weekDays: weekDots,
            todaySchedules: schedules,
            needsLogin: false
        )
    }

    static func fetchCalendarEntry() async -> TraceCalendarEntry {
        let resolved = WidgetAPI.resolveSharedConfig()
        let base = resolved.base
        let space = resolved.space
        let token = resolved.token

        if var comps = URLComponents(string: "\(base)/api/calendar/overview") {
            var qItems = [
                URLQueryItem(name: "view", value: "week"),
                URLQueryItem(name: "include_done", value: "true")
            ]
            if token.isEmpty || (!space.isEmpty && space != "default") {
                qItems.append(URLQueryItem(name: "space_id", value: space.isEmpty ? "default" : space))
            }
            comps.queryItems = qItems
            if let url = comps.url {
                var req = URLRequest(url: url)
                req.timeoutInterval = 8
                if !token.isEmpty {
                    req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                }
                if let (data, resp) = try? await URLSession.shared.data(for: req),
                   let http = resp as? HTTPURLResponse {
                    if (200...299).contains(http.statusCode),
                       let parsed = try? decodeCalendarData(data) {
                        SharedWidgetBridge.saveCalendarSnapshotData(data)
                        return parsed
                    }
                    if http.statusCode == 401 || http.statusCode == 403 {
                        if let cached = SharedWidgetBridge.loadCalendarSnapshotData(),
                           let parsed = try? decodeCalendarData(cached) {
                            return parsed
                        }
                        return .placeholder(needsLogin: true)
                    }
                }
            }
        }

        if let cached = SharedWidgetBridge.loadCalendarSnapshotData(),
           let parsed = try? decodeCalendarData(cached) {
            return parsed
        }
        return .placeholder(needsLogin: token.isEmpty)
    }
}

struct TraceCalendarWidgetEntryView: View {
    @Environment(\.widgetFamily) private var family
    let entry: TraceCalendarEntry

    private let offDayRed = Color(red: 0.88, green: 0.11, blue: 0.28)

    private let tealGradient = LinearGradient(
        colors: [Color(red: 0.05, green: 0.62, blue: 0.53), Color(red: 0.14, green: 0.48, blue: 0.89)],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    private let restCardGradient = LinearGradient(
        colors: [Color(red: 0.88, green: 0.11, blue: 0.28), Color(red: 0.96, green: 0.32, blue: 0.44)],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    @ViewBuilder
    private func holPill(_ status: String) -> some View {
        if status == "rest" {
            Text("休")
                .font(.system(size: 8.5, weight: .bold))
                .padding(.horizontal, 4)
                .padding(.vertical, 1)
                .background(offDayRed.opacity(0.16))
                .foregroundStyle(offDayRed)
                .clipShape(RoundedRectangle(cornerRadius: 3.5, style: .continuous))
        } else if status == "work" {
            Text("班")
                .font(.system(size: 8.5, weight: .bold))
                .padding(.horizontal, 4)
                .padding(.vertical, 1)
                .background(Color.orange.opacity(0.20))
                .foregroundStyle(Color(red: 0.80, green: 0.38, blue: 0.04))
                .clipShape(RoundedRectangle(cornerRadius: 3.5, style: .continuous))
        }
    }

    var body: some View {
        switch family {
        case .systemSmall:
            smallCalView
        case .systemMedium:
            mediumCalView
        case .systemLarge:
            largeCalView
        #if os(iOS)
        case .accessoryRectangular:
            lockCalView
        #endif
        default:
            mediumCalView
        }
    }

    private var smallCalView: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text("\(entry.dayNumber)")
                    .font(.system(size: 28, weight: .heavy, design: .rounded))
                    .foregroundStyle(entry.isOffDay ? offDayRed : Color(red: 0.06, green: 0.58, blue: 0.50))
                VStack(alignment: .leading, spacing: 1) {
                    HStack(spacing: 4) {
                        Text("\(entry.monthNumber)月 · \(entry.weekdayLabel)")
                            .font(.system(size: 10, weight: .bold))
                            .foregroundStyle(entry.isOffDay ? offDayRed : .secondary)
                        holPill(entry.holidayStatus)
                    }
                    Text(!entry.lunarLabel.isEmpty ? "农历\(entry.lunarLabel)" : "今日日程 \(entry.todaySchedules.count)")
                        .font(.system(size: 9.5, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }

            Divider()

            if entry.needsLogin {
                Spacer(minLength: 2)
                Text("🔐 打开应用同步日历排期")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 2)
            } else if entry.todaySchedules.isEmpty {
                Spacer(minLength: 2)
                Text("今日暂无排期日程，轻点规划时间块 ✨")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                Spacer(minLength: 2)
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(entry.todaySchedules.prefix(2)) { row in
                        HStack(spacing: 5) {
                            RoundedRectangle(cornerRadius: 1.5)
                                .fill(row.accentColor)
                                .frame(width: 3, height: 13)
                            VStack(alignment: .leading, spacing: 0) {
                                Text(row.title)
                                    .font(.system(size: 11, weight: .semibold))
                                    .strikethrough(row.isDone)
                                    .foregroundStyle(row.isDone ? .secondary : .primary)
                                    .lineLimit(1)
                                Text(row.timeBadge)
                                    .font(.system(size: 9, design: .monospaced))
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                        }
                    }
                }
                Spacer(minLength: 0)
            }
        }
        .widgetURL(URL(string: entry.needsLogin ? "tracetodo://login" : "tracetodo://calendar"))
    }

    private var mediumCalView: some View {
        let holSuffix = entry.holidayStatus == "rest" ? " · 休" : (entry.holidayStatus == "work" ? " · 班" : "")
        return HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text("\(entry.monthNumber)月 · \(entry.weekdayLabel)\(holSuffix)")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(.white.opacity(0.92))
                    .lineLimit(1)

                Text("\(entry.dayNumber)")
                    .font(.system(size: 32, weight: .heavy, design: .rounded))
                    .foregroundStyle(.white)

                if !entry.lunarLabel.isEmpty {
                    Text("农历\(entry.lunarLabel)")
                        .font(.system(size: 9.2, weight: .semibold))
                        .foregroundStyle(.white.opacity(0.92))
                        .lineLimit(1)
                }

                Spacer(minLength: 2)

                Text(entry.focusMinutes > 0 ? "🍅 今日专注 \(entry.focusMinutes)m" : "📅 今日 \(entry.todaySchedules.count) 项日程")
                    .font(.system(size: 9.5, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.92))
                    .lineLimit(1)

                Link(destination: URL(string: "tracetodo://calendar")!) {
                    Label("时间轴", systemImage: "calendar")
                        .font(.system(size: 10, weight: .bold))
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Color.white.opacity(0.22))
                        .foregroundStyle(.white)
                        .clipShape(Capsule())
                }
            }
            .padding(11)
            .frame(width: 112)
            .frame(maxHeight: .infinity, alignment: .leading)
            .background(entry.isOffDay ? restCardGradient : tealGradient)
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))

            VStack(alignment: .leading, spacing: 6) {
                if !entry.weekDays.isEmpty {
                    HStack(spacing: 3) {
                        ForEach(entry.weekDays) { wd in
                            VStack(spacing: 1) {
                                Text(wd.shortWeekday)
                                    .font(.system(size: 8.5, weight: .semibold))
                                    .foregroundStyle(wd.isToday ? Color.white : (wd.isOffDay ? offDayRed : .secondary))
                                Text("\(wd.dayNumber)")
                                    .font(.system(size: 10, weight: .bold, design: .rounded))
                                    .foregroundStyle(wd.isToday ? Color.white : (wd.isOffDay ? offDayRed : .primary))
                                Circle()
                                    .fill(wd.isToday ? Color.white : (wd.hasEvents ? (wd.isOffDay ? offDayRed : Color(red: 0.06, green: 0.62, blue: 0.53)) : Color.clear))
                                    .frame(width: 3.5, height: 3.5)
                            }
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 3)
                            .background(wd.isToday ? (wd.isOffDay ? offDayRed : Color(red: 0.06, green: 0.60, blue: 0.52)) : Color.clear)
                            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                        }
                    }
                }

                if entry.needsLogin {
                    Spacer()
                    Text("🔐 轻点打开 有迹 登录同步日历日程")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                } else if entry.todaySchedules.isEmpty {
                    Spacer()
                    Text("✨ 今日暂无排期事项，轻点进入周时间轴排程")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                } else {
                    ForEach(entry.todaySchedules.prefix(3)) { row in
                        HStack(spacing: 6) {
                            RoundedRectangle(cornerRadius: 1.5)
                                .fill(row.accentColor)
                                .frame(width: 3, height: 14)
                            Text(row.timeBadge)
                                .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                                .foregroundStyle(row.accentColor)
                                .lineLimit(1)
                            Text(row.title)
                                .font(.system(size: 11.5, weight: .semibold))
                                .strikethrough(row.isDone)
                                .foregroundStyle(row.isDone ? .secondary : .primary)
                                .lineLimit(1)
                            Spacer(minLength: 0)
                        }
                    }
                    Spacer(minLength: 0)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .widgetURL(URL(string: entry.needsLogin ? "tracetodo://login" : "tracetodo://calendar"))
    }

    private var largeCalView: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 5) {
                        Text("📅 \(entry.monthNumber)月\(entry.dayNumber)日 \(entry.weekdayLabel)")
                            .font(.system(size: 14, weight: .bold))
                            .foregroundStyle(entry.isOffDay ? offDayRed : .primary)
                        holPill(entry.holidayStatus)
                        if !entry.lunarLabel.isEmpty {
                            Text("农历\(entry.lunarLabel)")
                                .font(.system(size: 10, weight: .semibold))
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                    }
                    Text("今日日程 \(entry.todaySchedules.count) 项 · 待排期池 \(entry.unscheduledCount) 项 · 专注 \(entry.focusMinutes)m")
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Link(destination: URL(string: "tracetodo://refresh")!) {
                    Image(systemName: "arrow.clockwise.circle.fill")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                }
                Link(destination: URL(string: "tracetodo://calendar")!) {
                    Image(systemName: "calendar.circle.fill")
                        .font(.title3)
                        .foregroundStyle(entry.isOffDay ? offDayRed : Color(red: 0.06, green: 0.60, blue: 0.52))
                }
            }

            if !entry.weekDays.isEmpty {
                HStack(spacing: 5) {
                    ForEach(entry.weekDays) { wd in
                        VStack(spacing: 1.5) {
                            Text(wd.shortWeekday)
                                .font(.system(size: 9.5, weight: .semibold))
                                .foregroundStyle(wd.isToday ? Color.white : (wd.isOffDay ? offDayRed : .secondary))
                            Text("\(wd.dayNumber)")
                                .font(.system(size: 12, weight: .bold, design: .rounded))
                                .foregroundStyle(wd.isToday ? Color.white : (wd.isOffDay ? offDayRed : .primary))
                            if !wd.subLabel.isEmpty {
                                Text(wd.subLabel)
                                    .font(.system(size: 8, weight: .medium))
                                    .foregroundStyle(wd.isToday ? Color.white.opacity(0.9) : (wd.isOffDay ? offDayRed.opacity(0.85) : .secondary))
                                    .lineLimit(1)
                            }
                            Circle()
                                .fill(wd.isToday ? Color.white : (wd.hasEvents ? (wd.isOffDay ? offDayRed : Color(red: 0.06, green: 0.62, blue: 0.53)) : Color.clear))
                                .frame(width: 4, height: 4)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 4)
                        .background(
                            wd.isToday
                                ? (wd.isOffDay ? restCardGradient : tealGradient)
                                : LinearGradient(colors: [wd.isOffDay ? offDayRed.opacity(0.05) : Color.primary.opacity(0.04)], startPoint: .top, endPoint: .bottom)
                        )
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                    }
                }
            }

            Divider()

            if entry.todaySchedules.isEmpty {
                Spacer()
                Text(entry.needsLogin ? "🔐 打开 有迹 登录后在桌面查看整周排期与时间块" : "🎉 今日暂无排期日程，轻点打开日历将待排期池任务拖入时间轴")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
            } else {
                VStack(alignment: .leading, spacing: 7) {
                    ForEach(entry.todaySchedules.prefix(6)) { row in
                        HStack(spacing: 8) {
                            Image(systemName: row.isDone ? "checkmark.circle.fill" : "clock.fill")
                                .font(.system(size: 12))
                                .foregroundStyle(row.isDone ? .green : row.accentColor)
                            Text(row.timeBadge)
                                .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(row.accentColor.opacity(0.13))
                                .foregroundStyle(row.accentColor)
                                .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
                            Text(row.title)
                                .font(.system(size: 12.5, weight: .semibold))
                                .strikethrough(row.isDone)
                                .foregroundStyle(row.isDone ? .secondary : .primary)
                                .lineLimit(1)
                            Spacer(minLength: 0)
                        }
                    }
                }
                Spacer(minLength: 0)
            }
        }
        .widgetURL(URL(string: entry.needsLogin ? "tracetodo://login" : "tracetodo://calendar"))
    }

    #if os(iOS)
    private var lockCalView: some View {
        let holTag = entry.holidayStatus == "rest" ? " 休" : (entry.holidayStatus == "work" ? " 班" : "")
        let lunarTag = entry.lunarLabel.isEmpty ? "" : " · \(entry.lunarLabel)"
        return VStack(alignment: .leading, spacing: 2) {
            HStack {
                Label("\(entry.monthNumber)月\(entry.dayNumber)日 \(entry.weekdayLabel)\(holTag)\(lunarTag)", systemImage: "calendar")
                    .font(.caption2.bold())
                    .lineLimit(1)
                Spacer()
                Text("\(entry.todaySchedules.count)项")
                    .font(.caption2)
            }
            if let first = entry.todaySchedules.first {
                Text("\(first.timeBadge) \(first.title)")
                    .font(.caption)
                    .lineLimit(1)
            } else {
                Text("今日暂无排期日程")
                    .font(.caption)
            }
        }
        .widgetURL(URL(string: "tracetodo://calendar"))
    }
    #endif
}

struct TraceCalendarWidget: Widget {
    let kind: String = "TraceCalendarWidget"

    #if os(iOS)
    private let families: [WidgetFamily] = [
        .systemSmall,
        .systemMedium,
        .systemLarge,
        .accessoryRectangular
    ]
    #else
    private let families: [WidgetFamily] = [
        .systemSmall,
        .systemMedium,
        .systemLarge
    ]
    #endif

    var body: some WidgetConfiguration {
        StaticConfiguration(
            kind: kind,
            provider: TraceCalendarStaticProvider()
        ) { entry in
            #if os(iOS)
            TraceCalendarWidgetContainer(entry: entry)
            #else
            TraceCalendarWidgetEntryView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
            #endif
        }
        .configurationDisplayName("有迹 今日日历日程")
        .description("展示本周迷你日历条、今日时间段排期与全天事项，一键直达周时间轴。")
        .supportedFamilies(families)
    }
}

#if os(iOS)
private struct TraceCalendarWidgetContainer: View {
    @Environment(\.widgetFamily) private var family
    let entry: TraceCalendarEntry

    var body: some View {
        switch family {
        case .accessoryRectangular:
            TraceCalendarWidgetEntryView(entry: entry)
                .containerBackground(.clear, for: .widget)
        default:
            TraceCalendarWidgetEntryView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
        }
    }
}
#endif

@main
struct TraceTodoWidgetsBundle: WidgetBundle {
    var body: some Widget {
        TraceTodoWidget()
        TraceCalendarWidget()
        TraceMemoWidget()
        TraceCountdownWidget()
    }
}
