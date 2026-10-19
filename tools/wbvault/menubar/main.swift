import AppKit
import Foundation

// WBVault 菜单栏：一个 App 同时负责「采集登录态」和「切换账号」。
// 启动时自动拉起 wbvault.py watch；点账号即可切换。
// 支持开机自启（写 LaunchAgent），无需终端。

let home = FileManager.default.homeDirectoryForCurrentUser
let vaultRoot = home.appendingPathComponent("Library/Application Support/WorkBuddyAccountVault")
let runtimeScript = vaultRoot.appendingPathComponent("runtime/wbvault.py")
let authFile = home.appendingPathComponent(
    "Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop-ai.info"
)
let watchLog = home.appendingPathComponent("Library/Logs/wbvault-watch.log")
let loginItemLabel = "ai.workbuddy.wbvault.app"
let loginItemPath = home.appendingPathComponent("Library/LaunchAgents/\(loginItemLabel).plist")
// App 自带一份 wbvault 源码（build.sh 打进 Resources/runtime-src），
// 启动时用它校准运行时副本，避免「App 传新参数、副本还是旧代码」。
let runtimeDir = vaultRoot.appendingPathComponent("runtime")
let bundledRuntime = Bundle.main.resourceURL?.appendingPathComponent("runtime-src")

// MARK: - 基础工具

func readJSON(_ url: URL) -> [String: Any]? {
    guard let data = try? Data(contentsOf: url) else { return nil }
    return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
}

func shell(_ path: String, _ args: [String]) -> (Int32, String) {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: path)
    process.arguments = args
    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = pipe
    do {
        try process.run()
    } catch {
        return (127, error.localizedDescription)
    }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    return (process.terminationStatus, String(data: data, encoding: .utf8) ?? "")
}

func runPython(_ args: [String]) -> (Int32, String) {
    shell("/usr/bin/python3", [runtimeScript.path] + args)
}

// MARK: - 运行时同步

func readStamp(_ dir: URL?) -> String {
    guard let dir,
          let text = try? String(contentsOf: dir.appendingPathComponent(".stamp"), encoding: .utf8) else {
        return ""
    }
    return text.trimmingCharacters(in: .whitespacesAndNewlines)
}

/// 副本指纹和 App 自带源码不一致时整份覆盖。返回是否刷新过。
@discardableResult
func syncBundledRuntime() -> Bool {
    guard let src = bundledRuntime,
          FileManager.default.fileExists(atPath: src.appendingPathComponent("wbvault.py").path) else {
        return false
    }
    let stamp = readStamp(src)
    if !stamp.isEmpty, stamp == readStamp(runtimeDir) { return false }
    do {
        if FileManager.default.fileExists(atPath: runtimeDir.path) {
            try FileManager.default.removeItem(at: runtimeDir)
        }
        try FileManager.default.createDirectory(at: vaultRoot, withIntermediateDirectories: true)
        try FileManager.default.copyItem(at: src, to: runtimeDir)
        return true
    } catch {
        return false
    }
}

// MARK: - 数据

struct Account {
    let uid: String
    let alias: String
    let email: String
    let uin: String
    let domain: String
    let daysLeft: Int?
    let lastSeen: Int

    var shortUid: String { String(uid.prefix(8)) }
    /// 邮箱最好认；没有邮箱就退回别名，再退 uid 前 8 位。
    var displayName: String {
        if !email.isEmpty { return email }
        return alias.isEmpty ? shortUid : alias
    }

    /// 菜单里的一行：别名 · 邮箱（没邮箱就补 uid）· 剩余天数，不重复展示同一个标识。
    var menuTitle: String {
        var parts: [String] = []
        if !alias.isEmpty, alias.lowercased() != shortUid { parts.append(alias) }
        parts.append(email.isEmpty ? shortUid : email)
        if let days = daysLeft { parts.append("剩 \(days) 天") }
        return parts.joined(separator: " · ")
    }
}

func loadActiveUid() -> String? {
    guard let root = readJSON(authFile),
          let account = root["account"] as? [String: Any],
          let uid = account["uid"] as? String else { return nil }
    return uid
}

func loadAccounts() -> [Account] {
    guard let root = readJSON(vaultRoot.appendingPathComponent("accounts.json")),
          let items = root["accounts"] as? [[String: Any]] else { return [] }
    let now = Date().timeIntervalSince1970 * 1000
    return items.compactMap { item in
        guard let uid = item["uid"] as? String else { return nil }
        let expires = item["refreshExpiresAt"] as? Double ?? 0
        let days: Int? = expires > 0 ? max(0, Int((expires - now) / 86_400_000)) : nil
        return Account(
            uid: uid,
            alias: item["alias"] as? String ?? "",
            email: item["email"] as? String ?? "",
            uin: item["uin"] as? String ?? "",
            domain: item["domain"] as? String ?? "",
            daysLeft: days,
            lastSeen: item["lastSeenAt"] as? Int ?? item["lastSeenActiveAt"] as? Int ?? 0
        )
    }
}

// MARK: - 版本

/// 构建时写进 Resources/build-info.txt（commit / built / 运行时源码指纹）。
/// 用来回答「我这份是不是最新的」——App 不会自己热更新，版本号是唯一的凭证。
func buildInfo() -> [String: String] {
    guard let url = Bundle.main.resourceURL?.appendingPathComponent("build-info.txt"),
          let text = try? String(contentsOf: url, encoding: .utf8) else { return [:] }
    var dict: [String: String] = [:]
    for line in text.split(separator: "\n") {
        let parts = line.split(separator: "=", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
        if parts.count == 2 { dict[parts[0]] = parts[1] }
    }
    return dict
}

func buildLabel() -> String {
    // 只报构建时间：发布流程是「先编译、后提交」，hash 天然比提交慢一拍，
    // 拿它比对会误判；时间才是判断新旧的唯一可靠依据。
    buildInfo()["built"] ?? "unknown"
}

func buildDetail() -> String {
    let info = buildInfo()
    return "\(buildLabel())（commit \(info["commit"] ?? "?")，源码指纹 \(info["stamp"] ?? "?")）"
}

// MARK: - 监听进程

func watcherPids() -> [String] {
    let (code, output) = shell("/usr/bin/pgrep", ["-f", "wbvault.py watch"])
    guard code == 0 else { return [] }
    return output.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
}

func isWatcherRunning() -> Bool { !watcherPids().isEmpty }

// MARK: - 开机自启

func loginItemInstalled() -> Bool {
    FileManager.default.fileExists(atPath: loginItemPath.path)
}

func installLoginItem() -> (Bool, String) {
    let plist: [String: Any] = [
        "Label": loginItemLabel,
        "ProgramArguments": ["/usr/bin/open", "-a", Bundle.main.bundlePath],
        "RunAtLoad": true,
        "ProcessType": "Interactive",
    ]
    do {
        try FileManager.default.createDirectory(
            at: loginItemPath.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
        try data.write(to: loginItemPath)
    } catch {
        return (false, "写入自启配置失败：\(error.localizedDescription)")
    }
    let (code, output) = shell("/bin/launchctl", ["bootstrap", "gui/\(getuid())", loginItemPath.path])
    if code != 0, !output.contains("already bootstrapped") {
        // plist 已写好，下次登录时 launchd 会自动加载；本次会话注册失败不算致命
        return (true, "自启配置已写入，下次登录时生效。\n当前会话注册被系统拒绝：\(output.trimmingCharacters(in: .whitespacesAndNewlines))")
    }
    return (true, "")
}

func removeLoginItem() -> (Bool, String) {
    _ = shell("/bin/launchctl", ["bootout", "gui/\(getuid())/\(loginItemLabel)"])
    do {
        if FileManager.default.fileExists(atPath: loginItemPath.path) {
            try FileManager.default.removeItem(at: loginItemPath)
        }
    } catch {
        return (false, "移除自启配置失败：\(error.localizedDescription)")
    }
    return (true, "")
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var statusItem: NSStatusItem!
    var busy = false
    var ownedWatcher: Process?
    var startedWatcher = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        syncBundledRuntime()
        startWatcherIfNeeded()
        AutoRetry.shared.start()

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            let image = NSImage(systemSymbolName: "person.2.circle", accessibilityDescription: "WBVault")
            image?.isTemplate = true
            button.image = image
            button.toolTip = "WBVault · WorkBuddy 账号切换"
        }
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu

        // 开着自动重试却没拿到权限时，早点说清楚，别等用户发现「怎么没生效」
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            if AutoRetry.shared.enabled, !AutoRetry.shared.trusted {
                self.showPermissionAlert()
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        // 只结束自己拉起的监听，避免误杀 LaunchAgent 那个
        if startedWatcher, let process = ownedWatcher, process.isRunning {
            process.terminate()
        }
    }

    func startWatcherIfNeeded() {
        if isWatcherRunning() { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [runtimeScript.path, "watch", "--interval", "1"]
        if !FileManager.default.fileExists(atPath: watchLog.path) {
            FileManager.default.createFile(atPath: watchLog.path, contents: nil)
        }
        if let handle = try? FileHandle(forWritingTo: watchLog) {
            handle.seekToEndOfFile()
            process.standardOutput = handle
            process.standardError = handle
        }
        do {
            try process.run()
            ownedWatcher = process
            startedWatcher = true
        } catch {
            startedWatcher = false
        }
    }

    // MARK: 菜单

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let accounts = loadAccounts()
        let activeUid = loadActiveUid()

        let current = accounts.first { $0.uid == activeUid }
        let header = NSMenuItem(
            title: current.map { "当前：\($0.displayName)（\($0.shortUid)）" } ?? "当前：未登录 / 未入库",
            action: nil,
            keyEquivalent: ""
        )
        header.isEnabled = false
        menu.addItem(header)

        if let current, let days = current.daysLeft {
            let sub = NSMenuItem(title: "登录态剩余 \(days) 天", action: nil, keyEquivalent: "")
            sub.isEnabled = false
            menu.addItem(sub)
        }

        let pids = watcherPids()
        let watcher = NSMenuItem(
            title: pids.isEmpty ? "监听：未运行" : "监听：运行中（pid \(pids[0])）",
            action: nil,
            keyEquivalent: ""
        )
        watcher.isEnabled = false
        menu.addItem(watcher)
        menu.addItem(.separator())

        if accounts.isEmpty {
            let empty = NSMenuItem(title: "账号库为空", action: nil, keyEquivalent: "")
            empty.isEnabled = false
            menu.addItem(empty)
        } else {
            for account in accounts {
                let mark = account.uid == activeUid ? "● " : "○ "
                let item = NSMenuItem(title: mark + account.menuTitle, action: #selector(switchAccount(_:)), keyEquivalent: "")
                item.target = self
                item.representedObject = account.uid
                item.state = account.uid == activeUid ? .on : .off
                item.isEnabled = !busy && account.uid != activeUid
                menu.addItem(item)
            }
        }

        menu.addItem(.separator())

        let carry = NSMenuItem(title: "切换并带走当前会话", action: nil, keyEquivalent: "")
        let carryMenu = NSMenu()
        for account in accounts where account.uid != activeUid {
            let item = NSMenuItem(
                title: account.menuTitle,
                action: #selector(switchWithCurrent(_:)),
                keyEquivalent: ""
            )
            item.target = self
            item.representedObject = account.uid
            carryMenu.addItem(item)
        }
        if carryMenu.items.isEmpty {
            let none = NSMenuItem(title: "没有其他账号", action: nil, keyEquivalent: "")
            none.isEnabled = false
            carryMenu.addItem(none)
        }
        carry.submenu = carryMenu
        menu.addItem(carry)
        menu.addItem(menuItem("撤销上次会话过继", #selector(undoHandover)))

        let hint = NSMenuItem(
            title: accounts.count < 2 ? "再登录一个账号，会自动收录" : "点账号即可切换",
            action: nil,
            keyEquivalent: ""
        )
        hint.isEnabled = false
        menu.addItem(hint)

        menu.addItem(menuItem("采集当前账号", #selector(snapshotCurrent)))
        menu.addItem(menuItem("重启 WorkBuddy 生效", #selector(restartApp)))
        menu.addItem(menuItem("刷新列表", #selector(refreshMenu)))
        menu.addItem(.separator())

        // 自动重试：模型繁忙（14003）中断后自动点「重试」
        let retry = AutoRetry.shared
        let retryItem = menuItem(retry.enabled ? "自动重试：开" : "自动重试：关", #selector(toggleAutoRetry))
        retryItem.state = retry.enabled ? .on : .off
        menu.addItem(retryItem)
        if retry.enabled {
            let detail: String
            if retry.trusted {
                detail = "已授权 · 已重试 \(retry.totalPresses) 次"
            } else {
                detail = "缺权限（勾了也不生效？点这里看修法）"
            }
            let sub = NSMenuItem(
                title: "  " + detail,
                action: retry.trusted ? nil : #selector(fixPermission),
                keyEquivalent: ""
            )
            sub.target = self
            sub.isEnabled = !retry.trusted
            menu.addItem(sub)
        }

        // 额度耗尽自动切下一个号（和上面「自动重试」是两件事：按钮样式不同）
        let rollover = Rollover.shared
        let roItem = menuItem(
            rollover.enabled ? "额度耗尽自动切号：开" : "额度耗尽自动切号：关",
            #selector(toggleRollover)
        )
        roItem.state = rollover.enabled ? .on : .off
        menu.addItem(roItem)
        if rollover.enabled {
            let total = loadAccounts().count
            let detail = total < 2
                ? "库里只有 \(total) 个号，切不了"
                : rollover.lastMessage.isEmpty
                    ? "待命 · 共 \(total) 个号"
                    : "\(rollover.lastMessage) · 共 \(total) 个号"
            let sub = NSMenuItem(title: "  " + detail, action: nil, keyEquivalent: "")
            sub.isEnabled = false
            menu.addItem(sub)
        }
        menu.addItem(.separator())

        let autoStart = menuItem(loginItemInstalled() ? "开机自启：已开启" : "开机自启：关闭", #selector(toggleLoginItem))
        autoStart.state = loginItemInstalled() ? .on : .off
        menu.addItem(autoStart)

        menu.addItem(menuItem("打开账号库", #selector(openVault)))
        menu.addItem(menuItem("查看监听日志", #selector(openLog)))
        menu.addItem(.separator())
        let version = NSMenuItem(title: "版本 \(buildLabel())", action: nil, keyEquivalent: "")
        version.isEnabled = false
        menu.addItem(version)
        menu.addItem(menuItem("退出", #selector(quit)))
    }

    func menuItem(_ title: String, _ action: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        return item
    }

    @objc func refreshMenu() {
        if let menu = statusItem.menu { menuNeedsUpdate(menu) }
    }

    @objc func snapshotCurrent() {
        runAsync(["snapshot"], successFlash: "已采集")
    }

    @objc func restartApp() {
        runAsync(["restart-app"], successFlash: "已重启")
    }

    @objc func switchAccount(_ sender: NSMenuItem) {
        guard let uid = sender.representedObject as? String else { return }
        runAsync(["switch", uid, "--wait", "3"], successFlash: "已切换")
    }

    @objc func switchWithCurrent(_ sender: NSMenuItem) {
        guard let uid = sender.representedObject as? String else { return }
        runAsync(["switch", uid, "--with-current", "--wait", "3"], successFlash: "已带走")
    }

    @objc func undoHandover() {
        runAsync(["handovers", "--undo"], successFlash: "已撤销")
    }

    @objc func toggleAutoRetry() {
        let retry = AutoRetry.shared
        retry.enabled.toggle()
        if retry.enabled, !retry.trusted {
            showPermissionAlert()
        }
        refreshMenu()
    }

    @objc func toggleRollover() {
        let rollover = Rollover.shared
        rollover.enabled.toggle()
        if rollover.enabled {
            showInfo(
                "开启后：遇到「已超出频率限制，可在 … 重置后使用」这种提示（按钮是"
                + "「消耗积分继续使用该模型 / 切换 Auto」），会自动切到库里的下一个号。\n\n"
                + "「模型忙，请稍后重试」那种不切号，还是按「重试」——那两件事按钮不一样。",
                title: "额度耗尽自动切号"
            )
        }
        refreshMenu()
    }

    @objc func openAccessibilitySettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    /// 菜单里那一行「缺权限」：先说清楚为什么，再给按钮去设置
    @objc func fixPermission() {
        showPermissionAlert()
    }

    @objc func toggleLoginItem() {
        let installed = loginItemInstalled()
        let (ok, message) = installed ? removeLoginItem() : installLoginItem()
        if !ok {
            showError(message.isEmpty ? "操作失败" : message, code: 1)
        } else if !message.isEmpty {
            showInfo(message)
        }
        refreshMenu()
    }

    func showInfo(_ message: String, title: String = "提示") {
        let alert = NSAlert()
        alert.alertStyle = .informational
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "好")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    /// 「设置里明明勾着，工具却说没权限」——系统授权绑的是这一份二进制，
    /// 工具重新编译后旧授权就作废了，但列表里的勾是另一条记录，所以还亮着。
    /// 光取消勾选再勾上没用：那只会改「允许/禁止」，不会重算指纹。
    /// 必须把整条记录删掉，让系统重新记一次。
    func showPermissionAlert() {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "自动重试还差一步授权"
        alert.informativeText = """
        现象：系统设置里 WBVault 明明是勾上的，工具却提示缺「辅助功能」权限。

        原因：macOS 的授权记的是「这一份二进制的指纹」。工具重新编译后指纹变了，
        旧记录就对不上；而列表里那个勾属于旧记录，所以看着还亮着。
        取消再勾选只是改「允许/禁止」，指纹不会重算，因此不管用。

        点下面这个按钮：我会把旧记录删掉，再让系统重新弹一次授权。
        到时候在列表里把 WBVault 勾上就行。
        """
        alert.addButton(withTitle: "重置并重新授权")
        alert.addButton(withTitle: "稍后")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        let (code, output) = shell("/usr/bin/tccutil", ["reset", "Accessibility", "ai.workbuddy.wbvault.menu"])
        if code != 0 {
            showInfo(
                "重置失败：\(output)\n\n"
                + "手动做法：系统设置 → 隐私与安全性 → 辅助功能，选中 WBVault，\n"
                + "点列表下方的「−」把它删掉，然后重新打开 WBVault，系统会再问一次。",
                title: "没能自动重置"
            )
            return
        }
        AutoRetry.shared.requestPermission()
        showInfo(
            "旧记录已清掉，系统马上会弹「WBVault 想要控制这台电脑」。\n\n"
            + "点「打开系统设置」，在辅助功能列表里把 WBVault 打开即可。\n"
            + "授权成功后菜单里会显示「已授权」，不用重启 App。",
            title: "现在去勾选 WBVault"
        )
    }

    func runAsync(_ args: [String], successFlash: String) {
        if busy { return }
        busy = true
        setStatusText("…")
        DispatchQueue.global(qos: .userInitiated).async {
            let (code, output) = runPython(args)
            DispatchQueue.main.async {
                self.busy = false
                if code == 0 {
                    self.setStatusText(successFlash)
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) {
                        self.setStatusText(nil)
                    }
                } else if code == 3 {
                    // 3 = 登录态写进去了，但 App 没热重载；只能重启才看得见
                    self.setStatusText(nil)
                    self.showRestartOffer(output)
                } else {
                    self.setStatusText(nil)
                    self.showError(output, code: code)
                }
                self.refreshMenu()
            }
        }
    }

    func setStatusText(_ text: String?) {
        guard let button = statusItem.button else { return }
        if let text {
            button.title = " " + text
            button.imagePosition = .imageLeading
        } else {
            button.title = ""
            button.imagePosition = .imageOnly
        }
    }

    /// argparse 失败时会先打一大段 usage，真正有用的只有最后那行 error。
    func tidyError(_ text: String) -> String {
        let lines = text
            .split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        if let index = lines.lastIndex(where: { $0.contains("error:") }) {
            return lines[index...].joined(separator: "\n")
        }
        return lines.suffix(10).joined(separator: "\n")
    }

    /// 退出码 3：文件写对了，但 WorkBuddy 没热重载 —— 给一个「现在重启」的按钮。
    func showRestartOffer(_ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "切换已写入，但 WorkBuddy 没有热重载"
        alert.informativeText = message + "\n\n重启 App 才会切过去（会结束当前会话）。"
        alert.addButton(withTitle: "现在重启")
        alert.addButton(withTitle: "稍后")
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            runAsync(["restart-app"], successFlash: "已重启")
        }
    }

    func showError(_ message: String, code: Int32) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "操作没有完成（退出码 \(code)）"
        let detail = tidyError(message)
        alert.informativeText = detail.isEmpty ? "没有更多信息，可查看监听日志" : detail
        alert.addButton(withTitle: "知道了")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    @objc func openVault() { NSWorkspace.shared.open(vaultRoot) }
    @objc func openLog() { NSWorkspace.shared.open(watchLog) }
    @objc func quit() { NSApp.terminate(nil) }
}

// MARK: - 命令行辅助（便于脚本化验证）

let arguments = CommandLine.arguments
if arguments.contains("--status") {
    print("app: \(Bundle.main.bundlePath)")
    print("version: \(buildDetail())")
    print("watcher: \(isWatcherRunning() ? "running" : "stopped")")
    print("loginItem: \(loginItemInstalled() ? "on" : "off")")
    print("accounts: \(loadAccounts().count)")
    print("autoRetry: \(AutoRetry.shared.enabled ? "on" : "off")")
    print("autoRollover: \(Rollover.shared.enabled ? "on" : "off")")
    if !Rollover.shared.lastMessage.isEmpty { print("rollover: \(Rollover.shared.lastMessage)") }
    exit(0)
}
if arguments.contains("--ax-quota") {
    // 额度提示在屏幕上时跑一次：按钮候选和文案候选分开列，看清楚到底凭什么触发
    for line in AutoRetry.shared.quotaDiagnostics() { print(line) }
    exit(0)
}
if arguments.contains("--ax-trusted") {
    print("trusted: \(AutoRetry.shared.trusted ? "yes" : "no")")
    exit(0)
}
if arguments.contains("--ax-test") {
    // 授权后跑一次：确认能读到页面里的「重试」按钮
    for line in AutoRetry.shared.diagnose() { print(line) }
    exit(0)
}
if arguments.contains("--ax-dump") {
    // 树结构不对时用它看清楚
    for line in AutoRetry.shared.dumpTree() { print(line) }
    exit(0)
}
if arguments.contains("--reset-permission") {
    // 删掉系统里那条「绑着旧二进制」的授权记录，让下次启动重新弹授权
    let (code, output) = shell("/usr/bin/tccutil", ["reset", "Accessibility", "ai.workbuddy.wbvault.menu"])
    print(code == 0 ? "permission: reset" : "permission: reset failed: \(output)")
    exit(code == 0 ? 0 : 1)
}
if arguments.contains("--menu") {
    // 只是把菜单会显示的文字打出来，方便核对显示逻辑
    let activeUid = loadActiveUid()
    for account in loadAccounts() {
        let mark = account.uid == activeUid ? "● " : "○ "
        print(mark + account.menuTitle)
    }
    exit(0)
}
if arguments.contains("--install-login-item") {
    let (ok, message) = installLoginItem()
    print(ok ? "loginItem: on" : "loginItem failed: \(message)")
    exit(ok ? 0 : 1)
}
if arguments.contains("--remove-login-item") {
    let (ok, message) = removeLoginItem()
    print(ok ? "loginItem: off" : "loginItem failed: \(message)")
    exit(ok ? 0 : 1)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
