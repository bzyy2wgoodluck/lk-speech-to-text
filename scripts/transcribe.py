"""Transcribe a local media snapshot; no cross-run audio cache is reused."""

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import wave

from storage import check_output, new_output, timestamp, write_text
from text import apply_glossary, join_text, load_glossary

DEFAULT_GLOSSARY = Path(__file__).resolve().parent.parent / "references" / "glossary.txt"


def source_snapshot(source, destination):
    """Copy and identify exactly the bytes handed to ffmpeg, not just a basename."""
    digest = hashlib.sha256()
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        before = os.fstat(incoming.fileno())
        while chunk := incoming.read(1024 * 1024):
            digest.update(chunk)
            outgoing.write(chunk)
        after = os.fstat(incoming.fileno())
    current = source.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if os.name == "nt":
        # Windows 上 os.fstat().st_ctime 是 MFT 变更时间，Path.stat().st_ctime 是创建时间，
        # 两者语义不同必然不等，会把稳定文件误判为“读取时发生变化”，故 Windows 不比较 ctime。
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, key) != getattr(after, key) or
           getattr(before, key) != getattr(current, key) for key in fields):
        raise RuntimeError("源文件在读取时发生变化，已停止；请等文件保存完成后重试。")
    return {"name": source.name, "bytes": before.st_size, "sha256": digest.hexdigest()}


def extract_audio(source, destination, ffmpeg=None):
    if ffmpeg is None:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
         "-acodec", "pcm_s16le", "-f", "wav", str(destination)],
        check=True, capture_output=True, text=True,
    )
    with wave.open(str(destination), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getframerate() != 16000 or audio.getnframes() == 0:
            raise RuntimeError("未得到有效的 16 kHz 单声道音频。")
        return audio.getnframes() / audio.getframerate()


def transcribe(args):
    source = Path(args.input).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("输入必须是本地媒体文件。")
    output_dir = args.output_dir or os.environ.get("TRANSCRIPT_OUT_DIR") or source.parent
    output = Path(args.output).expanduser() if args.output else new_output(output_dir, source.stem, "transcript")
    output = check_output(output, source, args.overwrite)
    if args.threads < 1:
        raise ValueError("--threads 必须大于 0。")
    if args.offline:
        # huggingface_hub reads environment variables at import time.
        os.environ["HF_HUB_OFFLINE"] = "1"
    from faster_whisper import WhisperModel

    rules = []
    if not args.no_glossary:
        glossary = (Path(args.glossary).expanduser() if args.glossary else DEFAULT_GLOSSARY)
        if glossary.exists():
            rules = load_glossary(glossary)
        elif args.glossary:
            raise ValueError(f"术语表不存在：{glossary}")

    with tempfile.TemporaryDirectory(prefix="lk-speech-to-text-") as temporary:
        work = Path(temporary)
        snapshot = work / ("source" + source.suffix)
        identity = source_snapshot(source, snapshot)
        wav = work / "audio.wav"
        print("Extracting current input snapshot...", flush=True)
        duration = extract_audio(snapshot, wav, args.ffmpeg)
        print(f"Loading {args.model}; offline={args.offline}", flush=True)
        model = WhisperModel(args.model, device="cpu", compute_type="int8",
                             cpu_threads=args.threads, local_files_only=args.offline)
        segments, info = model.transcribe(
            str(wav), language=None if args.language == "auto" else args.language,
            beam_size=5, vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
            word_timestamps=args.word_timestamps,
        )
        rows = []
        corrections = []
        for segment in segments:
            start, end = float(segment.start), float(segment.end)
            if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start
                    or (rows and start < rows[-1]["start"])):
                raise RuntimeError("识别返回无效时间戳，未写入结果。")
            text = " ".join(segment.text.splitlines()).strip()
            if text:
                if rules:
                    text, hits = apply_glossary(text, rules)
                    corrections += hits
                rows.append({"start": start, "end": end, "text": text})
            if len(rows) and len(rows) % 20 == 0:
                print(f"progress: {len(rows)} segments, {timestamp(end)}", flush=True)
        if not rows:
            raise RuntimeError("没有识别出文字片段；未生成空稿。请检查音轨、语言或模型。")

    language = getattr(info, "language", args.language)
    if corrections:
        note = f"{len(corrections)} 处：" + "、".join(corrections)
    elif rules:
        note = "未命中术语表，识别文字保持原样"
    else:
        note = "未启用术语表（--no-glossary）"
    lines = ["# 视频 / 音频逐字稿", "", f"- **源文件**：`{identity['name']}`",
             f"- **源快照 SHA-256**：`{identity['sha256']}`",
             f"- **源文件字节数**：{identity['bytes']}",
             f"- **识别语言**：{language}", f"- **识别模型**：faster-whisper `{args.model}`",
             f"- **时间戳精度**：{'词级对齐' if args.word_timestamps else '段级（边界会被量化到整秒）'}",
             f"- **术语纠正**：{note}",
             f"- **音频时长**：{timestamp(duration)}", f"- **原始识别片段数**：{len(rows)}",
             f"- **生成时间**：{datetime.now().astimezone().isoformat(timespec='seconds')}",
             "", "---", ""]
    plain = ""
    for row in rows:
        lines += [f"**[{timestamp(row['start'])} - {timestamp(row['end'])}]**  {row['text']}", ""]
        plain = join_text(plain, row["text"])
    lines += ["---", "", "## 纯文本（无时间戳）", "", plain, ""]
    write_text(output, "\n".join(lines), args.overwrite)
    result = {"output": str(output.resolve()), "segments": len(rows),
              "audio_duration_seconds": duration, "source": identity}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("input", help="本地音视频文件")
    value.add_argument("--output", help="指定 Markdown 结果路径，默认不覆盖")
    value.add_argument("--output-dir", help="结果目录，默认与输入文件同目录（也可用环境变量 TRANSCRIPT_OUT_DIR）")
    value.add_argument("--overwrite", action="store_true", help="明确替换已存在的结果；不能替换源文件")
    value.add_argument("--model", default=os.environ.get("WHISPER_MODEL", "medium"), help="模型名或本地模型目录")
    value.add_argument("--language", default=os.environ.get("WHISPER_LANG", "zh"), help="zh / en / auto 等")
    value.add_argument("--threads", type=int, default=int(os.environ.get("WHISPER_THREADS", "8")))
    value.add_argument("--offline", action="store_true", help="仅使用本地完整模型缓存，缺失即失败")
    value.add_argument("--ffmpeg", help="可选：已有 ffmpeg 可执行文件，默认用 imageio-ffmpeg")
    value.add_argument("--word-timestamps", action=argparse.BooleanOptionalAction, default=True,
                       help="词级对齐，时间戳精度可达 0.02 秒；用 --no-word-timestamps 关闭，"
                            "关闭后边界会被量化到整秒")
    value.add_argument("--glossary", help="术语与纠错表路径，默认用包内 references/glossary.txt")
    value.add_argument("--no-glossary", action="store_true", help="本次不做术语纠正")
    return value


if __name__ == "__main__":
    try:
        transcribe(parser().parse_args())
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.CalledProcessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        print(f"ERROR: {detail}", file=sys.stderr)
        sys.exit(1)
