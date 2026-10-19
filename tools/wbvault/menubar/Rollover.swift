import AppKit
import Foundation

// 额度耗尽 → 自动切下一个号。
//
// 为什么要和「自动重试」分开：
//   两种提示长得像，但按钮不一样，能救回来的办法也不一样——
//   - 模型忙（按钮「重试」）：重试一下往往就过去了，这种别动账号；
//   - 额度耗尽（按钮「消耗积分继续使用该模型 / 切换 Auto」）：重试多少次都会被拒，
//     只能换号。
// 所以这里是「没有重试按钮的那一类」才走换号，按重试之前已经先排除掉了。
//
// 安全阀：
//   - 两次切换至少隔 30 秒（刚切过去那边还没开始跑，别急着再切）；
//   - 10 分钟内把库里的号都轮过一遍还没缓过来 → 停下来报「都耗尽了」，不再空转；
//   - 库里只有一个号 → 什么都不做。

final class Rollover {
    static let shared = Rollover()

    private let vaultRoot = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/WorkBuddyAccountVault")
    private let authFile = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(
            "Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop-ai.info"
        )
    private let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/wbvault-retry.log")

    private let cooldown: TimeInterval = 30
    private let cycleWindow: TimeInterval = 600
    private var lastSwitch: Date = .distantPast
    private var tried: [(uid: String, at: Date)] = []
    private var busy = false

    private(set) var totalSwitches = 0
    private(set) var lastMessage = ""

    private init() {
        // 默认关。切号是重动作（会打断当前任务），而额度提示的文案还没在真机上
        // 核实过关键词，宁可先不动手；用户在菜单里打开即可。
        UserDefaults.standard.register(defaults: ["autoRollover": false])
    }

    var enabled: Bool {
        get { UserDefaults.standard.bool(forKey: "autoRollover") }
        set { UserDefaults.standard.set(newValue, forKey: "autoRollover") }
    }

    // MARK: - 触发

    /// 页面上出现了「额度耗尽」提示。matchedText 是命中的原文，写日志用来核对关键词。
    func handleQuotaToast(matchedText: String) {
        guard enabled else { return }
        guard !busy else { return }
        let now = Date()
        guard now.timeIntervalSince(lastSwitch) >= cooldown else { return }

        let accounts = loadAccountUids()
        guard accounts.count >= 2 else {
            if lastMessage != "只有一个号，没法切" {
                lastMessage = "只有一个号，没法切"
                log("额度耗尽，但库里只有 \(accounts.count) 个号，切不了")
            }
            return
        }

        // 短时间已经轮过一圈 → 说明都耗尽了，别再空转
        tried = tried.filter { now.timeIntervalSince($0.at) < cycleWindow }
        if Set(tried.map { $0.uid }).count >= accounts.count {
            if lastMessage != "号都用完了" {
                lastMessage = "号都用完了"
                log("\(Int(cycleWindow / 60)) 分钟内已切过 \(tried.count) 次、覆盖全部 \(accounts.count) 个号，先停手")
            }
            return
        }

        guard let current = loadActiveUid() else { return }
        guard let next = nextUid(after: current, in: accounts) else {
            lastMessage = "找不到下一个号"
            return
        }

        busy = true
        defer { busy = false }
        log("检测到额度耗尽（原文：\(matchedText)），切到下一个号 \(short(next))")

        let script = vaultRoot.appendingPathComponent("runtime/wbvault.py")
        let (code, output) = shell("/usr/bin/python3", [script.path, "switch", next, "--wait", "2"])
        lastSwitch = Date()
        if code == 0 {
            totalSwitches += 1
            tried.append((uid: next, at: lastSwitch))
            lastMessage = "刚切到 \(short(next))"
            log("已切到 \(short(next))（第 \(totalSwitches) 次换号）")
        } else if code == 3 {
            // 登录态写进去了，但 App 没热重载 —— 开关里会提示「重启 WorkBuddy 生效」
            totalSwitches += 1
            tried.append((uid: next, at: lastSwitch))
            lastMessage = "已切号，但需重启 App 生效"
            log("切号已写入，但 App 没热重载：\(output.trimmingCharacters(in: .whitespacesAndNewlines))")
        } else {
            lastMessage = "切号失败（\(code)）"
            log("切号失败，退出码 \(code)：\(output.trimmingCharacters(in: .whitespacesAndNewlines))")
        }
    }

    /// 按顺序取下一个：跳过刚切过的，避免在两个号之间来回弹。
    private func nextUid(after current: String, in accounts: [String]) -> String? {
        guard let start = accounts.firstIndex(of: current) else {
            return accounts.first
        }
        let now = Date()
        let recent = Set(tried.filter { now.timeIntervalSince($0.at) < cycleWindow }.map { $0.uid })
        for step in 1...accounts.count {
            let candidate = accounts[(start + step) % accounts.count]
            if !recent.contains(candidate) { return candidate }
        }
        return nil
    }

    // MARK: - 数据

    private func loadAccountUids() -> [String] {
        guard let data = try? Data(contentsOf: vaultRoot.appendingPathComponent("accounts.json")),
              let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let items = root["accounts"] as? [[String: Any]] else { return [] }
        return items.compactMap { $0["uid"] as? String }
    }

    private func loadActiveUid() -> String? {
        guard let data = try? Data(contentsOf: authFile),
              let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let account = root["account"] as? [String: Any],
              let uid = account["uid"] as? String else { return nil }
        return uid
    }

    private func short(_ uid: String) -> String { String(uid.prefix(8)) }

    private func shell(_ path: String, _ args: [String]) -> (Int32, String) {
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
