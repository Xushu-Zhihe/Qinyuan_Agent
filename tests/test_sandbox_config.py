from __future__ import annotations

import shutil
import unittest
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock, Thread
from unittest.mock import MagicMock, patch
from uuid import uuid4

import requests

from MCPs.Mcp_client import load_registry, select_registry_servers
from Sandbox.docker_runtime import DockerRuntime, DockerRuntimeError
from Sandbox.models import (
    CommandRequest,
    CommandResult,
    SandboxMounts,
    SandboxStatus,
)
from Sandbox.service import SandboxError, SandboxService
from Sandbox.settings import SandboxSettings


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "MCPs" / "Mcp_client" / "registry.json"


@contextmanager
def _directories():
    root = ROOT / ".test-work" / uuid4().hex
    challenge = root / "challenge"
    work = root / "work"
    challenge.mkdir(parents=True)
    work.mkdir()
    try:
        yield root, challenge, work
    finally:
        shutil.rmtree(root, ignore_errors=True)
        try:
            root.parent.rmdir()
        except OSError:
            pass


class _FakeRuntime:
    def __init__(self) -> None:
        self.created = None
        self.interrupted = []
        self.destroyed = []
        self.closed = False

    def create(self, agent_id, mounts):
        self.created = (agent_id, mounts)
        return "sandbox-1"

    def destroy(self, sandbox_id):
        self.destroyed.append(sandbox_id)

    def interrupt(self, sandbox_id):
        self.interrupted.append(sandbox_id)

    def close(self):
        self.closed = True


class _BlockingRuntime(_FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.released = Event()

    def execute(self, sandbox_id, request):
        del request
        self.started.set()
        self.released.wait()
        return CommandResult(
            sandbox_id=sandbox_id,
            exit_code=137,
            stdout="",
            stderr="interrupted",
        )

    def interrupt(self, sandbox_id):
        super().interrupt(sandbox_id)
        self.released.set()


class _StuckInterruptRuntime(_BlockingRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.interrupt_started = Event()
        self.interrupt_released = Event()

    def interrupt(self, sandbox_id):
        _FakeRuntime.interrupt(self, sandbox_id)
        self.interrupt_started.set()
        self.interrupt_released.wait()


class _BlockingOutputStream:
    def __init__(self) -> None:
        self.started = Event()
        self.closed = Event()
        self.close_calls = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.started.set()
        self.closed.wait()
        raise StopIteration

    def close(self) -> None:
        self.close_calls += 1
        self.closed.set()


class _FakeContainer:
    id = "container-1"
    status = "running"

    def __init__(self) -> None:
        self.start_calls = 0

    def start(self):
        self.start_calls += 1

    def reload(self):
        return None


class _FakeContainers:
    def __init__(self) -> None:
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _FakeContainer()


class SandboxConfigurationTests(unittest.TestCase):
    def test_interrupt_stops_then_cleanup_destroys_after_execution_unlocks(
        self,
    ) -> None:
        runtime = _BlockingRuntime()
        service = SandboxService(runtime)
        service.start_for_agent("agent-1")
        execution = Thread(
            target=service.execute,
            args=("agent-1", CommandRequest("bash", timeout_seconds=30)),
        )
        execution.start()
        self.assertTrue(runtime.started.wait(1))

        self.assertEqual(service.interrupt_for_agent("agent-1"), "sandbox-1")
        execution.join(1)

        self.assertFalse(execution.is_alive())
        self.assertEqual(runtime.interrupted, ["sandbox-1"])
        self.assertEqual(runtime.destroyed, [])
        self.assertEqual(
            service.get_session("agent-1").status,
            SandboxStatus.STOPPED,
        )

        service.close()
        self.assertEqual(runtime.destroyed, ["sandbox-1"])

    def test_interrupted_sandbox_rejects_new_execution_before_agent_lock(
        self,
    ) -> None:
        runtime = _StuckInterruptRuntime()
        service = SandboxService(runtime)
        service.start_for_agent("agent-1")
        execution = Thread(
            target=service.execute,
            args=("agent-1", CommandRequest("bash", timeout_seconds=30)),
        )
        execution.start()
        self.assertTrue(runtime.started.wait(1))
        interruption = Thread(
            target=service.interrupt_for_agent,
            args=("agent-1",),
        )
        interruption.start()
        self.assertTrue(runtime.interrupt_started.wait(1))

        try:
            with self.assertRaisesRegex(SandboxError, "is not running"):
                service.execute(
                    "agent-1",
                    CommandRequest("bash", timeout_seconds=30),
                )
        finally:
            runtime.interrupt_released.set()
            runtime.released.set()
            interruption.join(1)
            execution.join(1)

        self.assertFalse(interruption.is_alive())
        self.assertFalse(execution.is_alive())

    def test_repeated_interrupt_only_stops_docker_once(self) -> None:
        runtime = _FakeRuntime()
        service = SandboxService(runtime)
        service.start_for_agent("agent-1")

        self.assertEqual(service.interrupt_for_agent("agent-1"), "sandbox-1")
        self.assertEqual(service.interrupt_for_agent("agent-1"), "sandbox-1")

        self.assertEqual(runtime.interrupted, ["sandbox-1"])

    def test_docker_connection_error_preserves_underlying_cause(self) -> None:
        with patch(
            "Sandbox.docker_runtime.docker.DockerClient",
            side_effect=PermissionError("access denied to Docker Desktop socket"),
        ):
            with self.assertRaisesRegex(
                DockerRuntimeError,
                "Cannot connect to Docker: access denied to Docker Desktop socket",
            ):
                DockerRuntime(SandboxSettings())

    def test_docker_client_uses_explicit_endpoint_without_parent_proxy(self) -> None:
        client = MagicMock()
        client.version.return_value = {"ApiVersion": "1.45"}
        with patch(
            "Sandbox.docker_runtime.docker.DockerClient",
            return_value=client,
        ) as docker_client, patch.object(DockerRuntime, "_ensure_network"):
            DockerRuntime(
                SandboxSettings(
                    docker_host="npipe:////./pipe/dockerDesktopLinuxEngine",
                )
            )
        docker_client.assert_called_once_with(
            base_url="npipe:////./pipe/dockerDesktopLinuxEngine",
            version="auto",
            timeout=10,
            max_pool_size=8,
        )
        self.assertFalse(client.api.trust_env)
        self.assertEqual(client.api.timeout, 5)

    def test_transient_docker_transport_failure_is_retried(self) -> None:
        runtime = object.__new__(DockerRuntime)
        runtime.settings = SandboxSettings(
            docker_operation_retries=3,
            docker_retry_backoff_seconds=0,
        )
        operation = MagicMock(
            side_effect=[requests.exceptions.ConnectionError("EOF"), "ok"]
        )
        self.assertEqual(
            runtime._with_retries(operation, action="test operation"),
            "ok",
        )
        self.assertEqual(operation.call_count, 2)

    def test_long_exec_stream_uses_a_separate_docker_client(self) -> None:
        runtime = object.__new__(DockerRuntime)
        runtime.settings = SandboxSettings()
        runtime.client = MagicMock()
        runtime.client.api.api_version = "1.55"
        runtime._stream_client = None
        runtime._stream_client_lock = Lock()
        stream_client = MagicMock()

        with patch(
            "Sandbox.docker_runtime.docker.DockerClient",
            return_value=stream_client,
        ) as docker_client:
            api = runtime._stream_api()

        self.assertIs(api, stream_client.api)
        docker_client.assert_called_once_with(
            base_url="npipe:////./pipe/dockerDesktopLinuxEngine",
            version="1.55",
            timeout=315,
            max_pool_size=8,
        )
        self.assertFalse(stream_client.api.trust_env)

    def test_mounts_resolve_sibling_directories(self) -> None:
        with _directories() as (_, challenge, work):
            mounts = SandboxMounts(challenge, work)
            self.assertEqual(mounts.challenge_dir, challenge.resolve())
            self.assertEqual(mounts.work_dir, work.resolve())

    def test_mounts_reject_overlap_and_traversal(self) -> None:
        with _directories() as (root, challenge, work):
            nested = challenge / "nested"
            nested.mkdir()
            with self.assertRaisesRegex(ValueError, "must not overlap"):
                SandboxMounts(challenge, nested)
            with self.assertRaisesRegex(ValueError, "traversal"):
                SandboxMounts(root / "work" / ".." / "challenge", work)

    def test_service_passes_mounts_to_runtime(self) -> None:
        with _directories() as (root, challenge, work):
            runtime = _FakeRuntime()
            service = SandboxService(runtime)
            mounts = SandboxMounts(challenge, work)
            session = service.start_for_agent("agent-1", mounts)
            self.assertEqual(runtime.created, ("agent-1", mounts))
            self.assertEqual(session.mounts, mounts)
            replacement = root / "replacement"
            replacement.mkdir()
            with self.assertRaisesRegex(SandboxError, "cannot change mounts"):
                service.start_for_agent(
                    "agent-1",
                    SandboxMounts(work_dir=replacement),
                )
            service.close()
            self.assertEqual(runtime.destroyed, ["sandbox-1"])
            self.assertTrue(runtime.closed)

    def test_service_detach_keeps_sandbox_for_next_process(self) -> None:
        with _directories() as (_, challenge, work):
            runtime = _FakeRuntime()
            service = SandboxService(runtime)
            service.start_for_agent(
                "agent-1",
                SandboxMounts(challenge, work),
            )

            service.detach()

            self.assertEqual(runtime.destroyed, [])
            self.assertTrue(runtime.closed)
            self.assertIsNone(service.get_session("agent-1"))

    def test_docker_create_uses_readonly_challenge_and_writable_work(self) -> None:
        with _directories() as (_, challenge, work):
            runtime = object.__new__(DockerRuntime)
            runtime.settings = SandboxSettings()
            runtime.client = type("Client", (), {"containers": _FakeContainers()})()
            with patch.object(runtime, "_ensure_image_exists"):
                runtime.create("agent-1", SandboxMounts(challenge, work))
            kwargs = runtime.client.containers.kwargs
            self.assertEqual(kwargs["working_dir"], "/work")
            self.assertEqual(
                kwargs["volumes"][str(challenge.resolve())],
                {"bind": "/challenge", "mode": "ro"},
            )
            self.assertEqual(
                kwargs["volumes"][str(work.resolve())],
                {"bind": "/work", "mode": "rw"},
            )
            self.assertNotIn("/workspace", kwargs["tmpfs"])
            self.assertEqual(kwargs["environment"]["HOME"], "/tmp/qinyuan-home")
            self.assertEqual(
                kwargs["environment"]["XDG_CACHE_HOME"],
                "/tmp/qinyuan-cache",
            )
            self.assertEqual(
                kwargs["environment"]["POWERSHELL_TELEMETRY_OPTOUT"],
                "1",
            )

    def test_docker_start_retry_reuses_the_created_container(self) -> None:
        runtime = object.__new__(DockerRuntime)
        runtime.settings = SandboxSettings(
            docker_operation_retries=3,
            docker_retry_backoff_seconds=0,
        )
        container = MagicMock()
        container.id = "sandbox-1"
        container.status = "running"
        container.start.side_effect = [
            requests.exceptions.ConnectionError("temporary start failure"),
            None,
        ]
        runtime.client = MagicMock()
        runtime.client.containers.create.return_value = container

        with patch.object(runtime, "_ensure_image_exists"):
            sandbox_id = runtime.create("agent-1", SandboxMounts())

        self.assertEqual(sandbox_id, "sandbox-1")
        runtime.client.containers.create.assert_called_once()
        self.assertEqual(container.start.call_count, 2)
        container.remove.assert_not_called()

    def test_docker_start_failure_preserves_root_error_without_recreating(self) -> None:
        runtime = object.__new__(DockerRuntime)
        runtime.settings = SandboxSettings(
            docker_operation_retries=3,
            docker_retry_backoff_seconds=0,
        )
        container = MagicMock()
        container.id = "sandbox-1"
        container.status = "created"
        container.start.side_effect = requests.exceptions.ConnectionError(
            "failed to start shim: SIGILL: illegal instruction"
        )
        runtime.client = MagicMock()
        runtime.client.containers.create.return_value = container
        runtime.client.containers.list.side_effect = [[], [container]]

        with (
            patch.object(runtime, "_ensure_image_exists"),
            self.assertRaisesRegex(
                DockerRuntimeError,
                "start sandbox failed after 3 attempts.*SIGILL",
            ),
        ):
            runtime.create("agent-1", SandboxMounts())

        runtime.client.containers.create.assert_called_once()
        self.assertEqual(container.start.call_count, 3)
        container.remove.assert_called_once_with(force=False)

    def test_docker_create_reuses_running_agent_container(self) -> None:
        with _directories() as (_, challenge, work):
            runtime = object.__new__(DockerRuntime)
            runtime.settings = SandboxSettings()
            existing = _FakeContainer()

            class _ExistingContainers:
                def list(self, **_kwargs):
                    return [existing]

                def create(self, **_kwargs):
                    raise AssertionError("a second container must not be created")

            runtime.client = type(
                "Client",
                (),
                {"containers": _ExistingContainers()},
            )()
            with patch.object(runtime, "_ensure_image_exists"):
                self.assertEqual(
                    runtime.create("agent-1", SandboxMounts(challenge, work)),
                    "container-1",
                )

    def test_settings_read_validated_environment(self) -> None:
        environment = {
            "QINYUAN_SANDBOX_IMAGE": "example/sandbox:1",
            "QINYUAN_SANDBOX_NETWORK": "isolated_net",
            "QINYUAN_SANDBOX_NETWORK_INTERNAL": "false",
            "QINYUAN_SANDBOX_ALLOWED_TOOLS": "nmap,python3,pwsh",
        }
        with patch.dict("os.environ", environment, clear=True):
            settings = SandboxSettings.from_environment()
        self.assertEqual(settings.image, "example/sandbox:1")
        self.assertEqual(settings.network, "isolated_net")
        self.assertFalse(settings.network_internal)
        self.assertEqual(
            settings.allowed_tools,
            frozenset({"nmap", "python3", "pwsh"}),
        )

    def test_settings_pin_docker_endpoint_and_retry_policy(self) -> None:
        environment = {
            "QINYUAN_DOCKER_HOST": "npipe:////./pipe/dockerDesktopLinuxEngine",
            "QINYUAN_DOCKER_CONNECT_TIMEOUT_SECONDS": "7",
            "QINYUAN_DOCKER_CONTROL_TIMEOUT_SECONDS": "4",
            "QINYUAN_DOCKER_API_TIMEOUT_SECONDS": "320",
            "QINYUAN_DOCKER_STOP_TIMEOUT_SECONDS": "3",
            "QINYUAN_DOCKER_MAX_POOL_SIZE": "4",
            "QINYUAN_DOCKER_OPERATION_RETRIES": "5",
            "QINYUAN_DOCKER_RETRY_BACKOFF_SECONDS": "0.1",
        }
        with patch.dict("os.environ", environment, clear=True):
            settings = SandboxSettings.from_environment()
        self.assertEqual(
            settings.docker_host,
            "npipe:////./pipe/dockerDesktopLinuxEngine",
        )
        self.assertEqual(settings.docker_connect_timeout_seconds, 7)
        self.assertEqual(settings.docker_control_timeout_seconds, 4)
        self.assertEqual(settings.docker_api_timeout_seconds, 320)
        self.assertEqual(settings.docker_stop_timeout_seconds, 3)
        self.assertEqual(settings.docker_max_pool_size, 4)
        self.assertEqual(settings.docker_operation_retries, 5)
        self.assertEqual(settings.docker_retry_backoff_seconds, 0.1)

    def test_settings_reject_invalid_network_mode(self) -> None:
        with patch.dict(
            "os.environ",
            {"QINYUAN_SANDBOX_NETWORK_INTERNAL": "sometimes"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                SandboxSettings.from_environment()

    def test_orphan_cleanup_uses_all_ownership_labels(self) -> None:
        runtime = object.__new__(DockerRuntime)
        runtime.settings = SandboxSettings()
        runtime.client = MagicMock()
        stale = MagicMock()
        stale.id = "stale-1"
        stale.status = "exited"
        runtime.client.containers.list.return_value = [stale]

        removed = runtime.cleanup_agent_containers("agent-1")

        self.assertEqual(removed, ("stale-1",))
        runtime.client.containers.list.assert_called_once_with(
            all=True,
            filters={
                "label": [
                    "qinyuan.managed=true",
                    "qinyuan.component=sandbox",
                    "qinyuan.agent_id=agent-1",
                ]
            },
        )
        stale.remove.assert_called_once_with(force=False)

    def test_explicit_owner_release_removes_running_sandbox(self) -> None:
        runtime = object.__new__(DockerRuntime)
        runtime.settings = SandboxSettings()
        runtime.client = MagicMock()
        running = MagicMock()
        running.id = "running-1"
        running.status = "running"
        running.stop.side_effect = lambda **_kwargs: setattr(running, "status", "exited")
        runtime.client.containers.list.return_value = [running]

        removed = runtime.destroy_agent_containers("agent-1")

        self.assertEqual(removed, ("running-1",))
        running.stop.assert_called_once_with(timeout=2)
        running.remove.assert_called_once_with(force=False)

    def test_interrupt_stops_running_container_without_removing_it(self) -> None:
        runtime = object.__new__(DockerRuntime)
        runtime.settings = SandboxSettings()
        runtime.client = MagicMock()
        runtime._active_output_streams = {}
        runtime._active_output_streams_lock = Lock()
        running = MagicMock()
        running.id = "running-1"
        running.status = "running"
        running.stop.side_effect = lambda **_kwargs: setattr(running, "status", "exited")
        runtime.client.containers.get.return_value = running

        runtime.interrupt("running-1")

        running.stop.assert_called_once_with(timeout=2)
        running.remove.assert_not_called()

    def test_interrupt_closes_host_output_stream_and_unblocks_reader(self) -> None:
        runtime = object.__new__(DockerRuntime)
        runtime.settings = SandboxSettings()
        runtime.client = MagicMock()
        runtime._active_output_streams = {}
        runtime._active_output_streams_lock = Lock()
        output_stream = _BlockingOutputStream()
        stream_api = MagicMock()
        stream_api.exec_start.return_value = output_stream
        runtime._stream_api = MagicMock(return_value=stream_api)
        running = MagicMock()
        running.id = "running-1"
        running.status = "running"
        running.attrs = {"Config": {"WorkingDir": "/work"}}
        running.stop.side_effect = lambda **_kwargs: setattr(running, "status", "exited")
        runtime._get_running_container = MagicMock(return_value=running)
        runtime.client.containers.get.return_value = running
        runtime.client.api.exec_create.return_value = {"Id": "exec-1"}
        runtime.client.api.exec_inspect.return_value = {"ExitCode": 137}
        results = []
        execution = Thread(
            target=lambda: results.append(
                runtime.execute(
                    "running-1",
                    CommandRequest("bash", timeout_seconds=30),
                )
            )
        )
        execution.start()
        self.assertTrue(output_stream.started.wait(1))

        runtime.interrupt("running-1")
        execution.join(1)

        self.assertFalse(execution.is_alive())
        self.assertEqual(output_stream.close_calls, 1)
        self.assertEqual(results[0].exit_code, 137)
        running.stop.assert_called_once_with(timeout=2)

    def test_destroy_does_not_retry_or_remove_when_stop_times_out(self) -> None:
        runtime = object.__new__(DockerRuntime)
        runtime.settings = SandboxSettings(
            docker_operation_retries=3,
            docker_retry_backoff_seconds=0,
        )
        runtime.client = MagicMock()
        running = MagicMock()
        running.id = "running-1"
        running.status = "running"
        running.stop.side_effect = requests.exceptions.ReadTimeout("Docker stuck")
        runtime.client.containers.get.return_value = running

        with self.assertRaisesRegex(DockerRuntimeError, "stop sandbox"):
            runtime.destroy("running-1")

        running.stop.assert_called_once_with(timeout=2)
        running.remove.assert_not_called()

    def test_mcp_server_allowlist_only_restricts_enabled_registry(self) -> None:
        registry = load_registry(REGISTRY)
        selected = select_registry_servers(registry, {"nmap", "httpx"})
        self.assertEqual(set(selected.servers), {"nmap", "httpx"})
        with self.assertRaisesRegex(ValueError, "disabled"):
            select_registry_servers(registry, {"burp"})
        with self.assertRaisesRegex(ValueError, "unknown"):
            select_registry_servers(registry, {"not-registered"})


if __name__ == "__main__":
    unittest.main()
