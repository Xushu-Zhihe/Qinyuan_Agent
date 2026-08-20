# Docker Desktop stability checklist

This project uses the Docker Desktop Linux engine through a Windows named
pipe. A Clash HTTP proxy is only for HTTP traffic; it is never a Docker API
socket.

## 1. Pin the Docker endpoint

Run these checks in a new PowerShell window:

```powershell
docker context use desktop-linux
docker context inspect desktop-linux --format '{{json .Endpoints.docker}}'
docker version
docker info
```

The endpoint must be:

```text
npipe:////./pipe/dockerDesktopLinuxEngine
```

Do not set `DOCKER_HOST` to `127.0.0.1:7897`, an HTTP URL, or a Clash
mixed-port. For the current shell, clear an accidental override:

```powershell
$env:DOCKER_HOST = $null
$env:DOCKER_CONTEXT = 'desktop-linux'
$env:QINYUAN_DOCKER_HOST = 'npipe:////./pipe/dockerDesktopLinuxEngine'
```

To remove a persistent user-level override, use Windows Environment Variables
and delete `DOCKER_HOST` and `DOCKER_CONTEXT` if they point at Clash or the
old `default` context. A new terminal is required after changing them.

## 2. Separate Clash from Docker

Docker Desktop Settings > Resources > Proxies may use the Clash host proxy
for image pulls. That setting does not change the Docker socket. Containers
must use `host.docker.internal:7897` when they need to reach a Windows proxy;
`127.0.0.1` inside a container refers to the container itself.

The Python runtime now uses the pinned endpoint and sets the Docker SDK
session to `trust_env = False`, so `HTTP_PROXY`, `HTTPS_PROXY`, and
`ALL_PROXY` cannot redirect Docker API calls. Those variables may remain for
LLM/MCP HTTP requests, but they must not be used as `DOCKER_HOST`.

Optional project settings:

```powershell
$env:QINYUAN_DOCKER_HOST = 'npipe:////./pipe/dockerDesktopLinuxEngine'
$env:QINYUAN_DOCKER_OPERATION_RETRIES = '3'
$env:QINYUAN_DOCKER_RETRY_BACKOFF_SECONDS = '0.25'
$env:QINYUAN_DOCKER_MAX_POOL_SIZE = '8'
$env:QINYUAN_DOCKER_CONTROL_TIMEOUT_SECONDS = '5'
$env:QINYUAN_DOCKER_API_TIMEOUT_SECONDS = '315'
$env:QINYUAN_DOCKER_STOP_TIMEOUT_SECONDS = '2'
```

## 3. Give WSL2 enough headroom

The previous logs contained WSL out-of-memory, VHD mount timeout, and runc
crash symptoms. Docker Desktop being up to date does not prevent resource
exhaustion. Create `%UserProfile%\\.wslconfig` with limits appropriate for the
machine, for example:

```ini
[wsl2]
memory=8GB
processors=4
swap=8GB
localhostForwarding=true
```

Do not allocate more than the host can sustain. After changing the file:

```powershell
wsl --shutdown
```

Then restart Docker Desktop and confirm `docker info` succeeds before starting
the project. Keep at least 20 GB free on the Docker disk image and prune only
known-unused images/containers; do not delete `docker_data.vhdx` as a first
repair step.

## 4. Container ownership rules implemented in code

Each sandbox has the labels `qinyuan.managed=true`,
`qinyuan.component=sandbox`, and a stable `qinyuan.agent_id`. The container
name is deterministic for that identity. A second startup for the same Agent:

1. adopts the already-running container;
2. removes only stopped containers from the same identity;
3. rejects multiple running containers instead of silently creating another;
4. retries transient EOF/connection/5xx failures with bounded backoff for
   non-destructive Docker operations.

Different Agents still have independent containers and can run concurrently.
The code does not reduce the configured Agent concurrency.

Timeout cancellation stops a sandbox without deleting it while an exec output
stream is active. Lifecycle cleanup removes the stopped container afterwards.
Docker metadata and lifecycle calls use the short control timeout; the long API
timeout is reserved for command output streams. A stop request is never retried:
the Docker daemon may still be processing the first request after the client
times out. An interrupt first closes the active host-side output socket, then
quarantines the sandbox and sends one stop request. Later tool calls fail
immediately instead of waiting on the stuck output stream or issuing another
stop request. Health checks no longer create and immediately delete probe
containers.

## 5. Recovery sequence after a Docker failure

```powershell
docker context use desktop-linux
docker version
docker info
docker ps --filter 'label=qinyuan.managed=true' --format 'table {{.ID}}\\t{{.Status}}\\t{{.Names}}\\t{{.Label "qinyuan.agent_id"}}'
qinyuan-agent --health
```

If `docker info` fails, repair Docker Desktop/WSL first. Do not repeatedly
restart the application while the engine is unhealthy. If one Agent reports
multiple running sandboxes, stop only the listed stale container IDs, then
rerun the health check; never remove the Docker VHD or reset Docker Desktop
without a backup and explicit confirmation.
