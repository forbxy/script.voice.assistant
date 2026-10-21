# 开发工具

当前目录保留构建、组件清单维护，以及现有测试仍引用的工具。

| 文件 | 用途 |
| --- | --- |
| `build_speech.py`、`matcha_worker.c` | 构建 ASR / Matcha 桥接程序 |
| `build_decoders.py`、`build_cmcc.py`、`build_nanosic.py`、`build_r22.py`、`remote/` | 构建遥控器音频解码库，保留对应 C 源码和许可证 |
| `generate_components.py`、`codex_component.json`、`codebuddy_component.json`、`components-linux_x86_64.json` | 生成 ARM／x86_64 固定版本的依赖下载清单 |
| `generate_settings.py` | 设置和语言资源的生成工具 |
| `prepare_codex.py`、`prepare_websocket.py` | 准备开发环境中的组件或 WebSocket 依赖 |
| `deploy.py`、`device_check.py` | 早期部署工具及其设备检查脚本；部署设置辅助函数仍由测试引用 |
| `profile_layout.py`、`remove_unused_asr_models.py`、`iflytek_esr_protocol.py`、`iflytek_offline_flow.py` | 现有测试引用的迁移、清理和实验协议辅助模块 |

2026-10-20 已将历史测试报告、截图、试听音频、旧版本备份和一次性诊断、部署脚本移到
[`../.cache/dev-archive/20261020/dev/`](../.cache/dev-archive/20261020/dev/)。

完整原路径、归档路径、文件大小和 SHA256 见
[`index.json`](../.cache/dev-archive/20261020/index.json)。所有文件按原目录结构保留；
归档脚本中的路径仍基于原位置，重用时应先按索引恢复原路径。

历史[语音识别模型测试汇总](../.cache/dev-archive/20261020/dev/语音识别模型测试汇总.md)也已归档。
插件打包仍使用根目录的 `build_package.py`，开发目录与归档目录均不进入安装包。

## x86_64 构建

下载 sherpa-onnx 1.13.8 的 `linux-x64-shared-lib` 包及同版本 `sherpa-onnx/c-api/c-api.h`，解压运行库后构建小型桥接程序：

```sh
python dev/build_speech.py --arch linux_x86_64 \
  --cc 'zig cc -target x86_64-linux-gnu.2.27' \
  --runtime-dir /path/to/sherpa/lib --header-dir /path/to/header
```

`--cc` 也可使用兼容目标系统 glibc 的 C 编译器。模型及 sherpa-onnx 运行库仍由插件按需安装，桥接程序无需捆绑另一套 x86_64 glibc。

遥控器解码器使用目标系统对应的 FFmpeg 头文件和链接库，以库的 ABI 主版本选择，独立于 Kodi 小版本。LE 12 的 60／58 和当前 LE 13 的 63／61 均保留：

```sh
python dev/build_decoders.py --compiler-prefix '' --arch linux_x86_64 \
  --avcodec 63 --avutil 61 --include-dir /path/to/ffmpeg \
  --library-dir /path/to/target-libraries
```

构建记录、文件摘要和对应源码位于 `resources/licenses/`；生成的平台桥接程序和解码库位于 `resources/native/linux_x86_64/`，需要一起提交 Git 并打包。x86_64 Codex 使用同版本官方 app-server、code-mode-host 和配套 bwrap；ARM 继续使用现有 CE 适配版本。
