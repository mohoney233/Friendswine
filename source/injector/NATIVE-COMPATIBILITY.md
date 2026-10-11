# 原生兼容性检查器（1.3）

`inspect-engine <neox_engine.dll>` 只读取 PE 文件，输出单个 JSON 对象。它不加载 DLL、不访问游戏进程、不调用游戏函数。失败返回退出码 1 和 `supported:false`。

成功结果包含 `schema:1`、`architecture:x64`、`machine:34404`、`image_size`、`engine_sha256`、`exports`、`executable_sections`、`native_fingerprint`。导出 RVA 为整数。`code_sha256` 是有明确边界的完整过程字节；SHA 均为小写十六进制。

## 指纹与兼容范围

- 必须存在三个本地、非转发的 CPython 导出；导出须位于唯一、由文件数据完整支持的可执行节。
- 常规过程使用 x64 异常目录的起止边界，不扫描 `RET`，也不把任意长度前缀当作完整函数。
- 唯一允许的无 unwind 入口是 `PyRun_SimpleString` 的明确 7 字节语法：`xor edx,edx; jmp rel32`。目标必须正好是具备唯一 unwind 边界的 `PyRun_SimpleStringFlags` 导出；目标完整过程也进入指纹。其他叶函数、未知指令序列、链式 unwind 均拒绝。
- 指纹同时覆盖所有可执行 PE 节的完整原始字节及大小，因此非关键入口之外的原生代码变化也会改变指纹。
- 指纹不包含 RVA 本身。各节代码完全相同的整体地址移动、非代码资源变化，可以保留原生兼容族；相对寻址机器码变化仍会保守地改变指纹。
- 可执行节内出现基址重定位时拒绝，避免把加载后的 ASLR 字节与磁盘原字节混淆。暂不对未知代码做指令重写或归一化。

匹配原生指纹只是进入既有适配器的条件；它不能证明任意未来游戏版本兼容。脚本接口、主线程调度、模型发现及恢复逻辑仍必须由 Python 层独立检查。

## 执行前约束

`raw` 必须同时收到以下由启动器根据已验证配置设置的环境变量：

```text
FUXIANG_GAME_DIR=<已验证游戏目录的绝对路径>
FUXIANG_EXPECTED_PID=<唯一的 dwrg.exe PID>
FUXIANG_EXPECTED_ENGINE_SHA=<inspect-engine 返回的文件 SHA256>
FUXIANG_EXPECTED_NATIVE_FINGERPRINT=<被选兼容配置的原生指纹>
FUXIANG_EXPECTED_CREATED_TIME=<可选：进程创建时间，UTC Unix 秒>
```

接入器重新检查唯一进程、实际 exe 路径、实际引擎模块路径、映像大小、文件 SHA 和原生指纹；保留进程句柄，避免 PID 复用。文件在检查和接入期间保持只读共享锁，阻止替换/写入。之后还要检查已加载映像是否与所读磁盘版本一致；通过后才分配或执行代码。三个函数的调用地址直接使用当次解析的 RVA，不再读取旧的 6604 项导出快照。

`verify-loaded <pid>` 使用相同前置约束，只进行已加载引擎的一致性检查，不分配或执行代码。头部中的 `OptionalHeader.ImageBase` 允许保留磁盘首选地址，或精确等于实际模块加载基址；这是对完整 8 字节值的校验，任意第三个地址都会拒绝。其他头部字段、全部导出/异常表、完整可执行节仍须逐字节一致。该规则来自当前引擎只读对比的实际证据：只有映像基址改变，其他受检区域完全一致。

`diagnose-loaded <pid>` 同样只读，输出各受检区域的摘要和有限差异样本，供分析一致性失败；它不会放宽执行检查。`verify-loaded-header <dll> <header-file> <module-base-hex>` 为纯文件离线测试入口，供检查合法 ASLR 值及伪基址拒绝路径，不访问进程。

旧 `verify-exports` / `selftest` 保留为历史导出快照的离线核对工具，其匹配结果不替代以上兼容性约束。

## 离线验证

`verification/test_native_update.py` 构造小型 PE 数据，不使用真实进程。覆盖整体 RVA 移动、资源变化、非关键代码变化、thunk 目标变化，以及损坏文件、缺失/转发导出、越界序号、未知叶函数、错误跳转、异常表重叠和代码重定位等拒绝路径。测试结果写入 `verification/native-update-tests.json`。
