class SandboxBridgeError(RuntimeError):
    category = "bridge"
    request_may_have_started = False


class BridgeTransportError(SandboxBridgeError):
    category = "transport"
    request_may_have_started = True


class BridgeAuthenticationError(SandboxBridgeError):
    category = "authentication"


class BridgeExpiredError(SandboxBridgeError):
    category = "expired"


class BridgePolicyError(SandboxBridgeError):
    category = "policy"


class BridgeUnavailableError(SandboxBridgeError):
    category = "sandbox_unavailable"


class BridgeResponseError(SandboxBridgeError):
    category = "response_protocol"
    request_may_have_started = True
