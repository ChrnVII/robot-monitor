"""HTTP 层的错误类型。

所有失败都带一个 `code`，前端按 code 决定展示什么（重试按钮、跳转、还是
直接提示"这台机器路径不对"）。只回一句话的接口最后一定会在前端长出一堆
字符串匹配。
"""

from __future__ import annotations

from fastapi import status

from .hub import AgentError
from .protocol import ErrorCode


class ApiError(Exception):
    def __init__(self, status_code: int, code: ErrorCode, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


def from_agent_error(error: AgentError) -> ApiError:
    """agent 的失败码 → HTTP 状态码。

    映射不是机械的：`agent_offline` 是 409（服务端状态不满足），`timeout` 是
    504（上游没响应），`forbidden` 是 403。前端拿到的语义必须准确，否则
    "点重试有没有用"就判断不了。
    """
    mapping = {
        ErrorCode.AGENT_OFFLINE: status.HTTP_409_CONFLICT,
        ErrorCode.TIMEOUT: status.HTTP_504_GATEWAY_TIMEOUT,
        ErrorCode.UNKNOWN_COMMAND: status.HTTP_400_BAD_REQUEST,
        ErrorCode.INVALID_ARGUMENTS: status.HTTP_400_BAD_REQUEST,
        ErrorCode.FORBIDDEN: status.HTTP_403_FORBIDDEN,
        ErrorCode.FILE_NOT_FOUND: status.HTTP_404_NOT_FOUND,
        ErrorCode.NOT_A_FILE: status.HTTP_400_BAD_REQUEST,
        ErrorCode.SIZE_CHANGED: status.HTTP_409_CONFLICT,
        ErrorCode.RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
        ErrorCode.INSUFFICIENT_SPACE: status.HTTP_507_INSUFFICIENT_STORAGE,
    }
    return ApiError(
        mapping.get(error.code, status.HTTP_502_BAD_GATEWAY), error.code, error.message
    )
