# 更新日志

本文件记录 Music Together Companion 的重要变更。

版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-09-12

首个公开版本，面向 AstrBot 插件市场发布。

### 新增

- 增加 AstrBot 插件入口 `main.py`。
- 增加插件元数据 `metadata.yaml` 和配置面板 schema `_conf_schema.json`。
- 增加 Music Together 房间加入、自动加入和自动创建流程。
- 增加播放、暂停、恢复、拖动和切歌状态同步。
- 增加 MP3 内存切片和 Gemini 音频分析。
- 增加音频不可用时的歌曲元数据分析降级模式。
- 增加可选的 AstrBot Chat API 聊天回复和歌曲收尾。
- 增加独立运行入口，兼容原有 systemd 部署方式。
- 增加依赖清单、示例配置、中文 README 和 MIT License。

### 改进

- 配置从固定服务器和本地文件改为可注入配置。
- Gemini endpoint 支持 `{model}` 占位符。
- 对音频 URL、模型响应、配置范围和聊天长度进行校验。
- 对 Gemini 文本请求增加异常处理和临时错误重试。
- 对 REST 查询参数使用 URL 编码。
- 插件停止时主动取消任务并断开 Socket.IO。
- 避免重复处理相同播放 revision。
- 暂停期间等待播放位置时保持任务挂起，不提前发送反应。
- 从发布目录移除生产密钥、服务器地址和运行状态文件。

### 已知限制

- 需要外部 Music Together 服务端，不包含音乐搜索和播放服务。
- 音频分析依赖 Gemini-compatible endpoint，模型延迟会影响反应数量和时间精度。
- 当前 MP3 切片器面向常见 MP3 文件，非 MP3 音频会进入降级模式。
- AstrBot 环境需要安装 `python-socketio[client]` 和 `curl-cffi`。
