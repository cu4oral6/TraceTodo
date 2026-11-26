import Foundation
import SwiftUI
#if canImport(UserNotifications)
@preconcurrency import UserNotifications
#endif
#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

extension Notification.Name {
    static let traceTodoOpenOverview = Notification.Name("traceTodoOpenOverview")
}

@MainActor
final class NotificationManager: NSObject, ObservableObject {
    static let shared = NotificationManager()

    @Published var notificationsEnabled: Bool {
        didSet { UserDefaults.standard.set(notificationsEnabled, forKey: "tm_notif_enabled") }
    }
    /// Default advance reminder in minutes for tasks that have `due_at` but no explicit offset (-1 = manual only).
    @Published var defaultReminderMinutes: Int {
        didSet { UserDefaults.standard.set(defaultReminderMinutes, forKey: "tm_notif_default_minutes") }
    }
    @Published var dailyDigestEnabled: Bool {
        didSet { UserDefaults.standard.set(dailyDigestEnabled, forKey: "tm_notif_daily_digest") }
    }
    @Published var notifyNewRemoteTasks: Bool {
        didSet { UserDefaults.standard.set(notifyNewRemoteTasks, forKey: "tm_notif_remote_tasks") }
    }

    #if canImport(UserNotifications)
    @Published private(set) var authorizationStatus: UNAuthorizationStatus = .notDetermined
    #endif

    private var delegateConfigured = false

    private override init() {
        let ud = UserDefaults.standard
        if ud.object(forKey: "tm_notif_enabled") == nil {
            self.notificationsEnabled = true
        } else {
            self.notificationsEnabled = ud.bool(forKey: "tm_notif_enabled")
        }
        if ud.object(forKey: "tm_notif_default_minutes") == nil {
            self.defaultReminderMinutes = 15
        } else {
            self.defaultReminderMinutes = ud.integer(forKey: "tm_notif_default_minutes")
        }
        if ud.object(forKey: "tm_notif_daily_digest") == nil {
            self.dailyDigestEnabled = true
        } else {
            self.dailyDigestEnabled = ud.bool(forKey: "tm_notif_daily_digest")
        }
        if ud.object(forKey: "tm_notif_remote_tasks") == nil {
            self.notifyNewRemoteTasks = true
        } else {
            self.notifyNewRemoteTasks = ud.bool(forKey: "tm_notif_remote_tasks")
        }
        super.init()
    }

    private var hasBundleIdentifier: Bool {
        Bundle.main.bundleIdentifier != nil
    }

    var statusLabel: String {
        #if canImport(UserNotifications)
        switch authorizationStatus {
        case .authorized, .provisional, .ephemeral:
            return notificationsEnabled ? "已开启" : "系统已授权（应用内已暂停）"
        case .denied:
            return "已在系统设置中关闭"
        case .notDetermined:
            return "未授权（点击开启）"
        @unknown default:
            return "未知状态"
        }
        #else
        return "当前平台不支持"
        #endif
    }

    var isSystemDenied: Bool {
        #if canImport(UserNotifications)
        return authorizationStatus == .denied
        #else
        return false
        #endif
    }

    func configureDelegate() {
        #if canImport(UserNotifications)
        guard hasBundleIdentifier, !delegateConfigured else { return }
        delegateConfigured = true
        UNUserNotificationCenter.current().delegate = self
        refreshAuthorizationStatus()
        #endif
    }

    func refreshAuthorizationStatus() {
        #if canImport(UserNotifications)
        guard hasBundleIdentifier else { return }
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            Task { @MainActor in
                self.authorizationStatus = settings.authorizationStatus
            }
        }
        #endif
    }

    func requestPermissionIfNeeded() {
        Task {
            _ = await requestPermissionAndReport()
        }
    }

    @discardableResult
    func requestPermissionAndReport() async -> Bool {
        #if canImport(UserNotifications)
        guard hasBundleIdentifier else { return false }
        configureDelegate()
        let center = UNUserNotificationCenter.current()
        do {
            let granted = try await center.requestAuthorization(options: [.alert, .sound, .badge])
            let settings = await center.notificationSettings()
            self.authorizationStatus = settings.authorizationStatus
            if granted {
                self.notificationsEnabled = true
            }
            return granted
        } catch {
            refreshAuthorizationStatus()
            return false
        }
        #else
        return false
        #endif
    }

    func openSystemNotificationSettings() {
        #if os(iOS)
        if let url = URL(string: UIApplication.openSettingsURLString) {
            UIApplication.shared.open(url)
        }
        #elseif os(macOS)
        if let url = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension") {
            NSWorkspace.shared.open(url)
        }
        #endif
    }

    func sendTestNotification() async -> String {
        #if canImport(UserNotifications)
        guard hasBundleIdentifier else { return "当前为非 App Bundle 运行模式，无法发送系统通知" }
        configureDelegate()
        let center = UNUserNotificationCenter.current()
        var settings = await center.notificationSettings()
        if settings.authorizationStatus == .notDetermined {
            _ = await requestPermissionAndReport()
            settings = await center.notificationSettings()
        }
        self.authorizationStatus = settings.authorizationStatus
        guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
            return "系统通知权限未开启，请点击「前往系统设置」允许 有迹 通知"
        }
        let content = UNMutableNotificationContent()
        content.title = "🔔 有迹 测试通知"
        content.body = "通知权限与前台弹窗配置正常！后续任务到期与跨端新任务都会及时提醒你。"
        content.sound = .default
        let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 3, repeats: false)
        let req = UNNotificationRequest(
            identifier: "tracetodo.test.\(UUID().uuidString)",
            content: content,
            trigger: trigger
        )
        do {
            try await center.add(req)
            return "已排程测试通知，3 秒后将弹出提醒"
        } catch {
            return "发送测试通知失败：\(error.localizedDescription)"
        }
        #else
        return "当前环境不支持系统通知"
        #endif
    }

    func notifyRemoteTasksArrived(_ newTasks: [TaskItemModel]) {
        #if canImport(UserNotifications)
        guard hasBundleIdentifier, notificationsEnabled, notifyNewRemoteTasks, !newTasks.isEmpty else { return }
        configureDelegate()
        let center = UNUserNotificationCenter.current()
        center.getNotificationSettings { settings in
            guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
                return
            }
            let content = UNMutableNotificationContent()
            content.title = "🔄 收到跨端同步的新任务"
            if newTasks.count == 1, let first = newTasks.first {
                content.body = "[\(first.payload.quadrantKind.shortCode)] \(first.title)"
            } else {
                let firstTitle = newTasks.first?.title ?? "新待办"
                content.body = "新增 \(newTasks.count) 项待办：\(firstTitle) 等"
            }
            content.sound = .default
            let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 1, repeats: false)
            let req = UNNotificationRequest(
                identifier: "tracetodo.sync.\(UUID().uuidString)",
                content: content,
                trigger: trigger
            )
            center.add(req)
        }
        #endif
    }

    func syncReminders(for tasks: [TaskItemModel]) {
        #if canImport(UserNotifications)
        guard hasBundleIdentifier else { return }
        configureDelegate()
        let center = UNUserNotificationCenter.current()
        let enabled = self.notificationsEnabled
        let defaultOffset = self.defaultReminderMinutes
        let digestEnabled = self.dailyDigestEnabled

        center.getNotificationSettings { settings in
            Task { @MainActor in
                self.authorizationStatus = settings.authorizationStatus
            }
            guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
                return
            }

            let taskPrefix = "tracetodo.task."
            let digestId = "tracetodo.daily.digest"

            center.getPendingNotificationRequests { pending in
                let oldIds = pending.map(\.identifier).filter {
                    $0.hasPrefix(taskPrefix) || $0 == digestId
                }
                if !oldIds.isEmpty {
                    center.removePendingNotificationRequests(withIdentifiers: oldIds)
                }

                let activeTodos = tasks.filter { !$0.payload.isDone }
                let urgentBadge = activeTodos.filter {
                    $0.payload.isOverdue == true || $0.payload.quadrant == "Q1_do_first"
                }.count
                if #available(iOS 16.0, macOS 13.0, *) {
                    center.setBadgeCount(enabled ? urgentBadge : 0) { _ in }
                }

                guard enabled else { return }

                let now = Date()
                var candidates: [(fireDate: Date, task: TaskItemModel, offsetMin: Int)] = []

                for task in activeTodos {
                    guard let dueStr = task.payload.dueAt, !dueStr.isEmpty else { continue }
                    let effectiveOffset: Int
                    if let explicit = task.payload.reminderOffsetMinutes {
                        effectiveOffset = explicit
                    } else if defaultOffset >= 0 {
                        effectiveOffset = defaultOffset
                    } else {
                        continue
                    }
                    guard let dueDate = TraceTodoDateParser.parseBeijingDate(dueStr) else {
                        continue
                    }
                    let fireDate = dueDate.addingTimeInterval(TimeInterval(-effectiveOffset * 60))
                    guard fireDate > now else { continue }
                    candidates.append((fireDate, task, effectiveOffset))
                }

                // Cap at 50 upcoming task reminders to stay safely below iOS's 64 pending limit
                candidates.sort { $0.fireDate < $1.fireDate }
                for item in candidates.prefix(50) {
                    let content = UNMutableNotificationContent()
                    content.title = "⏰ 有迹 待办提醒"
                    let prefixTag = item.offsetMin > 0 ? "提前\(item.offsetMin)分钟 · " : "已到截止时间 · "
                    content.body = "\(prefixTag)[\(item.task.payload.quadrantKind.shortCode)] \(item.task.title)"
                    content.sound = .default

                    let comps = Calendar.current.dateComponents(
                        [.year, .month, .day, .hour, .minute, .second],
                        from: item.fireDate
                    )
                    let trigger = UNCalendarNotificationTrigger(dateMatching: comps, repeats: false)
                    let req = UNNotificationRequest(
                        identifier: taskPrefix + item.task.memoryId,
                        content: content,
                        trigger: trigger
                    )
                    center.add(req)
                }

                // Daily 09:00 Beijing Time morning digest
                if digestEnabled && !activeTodos.isEmpty {
                    let q1Cnt = activeTodos.filter { $0.payload.quadrant == "Q1_do_first" }.count
                    let overdueCnt = activeTodos.filter { $0.payload.isOverdue == true }.count
                    let digestContent = UNMutableNotificationContent()
                    digestContent.title = "☀️ 有迹 今日待办巡检"
                    digestContent.body = "当前共有 \(activeTodos.count) 项待办（Q1 紧急 \(q1Cnt) 项，已逾期 \(overdueCnt) 项），轻点查看今日规划。"
                    digestContent.sound = .default

                    var comps = DateComponents()
                    comps.hour = 9
                    comps.minute = 0
                    comps.timeZone = TimeZone(identifier: "Asia/Shanghai")
                    let digestTrigger = UNCalendarNotificationTrigger(dateMatching: comps, repeats: true)
                    let digestReq = UNNotificationRequest(
                        identifier: digestId,
                        content: digestContent,
                        trigger: digestTrigger
                    )
                    center.add(digestReq)
                }
            }
        }
        #endif
    }
}

#if canImport(UserNotifications)
extension NotificationManager: UNUserNotificationCenterDelegate {
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        return [.banner, .list, .sound]
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        await MainActor.run {
            NotificationCenter.default.post(name: .traceTodoOpenOverview, object: nil)
        }
    }
}
#endif
