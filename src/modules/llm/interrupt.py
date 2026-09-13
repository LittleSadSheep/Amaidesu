"""可中断、有硬超时边界的 LLM 请求取消原语。"""

import asyncio
from typing import Awaitable, Callable, Optional, TypeVar

from src.modules.llm.errors import LLMInterruptedError
from src.modules.logging import get_logger

_logger = get_logger(__name__)

_T = TypeVar("_T")


class HardTimeoutExceeded(Exception):
    """单模型尝试整体越过硬超时墙（墙内重试被截断）。

    由 :func:`guarded_call` 抛出；Engine 捕获后按流式状态决定
    切下一个模型还是整体中止。
    """

    def __init__(self, timeout_ms: int) -> None:
        super().__init__(f"硬超时（{timeout_ms}ms），in-flight 请求已取消")
        self.timeout_ms = timeout_ms


async def _reap(task: "asyncio.Task") -> None:
    """取消并等待子任务彻底结束，保证底层连接清理先于外层继续。"""
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _logger.debug(f"子任务清理期异常（已忽略）: {e}")


async def guarded_call(
    factory: Callable[[], Awaitable[_T]],
    *,
    timeout_ms: int,
    interrupt_flag: Optional[asyncio.Event] = None,
) -> _T:
    """把一次异步调用置于硬超时墙内，超时 / 中断 / 父任务取消统一收敛到子任务取消。

    三种取消来源共用同一条路径：墙到点、调用方 ``interrupt_flag`` 被置位、
    外层任务收到取消——三者都先取消 in-flight 子任务并等待其清理完成，
    再以各自的信号向上表达：

    - 墙到点 → ``HardTimeoutExceeded``（Engine 据此做故障切换决策）
    - 调用方中断 → ``LLMInterruptedError``（整体中止）
    - 父任务取消 → ``CancelledError`` 原样传播
    """
    task = asyncio.ensure_future(factory())
    # 中断等待器与业务任务并列挂进 wait：Event 置位即刻触发，无需轮询
    interrupt_waiter: Optional[asyncio.Future] = (
        asyncio.ensure_future(interrupt_flag.wait()) if interrupt_flag is not None else None
    )
    try:
        wait_set = {task, interrupt_waiter} if interrupt_waiter is not None else {task}
        done, _pending = await asyncio.wait(wait_set, timeout=timeout_ms / 1000)
        if task in done:
            return task.result()
        if interrupt_waiter is not None and interrupt_waiter in done:
            await _reap(task)
            raise LLMInterruptedError("调用方中断，in-flight 请求已取消")
        # 既未完成也未中断 = 墙到点
        await _reap(task)
        raise HardTimeoutExceeded(timeout_ms)
    except asyncio.CancelledError:
        # 父任务取消：走同一条清理路径后再传播
        await _reap(task)
        raise
    finally:
        if interrupt_waiter is not None and not interrupt_waiter.done():
            interrupt_waiter.cancel()


async def await_with_timeout_and_interrupt(
    task: asyncio.Task[_T],
    *,
    timeout: float,
    interrupt_flag: asyncio.Event | None,
    poll_interval: float = 0.02,
) -> _T:
    """等待 LLM 请求完成并支持超时/外部中断，确保 httpx 连接得到清理。

    provider 级超时防止请求无限挂起；中断事件置位时取消底层任务
    并等待其结束，让 httpx 的响应/连接清理先完成，再向上传播
    ``CancelledError``。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while True:
            if interrupt_flag is not None and interrupt_flag.is_set():
                _ = task.cancel()
                raise asyncio.CancelledError

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError

            wait_time = remaining
            if interrupt_flag is not None:
                wait_time = min(wait_time, poll_interval)

            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=wait_time)
            except asyncio.TimeoutError:
                if interrupt_flag is None:
                    _ = task.cancel()
                    raise
    finally:
        if not task.done():
            _ = task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            _logger.debug(f"任务收尾期异常（已忽略）: {e}")
