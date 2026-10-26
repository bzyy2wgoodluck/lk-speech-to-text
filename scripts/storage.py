"""Output naming, timestamp formatting and safe writing, shared by every script."""

from datetime import datetime
import os
from pathlib import Path
import tempfile


def timestamp(seconds):
    """Format seconds as HH:MM:SS.mmm; used only for display, never for arithmetic."""
    milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def new_output(directory, stem, kind):
    """Build a run-stamped path so a second run never silently reuses an old result."""
    run = datetime.now().strftime("%Y-%m-%d_%H%M")
    return Path(directory).expanduser() / f"{stem}_{kind}_{run}.md"


def check_output(path, source, overwrite=False):
    """Refuse the paths that would destroy the input or bypass the no-clobber default."""
    path = Path(path).expanduser()
    if path.resolve() == Path(source).resolve():
        raise ValueError("输出不能是输入文件；请另选结果路径。")
    if path.is_symlink():
        raise ValueError("输出路径是符号链接；请另选普通文件路径。")
    if path.exists() and not overwrite:
        raise FileExistsError(f"结果已存在，未覆盖：{path}（另选路径，或明确使用 --overwrite）")
    if path.exists() and not path.is_file():
        raise ValueError(f"输出不是普通文件：{path}")
    return path


def write_text(path, text, overwrite=False):
    """Publish only complete text; never replace an existing file by default."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if overwrite:
        # The explicit overwrite path is replaced only after a complete staged write.
        handle, name = tempfile.mkstemp(prefix=".transcript-", dir=path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            if path.is_symlink():
                raise ValueError("输出路径变成符号链接，停止覆盖。")
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    else:
        # x also protects against a second process creating the path after preflight.
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
