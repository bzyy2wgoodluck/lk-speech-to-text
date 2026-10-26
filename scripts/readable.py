"""Build this run's readable Markdown from a timestamped transcript; never replaces the raw one."""

import argparse
import json
from pathlib import Path
import re
import sys

from storage import check_output, new_output, timestamp, write_text
from text import join_text


TIME = r"\d{2,}:\d{2}:\d{2}(?:\.\d{1,3})?"
ENTRY = re.compile(r"^\*\*\s*\[(" + TIME + r")\s*(?:-->|-)\s*(" + TIME + r")\]\*\*\s*(.*)$")


def seconds(value):
    hours, minutes, secs = value.split(":")
    if int(minutes) >= 60 or float(secs) >= 60:
        raise ValueError(f"无效时间戳：{value}")
    return int(hours) * 3600 + int(minutes) * 60 + float(secs)


def parse_transcript(raw):
    rows, first_line = [], None
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        line = line.strip()
        if line.startswith("## 纯文本"):
            break
        matched = ENTRY.fullmatch(line)
        if matched:
            start, end = seconds(matched[1]), seconds(matched[2])
            if end < start or (rows and start < rows[-1]["start"]):
                raise ValueError("时间戳倒序或结束早于开始，停止合并。")
            if not matched[3].strip():
                raise ValueError("时间戳片段没有文字，停止合并。")
            rows.append({"start": start, "end": end, "text": matched[3].strip()})
            if first_line is None:
                first_line = index
        elif line.startswith("**[") or line.startswith("** ["):
            raise ValueError(f"第 {index + 1} 行时间戳无法解析，停止合并。")
        elif rows and line and line != "---":
            raise ValueError(f"第 {index + 1} 行含未识别正文，停止合并以免漏字。")
    if not rows:
        raise ValueError("解析到 0 个时间戳片段；未生成结果、未修改输入。")
    header = "\n".join(lines[:first_line]).split("\n---", 1)[0].rstrip()
    return header, rows


def group_rows(rows, limits):
    """Merge adjacent rows while every limit still holds for the running group."""
    groups, current = [], None
    for row in rows:
        if current is None:
            current = dict(row)
            continue
        duration = current["end"] - current["start"]
        terminal = current["text"].endswith(tuple("。！？!?…"))
        if (duration >= limits["max_span"]
                or (terminal and duration >= limits["sentence_min"])
                or len(current["text"]) >= limits["max_chars"]
                or row["start"] - current["end"] > limits["pause"]):
            groups.append(current)
            current = dict(row)
        else:
            current["end"] = max(current["end"], row["end"])
            current["text"] = join_text(current["text"], row["text"])
    groups.append(current)
    return groups


def limits_from(args):
    """Validate thresholds before touching any file, so a bad value fails fast."""
    limits = {"max_span": args.max_span, "max_chars": args.max_chars,
              "pause": args.pause, "sentence_min": args.sentence_min}
    if limits["sentence_min"] is None:
        # 用户只调小 --max-span 时，句子阈值跟着降，否则那条规则永远不会命中。
        limits["sentence_min"] = min(6.0, limits["max_span"])
    for name in ("max_span", "max_chars"):
        if limits[name] <= 0:
            raise ValueError(f"--{name.replace('_', '-')} 必须大于 0。")
    for name in ("pause", "sentence_min"):
        if limits[name] < 0:
            raise ValueError(f"--{name.replace('_', '-')} 不能为负。")
    if limits["sentence_min"] > limits["max_span"]:
        raise ValueError(
            f"--sentence-min（{limits['sentence_min']:g}）不能大于 --max-span"
            f"（{limits['max_span']:g}），否则这条规则永远不会命中。")
    return limits


def merge(args):
    limits = limits_from(args)
    source = Path(args.input).expanduser().resolve(strict=True)
    output = Path(args.output).expanduser() if args.output else new_output(source.parent, source.stem, "readable")
    output = check_output(output, source, args.overwrite)
    header, entries = parse_transcript(source.read_text(encoding="utf-8"))
    groups = group_rows(entries, limits)
    rule = (f"最长 {limits['max_span']:g} 秒 / {limits['max_chars']} 字 / 停顿超过 "
            f"{limits['pause']:g} 秒 / 句末且满 {limits['sentence_min']:g} 秒")
    lines = [header, "", f"_（分段说明：读取本次输入的 {len(entries)} 个片段，按「{rule}」合并为 {len(groups)} 段；保留识别文字与时间范围，不做摘要或事实改写。）_", "", "---", ""]
    plain = ""
    for group in groups:
        lines += [f"**[{timestamp(group['start'])} - {timestamp(group['end'])}]**  {group['text']}", ""]
        plain = join_text(plain, group["text"])
    lines += ["---", "", "## 纯文本（无时间戳）", "", plain, ""]
    write_text(output, "\n".join(lines), args.overwrite)
    result = {"input": str(source), "output": str(output.resolve()), "entries": len(entries),
              "groups": len(groups), "limits": limits}
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="本次 transcribe.py 实际输出的 Markdown 路径")
    parser.add_argument("--output", help="可读稿的新路径")
    parser.add_argument("--overwrite", action="store_true", help="明确替换已存在的结果；仍不改原始稿")
    parser.add_argument("--max-span", type=float, default=18.0,
                        help="单段最长秒数，超过即断开；默认 18")
    parser.add_argument("--max-chars", type=int, default=90,
                        help="单段最多字符数，超过即断开；默认 90")
    parser.add_argument("--pause", type=float, default=3.0,
                        help="相邻片段之间的停顿超过该秒数即断开；默认 3")
    parser.add_argument("--sentence-min", type=float, default=None,
                        help="已到句末且长度满该秒数即断开；默认 6，若调小 --max-span 则自动跟随该值")
    try:
        merge(parser.parse_args())
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
