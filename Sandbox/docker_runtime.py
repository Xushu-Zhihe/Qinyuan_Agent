from __future__ import annotations

import re
import uuid

import docker
from docker.errors import APIError, ImageNotFound, NotFound

from Sandbox.models import CommandRequest, CommandResult
from Sandbox.settings import SandboxSettings

class DockerRuntimeError(RuntimeError):
    pass

class DockerRuntime:
    def __init__(
        self,
        settings: SandboxSettings | None = None,
    ) -> None:
        self.settings = settings or SandboxSettings()

        client = None

        try:
            # 初始化阶段使用较短的超时。
            client = docker.from_env(
                timeout=self.settings.docker_connect_timeout_seconds
            )

            version_info = client.version()

            if not isinstance(version_info, dict):
                raise DockerRuntimeError(
                    "Docker returned an invalid version response"
                )

            if not version_info.get("ApiVersion"):
                raise DockerRuntimeError(
                    "Docker version response does not contain ApiVersion"
                )

            # 初始化完成后，将 API 超时调整为能够覆盖最长工具执行时间。
            client.api.timeout = self.settings.docker_api_timeout_seconds

            self.client = client
            self._ensure_internal_network()

        except DockerRuntimeError:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            raise

        except Exception as error:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

            raise DockerRuntimeError(
                "Cannot connect to Docker."
            ) from error

    def create(self,agent_id: str) -> str:
        self._ensure_image_exists()

        normalized_agent_id = self._normalize_agent_id(agent_id)
        unique_id = uuid.uuid4().hex[:12]
        container_name = f"qinyuan-{normalized_agent_id}-{unique_id}"

        try:
            container = self.client.containers.run(
                image=self.settings.image,
                name=container_name,
                command=["sleep","infinity"],
                detach=True,
                auto_remove=False,
                network=self.settings.network,
                read_only=True,
                tmpfs={
                    "/tmp":(
                        "rw,noexec,nosuid,nodev,"
                        f"size={self.settings.temporary_size},"
                        "mode=1777"
                    ),
                    "/workspace":(
                        "rw,noexec,nosuid,nodev,"
                        f"size={self.settings.workspace_size},"
                        "uid=10001,gid=10001,mode=0700"
                    ),
                },
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit=self.settings.memory_limit,
            memswap_limit=self.settings.memory_swap_limit,
            nano_cpus=self.settings.cpu_limit,
            pids_limit=self.settings.process_limit,
            user="10001:10001",
            working_dir="/workspace",
            labels={
                "qinyuan.managed": "true",
                "qinyuan.component": "sandbox",
                "qinyuan.agent_id": agent_id,
            },
        )

            container.reload()

            if container.status != "running":
                logs = container.logs(
                    stdout=True,
                    stderr=True,
                    tail=100,
                ).decode("utf-8",errors="replace")

                self.destroy(container.id)

                raise DockerRuntimeError(
                        f"Sandbox failed to start: {logs}"
                )

            return container.id

        except DockerRuntimeError:
            raise
        except APIError as error:
            raise DockerRuntimeError(
                f"Failed to create sandbox for agent {agent_id!r}:"
                f"{error.explanation}"
            ) from error

    def execute(
        self,
        sandbox_id: str,
        request: CommandRequest,
    ) -> CommandResult:
        """
        在指定 Agent 的容器中执行命令。

        安全性质：
        1. 不调用 Shell。
        2. 命令和参数使用 argv 列表。
        3. 使用非 root 用户。
        4. 使用 GNU timeout 限制执行时间。
        5. 流式读取输出，防止超大输出占满宿主机内存。
        """

        container = self._get_running_container(sandbox_id)

        command = [
            "timeout",
            "--signal=TERM",
            "--kill-after=5s",
            f"{request.timeout_seconds}s",
            *request.argv,
        ]

        stdout_buffer = _BoundedByteBuffer(
            self.settings.maximum_output_bytes_per_stream
        )
        stderr_buffer = _BoundedByteBuffer(
            self.settings.maximum_output_bytes_per_stream
        )

        output_stream = None

        try:
            # 使用 Docker 低级 API，是为了流式读取 stdout/stderr。
            execution = self.client.api.exec_create(
                container=container.id,
                cmd=command,
                stdout=True,
                stderr=True,
                stdin=False,
                tty=False,
                privileged=False,
                user="10001:10001",
                workdir="/workspace",
            )

            exec_id = execution.get("Id")
            if not isinstance(exec_id, str) or not exec_id:
                raise DockerRuntimeError(
                    "Docker did not return a valid exec ID"
                )

            output_stream = self.client.api.exec_start(
                exec_id=exec_id,
                detach=False,
                tty=False,
                stream=True,
                demux=True,
            )

            for output in output_stream:
                if isinstance(output, tuple):
                    stdout_chunk, stderr_chunk = output
                    stdout_buffer.append(stdout_chunk)
                    stderr_buffer.append(stderr_chunk)

                elif isinstance(output, bytes):
                    # 理论上 demux=True 时应该返回 tuple。
                    # 保留这个分支是为了兼容不同 Docker API 行为。
                    stdout_buffer.append(output)

            execution_info = self.client.api.exec_inspect(exec_id)
            exit_code = execution_info.get("ExitCode")

            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                raise DockerRuntimeError(
                    "Docker did not return a valid exit code"
                )

        except DockerRuntimeError:
            raise

        except APIError as error:
            raise DockerRuntimeError(
                f"Failed to execute command in sandbox {sandbox_id!r}: "
                f"{error.explanation}"
            ) from error

        except Exception as error:
            raise DockerRuntimeError(
                f"Failed to read command output from sandbox "
                f"{sandbox_id!r}"
            ) from error

        finally:
            if output_stream is not None:
                close_stream = getattr(output_stream, "close", None)
                if callable(close_stream):
                    close_stream()

        stdout_bytes, stdout_truncated = stdout_buffer.render()
        stderr_bytes, stderr_truncated = stderr_buffer.render()

        return CommandResult(
            sandbox_id=sandbox_id,
            exit_code=exit_code,
            stdout=self._decode_output(stdout_bytes),
            stderr=self._decode_output(stderr_bytes),

            # 137 还可能是 OOM Kill 或外部 SIGKILL，不能直接视为超时。
            timed_out=exit_code == 124,

            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    def destroy(self,sandbox_id: str) -> None:
        """
        强制停止并删除 Docker容器。

        如果容器已经不存在，直接返回。
        因此该方法可以安全地重复调用。
        """

        try:
            container = self.client.containers.get(sandbox_id)

            container.remove(force=True)

        except NotFound:
            return 

        except APIError as error:
            raise DockerRuntimeError(
                f"Failed to destroy sandbox {sandbox_id!r}:"
                f"{error.explanation}"
            ) from error

    def close(self) -> None:
        """
        关闭 Docker SDK客户端连接

        该方法不会自动删除容器
        SandboxService.close() 会先执行 stop_all()
        然后再调用这个方法
        """
        self.client.close()

    def _ensure_internal_network(self) -> None:
        """
        创建或检查 Docker内部网络

        internal=True的作用：
        - 沙盒之间可以通过该网络同学
        - 沙盒可以访问同网络中的靶场容器
        - 默认不能通过该网络访问外部互联网

        目标靶场容器后续也需要加入该网络
        """

        try:
            network = self.client.networks.get(
                self.settings.network
            )

        except NotFound:
            try:
                network = self.client.networks.create(
                    name=self.settings.network,
                    driver="bridge",
                    internal=True,
                    check_duplicate=True,
                    labels={
                        "qinyuan.managed":"true",
                        "qinyuan.component":"sandbox-network",
                    },
                )


            except APIError as error:
                if error.status_code != 409:
                    raise DockerRuntimeError(
                        "Failed to create sandbox network:"
                        f"{error.explanation}"
                    ) from error

                network = self.client.networks.get(
                    self.settings.network
                )

        network.reload()

        if not network.attrs.get("Internal",False):
            raise DockerRuntimeError(
                f"Docker network {self.settings.network!r} exists"
                "but is not configured as an internal network"
            )

    def _ensure_image_exists(self) -> None:
        """
        检查沙盒镜像是否已经构建。

        镜像不存在时不自动从互联网拉取，
        防止使用未经确认的外部镜像
        """

        try:
            self.client.images.get(self.settings.image)

        except ImageNotFound as error:
            raise DockerRuntimeError(
                f"Sandbox image {self.settings.image!r} does not exist."
                "Build the sandbox iamge before startign agents."
            ) from error

    def _get_running_container(self,sandbox_id:str):
        """
        获取容器并确认容器处于运行状态
        """

        try:
            container = self.client.containers.get(sandbox_id)

        except NotFound as error:
            raise DockerRuntimeError(
                f"Sandbox {sandbox_id!r} does not exist"
            ) from error

        container.reload()

        if container.status != "running":
            raise DockerRuntimeError(
                f"Sandbox {sandbox_id!r} is not running;"
                f"current status: {container.status}"
            )

        return container

    @staticmethod
    def _normalize_agent_id(agent_id: str) -> str:
        """
        将 Agent ID转换为安全的 Docker容器名称片段
        非字母、数字、下划线、点号和连字符汇编替换为连字符
        """

        normalized = re.sub(
            r"[^a-zA-Z0-9_.-]",
            "-",
            agent_id,
        ).strip(".-")

        return normalized[:40] or "agent"

    @staticmethod
    def _decode_output(output: bytes | None) -> str:
        """
        将 Docker返回的字节输出转换为字符串

        errors="replace"可以避免工具输出包含非 UTF-8字节时
        导致整个 Agent流程异常
        """

        if output is None:
            return ""

        return output.decode(
            "utf-8",
            errors="replace",
        )

class _BoundedByteBuffer:
    """
    有界字节缓冲区。

    Docker 命令可能产生几十 MB 甚至更多输出。如果先完整读取再截断，
    宿主机仍然会占用大量内存。

    这个缓冲区只保留：
    - 输出开头的一半
    - 输出结尾的一半
    """

    def __init__(self, limit: int) -> None:
        if limit < 2:
            raise ValueError("buffer limit must be at least 2")

        self.limit = limit
        self.head_limit = limit // 2
        self.tail_limit = limit - self.head_limit

        self.head = bytearray()
        self.tail = bytearray()
        self.total_bytes = 0

    def append(self, data: bytes | None) -> None:
        """追加一段 Docker 输出，但不会让缓冲区无限增长。"""

        if not data:
            return

        self.total_bytes += len(data)
        remaining = data

        if len(self.head) < self.head_limit:
            take_count = min(
                self.head_limit - len(self.head),
                len(remaining),
            )
            self.head.extend(remaining[:take_count])
            remaining = remaining[take_count:]

        if remaining:
            self.tail.extend(remaining)

            if len(self.tail) > self.tail_limit:
                del self.tail[:-self.tail_limit]

    def render(self) -> tuple[bytes, bool]:
        """返回最终字节内容以及是否发生过截断。"""

        if self.total_bytes <= self.limit:
            return bytes(self.head + self.tail), False

        omitted = (
            self.total_bytes
            - len(self.head)
            - len(self.tail)
        )

        marker = (
            f"\n...[{omitted} bytes omitted by Sandbox Runtime]...\n"
        ).encode("utf-8")

        return bytes(self.head) + marker + bytes(self.tail), True