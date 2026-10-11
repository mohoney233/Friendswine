# Friendswine 1.3.0 源码包

包含完整控制器、展示／训练适配器、更新检查与配置包实现、启动和安装脚本、原生接入器源码、测试及文档。不含游戏文件、账户状态、第三方参考模组和视频音乐。

在 Windows Python 执行 python -B tests/run_tests.py 运行 151 项单元检查，不接入游戏。安装边界检查单独执行 python -B verification/test_installer_edges.py，只创建隔离夹具并运行卸载预检。原生模拟检查见 verification/test_native_update.py，须先构建接入器，或通过 FW_TEST_INJECTOR 指定已有接入器；夹具不接入游戏。

重建需要 Windows x64、Python 和 .NET SDK 8.0.424。执行 python -B build.py；可用 --python-zip 指定官方 Python 3.14.0 embeddable ZIP，--out 指定不存在的输出目录。脚本校验固定运行环境摘要，发布自包含 .NET 8.0.30，生成文件清单并执行安装预检。

1.3 动态解析 PE 导出，按已审核原生实现与 12 项脚本实现检查能力。纯数据适配包不能自动信任未知实现；缓存不能跳过现场探针。限定的 NeoX Python 3.11 执行状态位实验说明保存在 verification/runtime-seed-rationale.md；默认保留其他 flags。当前写入范围仍为展示和 WorldBattleTrain。

设计与操作说明在 templates/docs 和 templates/README.md。用户直接使用 Windows-x64 成品包，无需开发工具。接入器源项目未提供独立分发许可证，本包不声称其适用 MIT／Apache；第三方运行环境许可见 templates/licenses。

原始研究观察含游戏字节码，不随源码包分发；本包保留限定状态位实验的摘要证据和基准指纹，运行时规范化行为可由单元测试复核。
