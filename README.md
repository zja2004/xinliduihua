# 心语 · 本地心理访谈工作台

心语在 Jetson Orin NX 上运行中文学生访谈。心理老师可以从名册发起访谈，学生用文字或语音完成基本资料、21 道自评题、固定朗读、三个开放问题及三项补充自评。老师能回看逐题回答、录音、已采集的视频和评分。两组分数用于复核学生的自评，正式测评需要相应的授权材料和专家验证。

## 从哪里开始

| 目录 | 用途 |
| --- | --- |
| `机器人头/` | 当前集成应用，包含老师与学生网页、SQLite、语音识别与播报、本地模型接口和机器人控制 |
| `心理模型/` | 早期独立的语音访谈实现；其中的 `scripts/clinic.sh` 会定位同级的集成应用 |
| `desktop/` | 五个桌面入口的原始模板；安装器会根据本次解压或克隆的位置填写路径 |

Python 需要 3.10 或更高版本。在 Ubuntu 桌面上打开终端，从仓库根目录运行以下命令。首次安装依赖需要联网。

```bash
cd 机器人头
bash scripts/bootstrap.sh
.venv/bin/python -m pip install -e '.[robot]'
bash scripts/web.sh --open-school
```

选择「文字与按钮」即可体验老师发起、学生作答和老师回看。此命令在前台运行，按 Ctrl+C 可停止。学校工作台位于 [http://127.0.0.1:18088/school](http://127.0.0.1:18088/school)，学生页面位于 [http://127.0.0.1:18088/](http://127.0.0.1:18088/)。

首次启动会在本机 `机器人头/sessions/学校工作台-首次登录.txt` 生成随机初始密码。用 `teacher` 登录可查看「访谈原始记录」，`school_admin` 用于学校管理。老师在名册选好学生并发起访谈后，学生直接使用当前浏览器回答。访谈详情、SQLite 数据库和登录文件保存在本机的 `sessions/`，请在学校授权范围内管理。

## 语音、模型与硬件

下列命令在 `机器人头/` 执行。语音模式需要能用 `pactl` 访问的 PulseAudio 桌面服务、带 Pulse 支持的 PortAudio、麦克风和扬声器。摄像头和机器人串口用于视频与跟踪功能。

```bash
.venv/bin/python -m orin_voice.assets asr tts
.venv/bin/python scripts/download_yolo_head.py
```

搭配 Psyche-R1 时，在已装 JetPack/CUDA、Git、CMake 及 C++ 编译器的 Jetson 上运行 `bash scripts/build_jetson.sh` 构建 llama.cpp。将经授权取得、与配置匹配的量化模型放到 `机器人头/models/Psyche-R1/Psyche-R1-Q4_K_M.gguf`。发布包不携带模型权重。原始权重版本、转换命令和部署机上的校验记录见 [Psyche-R1 本机部署说明](机器人头/docs/Psyche-R1-本机部署.md)。

若要从原始权重转换，可用已获 [MindIntLab/Psyche-R1](https://huggingface.co/MindIntLab/Psyche-R1) 访问许可的 Hugging Face 账号，从模型官方源下载项目使用的固定版本。在 `机器人头/` 执行以下命令并在本机交互登录。

```bash
python3 -m venv .runtime/psyche-tools
.runtime/psyche-tools/bin/python -m pip install 'huggingface_hub[cli]==0.36.2'
.runtime/psyche-tools/bin/hf auth login
.runtime/psyche-tools/bin/hf download MindIntLab/Psyche-R1 \
    --revision 2c2dc49d0f5027931b152ab776a90c6bfcd0eecb \
    --local-dir models/Psyche-R1/source
```

随后按部署说明准备转换器及其 Python 依赖，在当前 Jetson 的 `.runtime/llama.cpp/build` 编译 `llama-quantize` 目标，再进行 F16 转换和 Q4_K_M 量化。另一条镜像下载脚本 `scripts/download_psyche.sh` 还读取部署机的 `.runtime/psyche-deploy/source-metadata.json`。准备好模型与音频输入后，先停止之前的文字演示服务，再运行 `bash scripts/school.sh` 打开带语音和模型的工作台。

若需要机头驱动，先核对已获授权的 ROS 2 Humble 配套包与硬件。公开版仅随附自研集成代码。实物驱动所需的厂商参考源码由授权方另行提供：将 `robot_interfaces`、`robot_msgs`、`device_io` 放在仓库根目录 `robot-talk-ros2/`（或设置 `ORIN_HEAD_ROS_PACKAGES`），并按硬件说明放置嘴型和云台参考源码。ROS 配置与串口接入见 [机器人头硬件接入](机器人头/docs/机器人头硬件接入.md)。单独的 HeadTool 使用 PySide6 与 pyserial。

## 安装桌面图标与运行验证

在仓库根目录运行 `python3 tools/install-desktop.py` 安装桌面图标。安装器跳过同名文件，操作结束后可按需要双击「心语访谈」。要试装到其他目录，可加 `--desktop 目录`。

可在 `机器人头/` 运行软件回归。

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
```

更多使用说明见 [学校工作台交付与验收](机器人头/docs/学校工作台交付与验收.md) 和 [集成应用说明](机器人头/README.md)。
