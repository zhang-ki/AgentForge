"""有界的只读仓库扫描器，绝不跟随重解析点。"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import stat as stat_module

from .config import MAX_ENTRIES, MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES
from .errors import ScanError
from .safeio import open_source

_REPARSE_POINT = 0x400
_EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist",
    "target", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
    ".cache", "coverage", ".next", ".turbo", ".gradle", ".ssh", ".aws",
    ".azure", ".gcloud", ".agentforge",
}
_SENSITIVE_EXACT_NAMES = {
    ".npmrc", ".pypirc", ".netrc", ".git-credentials", "credentials",
    "credential", "secrets", "secret", "id_rsa", "id_ed25519", "id_ecdsa",
    "id_dsa", "authorized_keys", "known_hosts_private",
}
_SENSITIVE_NAME_PARTS = re.compile(
    r"(^|[_.-])(secret|secrets|credential|credentials|password|passwd|private[_-]?key|"
    r"api[_-]?key|access[_-]?token|refresh[_-]?token)([_.-]|$)", re.IGNORECASE
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{24,}\b"),
    re.compile(
        r"\b(?:password|passwd|api[_-]?key|secret|access[_-]?token|refresh[_-]?token)"
        r"\s*[:=]\s*['\"][^'\"\r\n]{12,}['\"]",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class ScanLimits:
    max_file_bytes: int = MAX_FILE_BYTES
    max_total_bytes: int = MAX_TOTAL_BYTES
    max_files: int = MAX_FILES
    max_entries: int = MAX_ENTRIES

    def __post_init__(self) -> None:
        if self.max_file_bytes <= 0 or self.max_total_bytes <= 0:
            raise ValueError("byte limits must be greater than zero")
        if self.max_files <= 0 or self.max_entries <= 0:
            raise ValueError("file and entry limits must be greater than zero")


@dataclass(frozen=True)
class ScanCandidate:
    relative_path: str
    absolute_path: Path
    size_bytes: int
    mtime_ns: int
    device: int | None = None
    inode: int | None = None
    ctime_ns: int | None = None


@dataclass(frozen=True)
class ScanIssue:
    relative_path: str
    reason: str


@dataclass
class ScanReport:
    root: Path
    candidates: list[ScanCandidate] = field(default_factory=list)
    issues: list[ScanIssue] = field(default_factory=list)
    complete: bool = True
    entries_visited: int = 0

    def add_issue(self, relative_path: str, reason: str) -> None:
        self.issues.append(ScanIssue(relative_path=relative_path, reason=reason))


class ScanReadError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _has_reparse_attribute(info: os.stat_result) -> bool:
    return stat_module.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        common = os.path.commonpath((str(root), str(candidate)))
    except (ValueError, OSError):
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


def _canonical_inside(root: Path, candidate: Path) -> Path:
    """在使用路径前，重新解析并核验其是否仍位于授权范围内。"""
    try:
        normalized = Path(os.path.abspath(os.fspath(candidate)))
        if not _is_within(root, normalized):
            raise ScanReadError("outside_root")
        current = normalized
        while True:
            if _has_reparse_attribute(current.lstat()):
                raise ScanReadError("reparse_point")
            if current == root:
                break
            current = current.parent
        resolved = normalized.resolve(strict=True)
        if not _is_within(root, resolved):
            raise ScanReadError("outside_root")
        return resolved
    except ScanReadError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise ScanReadError("unavailable_path") from None


def _sensitive_name(name: str) -> bool:
    lowered = name.casefold()
    if lowered == ".env" or lowered.startswith(".env.") or lowered.endswith(".env"):
        return True
    if lowered in _SENSITIVE_EXACT_NAMES:
        return True
    if any(lowered.startswith(key + ".") for key in ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")):
        return True
    return bool(_SENSITIVE_NAME_PARTS.search(lowered))


def _excluded_file(name: str) -> str | None:
    lowered = name.casefold()
    if _sensitive_name(lowered):
        return "sensitive_name"
    if lowered.endswith(("-wal", "-shm")) and any(
        suffix in lowered for suffix in (".db", ".sqlite")
    ):
        return "database_file"
    if lowered.endswith((".db", ".sqlite", ".sqlite3")):
        return "database_file"
    if lowered.endswith((".gguf", ".ggml", ".safetensors", ".pt", ".pth", ".onnx", ".ckpt", ".model", ".h5", ".pb", ".tflite", ".bin")):
        return "model_or_binary_extension"
    if lowered.endswith((".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der", ".keystore", ".jks")):
        return "certificate_or_key"
    if lowered.endswith((".exe", ".dll", ".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".7z", ".pdf", ".mp4", ".mp3")):
        return "binary_extension"
    return None


def _inside_excluded_dir(name: str) -> bool:
    lowered = name.casefold()
    return lowered in _EXCLUDED_DIRS or _sensitive_name(lowered)


def scan_repository(root: str | os.PathLike[str], limits: ScanLimits | None = None) -> ScanReport:
    """检查用户明确指定的仓库根目录；绝不跟随链接。"""
    limits = limits or ScanLimits()
    root_input = Path(root).expanduser()
    try:
        root_lexical = Path(os.path.abspath(os.fspath(root_input)))
        root_stat = root_lexical.lstat()
    except (OSError, RuntimeError, ValueError):
        raise ScanError("Repository path is unavailable.") from None
    if _has_reparse_attribute(root_stat):
        raise ScanError("Repository root is a symbolic link or reparse point; choose its real directory.")
    if not stat_module.S_ISDIR(root_stat.st_mode):
        raise ScanError("Repository path must be a directory.")
    try:
        root_real = root_lexical.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ScanError("Repository path could not be normalized safely.") from None
    if not root_real.is_dir():
        raise ScanError("Repository path must resolve to a directory.")

    report = ScanReport(root=root_real)
    stack: list[Path] = [root_real]
    discovered: list[ScanCandidate] = []

    while stack:
        directory = stack.pop()
        try:
            _canonical_inside(root_real, directory)
            remaining = limits.max_entries - report.entries_visited
            entries = []
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    entries.append(entry)
                    if len(entries) > remaining:
                        break
            if len(entries) > remaining:
                report.add_issue(".", "entry_limit")
                report.complete = False
                break
        except (OSError, ScanReadError):
            relative = "." if directory == root_real else directory.relative_to(root_real).as_posix()
            report.add_issue(relative, "unreadable_directory")
            report.complete = False
            continue
        entries.sort(key=lambda entry: entry.name.casefold())
        child_directories: list[Path] = []
        for entry in entries:
            report.entries_visited += 1
            relative = (Path(entry.path).relative_to(root_real)).as_posix()
            if report.entries_visited > limits.max_entries:
                report.add_issue(relative, "entry_limit")
                report.complete = False
                stack.clear()
                break
            candidate_path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                report.add_issue(relative, "unreadable_metadata")
                continue
            if _has_reparse_attribute(info):
                report.add_issue(relative, "reparse_point")
                continue
            try:
                canonical = _canonical_inside(root_real, candidate_path)
            except ScanReadError as exc:
                report.add_issue(relative, exc.reason)
                continue

            if stat_module.S_ISDIR(info.st_mode):
                if _inside_excluded_dir(entry.name):
                    report.add_issue(relative, "excluded_directory")
                else:
                    child_directories.append(canonical)
                continue
            if not stat_module.S_ISREG(info.st_mode):
                report.add_issue(relative, "unsupported_file_type")
                continue
            # Windows 的 DirEntry.stat() 可能返回为零的链接数或文件身份信息。
            # 重新查询规范路径；元数据缺失时采用失败关闭策略。
            try:
                info = canonical.lstat()
            except OSError:
                report.add_issue(relative, "unreadable_metadata")
                continue
            if _has_reparse_attribute(info):
                report.add_issue(relative, "reparse_point")
                continue
            if not stat_module.S_ISREG(info.st_mode):
                report.add_issue(relative, "unsupported_file_type")
                continue
            if info.st_nlink < 1 or not info.st_dev or not info.st_ino:
                report.add_issue(relative, "unverifiable_file_identity")
                continue
            if info.st_nlink > 1:
                report.add_issue(relative, "hard_link")
                continue
            if any(ord(char) < 32 or char in "[]:" for char in relative):
                report.add_issue(relative, "unsafe_reference_path")
                continue
            exclusion = _excluded_file(entry.name)
            if exclusion:
                report.add_issue(relative, exclusion)
                continue
            if info.st_size > limits.max_file_bytes:
                report.add_issue(relative, "file_size_limit")
                continue
            discovered.append(
                ScanCandidate(
                    relative_path=relative,
                    absolute_path=canonical,
                    size_bytes=info.st_size,
                    mtime_ns=info.st_mtime_ns,
                    device=info.st_dev,
                    inode=info.st_ino,
                    ctime_ns=getattr(info, "st_ctime_ns", None),
                )
            )
        # 反向压入目录，使后进先出栈的遍历顺序保持确定。
        for child in reversed(child_directories):
            stack.append(child)

    selected: list[ScanCandidate] = []
    total_bytes = 0
    for candidate in sorted(discovered, key=lambda item: item.relative_path.casefold()):
        if len(selected) >= limits.max_files:
            report.add_issue(candidate.relative_path, "file_count_limit")
            report.complete = False
            break
        if total_bytes + candidate.size_bytes > limits.max_total_bytes:
            report.add_issue(candidate.relative_path, "total_size_limit")
            report.complete = False
            continue
        selected.append(candidate)
        total_bytes += candidate.size_bytes
    report.candidates = selected
    return report


def read_candidate(candidate: ScanCandidate, root: Path, max_file_bytes: int = MAX_FILE_BYTES) -> str:
    """读取一个已发现的文本文件，并重新验证文件类型与仓库边界。"""
    relative_parts = candidate.relative_path.replace("\\", "/").split("/")
    if not relative_parts or any(part in ("", ".", "..") for part in relative_parts):
        raise ScanReadError("invalid_relative_path")
    root_real = root.resolve(strict=True)
    expected = root_real.joinpath(*relative_parts)
    if os.path.normcase(str(expected)) != os.path.normcase(str(candidate.absolute_path)):
        raise ScanReadError("invalid_relative_path")
    try:
        path = _canonical_inside(root_real, candidate.absolute_path)
        info = path.lstat()
    except (OSError, RuntimeError, ScanReadError):
        raise ScanReadError("unavailable_path") from None
    if _has_reparse_attribute(info):
        raise ScanReadError("reparse_point")
    if not stat_module.S_ISREG(info.st_mode):
        raise ScanReadError("not_regular_file")
    if (info.st_nlink != 1 or not info.st_dev or not info.st_ino
            or (candidate.device is not None and candidate.device != info.st_dev)
            or (candidate.inode is not None and candidate.inode != info.st_ino)):
        raise ScanReadError("file_changed_or_hard_link")
    if info.st_size > max_file_bytes:
        raise ScanReadError("file_size_limit")
    try:
        with open_source(root_real, path) as source:
            opened = os.fstat(source.fileno())
            if opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ScanReadError("file_changed_or_hard_link")
            raw = source.read(max_file_bytes + 1)
            finished = os.fstat(source.fileno())
            if (finished.st_nlink != 1
                    or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                    != (finished.st_dev, finished.st_ino, finished.st_size, finished.st_mtime_ns)):
                raise ScanReadError("file_changed_during_read")
    except OSError:
        raise ScanReadError("unreadable_file") from None
    if len(raw) > max_file_bytes:
        raise ScanReadError("file_size_limit")
    if b"\x00" in raw or any(byte < 9 or 13 < byte < 32 or byte == 127 for byte in raw):
        raise ScanReadError("binary_content")
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise ScanReadError("unsupported_encoding") from None
    if any(pattern.search(text) for pattern in _SECRET_TEXT_PATTERNS):
        raise ScanReadError("secret_like_content")
    return text

