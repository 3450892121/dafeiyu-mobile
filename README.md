# 大肥鱼手机版（测试版）

个人使用的安卓 QQ 小号机器人。复刻服务器版大肥鱼的功能，建立新的独立实例；新小号、新配置、新聊天、新记忆。手机直接使用所配置的 AI 接口，电脑与原服务器不参与运行。

**当前为公开测试版，尚未完成全部真机验收，不承诺安装后零配置运行或长期后台稳定。**

**[点击下载 Android ARM64 安装包（APK）](https://github.com/3450892121/dafeiyu-mobile/releases/download/v0.3.0-beta.1/dafeiyu-mobile-0.3.0-arm64.apk)**

[全部附件、校验与测试版说明](https://github.com/3450892121/dafeiyu-mobile/releases/tag/v0.3.0-beta.1) · [所有历史版本](https://github.com/3450892121/dafeiyu-mobile/releases)

本版已把 Ubuntu、系统依赖、Python 包、NapCat 和 CPU 识别模型放进 APK，安装包较大，准备这些组件不再访问 GitHub、Ubuntu 或 PyPI。**QQ 客户端因没有公开再分发授权，首次仍从腾讯官方 HTTPS 地址下载约 200 MB；QQ 登录、AI 和实际联网功能也仍需网络。** 获取 APK 本身仍受所用下载平台的网络可达性影响，下载后不需要再去 GitHub 补运行组件。

GitHub 右侧 Releases 的 Latest 用于显示当前下载版本；版本标题和本说明始终写明“测试版”，该展示分类不表示已通过全部真机验收。

## 安装与使用

从上方直接下载 `dafeiyu-mobile-0.3.0-arm64.apk`，或进入[发布附件](https://github.com/3450892121/dafeiyu-mobile/releases/tag/v0.3.0-beta.1)查找以 `.apk` 结尾的文件；GitHub 的 “Source code” ZIP 不能安装到手机。自行构建时产物位于 `dist/`。需要安卓 8.0 以上、64 位 ARM，建议至少 6 GB 内存和 6 GB 空闲存储。只安装这个 App，无需另装 Termux。配置自己的 AI 接口和 QQ 小号并完成登录后才可使用；安装包不含任何人的密钥或登录资料。

1. 打开软件，点击“初始化 / 修复运行组件”。连接网络和电源，按本机空间提示准备。Ubuntu、系统包、Python、NapCat 和 CPU 识别模型从包内校验、展开和安装；仅 QQ 客户端从固定腾讯官方 HTTPS 来源下载。缺内置文件或校验失败会停止，不偷偷从公网补依赖。此过程仍需一定时间，完成初始化不等于 QQ 已登录。
2. 打开“设置与管理”，填写**新机器人小号**和与之不同的管理员 QQ 并保存。进入“完整管理页 → 模型”，填写自己的接口地址、模型名及该线路专用密钥，保存后可明确点击测试。软件不赠送模型额度，实际联网调用按所配服务的规则计费。
3. 在完整管理页的“设置 → 身份与聊天范围”加入允许回复的好友 QQ 和群号并保存；默认不会向任意好友或群开放。然后点击“启动机器人”，进入“QQ 登录”，由本人使用所填新小号完成登录。登录身份与所填小号不一致时暂停，不自动替换号码。
4. 如需听懂语音或没有字幕的视频，初始化会准备随包的 CPU 识别模型；设置页可“准备 / 修复内置识别模型”。也可明确选择自配的语音 API，转写 API 仅使用所选接口同主机的密钥。实际识别效果与手机耗时仍待验证。
5. 在本机检查中设置电池与后台权限。运行时保留通知；“停止运行”会停止本实例并取消开机恢复。
6. 先在自己明确允许的私聊或群内测试文字回复。群里可 @本小号，或使用当前配置的唤醒词“小妲”；无回复时查看“检查消息链路”。B站登录、空间及公开评论/发布须另行配置和明确授权，不因开启聊天范围而自动开通。

此手机实例的角色为纳西妲，叫名唤醒词为“小妲”。设置页分别保存机器人名称与唤醒词，生效后无需重新登录；项目名称不作为机器人的叫名触发词。默认关闭群回复时自动@对方，仍能接收别人@本小号的消息。群聊需先在完整管理页的“身份与聊天范围”中加入允许回复的群号。

设置页会分别提示账号不一致、指定小号已登录但消息接口未接通、消息接口可用。若扫错账号，即使消息接口未启动也会暂停；自行切换到所填小号后，再手动启动机器人。不会自动替换设置中的 QQ 号。

更新使用同签名 APK 覆盖安装，保留本实例资料。QQ 界面和内核登录目录分别固定，检测到唯一已有登录目录时原地沿用；不会为更新清空或复制登录票据。设置页可“检查登录保存状态”，区分保存记录与实际在线。QQ 会话失效时仍须本人处理，目录存在不能当作登录成功。

若已保存小号却没有恢复在线，可在设置页点击“使用保存的小号登录”。只按本实例设置的小号尝试一次，不从上游下拉列表猜选账号；有错误、验证或安全暂停时不继续，成功与否以当前在线状态为准。

没有回复时，可在设置页点击“检查消息链路”。QQ 离线时也能查看本实例已配置会话的收发计数、模型成败及沉默选择；检查不会发送消息、调用模型或重新登录。统计和回调接纳不等于对方已收到回复。

若显示“业务回复已暂停”，在设置页明确点击“恢复业务回复”；普通启动不会解除此暂停。恢复业务开关不会解除 QQ 验证或连接安全暂停。原生按钮和网页启停均同步开机恢复意图，手动停止或安全暂停后不会自动重启机器人。

全部功能及逐项验收要求见 [功能清单](spec/features.json)。`implemented` 表示已有功能源码和隔离检查，**不表示手机已经实测通过**。真实 QQ、B站与空间操作仍需现场授权并取得实际回执。接口失效、风控和系统后台限制会如实报错。

聊天线路可自行配置兼容接口，每条线路单独填写密钥。当前绘图和向量索引适配方舟 `https://ark.cn-beijing.volces.com/api/plan/v3`，只使用同一接口路径中已启用线路的密钥；普通聊天线路可用不代表这些能力也可用。不同套餐的地址、密钥和模型支持范围需分别核对：[Agent Plan 图片接口](https://docs.volcengine.com/docs/ark/agent-plan-personal-visual-models?lang=en)、[Agent Plan 向量接口](https://docs.volcengine.com/docs/ark/agent-plan-personal-vectorized-models?lang=en)。本版不自动切换套餐或调用其他计费路径；没有匹配线路时提示缺项，记忆与表情仍可回退关键词和标签检索。

原版的语音发声与翻唱依赖电脑端 Breeze/GPU 服务，本版按用户允许范围不接入这些服务；文字、图片、歌曲分享、文件、字幕、视频取帧和声音转写继续保留。没有证据证明所选实现可以降低封号率。

## 测试版验证边界

功能清单仍是唯一状态来源：36 项为 `implemented`（已有源码及隔离证据，待手机验收），2 项为 `omitted`（上述原电脑语音发声/翻唱后端）；没有将它们批量标为真机 `verified`。控制台入口和设置有对应处理，也不等于外部接口或真实发送已经成功。

- 此次发布的 0.3.0 内置组件 APK 尚未完成真机安装、控制台视觉及全部控件操作验收。此前旧包和局部更新只在指定会话取得基础文字收发及部分角色/唤醒词的现场证据，不能外推本测试版全部功能。
- 脱离电脑后完成对话、落库与提醒，以及干净手机上独立完成内置组件初始化，仍未完整验收。内置资产与依赖静态闭包检查不代替实际 ARM64 APT/PIP 安装；QQ 官方下载和手机内核限制仍需现场核对。
- 熄屏至少两小时、网络切换后的有限恢复、手机重启后的资料及运行意图保留，仍未完成真机验收；不能用电脑模拟检查代替。
- 记忆/隐私/成长/梦/日记/提醒等详细行为，语音接收与视频音轨识别、图片理解与生成、文件读写及实际 QQ 交付、歌曲分享、B站与空间真实互动，均未逐项取得完整手机交付证据。需要相应账号、接口权限及会话授权。
- 历史补拉、引用/转发、戳一戳、多人发言归属、主动私聊/群陪伴、权限边界、搜索与天气等其余清单条目，也尚未完成整项真机验收。请按[功能清单](spec/features.json)每项的证据要求判断，不能把基础文字收发通过视为这些条目通过。
- 视频理解目前通过支持的视频链接进入，不解析直接收到的 `video` 附件。链接视频的字幕、取帧和音轨转写仍需分别实测。
- 绘图和向量检索受上文固定服务接口限制；既有向量模型默认值与当前套餐的兼容性、CPU 识别耗时仍待实测，不自动换套餐或迁移已有索引。
- Android Keystore、手机上加密备份的导出/恢复、真实挑战或被踢时的暂停行为仍待现场核对。不要主动触发风控来验证；有挑战时由本人处理。

电脑侧隔离回归、编译和签名检查的本次结果见[该测试版发布说明](https://github.com/3450892121/dafeiyu-mobile/releases/tag/v0.3.0-beta.1)；它们仅证明列出的检查范围。完整判据见 [验收标准](spec/acceptance.md)，未因测试版公开而降低。

## 资料与备份

设置、登录和业务数据只在本 App 私有目录。默认不继承原实例的关系与公开分享授权；管理员私聊感受跨会话分享需要在本实例的隐私设置中明确开启。

从概览导出或导入 `.dafm` 加密备份，密码至少 8 个字符。备份含本实例业务数据、人设、接口配置、加密材料和独立 B站资料；**不含 QQ 客户端、QQ 登录缓存和 CPU 模型**。恢复会停止机器人、校验数据库、保留本机控制令牌、替换旧资料并保持暂停，核对设置后手动登录和启动。忘记密码无法解密。

## 构建与检查

工具仅放在本项目 `.tools/`，开发签名在 `.local/`，均不提交。使用 JDK 17、Android SDK Platform 35、Build Tools 35.0.0、Node 和 Python 3.12+。

```powershell
node tools/fetch-runtime.mjs
node tools/fetch-native-sources.mjs
.tools/python/python.exe tools/fetch-offline-runtime.py --stage apt
.tools/python/python.exe tools/fetch-offline-runtime.py --stage wheels
node tools/fetch-fixed-model.mjs
.tools/python/python.exe tools/fetch-offline-runtime.py --stage archives
.tools/python/python.exe tools/fetch-offline-runtime.py --stage sources
.tools/python/python.exe tools/fetch-offline-runtime.py --stage freeze
.tools/python/python.exe tools/fetch-offline-runtime.py --stage source-zip
.tools/python/python.exe tools/fetch-wheel-native-sources.py
.tools/python/python.exe tools/fetch-wheel-native-sources.py --check --append-zip dist/dafeiyu-mobile-0.3.0-linux-sources.zip
node tools/check-project.mjs
node --test tests/*.test.mjs
powershell -ExecutionPolicy Bypass -File tools/test-runtime.ps1
powershell -ExecutionPolicy Bypass -File tools/build-apk.ps1
```

Python 检查环境需要 `runtime/requirements.txt` 的依赖，可通过 `-Python` 指定已有的私有解释器。资源准备还需本项目 Python 的 `packaging`、pip/wheel、Git 自带 GPG；固定模型的分段下载工具使用 GitHub CLI，认证仅留在该 CLI 中。它们都只在构建电脑使用，手机不需要这些工具。Python wheel 目标是 Linux ARM64/Python3.12，补全目标环境标记依赖；少数纯 Python 源码包在电脑上生成通用 wheel，不在手机编译。构建脚本可传 `-JdkHome` 和 `-SdkHome`，默认使用本项目工具目录。APK 通过完整内置资产、资源、DEX、ARM64 库、16 KB ZIP 对齐及签名校验；不修改系统配置或参考项目。

`fetch-wheel-native-sources.py` 按 `runtime/wheel-native-sources.lock.json` 核对实际 wheel 与库文件，取得其中 ALSA、静态 libiconv、GCC 运行库和实际内含的 Eigen 的精确源码、构建配方/补丁及相关 Python 源码发行。ONNX Runtime 的 MIT/第三方许可来源与实际 MPL 覆盖的 Eigen 源码分别记录，不把通用附录中的可选库都算成已集成。先生成基础 Linux 源码 ZIP，再使用 `--append-zip` 将这些材料、完整许可和替换/重链接说明原子加入同一附件；`--check` 禁止联网并校验所有固定输入。重新生成基础 ZIP 后须再次执行追加步骤。完整重建说明见 `runtime/wheel-native-BUILDING.txt`，不能只保留 wheel 的 LICENSE 就省略其中 GPL/LGPL/MPL 库的对应源码。

公开 Git 仓库前，在完整的本项目 Git 检出中运行 `node tools/check-git-history.mjs`。它检查可达历史文件、历史文件名及提交和标签的敏感特征，命中时仅报告路径或对象编号；源码 ZIP 不含 Git 历史，不能用它证明历史扫描通过。

工程编译 SDK 为 35，私有侧载包目标 SDK 为 28，以允许运行 App 私有 Linux 用户态文件，原因见 [原生应用规格](spec/modules/application.md)。这不是应用商店发布方案，现代安卓的兼容提示和具体内核限制须真机核对。

发布附件同时提供项目源码、原生组件对应源码和内置 Linux / wheel 库对应源码 ZIP，后两者不需要装进手机；固定清单见 `runtime/offline-manifest.json`、`runtime/linux-sources.lock.json`、`runtime/wheel-native-sources.lock.json`。Linux 源码 ZIP 的 `wheel-native/` 另含实际 wheel 库的源码、构建材料与重链接说明。自有代码与业务源码见本仓库。正式来源及许可见 [第三方登记](THIRD-PARTY-NOTICES.md)，模块与边界见 [项目总览](spec/00-overview.md)，验收规则见 [验收标准](spec/acceptance.md)。运行进度与交接放在忽略的 `notes/`，不冒充设备结果。

## 许可

本项目自有代码及复用的业务源码采用 [MIT](LICENSE)，复用版权声明保留于 `runtime/core/UPSTREAM-LICENSE`。PRoot、talloc、Android 共享内存库及下载组件各自遵循原许可，不能套用本项目的 MIT。
