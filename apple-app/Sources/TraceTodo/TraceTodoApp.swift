import SwiftUI
#if os(macOS)
import AppKit
#endif

enum AppSection: String, CaseIterable, Identifiable {
    case tasks = "待办"
    case calendar = "日历"
    case focus = "专注"
    case notes = "随记"
    case memories = "记忆"

    var id: String { rawValue }

    var icon: String {
        switch self {
        case .tasks: return "checklist"
        case .calendar: return "calendar"
        case .focus: return "timer"
        case .notes: return "note.text"
        case .memories: return "brain.head.profile"
        }
    }

    var toggleKey: ModuleToggleKey? {
        switch self {
        case .tasks, .memories: return nil
        case .calendar: return .calendar
        case .focus: return .focus
        case .notes: return .notes
        }
    }
}

struct MainContentView: View {
    @EnvironmentObject var store: TodoStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var selectedSection: AppSection = .tasks
    @State private var topCaptureText: String = ""

    private var visibleSections: [AppSection] {
        AppSection.allCases.filter { sec in
            guard let key = sec.toggleKey else { return true }
            return store.isModuleEnabled(key)
        }
    }

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            AmbientSceneBackgroundView()

            #if os(iOS)
            TabView(selection: $selectedSection) {
                NavigationStack {
                    iosThemedContainer { UnifiedTodoHostView() }
                        .navigationTitle("待办")
                        .navigationBarTitleDisplayMode(.inline)
                        .toolbar { iosTopToolbar }
                        .iosKeyboardDoneToolbar()
                }
                .tabItem { Label("待办", systemImage: AppSection.tasks.icon) }
                .tag(AppSection.tasks)

                if store.isModuleEnabled(.calendar) {
                    NavigationStack {
                        iosThemedContainer { CalendarPreviewHostView() }
                            .navigationTitle("日历")
                            .navigationBarTitleDisplayMode(.inline)
                            .toolbar { iosTopToolbar }
                            .iosKeyboardDoneToolbar()
                    }
                    .tabItem { Label("日历", systemImage: AppSection.calendar.icon) }
                    .tag(AppSection.calendar)
                }

                if store.isModuleEnabled(.focus) {
                    NavigationStack {
                        iosThemedContainer { FocusCenterView() }
                            .navigationTitle("番茄专注")
                            .navigationBarTitleDisplayMode(.inline)
                            .toolbar { iosTopToolbar }
                            .iosKeyboardDoneToolbar()
                    }
                    .tabItem { Label("专注", systemImage: AppSection.focus.icon) }
                    .tag(AppSection.focus)
                }

                if store.isModuleEnabled(.notes) {
                    NavigationStack {
                        iosThemedContainer { QuickNotesStreamView() }
                            .navigationTitle("随记")
                            .navigationBarTitleDisplayMode(.inline)
                            .toolbar { iosTopToolbar }
                            .iosKeyboardDoneToolbar()
                    }
                    .tabItem { Label("随记", systemImage: AppSection.notes.icon) }
                    .tag(AppSection.notes)
                }

                NavigationStack {
                    iosThemedContainer { MemoriesAndAskHostView() }
                        .navigationTitle("记忆与问答")
                        .navigationBarTitleDisplayMode(.inline)
                        .toolbar { iosTopToolbar }
                        .iosKeyboardDoneToolbar()
                }
                .tabItem { Label("记忆", systemImage: AppSection.memories.icon) }
                .tag(AppSection.memories)
            }
            .tint(store.activeTheme.primaryColor)
            .preferredColorScheme(store.activeTheme.isDark ? .dark : .light)
            .task {
                NotificationManager.shared.refreshAuthorizationStatus()
                await store.refresh()
                store.startAutoSyncLoop()
            }
            .onChange(of: scenePhase) { _, newPhase in
                if newPhase == .active {
                    NotificationManager.shared.refreshAuthorizationStatus()
                    store.startAutoSyncLoop()
                    Task { await store.syncIfChanged() }
                } else if newPhase == .background {
                    store.stopAutoSyncLoop()
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: .NSCalendarDayChanged)) { _ in
                Task { await store.refresh() }
            }
            .onReceive(NotificationCenter.default.publisher(for: .traceTodoOpenOverview)) { _ in
                selectedSection = .tasks
                Task { await store.refresh() }
            }
            .onOpenURL { url in
                handleDeepLink(url)
            }
            #else
            VStack(spacing: 0) {
                // Top Header Bar with Theme Vector Micro-Illustration or Scene Header Art
                ZStack(alignment: .trailing) {
                    if store.hasActiveScene {
                        RemoteSceneWallpaperImage(sceneId: store.activeSceneId, serverURL: store.serverURL)
                            .frame(width: 380, height: 58)
                            .opacity(0.45)
                            .mask(
                                LinearGradient(
                                    colors: [.clear, .black.opacity(0.65), .black],
                                    startPoint: .leading,
                                    endPoint: .trailing
                                )
                            )
                            .allowsHitTesting(false)
                    } else {
                        ThemeHeaderArtworkView(theme: store.activeTheme)
                    }

                    HStack(spacing: 14) {
                        if let appIconImg = NSApp.applicationIconImage {
                            Image(nsImage: appIconImg)
                                .resizable()
                                .scaledToFit()
                                .frame(width: 36, height: 36)
                                .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
                        } else {
                            ZStack {
                                RoundedRectangle(cornerRadius: 10, style: .continuous)
                                    .fill(
                                        LinearGradient(
                                            colors: [store.activeTheme.primaryColor, store.activeTheme.accentColor],
                                            startPoint: .topLeading,
                                            endPoint: .bottomTrailing
                                        )
                                    )
                                    .frame(width: 34, height: 34)
                                Image(systemName: "square.grid.2x2.fill")
                                    .font(.system(size: 15, weight: .bold))
                                    .foregroundStyle(.white)
                            }
                        }

                        Divider().frame(height: 24)

                        HStack(spacing: 8) {
                            Image(systemName: "magnifyingglass")
                                .foregroundStyle(.secondary)
                            TextField("一句话极速录入待办或记忆（支持 🎤 语音听写）...", text: $topCaptureText)
                                .textFieldStyle(.plain)
                                .font(.system(size: 13.5))
                                .onSubmit { submitTopCapture() }

                            VoiceMicButton(text: $topCaptureText, compact: true)

                            let activePending = max(store.pendingIngestCount, store.optimisticIngestItems.count)
                            if activePending > 0 {
                                HStack(spacing: 4) {
                                    ProgressView().controlSize(.mini)
                                    Text("AI 整理中(\(activePending))")
                                        .font(.system(size: 11, weight: .semibold))
                                        .foregroundStyle(store.activeTheme.primaryColor)
                                }
                                .padding(.horizontal, 8)
                                .padding(.vertical, 3)
                                .background(store.activeTheme.primaryColor.opacity(0.10))
                                .clipShape(Capsule())
                            }

                            if store.isBusy {
                                ProgressView().controlSize(.small)
                            } else if !topCaptureText.trimmingCharacters(in: .whitespaces).isEmpty {
                                Button("AI 录入") { submitTopCapture() }
                                    .font(.caption.bold())
                                    .buttonStyle(.borderedProminent)
                                    .tint(store.activeTheme.primaryColor)
                            }
                        }

                        Spacer()

                        Button {
                            store.isPresentingNewTaskSheet = true
                        } label: {
                            Label("新建任务", systemImage: "plus.rectangle.fill")
                                .font(.system(size: 12.5, weight: .semibold))
                                .padding(.horizontal, 14)
                                .padding(.vertical, 7)
                                .background(store.activeTheme.primaryFillColor)
                                .foregroundStyle(.white)
                                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                        }
                        .buttonStyle(.plain)

                        if store.isModuleEnabled(.supervision) {
                            Button {
                                store.isPresentingSupervisionSheet = true
                            } label: {
                                Label("监工", systemImage: "person.2.fill")
                                    .font(.system(size: 12, weight: .medium))
                                    .padding(.horizontal, 10)
                                    .padding(.vertical, 6)
                                    .background(Color.primary.opacity(0.06))
                                    .clipShape(Capsule())
                            }
                            .buttonStyle(.plain)
                        }

                        Button {
                            store.isPresentingSettingsSheet = true
                        } label: {
                            Label("外观/设置", systemImage: "paintpalette.fill")
                                .font(.system(size: 12, weight: .medium))
                                .padding(.horizontal, 10)
                                .padding(.vertical, 6)
                                .background(Color.primary.opacity(0.06))
                                .clipShape(Capsule())
                        }
                        .buttonStyle(.plain)

                        // Overdue / Status Bell Indicator
                        ZStack(alignment: .topTrailing) {
                            Button {
                                selectedSection = .tasks
                                Task { await store.refresh() }
                            } label: {
                                Image(systemName: "bell")
                                    .font(.system(size: 16))
                                    .foregroundStyle(.secondary)
                            }
                            .buttonStyle(.plain)
                            .contentShape(Rectangle())
                            .help("刷新与查看逾期/顺延提醒")

                            let alertCnt = (store.matrix?.summaryCounts.overdue ?? 0) + store.q1Count
                            if alertCnt > 0 {
                                Text("\(alertCnt)")
                                    .font(.system(size: 9, weight: .bold))
                                    .foregroundStyle(.white)
                                    .padding(.horizontal, 4)
                                    .padding(.vertical, 1)
                                    .background(store.activeTheme.primaryColor)
                                    .clipShape(Capsule())
                                    .offset(x: 6, y: -5)
                            }
                        }

                        Button {
                            store.openAuthSheetFromAnywhere()
                        } label: {
                            if let user = store.currentUser, store.isAuthenticated {
                                HStack(spacing: 6) {
                                    ZStack(alignment: .bottomTrailing) {
                                        Image(systemName: "person.crop.circle.fill")
                                            .font(.system(size: 15, weight: .semibold))
                                            .foregroundStyle(store.activeTheme.primaryColor)
                                        Circle()
                                            .fill(store.isOnline ? Color.green : Color.orange)
                                            .frame(width: 6.5, height: 6.5)
                                    }
                                    Text(user.displayName)
                                        .font(.system(size: 12.5, weight: .semibold))
                                        .lineLimit(1)
                                    Image(systemName: "chevron.down")
                                        .font(.system(size: 9, weight: .bold))
                                        .foregroundStyle(.secondary)
                                }
                                .padding(.horizontal, 11)
                                .padding(.vertical, 6)
                                .background(store.activeTheme.primaryColor.opacity(0.10))
                                .overlay(
                                    Capsule().stroke(store.activeTheme.primaryColor.opacity(0.24), lineWidth: 1)
                                )
                                .clipShape(Capsule())
                            } else {
                                HStack(spacing: 5) {
                                    Image(systemName: "person.crop.circle.badge.plus")
                                        .font(.system(size: 13.5, weight: .bold))
                                    Text("登录 / 注册")
                                        .font(.system(size: 12.5, weight: .bold))
                                }
                                .padding(.horizontal, 12)
                                .padding(.vertical, 6)
                                .background(store.activeTheme.primaryFillColor)
                                .foregroundStyle(.white)
                                .clipShape(Capsule())
                            }
                        }
                        .buttonStyle(.plain)
                        .help(store.isAuthenticated ? "查看我的账号、配额、切换账号或退出登录 (⌘⇧L)" : "登录或注册云端账号 (⌘⇧L)")

                        Button {
                            Task { await store.refresh() }
                        } label: {
                            Image(systemName: "arrow.clockwise")
                                .font(.system(size: 14))
                                .foregroundStyle(.secondary)
                        }
                        .frame(width: 30, height: 30)
                        .contentShape(Rectangle())
                        .buttonStyle(.plain)
                    }
                    .padding(.horizontal, 18)
                }
                .frame(height: 58)
                .background(store.cardSurfaceBackground)
                .overlay(Rectangle().fill(Color.primary.opacity(0.08)).frame(height: 1), alignment: .bottom)

                // Main 2-Column Body: Converged 5-Tab Left Rail + Full-Width Center Canvas
                HStack(spacing: 0) {
                    leftFloatingRail
                        .padding(.vertical, 16)
                        .padding(.leading, 16)

                    Group {
                        switch selectedSection {
                        case .tasks:
                            UnifiedTodoHostView()
                        case .calendar:
                            CalendarPreviewHostView()
                        case .focus:
                            FocusCenterView()
                        case .notes:
                            QuickNotesStreamView()
                        case .memories:
                            MemoriesAndAskHostView()
                        }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
                .background(Color.clear)
            }
            .preferredColorScheme(store.activeTheme.isDark ? .dark : .light)
            .frame(minWidth: 920, minHeight: 660)
            .task {
                NotificationManager.shared.refreshAuthorizationStatus()
                await store.refresh()
                store.startAutoSyncLoop()
            }
            .onChange(of: scenePhase) { _, newPhase in
                if newPhase == .active {
                    NotificationManager.shared.refreshAuthorizationStatus()
                    store.startAutoSyncLoop()
                    Task { await store.syncIfChanged() }
                } else if newPhase == .background {
                    store.stopAutoSyncLoop()
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: .NSCalendarDayChanged)) { _ in
                Task { await store.refresh() }
            }
            .onReceive(NotificationCenter.default.publisher(for: .traceTodoOpenOverview)) { _ in
                selectedSection = .tasks
                Task { await store.refresh() }
            }
            .onOpenURL { url in
                handleDeepLink(url)
            }
            #endif

            // Global Floating "💬 问记忆" Copilot Button + Active Focus Mini Capsule
            if !store.needsLoginGate && !store.isFocusLockPresented {
                HStack(spacing: 10) {
                    if let fSess = store.activeFocusSession {
                        Button {
                            store.enterFocusLockScreen()
                        } label: {
                            HStack(spacing: 6) {
                                Image(systemName: fSess.isPaused ? "pause.circle.fill" : "timer")
                                    .font(.system(size: 13.5, weight: .bold))
                                Text("\(formatFocusClockString(fSess.displaySeconds)) · 🔒 沉浸锁屏")
                                    .font(.system(size: 12.5, weight: .bold, design: .rounded))
                                    .monospacedDigit()
                            }
                            .padding(.horizontal, 13)
                            .padding(.vertical, 10)
                            .background(Color(red: 0.10, green: 0.12, blue: 0.20).opacity(0.92))
                            .foregroundStyle(.white)
                            .clipShape(Capsule())
                            .overlay(Capsule().stroke(store.activeTheme.accentColor.opacity(0.65), lineWidth: 1.2))
                            .shadow(color: Color.black.opacity(0.24), radius: 10, x: 0, y: 4)
                        }
                        .buttonStyle(.plain)
                        .help("点击重新进入全屏沉浸专注锁屏")
                    }

                    Button {
                        store.isPresentingFloatingAsk = true
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "bubble.left.and.bubble.right.fill")
                                .font(.system(size: 13.5, weight: .bold))
                            Text("问记忆")
                                .font(.system(size: 13, weight: .bold))
                            #if os(macOS)
                            Text("⌘K")
                                .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                                .padding(.horizontal, 5)
                                .padding(.vertical, 1.5)
                                .background(Color.white.opacity(0.20))
                                .clipShape(RoundedRectangle(cornerRadius: 4))
                            #endif
                        }
                        .padding(.horizontal, 15)
                        .padding(.vertical, 10)
                        .background(
                            LinearGradient(
                                colors: [store.activeTheme.primaryFillColor, store.activeTheme.accentColor],
                                startPoint: .topLeading,
                                endPoint: .bottomTrailing
                            )
                        )
                        .foregroundStyle(.white)
                        .clipShape(Capsule())
                        .shadow(color: Color.black.opacity(0.22), radius: 12, x: 0, y: 5)
                    }
                    .buttonStyle(.plain)
                    #if os(macOS)
                    .keyboardShortcut("k", modifiers: .command)
                    #endif
                }
                #if os(macOS)
                .padding(.trailing, 24)
                .padding(.bottom, 20)
                #else
                .padding(.trailing, 18)
                .padding(.bottom, 64)
                #endif
                .zIndex(60)
            }

            if store.isFocusLockPresented && store.activeFocusSession != nil {
                FocusLockView()
                    .environmentObject(store)
                    .transition(.opacity)
                    .zIndex(100)
            }
        }
        .onChange(of: store.enabledModules) { _, _ in
            if let key = selectedSection.toggleKey, !store.isModuleEnabled(key) {
                selectedSection = .tasks
            }
        }
        .sheet(isPresented: $store.isPresentingNewTaskSheet) {
            TaskEditorSheet(existingTask: nil)
                .environmentObject(store)
        }
        .sheet(item: $store.editingTaskTarget) { target in
            TaskEditorSheet(existingTask: target)
                .environmentObject(store)
        }
        .sheet(isPresented: $store.isPresentingSupervisionSheet) {
            NavigationStack {
                SupervisionBoardView()
                    .environmentObject(store)
                    .navigationTitle("好友监工小队")
                    .toolbar {
                        ToolbarItem(placement: .cancellationAction) {
                            Button("关闭") { store.isPresentingSupervisionSheet = false }
                        }
                    }
            }
            #if os(macOS)
            .frame(minWidth: 680, minHeight: 540)
            #endif
        }
        .sheet(isPresented: $store.isPresentingSettingsSheet) {
            NavigationStack {
                SettingsSyncView()
                    .environmentObject(store)
                    .navigationTitle("外观主题与设置")
                    .toolbar {
                        ToolbarItem(placement: .cancellationAction) {
                            Button("完成") { store.isPresentingSettingsSheet = false }
                        }
                    }
            }
            #if os(macOS)
            .frame(minWidth: 620, minHeight: 580)
            #endif
        }
        .sheet(isPresented: $store.isPresentingFloatingAsk) {
            NavigationStack {
                AskMemoryView()
                    .environmentObject(store)
                    .navigationTitle("💬 随手问记忆（证据溯源）")
                    .toolbar {
                        ToolbarItem(placement: .cancellationAction) {
                            Button("关闭") { store.isPresentingFloatingAsk = false }
                        }
                    }
            }
            #if os(iOS)
            .presentationDetents([.medium, .large])
            #else
            .frame(minWidth: 660, minHeight: 540)
            #endif
        }
        .sheet(isPresented: Binding(
            get: { store.isPresentingAuthSheet || store.needsLoginGate },
            set: { newVal in
                if !store.needsLoginGate {
                    store.isPresentingAuthSheet = newVal
                }
            }
        )) {
            AuthGateSheet()
                .environmentObject(store)
                .interactiveDismissDisabled(store.needsLoginGate)
        }
    }

    #if os(macOS)
    private var leftFloatingRail: some View {
        VStack(spacing: 6) {
            ForEach(visibleSections) { sec in
                let isSel = selectedSection == sec
                Button {
                    selectedSection = sec
                } label: {
                    VStack(spacing: 5) {
                        ZStack {
                            if isSel {
                                Circle()
                                    .fill(store.activeTheme.primaryFillColor)
                                    .frame(width: 34, height: 34)
                            }
                            Image(systemName: sec.icon)
                                .font(.system(size: 14.5, weight: .semibold))
                                .foregroundStyle(isSel ? .white : .secondary)
                        }
                        .frame(height: 34)

                        Text(sec.rawValue)
                            .font(.system(size: 11, weight: isSel ? .bold : .medium))
                            .foregroundStyle(isSel ? .primary : .secondary)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 8)
                    .background(isSel ? store.activeTheme.railSelectedBg : Color.clear)
                    .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .contentShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                }
                .buttonStyle(.plain)
            }

            Spacer(minLength: 8)

            Divider()
                .padding(.horizontal, 8)
                .padding(.bottom, 2)

            Button {
                store.openAuthSheetFromAnywhere()
            } label: {
                VStack(spacing: 4) {
                    ZStack {
                        Circle()
                            .fill(
                                store.isAuthenticated
                                    ? store.activeTheme.primaryColor.opacity(0.14)
                                    : store.activeTheme.primaryFillColor
                            )
                            .frame(width: 34, height: 34)
                        if let user = store.currentUser, store.isAuthenticated {
                            Text(String(user.displayName.prefix(1)).uppercased())
                                .font(.system(size: 14, weight: .bold, design: .rounded))
                                .foregroundStyle(store.activeTheme.primaryColor)
                        } else {
                            Image(systemName: "person.crop.circle.badge.plus")
                                .font(.system(size: 14.5, weight: .bold))
                                .foregroundStyle(.white)
                        }
                    }
                    Text(store.isAuthenticated ? (store.currentUser?.displayName ?? "账号") : "登录")
                        .font(.system(size: 10.5, weight: .bold))
                        .foregroundStyle(store.isAuthenticated ? Color.primary : store.activeTheme.primaryColor)
                        .lineLimit(1)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 7)
                .background(store.isAuthenticated ? Color.clear : store.activeTheme.primaryColor.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                .contentShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            }
            .buttonStyle(.plain)
            .help(store.isAuthenticated ? "打开我的账号中心" : "点击登录或注册账号")
        }
        .padding(.vertical, 12)
        .padding(.horizontal, 6)
        .frame(width: 96)
        .background(store.cardSurfaceBackground)
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(Color.primary.opacity(0.08), lineWidth: 1)
        )
    }

    private func submitTopCapture() {
        let txt = topCaptureText
        topCaptureText = ""
        Task { await store.submitQuickNote(txt) }
    }
    #endif

    #if os(iOS)
    @ViewBuilder
    private func iosThemedContainer<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        ZStack(alignment: .topTrailing) {
            AmbientSceneBackgroundView()

            if !store.hasActiveScene {
                ThemeHeaderArtworkView(theme: store.activeTheme)
                    .opacity(0.85)
                    .ignoresSafeArea(edges: .top)
            }

            content()
                .scrollContentBackground(.hidden)
        }
        .toolbarBackground(
            store.activeTheme.surfaceBackground.opacity(store.hasActiveScene ? 0.78 : 0.90),
            for: .navigationBar
        )
        .toolbarBackground(.visible, for: .navigationBar)
    }
    #endif

    private func handleDeepLink(_ url: URL) {
        guard url.scheme == "tracetodo" else { return }
        let host = url.host ?? ""
        if host == "new" {
            store.isPresentingNewTaskSheet = true
        } else if host == "login" || host == "auth" || host == "account" {
            store.openAuthSheetFromAnywhere()
            return
        } else if host == "complete" {
            let comps = URLComponents(url: url, resolvingAgainstBaseURL: false)
            let memId = comps?.queryItems?.first(where: { $0.name == "id" })?.value ?? ""
            let ver = Int(comps?.queryItems?.first(where: { $0.name == "v" })?.value ?? "1") ?? 1
            if !memId.isEmpty {
                Task {
                    _ = try? await store.client.transitionTask(
                        memoryId: memId,
                        action: "complete_task",
                        expectedVersion: ver
                    )
                    await store.refresh()
                }
                return
            }
        } else if host == "refresh" {
            Task { await store.refresh() }
            return
        } else if host == "memories" {
            selectedSection = .memories
        } else if host == "notes" || host == "memo" || host == "journal" {
            selectedSection = .notes
        } else if host == "supervision" {
            store.isPresentingSupervisionSheet = true
        } else if host == "focus" {
            selectedSection = .focus
        } else if host == "calendar" || host == "countdown" {
            selectedSection = .calendar
        } else {
            selectedSection = .tasks
        }
        Task { await store.refresh() }
    }

    @ToolbarContentBuilder
    private var iosTopToolbar: some ToolbarContent {
        #if os(iOS)
        ToolbarItem(placement: .topBarLeading) {
            Button {
                store.openAuthSheetFromAnywhere()
            } label: {
                if let user = store.currentUser, store.isAuthenticated {
                    HStack(spacing: 4) {
                        Image(systemName: "person.crop.circle.fill")
                            .font(.system(size: 14, weight: .semibold))
                        Text(user.displayName)
                            .font(.system(size: 12.5, weight: .semibold))
                            .lineLimit(1)
                    }
                } else {
                    HStack(spacing: 4) {
                        Image(systemName: "person.crop.circle.badge.plus")
                            .font(.system(size: 13.5, weight: .bold))
                        Text("登录")
                            .font(.system(size: 12.5, weight: .bold))
                    }
                    .padding(.horizontal, 9)
                    .padding(.vertical, 4)
                    .background(store.activeTheme.primaryFillColor)
                    .foregroundStyle(.white)
                    .clipShape(Capsule())
                }
            }
        }
        #endif

        ToolbarItemGroup(placement: .automatic) {
            Button {
                store.isPresentingNewTaskSheet = true
            } label: {
                Image(systemName: "plus")
            }
            if store.isModuleEnabled(.supervision) {
                Button {
                    store.isPresentingSupervisionSheet = true
                } label: {
                    Image(systemName: "person.2")
                }
            }
            Button {
                store.isPresentingSettingsSheet = true
            } label: {
                Image(systemName: "paintpalette")
            }
            Button {
                Task { await store.refresh() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
        }
    }
}

#if os(macOS)
struct MenuBarQuickView: View {
    @EnvironmentObject var store: TodoStore
    @Environment(\.openWindow) private var openWindow
    @State private var quickText: String = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("🧠 有迹 快捷待办")
                    .font(.headline)
                Spacer()
                Button {
                    NSApp.activate(ignoringOtherApps: true)
                    openWindow(id: "main")
                    store.openAuthSheetFromAnywhere()
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: store.isAuthenticated ? "person.crop.circle.fill" : "person.crop.circle.badge.plus")
                        Text(store.isAuthenticated ? (store.currentUser?.displayName ?? "账号") : "登录")
                    }
                    .font(.caption.bold())
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }

            HStack(spacing: 6) {
                TextField("一句话记待办，回车即抽...", text: $quickText)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit {
                        let txt = quickText
                        quickText = ""
                        Task { await store.submitQuickNote(txt) }
                    }
                Button("添加") {
                    let txt = quickText
                    quickText = ""
                    Task { await store.submitQuickNote(txt) }
                }
                .disabled(quickText.trimmingCharacters(in: .whitespaces).isEmpty)
            }

            Divider()

            ForEach(store.optimisticIngestItems.prefix(2)) { opt in
                HStack(spacing: 8) {
                    ProgressView().controlSize(.mini)
                    Text(opt.text)
                        .font(.system(size: 12, weight: .medium))
                        .lineLimit(1)
                    Spacer()
                    Text("解析中")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(.purple)
                }
                .padding(.vertical, 2)
            }

            let topTasks = Array((store.matrix?.sortedTasks ?? []).filter { !$0.payload.isDone }.prefix(5))
            if topTasks.isEmpty {
                Text("当前全部待办已清空 🎉")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 8)
            } else {
                ForEach(topTasks) { t in
                    HStack(spacing: 8) {
                        Button {
                            Task { await store.transitionTask(t, action: "complete_task") }
                        } label: {
                            Image(systemName: "circle")
                                .foregroundStyle(t.payload.themeKind.accentColor)
                        }
                        .buttonStyle(.plain)

                        Text(t.title)
                            .font(.system(size: 12.5))
                            .lineLimit(1)
                        Spacer()
                        Text(t.payload.quadrantKind.shortCode)
                            .font(.system(size: 10, weight: .bold))
                            .foregroundStyle(t.payload.quadrantKind.tintColor)
                    }
                }
            }

            Divider()

            HStack {
                Button("+ 新建彩色卡片") {
                    NSApp.activate(ignoringOtherApps: true)
                    openWindow(id: "main")
                    store.isPresentingNewTaskSheet = true
                }
                Button("打开主窗口") {
                    NSApp.activate(ignoringOtherApps: true)
                    openWindow(id: "main")
                }
                Spacer()
                Button("刷新") {
                    Task { await store.refresh() }
                }
                Button("退出") {
                    NSApp.terminate(nil)
                }
            }
            .font(.caption)
        }
        .padding(14)
        .frame(width: 360)
    }
}
#endif

@main
struct TraceTodoApp: App {
    @StateObject private var store = TodoStore()

    init() {
        NotificationManager.shared.configureDelegate()
        #if os(macOS)
        NSApplication.shared.setActivationPolicy(.regular)
        #endif
    }

    var body: some Scene {
        WindowGroup("有迹", id: "main") {
            MainContentView()
                .environmentObject(store)
        }
        #if os(macOS)
        .commands {
            CommandMenu("账号") {
                Button(store.isAuthenticated ? "我的账号与配额 (\(store.currentUser?.displayName ?? ""))..." : "登录 / 注册账号...") {
                    store.openAuthSheetFromAnywhere()
                }
                .keyboardShortcut("l", modifiers: [.command, .shift])

                Button("外观主题与设置...") {
                    store.isPresentingSettingsSheet = true
                }

                if store.isAuthenticated {
                    Divider()
                    Button("退出当前账号") {
                        Task { await store.logout() }
                    }
                }
            }
        }
        #endif

        #if os(macOS)
        MenuBarExtra {
            MenuBarQuickView()
                .environmentObject(store)
        } label: {
            HStack(spacing: 3) {
                Image(systemName: store.q1Count > 0 ? "flame.fill" : "checkmark.circle")
                Text("\(store.totalTodoCount)")
            }
        }
        .menuBarExtraStyle(.window)
        #endif
    }
}
