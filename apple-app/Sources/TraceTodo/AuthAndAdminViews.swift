import SwiftUI
#if os(macOS)
import AppKit
#elseif os(iOS)
import UIKit
#endif

struct AuthGateSheet: View {
    @EnvironmentObject var store: TodoStore
    @Environment(\.dismiss) private var dismiss

    private enum FocusField: Hashable {
        case username
        case displayName
        case password
        case captchaCode
        case serverURL
    }

    @FocusState private var focusedField: FocusField?
    @State private var isRegisterMode: Bool = false
    @State private var username: String = ""
    @State private var displayName: String = ""
    @State private var password: String = ""
    @State private var captchaId: String = ""
    @State private var captchaCode: String = ""
    @State private var captchaPngData: Data? = nil
    @State private var isLoadingCaptcha: Bool = false
    @State private var showPassword: Bool = false
    @State private var showServerConfig: Bool = false
    @State private var forceSwitchAccount: Bool = false
    @State private var showInlineAdmin: Bool = false
    @State private var copiedHint: String?
    @State private var errorMsg: String?
    @State private var isSubmitting: Bool = false

    private var canDismiss: Bool {
        !store.needsLoginGate
    }

    private var canSubmit: Bool {
        !isSubmitting
            && !username.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && password.count >= 1
            && (!isRegisterMode || !captchaCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }

    private var shortServerLabel: String {
        store.serverURL
            .replacingOccurrences(of: "https://", with: "")
            .replacingOccurrences(of: "http://", with: "")
    }

    var body: some View {
        ZStack(alignment: .topTrailing) {
            LinearGradient(
                colors: [
                    Color(red: 0.94, green: 0.95, blue: 1.00),
                    Color(red: 0.98, green: 0.98, blue: 1.00),
                    Color.white
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            .ignoresSafeArea()

            Circle()
                .fill(
                    RadialGradient(
                        colors: [Color(red: 0.45, green: 0.36, blue: 0.98).opacity(0.14), .clear],
                        center: .center,
                        startRadius: 10,
                        endRadius: 160
                    )
                )
                .frame(width: 280, height: 280)
                .offset(x: 90, y: -90)
                .allowsHitTesting(false)

            ScrollView(showsIndicators: false) {
                if store.isAuthenticated && !forceSwitchAccount, let user = store.currentUser {
                    authenticatedProfileView(user: user)
                        .padding(.horizontal, 26)
                        .padding(.vertical, 26)
                } else {
                    guestAuthFormView
                        .padding(.horizontal, 28)
                        .padding(.top, 28)
                        .padding(.bottom, 22)
                }
            }
            .iosScrollDismissesKeyboard()

            if canDismiss || (store.isAuthenticated && forceSwitchAccount) {
                Button {
                    if store.isAuthenticated && forceSwitchAccount {
                        withAnimation(.spring(response: 0.28, dampingFraction: 0.85)) {
                            forceSwitchAccount = false
                        }
                    } else {
                        store.isPresentingAuthSheet = false
                        dismiss()
                    }
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(Color(red: 0.38, green: 0.43, blue: 0.52))
                        .frame(width: 28, height: 28)
                        .background(Color.black.opacity(0.05))
                        .clipShape(Circle())
                }
                .buttonStyle(.plain)
                .padding(16)
            }
        }
        .preferredColorScheme(.light)
        #if os(macOS)
        .frame(width: 420)
        .fixedSize(horizontal: false, vertical: true)
        #endif
        .iosKeyboardDoneToolbar()
        .sheet(isPresented: $showInlineAdmin) {
            NavigationStack {
                AdminConsoleView()
                    .environmentObject(store)
                    .toolbar {
                        ToolbarItem(placement: .cancellationAction) {
                            Button("完成") { showInlineAdmin = false }
                        }
                    }
            }
            #if os(macOS)
            .frame(minWidth: 600, minHeight: 560)
            #endif
        }
        .onAppear {
            if !store.hasUsers {
                isRegisterMode = true
            }
            if isRegisterMode {
                refreshCaptcha()
            }
        }
    }

    // MARK: - Guest Login / Register Card

    private var guestAuthFormView: some View {
        VStack(spacing: 18) {
            // Brand Hero
            VStack(spacing: 10) {
                appBrandIcon
                    .shadow(color: Color(red: 0.31, green: 0.36, blue: 0.92).opacity(0.24), radius: 14, x: 0, y: 8)

                Text(isRegisterMode ? "创建你的 有迹 账号" : "欢迎回到 有迹")
                    .font(.system(size: 21, weight: .bold, design: .rounded))
                    .foregroundStyle(Color(red: 0.08, green: 0.11, blue: 0.18))

                Text("AI 长期记忆驱动的四象限待办与执行中心")
                    .font(.system(size: 12.5, weight: .medium))
                    .foregroundStyle(Color(red: 0.42, green: 0.47, blue: 0.56))
            }
            .padding(.top, 2)

            if !store.hasUsers {
                HStack(spacing: 8) {
                    Text("🌱")
                    Text("首次部署：第一个注册的账号将自动成为管理员并接管默认空间数据")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Color(red: 0.14, green: 0.36, blue: 0.82))
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 9)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color(red: 0.91, green: 0.95, blue: 1.00))
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            }

            // Custom Mode Switcher Pill
            if store.allowRegistration || !store.hasUsers {
                HStack(spacing: 4) {
                    modeTabButton(title: "登录账号", active: !isRegisterMode) {
                        withAnimation(.spring(response: 0.25, dampingFraction: 0.82)) {
                            isRegisterMode = false
                            errorMsg = nil
                        }
                    }
                    modeTabButton(title: "注册新账号", active: isRegisterMode) {
                        withAnimation(.spring(response: 0.25, dampingFraction: 0.82)) {
                            isRegisterMode = true
                            errorMsg = nil
                        }
                        refreshCaptcha()
                    }
                }
                .padding(4)
                .background(Color(red: 0.92, green: 0.94, blue: 0.98))
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            }

            // Input Fields Stack
            VStack(spacing: 12) {
                inputFieldCard(
                    icon: "person.fill",
                    isFocused: focusedField == .username
                ) {
                    TextField("用户名（3-32位字母 / 数字 / 中文）", text: $username)
                        .textFieldStyle(.plain)
                        .font(.system(size: 14))
                        .foregroundStyle(Color(red: 0.09, green: 0.12, blue: 0.20))
                        .focused($focusedField, equals: .username)
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        #endif
                }

                if isRegisterMode {
                    inputFieldCard(
                        icon: "sparkles",
                        isFocused: focusedField == .displayName
                    ) {
                        TextField("显示昵称（选填，如 Xander）", text: $displayName)
                            .textFieldStyle(.plain)
                            .font(.system(size: 14))
                            .foregroundStyle(Color(red: 0.09, green: 0.12, blue: 0.20))
                            .focused($focusedField, equals: .displayName)
                    }
                    .transition(.opacity.combined(with: .move(edge: .top)))
                }

                inputFieldCard(
                    icon: "lock.fill",
                    isFocused: focusedField == .password
                ) {
                    HStack(spacing: 8) {
                        Group {
                            if showPassword {
                                TextField("密码（至少 6 位）", text: $password)
                            } else {
                                SecureField("密码（至少 6 位）", text: $password)
                            }
                        }
                        .textFieldStyle(.plain)
                        .font(.system(size: 14))
                        .foregroundStyle(Color(red: 0.09, green: 0.12, blue: 0.20))
                        .focused($focusedField, equals: .password)
                        .submitLabel(.go)
                        .onSubmit {
                            if canSubmit { submit() }
                        }

                        Button {
                            showPassword.toggle()
                        } label: {
                            Image(systemName: showPassword ? "eye.slash.fill" : "eye.fill")
                                .font(.system(size: 13))
                                .foregroundStyle(Color(red: 0.55, green: 0.60, blue: 0.70))
                        }
                        .buttonStyle(.plain)
                    }
                }

                if isRegisterMode {
                    HStack(spacing: 8) {
                        inputFieldCard(
                            icon: "checkmark.shield.fill",
                            isFocused: focusedField == .captchaCode
                        ) {
                            TextField("输入右侧 4 位验证码", text: $captchaCode)
                                .textFieldStyle(.plain)
                                .font(.system(size: 14, weight: .bold, design: .monospaced))
                                .foregroundStyle(Color(red: 0.09, green: 0.12, blue: 0.20))
                                .focused($focusedField, equals: .captchaCode)
                                .autocorrectionDisabled()
                                #if os(iOS)
                                .textInputAutocapitalization(.characters)
                                #endif
                                .submitLabel(.go)
                                .onSubmit {
                                    if canSubmit { submit() }
                                }
                        }

                        Button {
                            refreshCaptcha()
                        } label: {
                            ZStack {
                                RoundedRectangle(cornerRadius: 12, style: .continuous)
                                    .fill(Color(red: 0.93, green: 0.95, blue: 1.00))

                                if let data = captchaPngData, let img = captchaSwiftUIImage(from: data) {
                                    img
                                        .resizable()
                                        .interpolation(.none)
                                        .scaledToFill()
                                        .frame(width: 128, height: 46)
                                        .clipped()
                                } else if isLoadingCaptcha {
                                    ProgressView()
                                        .controlSize(.small)
                                } else {
                                    HStack(spacing: 4) {
                                        Image(systemName: "arrow.clockwise")
                                            .font(.system(size: 11, weight: .bold))
                                        Text("加载验证码")
                                            .font(.system(size: 11.5, weight: .semibold))
                                    }
                                    .foregroundStyle(Color(red: 0.25, green: 0.44, blue: 0.92))
                                }
                            }
                            .frame(width: 128, height: 46)
                            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                            .overlay(
                                RoundedRectangle(cornerRadius: 12, style: .continuous)
                                    .stroke(Color(red: 0.80, green: 0.84, blue: 0.96), lineWidth: 1)
                            )
                        }
                        .buttonStyle(.plain)
                        .help("看不清？点击刷新验证码")
                    }
                    .transition(.opacity.combined(with: .move(edge: .top)))
                }
            }

            if let errorMsg, !errorMsg.isEmpty {
                HStack(spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 12.5))
                    Text(errorMsg)
                        .font(.system(size: 12.5, weight: .medium))
                        .fixedSize(horizontal: false, vertical: true)
                }
                .foregroundStyle(Color(red: 0.82, green: 0.16, blue: 0.18))
                .padding(.horizontal, 12)
                .padding(.vertical, 9)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color(red: 1.00, green: 0.94, blue: 0.94))
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            }

            // Primary Gradient CTA Button
            Button {
                submit()
            } label: {
                HStack(spacing: 8) {
                    if isSubmitting {
                        ProgressView()
                            .controlSize(.small)
                            .tint(.white)
                    } else {
                        Image(systemName: isRegisterMode ? "person.badge.plus" : "arrow.right.circle.fill")
                            .font(.system(size: 15, weight: .semibold))
                    }
                    Text(isRegisterMode ? "注册并进入空间" : "立即登录")
                        .font(.system(size: 15, weight: .bold))
                }
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity)
                .frame(height: 46)
                .background(
                    LinearGradient(
                        colors: canSubmit
                            ? [Color(red: 0.22, green: 0.48, blue: 0.98), Color(red: 0.49, green: 0.30, blue: 0.96)]
                            : [Color(red: 0.72, green: 0.77, blue: 0.86), Color(red: 0.68, green: 0.73, blue: 0.83)],
                        startPoint: .leading,
                        endPoint: .trailing
                    )
                )
                .clipShape(RoundedRectangle(cornerRadius: 13, style: .continuous))
                .shadow(
                    color: canSubmit ? Color(red: 0.30, green: 0.40, blue: 0.96).opacity(0.28) : .clear,
                    radius: 10,
                    x: 0,
                    y: 5
                )
            }
            .buttonStyle(.plain)
            .disabled(!canSubmit)

            // Subtle Collapsible Server Footer
            VStack(spacing: 10) {
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        showServerConfig.toggle()
                    }
                } label: {
                    HStack(spacing: 6) {
                        Circle()
                            .fill(Color(red: 0.16, green: 0.72, blue: 0.48))
                            .frame(width: 6.5, height: 6.5)
                        Text("连接服务器：\(shortServerLabel)")
                            .lineLimit(1)
                        Image(systemName: showServerConfig ? "chevron.up" : "chevron.down")
                            .font(.system(size: 10, weight: .bold))
                    }
                    .font(.system(size: 11.5, weight: .medium))
                    .foregroundStyle(Color(red: 0.45, green: 0.50, blue: 0.60))
                    .padding(.horizontal, 12)
                    .padding(.vertical, 5)
                    .background(Color(red: 0.95, green: 0.96, blue: 0.99))
                    .clipShape(Capsule())
                }
                .buttonStyle(.plain)

                if showServerConfig {
                    VStack(alignment: .leading, spacing: 8) {
                        inputFieldCard(
                            icon: "network",
                            isFocused: focusedField == .serverURL
                        ) {
                            TextField("http://127.0.0.1:18090", text: $store.serverURL)
                                .textFieldStyle(.plain)
                                .font(.system(size: 12.5, design: .monospaced))
                                .foregroundStyle(Color(red: 0.15, green: 0.20, blue: 0.28))
                                .focused($focusedField, equals: .serverURL)
                                .autocorrectionDisabled()
                                #if os(iOS)
                                .textInputAutocapitalization(.never)
                                .keyboardType(.URL)
                                #endif
                        }

                        HStack(spacing: 8) {
                            serverPresetChip("🧪 云端测试服", url: TodoStore.testCloudServerURL)
                            serverPresetChip("☁️ 线上正式服", url: TodoStore.prodCloudServerURL)
                            serverPresetChip("💻 本地环境", url: TodoStore.localServerURL)
                            Spacer()
                        }
                    }
                    .transition(.opacity.combined(with: .move(edge: .bottom)))
                }
            }
            .padding(.top, 2)
        }
    }

    // MARK: - Authenticated Profile & Quota Card

    private func authenticatedProfileView(user: UserAccountModel) -> some View {
        VStack(spacing: 16) {
            HStack(spacing: 14) {
                ZStack {
                    Circle()
                        .fill(
                            LinearGradient(
                                colors: [Color(red: 0.24, green: 0.50, blue: 0.98), Color(red: 0.52, green: 0.33, blue: 0.97)],
                                startPoint: .topLeading,
                                endPoint: .bottomTrailing
                            )
                        )
                        .frame(width: 52, height: 52)
                    Text(String((user.displayName.isEmpty ? user.username : user.displayName).prefix(1)).uppercased())
                        .font(.system(size: 22, weight: .bold, design: .rounded))
                        .foregroundStyle(.white)
                }

                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 6) {
                        Text(user.displayName)
                            .font(.system(size: 18, weight: .bold))
                            .foregroundStyle(Color(red: 0.08, green: 0.11, blue: 0.18))
                        Text(user.isAdmin ? "👑 管理员" : "👤 成员")
                            .font(.system(size: 11, weight: .bold))
                            .padding(.horizontal, 8)
                            .padding(.vertical, 3)
                            .background(Color(red: 0.93, green: 0.90, blue: 1.00))
                            .foregroundStyle(Color(red: 0.42, green: 0.22, blue: 0.85))
                            .clipShape(Capsule())
                    }
                    Text("@\(user.username) · 空间 \(user.spaceId)")
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundStyle(Color(red: 0.45, green: 0.50, blue: 0.60))
                }
                Spacer()
            }
            .padding(.top, 4)

            if let q = store.currentQuota {
                HStack(spacing: 10) {
                    quotaStatTile(title: "今日录入", value: "\(q.notesCount)/\(user.dailyNoteLimit)", tint: .blue)
                    quotaStatTile(title: "AI 调用", value: "\(q.llmCalls)/\(user.dailyLlmLimit)", tint: .purple)
                    quotaStatTile(title: "🛡️ 防线省费", value: "\(q.blockedNoiseCount) 次", tint: .green)
                }
            }

            if let copiedHint {
                Text(copiedHint)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Color(red: 0.08, green: 0.55, blue: 0.34))
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .frame(maxWidth: .infinity)
                    .background(Color(red: 0.90, green: 0.98, blue: 0.93))
                    .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
            }

            VStack(spacing: 8) {
                profileActionRow(icon: "doc.on.doc.fill", title: "复制小组件 / 客户端 Bearer Token", tint: Color(red: 0.20, green: 0.48, blue: 0.96)) {
                    copyString(store.authToken)
                    copiedHint = "✅ 已复制 Bearer Token"
                }

                profileActionRow(icon: "terminal.fill", title: "复制电脑端 MCP 云端桥接命令", tint: Color(red: 0.46, green: 0.30, blue: 0.92)) {
                    let cleanURL = store.serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
                    copyString("python3 -m trace_memory mcp --remote \(cleanURL) --token \(store.authToken)")
                    copiedHint = "✅ 已复制云端 MCP 桥接命令"
                }

                if user.isAdmin {
                    profileActionRow(icon: "shield.lefthalf.filled", title: "打开管理员后台与低成本防线配置", tint: Color(red: 0.09, green: 0.58, blue: 0.42)) {
                        showInlineAdmin = true
                    }
                }
            }

            HStack(spacing: 10) {
                Button {
                    withAnimation(.spring(response: 0.26, dampingFraction: 0.84)) {
                        forceSwitchAccount = true
                    }
                } label: {
                    Text("切换账号")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Color(red: 0.30, green: 0.36, blue: 0.46))
                        .frame(maxWidth: .infinity)
                        .frame(height: 38)
                        .background(Color(red: 0.94, green: 0.95, blue: 0.98))
                        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                }
                .buttonStyle(.plain)

                Button {
                    Task {
                        await store.logout()
                        if !store.needsLoginGate {
                            dismiss()
                        }
                    }
                } label: {
                    Text("退出登录")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Color(red: 0.84, green: 0.18, blue: 0.22))
                        .frame(maxWidth: .infinity)
                        .frame(height: 38)
                        .background(Color(red: 1.00, green: 0.93, blue: 0.94))
                        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                }
                .buttonStyle(.plain)
            }
        }
    }

    // MARK: - Subviews & Helpers

    @ViewBuilder
    private var appBrandIcon: some View {
        #if os(macOS)
        if let nsImg = NSApp.applicationIconImage {
            Image(nsImage: nsImg)
                .resizable()
                .scaledToFit()
                .frame(width: 66, height: 66)
                .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        } else {
            fallbackBrandBadge
        }
        #else
        if let uiImg = UIImage(named: "AppIcon60x60") {
            Image(uiImage: uiImg)
                .resizable()
                .scaledToFit()
                .frame(width: 66, height: 66)
                .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        } else {
            fallbackBrandBadge
        }
        #endif
    }

    private var fallbackBrandBadge: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(
                    LinearGradient(
                        colors: [Color(red: 0.24, green: 0.48, blue: 0.98), Color(red: 0.52, green: 0.33, blue: 0.97)],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                )
                .frame(width: 64, height: 64)
            Image(systemName: "checklist.checked")
                .font(.system(size: 28, weight: .bold))
                .foregroundStyle(.white)
        }
    }

    private func modeTabButton(title: String, active: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(.system(size: 13.5, weight: active ? .bold : .medium))
                .foregroundStyle(active ? Color(red: 0.10, green: 0.14, blue: 0.24) : Color(red: 0.45, green: 0.50, blue: 0.60))
                .frame(maxWidth: .infinity)
                .frame(height: 34)
                .background(active ? Color.white : Color.clear)
                .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
                .shadow(color: active ? Color.black.opacity(0.06) : .clear, radius: 4, x: 0, y: 2)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func inputFieldCard<Content: View>(
        icon: String,
        isFocused: Bool,
        @ViewBuilder content: () -> Content
    ) -> some View {
        HStack(spacing: 11) {
            Image(systemName: icon)
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(
                    isFocused
                        ? Color(red: 0.25, green: 0.46, blue: 0.96)
                        : Color(red: 0.56, green: 0.61, blue: 0.70)
                )
                .frame(width: 18)

            content()
        }
        .padding(.horizontal, 14)
        .frame(height: 46)
        .background(isFocused ? Color.white : Color(red: 0.96, green: 0.97, blue: 0.99))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(
                    isFocused
                        ? Color(red: 0.28, green: 0.48, blue: 0.98)
                        : Color(red: 0.87, green: 0.90, blue: 0.95),
                    lineWidth: isFocused ? 1.6 : 1.0
                )
        )
        .shadow(
            color: isFocused ? Color(red: 0.28, green: 0.48, blue: 0.98).opacity(0.10) : .clear,
            radius: 6,
            x: 0,
            y: 2
        )
    }

    private func serverPresetChip(_ label: String, url: String) -> some View {
        let isCurrent = store.serverURL == url
        return Button {
            store.switchServerURL(url)
            Task {
                await store.refresh()
                if isRegisterMode {
                    refreshCaptcha()
                }
            }
        } label: {
            Text(label)
                .font(.system(size: 11.5, weight: .semibold))
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                .background(isCurrent ? Color(red: 0.24, green: 0.48, blue: 0.98).opacity(0.14) : Color(red: 0.94, green: 0.95, blue: 0.98))
                .foregroundStyle(isCurrent ? Color(red: 0.18, green: 0.40, blue: 0.92) : Color(red: 0.38, green: 0.43, blue: 0.52))
                .clipShape(Capsule())
        }
        .buttonStyle(.plain)
    }

    private func quotaStatTile(title: String, value: String, tint: Color) -> some View {
        VStack(spacing: 4) {
            Text(value)
                .font(.system(size: 14, weight: .bold, design: .rounded))
                .foregroundStyle(tint)
            Text(title)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(Color(red: 0.45, green: 0.50, blue: 0.60))
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 10)
        .background(Color.white)
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(Color.black.opacity(0.06), lineWidth: 1)
        )
    }

    private func profileActionRow(icon: String, title: String, tint: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .font(.system(size: 13.5, weight: .semibold))
                    .foregroundStyle(tint)
                    .frame(width: 26, height: 26)
                    .background(tint.opacity(0.12))
                    .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
                Text(title)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Color(red: 0.14, green: 0.18, blue: 0.26))
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Color(red: 0.62, green: 0.67, blue: 0.76))
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .background(Color.white)
            .clipShape(RoundedRectangle(cornerRadius: 11, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 11, style: .continuous)
                    .stroke(Color.black.opacity(0.06), lineWidth: 1)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func copyString(_ text: String) {
        #if os(macOS)
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        #elseif os(iOS)
        UIPasteboard.general.string = text
        #endif
    }

    private func captchaSwiftUIImage(from data: Data) -> Image? {
        #if os(macOS)
        if let nsImg = NSImage(data: data) {
            return Image(nsImage: nsImg)
        }
        #elseif os(iOS)
        if let uiImg = UIImage(data: data) {
            return Image(uiImage: uiImg)
        }
        #endif
        return nil
    }

    private func refreshCaptcha() {
        captchaCode = ""
        isLoadingCaptcha = true
        Task {
            defer { isLoadingCaptcha = false }
            if let resp = try? await store.fetchAuthCaptcha() {
                captchaId = resp.captchaId
                if let pngUrl = resp.imagePng,
                   let commaIdx = pngUrl.firstIndex(of: ",") {
                    let b64 = String(pngUrl[pngUrl.index(after: commaIdx)...])
                    captchaPngData = Data(base64Encoded: b64)
                }
            }
        }
    }

    private func submit() {
        dismissAppKeyboard()
        focusedField = nil
        errorMsg = nil
        isSubmitting = true
        let cleanCaptchaCode = captchaCode.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        Task {
            do {
                if isRegisterMode {
                    try await store.register(
                        username: username,
                        password: password,
                        displayName: displayName,
                        captchaId: captchaId,
                        captchaCode: cleanCaptchaCode
                    )
                } else {
                    try await store.login(username: username, password: password)
                }
                forceSwitchAccount = false
                dismiss()
            } catch {
                errorMsg = error.localizedDescription
                if isRegisterMode {
                    refreshCaptcha()
                }
            }
            isSubmitting = false
        }
    }
}

struct AdminConsoleView: View {
    @EnvironmentObject var store: TodoStore

    @State private var overview: AdminOverviewResponse?
    @State private var users: [AdminUserItemModel] = []
    @State private var settingsDraft: AdminSettingsModel?
    @State private var isLoading: Bool = false
    @State private var message: String?

    var body: some View {
        Form {
            if let message, !message.isEmpty {
                Section {
                    Text(message)
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(.blue)
                }
            }

            if let ov = overview {
                Section("全站实时概览 (\(ov.today))") {
                    LabeledContent("注册用户", value: "\(ov.users.total)（活跃 \(ov.users.active) / 禁用 \(ov.users.disabled)）")
                    LabeledContent("内容总量", value: "原话 \(ov.content.rawNotesTotal) · 记忆 \(ov.content.activeMemories) · 待办 \(ov.content.openTasks)")
                    LabeledContent("今日有效录入", value: "\(ov.todayUsage.notesCount) 次")
                    LabeledContent("🛡️ 今日防线拦截", value: "\(ov.todayUsage.blockedNoiseCount) 次（0 Token 省费）")
                    LabeledContent("今日 AI 调用 / Token", value: "\(ov.todayUsage.llmCalls) 次 / \(ov.todayUsage.tokensUsed)")
                }
            }

            if let binding = Binding($settingsDraft) {
                Section("低成本防线与配额策略") {
                    Toggle("允许公开自助注册", isOn: binding.allowRegistration)
                    Toggle("开启本地水话/乱码规则拦截", isOn: binding.noiseFilterEnabled)
                    Stepper("单条最大字数：\(binding.wrappedValue.maxNoteChars)", value: binding.maxNoteChars, in: 200...20000, step: 200)
                    Stepper("每分钟限流：\(binding.wrappedValue.rateLimitPerMinute) 次", value: binding.rateLimitPerMinute, in: 5...500, step: 5)
                    Stepper("默认每日录入上限：\(binding.wrappedValue.defaultDailyNoteLimit)", value: binding.defaultDailyNoteLimit, in: 10...10000, step: 10)
                    Stepper("默认每日 AI 上限：\(binding.wrappedValue.defaultDailyLlmLimit)", value: binding.defaultDailyLlmLimit, in: 10...10000, step: 10)
                    Button("💾 保存防线与配额设置") {
                        saveSettings()
                    }
                    .fontWeight(.semibold)
                }
            }

            Section("用户列表 (\(users.count))") {
                ForEach(users) { u in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text(u.username).font(.headline)
                            Text("(\(u.displayName))").font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            Text(u.role == "admin" ? "管理员" : "用户")
                                .font(.caption2.bold())
                                .padding(.horizontal, 7)
                                .padding(.vertical, 2)
                                .background(Color.purple.opacity(0.14))
                                .clipShape(Capsule())
                            Text(u.status == "active" ? "正常" : "禁用")
                                .font(.caption2.bold())
                                .padding(.horizontal, 7)
                                .padding(.vertical, 2)
                                .background(u.status == "active" ? Color.green.opacity(0.16) : Color.red.opacity(0.16))
                                .foregroundStyle(u.status == "active" ? .green : .red)
                                .clipShape(Capsule())
                        }
                        if let tu = u.todayUsage, let cnt = u.counts {
                            Text("今日录入 \(tu.notesCount)/\(u.dailyNoteLimit) · AI \(tu.llmCalls)/\(u.dailyLlmLimit) · 拦截 \(tu.blockedNoiseCount) ｜ 记忆 \(cnt.memoriesTotal)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        HStack(spacing: 12) {
                            Button(u.status == "active" ? "停用账号" : "启用账号") {
                                toggleUserStatus(u)
                            }
                            .font(.caption)
                            .buttonStyle(.bordered)
                            .tint(u.status == "active" ? .red : .green)

                            Button(u.role == "admin" ? "降为普通用户" : "设为管理员") {
                                toggleUserRole(u)
                            }
                            .font(.caption)
                            .buttonStyle(.bordered)
                        }
                    }
                    .padding(.vertical, 4)
                }
            }
        }
        .formStyle(.grouped)
        .navigationTitle("🛡️ 后台管理")
        .toolbar {
            Button {
                Task { await loadAll() }
            } label: {
                if isLoading {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: "arrow.clockwise")
                }
            }
        }
        .task { await loadAll() }
    }

    private func loadAll() async {
        isLoading = true
        defer { isLoading = false }
        do {
            async let ovReq = store.client.fetchAdminOverview()
            async let usrReq = store.client.fetchAdminUsers()
            let (ov, usr) = try await (ovReq, usrReq)
            self.overview = ov
            self.settingsDraft = ov.settings
            self.users = usr.users
            self.message = nil
        } catch {
            self.message = "加载失败：\(error.localizedDescription)"
        }
    }

    private func saveSettings() {
        guard let draft = settingsDraft else { return }
        Task {
            do {
                let updated = try await store.client.updateAdminSettings(draft)
                self.settingsDraft = updated
                self.message = "✅ 防线与配额配置已保存"
            } catch {
                self.message = "保存失败：\(error.localizedDescription)"
            }
        }
    }

    private func toggleUserStatus(_ user: AdminUserItemModel) {
        let next = user.status == "active" ? "disabled" : "active"
        Task {
            do {
                try await store.client.updateAdminUser(userId: user.userId, status: next)
                await loadAll()
            } catch {
                self.message = "操作失败：\(error.localizedDescription)"
            }
        }
    }

    private func toggleUserRole(_ user: AdminUserItemModel) {
        let next = user.role == "admin" ? "user" : "admin"
        Task {
            do {
                try await store.client.updateAdminUser(userId: user.userId, role: next)
                await loadAll()
            } catch {
                self.message = "操作失败：\(error.localizedDescription)"
            }
        }
    }
}
