"""部署在 B 上的 agent：只读、不执行命令、主动外连。"""

__version__ = "0.1.0"

from .sandbox import Sandbox, SandboxError, is_within

__all__ = ["Sandbox", "SandboxError", "__version__", "is_within"]
