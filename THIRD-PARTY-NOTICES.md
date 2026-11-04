# 第三方组件及源码

自有源码许可见根 `LICENSE`。组件版本、下载源和校验值以 `runtime/*lock.json` 和 `runtime/offline-manifest.json` 为准；第三方代码、二进制和模型权重不因被本 App 调用而改为 MIT。App 的“关于与第三方许可”可阅读内附许可。包含 NapCat 的完整安装包还受其非商业及其他许可条件约束，不能把自有代码的 MIT 当成对整个安装包的统一授权。

## APK 内的组件

| 组件 | 用途与许可 | 来源与对应源码 |
|---|---|---|
| 参考项目大肥鱼的 Python / HTML 业务源码及回归 | MIT，保留 2026 3450892121 的声明；独立适配，不含原配置或资料 | `runtime/reference-manifest.json`、`tests/ported-sources.json` 记录只读来源及原始哈希；`runtime/core/UPSTREAM-LICENSE` 保留原许可 |
| PRoot 5.1.107.102 | 独立 Linux 用户态进程；GPL-2.0-or-later；预编译 ELF 未改，仅以 APK 可提取的文件名保存 | [Termux PRoot](https://github.com/termux/proot/tree/v5.1.107.102)，源码包内含固定上游归档、配方和许可 |
| talloc 2.5.0 | PRoot 的动态依赖；库为 LGPL-3.0-or-later，包内部分构建文件为 GPL-3.0 | [Samba talloc](https://www.samba.org/ftp/talloc/)，库头文件的声明与完整 LGPL/GPL 文本保留 |
| libandroid-shmem 0.7 | Android 共享内存支持；BSD-3-Clause | [Termux libandroid-shmem](https://github.com/termux/libandroid-shmem/tree/v0.7)，保留原版权与 BSD 文本 |

上述原生库来自已校验的 Termux 官方包，未与 App Java 代码链接；通过独立进程及动态库调用。`runtime/native-sources.lock.json` 与 `runtime/native-build-recipes.lock.json` 固定对应源码和构建文件；`dist/*-native-sources.zip` 与 APK 一并提供。用户可修改、重建和替换相应库并重新签名，不施加额外逆向或替换限制。具体方法见源码包内 `BUILDING.txt`。

## APK 内置的首次安装资料

完整安装包携带以下原始上游归档、安装包和完整 wheel；仅有源码发行的 qrcode-terminal 由构建机打成通用 wheel，细节见下表。手机在本 App 私有目录内校验后安装。构建缓存仅位于本项目 `.tools/offline-bundle/`，不会从已登录实例复制运行目录或真实资料；各文件的实际来源、大小和 SHA256 见 `runtime/offline-manifest.json`。

| 组件 | 来源、许可和分发边界 |
|---|---|
| Ubuntu Base ARM64 24.04.5 | 来自 [Ubuntu 官方原始基础归档](https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/)，按官方摘要校验；归档及其中软件包各自保留原许可。依照 [Canonical 知识产权政策](https://canonical.com/legal/intellectual-property-policy) 分发未经改动的官方归档和包，不将手机初始化后组合出的环境改称官方 Ubuntu 镜像，也不暗示 Canonical 背书。 |
| Python、FFmpeg、Xvfb、字体与系统依赖 | 内置 Ubuntu 官方签名索引和按索引摘要固定的原始 deb；按包适用 GPL、LGPL、BSD、MIT、Apache 等各自许可。安装后保留 `/usr/share/doc/*/copyright`、许可及构建来源。FFmpeg 及其依赖不能统一套用 MIT，实际许可依内置 Ubuntu 包的版权文件为准。 |
| Python 业务包及其依赖 | 从 PyPI 官方发布文件取得匹配 Linux ARM64/Python 3.12 的完整 wheel 集合，顶层需求见 `runtime/requirements.txt`，实际安装闭包见离线清单。Pillow、python-docx、openpyxl、python-pptx、pypdf、cryptography、yt-dlp、qrcode、NumPy 等按各包及 wheel 内所含库的许可分发；保留完整 `.dist-info`、`licenses`、`LICENSE` 和 `NOTICE`，不删除原始 wheel 的声明。实际内含 GPL/LGPL 库的精确对应源码另按下表及 `runtime/wheel-native-sources.lock.json` 提供，不能只保留 wheel 许可文本，也不能从顶层包推定整个依赖闭包适用同一许可。 |
| qrcode-terminal 0.8 | 官方 [PyPI 源码发行](https://pypi.org/project/qrcode-terminal/0.8/) 的 `setup.py` / `PKG-INFO` 标注 MIT；原始 Python 文件署名为 `wang_pc`，原包未附独立 LICENSE 或版权行。构建机仅将原始源码打为纯 Python 通用 wheel，保留源码头与元数据；原 sdist、摘要及配方一并收入 `*-linux-sources.zip`。`runtime/licenses/qrcode-terminal-0.8-MIT-NOTICE.txt` 保留原署名，并补附标准完整 MIT 条款，注明其为补充声明，不冒充原包自带文件。 |
| bili-agent 0.9.0 | MIT，来源为 [kukemc/bili-cli](https://github.com/kukemc/bili-cli)，作为上述 wheel 集合的一部分内置；其依赖保留各自许可，运行时使用本实例独立凭证目录。 |
| bilibili-api-python 17.4.2、setuptools 内 autocommand 2.2.2、certifi 2026.10.23 | 分别适用原 GPL、LGPL、MPL-2.0 条款；保留完整 Python 源码/证书数据、原版权和许可。除原 wheel 内的源码外，还同页提供这些固定 PyPI sdist；setuptools 84.2.1 的完整 sdist 保留其 vendoring 配置，另附原始 autocommand sdist，不只给一个缺打包脚本的目录。 |
| NapCat Shell 4.18.42 | 内置 [官方完整发行 ZIP](https://github.com/NapNeko/NapCatQQ/releases/tag/v4.18.42)，保持其原始字节和完整文件，并按固定官方摘要校验。适用 [固定版本的 Limited Redistribution License for NapCat](https://github.com/NapNeko/NapCatQQ/blob/v4.18.42/LICENSE) 及其中第三方组件各自的许可。全文另附于 `runtime/licenses/NapCat-v4.18.42-LICENSE.txt`，保留 © 2024 Mlikiowa 及来源；非商业使用、再分发和其他限制均保留。 |
| sherpa-onnx 2.0.1 | 识别引擎代码采用 Apache-2.0，来自 [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx/tree/v2.0.1)，完整许可见 `runtime/licenses/sherpa-onnx-Apache-2.0.txt`；其 wheel 内所含其他库仍按原声明。此代码许可不自动覆盖识别模型权重。 |
| sherpa wheel 中的 ALSA libasound 1.2.12 | 动态库适用 LGPL-2.1-or-later。固定 [ARM64 wheel 配方](https://github.com/k2-fsa/sherpa-onnx/blob/v2.0.1/.github/workflows/build-wheels-aarch64.yaml) 明确从官方 v1.2.12 执行 `gitcompile`；同页源码附件保留该版本完整源码、构建文件和配方。完整 LGPL 另见 `runtime/licenses/alsa-lib-1.2.12-LGPL-2.1.txt`，允许兼容库替换与调试修改。 |
| lxml 6.1.3 静态内入的 GNU libiconv 1.18 | libiconv 库适用 LGPL-2.1-or-later。保留完整 libiconv 源码和 [lxml 固定构建配方](https://github.com/lxml/lxml/blob/lxml-6.1.3/.github/workflows/wheels.yml)，提供完整 lxml 源码及生成 C、打包/构建脚本，并附固定 libxml2 2.14.6、libxslt 1.1.43、zlib 1.3.2 源码，使用户可换库后重建/重链接，而非仅给已链接的扩展。完整 LGPL 见 `runtime/licenses/libiconv-1.18-LGPL-2.1.txt`。 |
| NumPy 2.5.4 wheel 内的 libgfortran | GPL-3.0-or-later WITH GCC-exception-3.1，来源精确匹配 AlmaLinux `gcc-8.5.0-28.el8_10.alma.1.src.rpm`；GNU build-id、代码/数据段摘要及官方 source repodata 摘要见 wheel 源码锁。源码附件保留完整 SRPM 中的原始 GCC 源码、发行补丁和 `gcc.spec`，不以 Ubuntu 的其他 GCC 版本替代。原 NumPy/OpenBLAS/LAPACK 许可保留，完整 GCC 例外另附于 `runtime/licenses/GCC-runtime-exception-3.1.txt`。 |
| ONNX Runtime 1.28.2 | sherpa-onnx-core 的实际库适用 MIT 及其各第三方条款。固定官方依赖归档的完整摘要和来源见 wheel 源码锁；从该归档逐字保留 `runtime/licenses/onnxruntime-1.28.2-LICENSE.txt`、`runtime/licenses/onnxruntime-1.28.2-ThirdPartyNotices.txt`，不能用 sherpa 的 Apache 文本代替。通用附录列出可选组件不表示它们全部进入本 ARM64 构建。 |
| ONNX Runtime 实际内入的 Eigen | MPL-2.0，精确提交 `1d8b82b0740839c0de7f1242a3585e3390ff5f33`，由固定 ONNX `deps.txt` 的源摘要及实际编译模板代码确认。源码附件保留完整该提交源码、固定 `eigen.cmake` / `deps.txt` 和实际配方应用的两份原始补丁；全文另附 `runtime/licenses/Eigen-MPL-2.0.txt`。此源码义务与 ONNX Runtime 的 MIT/通用第三方声明来源分别记录。 |
| SenseVoiceSmall int8 ONNX 识别权重 | 来自 [sherpa-onnx 官方模型发行](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)，固定 `2024-07-17` 归档及摘要见 `runtime/asr-model.lock.json`。原始权重由 FunAudioLLM / Alibaba Group 提供，ONNX 转换来源见 [转换模型仓库](https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17)。权重采用 **FunASR Model Open Source License Agreement v1.1**，不是 sherpa-onnx 的 Apache-2.0，也不是当前代码仓库的 MIT；完整模型许可及来源见 `runtime/licenses/SenseVoice-FunASR-MODEL-LICENSE-v1.1.txt`、`runtime/licenses/SenseVoice-NOTICE.txt`。须保留作者、出处和模型名称，并遵守其余条款。 |

NapCat 作为未经修改的完整独立组件运行，本项目通过公开接口调用它，不把其代码改为本项目 MIT，也不公开修改后的 NapCat 代码。其 [固定版本 README](https://github.com/NapNeko/NapCatQQ/blob/v4.18.42/README.md) 说明混合许可及未经授权基于其代码开发的限制；上游另行取得的授权不会自动授予本项目。完整分发包中的原生附加组件、第三方库及声明须一并保留，不将这些附加组件拆成单独 SDK 分发。

## 内置 Linux 二进制的对应源码

`runtime/linux-sources.lock.json` 按内置 Ubuntu 基础归档中的已安装包和新增 deb 的 `Source` 字段记录去重后的精确源码包版本、原始下载来源和摘要。公开完整安装包时，必须同时提供与该版 APK 对应的 `dist/*-linux-sources.zip`，其中包含相应 `.dsc`、上游原始源码归档、Debian/Ubuntu 补丁与构建文件、清单和使用说明；不能用仅有下载首页或后续版本的源码代替。此源码附件不需要安装到手机。

同一源码附件另含 qrcode-terminal 的原始 PyPI sdist 与本地通用 wheel 打包配方；它的 MIT 许可与 Linux 包的 GPL/LGPL 等许可分别适用。

`runtime/wheel-native-sources.lock.json` 另将实际 wheel 及所含库逐项摘要绑定到精确源码/构建材料。`tools/fetch-wheel-native-sources.py` 校验这些固定输入，并将其源码、配方、完整声明及 `runtime/wheel-native-BUILDING.txt` 加入同一个 `*-linux-sources.zip` 的 `wheel-native/`，不增加必须安装到手机的文件。动态 LGPL 库可兼容替换；静态 libiconv 提供完整 lxml 使用方源码以便重建/重链接。允许为调试修改进行必要逆向，不增加禁止替换、重链接或要求使用发布者签名密钥的限制。使用者可按原许可修改相应库、重打 wheel、更新清单并重新构建签名 APK。

原生 PRoot/talloc/shmem 的对应源码仍在独立的 `*-native-sources.zip`；本项目 Java/Python/HTML 自有与复用源码随仓库及项目源码 ZIP 提供。完整发行时分别核对这些源码与实际二进制版本，保留与 APK 同页、免费取得源码的入口。GPL/LGPL 的对应源码及替换权利不因 App 签名或压缩归档改变；用户可以修改相关组件并重新构建、替换和签名。具体义务按组件许可，不以本段代替许可全文。

## 仍由手机从官方来源取得的 QQ 客户端

Linux QQ 3.2.34-53644 仍从固定腾讯官方 HTTPS 地址下载到本 App 私有目录并校验摘要，仅解包本 App 的独立副本，不读写系统 QQ 或其他机器人的资料。该地址属于腾讯官方分发域名，不要求先访问 GitHub；真实地区网络可达性仍以设备现场为准。

本项目没有腾讯对 QQ 客户端的公开再分发授权，因此完整 APK 不内置 QQ deb，也不随 Release 重新分发。腾讯的 [QQ 软件许可及服务协议](https://ti.qq.com/agreement/index.html) 对复制、汇编、出版及实施相关知识产权有书面授权要求；QQ 包内的 Chromium/Electron 第三方许可证不能替代腾讯对 QQ 本身的授权。QQ、腾讯标识及其自带组件继续适用原条款，不作为本项目开源代码重新授权。

SnowLuma、LLBot 与 Lagrange 未集成；候选评估见 `spec/qq-access.md`。未复制参考项目素材、真实账号、密钥、Cookie、数据库或聊天记忆。

## 构建工具

Android SDK / Build Tools 遵守 Android SDK 条款及各自许可，Amazon Corretto / OpenJDK 作为本项目私有构建工具使用；它们不打包进 APK。来源分别为 [Android 官方](https://developer.android.com/tools) 与 [Corretto 官方](https://docs.aws.amazon.com/corretto/latest/corretto-17-ug/downloads-list.html)。
