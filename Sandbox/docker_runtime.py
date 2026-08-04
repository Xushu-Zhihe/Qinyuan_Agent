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

        try:
            self.client = docker.from_env()
            self.client.ping()
        except DockerRuntimeError:
            raise
        except Exception as error:
            raise DockerRuntimeError(
                "Cannot connect to Docker."
            ) from error

        self._ensure_internal_network()

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
        在指定容器中执行命令。

        重要：
        command是字符串列表，不经过 Shell
        因此 arguments中的 &&、;、 |不会解释为 Shell操作符
        """

        container = self._get_running_container(sandbox_id)

        command = [
            "timeout",
            "--signal=KILL",
            "--kill-after=5s",
            f"{request.timeout_seconds}s",
            *request.argv,
        ]

        try:
            execution = container.exec_run(
                cmd=command,

                user="10001:10001",
                workdir="/workspace",

                stdout=True,
                stderr=True,

                stdin=False,
                tty=False,

                privileged=False,
                demux=True,
            )
        except APIError as error:
            raise DockerRuntimeError(
                f"Failed to execute command in sandbox {sandbox_id!r}:"
                f"{error.explanation}"
            ) from error

        stdout_bytes: bytes | None = None
        stderr_bytes: bytes | None = None

        if isinstance(execution.output,tuple):
            stdout_bytes, stderr_bytes = execution.output

        elif isinstance(execution.output,bytes):
            stdout_bytes = execution.output

        return CommandResult(
            sandbox_id=sandbox_id,
            exit_code=execution.exit_code,
            stdout=self._decode_output(stdout_bytes),
            stderr=self._decode_output(stderr_bytes),

            timed_out=execution.exit_code in {124,137},
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