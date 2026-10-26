"""Read burned-in subtitles with OCR and diff them against a transcript.

字幕是作者手工输入的，可能比 ASR 准，也可能拼错。本脚本不判断谁对，只把
「同一时间点上两种写法不同」的地方列出来，交给人工看画面确认。
"""

import argparse
from difflib import SequenceMatcher
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile

from storage import check_output, new_output, timestamp, write_text

ENTRY = re.compile(r"^\*\*\s*\[(\d{2,}:\d{2}:\d{2}(?:\.\d{1,3})?)\s*(?:-->|-)\s*"
                   r"(\d{2,}:\d{2}:\d{2}(?:\.\d{1,3})?)\]\*\*\s*(.*)$")


def seconds(value):
    hours, minutes, secs = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(secs)


def probe_size(ffmpeg, source):
    """Read the frame size out of ffmpeg's banner; avoid depending on ffprobe being present."""
    done = subprocess.run([str(ffmpeg), "-hide_banner", "-i", str(source)],
                          capture_output=True, text=True)
    matched = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", done.stderr, re.S)
    if not matched:
        raise RuntimeError("无法从 ffmpeg 输出中解析画面尺寸。")
    return int(matched[1]), int(matched[2])


def grab_frames(ffmpeg, source, folder, interval, region):
    """Sample one frame every `interval` seconds, cropped to the subtitle band."""
    width, height = probe_size(ffmpeg, source)
    top = max(0, min(height - 2, round(height * region[0])))
    bottom = max(top + 2, min(height, round(height * region[1])))
    subprocess.run(
        [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(source),
         "-vf", f"fps=1/{interval},crop={width}:{bottom - top}:0:{top}",
         "-start_number", "1", str(folder / "f_%05d.png")],
        check=True, capture_output=True, text=True,
    )
    frames = sorted(folder.glob("f_*.png"))
    if not frames:
        raise RuntimeError("没有抽到任何帧；请检查输入文件与抽帧间隔。")
    return [{"index": int(path.stem.split("_")[1]), "path": path,
             "time": (int(path.stem.split("_")[1]) - 1) * interval} for path in frames]


def load_engine():
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as error:
        raise RuntimeError(
            "缺少 OCR 依赖。安装方式：pip install rapidocr-onnxruntime\n"
            "若已装在单独目录，把该目录加进 PYTHONPATH 再运行。") from error
    return RapidOCR()


def read_frame(engine, frame, min_score, min_len, max_len, ignore=()):
    """Return the frame's subtitle text, top-to-bottom.

    A frame whose text is far longer than one subtitle line means the crop caught the
    whole screen (progress bar, comments, recommended feeds). Dropping it is safer than
    feeding noise into the comparison.
    """
    outcome = engine(str(frame["path"]))
    rows = outcome[0] if isinstance(outcome, tuple) else outcome
    kept = []
    for item in rows or []:
        box, text, score = item[0], item[1], float(item[2])
        text = str(text).strip()
        if score < min_score or len(text) < min_len:
            continue
        kept.append((box[0][1], box[0][0], text))
    kept.sort()
    merged = "".join(text for _, _, text in kept).strip()
    for word in ignore:
        merged = merged.replace(word, "")
    merged = merged.strip()
    frame["raw"] = merged
    return "" if len(merged) > max_len else merged


def merge_frames(frames, interval, ratio):
    """Collapse per-frame text into subtitle lines.

    Only consecutive sampled frames may join: a repeated line after a blank frame is a
    new occurrence, not a continuation.
    """
    lines = []
    for frame in frames:
        text = frame["text"]
        if not text:
            continue
        if lines:
            last = lines[-1]
            adjacent = frame["index"] - last["last_index"] <= 1
            similar = SequenceMatcher(None, last["text"], text).ratio() >= ratio
            if adjacent and similar:
                # 同一句字幕在相邻帧里 OCR 结果会有抖动，取更长的那个。
                if len(text) > len(last["text"]):
                    last["text"] = text
                last["end"] = frame["time"] + interval
                last["last_index"] = frame["index"]
                continue
        lines.append({"start": frame["time"], "end": frame["time"] + interval,
                      "text": text, "last_index": frame["index"]})
    for line in lines:
        line.pop("last_index", None)
    return lines


def read_transcript(path):
    rows = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        matched = ENTRY.fullmatch(raw.strip())
        if matched:
            rows.append({"start": seconds(matched[1]), "end": seconds(matched[2]),
                         "text": matched[3].strip()})
        elif raw.strip().startswith("## 纯文本"):
            break
    return rows


def pair_score(subtitle, row, tolerance):
    """Text similarity, discounted by how far apart the two timestamps are."""
    ratio = SequenceMatcher(None, row["text"], subtitle["text"]).ratio()
    gap = abs((subtitle["start"] + subtitle["end"]) / 2 - (row["start"] + row["end"]) / 2)
    return ratio - 0.2 * min(1.0, gap / tolerance)


def compare(subtitles, asr, tolerance):
    """Align subtitle lines to ASR rows monotonically, then grade each pair.

    逐行取「重叠最大」会让相邻句互相抢，把本来对得上的行拆散。这里做全局单调对齐：
    字幕行与 ASR 行的对应关系只能向前走，不允许交叉。
    """
    n, m = len(subtitles), len(asr)
    table = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            candidate = pair_score(subtitles[i - 1], asr[j - 1], tolerance)
            best = max(table[i - 1][j], table[i][j - 1])
            if candidate > 0:
                best = max(best, table[i - 1][j - 1] + candidate)
            table[i][j] = best

    pairs, i, j = [], n, m
    while i > 0 and j > 0:
        candidate = pair_score(subtitles[i - 1], asr[j - 1], tolerance)
        if candidate > 0 and abs(table[i][j] - (table[i - 1][j - 1] + candidate)) < 1e-9:
            pairs.append((subtitles[i - 1], asr[j - 1], SequenceMatcher(
                None, asr[j - 1]["text"], subtitles[i - 1]["text"]).ratio()))
            i, j = i - 1, j - 1
        elif table[i - 1][j] >= table[i][j - 1]:
            i -= 1
        else:
            j -= 1
    pairs.reverse()

    paired = {id(line) for line, _, _ in pairs}
    unmatched = [line for line in subtitles if id(line) not in paired]
    differing, doubtful, matching = [], [], 0
    for line, row, ratio in pairs:
        if ratio >= 0.999:
            matching += 1
        elif ratio >= 0.3:
            differing.append({"line": line, "asr": row, "ratio": ratio})
        else:
            # 时间上对得上、文字却几乎不重合：多是字幕与口播各说各的。
            doubtful.append({"line": line, "asr": row, "ratio": ratio})
    return unmatched, differing, doubtful, matching


def append_dump(lines, frames, enabled):
    """Optional per-frame dump: the fastest way to see whether --region is aimed right."""
    if not enabled:
        return
    lines += ["", "## 每帧原始 OCR 文本", "",
              "调 `--region` 时看这一节：区域没框准时，这里会出现进度条、评论或整屏文案。", ""]
    for frame in frames:
        lines.append(f"- `{timestamp(frame['time'])}` {frame.get('raw') or '（无文字）'}")
    lines.append("")


def build(args):
    source = Path(args.input).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("输入必须是本地媒体文件。")
    if not 0 <= args.region[0] < args.region[1] <= 1:
        raise ValueError("--region 需形如 0.45:0.65，两个比例都在 0~1 之间且前者小于后者。")
    if args.interval <= 0:
        raise ValueError("--interval 必须大于 0。")
    output = Path(args.output).expanduser() if args.output else new_output(
        args.output_dir or source.parent, source.stem, "subs")
    output = check_output(output, source, args.overwrite)
    asr = read_transcript(args.transcript) if args.transcript else []

    import imageio_ffmpeg
    ffmpeg = args.ffmpeg or imageio_ffmpeg.get_ffmpeg_exe()
    engine = load_engine()

    print(f"Sampling one frame every {args.interval:g}s ...", flush=True)
    with tempfile.TemporaryDirectory(prefix="lk-subs-") as temporary:
        frames = grab_frames(ffmpeg, source, Path(temporary), args.interval, args.region)
        for number, frame in enumerate(frames, 1):
            frame["text"] = read_frame(engine, frame, args.min_score, args.min_len,
                                       args.max_len, args.ignore)
            if number % 20 == 0:
                print(f"progress: {number}/{len(frames)} frames", flush=True)
    subtitles = merge_frames(frames, args.interval, args.merge_ratio)

    lines = ["# 字幕 OCR 对照报告", "",
             f"- **源文件**：`{source.name}`",
             f"- **抽帧间隔**：{args.interval:g} 秒",
             f"- **字幕区域**：画面高度 {args.region[0]:.0%} ~ {args.region[1]:.0%}",
             f"- **抽帧数**：{len(frames)}",
             f"- **合并后字幕行数**：{len(subtitles)}"]
    if not subtitles:
        lines.append("- **结论**：这一区域没有识别到文字。若视频确有字幕，"
                     "用 `--region` 调整纵向范围后重跑。")
        append_dump(lines, frames, args.dump)
        write_text(output, "\n".join(lines) + "\n", args.overwrite)
        result = {"output": str(output.resolve()), "frames": len(frames), "subtitles": 0}
        print(json.dumps(result, ensure_ascii=False))
        return result

    if asr:
        unmatched, differing, doubtful, matching = compare(subtitles, asr, args.tolerance)
        lines += [f"- **与 ASR 对照**：完全一致 {matching} 行，写法不同 {len(differing)} 行，"
                  f"对齐存疑 {len(doubtful)} 行，仅在字幕出现 {len(unmatched)} 行"]
        lines += ["", "## 一、写法不同（需看画面确认哪边对）", "",
                  "| 时间 | ASR 识别 | 画面对照 | 相似度 |", "|---|---|---|---|"]
        if differing:
            for item in sorted(differing, key=lambda entry: entry["line"]["start"]):
                lines.append(f"| {timestamp(item['line']['start'])} | {item['asr']['text']} | "
                             f"{item['line']['text']} | {item['ratio']:.2f} |")
        else:
            lines.append("| — | 无 | 无 | — |")
        lines += ["", "## 二、对齐存疑（时间重叠但文字几乎不同）", "",
                  "多半是字幕与口播各说各的，或抽帧间隔太粗。逐条看画面，不要当成错字依据。", ""]
        if doubtful:
            for item in sorted(doubtful, key=lambda entry: entry["line"]["start"]):
                lines.append(f"- **[{timestamp(item['line']['start'])}]** ASR「{item['asr']['text']}」"
                             f" / 画面「{item['line']['text']}」")
        else:
            lines.append("- 无")
        lines += ["", "## 三、只在字幕里出现", ""]
        if unmatched:
            for line in unmatched:
                lines.append(f"- **[{timestamp(line['start'])} - {timestamp(line['end'])}]**  {line['text']}")
        else:
            lines.append("- 无")
    else:
        lines.append("- **与 ASR 对照**：未提供原稿，跳过")

    lines += ["", "## 附：字幕时间轴（原始 OCR 结果）", ""]
    for line in subtitles:
        lines.append(f"**[{timestamp(line['start'])} - {timestamp(line['end'])}]**  {line['text']}")
        lines.append("")
    append_dump(lines, frames, args.dump)
    write_text(output, "\n".join(lines) + "\n", args.overwrite)
    result = {"output": str(output.resolve()), "frames": len(frames),
              "subtitles": len(subtitles), "asr_rows": len(asr)}
    print(json.dumps(result, ensure_ascii=False))
    return result


def region_value(text):
    parts = text.replace(",", ":").split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("请写成 0.55:1.0 这样的两个比例。")
    return float(parts[0]), float(parts[1])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="本地音视频文件")
    parser.add_argument("--transcript", help="可选的 ASR 原稿 Markdown，用于逐行对照")
    parser.add_argument("--output", help="报告路径，默认与输入同目录")
    parser.add_argument("--output-dir", help="报告目录")
    parser.add_argument("--overwrite", action="store_true", help="明确替换已存在的报告")
    parser.add_argument("--interval", type=float, default=2.0, help="抽帧间隔秒数，默认 2")
    parser.add_argument("--region", type=region_value, default=(0.45, 0.62),
                        help="字幕纵向范围占画面高度的比例，默认 0.45:0.62。"
                             "竖屏视频字幕常在画面中部偏下，取到 1.0 会把进度条和评论区一起读进来")
    parser.add_argument("--ignore", action="append", default=[],
                        help="从每帧文本中剔除的固定界面词，可重复。先用 --dump 看清有哪些再填")
    parser.add_argument("--min-score", type=float, default=0.6, help="OCR 置信度下限，默认 0.6")
    parser.add_argument("--min-len", type=int, default=2, help="忽略短于该长度的碎片，默认 2")
    parser.add_argument("--max-len", type=int, default=40,
                        help="单帧文本超过该长度即视为截到了整屏，丢弃；默认 40")
    parser.add_argument("--merge-ratio", type=float, default=0.9,
                        help="相邻帧文本相似度达到该值才认为还是同一句字幕；默认 0.9")
    parser.add_argument("--tolerance", type=float, default=2.5,
                        help="字幕与口播的时间偏移容差秒数，默认 2.5；字幕普遍滞后时可调大")
    parser.add_argument("--dump", action="store_true", help="在报告末尾附上每帧的原始 OCR 文本，便于调区域")
    parser.add_argument("--ffmpeg", help="已有 ffmpeg 可执行文件，默认用 imageio-ffmpeg")
    try:
        build(parser.parse_args())
    except (OSError, ValueError, RuntimeError, ImportError,
            subprocess.CalledProcessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        print(f"ERROR: {detail}", file=sys.stderr)
        sys.exit(1)
