# Windows weixin gateway auto-recovery

Hermes 的微信（iLink）网关在 `hermes update` 之后经常起不来：更新过程会削坏
hermes venv 的依赖（典型症状 `module 'aiohttp' has no attribute 'ClientSession'`），
网关把 weixin 平台从重试队列里移除、永不再试 → 需要**修 venv + 重启网关**才恢复。

本目录提供一套一键部署的 Windows 自愈方案：

| 文件 | 作用 |
|---|---|
| `venv_integrity.py` | 探测 11 个微信关键依赖；坏了自动修（wheel 只补缺失文件 → `uv pip install` → PyPI 下 wheel 三级阶梯，绝不覆盖锁定文件） |
| `weixin_watchdog.py` | 看门狗主逻辑（venv 检查/修复/网关重启 + agent.log 增量扫描 + 节流/升级告警） |
| `weixin_watchdog_launcher.bat` | 计划任务入口（日志重定向到 `%HERMES_HOME%\weixin\watchdog.stdout.log`） |
| `deploy_weixin_watchdog.ps1` | 一键部署：拷脚本 + 建计划任务 `\Hermes_WeixinWatchdog`（每 10 分钟）+ 初始化状态 |

## 无窗口运行
计划任务动作是 `wscript.exe //B //Nologo WeixinWatchdog.vbs`（隐藏 cmd 风格 0 启动 bat）。**不要**把任务动作直接设成 `cmd.exe /c ...bat`——交互登录方式下每 10 分钟会在桌面闪一个控制台窗口。

## 部署（一次性）

```powershell
# HERMES_HOME 默认 E:\BACK-AI\Hermes-win，可用 -HermesHome 覆盖
powershell -ExecutionPolicy Bypass -File .\deploy_weixin_watchdog.ps1
```

验证：

```cmd
schtasks /run /tn \Hermes_WeixinWatchdog
type %HERMES_HOME%\weixin\watchdog.stdout.log
```

健康环境下一轮 tick 应只输出一行 `OK: all 11 deps healthy (0 repaired this run)`，
不碰网关。

## 每轮 tick 做什么

1. `venv_integrity.py --check`：11 个依赖哨兵属性级探测。
   坏了 → 自动修复 → 成功即 `hermes gateway restart`（新 venv 必须新进程才生效）。
2. 增量扫描 `%HERMES_HOME%\logs\agent.log`（**只认 `gateway.run:` 行**，防止 agent
   自己的工具输出里带出的同名字样造成误报）：
   - `removing from retry queue` / `no connected platforms ... weixin` → 网关放弃微信 → 重启
   - `weixin connected` → 复位失败计数
3. 节流：5 分钟内最多重启 1 次；连续 4 次失败仍不重连 → 写
   `%HERMES_HOME%\weixin\ATTENTION.txt` 并停止自动动作（需要人工检查 iLink 凭证/网络）。

并发保护：`watchdog.lock`（900s 过期，tick 结束释放）。

## 手动运维

```cmd
:: 手动查 venv 健康
python %HERMES_HOME%\hermes-agent\venv\Scripts\python.exe ..\..\weixin\venv_integrity.py --check
:: 手动跑一轮看门狗（--no-restart 只检查+修复，不动网关）
python ...weixin_watchdog.py --no-restart
:: 卸载计划任务
schtasks /delete /tn \Hermes_WeixinWatchdog /f
```

## 明确不进本目录的东西（安全）

- `weixin\accounts\*.json`（iLink bot 凭证/context-tokens/sync）——不进库
- `ima_credentials.ini`、`.env`、wheels 缓存、state/lock/log 运行时文件——不进库
- 这些留在 `%HERMES_HOME%\weixin\` 本地目录，计划任务与状态都写在那里

## 升级 Hermes 时的建议动作（不变）

`hermes update` 完成后顺手 `hermes gateway restart`（秒级恢复）；看门狗是双保险，
最坏情况 10 分钟内自动把微信接回来。

## 已知坑（来自 2026-09-16 实操）

- `hermes update` 削坏 venv 的两种形态：顶层 `__init__.py` 丢失（模块变成空 namespace，
  import 成功但属性缺失）与整包消失。`venv_integrity.py` 两种都覆盖。
- 网关"放弃平台"的日志原文：`Reconnect weixin: no bot credential on queued config,
  removing from retry queue`（`gateway/run_adapters.py`）。重连被永久取消，所以**必须重启
  网关进程**才能重新入队。
- venv 被桌面端进程锁住 `.pyd` 时，**不要 `uv --reinstall`**——只补缺失的纯 Python
  文件即可（本脚本的 wheel 阶梯天生不会覆盖已存在文件）。
