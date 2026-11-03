# 第三方组件及源码

自有源码许可见根 `LICENSE`。组件版本、下载源和校验值以 `runtime/*lock.json` 为准；第三方代码不因被本 App 调用而改为 MIT。App 的“关于与第三方许可”可阅读内附许可。

## APK 内的组件

| 组件 | 用途与许可 | 来源与对应源码 |
|---|---|---|
| 参考项目大肥鱼的 Python / HTML 业务源码及回归 | MIT，保留 2026 3450892121 的声明；独立适配，不含原配置或资料 | `runtime/reference-manifest.json`、`tests/ported-sources.json` 记录只读来源及原始哈希；`runtime/core/UPSTREAM-LICENSE` 保留原许可 |
| PRoot 5.1.107.102 | 独立 Linux 用户态进程；GPL-2.0-or-later；预编译 ELF 未改，仅以 APK 可提取的文件名保存 | [Termux PRoot](https://github.com/termux/proot/tree/v5.1.107.102)，源码包内含固定上游归档、配方和许可 |
| talloc 2.5.0 | PRoot 的动态依赖；库为 LGPL-3.0-or-later，包内部分构建文件为 GPL-3.0 | [Samba talloc](https://www.samba.org/ftp/talloc/)，库头文件的声明与完整 LGPL/GPL 文本保留 |
| libandroid-shmem 0.7 | Android 共享内存支持；BSD-3-Clause | [Termux libandroid-shmem](https://github.com/termux/libandroid-shmem/tree/v0.7)，保留原版权与 BSD 文本 |

上述原生库来自已校验的 Termux 官方包，未与 App Java 代码链接；通过独立进程及动态库调用。`runtime/native-sources.lock.json` 与 `runtime/native-build-recipes.lock.json` 固定对应源码和构建文件；`dist/*-native-sources.zip` 与 APK 一并提供。用户可修改、重建和替换相应库并重新签名，不施加额外逆向或替换限制。具体方法见源码包内 `BUILDING.txt`。

## 手机初始化时直接下载的组件

- Ubuntu Base ARM64 24.04.5：从 [Ubuntu 官方](https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/) 下载并按官方 SHA256 校验。APK 不再分发其根文件系统二进制；安装后保留 `/usr/share/doc/*/copyright` 等原始声明。软件包及对应源码由 Ubuntu 官方仓库提供，适用各包自己的许可。
- Linux QQ 3.2.34-53644：从腾讯官方地址下载到本 App 私有目录；仅解包本 App 的独立副本，原 QQ 不受影响。腾讯许可及其自带组件声明适用，不把 QQ 重新标为开源。
- NapCat Shell 4.18.42：从 [官方完整发行包](https://github.com/NapNeko/NapCatQQ/releases/tag/v4.18.42) 下载，保留上游所有文件与声明，按官方发布摘要验包。适用 [NapCat 上游许可](https://github.com/NapNeko/NapCatQQ/blob/main/LICENSE)，不拆分、复制其专有附加组件到 APK，也不改标 MIT。
- Python / FFmpeg / Xvfb 及系统库：通过 Ubuntu 签名仓库安装，保留发行包许可和源码获取信息。Python 业务依赖按 `runtime/requirements.txt` 从 PyPI 安装，适用各自随包许可。
- sherpa-onnx 2.0.1 / SenseVoice CPU 模型：Apache-2.0，来源为 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 与其官方模型发行；模型不在 APK 中，下载前固定官方摘要。NumPy 为 BSD 许可，随 PyPI 包保留声明。
- bili-agent 0.9.0：MIT，来源为 [kukemc/bili-cli](https://github.com/kukemc/bili-cli)，使用本实例独立凭证目录；其依赖保留各自许可。

SnowLuma、LLBot 与 Lagrange 未集成；候选评估见 `spec/qq-access.md`。未复制参考项目素材、真实账号、密钥、Cookie、数据库或聊天记忆。

## 构建工具

Android SDK / Build Tools 遵守 Android SDK 条款及各自许可，Amazon Corretto / OpenJDK 作为本项目私有构建工具使用；它们不打包进 APK。来源分别为 [Android 官方](https://developer.android.com/tools) 与 [Corretto 官方](https://docs.aws.amazon.com/corretto/latest/corretto-17-ug/downloads-list.html)。
