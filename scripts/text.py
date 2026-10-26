"""Text joining and the deterministic glossary that repairs ASR misspellings."""

from pathlib import Path


def join_text(left, right):
    """Keep recognized characters; add a word boundary for adjacent ASCII text."""
    if not left:
        return right
    if not right:
        return left
    separator = "" if not (left[-1].isascii() and right[0].isalnum() and right[0].isascii()) else " "
    return left + separator + right


def load_glossary(path):
    """Read `错写 => 正写` lines. 左侧按长度降序，长规则优先，避免被短规则截断。"""
    rules = []
    with Path(path).open(encoding="utf-8") as stream:
        for number, raw in enumerate(stream, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=>" not in line:
                raise ValueError(f"术语表第 {number} 行缺少 '=>'：{line}")
            wrong, right = (part.strip() for part in line.split("=>", 1))
            if not wrong or wrong == right:
                raise ValueError(f"术语表第 {number} 行左右两侧为空或相同：{line}")
            rules.append((wrong, right))
    rules.sort(key=lambda item: len(item[0]), reverse=True)
    return rules


def apply_glossary(text, rules):
    """只改命中的子串，返回新文本和命中记录；未命中处逐字保持不变。"""
    hits = []
    for wrong, right in rules:
        if wrong in text:
            text = text.replace(wrong, right)
            hits.append(f"{wrong}→{right}")
    return text, hits
