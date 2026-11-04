"""稳定的按行窗口切块，并保留从 1 开始的原始行号。"""

from dataclasses import dataclass
import hashlib
from .config import MAX_TOTAL_CHUNKS

CHUNKER_VERSION = 2


@dataclass(frozen=True)
class Chunk:
    relative_path: str
    start_line: int
    end_line: int
    text: str
    content_hash: str


def split_source_lines(text: str) -> list[str]:
    """只按 CRLF、LF 或 CR 换行；其他 Unicode/控制分隔符仍视为行内字符。

    末尾的换行符只结束前一行，不会凭空增加另一行源码行。
    非空文件若只包含一个行终止符，就有一行空白行。
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not text:
        return []
    lines: list[str] = []
    start = index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\r":
            lines.append(text[start:index])
            index += 2 if index + 1 < length and text[index + 1] == "\n" else 1
            start = index
        elif char == "\n":
            lines.append(text[start:index])
            index += 1
            start = index
        else:
            index += 1
    if start < length:
        lines.append(text[start:])
    elif text[-1] not in ("\r", "\n"):
        lines.append("")
    return lines


def chunk_signature(window_lines: int, overlap_lines: int) -> str:
    if window_lines <= 0:
        raise ValueError("window_lines must be greater than zero")
    if overlap_lines < 0 or overlap_lines >= window_lines:
        raise ValueError("overlap_lines must be at least zero and smaller than window_lines")
    return f"line-breaks-v{CHUNKER_VERSION}:{window_lines}:{overlap_lines}"


def chunk_text(
    relative_path: str,
    text: str,
    window_lines: int = 80,
    overlap_lines: int = 10,
) -> list[Chunk]:
    if (not isinstance(relative_path, str) or not relative_path
            or relative_path.startswith(("/", "\\")) or ":" in relative_path):
        raise ValueError("relative_path must be a non-empty repository-relative path")
    if ".." in relative_path.replace("\\", "/").split("/"):
        raise ValueError("relative_path must not traverse outside the repository")
    if window_lines <= 0:
        raise ValueError("window_lines must be greater than zero")
    if overlap_lines < 0 or overlap_lines >= window_lines:
        raise ValueError("overlap_lines must be at least zero and smaller than window_lines")
    lines = split_source_lines(text)
    if not lines:
        return []

    step = window_lines - overlap_lines
    count = 1 + max(0, (len(lines) - window_lines + step - 1) // step)
    if count > MAX_TOTAL_CHUNKS:
        raise ValueError("Chunk limit exceeded; use a larger line window.")
    chunks: list[Chunk] = []
    start = 0
    while start < len(lines):
        end = min(start + window_lines, len(lines))
        content = "\n".join(lines[start:end])
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        chunks.append(
            Chunk(
                relative_path=relative_path.replace("\\", "/"),
                start_line=start + 1,
                end_line=end,
                text=content,
                content_hash=digest,
            )
        )
        if end == len(lines):
            break
        start += step
    return chunks

