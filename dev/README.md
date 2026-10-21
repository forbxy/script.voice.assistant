# 开发工具

当前目录保留构建、组件清单维护，以及现有测试仍引用的工具。

| 文件 | 用途 |
| --- | --- |
| `build_speech.py`、`matcha_worker.c` | 构建 ASR / Matcha 桥接程序 |
| `build_decoders.py`、`build_cmcc.py`、`build_nanosic.py`、`build_r22.py`、`remote/` | 构建遥控器音频解码库，保留对应 C 源码和许可证 |
| `generate_components.py`、`codex_component.json`、`codebuddy_component.json` | 生成固定版本的依赖下载清单 |
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
