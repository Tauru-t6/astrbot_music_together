# 更新日志

## [0.2.2] - 2026-09-21

### 修复

- 上架合规：`bot.py` 日志统一使用 `astrbot.api.logger`（仅独立运行时回退标准 logging）。
- `download_audio` 中的同步 REST 请求改为 `asyncio.to_thread`，不再阻塞 AstrBot 事件循环。

## [0.2.1] - 2026-09-21

### 修复

- 蹲在自己创建的空房时不会跟进用户新开的房间；现在发现有真人的房间会主动离开空房加入。
- 新增 30 秒一次的房间列表轮询，蹲空房时也能及时发现用户开的房间。
- 处理服务端 `room:deleted` 事件（空房宽限期过后被服务端自动删除）。
- 自建空房改为安静等待真人加入，不再每 90 秒建房/退房循环。

本文件记录 Music Together Companion 的重要变更。

版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.2.0] - 2026-09-21

### 变更（不兼容）

- **房间聊天与歌曲收尾改为注入 AstrBot 原生管道**（webchat / aiocqhttp 适配器），不再走 HTTP 开放 API：注入消息落在真实会话桶里，memory 类插件可正常读写，也不再需要 API key。相应移除 `astrbot_chat_url` / `astrbot_api_key` / `astrbot_session` / `astrbot_username` / `astrbot_provider` 五个配置项，新增 `chat_session_id` / `chat_platform_id` / `chat_user`。
- QQ 桶注入：`chat_session_id` 填 QQ 会话字符串（如 `Tauru:FriendMessage:1125961157`）时经 aiocqhttp 适配器注入原号主会话；回复通过临时包装发送路径捕获，仅注入期间生效，不影响正常 QQ 流量。平台离线时跳过注入不报错。

### 新增

- 增加 `player:sync_request` 周期对时，按服务端权威时钟校正播放锚点，防止弹幕漂移。
- 增加暂停反应：暂停时以人设口吻发一句简短反应，恢复后可再次触发。
- 增加空房处理：加入的房间没有真人时自动退出并冷却，房间发现只加入有真人的房间；自己创建的房间则留在里面等真人加入，不再反复建房/退房。

### 修复

- 插件入口改为在 `initialize()` 生命周期中启动监听任务，避免 `__init__` 阶段没有运行中事件循环时崩溃。
- 插件内模块导入兼容 AstrBot 包加载和独立运行两种方式。
- 状态文件优先保存到 AstrBot 插件数据目录，避免插件升级或重装时丢失。
- 修复未配置 `identity_secret` 时调用音乐 URL 接口发生 `IndexError` 的问题。
- 移除插件模式下的 `logging.basicConfig`，避免覆盖 AstrBot 的日志配置。
- 统一 `gemini_model` 默认值为 `gemini-2.5-flash`，与配置面板一致。
- 移除代码中硬编码的私有人设、会话 ID 和 provider 默认值，改为与发布版默认配置一致。
- 分析节奏默认值调优为 30 秒片段 + 提前 25 秒预分析（线上调好的值）。
- Gemini 思考配置按模型方言自适应（Gemini 3 系用 `thinkingLevel`，旧模型用 `thinkingBudget`)，400 时自动切换并记忆。
- 模型输出 JSON 解析增加容错：Markdown 包裹、尾逗号、全角引号。
- Gemini 请求对 kdysite 中转和 localhost 端点自动直连绕开代理（1MB 音频段经代理上传会超时）。
- 歌曲结束且没有任何听歌笔记时仍会发送收尾消息。

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
