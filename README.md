# 大肥鱼手机版

为个人开发的安卓 QQ 小号机器人，参考服务器版大肥鱼的功能，建立新的独立实例。新小号、新聊天、新记忆；电脑和原服务器不作为手机版的运行依赖。

## 下载

这是初始化骨架工程，完整功能目标与当前逐项状态见 [功能清单](spec/features.json)。构建产物位于 `dist/`，文件名带 `scaffold`，表示基础界面阶段；不能据此认为已接入 QQ 或已复刻完成。

## 使用

安卓最低版本是 8.0。当前界面提供概览、功能目标和本机条件检查；明确显示尚未接入 QQ。单 App 独立运行的验收要求见 [验收标准](spec/acceptance.md)，QQ 接入尚未完成。

工程检查与生成展示资源：

```powershell
node tools/check-project.mjs
node --test tests/*.test.mjs
node tools/prepare-assets.mjs
```

使用 JDK 17、Android SDK Platform 35 与 Build Tools 35.0.0 构建基础 APK：

```powershell
powershell -ExecutionPolicy Bypass -File tools/build-apk.ps1 -JdkHome "JDK目录" -SdkHome "Android SDK目录"
```

也可把工具分别放在本项目 `.tools/jdk/` 和 `.tools/android-sdk/` 后省略参数。脚本只写本项目，不修改系统环境；签名是本项目新建的本地开发签名，不能作为正式发布凭据。

标准 Android Gradle 工程可使用 JDK 17、Gradle 8.9 与 Android SDK 35 打开或构建。具体模块边界见 [项目总览](spec/00-overview.md)，开发先读根 `AGENTS.md`。

## 许可

本项目自有代码采用 [MIT](LICENSE)，署名沿用当前 Git 身份。候选 QQ 组件未内置，不能把本项目许可套给它们；组件来源及许可见 [第三方登记](THIRD-PARTY-NOTICES.md)。
