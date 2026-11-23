**Psyche-R1 本机部署进度与国内镜像**

核验日期：2026-11-12。最新状态：北京时间2026-11-12 07:34已完成官方源文件下载与校验、F16转换、Q4_K_M量化和独立文本冒烟测试。下文403及下载中记录属于完成前的排查过程。

**本机完成结果**

- 17个源文件的大小及哈希全部校验通过，4个Safetensors权重分片合计15,231,271,912字节。
- Q4_K_M文件：`models/Psyche-R1/Psyche-R1-Q4_K_M.gguf`，4,683,073,984字节，约4.68 GB（4.36 GiB）；GGUF v3，339 tensors。
- Q4 SHA256：`0e7d9e348524fa53e1d746dd31c7e8b562a15d4e12d193201483ec7723193647`。
- 4096上下文、每例1024输出预算下，三个虚构中文场景全部获得可见回复，无截断。模型加载约7.59秒，文本首句约0.21–1.14秒，完整回复约1.22–2.66秒。这些时间不含语音识别、合成及播放。
- 已采样的推理进程内存峰值约4.97 GiB；采样不等于完整GPU峰值内存。现有服务关闭了详细日志，不能仅依据请求参数确认实际GPU卸载层数。
- 测试中部分回复超过提示要求的1–2句；允许跳过后的下一题应继续由访谈流程控制，不能由模型自行选择。该测试仅验证独立文本推理能够完成。

结果记录：`.runtime/psyche-deploy/smoke-20261111T233410Z-73473e.json`。现有访谈默认模型未切换，Psyche已作为独立`psyche`配置可用。

**2026-11-15 当前运行实例**

已启动本机机器人服务：

- 访谈网页：[http://127.0.0.1:18088/](http://127.0.0.1:18088/)
- 老师页面：[http://127.0.0.1:18088/teacher](http://127.0.0.1:18088/teacher)
- Psyche 本地推理端口：`127.0.0.1:18082`
- `/api/robot/status`显示`dialogue.phase=ready`，实际发送中文场景后返回了温和追问。

当前 Psyche 已接入机器人自由对话和心理访谈脚本：`scripts/robot.sh`与`scripts/interview.sh`默认使用`psyche`，仍可用`ORIN_CHAT_PROFILE=baseline`切回旧模型。固定题目、跳过、上一题、计分和老师报告仍由程序控制；机器人收到“不想回答/跳过”时会直接确认跳过，不让模型自行换题，自由回复最多保留两句。

本机当前没有`/dev/video*`或串口设备，因此状态页会显示摄像头和头部硬件未连接；这不影响已启动的文本模型，但真实人脸跟踪、麦克风录音、扬声器播放仍需接上设备后验收。

可运行以下命令进行独立文字聊天：

```bash
cd /home/nvidia/Desktop/机器人头
PYTHONPATH=src .venv/bin/python -m orin_voice.cli chat \
  --profile psyche --port 18082 --ctx-size 4096 --max-tokens 1024
```

**后台作业与进度**

实际作业入口为`scripts/deploy_psyche.py`，已用独立进程组在后台启动。它依次执行：断点续传、全部源文件大小及哈希校验、CPU转换F16、原生Q4_K_M量化、Q4哈希计算、三个虚构中文场景的独立文本冒烟测试。下载失败会刷新下载链接后重试；进度保留在设备本地，设备保持开机联网即可继续。

- 当前阶段及结果：`.runtime/psyche-deploy/job-status.json`。
- 最近下载进度：`.runtime/psyche-deploy/transfer-progress.json`。
- 作业主日志：`.runtime/psyche-deploy/deploy.log`；具体阶段日志路径在job-status中。
- 冒烟结果：`.runtime/psyche-deploy/smoke-*.json`。

`state=isolated_text_smoke_passed`才表示独立文本测试通过；下载目录或稀疏分片的逻辑大小不能证明下载完成。默认访谈模型与机器人配置保持原值，Psyche是单独的可选模型配置。

如后台作业异常退出，可在项目目录运行下面命令重试；`job.lock`会防止重复作业。只有上次成功生成且记录匹配的GGUF才会被复用，临时产物不会被当作部署成功。

```bash
cd /home/nvidia/Desktop/机器人头
.runtime/psyche-tools/bin/python scripts/deploy_psyche.py
```

国内镜像入口已能取到授权文件，但大文件重定向至官方CDN，线路速度仍有波动；已验证206 Range，并用本地ARM64 aria2分段续传，未向CDN发送HF账户Token。临时签名链接文件权限为0600，用完删除，日志过滤凭证及链接。

**已完成的本机工作**

- 原生ARM64 llama-server已存在；llama-quantize已编译，并验证能执行。量化程序位置记录在`.runtime/llama-quantize.path`。
- 隔离工具环境`.runtime/psyche-tools`已安装PyTorch 2.9.1 CPU、Transformers 4.57.6、Hugging Face Hub 0.36.2、NumPy、Safetensors及SentencePiece。工具依赖通过清华PyPI镜像安装，没有改动访谈应用的`.venv`。
- `pip check`、PyTorch BF16转FP16小张量检查、HF CLI帮助及GGUF转换器帮助命令均通过，随后已完成真实模型转换、量化及独立推理测试。
- 原有llama.cpp转换依赖文件推荐的PyTorch 2.11没有本机Python 3.10 ARM64对应轮子，本次使用可取得的2.9.1 CPU版；不能把帮助命令通过视为完整模型转换成功。

**国内下载入口与实测结果**

使用[HF-Mirror](https://hf-mirror.com/)。这是第三方公益镜像，其文档支持通过`HF_ENDPOINT`切换下载入口；gated模型仍需先在Hugging Face官网获得访问权。

本机镜像模型元数据请求返回200：仓库`MindIntLab/Psyche-R1`，固定版本`2c2dc49d0f5027931b152ab776a90c6bfcd0eecb`，gated状态为`auto`。17个文件的大小及哈希已记录到`.runtime/psyche-deploy/source-metadata.json`；4个权重分片合计15,231,271,912字节。

无凭证获取`config.json`返回403，响应明确要求申请模型访问。直接访问huggingface.co在本机超时或被重置。核查的魔搭同名仓库和社区GGUF候选未找到可用权重，目前采用“官网获批、国内镜像下载、在本机量化”的路径。

随后按镜像首页方法二实际执行了完整`hf download`：README.md的3186字节下载成功，受限文件返回403 `GatedRepoError`。完整性检查确认4个权重分片均未取得，未标记模型下载成功。更换为站内hfd方法仍需相同的账户授权。

本机可运行已准备的入口，缺少凭证时会在终端隐藏输入登录，随后继续下载固定版本并逐项校验大小及哈希：

```bash
cd /home/nvidia/Desktop/机器人头
bash scripts/download_psyche.sh
```

仅检查已下载内容可加`--verify-only`。文件不全时会列出缺失并返回非零状态，不会把README或空目录当作模型下载完成。

**输入Token后的失败原因核验**

授权完成前的带认证检查结果：身份接口`whoami-v2`返回200，Token为fine-grained且`canReadGatedRepos=true`；独立`auth-check`、固定版本配置文件及首个权重分片均返回403 `GatedRepo`，服务器说明当前账号不在模型授权名单内。换用不同配置请求参数并要求不使用缓存后结果相同。因此当时的阻碍是该账号对Psyche-R1的访问授权尚未生效，不能归因为Token未输入或缺少gated读取开关。

需要在能访问官网的设备上，使用创建该Token的同一账号查看模型页：若尚未申请则阅读并确认访问条件；若已申请则查看是否获批，或核对是否使用了另一个账号。本次只读接口不能区分未申请、待审或被拒。授权按个人账号授予，见[HF官方说明](https://huggingface.co/docs/hub/en/models-gated)。现有有效Token无需重复创建或扩大权限。

下载入口现已在正式下载前分别验证身份和模型访问权，对授权失败给出上述具体说明。诊断结果只保存状态与错误摘要，不保存Token；本机凭证文件权限已收紧为仅本人可读写。

**获批后的本机登录**

先在能够访问官网的设备上打开[官方模型页](https://huggingface.co/MindIntLab/Psyche-R1)，完成访问申请，并在[Token页面](https://huggingface.co/settings/tokens)创建只读凭证。Token只在本机终端的隐藏输入框输入，不放到聊天或命令参数里。

以下登录及后续下载会向第三方`hf-mirror.com`发送认证凭证，请使用范围尽量小的只读Token。镜像首页说明了这一认证方式。

```bash
cd /home/nvidia/Desktop/机器人头
HF_ENDPOINT=https://hf-mirror.com .runtime/psyche-tools/bin/hf auth login
```

本次没有代替用户申请、接受账户资料分享条件或发送Token。

**固定版本下载**

以下命令支持复用已经下载的文件；禁用Xet以使用镜像提供的普通HTTP下载路径。网络变量只作用于这次命令，不修改系统或其他项目的全局配置。

```bash
cd /home/nvidia/Desktop/机器人头
HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 HF_HUB_DOWNLOAD_TIMEOUT=120 \
  .runtime/psyche-tools/bin/hf download MindIntLab/Psyche-R1 \
  --revision 2c2dc49d0f5027931b152ab776a90c6bfcd0eecb \
  --max-workers 2 --local-dir models/Psyche-R1/source
```

下载完成后，核对文件大小和记录中的LFS SHA256，检查真实config及聊天模板。不要仅以下载目录存在判断完整性；镜像元数据可读也不等于权重已取得。

**已完成的转换命令参考**

```bash
cd /home/nvidia/Desktop/机器人头
OMP_NUM_THREADS=2 .runtime/psyche-tools/bin/python \
  .runtime/llama.cpp/convert_hf_to_gguf.py models/Psyche-R1/source \
  --outfile models/Psyche-R1/Psyche-R1-F16.gguf --outtype f16 --use-temp-file

/home/nvidia/Desktop/心理模型/.runtime/llama.cpp/build/bin/llama-quantize \
  --max-buffer-size 512 models/Psyche-R1/Psyche-R1-F16.gguf \
  models/Psyche-R1/Psyche-R1-Q4_K_M.gguf Q4_K_M 2
```

机器人头目录中的CMake缓存保留了原心理模型目录，因此本次在原生构建的实际目录完成量化目标编译。没有重置源码或重新构建访谈应用。

量化后先在127.0.0.1独立端口试加载，验证最终可见回答、思考标签处理、输出预算、峰值内存和首句延迟，再配置应用中的模型选项。Psyche-R1尚未加入应用默认模型，原问卷计分继续沿用固定规则。
