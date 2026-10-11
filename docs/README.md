# 第五人格「朋友的酒」Q 弹模组 1.3.0

1.3.0 增加游戏更新检测、动态导出解析、逐能力脚本检查和纯数据适配包。兼容的资源、地址或代码行号变化可重新检查后继续使用；原生代码或关键脚本实现变化需要审核后的适配配置，有时需要更新程序。不能保证任意未来版本兼容。

## 使用

1. 完整解压 Windows-x64 安装包。包内自带 Python 3.14.0 和 .NET 8.0.30，不需要另外安装任何东西。
2. 双击 **`启动面板.cmd`**。这是模组唯一的入口，只会打开一个控制面板窗口。
3. 先启动游戏，进入角色时装展示页或匹配对局。
4. 回到面板点「启动」。首次执行需要管理员权限，确认一次后这个面板里不再重复询问。

模组是**解压即用**的：配置和数据写在解压目录内的 `data` 文件夹，不会写入 C 盘用户目录。不需要安装，也不需要卸载——不再使用时直接删掉整个文件夹即可。支持含中文和空格的路径。

## 控制面板

面板只有三个按钮：

| 按钮 | 作用 |
| --- | --- |
| 启动 | 启动持续效果（角色时装展示页 + 对局：训练场与匹配） |
| 停止 | 停止效果并把模型恢复原状；关闭游戏前请先点它 |
| 错误修复 | 依次执行：停止并恢复 → 清理残留状态 → 清理兼容缓存 → 重新诊断，并把结论写进「运行输出」 |

面板顶部实时显示阶段、目标数、兼容校验和数据目录；底部「运行输出」保留每一步动作的原始输出。状态会自动刷新：面板在前台时约每 3 秒一次，切到后台时放慢到约 9 秒。

## 出问题时

先点「错误修复」。它会先把模型恢复干净、清掉上一次会话的残留状态、清掉兼容缓存，再重新做一次只读环境诊断，并在「运行输出」里给出结论。常见结论：

- 提示需要管理员权限：面板会重新以管理员身份打开自己，确认一次即可。
- 提示没有检测到 `dwrg.exe`：先启动游戏并进入时装展示页、训练场或匹配对局。
- 提示没有匹配的已验证适配配置：说明这次游戏更新改变了已审核的原生实现或关键脚本，需要维护者审核并提供适配配置，有时还需要更新程序。
- 提示游戏实际加载的引擎与磁盘文件不一致：先重启游戏再试。

「启动」本身就包含完整的原生检查、只读脚本探针和逐能力核对，所以正常情况下不需要单独再做兼容性检查。

## 命令行

面板只是外壳：每个按钮最终都调用与命令行完全相同的那条动作，因此批量使用或脚本化仍然可行。以下命令在解压目录运行，使用包内 Python：

```bat
runtime\python.exe -B -X utf8 app\friendswine.py status
runtime\python.exe -B -X utf8 app\friendswine.py start --battle
runtime\python.exe -B -X utf8 app\friendswine.py stop
runtime\python.exe -B -X utf8 app\friendswine.py check-update
runtime\python.exe -B -X utf8 app\friendswine.py validate --battle
runtime\python.exe -B -X utf8 app\friendswine.py start-watch --battle
runtime\python.exe -B -X utf8 app\friendswine.py profiles
runtime\python.exe -B -X utf8 app\friendswine.py profile-import --file "D:\Downloads\reviewed-profile.json"
runtime\python.exe -B -X utf8 app\friendswine.py cache-clear
runtime\python.exe -B -X utf8 app\friendswine.py cleanup
```

游戏目录可通过唯一运行进程自动识别，也可显式指定：

```bat
runtime\python.exe -B -X utf8 app\friendswine.py configure --game "E:\游戏\第五人格\dwrg.exe"
```

## 效果边界

- 写入范围为角色时装展示页与对局世界：**匹配对局走一条已审核路径**，是否在对局中启用由使用者自己决定。
- **在匹配对局中使用会使账号面临违禁风险**，请自行判断并承担。不想承担风险时，只在角色时装展示页使用即可。
- 恢复成功要求 `STOPPED`，且 `modified`、`unresolved`、`pending_restores` 均为 0；恢复未确认时会保留记录并阻止重新接入，此时用「错误修复」（或命令行 `cleanup`）清理残留状态后即可重新启动。游戏更换、释放或隐藏导致模型尺度不可观测时，程序会先请求已验证的官方重建再退役该记录，不会把这类模型永久锁成未恢复。效果没有五分钟截止时间，换角色或时装后会等待并重新绑定模型。
- 用户此前确认所有求生者、监管者角色与时装效果均正常；这属于既有版本的人工验收，不代表穷举自动验证或未来版本承诺。
- 详细边界、状态和适配包格式见 [更新与适配说明](docs/UPDATE-COMPATIBILITY.md)。

## 文件与数据

解压目录里对用户可见的只有一个双击入口 `启动面板.cmd`；其余都是程序文件（`app`、`bin`、`runtime`、`panel.ps1`、`launch.ps1`、`licenses`、`docs`）。

配置、兼容包、状态和证据都放在解压目录内的 `data` 文件夹，并按解压路径隔离，不会写入 C 盘用户目录。系统 UAC 使用另一管理员账号时，数据目录仍由解压路径决定，不受账号影响。

详见 [设计](docs/DESIGN.md)、[项目总结](docs/PROJECT-SUMMARY.md)、[验证记录](docs/VALIDATION.md)。运行依赖许可位于 `licenses`，包内不含游戏程序、模型、账户数据或参考视频音乐。
