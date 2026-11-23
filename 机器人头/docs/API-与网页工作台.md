# API 与网页工作台

## 启动

先停止仍在运行的旧命令行访谈，避免两个进程同时占用麦克风。
在开发板已登录的桌面终端执行：

```bash
cd /home/nvidia/Desktop/心理模型
bash scripts/launch-web.sh
```

启动后自动打开 `http://127.0.0.1:18088`。也可使用桌面的“心语访谈”启动项。
需要只启动 API、不自动打开浏览器时执行 `bash scripts/web.sh`。
服务使用 Python 标准库，不需要安装 Node、FastAPI、uvicorn 或下载网页资源。
API 和网页默认只监听本机。若端口已占用，可传 `--port 18089`，页面和 API 使用同一个端口。
终端保持运行；Ctrl+C 会停止服务并通知正在进行的访谈结束保存。

当前受限开发环境不能创建监听端口或启动 Chrome，已用真实 HTTP 处理器的内存请求测试替代
网络集成测试；尚未完成浏览器画面与真实音频联调。

## 页面功能

- “语音 + 按钮”：开发板上的麦克风持续采集，耳机或扬声器播放，仍走 PulseAudio WebRTC AEC。
- “仅按钮与文字”：不打开音频设备，用于表单操作或无麦克风时验证完整流程。
- 开始后主动显示并提问当前题；0–3 四档选择直接保存并推进。
- 支持补充文字、重听、上一题、跳过、结束并保存。
- 显示语音准备、播放、识别、监听、结束状态，以及逐题进度、对话和设备错误。
- 历史会话可查看回答、展开录音播放器，导出 JSON 和完成后的总结。
- 已保存的三个维度计分放在折叠区域，保留不作自动诊断的原则。

只有实际录音的回答有 WAV。按钮和文字回答同样会存档，但不会生成虚假的录音。
页面不使用浏览器麦克风；所有录放均发生在开发板，远程打开网页不会把远端电脑当成麦克风。

## 简体与缓存

`configs/dass21.zh.json` 题目和选项统一为简体；对应繁体原文分别保留为 `source_text` 和
`source_response_options`。转换不改变题号和计分键。
JSON 回答记录同时保留来源文本。旧会话在 API 展示时转换题目，原始 JSON 下载仍是原始存档。
发音前映射回已有语音缓存对应的文本，同音的繁简转换不要求重新生成所有题目语音。

## API

- 人类可读说明：`GET /api/docs`
- OpenAPI 3.0.3：`GET /api/openapi.json`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/status` | 当前会话 id 与运行阶段 |
| GET | `/api/questions` | 题目、版本来源与选项 |
| GET | `/api/sessions` | 最近 100 场会话摘要 |
| POST | `/api/sessions` | 创建访谈；`mode` 为 `voice` 或 `text` |
| GET | `/api/sessions/{id}` | 完整会话快照、阶段、当前题、事件和计分 |
| POST | `/api/sessions/{id}/actions` | 选分、文字回答和流程操作 |
| GET | `/api/sessions/{id}/answers.json` | 原始 JSON 下载 |
| GET | `/api/sessions/{id}/summary.txt` | 完成总结下载 |
| GET | `/api/sessions/{id}/audio/{file}.wav` | 回答录音 |

创建：

```bash
curl http://127.0.0.1:18088/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"mode":"voice"}'
```

选择 2 分：

```bash
curl http://127.0.0.1:18088/api/sessions/会话编号/actions \
  -H 'Content-Type: application/json' \
  -d '{"action":"answer","score":2,"question_id":"Q01"}'
```

文字回答使用 `{"action":"answer","text":"二分"}`。
流程操作的 `action` 可取 `repeat`、`previous`、`skip`、`options`、`stop`。
建议同时传 `question_id`，题号已变化时拒绝旧操作，避免重复点击写到下一题。

语音模式 POST 表示操作已入队；轮询 GET（页面默认每秒一次）观察实际执行与保存结果。
文字模式立即执行。任何时刻只允许一个访谈，语音工作线程完全退出后才能新开。
语音准备阶段可以停止，但其他操作需等设备准备完成。

常见响应字段：`session_id`、`status`、`phase`、`mode`、`current_index`、`current_question`、
`completed_count`、`questions`、`events`、`scoring`、`overall_summary`、`error`。
`phase` 包括 `preparing`、`listening`、`speaking`、`transcribing`、`ready`、`stopping`、
`stopped`、`completed`、`error`、`archived`。

错误返回 `{"error":"说明"}`；400 为参数错误，403 为非本机 Host 或跨站 Origin，
404 为资源缺失，409 为已有访谈、旧题号或状态冲突。异步音频启动失败会反映为
会话 `phase: error`、`status: stopped`，页面会显示 `error` 内容并允许新建文字会话。

本版本没有跨设备鉴权、公开互联网部署、删除会话、断点恢复或视频录制接口。

## 收音状态与未识别录音（2026-10-26）

语音会话快照增加 `audio` 对象，包括输入 RMS、平滑峰值、自适应门限、是否检测到发音、
累计采集帧数和检测到的语音段数。页面显示输入电平及“正在接收你的声音”。
音频回调只更新轻量状态，网页快照由处理循环限频更新，不在回调中做文件写入或模型推理。

访谈的能量检测改为带背景跟踪和迟滞的自适应门限，轻声默认起始门限约 0.0025，
`--speech-threshold` 作为上限。短回答要求至少 120ms 有效语音，并允许起始检测窗口中有短暂断音。
这仍是能量检测，不是经过声学验收的神经网络 VAD；嘈杂环境和扬声器回声仍需真机测试。

当已采集的语音段没有识别文字时，会保留 `audio/Qxx-unrecognized-*.wav`，
在 JSON 的 `unrecognized_segments` 中记录题号、时长和原因，并明确播报“没听清，请再说一次”。
页面右侧可展开这些录音。这些录音不自动形成题目答案，不计分。
如果麦克风连续三秒没有回调数据，则明确报告设备数据中断，不无限显示正在监听。
