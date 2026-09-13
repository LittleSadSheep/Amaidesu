"""
EventBus 事件名 → Dashboard WS 类型名（单一事实源）

WS 类型规则：``type = 事件名``（精确直通）；唯一例外是 ``room.message.*``
折叠为 ``room.message``（前端多处按此聚合过滤），本模块只承载该例外名。
"""

# room.message.* 族事件的折叠类型名（唯一非直通映射）
ROOM_MESSAGE_TYPE = "room.message"

__all__ = ["ROOM_MESSAGE_TYPE"]
