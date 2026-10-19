import AppKit
import ApplicationServices

// 自动重试：模型繁忙（14003）导致任务中断时，自动点掉错误提示里的「重试」。
//
// 为什么要用辅助功能（AX）：
//   「重试」是页面里的按钮，外部没有公开接口能触发它。唯一和「人手动点」等价的
//   办法，是通过 macOS 辅助功能 API 找到那个按钮并按下。代价是必须给本 App
//   授权「辅助功能」，只需一次。
//
// 怎么省 CPU：
//   一直全量遍历页面结构（几百个节点）太浪费。所以用日志当触发器——
//   App 每次遇到 14003 都会在 ~/.workbuddy-ai/logs/<日期>/<空间>.log 里写
//   `refusal classified: ... bizCode=14003`。只有看到新的一条，才切到高频扫描；
//   平时 15 秒兜底扫一次（防止日志格式变了导致完全失效）。
//
// 安全阀：
//   - 必须同时看到「重试」按钮 + 错误提示特征（繁忙文案或「换模型」按钮）才动手；
//   - 两次点击至少间隔 8 秒（重试期间提示不会立刻消失）；
//   - 每小时最多 40 次，防止死循环刷接口；
//   - 每次动作都写 ~/Library/Logs/wbvault-retry.log。

let workbuddyBundleID = "com.workbuddy.workbuddy-ai"

final class AutoRetry {
    static let shared = AutoRetry()

    private let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/wbvault-retry.log")
    private let appLogRoot = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".workbuddy-ai/logs")

    private let queue = DispatchQueue(label: "ai.workbuddy.wbvault.autoretry")
    private var timer: Timer?
    private var scanning = false

    private var lastPress: Date = .distantPast
    private var presses: [Date] = []
    private var gateUntil: Date = .distantPast
    private var lastGateCheck: Date = .distantPast
    private var lastScan: Date = .distantPast
    private var offsets: [String: UInt64] = [:]

    private let tickInterval: TimeInterval = 2.0
    private let gateInterval: TimeInterval = 3.0
    private let fastScanInterval: TimeInterval = 3.0
    private let idleScanInterval: TimeInterval = 15.0
    private let gateHoldSeconds: TimeInterval = 180
    private let cooldown: TimeInterval = 8
    private let maxPerHour = 40
    private let nodeBudget = 2200
    private let timeBudget: TimeInterval = 1.0

    private(set) var totalPresses = 0
    private(set) var lastMessage = ""
    private(set) var gateOpen = false
    private var wasTrusted = false

    private init() {
        // 默认开启：这是给「模型繁忙」擦屁股的兜底，装好就该工作。
        UserDefaults.standard.register(defaults: ["autoRetry": true])
    }

    // MARK: - 开关

    var enabled: Bool {
        get { UserDefaults.standard.bool(forKey: "autoRetry") }
        set {
            UserDefaults.standard.set(newValue, forKey: "autoRetry")
            if newValue {
                requestPermission()
                start()
            } else {
                stop()
            }
        }
    }

    var trusted: Bool { AXIsProcessTrusted() }

    /// 触发系统的「辅助功能」授权弹窗（只弹一次，之后要用户自己去设置里勾）。
    func requestPermission() {
        guard !trusted else { return }
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        _ = AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
    }

    func start() {
        stop()
        guard enabled else { return }
        if !trusted { requestPermission() }
        let t = Timer(timeInterval: tickInterval, repeats: true) { [weak self] _ in
            self?.tick()
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
        log("自动重试已启动（授权：\(trusted ? "有" : "无")）")
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        gateOpen = false
    }

    // MARK: - 轮询

    private func tick() {
        // 权限是随时可能变的：用户在设置里勾上之后，这里能自己发现，不用重启 App
        let nowTrusted = trusted
        if nowTrusted, !wasTrusted {
            log("已获得辅助功能授权，开始工作")
            lastMessage = "已授权"
        }
        wasTrusted = nowTrusted
        guard enabled, nowTrusted else { return }
        refreshGate()

        let now = Date()
        let interval = gateOpen ? fastScanInterval : idleScanInterval
        guard now.timeIntervalSince(lastScan) >= interval else { return }
        if scanning { return }
        lastScan = now
        scanning = true
        queue.async { [weak self] in
            guard let self else { return }
            self.scanOnce()
            DispatchQueue.main.async { self.scanning = false }
        }
    }

    // MARK: - 日志触发器

    /// 只在有新日志时读增量字节，找 `bizCode=14003`。
    private func refreshGate() {
        let now = Date()
        guard now.timeIntervalSince(lastGateCheck) >= gateInterval else { return }
        lastGateCheck = now
        if now < gateUntil {
            gateOpen = true
            return
        }
        gateOpen = false
        if scanLogsForRefusal() {
            gateUntil = now.addingTimeInterval(gateHoldSeconds)
            gateOpen = true
            lastMessage = "检测到 14003，开始盯页面"
            log("检测到 bizCode=14003，\(Int(gateHoldSeconds)) 秒内高频检查页面")
        }
    }

    private func scanLogsForRefusal() -> Bool {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        let dayDir = appLogRoot.appendingPathComponent(formatter.string(from: Date()))
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: dayDir, includingPropertiesForKeys: [.fileSizeKey], options: [.skipsHiddenFiles]
        ) else { return false }

        var hit = false
        for file in files where file.pathExtension == "log" {
            let key = file.path
            var size: UInt64 = 0
            if let attrs = try? FileManager.default.attributesOfItem(atPath: key),
               let number = attrs[.size] as? NSNumber {
                size = number.uint64Value
            }
            var offset = offsets[key] ?? 0
            if size < offset { offset = 0 }        // 日志被截断/轮转
            guard size > offset else {
                offsets[key] = size
                continue
            }
            guard let handle = try? FileHandle(forReadingFrom: file) else { continue }
            defer { try? handle.close() }
            // 第一次见到某个日志文件时直接跳到末尾：只看「刚发生」的 14003，
            // 别把今天早些时候的历史记录当成新事件，一启动就空转 3 分钟。
            if offsets[key] == nil {
                offsets[key] = size
                continue
            }
            do {
                try handle.seek(toOffset: offset)
                let data = handle.readData(ofLength: Int(min(size - offset, 262_144)))
                offsets[key] = offset + UInt64(data.count)
                if let text = String(data: data, encoding: .utf8), text.contains("bizCode=14003") {
                    hit = true
                }
            } catch {
                continue
            }
        }
        return hit
    }

    // MARK: - 页面扫描

    private func scanOnce() {
        guard let app = NSRunningApplication
            .runningApplications(withBundleIdentifier: workbuddyBundleID).first else { return }
        let root = AXUIElementCreateApplication(app.processIdentifier)
        enableAccessibility(root)

        guard let hit = findRetry(in: root, allowWeak: gateOpen) else { return }

        let now = Date()
        guard now.timeIntervalSince(lastPress) >= cooldown else { return }
        presses = presses.filter { now.timeIntervalSince($0) < 3600 }
        guard presses.count < maxPerHour else {
            if lastMessage != "已达每小时上限" {
                lastMessage = "已达每小时上限"
                log("跳过：一小时内已重试 \(presses.count) 次，先停手")
            }
            return
        }

        let result = AXUIElementPerformAction(hit, kAXPressAction as CFString)
        if result == .success {
            lastPress = now
            presses.append(now)
            totalPresses += 1
            lastMessage = "刚刚自动重试"
            log("已自动点击「重试」（第 \(totalPresses) 次）")
        } else {
            lastMessage = "点击失败：\(result.rawValue)"
            log("点击失败，AX 错误码 \(result.rawValue)")
        }
    }

    /// Chromium/Electron 默认不把页面内容暴露给辅助功能，需要显式打开。
    func enableAccessibility(_ app: AXUIElement) {
        AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
    }

    /// 错误提示的特征：有「重试」按钮，且同窗口里还有「繁忙」文案或「换模型」按钮。
    ///
    /// 两点经验：
    ///   1. 必须从 `AXWindows` 往下走。App 根节点的子节点只有菜单栏，页面内容在窗口下面。
    ///   2. 判断「能按」用 `AXPress` 动作，别死认 AXButton —— 页面里的按钮有时是
    ///      AXLink / AXMenuItem / 自定义角色。
    private func findRetry(in root: AXUIElement, allowWeak: Bool) -> AXUIElement? {
        var queue: [(AXUIElement, Int)] = windows(root).map { ($0, 0) }
        var index = 0
        var visited = 0
        var retryButton: AXUIElement?
        var toastSignal = false
        let deadline = Date().addingTimeInterval(timeBudget)

        while index < queue.count, visited < nodeBudget {
            if visited % 150 == 0, Date() > deadline { break }
            let (element, depth) = queue[index]
            index += 1
            visited += 1

            let label = [
                string(element, kAXTitleAttribute),
                string(element, kAXDescriptionAttribute),
                string(element, kAXValueAttribute),
            ].joined(separator: " ")

            if label.contains("重试"), canPress(element), isEnabled(element) {
                if retryButton == nil { retryButton = element }
                if toastSignal { return retryButton }
            } else if label.contains("繁忙") || label.contains("稍后重试")
                        || label.contains("请求量饱和") || label.contains("换模型") {
                toastSignal = true
                if retryButton != nil { return retryButton }
            }

            if depth < 60 {
                for child in children(element) {
                    queue.append((child, depth + 1))
                }
            }
        }

        // 兜底：日志里刚确认过 14003，只是提示文案没被辅助功能暴露出来。
        // 这时页面上出现的「重试」基本只可能是那条错误提示。
        if allowWeak { return retryButton }
        return nil
    }

    private func windows(_ root: AXUIElement) -> [AXUIElement] {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(root, kAXWindowsAttribute as CFString, &value) == .success,
              let items = value as? [AXUIElement] else { return [] }
        return items
    }

    /// 元素是否支持「按下」——比判断角色可靠。
    private func canPress(_ element: AXUIElement) -> Bool {
        var names: CFArray?
        guard AXUIElementCopyActionNames(element, &names) == .success,
              let actions = names as? [String] else { return false }
        return actions.contains(kAXPressAction as String)
    }

    private func children(_ element: AXUIElement) -> [AXUIElement] {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &value) == .success,
              let items = value as? [AXUIElement] else { return [] }
        return items
    }

    private func string(_ element: AXUIElement, _ attribute: String) -> String {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success else {
            return ""
        }
        return (value as? String) ?? ""
    }

    private func isEnabled(_ element: AXUIElement) -> Bool {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXEnabledAttribute as CFString, &value) == .success else {
            return true
        }
        return (value as? Bool) ?? true
    }

    // MARK: - 诊断

    /// 给 --ax-dump 用：树结构不对时看清楚。
    func dumpTree() -> [String] {
        var lines: [String] = []
        guard let app = NSRunningApplication
            .runningApplications(withBundleIdentifier: workbuddyBundleID).first else {
            return ["app: 没在运行"]
        }
        let root = AXUIElementCreateApplication(app.processIdentifier)
        enableAccessibility(root)
        Thread.sleep(forTimeInterval: 0.8)

        var queue: [(AXUIElement, Int)] = windows(root).map { ($0, 0) }
        var index = 0
        var visited = 0
        while index < queue.count, visited < 400 {
            let (element, depth) = queue[index]
            index += 1
            visited += 1
            let role = string(element, kAXRoleAttribute)
            let label = [
                string(element, kAXTitleAttribute),
                string(element, kAXValueAttribute),
                string(element, kAXDescriptionAttribute),
            ].filter { !$0.isEmpty }.joined(separator: " | ")
            lines.append("\(String(repeating: "  ", count: min(depth, 12)))[\(role)] \(label.prefix(80))")
            if depth < 60 {
                for child in children(element) {
                    queue.append((child, depth + 1))
                }
            }
        }
        lines.append("节点数: \(visited)")
        return lines
    }

    /// 给 --ax-test 用：确认权限、能不能读到页面、有没有「重试」按钮。
    func diagnose() -> [String] {
        var lines: [String] = []
        lines.append("trusted: \(trusted ? "yes" : "no")")
        guard let app = NSRunningApplication
            .runningApplications(withBundleIdentifier: workbuddyBundleID).first else {
            lines.append("app: 没在运行")
            return lines
        }
        lines.append("app: \(app.localizedName ?? "?") pid=\(app.processIdentifier)")
        guard trusted else {
            lines.append("提示：未授权辅助功能，拿不到页面结构")
            return lines
        }

        let root = AXUIElementCreateApplication(app.processIdentifier)
        enableAccessibility(root)
        Thread.sleep(forTimeInterval: 0.8)

        var windowsValue: CFTypeRef?
        AXUIElementCopyAttributeValue(root, kAXWindowsAttribute as CFString, &windowsValue)
        let windows = (windowsValue as? [AXUIElement]) ?? []
        lines.append("窗口数: \(windows.count)")

        var queue: [(AXUIElement, Int)] = windows.map { ($0, 0) }
        var index = 0
        var visited = 0
        var buttons: [String] = []
        var signals: [String] = []
        let started = Date()
        while index < queue.count, visited < 4000 {
            let (element, depth) = queue[index]
            index += 1
            visited += 1
            let role = string(element, kAXRoleAttribute)
            if canPress(element) || role == "AXButton" {
                let label = [
                    string(element, kAXTitleAttribute),
                    string(element, kAXDescriptionAttribute),
                    string(element, kAXValueAttribute),
                ].joined(separator: " ").trimmingCharacters(in: .whitespaces)
                if !label.isEmpty { buttons.append(label) }
            } else {
                let text = [
                    string(element, kAXValueAttribute),
                    string(element, kAXTitleAttribute),
                    string(element, kAXDescriptionAttribute),
                ].joined(separator: " ")
                if text.contains("繁忙") || text.contains("重试") || text.contains("换模型") {
                    signals.append(String(text.prefix(40)))
                }
            }
            if depth < 60 {
                for child in children(element) {
                    queue.append((child, depth + 1))
                }
            }
        }
        let ms = Int(Date().timeIntervalSince(started) * 1000)
        lines.append("节点数: \(visited)（\(ms)ms）")
        lines.append("按钮(\(buttons.count)): \(buttons.prefix(30).joined(separator: " | "))")
        lines.append("命中文案: \(signals.prefix(6).joined(separator: " | "))")
        lines.append("命中「重试」按钮(强匹配): \(findRetry(in: root, allowWeak: false) != nil ? "yes" : "no")")
        lines.append("命中「重试」按钮(日志兜底): \(findRetry(in: root, allowWeak: true) != nil ? "yes" : "no")")
        return lines
    }

    // MARK: - 日志

    private func log(_ message: String) {
        let stamp = ISO8601DateFormatter().string(from: Date())
        let line = "[\(stamp)] \(message)\n"
        if let data = line.data(using: .utf8) {
            if let handle = try? FileHandle(forWritingTo: logURL) {
                handle.seekToEndOfFile()
                handle.write(data)
                try? handle.close()
            } else {
                try? data.write(to: logURL)
            }
        }
    }
}
