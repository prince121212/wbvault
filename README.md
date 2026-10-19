# wbvault

WorkBuddy AI 的本地多账号切换工具（macOS）。登录过的账号留在本机，一条命令切换，不重启、不重新登录。

## 环境

- macOS 13+
- Python 3.9+（系统自带即可）
- WorkBuddy AI 已登录
- 编译菜单栏需要 `xcode-select --install`

## 启动

菜单栏：双击 `tools/wbvault/WBVault.app`，右上角出现人形图标。

命令行：

```bash
cd tools/wbvault
python3 wbvault.py <命令>
```

## 首次使用

```bash
python3 wbvault.py install                # 常驻监听：登录态一变就采集
python3 wbvault.py snapshot --alias 主号   # 收录当前账号
```

然后正常登出、登录另一个账号，会自动收录。`list` 出现两个账号后即可切换。

## 命令

| 命令 | 作用 |
|---|---|
| `list` | 列出账号（`*` 为当前，带邮箱） |
| `current` | 当前账号详情 |
| `snapshot [--alias 名字]` | 收录当前登录态 |
| `switch <uid\|前缀\|别名\|邮箱\|uin>` | 切换账号 |
| `switch <账号> --with-current` | 切换并把当前会话过继过去 |
| `switch <账号> --restart` | 切换；App 没热重载时自动重启它 |
| `switch <账号> --no-restart` | 只写文件，不自动重启（默认会重启） |
| `restart-app` | 重启 WorkBuddy AI，让写入的登录态生效 |
| `config [auto-restart on\|off]` | 查看/修改「热重载失败时自动重启」开关 |
| `handover <会话\|current> --to <账号>` | 只过继会话，不切登录态 |
| `handovers [--undo]` | 查看/撤销过继 |
| `sessions [--all]` | 各账号的会话与空间归属（只读） |
| `alias <账号> <名字>` | 起别名 |
| `email <账号> <邮箱>` | 手动记邮箱（留空清除） |
| `watch [--once]` | 常驻采集 |
| `doctor` | 自检 |
| `restore` | 用最近备份还原登录态 |
| `install` / `uninstall` | 安装/卸载常驻监听 |
| `prune --keep 12` | 清理旧备份 |

全局参数：`--json`（机器可读输出）、`--vault DIR`、`--auth-file PATH`、`--no-color`。
`switch` 支持 `--dry-run`、`--wait N`、`--force`。

## Agent 调用

```bash
python3 wbvault.py --json list
python3 wbvault.py --json switch 主号
python3 wbvault.py --json switch 主号 --with-current
python3 wbvault.py --json handovers --undo
```

退出码：`0` 成功 / `1` 失败（已回滚） / `2` 参数或账号不存在 / `3` 已写入但未确认（`--json` 里 `needsRestart` 为 `true` 时表示要重启 App）。

## 路径

| 用途 | 路径 |
|---|---|
| 登录态（唯一真相） | `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop-ai.info` |
| 账号库 | `~/Library/Application Support/WorkBuddyAccountVault/` |
| 会话库 | `~/.workbuddy-ai/workbuddy.db` |
| 采集日志 | `~/Library/Logs/wbvault-watch.log` |

## 限制

- 快照里的 token 用本机 `keyblob` 加密，**机器绑定**，不可跨机迁移。
- 会话按 `user_id` 隔离；切换不删除会话，只是侧边栏换一批。
- 过继只支持 `transport=local` 的会话，且是移动不是复制。
- 过继会把会话的「最近活动」更新为当前时间，所以它排在侧边栏最上面；但 App 热切换后打开哪一页由 App 自己决定（外部只能写 `startup-route.json`，App 不重读）。重启 App 会直接落在该会话。
- 热切换依赖 App 自己的目录监听，它会静默失效且无法从外部唤醒；失效时只能重启 App（工具默认会自动做，见「排错」）。
- 长期不用的账号 refresh token 可能失效，需重新登录。
- 非官方工具，WorkBuddy 升级后内部路径可能变化，先跑 `doctor` 确认。

## 账号显示名（邮箱）

菜单栏和 `list` 优先显示邮箱，其次别名，最后 uid 前 8 位。

登录态文件里的 `nickname` 是密文，明文那份在：

```
~/.workbuddy-ai/storage/skeleton/account-snapshot.json   # {"primary": {"uid": ..., "nickname": "xxx@gmail.com"}}
```

这份只保存**当前**登录账号，所以：切到某个账号一次，常驻监听就会把它的邮箱记进 `accounts.json`。
从没切过的账号（或想立刻改）用 `wbvault email <账号> <邮箱>` 手动记一次即可。

## 菜单栏与运行时副本

菜单栏 App 不直接跑仓库里的脚本，而是跑 `~/Library/Application Support/WorkBuddyAccountVault/runtime/` 里的副本（launchd 无法可靠执行外接卷上的脚本）。副本旁边有个 `.stamp` 指纹：

- App 构建时会把源码打进 `WBVault.app/Contents/Resources/runtime-src/`，启动时指纹不一致就整份覆盖副本；
- 从仓库直接跑 `python3 wbvault.py <任意命令>` 时也会顺手校准副本；
- `doctor` 里的「运行时副本同步」一项可以确认两者一致。

## 更新到最新版

仓库里**提交了编译好的 `WBVault.app`**（二进制 + 内置源码 + 签名），所以 clone 下来双击就能用，不需要装 Xcode。但 App 不会自己热更新，更新要手动三步：

```bash
# 1. 先退出菜单栏 App（点图标 → 退出），别让它边跑边被覆盖
# 2. 拉最新代码
git pull
# 3. 重新打开
open tools/wbvault/WBVault.app
```

为什么要退出再开：运行中的进程用的是已经加载进内存的旧二进制；Python 那部分（内置的 `runtime-src`）也只在 App 启动时才校准到 `~/Library/Application Support/WorkBuddyAccountVault/runtime/`。

**核对是不是最新**：菜单最下面一行「版本 `05883e3` @ 2026-10-19 16:19」，或

```bash
tools/wbvault/WBVault.app/Contents/MacOS/WBVault --status
```

带 `-dirty` 表示那次构建时工作区有未提交的改动，不是干净的发布版。

**如果是下载 ZIP 而不是 clone**：解压出来的文件带 quarantine 标记，首次打开会被系统拦住（「无法验证开发者」或「已损坏」）。二选一：

```bash
xattr -dr com.apple.quarantine tools/wbvault/WBVault.app
```

或到「系统设置 → 隐私与安全性」底部点「仍要打开」。

其他注意：

- **辅助功能授权是每台机器各自的事**，不是跟着代码走的。自动重试要用，第一次得自己在设置里勾一下（见上一节）。授权记录现在认 bundle id，之后更新不会再掉。
- 仓库里的二进制是 **arm64**（Apple Silicon）。Intel Mac 需要自己编译：改 `menubar/build.sh` 里的 `-target` 后执行它。
- 系统要求 macOS 13+。

## 排错

**「操作没有完成（退出码 2）… wbvault: error: unrecognized arguments: --with-current」**

菜单栏调的运行时副本是旧版本（副本只在 `install` 或 App 启动时刷新）。修：

```bash
cd tools/wbvault
python3 wbvault.py doctor      # 看「运行时副本同步」是否为 ✓
python3 wbvault.py install     # 强制刷新副本 + 重装常驻监听
```

重新编译 App 也会带上最新源码：`tools/wbvault/menubar/build.sh`。

**切换带会话时提示「会话未过继」**：会话已属于目标账号、或属于云端会话（`transport≠local`）时会跳过过继但**照常切换登录态**；写库失败则中止切换，避免会话看起来丢了。用 `wbvault handovers` 看过继记录，`--undo` 可撤回。

**提示「登录态已写入，但 App 没有热重载」（退出码 3）**

App 靠自己的目录监听（`fs.watch` 监听 `…/Public/auth/`）发现外部改动，再重新读文件。这个监听会**静默失效**：它不报错、不重试，App 内部还停在旧账号，只有重启才会走 `restore()` 重新读文件。

实测过：失效之后，`os.utime`、原地覆写、在目录里增删文件都唤不醒它——**外部无法重建监听，唯一入口就是重启 App**。

所以默认行为是「切完自动兜底」，不用手动点：

1. 写文件 → 校验字节；
2. 核对 App 日志（`~/.workbuddy-ai/logs/<日期>/workbuddyMainThread*.log` 里的 `Restored auth session from file, userId: …`）确认真的换了账号；
3. 没换 → 自动退出并重开 App，等它加载到目标账号再报成功（`verifiedBy: restart`）。

细节：

- 同一个 App 进程上次就失效过时，会记住（`reload-state.json`），下次直接走重启，不再白等；
- **从 App 内部调用**（Agent / 内置终端）不会自动重启——那会把正在跑命令的 App 一起掐掉，此时只报 `pending`，去菜单栏点「重启 WorkBuddy 生效」；
- 不想自动重启：`wbvault config auto-restart off`，或单次 `switch <账号> --no-restart`；
- 想随时看监听还灵不灵：`wbvault doctor --probe`（只动 mtime，不改内容）。

## 自动重试（模型繁忙 14003）

模型请求量过大时，App 会弹出「当前模型请求量过大，请稍后重试」并中断这一轮，要人手点「重试」。菜单栏 App 默认开启自动重试：看到这个提示就替你按下「重试」，提示消失、回答接着往下写，全程不动鼠标、不抢焦点。

**为什么必须走「辅助功能」**：那个「重试」是页面里的按钮，App 没有对外接口（实测本机没有监听端口，也没有可调用的 IPC），外部唯一和「人点一下」等价的办法就是辅助功能 API 按下它。所以需要一次「隐私与安全性 → 辅助功能」授权。

**怎么判断该动手**：不常驻扫描页面（几百个节点，太费）。用日志当触发器——每次 14003 都会在 `~/.workbuddy-ai/logs/<日期>/<空间>.log` 写 `refusal classified: … bizCode=14003`，看到新的才切到 3 秒一查，平时 15 秒兜底。安全阀：必须同时看到「重试」按钮和错误提示特征才按，两次至少隔 8 秒，一小时最多 40 次。动作日志在 `~/Library/Logs/wbvault-retry.log`。

### 授权「明明勾了却说没权限」

macOS 记的不是「这个 App」，而是**这一份二进制的指纹**。重新编译后指纹变了，旧记录对不上；而设置列表里那个勾属于旧记录，所以还亮着，看着像系统在骗人。

关键：**取消勾选再勾上没用**，那只改「允许/禁止」，指纹不会重算（实测记录里的 `csreq` 仍是旧 cdhash）。

正确修法（菜单栏里一键）：

1. 点菜单里的「缺权限（勾了也不生效？点这里看修法）」；
2. 选「重置并重新授权」——它会执行 `tccutil reset Accessibility ai.workbuddy.wbvault.menu` 把旧记录整条删掉，再让系统重新弹授权；
3. 系统弹「WBVault 想要控制这台电脑」→ 打开系统设置 → 在辅助功能列表里勾上 WBVault。

也可以自己在终端跑：

```bash
tccutil reset Accessibility ai.workbuddy.wbvault.menu
```

然后重启菜单栏 App，让它重新申请。

`build.sh` 里签名时显式写了固定的设计要求（`designated => identifier "ai.workbuddy.wbvault.menu"`），目的就是让授权认 bundle id 而不是某一份二进制；但 ad-hoc 签名下系统是否真的按这个记，取决于 macOS 版本，重编译后请用菜单里的状态行确认一下（显示「已授权 · 已重试 N 次」才算数）。

### 限制

- 只能按页面上的「重试」，不会替你换模型（换模型等于改你的选择）；
- 提示还没渲染出来就按不到，所以日志触发后要等下一次扫描（最多 3 秒）；
- 权限失效时不会静默失败：菜单里会显示「缺权限」，启动时也会弹一次说明。

