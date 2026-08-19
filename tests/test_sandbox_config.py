from __future__ import annotations

import shutil
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import requests

from MCPs.Mcp_client import load_registry, select_registry_servers
from Sandbox.docker_runtime import DockerRuntime, DockerRuntimeError
from Sandbox.models import SandboxMounts
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
        self.destroyed = []
        self.closed = False

    def create(self, agent_id, mounts):
        self.created = (agent_id, mounts)
        return "sandbox-1"

    def destroy(self, sandbox_id):
        self.destroyed.append(sandbox_id)

    def close(self):
        self.closed = True


class _FakeContainer:
    id = "container-1"
    status = "running"

    def reload(self):
        return None


class _FakeContainers:
    def __init__(self) -> None:
        self.kwargs = None

    def run(self, **kwargs):
        self.kwargs = kwargs
        return _FakeContainer()


class SandboxConfigurationTests(unittest.TestCase):
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

    def test_docker_create_reuses_running_agent_container(self) -> None:
        with _directories() as (_, challenge, work):
            runtime = object.__new__(DockerRuntime)
            runtime.settings = SandboxSettings()
            existing = _FakeContainer()

            class _ExistingContainers:
                def list(self, **_kwargs):
                    return [existing]

                def run(self, **_kwargs):
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
        stale.remove.assert_called_once_with(force=True)

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
