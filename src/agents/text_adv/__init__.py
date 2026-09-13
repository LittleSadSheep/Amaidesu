"""文字冒险游戏 Agent

``src/agents/text_adv/`` 自包含包，体现
"加内容 = 加包 + 配置，框架零改动" 的包设计。

当前可导入面：
- :class:`TextAdvConfig` — Agent 配置 Schema（Pydantic；包内单一权威）
- :class:`TextAdvGameAgentState` — 游戏内部状态（内存态 + 环缓冲 + 快照导出）
- :class:`Option` / :class:`ScreenReading` — 识别契约的数据模型（vlm 模块承载）

Agent 主体（``TextAdvGameAgent``）与工具（``text_adv_*`` 系列）由同包
agent.py / tools.py 承载；包级导出面随后续改造同步恢复。
"""

from src.agents.text_adv.config import TextAdvConfig
from src.agents.text_adv.state import TextAdvGameAgentState
from src.agents.text_adv.vlm import Option, ScreenReading

__all__ = [
    # 配置
    "TextAdvConfig",
    # 状态
    "TextAdvGameAgentState",
    # 识别契约数据模型
    "Option",
    "ScreenReading",
]
