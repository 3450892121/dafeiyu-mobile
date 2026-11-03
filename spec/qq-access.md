# 普通 QQ 小号接入评估

本页是源码及项目方资料的静态评估，不是本机登录、手机测试或风控比较结果。核查日期为 2026-11-03。默认 QQ 内核尚未选定，后续版本变化须重新核对。

## 用户目标

普通 QQ 小号、免 Root 安卓、全新独立实例、单 App 优先。不能使用原机器人账号、原服务或官方 QQ 机器人通道来替代这个目标。

## 候选与证据

| 实现 | 已查到的事实 | 本项目尚缺的证据 |
|---|---|---|
| NapCat Shell | 官方说明提供 Termux + proot-distro 安装与启动路径；参考大肥鱼使用其 OneBot 接口 | 单 App 内置运行层、指定手机可用性及长期运行；再分发许可须按实际组件单独核对 |
| SnowLuma | 当前发行版有 Linux arm64；提供 OneBot、空间、消息历史、语音及文件相关动作；仍需接入 QQ 客户端进程 | 官方手机说明标为实验性，指出 proot 下 ptrace 注入可能失败；不能把 arm64 发行包当作原生安卓库 |
| LLBot | 当前文档提供 QQ 客户端接入与独立实现 NTQQ 的纯协议两种模式 | 未核实可内嵌的安卓 ABI、运行依赖、全部大肥鱼接口及手机稳定性；不能据此宣称风控更少 |
| Lagrange.Core V2 | 官方仓库说明当前维护线为 V2；提供 .NET 及 C ABI 包装；旧 V1 已停止维护 | Android 适配、许可、QQ 空间及参考接口完整性未完成验证，不直接引入 |

## 参考代码与接口差异

参考项目的 `02-服务器版/bot/onebot.py` 提供私聊、群聊、文件上传、消息/历史、空间和 Cookie 调用；`flow_media.py` 还用 `get_file` 作为普通附件读取兜底。

SnowLuma 文档的 `get_file` 仅覆盖图片、语音缓存，群文件需改用 `get_group_file_url`。因此“动作名称兼容”不等于不改就能用。语音、图片路径、历史游标、引用编号、空间 Cookie、发送超时与去重均要按本实例场景验证。

SnowLuma 当前许可允许按条款进行非商业使用，但同时限制基于它开发另一个项目；原生附加组件另为专有组件，不授予单独再分发等权利。因此在授权范围明确前，本项目只做接口评估，不复制其源码或原生组件到 APK。不能把它改标 MIT。

没有找到可据以量化这些实现账号风控率的比较证据。保持稳定资料、有限重连、验证挑战停止和明确发送回执是本项目的运行要求，不能表述成已证实降低封号率。

## 原始来源

- NapCat 手机路径：https://napneko.github.io/guide/boot/Shell#termux
- NapCat 许可：https://github.com/NapNeko/NapCatQQ/blob/main/LICENSE
- SnowLuma 发行：https://github.com/SnowLuma/SnowLuma/releases
- SnowLuma 手机限制：https://snowluma.github.io/zh/docs/guide/deploy/mobile
- SnowLuma 动作目录：https://snowluma.github.io/zh/docs/api
- SnowLuma 文件限制：https://snowluma.github.io/zh/docs/api/extended/get_file
- SnowLuma 许可：https://github.com/SnowLuma/SnowLuma/blob/main/LICENSE
- LLBot 两种实现：https://luckylillia.com/guide/introduction
- Lagrange 当前维护线：https://github.com/LagrangeDev/Lagrange.Core
