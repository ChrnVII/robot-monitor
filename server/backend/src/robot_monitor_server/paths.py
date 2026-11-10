"""A 侧的路径规范化（双层校验的第一层）。

为什么 A 也要校验：A 是唯一暴露在公网的面。如果只靠 B 兜底，一旦 B 的沙箱
有 bug，被利用的就是真实文件系统。两层独立的实现，即使其中一层被绕过，
另一层仍然拦得住。

这一层只做"协议语义"上的规范化：拒绝绝对路径、拒绝 `..`、拒绝空字节。
不做任何文件系统访问——A 根本不知道 B 的根目录在哪。
"""

from __future__ import annotations

SEP = "/"
MAX_REL_LENGTH = 4096
MAX_REL_DEPTH = 128


class InvalidPath(ValueError):
    """调用方应转成 HTTP 400 / ErrorCode.INVALID_ARGUMENTS。"""


def normalize_client_path(rel: str) -> str:
    """把 C 传来的相对路径规范化成线上形式。畸形就抛错，不做"尽力修复"。"""
    if not isinstance(rel, str):
        raise InvalidPath("path must be a string")

    if "\x00" in rel:
        raise InvalidPath("path contains a null byte")

    rel = rel.strip()
    if rel in ("", ".", "./"):
        return ""

    if rel.startswith(SEP):
        raise InvalidPath("absolute paths are not accepted")

    if len(rel) > MAX_REL_LENGTH:
        raise InvalidPath(f"path is too long (>{MAX_REL_LENGTH} chars)")

    # 反斜杠在 POSIX 上是普通文件名字符，只按 `/` 切分
    segments = [seg for seg in rel.split(SEP) if seg not in ("", ".")]

    if len(segments) > MAX_REL_DEPTH:
        raise InvalidPath(f"path is too deep (>{MAX_REL_DEPTH} segments)")

    if any(seg == ".." for seg in segments):
        raise InvalidPath("path traversal is not accepted")

    return SEP.join(segments)


def basename_of(rel: str) -> str:
    """取相对路径的最后一段；根目录返回空串。"""
    clean = normalize_client_path(rel)
    if not clean:
        return ""
    return clean.rsplit(SEP, 1)[-1]
