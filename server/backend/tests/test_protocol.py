"""协议契约测试（A 端）。

这套"两端各持一份 protocol.py、不抽共享包"的方案，靠两条测试兜住漂移：

1. **源码比对**：本项目的 `protocol.py` 与 agent 那份必须逐字节一致。
   改协议只改一边 → 这条红。
2. **黄金样例**：两端都读仓库根的 `contract/protocol-frames.json`，
   解析与序列化结果必须与样例一致。两边都改错但改得不一样 → 这条红。

第 2 条是本文件的主体，第 1 条只有 20 行但更狠。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from filebridge_server.protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    CommandName,
    ErrorCode,
    Message,
    MessageType,
    ProtocolError,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = REPO_ROOT / "contract" / "protocol-frames.json"
AGENT_COPY = REPO_ROOT / "agent" / "src" / "filebridge_agent" / "protocol.py"
OWN_COPY = Path(__file__).resolve().parents[1] / "src" / "filebridge_server" / "protocol.py"


# 单独部署一端时，仓库根的 contract/ 并不在机器上（它只是测试夹具）。
# 这时整模块跳过——注意 allow_module_level，模块级 skip 不加这个参数
# pytest 会把它当成收集错误。
if not CONTRACT_PATH.is_file():
    pytest.skip(f"契约样例不存在：{CONTRACT_PATH}（只部署一端时会跳过）", allow_module_level=True)

CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
FRAMES = CONTRACT["frames"]
FRAME_IDS = [case["name"] for case in FRAMES]


# --- 保险丝：两份源码必须一致 -------------------------------------------


def test_protocol_copy_is_identical():
    if not AGENT_COPY.is_file():
        pytest.skip("agent 不在同级目录（独立部署场景），跳过源码比对")

    assert OWN_COPY.read_bytes() == AGENT_COPY.read_bytes(), (
        "server 与 agent 的 protocol.py 已经不一致。改协议时两边要一起改，"
        "或者先把 contract/protocol-frames.json 改对再同步两份实现。"
    )


# --- 黄金样例 -----------------------------------------------------------


def test_contract_version_matches_code():
    assert CONTRACT["protocol_version"] == PROTOCOL_VERSION
    assert CONTRACT["max_frame_bytes"] == MAX_FRAME_BYTES


@pytest.mark.parametrize("case", FRAMES, ids=FRAME_IDS)
def test_golden_frame_roundtrip(case):
    """解析黄金帧，再序列化回去，必须与原样一致。

    这一条同时钉住了三件事：字段名、`exclude_none` 的裁剪行为、枚举的落值形态。
    只测"能解析"是不够的——序列化侧改了字段名照样能解析旧帧。
    """
    raw = json.dumps(case["frame"], ensure_ascii=False)
    message = Message.parse(raw)

    assert message.v == PROTOCOL_VERSION
    assert message.type.value == case["frame"]["type"]
    assert message.id == case["frame"]["id"]

    assert json.loads(message.to_json()) == case["frame"]


@pytest.mark.parametrize("case", FRAMES, ids=FRAME_IDS)
def test_golden_command_names_are_known(case):
    """样例里出现的命令必须都在枚举里。

    防止"样例写错一个字母、两端照着错"这种最隐蔽的漂移。
    """
    command = case["frame"].get("command")
    if command is not None:
        assert command in {item.value for item in CommandName}


@pytest.mark.parametrize("case", CONTRACT["rejected"], ids=[c["name"] for c in CONTRACT["rejected"]])
def test_rejected_frames(case):
    with pytest.raises(ProtocolError, match=case["error_contains"]):
        Message.parse(json.dumps(case["frame"]))


# --- 行为约定 -----------------------------------------------------------


def test_unknown_command_parses_but_is_not_dispatchable():
    """未列在枚举里的命令必须能解析出来。

    如果解析阶段就抛错，老版本 B 收到新命令时会直接关连接，而正确行为是回
    `unknown_command` 让 A 知道"这台机器不会做这件事"。
    """
    raw = json.dumps({"v": 1, "type": "command", "id": "x", "command": "run_command"})
    message = Message.parse(raw)

    assert message.command == "run_command"
    assert message.command_name is None


def test_command_helpers_build_expected_shapes():
    command = Message.command_frame(CommandName.PING, {"echo": 1}, id="fixed")
    assert json.loads(command.to_json()) == {
        "v": 1,
        "type": "command",
        "id": "fixed",
        "command": "ping",
        "payload": {"echo": 1},
        "ts": command.ts,
    }

    result = Message.result_frame("fixed", {"ent": []})
    assert json.loads(result.to_json())["ok"] is True
    assert "command" not in json.loads(result.to_json())
    assert "error" not in json.loads(result.to_json())

    error = Message.error_frame("fixed", ErrorCode.FORBIDDEN, "nope")
    body = json.loads(error.to_json())
    assert body["ok"] is False
    assert body["error"] == {"code": "forbidden", "message": "nope"}

    hello = Message.hello_frame("b-1", {"roots": ["/srv"]})
    assert json.loads(hello.to_json())["agent_id"] == "b-1"


def test_oversized_frame_is_rejected():
    huge = {"v": 1, "type": "command", "id": "x", "payload": {"blob": "a" * MAX_FRAME_BYTES}}
    with pytest.raises(ProtocolError, match="too large"):
        Message.parse(json.dumps(huge).encode())


def test_non_utf8_bytes_are_rejected():
    with pytest.raises(ProtocolError, match="utf-8"):
        Message.parse(b"\xff\xfe\x00")


def test_malformed_json_is_rejected():
    with pytest.raises(ProtocolError, match="malformed json"):
        Message.parse("{not json")


def test_message_type_enum_matches_contract():
    """样例里出现的 type 必须在枚举内（新增 type 要同步契约）。"""
    used = {case["frame"]["type"] for case in FRAMES}
    assert used <= {item.value for item in MessageType}
