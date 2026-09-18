"""异步任务管理器：支持服务端后台执行、客户端断开后恢复、任务状态查询。

任务状态：pending -> running -> completed / failed
任务进度：当前步骤、消息、已用时间、进度日志
任务结果：完成后保存结果摘要和文件路径
任务持久化：保存到 JSON 文件，服务重启后可以恢复（未完成的任务标记为 interrupted）
"""

import asyncio
import json
import time
import uuid
import os
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable, Awaitable

logger = logging.getLogger(__name__)


class Task:
    """单个分析任务。"""

    def __init__(self, task_id: str, brand: str, product: Optional[str] = None,
                 method: Optional[str] = None, brand_alias: Optional[str] = None,
                 thinking: Optional[str] = None):
        self.task_id = task_id
        self.brand = brand
        self.product = product
        self.method = method
        self.brand_alias = brand_alias
        self.thinking = thinking

        self.status = "pending"  # pending, running, completed, failed, interrupted
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.completed_at: Optional[float] = None
        self.elapsed = 0.0

        self.current_step = ""
        self.current_message = ""
        self.progress_log: List[Dict[str, Any]] = []  # {elapsed, step, message}

        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None

        # 额外参数（如 force_refresh 等）
        self.extra: Dict[str, Any] = {}

        # 内部使用：asyncio.Task 引用
        self._async_task: Optional[asyncio.Task] = None
        # 排队时保存的执行函数
        self._runner: Optional[Callable[[Task], Awaitable[None]]] = None
        # 进度回调队列（用于 SSE 实时推送）
        self._progress_queue: Optional[asyncio.Queue] = None

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典（用于持久化和API返回）。"""
        return {
            "task_id": self.task_id,
            "brand": self.brand,
            "product": self.product,
            "method": self.method,
            "brand_alias": self.brand_alias,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed": self.elapsed,
            "current_step": self.current_step,
            "current_message": self.current_message,
            "progress_log": self.progress_log[-100:],  # 只保留最近100条
            "result": self.result,
            "error": self.error,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        """从字典反序列化。"""
        task = cls(
            task_id=data["task_id"],
            brand=data.get("brand", ""),
            product=data.get("product"),
            method=data.get("method"),
            brand_alias=data.get("brand_alias"),
            thinking=data.get("thinking"),
        )
        task.status = data.get("status", "interrupted")
        task.created_at = data.get("created_at", time.time())
        task.started_at = data.get("started_at")
        task.completed_at = data.get("completed_at")
        task.elapsed = data.get("elapsed", 0)
        task.current_step = data.get("current_step", "")
        task.current_message = data.get("current_message", "")
        task.progress_log = data.get("progress_log", [])
        task.result = data.get("result")
        task.error = data.get("error")
        task.extra = data.get("extra", {})
        # 从持久化恢复的任务，如果是 running 状态，标记为 interrupted
        if task.status == "running":
            task.status = "interrupted"
            task.error = "服务重启，任务中断"
        return task

    def add_progress(self, step: str, message: str, elapsed: float):
        """添加进度记录。"""
        self.current_step = step
        self.current_message = message
        self.elapsed = elapsed
        self.progress_log.append({
            "elapsed": elapsed,
            "step": step,
            "message": message,
        })
        # 推送到 SSE 队列
        if self._progress_queue is not None:
            try:
                self._progress_queue.put_nowait({
                    "type": "progress",
                    "step": step,
                    "message": message,
                    "elapsed": elapsed,
                })
            except Exception:
                pass

    def set_result(self, result: Dict[str, Any]):
        """设置任务结果。"""
        self.status = "completed"
        self.completed_at = time.time()
        self.result = result
        if self._progress_queue is not None:
            try:
                self._progress_queue.put_nowait({"type": "done", "summary": result})
            except Exception:
                pass

    def set_error(self, error: str):
        """设置任务错误。"""
        self.status = "failed"
        self.completed_at = time.time()
        self.error = error
        if self._progress_queue is not None:
            try:
                self._progress_queue.put_nowait({"type": "error", "message": error})
            except Exception:
                pass


class TaskManager:
    """任务管理器：创建、查询、持久化任务，支持并发限制和排队。"""

    def __init__(self, persist_dir: str | None = None, max_concurrent: int = 2):
        self.tasks: Dict[str, Task] = {}
        from paths import TASKS_DIR
        self.persist_dir = Path(persist_dir) if persist_dir else TASKS_DIR
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self.max_concurrent = max_concurrent
        self._load_from_disk()

    def _load_from_disk(self):
        """从磁盘加载历史任务。

        注意：服务重启后，pending 状态的任务会丢失 runner（无法序列化），
        所以把它们标记为失败，避免一直卡在排队状态。
        """
        count = 0
        for f in self.persist_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                task = Task.from_dict(data)
                # 服务重启后，pending 状态的任务无法恢复（runner 丢失），标记为失败
                if task.status == "pending":
                    task.status = "failed"
                    task.error = "服务重启，排队任务中断"
                    task.completed_at = time.time()
                    self._persist_task(task)
                    logger.info(f"排队任务 {task.task_id} 因服务重启标记为失败")
                self.tasks[task.task_id] = task
                count += 1
            except Exception as e:
                logger.warning(f"加载任务文件失败 {f}: {e}")
        logger.info(f"从磁盘加载了 {count} 个历史任务")

    def _persist_task(self, task: Task):
        """持久化单个任务到磁盘。"""
        try:
            f = self.persist_dir / f"{task.task_id}.json"
            f.write_text(json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"持久化任务失败 {task.task_id}: {e}")

    async def create_task(self, brand: str, product: Optional[str] = None,
                          method: Optional[str] = None, brand_alias: Optional[str] = None,
                          thinking: Optional[str] = None) -> Task:
        """创建新任务。"""
        async with self._lock:
            task_id = uuid.uuid4().hex[:12]
            task = Task(task_id, brand, product, method, brand_alias, thinking)
            self.tasks[task_id] = task
            self._persist_task(task)
            logger.info(f"创建任务 {task_id}: {brand}")
            return task

    def _running_count(self) -> int:
        """统计当前运行中的任务数。"""
        return sum(1 for t in self.tasks.values() if t.status == "running")

    def _pending_tasks(self) -> List[Task]:
        """获取排队中的任务，按创建时间升序（最早的在前）。"""
        pending = [t for t in self.tasks.values() if t.status == "pending" and t._runner is not None]
        pending.sort(key=lambda t: t.created_at)
        return pending

    def _actually_start(self, task: Task):
        """真正启动任务执行。"""
        runner = task._runner
        if not runner:
            return
        task.status = "running"
        task.started_at = time.time()
        self._persist_task(task)
        task_id = task.task_id

        async def _wrap_runner():
            try:
                await runner(task)
                logger.info(f"任务 {task_id} 执行完成")
            except Exception as e:
                logger.exception(f"任务 {task_id} 执行失败: {type(e).__name__}: {e}")
                task.set_error(f"{type(e).__name__}: {e}")
            finally:
                logger.info(f"任务 {task_id} 结束，状态={task.status}，调用 _start_next")
                self._persist_task(task)
                # 任务完成后，启动下一个排队的任务
                self._start_next()

        task._async_task = asyncio.create_task(_wrap_runner())

    def _start_next(self):
        """启动下一个排队的任务（如果有并发槽位）。"""
        running = self._running_count()
        pending_all = [t for t in self.tasks.values() if t.status == "pending"]
        pending_with_runner = [t for t in pending_all if t._runner is not None]
        logger.info(f"_start_next: running={running}/{self.max_concurrent}, pending={len(pending_all)} (with_runner={len(pending_with_runner)})")

        if running >= self.max_concurrent:
            logger.info(f"_start_next: 并发数已满 ({running}/{self.max_concurrent})，不启动排队任务")
            return
        if not pending_with_runner:
            if pending_all:
                logger.warning(f"_start_next: 有 {len(pending_all)} 个排队任务但 runner 为 None，无法启动")
            return
        next_task = pending_with_runner[0]
        logger.info(f"启动排队任务 {next_task.task_id}: {next_task.brand}")
        self._actually_start(next_task)

    async def start_task(self, task_id: str, runner: Callable[[Task], Awaitable[None]]):
        """启动任务执行。如果并发数已满，任务进入排队状态。

        Args:
            task_id: 任务ID
            runner: 异步执行函数，接收 Task 对象，执行完成后调用 task.set_result 或 task.set_error
        """
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError(f"任务不存在: {task_id}")
        if task.status in ("running", "completed"):
            return

        # 保存 runner 函数（排队时需要）
        task._runner = runner

        # 检查并发数
        if self._running_count() >= self.max_concurrent:
            task.status = "pending"
            task.current_message = f"排队中（前方有 {self._running_count()} 个任务在运行）"
            self._persist_task(task)
            logger.info(f"任务 {task_id} 进入排队（当前运行 {self._running_count()}/{self.max_concurrent}）")
            return

        # 并发数未满，直接启动
        self._actually_start(task)

    def get_task(self, task_id: str) -> Optional[Task]:
        """获取任务。"""
        return self.tasks.get(task_id)

    def list_tasks(self, limit: int = 50) -> List[Task]:
        """列出任务，按创建时间倒序。"""
        tasks = sorted(self.tasks.values(), key=lambda t: t.created_at, reverse=True)
        return tasks[:limit]

    async def cancel_task(self, task_id: str) -> bool:
        """取消任务。"""
        task = self.tasks.get(task_id)
        if not task or task.status not in ("running", "pending"):
            return False
        if task._async_task:
            task._async_task.cancel()
        task.status = "failed"
        task.error = "用户取消"
        task.completed_at = time.time()
        self._persist_task(task)
        return True

    def delete_task(self, task_id: str) -> bool:
        """删除任务（从内存和磁盘中删除）。"""
        task = self.tasks.get(task_id)
        if not task:
            return False
        # 从内存中删除
        del self.tasks[task_id]
        # 从磁盘中删除
        try:
            f = self.persist_dir / f"{task_id}.json"
            if f.exists():
                f.unlink()
        except Exception as e:
            logger.warning(f"删除任务文件失败 {task_id}: {e}")
        logger.info(f"删除任务 {task_id}")
        return True

    def get_progress_queue(self, task_id: str) -> Optional[asyncio.Queue]:
        """获取任务的进度队列（用于 SSE 推送）。"""
        task = self.tasks.get(task_id)
        if not task:
            return None
        if task._progress_queue is None:
            task._progress_queue = asyncio.Queue()
        return task._progress_queue


# 全局单例
_manager: Optional[TaskManager] = None


def get_task_manager(persist_dir: str | None = None) -> TaskManager:
    """获取全局任务管理器单例。"""
    global _manager
    if _manager is None:
        _manager = TaskManager(persist_dir)
    return _manager
