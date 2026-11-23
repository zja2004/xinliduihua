# Jetson Orin NX 全语音心理支持原型

## 小学版题单与计分（2026-11-03）

集成网页默认改为项目自编 `PRIMARY-21` 小学一周感受题单，保持21个主题位置，使用更温和的日常表达。它不是DASS-21等效版本，也不使用DASS常模或乘二计分。21题每项0–3，三维各0–21，总分0–63。

固定朗读与三个开放问题后，另收集三个明确的感受选项，形成0–9补充自评；每维0–3与21题对应维度均值并列。不会从朗读流畅度、故事悲喜或表情推算心理分数，也不会强行令两份分数一致。未答齐显示缺失，分数供老师复核，未经信效度验证。

完整题单和规则见 [小学21题-主题映射与计分](docs/小学21题-主题映射与计分.md)。原DASS版本仅供专业人员显式选择：`bash scripts/web.sh --questions configs/dass21.zh.json`；旧档案按原计分显示。


## 机器人心理访谈集成入口（2026-11-02）

```bash
bash scripts/clinic.sh
```

集成版包含基本资料、心理访谈、固定阅读、三个开放任务、录音录像和 SQLite 老师管理台。
老师页面为 `http://127.0.0.1:18088/teacher`；集成后端和数据统一位于相邻的 `机器人头` 项目。
详细说明见 `机器人头/docs/心理访谈与机器人集成.md`。硬件需在连接设备的桌面会话验收。


## 机器人头集成（2026-10-28）

```bash
cd /home/nvidia/Desktop/机器人头
bash scripts/robot.sh
```

启动 `http://127.0.0.1:18088` 并进入人物/语音唤醒待机。唤醒后先交谈，说“开始测评”进入21题；
结束时播报数值并导出整场对话、分轨音频、JSON 和 ZIP。网页有设备状态与完整包下载。
当前真实摄像头/头板串口未被系统识别，头部运动默认模拟；详见 [机器人头运行与验收](docs/机器人头运行与验收.md)。


## 网页工作台与 API（2026-10-26）

```bash
cd /home/nvidia/Desktop/心理模型
bash scripts/launch-web.sh
```

打开 **http://127.0.0.1:18088**，可开始语音访谈、按钮选分、重听、返回修改、跳过、
结束保存，查看历史记录、播放录音和导出 JSON。题目与选项统一为简体。
也可双击桌面的“心语访谈”启动项；只启动服务使用 `bash scripts/web.sh`。
启动前请先停止旧的命令行语音访谈。

API 文档位于 **/api/docs**，机器可读规范位于 **/api/openapi.json**。
详细说明见 [API 与网页工作台](docs/API-与网页工作台.md)。
当前环境禁止监听端口和启动浏览器，接口与流程已通过内存 HTTP 测试，浏览器及真实耳机联调待本机桌面验证。


## 扬声器全双工入口（2026-10-24）

在本机桌面终端运行 `bash scripts/duplex.sh`，默认使用 9B 模型。
该入口建立 PulseAudio WebRTC 回声消除输入/输出，持续监听麦克风；
检测到连续约 90ms 的超阈值声音后中断旧回复播放，停顿后识别新一轮。
Ctrl+C 关闭通话并卸载本次创建的回声消除模块，不修改系统默认设备。
依赖缺失时脚本运行 bootstrap，需要联网。

选择物理设备可先执行 `pactl list short sources` 和 `pactl list short sinks`，再运行：

```bash
ORIN_MIC_SOURCE=物理输入名称 ORIN_SPEAKER_SINK=物理输出名称 bash scripts/duplex.sh
```

可追加 `--speech-threshold 0.025` 降低噪声误触发，或 `--profile baseline`
使用 4B。必须使用同一回声消除模块的输入与输出；脚本会拒绝绕过此路由。
仍使用能量检测与整段回复合成，未实现逐句流式合成；插话后丢弃过期合成结果，
正在执行的 TTS 运算需要结束后才能处理新一轮，LLM 在下一条流数据到达时检查取消。
回声消除效果和实际打断延迟需要实机验证，不能保证噪声下可靠分离人声。

本次执行环境没有 `/dev/snd`，桌面音频连接被限制，Python 环境也缺少
sounddevice；因此这里只完成实现及离线测试，尚未成功启动设备通话。
下面原有 `scripts/call.sh` 仍是轮流收听/播放入口。

## 当前 ARM64 迁移状态

### 自动语音通话入口

在本机安装 ARM 语音依赖后启动（需要系统 PortAudio 库）：

```bash
bash scripts/bootstrap.sh
bash scripts/call.sh
```

加载完成后自动监听麦克风；说话后停顿约 1 秒，自动识别并提交、生成回复、合成播放，再开始下一轮。无需按 Enter 或确认转写，Ctrl+C 挂断，音频与对话不自动保存。默认使用已下载的 9B，也可以 `bash scripts/call.sh --profile baseline` 使用 4B。

这是轮流收听/播放的通话原型：思考和播放阶段不监听，暂不支持插话打断或声学回声消除。请等“正在听”再开始下一轮。当前采用能量阈值与停顿状态机，不是神经网络 VAD；持续噪声可能误触发，45 秒切分是资源上限，不代表理解语义上的句末。支持 `--silence-ms 1500` 延长停顿容忍时间、`--speech-threshold 0.025` 提高噪声门限、`--input-device 设备编号` 指定麦克风。默认阈值为 0.015，轻声漏检可适度降低。尚未完成真实麦克风、扬声器和整机内存实测。

### 9B 文本对话 MVP

已接入本地 `models/Qwen3.8-9B-Distill/Qwen3.8-9B-Q4_K_M.gguf`。在本机终端执行：

```bash
bash scripts/mvp.sh
```

首次启动缺少 ARM 引擎时会调用 Jetson 构建脚本，需要访问 GitHub；已构建后可离线运行。文本入口只使用系统 Python 标准库，不依赖旧 `.venv` 或语音依赖。默认 9B、2048 上下文、512 输出 token、GPU 卸载；输入 `/clear` 清除历史，`/exit` 或 Ctrl+C 退出并关闭本次模型进程。回答完成后显示耗时，对话不保存。可用 `bash scripts/mvp.sh --profile baseline` 切回 4B，或追加 `--max-tokens 1024` 调整输出预算。

这一路径为终端文本 MVP，未启用麦克风或 TTS。CLI 与会话控制已经测试；当前受限执行环境不能创建 socket，因此尚无 9B 真实生成、GPU 内存或性能验证结果。

当前目标机已识别为 Orin NX 16GB、ARM64、Python 3.10。目录中遗留的 Python 3.12 虚拟环境、x86 llama.cpp 和桌面 CUDA 12.8 动态库不能直接运行。GGUF 和 ONNX 模型文件可以复用，推理引擎与 Python 原生扩展需要在目标机重新安装或编译。

迁移入口如下（需要终端能访问依赖下载站点）：

```bash
bash scripts/bootstrap.sh
bash scripts/build_jetson.sh
.venv/bin/orin-voice doctor
.venv/bin/orin-voice voice-smoke
.venv/bin/orin-voice ask --profile baseline --ctx-size 2048 '你好。'
```

bootstrap 会将检测到的 Python 版本或架构不匹配的旧环境移到 `.venv.backup.*`，随后创建本机环境；不删除旧环境。不要用 `requirements-host.lock.txt` 安装 ARM 依赖，它是原开发机的记录。Jetson 构建脚本会查找 `/usr/local/cuda/bin/nvcc`，保留固定 llama.cpp 版本与 SM87 编译设置。启动时检查 ELF 架构，并仅使用所选引擎旁的库；ARM 不再扫描桌面 CUDA 库。

重构边界：保留 `audio.py`（语音）、`llm.py`（本地协议）、`server.py`（进程管理）和 `assets.py`（模型校验）的现有职责。后续将设备资源参数独立于模型 profile 管理，并按模型配置推理模式与输出预算。当前仍使用已有两组 4B 模型；9B 资产尚未接入，不能仅修改名称就当成已部署。先分别验证文本推理、WAV 语音闭环，再测两者常驻时的统一内存与响应延迟，之后再决定是否增加流式播放和语音打断。

迁移检查已通过 13 项单元测试、Python 编译和 shell 语法检查；尚未完成 ARM 原生依赖安装、CUDA 编译或 GPU/语音实测。当前执行环境禁止网络 socket，依赖下载须在可联网的本机终端执行。

已确定两组：**baseline = Qwen3.5-4B-Q4_K_M**，**experimental = 社区 Qwen3.8-4B-Distill-Q4_K_M**。
两组共用 sherpa-onnx 中文 ASR 与 Kokoro 中文 TTS，模型按顺序加载，禁止同时常驻两个 LLM。

> 当前是研究/开发原型，不是学生心理服务成品。尚未接入正式量表、心理科普 RAG、人工危机支持通道或心理档案系统。规则提示不代表安全评估，不得直接用于真实学生筛查、诊断或危机处置。实验模型不是 Qwen 官方的 3.8-4B。

## 已实现与未验证

- 两套固定 revision 的 GGUF 下载、分段重试、SHA256 校验、统一配置切换。
- 中文流式 CTC ASR 引擎、Kokoro v1.1-zh INT8 合成，支持 WAV 文件输入输出。
- 手动按键录音、确认转写、语音回答；没有偷偷开启麦克风，没有自动保存真实会话。
- 仅绑定 `127.0.0.1` 的 llama.cpp 推理服务，拦截推理请求跨域重定向、禁用环境代理。
- 合成案例 A/B 测试：中文回答、首个可见 token、首句文本、首句音频数据就绪时间、待人工评审的安全边界。
- 目录锁限制本项目同时只启动一个受管理 LLM；退出时仅关闭自己启动的进程。
- **未验证**：Orin 的 JetPack/CUDA 兼容、整机内存、真实首音延迟、麦克风/扬声器、校园噪声及学生人群表现。
- **未实现**：自动 VAD 发言结束、回声消除、语音打断、逐句流式播放、专业安全分类器、正式档案和报警闭环。

当前 `talk` 是先生成完整短回复，再合成播放；`compare --tts` 会在首句文本出现时立即合成首句以单独测量 PCM 就绪时间。二者的延迟指标不能混同。

## 模型与目录

| 环节 | 固定选择 | 位置 |
| --- | --- | --- |
| 基线 LLM | Qwen3.5-4B-Q4_K_M，Unsloth 转换 | `models/llm/Qwen3.5-4B-Q4_K_M.gguf` |
| 实验 LLM | empero-ai Qwen3.8-4B-Distill-Q4_K_M | `models/llm/Qwen3.8-4B-Distill-Q4_K_M.gguf` |
| ASR | sherpa-onnx streaming Zipformer small CTC 中文 INT8，2025-04-01 | `models/asr/` |
| TTS | Kokoro INT8 multilingual v1.1，默认中文音色 `zf_001`、speaker 3 | `models/tts/` |
| Python 运行库 | sherpa-onnx 1.13.8 | `.venv/` |
| LLM 运行库 | llama.cpp b11669 | `.runtime/` |

下载清单、原始许可说明和固定地址见 `configs/assets.json`。GGUF SHA256 来自模型仓库元数据；两个语音压缩包 SHA256 来自本次官方发布文件的首次下载记录，不是上游数字签名。模型、数据、音色、词典及运行库的许可需分别审查，不能把代码许可推广到全部资产。

## 在当前开发电脑启动

在项目目录执行：

```bash
bash scripts/bootstrap.sh
.venv/bin/python -m orin_voice.assets asr tts baseline experimental --workers 2
.venv/bin/python scripts/install_llama.py --cuda
.venv/bin/orin-voice doctor
```

`install_llama.py --cuda` 仅供 x86_64 NVIDIA 开发电脑，使用 CUDA 12.8 桌面包；无 NVIDIA GPU 可省略 `--cuda` 安装 CPU 包，并使用 `--gpu-layers 0`。这些包不能拿到 Jetson 上直接用。

如果系统缺少 `python3-venv`，bootstrap 使用无 pip 的隔离环境，并从 PyPI 下载、核对 pip wheel 后安装，不修改系统 Python、不需要 sudo。`requirements-host.lock.txt` 记录本次开发电脑依赖，不是 Jetson 的通用锁文件。

### 先检查语音链路

```bash
.venv/bin/orin-voice voice-smoke
.venv/bin/orin-voice speak '你好，你想先聊聊最近发生的哪件事？' --output reports/hello.wav
.venv/bin/orin-voice transcribe reports/hello.wav
```

`voice-smoke` 是合成语音再识别的闭环，不证明真实麦克风、听感或临床质量。现有样例已出现“晚上”被转写成“为上”的误识别，不能把关键安全陈述直接当成确认事实。

### 分别测试两组文本与语音回答

```bash
.venv/bin/orin-voice ask --profile baseline '最近考试很多，我有些紧张。' --wav reports/baseline.wav
.venv/bin/orin-voice ask --profile experimental '最近考试很多，我有些紧张。' --wav reports/experimental.wav
```

每条命令自动启动本组 LLM、等待就绪、生成后关闭。主机端口冲突会报错，不会杀掉其他服务。输出 WAV 是用户显式请求的保存；文件权限设置为 0600。

### 手动语音对话

```bash
.venv/bin/orin-voice talk --profile baseline
.venv/bin/orin-voice talk --profile experimental
```

仅在按 Enter 启动后访问麦克风；再次 Enter 停止，再确认识别文本才提交。Ctrl+C 停止。该入口还不是免按键、可自动打断的语音产品。

麦克风播放需要 PortAudio。Jetson 可由管理员在本机安装 `libportaudio2`。当前开发电脑也支持从 `.runtime/portaudio/usr/lib/<架构>-linux-gnu/` 加载项目内提取的库，不改系统目录。设备权限、USB 声卡及默认输入/输出仍需在目标机器确认。

## A/B 对比

```bash
.venv/bin/orin-voice compare --tts --save-audio
.venv/bin/orin-voice compare --profiles experimental baseline --tts --limit 3
```

只使用 `configs/eval_cases.json` 中虚构的 8 个案例，覆盖考试压力、诊断请求、当前安全线索、否定表达、依赖、隐私、伪造分数和指令边界。每组先热身，使用相同提示词、温度 0、192 token 输出预算及 4096 上下文。报告写入 `reports/comparison-*/`。

- `first_visible_token_s`：本次请求开始到首个可见文本，不包含隐藏推理。
- `first_sentence_s`：本次请求开始到第一句带结束标点的可见文本；没有标点时为生成结束。
- `first_sentence_audio_ready_s`：请求开始到第一句 PCM 合成完成，**不是扬声器已播放**。
- `completion_s`：流式请求读取完成的墙钟时间；启用 TTS 回调时包含该合成耗时，不能当成纯 LLM 吞吐。
- 以上均不包含麦克风、发言结束判断、ASR、模型加载和设备播放，不是用户完整等待时间。
- `rule_flags` 是少量规则的调试提示；未命中绝不表示安全。每个案例的人工评审字段保持 `null`，由专业人员填写。

不要把真实学生对话直接放进默认实验集。不要将评测集再次用于微调后在同一集合报告效果。

## 在 Orin NX 8GB 上准备

先在 Orin 上核查：

```bash
uname -m
cat /etc/nv_tegra_release
free -h
command -v nvcc
```

将源代码、配置和已经校验的 `models/` 复制过去；**不要复制开发电脑的 `.venv/`、桌面 `.runtime/llama-bin/` 或 x86 PortAudio 库**。源代码不依赖当前电脑的绝对路径。

```bash
bash scripts/bootstrap.sh
bash scripts/build_jetson.sh
.venv/bin/orin-voice doctor
.venv/bin/orin-voice voice-smoke
.venv/bin/orin-voice ask --profile baseline --ctx-size 2048 '你好。'
.venv/bin/orin-voice ask --profile experimental --ctx-size 2048 '你好。'
.venv/bin/orin-voice compare --ctx-size 2048 --tts --limit 3
```

`build_jetson.sh` 只接受真实 Jetson，固定 CUDA 架构 87，限制构建并行度 2，不申请 sudo，不修改功耗模式或系统服务。需要已正确安装的 JetPack/CUDA、Git、CMake 和 C++ 编译器；当前未在目标 Orin 上执行。内存不足时应先降低上下文/输出预算并核查常驻进程，而不是同时加载两套模型。

当前桌面默认上下文为 4096，Orin 初测建议用 `--ctx-size 2048`；均不是已经证明适合 NX 8GB 的配置。需要目标机实测后确定最终参数。

## 验证与日志

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src scripts
bash -n scripts/bootstrap.sh scripts/build_jetson.sh
```

`models/receipts/` 保存下载校验记录；`.runtime/server-*.log` 用于启动错误；生成内容不默认写入服务日志。聊天历史只在当前进程内存中保留有限轮次。报告与样例录音属于开发资料，不能当成已经实施的心理档案系统。

## 官方参考

- https://huggingface.co/Qwen/Qwen3.5-4B
- https://huggingface.co/empero-ai/Qwen3.8-4B-Distill
- https://github.com/ggml-org/llama.cpp
- https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-ctc/zipformer-ctc-models.html
- https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html


## 21 题语音访谈入口（2026-10-26）

在开发板已登录桌面的终端运行：

```bash
cd /home/nvidia/Desktop/心理模型
bash scripts/interview.sh
```

默认使用 `configs/dass21.zh.json` 的 DASS-21 官方托管中文题目，以简体显示，保留繁体来源文本并复用已有语音缓存。
具体量表来源、语音选分及计分说明见 [DASS-21 语音实施说明](docs/DASS21-语音实施说明.md)。
如需切回开放式访谈，可通过环境变量指定原有示例题单：

```bash
ORIN_QUESTIONS=configs/interview.example.json bash scripts/interview.sh
```

启动后会提示录音和保存事项，并主动提问。DASS-21 每题说“零分 / 一分 / 二分 / 三分”，
选好即自动进入下一题，无需额外说“确认”；说“上一题”可以修改。可以先补充描述，模型不会从描述猜分。开放式访谈说“回答完了”进入下一题。
说“重复”/“再说一遍”重读当前题，说“跳过这题”跳过，说“结束访谈”保存后停止。
这些控制指令需要单独说，避免将回答中的类似词语误当命令。程序不会凭一次停顿跳题。

输出在 `sessions/会话编号/`：

- `answers.json`：全部 21 题、状态、回答分段、原文合并和关键词主题、音频相对路径。
- `audio/Q01-001.wav` 等：每题每次补充分别保存，不混入助手输出音轨；麦克风信号经过 AEC。
- `summary.txt`：完成全部题目后导出总结。DASS-21 为完成情况与一般支持信息；开放式访谈才使用模型总结。
- DASS-21 的 `answers.json` 另含每题 `score` 和三个维度的 `scoring`，供研究者或专业人员查看。

每段转写完成即保存 WAV 并原子更新 JSON。集成入口现已增加整场音频、WebM 视频和 SQLite 老师管理端；中断恢复尚未实现。
ASR 仍在一句结束后处理整段音频，尚未改成边说边解码；打断使用 90ms 连续能量检测，
并非语义 VAD。默认停顿门限 1000ms，可使用 `--silence-ms 600` 试验，但可能切开正常停顿。

访谈模式直接用程序回应和提问，避免每题都等待 LLM。DASS-21 不启动 LLM，使用确定性计分；
开放式访谈在全场结束时调用本地模型总结。
普通 `duplex.sh` 对话支持第一句生成后即合成播放；TTS 按句合成，中断可放弃等待过期结果。
推理/合成的底层调用不保证立即中止，上一句仍可能在后台占用资源。

录音与播放必须通过 `scripts/duplex.sh` 建立的 PulseAudio WebRTC 回声消除路由。
这里不保证后端具体为 AEC3，也不能仅凭模块加载成功证明回声消除效果。
截至本次修改，代码与离线测试通过，**尚未在当前执行环境完成真实麦克风/耳机的同时录放、
误打断率、声学回声和端到端延迟验收**。若提示无法连接桌面音频服务，请在本机桌面终端启动。
