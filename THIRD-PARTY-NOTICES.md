# 第三方组件

本项目自有代码采用根 `LICENSE`。参考项目的功能、说明及公开源码仅用于只读对照；初始化骨架未复制其业务代码、素材、配置、聊天、记忆或登录资料。

## 运行时

初始化骨架只使用 Android 系统 API，未内置 QQ、NapCat、SnowLuma、LLBot、Lagrange、Python、模型或第三方素材。候选不等于已引入，接入评估见 `spec/qq-access.md`。

## 构建工具

- Android SDK / Build Tools：编译、资源处理、DEX 和 APK 验签；来源 https://developer.android.com/tools ，遵循各自随附许可及 Android SDK 条款；只作为构建工具，不打包进应用。
- OpenJDK（本机构建使用 Amazon Corretto）：Java 编译与构建工具运行环境；仅使用已有或按官方来源下载的发行版及其随附许可，不打包进应用。官方来源 https://docs.aws.amazon.com/corretto/latest/corretto-17-ug/downloads-list.html 。

未来引入第三方代码、运行时、字体、图片、模型和二进制前，登记具体版本、用途、来源与许可，并核对能否随 APK 分发。
