# Music Together Companion

AstrBot 插件，让机器人加入 Music Together 房间，按歌曲播放进度分析音频，并在房间聊天中发送简短的实时听歌反应。

## 功能

- 加入指定房间，或自动加入/创建房间
- 下载 MP3 并按时间切片，交给 Gemini 做音频分析
- 播放、暂停、恢复、切歌和拖动时跟随播放状态
- 音频不可用时退化为仅根据歌曲元数据生成反应
- 可选调用本机 AstrBot API 处理聊天回复和歌曲收尾

## 安装

将本目录作为 AstrBot 插件安装，或在插件市场中安装后重启 AstrBot。插件需要 AstrBot 4.22 或更高版本，以及 `requirements.txt` 中的依赖。

在 AstrBot 插件配置中填写 `server_url`、`identity_secret`、`gemini_endpoint`、`gemini_model` 和 `gemini_key`。如果需要让 AstrBot 负责聊天回复，再填写 `astrbot_api_key`；留空时只发送听歌反应。

所有密钥只应放在 AstrBot 配置中，不要提交到 Git。`config.example.json` 只用于说明字段。

## 独立运行

插件入口是 `main.py`。保留 `bot.py` 是为了兼容已有的 systemd 部署：复制一份本地配置到 `config.json` 后运行 `python bot.py`。发布包不要包含真实的 `config.json`、`state.json` 或 `.astrbot_key`。

## 隐私和网络

启用音频分析后，歌曲片段会发送到你配置的 Gemini endpoint；启用聊天回复后，房间聊天内容会发送到 Gemini 或 AstrBot API。请使用可信的 endpoint，并在多人房间中提前告知参与者。

## License

MIT
