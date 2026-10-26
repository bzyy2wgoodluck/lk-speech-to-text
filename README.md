# lk-speech-to-text

> 项目名里的 `lk` 是作者账号缩写，功能本身与个人无关。

把本机的音视频转成带毫秒级时间戳的中文逐字稿。全程用本机 faster-whisper 推理，音频不出本机，不调用任何云端语音识别 API。

同时它是一个可被 Agent 直接加载的 Skill（`SKILL.md`），也可以当普通命令行脚本用。

---

## 它和「上传到某个网站转写」有什么不同

| | 云端转写服务 | 本项目 |
|---|---|---|
| 音频去向 | 上传到对方服务器 | 只在本机 ffmpeg + 模型之间流转 |
| 费用 | 按分钟计费 | 一次性下载模型，之后零成本 |
| 断网 | 不可用 | 模型就绪后可完全离线 |
| 时间戳 | 通常只到整秒 | 词级对齐，约 0.02 秒精度 |
| 专有名词 | 只能接受它的结果 | 可以用自己的术语表确定性纠正 |

## 三个需要提前知道的事实

这个项目**不承诺识别准确**，它解决的是另一类问题：

1. **词级时间戳**。默认开 `word_timestamps`，边界不再被量化到整秒。
2. **术语表纠错**。品牌名、工具名这类生造词，语音识别几乎必然出错，而且同一个词每次跑结果可能不同。项目提供一张 `错误写法 => 正确写法` 的表做确定性替换。
3. **产物是 Markdown**，带源文件 SHA-256、模型名、音频时长、片段数，可追溯。

不想让你误以为它能把 ASR 变成 100% 准确——做不到。它的价值是**结果稳定、可复现、可纠正**，而不是替你判断每个字。

## 环境要求

- Python 3.9+
- 约 1.5 GB 磁盘（`medium` 模型），CPU 即可，不需要显卡

## 安装

```sh
git clone https://github.com/bzyy2wgoodluck/lk-speech-to-text.git
cd lk-speech-to-text

python -m venv .venv
# macOS / Linux
.venv/bin/python -m pip install -r requirements.txt
# Windows PowerShell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

首次使用需要下载模型（联网一次）：

```sh
# macOS / Linux
.venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('medium', device='cpu', compute_type='int8')"
# Windows PowerShell
.venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; WhisperModel('medium', device='cpu', compute_type='int8')"
```

模型默认落在 Hugging Face 缓存目录。想放到别处，设置 `HF_HOME` 指向目标目录。

## 仓库结构

```
lk-speech-to-text/
├── SKILL.md                     Agent 加载的工作流、断句规则与交付检查
├── scripts/
│   ├── transcribe.py            转写主程序：抽音、识别、术语纠正、写稿
│   ├── readable.py              把原稿合并成可读稿，分段阈值可调
│   ├── subtitles.py             字幕 OCR 对照（可选依赖）
│   ├── storage.py               命名、时间戳格式化、安全检查、原子写盘
│   └── text.py                  文本拼接与术语表加载/替换
└── references/
    ├── glossary.example.txt     术语表示例（真实词表不进仓库）
    └── retro.md                 反馈与持续改进流程
```

## 用法

### 命令行

下面示例按 macOS / Linux 写，Windows 把 `.venv/bin/python` 换成 `.venv\Scripts\python.exe` 即可。

```sh
# 1. 转写（生成带时间戳的原稿）
.venv/bin/python scripts/transcribe.py "video.mp4" --output-dir results --language zh

# 2. 合并成可读稿（可选）
.venv/bin/python scripts/readable.py "results/xxx_transcript_2026-01-01_1200.md"
```

第一步会输出一行 JSON，其中 `output` 字段是本次实际生成的文件路径——把它传给第二步，不要手写路径。默认不覆盖已有文件。

常用参数：

| 参数 | 说明 |
|---|---|
| `--output-dir` | 结果目录，默认与输入文件同目录（也可用环境变量 `TRANSCRIPT_OUT_DIR`） |
| `--output` | 直接指定结果文件路径，优先级高于 `--output-dir` |
| `--overwrite` | 明确允许替换已有结果；不加时遇到同名文件直接报错退出 |
| `--model` | 默认 `medium`。`small` 更快但准确率明显下降；`large-v3` 更准，体积约为 `medium` 的 2 倍，本机未实测耗时 |
| `--language` | 默认 `zh`，可给 `en`、`auto` |
| `--threads` | CPU 线程数，默认 8 |
| `--offline` | 只用已下载好的模型，缺失即失败，不自动联网 |
| `--no-word-timestamps` | 关闭词级对齐（不推荐，边界会掉到整秒） |
| `--glossary` | 指定术语表路径 |
| `--no-glossary` | 本次不做术语纠正 |
| `--ffmpeg` | 指定已有的 ffmpeg 可执行文件，默认用 imageio-ffmpeg 自带的那份 |

`readable.py` 的参数：`input`、`--output`、`--overwrite`，外加四个分段阈值，默认值与原行为一致：

| 参数 | 说明 |
|---|---|
| `--max-span` | 单段最长秒数，默认 18 |
| `--max-chars` | 单段最多字符数，默认 90 |
| `--pause` | 停顿超过该秒数即断开，默认 3 |
| `--sentence-min` | 已到句末且满该秒数即断开，默认 6；调小 `--max-span` 时自动跟随 |

嫌段落太长就调 `--max-span`，实测 102 秒口播：默认 18 秒得到 8 段，改 8 秒得到 12 段。实际生效的阈值会写在稿件的分段说明行里，便于复现。

### 作为 Agent Skill

把整个目录放到 Agent 的 skills 目录下即可，具体路径取决于你的宿主：

- Claude Code / WorkBuddy：`~/.workbuddy/skills/lk-speech-to-text/`
- 其他支持 `SKILL.md` 的 Agent：放进它约定的 skill 目录

`SKILL.md` 里写明了完整工作流、断句标点规则和交付前检查项。Agent 会自己读。

## 术语表

语音识别对**品牌名、工具名、生造词**几乎必然出错，这是声学模型的固有短板，调参解决不了。

项目为此提供一张纯文本表，一行一条：

```
错误写法 => 正确写法
```

- 默认读 `references/glossary.txt`，**这个文件不随仓库分发**（它通常含个人内容）。
- 只做确定性字符串替换：命中才改，没命中逐字不动，不碰时间戳、不碰断句。
- **左侧必须带上下文**。写成 `爆款股价 => 爆款骨架`，而不是 `股价 => 骨架`，否则讲到真实股价时会改错。
- 左侧长的规则自动优先，不用手工排序。

先复制示例表再改：

```sh
# macOS / Linux
cp references/glossary.example.txt references/glossary.txt
# Windows PowerShell
Copy-Item references/glossary.example.txt references/glossary.txt
```

### 为什么不用 hotwords 或 initial_prompt

这是实测结论，不是凭感觉定的。

faster-whisper 支持 `hotwords` 和 `initial_prompt`，把术语塞进去确实能让专有名词一次听对。但实测（102 秒中文口播）发现代价更高：

- `hotwords`：最后 8 秒的 6 句话被压成一句，还多出一个幻觉词，84 段变 76 段
- `initial_prompt`：同样丢内容，并且时间戳被破坏成整秒

**丢内容比错别字严重得多**，所以本项目不提供这两个开关，同音字一律走术语表。

## 字幕 OCR 对照（可选）

视频自带硬字幕时，可以把画面上的字读出来，与 ASR 原稿逐行对照，专门用来抓专有名词的错字。

```sh
# 只提取字幕时间轴
.venv/bin/python scripts/subtitles.py "video.mp4"

# 与 ASR 原稿对照
.venv/bin/python scripts/subtitles.py "video.mp4" --transcript "results/xxx_transcript_....md"
```

需要额外依赖 RapidOCR：

```sh
.venv/bin/python -m pip install rapidocr-onnxruntime
```

没装时脚本给出安装提示并退出，不影响转写与合并两条主链路。

报告分四块：写法不同的行、对齐存疑的行、只在字幕出现的行、完整字幕时间轴。对照采用全局单调对齐（字幕行与 ASR 行的对应只能向前走，不允许交叉），比逐行找时间重叠稳得多——实测同一份素材，逐行匹配有 26 行被判成错配，单调对齐降到 0 行。

实测 102 秒竖屏口播、2.5 秒抽一帧：39 条字幕行中 18 条与 ASR 完全一致，20 条只是措辞长短不同，真正的错字是那一条——ASR 输出「帧级检测」、OCR 输出「顿级检测」，看画面才确认原文是前者。

几点必读：

- **OCR 不是权威**。同一份视频里 OCR 把「拆开」认成「折开」、「趋势」认成「超势」、「包括」漏掉「括」。它提供的是另一组证据，不替代人工核对。
- 区域默认取画面高度 45%~62%，按竖屏口播设的。跑之前先加 `--dump` 看「每帧原始 OCR 文本」，如果混进进度条、评论区或整屏文案，就用 `--region` 调窄。
- 固定界面词用 `--ignore 全屏观看` 逐个剔除，可重复传。
- 字幕普遍滞后口播 1~3 秒，`--tolerance` 默认 2.5 秒；对不上的比例偏高时再调大。

## 输出长什么样

```markdown
# 视频 / 音频逐字稿

- **源文件**：`video.mp4`
- **源快照 SHA-256**：`a383b302...`
- **源文件字节数**：10879776
- **识别语言**：zh
- **识别模型**：faster-whisper `medium`
- **时间戳精度**：词级对齐
- **术语纠正**：2 处：Midjourny→Midjourney、Github→GitHub
- **音频时长**：00:01:42.571
- **原始识别片段数**：84
- **生成时间**：2026-10-23T20:22:09+08:00

---

**[00:00:02.320 - 00:00:03.220]**  每条都不一样

**[00:00:03.220 - 00:00:04.280]**  不是简单的复制粘贴

---

## 纯文本（无时间戳）

每条都不一样不是简单的复制粘贴……
```

## 已知限制

- **标点不由脚本生成**。Whisper 处理中文口播时常不输出标点，`SKILL.md` 里定义了由 Agent 判断的补标点规则；纯命令行使用时需要你自己处理。
- **字幕 OCR 需要按视频调区域**。默认 45%~62% 只对竖屏口播成立，换版式必须先 `--dump` 看一遍再调 `--region`，不是开箱即用。
- **单独出现的品牌名仍需人工核对**。术语表只治理已知词，新出现的专有名词第一次还是会错；OCR 对照也只是提供第二组证据。
- **不支持说话人分离**。
- **一次只处理一个文件**，没有批量参数。多文件请自己循环调用。
- 长音频在 CPU 上较慢。实测 8 线程 `medium`：102 秒音频耗时 92 秒（含抽音、加载模型、写盘），大致与音频时长相当。`large-v3` 更慢，本机未实测。

## 许可

[MIT](LICENSE)
