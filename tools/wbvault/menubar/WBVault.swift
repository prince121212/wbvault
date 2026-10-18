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
        menu.addItem(menuItem("刷新列表", #selector(refreshMenu)))
        menu.addItem(.separator())

        let autoStart = menuItem(loginItemInstalled() ? "开机自启：已开启" : "开机自启：关闭", #selector(toggleLoginItem))
        autoStart.state = loginItemInstalled() ? .on : .off
        menu.addItem(autoStart)

        menu.addItem(menuItem("打开账号库", #selector(openVault)))
        menu.addItem(menuItem("查看监听日志", #selector(openLog)))
        menu.addItem(.separator())
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

    func showInfo(_ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .informational
        alert.messageText = "开机自启"
        alert.informativeText = message
        alert.addButton(withTitle: "好")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
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
    print("watcher: \(isWatcherRunning() ? "running" : "stopped")")
    print("loginItem: \(loginItemInstalled() ? "on" : "off")")
    print("accounts: \(loadAccounts().count)")
    exit(0)
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
