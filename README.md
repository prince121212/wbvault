# wbvault

WorkBuddy AI 的本地多账号切换工具（macOS）。登录过的账号留在本机，一条命令切换，不重启、不重新登录。

切换默认是**无感**的：切过去侧边栏不换一批，本机所有会话都还在（见「账号无感切换」）。

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

切过去侧边栏不会换一批：默认开着「切换时保留全部会话」，本机所有会话都在（见「账号无感切换」）。

## 命令

| 命令 | 作用 |
|---|---|
| `list` | 列出账号（`*` 为当前，带邮箱） |
| `current` | 当前账号详情 |
| `snapshot [--alias 名字]` | 收录当前登录态 |
| `switch <uid\|前缀\|别名\|邮箱\|uin>` | 切换账号（默认把本机所有会话一起带过去） |
| `switch <账号> --no-share` | 切换但不共享：目标账号只看得到自己的会话 |
| `switch <账号> --no-keep-route` | 切完让 App 自己决定打开哪一页 |
| `switch <账号> --restart` | 切换；App 没热重载时自动重启它 |
| `switch <账号> --no-restart` | 只写文件，不自动重启（默认会重启） |
| `restart-app` | 重启 WorkBuddy AI，让写入的登录态生效 |
| `config` | 查看设置（auto-restart / share / keep-route） |
| `config <键> on\|off` | 改设置，键为 `auto-restart`、`share`、`keep-route` |
| `share [--to 账号]` | 把本机所有会话共享给某个账号（默认当前账号） |
| `unshare` | 按记录把会话还回各自的原始账号 |
| `spaces [--all]` | 查看会话空间（按本地目录分组），标出同名冲突 |
| `spaces --merge-same-name [--yes]` | 合并「目录名相同、路径不同」的空间（默认只演练） |
| `spaces --undo` | 撤销最近一次空间合并 |
| `handover <会话\|current> --to <账号>` | 只把某一条会话过继过去，不切登录态 |
| `handovers [--undo]` | 查看/撤销单条过继 |
| `sessions [--all]` | 各账号的会话、空间与原始归属（只读） |
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
python3 wbvault.py --json share --to 主号
python3 wbvault.py --json unshare
python3 wbvault.py --json handovers --undo
```

退出码：`0` 成功 / `1` 失败（已回滚） / `2` 参数或账号不存在 / `3` 已写入但未确认（`--json` 里 `needsRestart` 为 `true` 时表示要重启 App）。

`switch --json` 里多一段 `share`，报告这次共享了几条会话、原来分别属于谁。

## 账号无感切换（会话共享）

目标：账号 A 有会话 1、2，账号 B 有会话 3、4；A 切到 B 之后，1、2、3、4 都在侧边栏里，而不是「换一批」。

**原理**（从 App 自己的 SQL 反推出来的）：决定「谁看得见某个会话」的只有 `workbuddy.db` 里 `sessions.user_id` 一个字段——

```sql
WHERE transport = 'local' AND deleted_at IS NULL
  AND (user_id = ? OR user_id = '')
```

而消息本体在 `projects/<目录名>/<sessionId>.jsonl`，**不带账号**。所以把本机所有本地会话的 `user_id` 归到目标账号，切过去就是「会话全在」，一条都不用复制，历史一个字不少。

具体做法：

1. 切换前先把本机所有 `transport=local` 的会话归到目标账号，原始归属记进 `ownership.json`；
2. 再写登录态。顺序不能反：App 收到登录态变更会立刻重读侧边栏，写晚了这一轮看不到新会话（得再触发一次刷新）；
3. 登录态没切成（回滚/出错）就把会话归位回原账号，否则原账号侧边栏会空；
4. 顺手把 `startup-route.json` 抄给目标账号，切完（以及重启后）还停在同一页。

默认就开着（`wbvault config share on`）。不想要：`wbvault config share off`，或单次 `switch <账号> --no-share`。

- 云端会话（`transport != 'local'`）一律不碰，它们跟着服务端走；
- 单条过继 `handover` 仍然可用，但共享模式下没必要——它反而会把那条会话变回「私有」。

### 空间（工作区）

空间在 App 里就是**会话按 `cwd` 分组**，所以：

- **同一个本地目录**（两个账号打开的是同一个文件夹）→ 共享后自动并成一个空间，会话并排出现，不用做任何事；
- **目录名相同、路径不同**（例如 `/a/proj` 和 `/b/proj`）→ 会显示成两个同名空间。`wbvault spaces` 会标出来，`wbvault spaces --merge-same-name` 可以把它们并到保留的那个路径上（会话的 `cwd` 和 `projects/` 下的记录一起搬，默认只演练，加 `--yes` 才动手，`spaces --undo` 可撤回）。

注意：合并同名空间等于把会话挂到另一个目录上，之后这些会话就在那个目录里干活。只有确认两个目录是同一份项目时才值得合并。

### 还原

```bash
python3 wbvault.py unshare           # 全部按原始归属还回去
python3 wbvault.py unshare --dry-run # 先看会动哪些
```

## 路径

| 用途 | 路径 |
|---|---|
| 登录态（唯一真相） | `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop-ai.info` |
| 账号库 | `~/Library/Application Support/WorkBuddyAccountVault/` |
| 会话库 | `~/.workbuddy-ai/workbuddy.db` |
| 会话消息 | `~/.workbuddy-ai/projects/<目录名>/<sessionId>.jsonl`（不带账号） |
| 原始归属记录 | `~/Library/Application Support/WorkBuddyAccountVault/ownership.json` |
| 采集日志 | `~/Library/Logs/wbvault-watch.log` |

## 限制

- 快照里的 token 用本机 `keyblob` 加密，**机器绑定**，不可跨机迁移。
- 共享会改写 `sessions.user_id`（原始归属另存 `ownership.json`，`unshare` 可还原）。App 升级后如果改了可见性规则，先跑 `doctor` 看「会话共享」一项。
- 过继/共享只对 `transport=local` 的会话有效；云端会话跟服务端走，切账号时本来就是各看各的。
- 过继会把会话的「最近活动」更新为当前时间，所以它排在侧边栏最上面；共享不动时间戳。但 App 热切换后打开哪一页由 App 自己决定（外部只能写 `startup-route.json`，App 不重读）。重启 App 会直接落在该会话。
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

**核对是不是最新**：菜单最下面一行「版本 2026-10-19 16:19」，或

```bash
tools/wbvault/WBVault.app/Contents/MacOS/WBVault --status
```

菜单里只报**构建时间**——判断新旧看它最准（`pull` 完重新打开，时间变了就是更新到了）。`--status` 里还能看到构建时的 commit 和内置源码指纹，方便对不上时排查。commit 会比「发布那次提交」慢一拍，因为发布流程是先编译、后提交，这是正常的。

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

**切换后侧边栏还是只有目标账号自己的会话**：确认共享是开着的（`wbvault config`，或菜单里「切换时保留全部会话」有没有勾）。关着的话单次 `wbvault share --to <账号>` 也能补上。共享只对本地会话生效，云端会话各看各的是正常的。

**切换带会话时提示「会话未过继」**：这是老的单条过继（`--with-current`）。会话已属于目标账号、或属于云端会话（`transport≠local`）时会跳过过继但**照常切换登录态**；写库失败则中止切换，避免会话看起来丢了。共享模式下不需要它，用 `wbvault share` / `unshare` 代替。

**「目标快照校验失败（sha256 不一致），已拒绝切换」（退出码 1）**

`accounts.json` 里的 `sha256` 是**派生数据**，事实是 `snapshots/<uid>.info` 和同目录的 `.meta.json`。常驻监听（watch）是个长命进程，如果它把索引缓存在内存里不重读，每次采集都会把别人写进去的更新整份覆盖回去——于是文件是新的、索引还是旧 sha，切换就被自己的校验拦下了。

先体检再修（只改 `accounts.json`，不碰登录态、不切号）：

```bash
python3 wbvault.py repair --dry-run   # 看哪些账号「索引过期 / 快照损坏」
python3 wbvault.py repair             # 按文件 + meta 把索引对齐
```

判定规则：文件 = 索引 → 正常；文件 = meta ≠ 索引 → 只是索引过期，自动修好继续用；文件与两者都不一致 → 快照真坏了，需要重新登录该账号再采集。

同时注意**过继顺序**：`switch --with-current` 现在先校验目标快照、再改会话归属。反过来的话，会出现「会话已经过继到目标账号、登录态却没切过去」，用户会以为会话丢了。

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

### 额度耗尽自动切号（默认关）

和上面「自动重试」是两件事，别混：

| | 提示样子 | 按钮 | 动作 |
|---|---|---|---|
| 模型忙 | 当前模型请求量过大，请稍后重试 | **重试** / 换模型 | 按「重试」 |
| 额度耗尽 | 已超出频率限制，可在 … 重置后使用 | **消耗积分继续使用该模型** / **切换 Auto** | 换号 |

额度耗尽那一条**没有「重试」按钮**，按多少次都会被拒，只能换号。菜单里「额度耗尽自动切号：开」打开后，检测到就按 `accounts.json` 的顺序切到下一个号，循环往下走。

**默认关**：切号会打断当前任务，而这条提示的关键词还没在真机上核实过，先不动手为宜。

**一个踩过的坑**：判断必须落在**可按的按钮**上，不能只看文字。聊天正文里讨论「切换 Auto / 消耗积分」这句话本身就含这些词，只看文字会把聊天当成提示、白切好几次号。现在的实现要求命中按钮（`canPress`）+ 标签含 `消耗积分` / `切换 Auto` / `切换至其他模型`；文字只是补充证据。

想核对它到底命中了什么（提示还挂在屏幕上时跑）：

```bash
tools/wbvault/WBVault.app/Contents/MacOS/WBVault --ax-quota
```

它会把「可按按钮命中」和「静态文案命中」分开列出来。只要前者是 0，就不会触发切号。

安全阀：两次切换至少隔 30 秒；10 分钟内把库里所有号都轮过一遍还没缓过来就停下报「号都用完了」；库里只有一个号则什么都不做。每次动作都写进 `~/Library/Logs/wbvault-retry.log`。

### 限制

- 只能按页面上的「重试」，不会替你换模型（换模型等于改你的选择）；
- 提示还没渲染出来就按不到，所以日志触发后要等下一次扫描（最多 3 秒）；
- 权限失效时不会静默失败：菜单里会显示「缺权限」，启动时也会弹一次说明。

