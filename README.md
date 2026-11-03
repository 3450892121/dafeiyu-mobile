# 大肥鱼手机版

个人使用的安卓 QQ 小号机器人。复刻服务器版大肥鱼的功能，建立新的独立实例；新小号、新配置、新聊天、新记忆。手机直接使用所配置的 AI 接口，电脑与原服务器不参与运行。

## 安装与使用

安装包：`dist/dafeiyu-mobile-0.2.1-arm64.apk`。需要安卓 8.0 以上、64 位 ARM，建议至少 6 GB 内存和 4 GB 空闲存储。只安装这个 App，无需另装 Termux。

1. 打开软件，点击“初始化 / 修复运行组件”。连接 Wi-Fi 和电源，预留至少 2.5 GB 空间；首次会从 Ubuntu、PyPI、腾讯及 NapCat 官方来源下载私有运行组件。
2. 打开“设置与管理”，填写**新机器人小号**和与之不同的管理员 QQ。点击完整管理页，添加自己的 AI 接口、模型以及允许的好友和群。
3. 点击“启动机器人”，进入“QQ 登录”，使用本项目新小号完成登录。登录身份与所填小号不一致时暂停，不自动替换号码。
4. 如需听懂语音或没有字幕的视频，在设置页下载 CPU 识别模型；也可明确选择自配的语音 API。转写 API 仅使用所选接口同主机的密钥。
5. 在本机检查中设置电池与后台权限。运行时保留通知；“停止运行”会停止本实例并取消开机恢复。

全部功能及逐项验收要求见 [功能清单](spec/features.json)。`implemented` 表示已有功能源码和隔离检查，**不表示手机已经实测通过**。真实 QQ、B站与空间操作仍需现场授权并取得实际回执。接口失效、风控和系统后台限制会如实报错。

原版的语音发声与翻唱依赖电脑端 Breeze/GPU 服务，本版按用户允许范围不接入这些服务；文字、图片、歌曲分享、文件、字幕、视频取帧和声音转写继续保留。没有证据证明所选实现可以降低封号率。

## 资料与备份

设置、登录和业务数据只在本 App 私有目录。默认不继承原实例的关系与公开分享授权；管理员私聊感受跨会话分享需要在本实例的隐私设置中明确开启。

从概览导出或导入 `.dafm` 加密备份，密码至少 8 个字符。备份含本实例业务数据、人设、接口配置、加密材料和独立 B站资料；**不含 QQ 客户端、QQ 登录缓存和 CPU 模型**。恢复会停止机器人、校验数据库、保留本机控制令牌、替换旧资料并保持暂停，核对设置后手动登录和启动。忘记密码无法解密。

## 构建与检查

工具仅放在本项目 `.tools/`，开发签名在 `.local/`，均不提交。使用 JDK 17、Android SDK Platform 35、Build Tools 35.0.0、Node 和 Python 3.12+。

```powershell
node tools/fetch-runtime.mjs
node tools/fetch-native-sources.mjs
node tools/check-project.mjs
node --test tests/*.test.mjs
powershell -ExecutionPolicy Bypass -File tools/test-runtime.ps1
powershell -ExecutionPolicy Bypass -File tools/build-apk.ps1
```

Python 检查环境需要 `runtime/requirements.txt` 的依赖，可通过 `-Python` 指定已有的私有解释器。构建脚本可传 `-JdkHome` 和 `-SdkHome`，默认使用本项目工具目录。APK 通过资源、DEX、ARM64 库打包、16 KB ZIP 对齐及签名校验；不修改系统配置或参考项目。

工程编译 SDK 为 35，私有侧载包目标 SDK 为 28，以允许运行 App 私有 Linux 用户态文件，原因见 [原生应用规格](spec/modules/application.md)。这不是应用商店发布方案，现代安卓的兼容提示和具体内核限制须真机核对。

`dist/` 同时交付原生组件对应源码包；自有代码与业务源码见本仓库。正式源码来源及许可见 [第三方登记](THIRD-PARTY-NOTICES.md)，模块与边界见 [项目总览](spec/00-overview.md)，验收规则见 [验收标准](spec/acceptance.md)。运行进度与交接放在忽略的 `notes/`，不冒充设备结果。

## 许可

本项目自有代码及复用的业务源码采用 [MIT](LICENSE)，复用版权声明保留于 `runtime/core/UPSTREAM-LICENSE`。PRoot、talloc、Android 共享内存库及下载组件各自遵循原许可，不能套用本项目的 MIT。
