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

退出码：`0` 成功 / `1` 失败（已回滚） / `2` 参数或账号不存在 / `3` 已写入但未确认。

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

