"""HTTP 层的错误类型。

所有失败都带一个 `code`，前端按 code 决定展示什么（重试按钮、跳转、还是
直接提示"这台机器路径不对"）。只回一句话的接口最后一定会在前端长出一堆
字符串匹配。
"""

from __future__ import annotations

from typing import Any

from fastapi import status

from .hub import AgentError
from .protocol import ErrorCode


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        # 失败也要带上"这条命令现在是什么状态"：页面靠它决定显示"重试"
        # 还是"结果未知，请查询当前预置点"。
        self.data = data
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
        # 控制链路的三种"没做成"都是 409：请求本身合法，是设备当前的状态
        # 不允许它发生（过期、被更新的指令覆盖、有人正在本地操作）。
        # 用 4xx 而不是 504，前端才会显示"重发/放弃"而不是"上游挂了"。
        ErrorCode.EXPIRED: status.HTTP_409_CONFLICT,
        ErrorCode.STALE: status.HTTP_409_CONFLICT,
        ErrorCode.CONTROL_HELD: status.HTTP_409_CONFLICT,
        # 媒体链路：相机不可达与"本机没开预览"都是"这台机器上暂时做不到"，
        # 归 503。前端据此显示具体原因并提示改用快照，而不是无限重连。
        ErrorCode.CAMERA_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.STREAM_DISABLED: status.HTTP_503_SERVICE_UNAVAILABLE,
        # 机器人连不上它自己的本地 backend：同样是 503，但页面文案不同——
        # "机器人本地服务不可达"指配置/进程问题，"相机不可用"指硬件问题。
        ErrorCode.BACKEND_UNREACHABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    return ApiError(
        mapping.get(error.code, status.HTTP_502_BAD_GATEWAY), error.code, error.message
    )
