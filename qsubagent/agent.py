from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from .approval import ApprovalManager
from .compaction import ContextCompactor, estimate_tokens
from .config import AppSettings
from .responses import build_tool_outputs, extract_tool_calls, parse_arguments
from .runtime import (
    ActiveTurn,
    ArtifactStore,
    ThreadRepository,
    ToolResult,
    tool_call_fingerprint,
)
from .security import redact_data, redact_text
from tools.registry import ToolRegistry


class ModelRequestTimeout(RuntimeError):
    pass


class AgentRunner:
    def __init__(
        self,
        *,
        client: Any,
        settings: AppSettings,
        registry: ToolRegistry,
        repository: ThreadRepository,
        emit: Callable[[str], None] = print,
        approval_manager: ApprovalManager | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.registry = registry
        self.repository = repository
        self.emit = emit
        self.tools = registry.get_schemas()
        self.approval_manager = approval_manager
        self.artifact_store = ArtifactStore(settings.artifacts)
        self.active_turn: ActiveTurn | None = None
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self.repository.acquire_lease()
        try:
            self.repository.recover_interrupted()
        except Exception:
            self.repository.release_lease()
            raise
        self.state = repository.load_state()
        self.compactor = ContextCompactor(
            client=client,
            settings=settings,
            repository=repository,
            emit=emit,
        )

    async def activate(self) -> None:
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def close(self) -> None:
        if self.has_active_turn():
            await self.interrupt()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        self.repository.release_lease()

    async def _heartbeat_loop(self) -> None:
        interval = max(0.1, self.settings.runtime.lease_ttl_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                self.repository.heartbeat_lease()
            except Exception as exc:
                self.emit(f"[租约错误] {redact_text(str(exc))}")
                turn = self.active_turn
                if turn is not None and turn.task is not None:
                    turn.cancel_event.set()
                    turn.task.cancel()
                raise

    def has_active_turn(self) -> bool:
        return bool(
            self.active_turn
            and self.active_turn.task
            and not self.active_turn.task.done()
        )

    def can_resume(self) -> bool:
        return self.repository.load_state().status == "paused"

    def start_turn(self, user_input: str) -> None:
        if self.has_active_turn():
            raise RuntimeError("已有正在执行的任务")
        turn = self._new_active_turn()
        self.active_turn = turn
        turn.task = asyncio.create_task(self.continue_turn(turn, user_input))
        turn.task.add_done_callback(lambda task: self._handle_turn_done(turn, task))

    def resume_turn(self) -> None:
        if self.has_active_turn():
            raise RuntimeError("已有正在执行的任务")
        if not self.can_resume():
            raise RuntimeError("当前没有可继续的暂停回合")
        turn = self._new_active_turn()
        self.active_turn = turn
        turn.task = asyncio.create_task(
            self.continue_turn(turn, None, resume=True)
        )
        turn.task.add_done_callback(lambda task: self._handle_turn_done(turn, task))

    def _new_active_turn(self) -> ActiveTurn:
        return ActiveTurn(
            tool_timeout_seconds=self.settings.runtime.tool_timeout_seconds,
            thread_id=self.settings.runtime.thread_id,
            execution_mode=self.settings.powershell.mode,
            approval_manager=self.approval_manager,
            artifact_store=self.artifact_store,
        )

    def _handle_turn_done(self, turn: ActiveTurn, task: asyncio.Task[Any]) -> None:
        if self.active_turn is turn:
            self.active_turn = None
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.emit(f"Error: {redact_text(str(exc))}")

    async def continue_turn(
        self,
        turn: ActiveTurn,
        user_input: str | None,
        *,
        resume: bool = False,
    ) -> Any:
        self.repository.heartbeat_lease()
        self._prepare_turn(turn)
        if resume:
            turn.turn_id = self.repository.resume_turn()
            self.repository.reset_tool_rounds(turn.turn_id)
        else:
            if user_input is None:
                raise ValueError("新回合必须包含用户输入")
            turn.turn_id = self.repository.start_turn(user_input)
        self.state = self.repository.load_state()

        try:
            return await asyncio.wait_for(
                self._run_turn(turn, user_input, resume=resume),
                timeout=self.settings.runtime.turn_timeout_seconds,
            )
        except ModelRequestTimeout:
            assert turn.turn_id is not None
            self.repository.pause_turn(turn.turn_id, "model_timeout")
            self.state = self.repository.load_state()
            self.emit(
                "[回合暂停] 模型请求超时且未自动重试；输入 /continue 可显式继续"
            )
            return None
        except asyncio.TimeoutError:
            assert turn.turn_id is not None
            self.repository.pause_turn(turn.turn_id, "turn_timeout")
            self.state = self.repository.load_state()
            self.emit(
                "[回合暂停] 已达到总超时；状态已保存，输入 /continue 可继续"
            )
            return None
        except asyncio.CancelledError:
            assert turn.turn_id is not None
            self.repository.finish_turn(turn.turn_id, "aborted")
            self.state = self.repository.load_state()
            raise
        except Exception:
            assert turn.turn_id is not None
            self.repository.finish_turn(turn.turn_id, "error")
            self.state = self.repository.load_state()
            raise

    def _prepare_turn(self, turn: ActiveTurn) -> None:
        turn.tool_timeout_seconds = self.settings.runtime.tool_timeout_seconds
        turn.thread_id = self.settings.runtime.thread_id
        turn.execution_mode = self.settings.powershell.mode
        turn.approval_manager = self.approval_manager
        turn.artifact_store = self.artifact_store

    async def _run_turn(
        self,
        turn: ActiveTurn,
        user_input: str | None,
        *,
        resume: bool,
    ) -> Any:
        assert turn.turn_id is not None
        if resume:
            if self.state.pending_tool_calls and all(
                call.get("status") != "pending"
                for call in self.state.pending_tool_calls.values()
            ):
                response = await self._create_response(
                    turn,
                    build_tool_outputs(self.state.pending_tool_calls),
                )
            elif self.state.pending_tool_calls:
                response = None
            else:
                original_input = self.repository.get_turn_user_input(turn.turn_id)
                response = await self._create_response(
                    turn,
                    [{"role": "user", "content": original_input}],
                )
        else:
            recovery_outputs = build_tool_outputs(self.state.pending_tool_calls)
            response = await self._create_response(
                turn,
                recovery_outputs + [{"role": "user", "content": user_input}],
            )

        while self.state.pending_tool_calls:
            rounds = self.repository.get_tool_rounds(turn.turn_id)
            if rounds >= self.settings.runtime.hard_tool_rounds:
                self.repository.pause_turn(turn.turn_id, "hard_tool_round_limit")
                self.state = self.repository.load_state()
                self.emit(
                    "[回合暂停] 已达到工具硬上限；状态已保存，"
                    "输入 /continue 可继续"
                )
                return None

            completed = await self._execute_pending_tools(turn)
            if not completed:
                return None
            rounds = self.repository.increment_tool_round(turn.turn_id)
            if rounds == self.settings.runtime.soft_tool_rounds:
                self.repository.append_event(
                    {
                        "type": "soft_tool_round_checkpoint",
                        "tool_rounds": rounds,
                    },
                    turn_id=turn.turn_id,
                )
                self.emit(f"[检查点] 工具轮数达到软上限 {rounds}")

            response = await self._create_response(
                turn,
                build_tool_outputs(self.state.pending_tool_calls),
            )
            if rounds >= self.settings.runtime.soft_tool_rounds:
                await self.compactor.maybe_compact(
                    response,
                    self.state,
                    force_check=True,
                )
                self.state = self.repository.load_state()
            if turn.cancel_event.is_set():
                raise asyncio.CancelledError

        assert response is not None
        await self.compactor.maybe_compact(response, self.state)
        self.repository.finish_turn(turn.turn_id, "completed")
        self.repository.append_event(
            {
                "type": "turn_completed",
                "response_id": getattr(response, "id", None),
            },
            turn_id=turn.turn_id,
        )
        self.state = self.repository.load_state()
        if not self.settings.capabilities.streaming:
            self.emit(str(getattr(response, "output_text", "")))
        return response

    async def _create_response(
        self,
        turn: ActiveTurn,
        incremental_input: list[dict[str, Any]],
    ) -> Any:
        capabilities = self.settings.capabilities
        self.state = self.repository.load_state()
        replay_history = (
            not capabilities.previous_response_id
            or self.state.replay_history_on_next_request
        )

        if (
            capabilities.local_compaction
            and not capabilities.server_compaction
            and self.state.last_input_tokens is not None
            and self.state.last_input_tokens >= self.settings.runtime.compact_threshold
        ):
            await self.compactor.compact(self.state.last_input_tokens, self.state)
            self.state = self.repository.load_state()
            replay_history = True

        request_input = incremental_input
        if replay_history:
            history = self.repository.rebuild_history()
            if estimate_tokens(history + incremental_input) >= (
                self.settings.runtime.compact_threshold
            ):
                await self.compactor.compact(
                    estimate_tokens(history),
                    self.state,
                )
                history = self.repository.rebuild_history()
            request_input = history + incremental_input

        kwargs: dict[str, Any] = {
            "model": self.settings.model.name,
            "input": request_input,
            "tools": self.tools,
            "tool_choice": "auto",
            "store": capabilities.store,
        }
        if (
            capabilities.previous_response_id
            and not replay_history
            and self.state.last_completed_response_id
        ):
            kwargs["previous_response_id"] = self.state.last_completed_response_id
        if capabilities.prompt_cache_key:
            kwargs["prompt_cache_key"] = self.state.thread_id
        if capabilities.previous_response_id and capabilities.server_compaction:
            kwargs["context_management"] = [
                {
                    "type": "compaction",
                    "compact_threshold": self.settings.runtime.compact_threshold,
                }
            ]

        response = await self._request_model(turn, kwargs)
        response_id = getattr(response, "id", None)
        if capabilities.previous_response_id and not response_id:
            raise RuntimeError(
                "服务未返回 response.id；请将 capabilities."
                "previous_response_id 设为 false"
            )
        pending = extract_tool_calls(response)
        for call in pending.values():
            call["fingerprint"] = tool_call_fingerprint(
                call["name"], call["arguments"]
            )
        assert turn.turn_id is not None
        self.repository.record_response(
            turn_id=turn.turn_id,
            request_input=incremental_input,
            response=response,
            pending_calls=pending,
        )
        self.state = self.repository.load_state()
        return response

    async def _request_model(
        self,
        turn: ActiveTurn,
        kwargs: dict[str, Any],
    ) -> Any:
        max_retries = self.settings.runtime.model_max_retries
        for attempt in range(max_retries + 1):
            try:
                return await asyncio.wait_for(
                    self._request_once(turn, kwargs),
                    timeout=self.settings.runtime.model_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise ModelRequestTimeout(
                    "模型请求超时；结果未确认且不会自动重试"
                ) from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status_code = getattr(exc, "status_code", None)
                if status_code in {401, 403}:
                    raise RuntimeError(
                        f"模型鉴权失败（HTTP {status_code}），已停止且不会重试"
                    ) from exc
                retryable = status_code == 429 or (
                    isinstance(status_code, int) and 500 <= status_code <= 599
                )
                if not retryable or attempt >= max_retries:
                    raise
                delay = self.settings.runtime.retry_initial_seconds * (2**attempt)
                self.repository.append_event(
                    {
                        "type": "model_retry",
                        "attempt": attempt + 1,
                        "status_code": status_code,
                        "delay_seconds": delay,
                    },
                    turn_id=turn.turn_id,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _request_once(
        self,
        turn: ActiveTurn,
        kwargs: dict[str, Any],
    ) -> Any:
        responses = self.client.responses
        if not self.settings.capabilities.streaming or not hasattr(
            responses, "stream"
        ):
            return await responses.create(**kwargs)

        async with responses.stream(**kwargs) as stream:
            turn.stream = stream
            try:
                async for event in stream:
                    event_type = getattr(event, "type", "")
                    if event_type == "response.output_text.delta":
                        self.emit(str(getattr(event, "delta", "")))
                return await stream.get_final_response()
            finally:
                turn.stream = None

    async def _execute_pending_tools(self, turn: ActiveTurn) -> bool:
        assert turn.turn_id is not None
        for call_id in list(self.state.pending_tool_calls):
            self.state = self.repository.load_state()
            call = self.state.pending_tool_calls.get(call_id)
            if call is None or call.get("status") != "pending":
                continue
            if turn.cancel_event.is_set():
                raise asyncio.CancelledError

            fingerprint = str(call["fingerprint"])
            repeats = self.repository.unchanged_result_count(
                turn.turn_id, fingerprint
            )
            if repeats >= self.settings.runtime.repeated_call_limit:
                result = ToolResult(
                    output=json.dumps(
                        {
                            "ok": False,
                            "error": "repeated_tool_call_limit",
                            "message": (
                                "The same tool call returned an unchanged result "
                                f"{repeats} times and was blocked."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    status="failed",
                )
                self.repository.finish_tool_call(call_id, result)
                self.repository.pause_turn(turn.turn_id, "repeated_tool_call_limit")
                self.state = self.repository.load_state()
                self.emit(
                    "[回合暂停] 检测到重复且结果不变的工具调用；"
                    "输入 /continue 可让模型接收阻止结果"
                )
                return False

            metadata = self.registry.get_metadata(call["name"])
            self.repository.mark_tool_running(call_id)
            turn.running_call_id = call_id
            turn.running_tool_side_effect = metadata["side_effect"]
            arguments = parse_arguments(call.get("arguments", "{}"))
            self.emit(
                f"[工具开始] {call['name']}: {redact_data(arguments)}"
            )

            try:
                timeout_grace = 5 if call["name"] == "powershell" else 0
                result = await asyncio.wait_for(
                    self.registry.execute(
                        {"name": call["name"], "arguments": arguments},
                        turn=turn,
                    ),
                    timeout=turn.tool_timeout_seconds + timeout_grace,
                )
            except asyncio.CancelledError:
                execution_started = (
                    turn.process is not None
                    or turn.process_controller is not None
                    or turn.interrupted_tool_result is not None
                )
                result = turn.interrupted_tool_result or ToolResult(
                    output=(
                        "Tool was interrupted after execution started; "
                        "outcome is unknown."
                        if metadata["side_effect"] and execution_started
                        else "Tool was aborted before a result was committed."
                    ),
                    status=(
                        "outcome_unknown"
                        if metadata["side_effect"] and execution_started
                        else "aborted"
                    ),
                )
                turn.interrupted_tool_result = None
                self.repository.finish_tool_call(call_id, result)
                raise
            except asyncio.TimeoutError:
                result = ToolResult(
                    output=json.dumps(
                        {
                            "ok": False,
                            "error": "tool_timeout",
                            "message": "Tool timed out and was not retried.",
                        }
                    ),
                    status=(
                        "outcome_unknown" if metadata["side_effect"] else "failed"
                    ),
                )
            except Exception as exc:
                result = ToolResult(
                    output=f"工具执行出错: {exc}",
                    status="failed",
                )
            finally:
                turn.running_call_id = None
                turn.running_tool_side_effect = False

            self.repository.finish_tool_call(call_id, result)
            self.state = self.repository.load_state()
            self.emit(f"[工具完成] {redact_text(result.output[:500])}")
            if turn.cancel_event.is_set():
                raise asyncio.CancelledError
        return True

    async def interrupt(self) -> bool:
        turn = self.active_turn
        if turn is None or turn.task is None or turn.task.done():
            self.emit("当前没有正在执行的任务")
            return False

        turn.cancel_event.set()
        if self.approval_manager is not None:
            self.approval_manager.cancel_all()
        await close_stream(turn.stream)
        if turn.process_controller is not None:
            with suppress(Exception):
                await turn.process_controller.terminate_tree()

        try:
            await asyncio.wait_for(
                asyncio.shield(turn.task),
                timeout=self.settings.runtime.interrupt_grace_seconds,
            )
        except asyncio.TimeoutError:
            turn.task.cancel()
            with suppress(asyncio.CancelledError):
                await turn.task
        except asyncio.CancelledError:
            pass

        self.state = self.repository.load_state()
        self.repository.append_event(
            {
                "type": "turn_aborted",
                "message": "Started side-effect tools may have unknown outcomes",
            },
            turn_id=turn.turn_id,
        )
        self.emit("当前任务已中断；已启动的副作用工具可能结果不确定")
        return True

    def approve(self, request_id: str) -> bool:
        return bool(
            self.approval_manager
            and self.approval_manager.decide(request_id, True)
        )

    def deny(self, request_id: str) -> bool:
        return bool(
            self.approval_manager
            and self.approval_manager.decide(request_id, False)
        )


async def close_stream(stream: Any | None) -> None:
    if stream is None:
        return
    close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
    if close is None:
        return
    with suppress(Exception):
        result = close()
        if inspect.isawaitable(result):
            await result
