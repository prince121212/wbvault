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
| `list` | 列出账号（`*` 为当前） |
| `current` | 当前账号详情 |
| `snapshot [--alias 名字]` | 收录当前登录态 |
| `switch <uid\|前缀\|别名\|uin>` | 切换账号 |
| `switch <账号> --with-current` | 切换并把当前会话过继过去 |
| `handover <会话\|current> --to <账号>` | 只过继会话，不切登录态 |
| `handovers [--undo]` | 查看/撤销过继 |
| `sessions [--all]` | 各账号的会话与空间归属（只读） |
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
- 长期不用的账号 refresh token 可能失效，需重新登录。
- 非官方工具，WorkBuddy 升级后内部路径可能变化，先跑 `doctor` 确认。
