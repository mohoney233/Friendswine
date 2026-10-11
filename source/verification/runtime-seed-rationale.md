# 运行时兼容基准选择依据

日期：2026-10-09。输入为同目录 `runtime-observed.json`，输出为 `runtime-seed.json`。`make_runtime_seed.py` 从观察结果提取固定的审核集合，并调用发行版严格 schema 验证器验证，未把观察到的全部函数自动加入信任集合。

本文件描述既有用户人工确认模组效果的游戏版本所对应的脚本实现基准。最初采样是新游戏进程在 `WorldCityHall` 的只读采样，只能证明这些模块、函数和原生接口当时已经加载；后续在 `WorldBattleTrain` 进行了额外的只读指纹检查和解释器标志实验。上述采样不是 1.3 发行版的展示或训练场缩放实机验收，也不证明未来任意游戏版本兼容。原生指纹由主代理另行组装，本 JSON 仅是 runtime 部分，不属于可直接安装的完整适配包。

## 展示能力：8 个函数

| 函数 | 选择原因 |
| --- | --- |
| `WorldArticle.set_model_transform` | 反汇编确认其对有效模型写入由同一数值构成的三轴向量，是展示模型初始缩放的直接入口。 |
| `WorldArticle._add_role_model_cb` | 加载回调调用上述变换，并将新对象记录为 `player_model`；涉及模型世代切换及监管者额外变换。该函数中的新时装特例改变也可能要求重新审核，不能根据名称猜测其无害。 |
| `WorldArticle.clear_old_player_model` | 负责旧主模型销毁及引用回收，对换装时的所有权判断直接相关。 |
| `WorldArticle.set_cloth_id` | 写入 `cloth_id` 并生成 `show_cloth_id`，适配器以这些字段与当前选中项的一致性作为绑定条件。 |
| `WorldArticle.generate_show_cloth_id` | 生成实际展示时装 ID，可能按时装数据产生别名；适配器保留别名不一致时等待的行为。 |
| `WorldArticle.get_player_model` | 反汇编确认主模型获取路径为 `player_model`，作为数据接口特征。模组仍直接读取已观察字段，不调用该函数。 |
| `UIArticleRole.refresh_world_by_data` | 方法元数据表明它将当前展示配置传给 `article_world.refresh_by_data`，用于发现展示数据通路的实现变化。 |
| `UIArticleRole.update_item_on_selected` | 方法元数据包含当前选择的 `selected_article_id` 更新，直接关系选中时装与模型世代的一致性。 |

排除 `UIArticleRole.update_article_model`：已查看反汇编，其分支主要处理装饰、家具和随从，不是当前服装展示绑定的入口。排除商店价格、购买按钮、UI 文字等方法，避免无关界面改动使模组失效。

`WorldArticle.game_tick` 包含 `__class__` 闭包，其值为自定义元类。当前指纹器不把类名当作实现证据，仍拒绝此类闭包。已查看其有限反汇编，未发现直接模型缩放写入；模组通过已验证的原生 `frame_delay_exec` 自行调度，不调用该方法，因此不把它列为调用许可。此选择不等于审核了 `game_tick` 的全部间接依赖。

## 训练能力：4 个函数

| 函数 | 选择原因 |
| --- | --- |
| `BaseUnit.update_model_scale` | 必选恢复函数。反汇编确认它将 `scale_dct` 中的因子相乘，检查模型有效性，写入原生三轴缩放及 `last_scale`，并更新已有 `another_model`。模组只有在当前函数精确匹配且确认对象和形状所有权后才调用 `force=True`。 |
| `BaseUnit.init_model_after_loaded` | 方法元数据包含初始 `scale`、`last_scale`、`init_scale` 与模型注册，保守检测模型重新加载后的缩放语义变化。 |
| `BaseUnit.set_scale` | 反汇编确认写入 `model.scale` 的直接数据路径；模组不调用该方法。 |
| `BaseUnit.get_scale3` | 反汇编确认读取 `model.scale` 的直接数据路径；模组不调用该方法。 |

没有加入 `on_model_replaced`（当前空实现）和只转发渲染配置的 `set_model_attr_after_loaded`。本 JSON 只是运行时函数指纹基线，不限定适配器的场景范围；适配器在客户端拥有的任一 battle world（匹配对局与训练场）走同一条已审核路径，是否启用由使用者的 `--battle` 决定。

## 指纹与实测边界

规范化指纹包含 Python 主次版本、字节码、递归常量与嵌套代码、函数参数、名称、标志、默认参数、关键字默认参数和可安全表示的闭包。路径、源代码行号及行号映射不参加哈希；单纯插入空行或移动源码位置不会使其失配。装饰器中的 Python 函数闭包递归处理，并有深度和循环限制。类、可变默认值和其他未知常量不会按名称或字符串表示猜测兼容。

指纹没有穷举函数的任意全局对象图、配置表或间接调用图。数据新增可以沿用相同实现；相关实现改变则需要审核过的新适配包。运行时每帧检查函数及递归闭包函数的对象/code 身份，每秒完整检查，新模型绑定重新完整检查；展示和训练独立锁存失配。实际修改仍受有限稳定帧、缩放读取核对、对象世代/形状所有权及后续帧恢复核对约束。

## 实测解释器执行标志的修正

后续训练场只读检查发现 `BaseUnit.init_model_after_loaded` 的初始指纹和执行后的指纹不同。没有直接接受新指纹，也没有移除该函数检查。对执行后的完整规范记录仅清除 `co_flags` 的第 28 位（`0x10000000`），重新计算 SHA256 后精确复现原始基准：`7aa041645e35d31fbf74016a42cd11609b3e7350e56918438f342fbf9badf4d8`。保留该位时为 `220177fad21c6a98af99d5ceebbcccb3bc05ac4f77a33df9dff684d1238c17fc`。无闭包、无包装链和默认参数变化，差异定位到该标志位，不是代码体变化。

随后在已验证游戏解释器内，仅执行我们自己的三个纯整数运算函数（普通函数、带嵌套代码的函数、普通方法）。三个样本均在首次执行时由 flags `0x3` 变成 `0x10000003`，执行 1000 次后保持相同；字节码及算术结果未改变；嵌套规范记录除这一位外一致。证据和可复现离线核对在 `interpreter-flag-analysis.json`、`analyze_interpreter_flags.py`。这证明当前解释器使用该位记录执行状态，不推断它具体属于何种 JIT 或内部优化机制。

因此本引擎基准显式声明 `volatile_code_flags: 268435456`。仅已通过原生指纹准入的该 NeoX Python 3.11 profile 使用此策略，递归清除代码对象中的这一位后计算指纹；默认离线指纹器及未声明策略的 profile 仍保留全部 flags。只允许掩码 0 或上述单个位，非零掩码禁止用于其他 Python 主次版本。其他 flags、全部字节码及语义常量仍参加检查，未自动学习未知实现。12 个关键函数全部保留。

最初大厅只读样本中原生模型为 `world.model`，`valid` 是布尔值，`valid`、`scale`、`filename` 均是原生 `getset_descriptor`。未据此冒认训练场或展示页的 1.3 缩放实机验证。
