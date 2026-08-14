import json
import os
import socket
import tempfile
import uuid

_TOKEN_FILE = os.path.join(
    tempfile.gettempdir(), "claude-in-blender", "blender-session-token"
)


class BlenderBridge:
    def __init__(self, host: str = "127.0.0.1", port: int = 9877):
        self._host = host
        self._port = port

    def _read_token(self) -> str | None:
        try:
            with open(_TOKEN_FILE) as f:
                return f.read().strip()
        except OSError:
            return None

    def send(self, command: str, params: dict | None = None) -> dict:
        """命令を送って envelope を返す。

        Returns:
            {"ok": True, "data": {...}, "elapsed_ms": int}
            or {"ok": False, "error": {"message": ...}, "elapsed_ms": int}
        """
        request_id = uuid.uuid4().hex
        result = self._send_once(command, params, request_id)

        ack_required = result.pop("ack_required", False) is True
        ack_operation_id = None
        if command == "execute_code" and ack_required:
            ack_operation_id = result.get("request_id") or request_id
        elif command == "get_request_status" and result.get("ok"):
            data = result.get("data")
            if isinstance(data, dict) and data.get("status") in {"succeeded", "failed"}:
                ack_operation_id = data.get("request_id")

        if isinstance(ack_operation_id, str) and ack_operation_id:
            self._ack_request_result(ack_operation_id)

        return result

    def _send_once(
        self,
        command: str,
        params: dict | None,
        request_id: str,
        *,
        timeout_seconds: int = 75,
    ) -> dict:
        token = self._read_token()
        payload = (
            json.dumps(
                {
                    "token": token,
                    "command": command,
                    "params": params or {},
                    "request_id": request_id,
                }
            )
            + "\n"
        )

        send_started = False
        try:
            with socket.create_connection(
                (self._host, self._port), timeout=timeout_seconds
            ) as sock:
                sock.settimeout(timeout_seconds)
                # From this point a timeout can mean a partial request reached
                # Blender, so execute_code must be treated as outcome_unknown.
                send_started = True
                sock.sendall(payload.encode("utf-8"))
                buffer = b""
                max_response = 10_000_000  # 10 MB
                while b"\n" not in buffer:
                    chunk = sock.recv(8192)
                    if not chunk:
                        break
                    buffer += chunk
                    if len(buffer) > max_response:
                        return self._connection_error(
                            f"Response too large ({len(buffer)} bytes)"
                        )
            line = buffer.split(b"\n", 1)[0]
            result = json.loads(line.decode("utf-8"))
            if not isinstance(result, dict) or "ok" not in result:
                return self._connection_error("Malformed response from Blender bridge")
            return result
        except ConnectionRefusedError:
            return self._connection_error(
                "Cannot connect to Blender. Is the bridge addon enabled?"
            )
        except TimeoutError:
            if command == "execute_code" and send_started:
                return self._connection_error(
                    f"No response from Blender in {timeout_seconds}s "
                    f"(request {request_id}). Outcome unknown; call "
                    f"get_request_status for {request_id} before another execute.",
                    elapsed_ms=timeout_seconds * 1000,
                    request_id=request_id,
                    status="outcome_unknown",
                )
            if not send_started:
                return self._connection_error(
                    f"Connection to Blender timed out after {timeout_seconds}s "
                    "before the request was sent.",
                    elapsed_ms=timeout_seconds * 1000,
                )
            return self._connection_error(
                f"No response from Blender in {timeout_seconds}s (request {request_id}).",
                elapsed_ms=timeout_seconds * 1000,
                request_id=request_id,
            )
        except json.JSONDecodeError:
            return self._connection_error(
                "Invalid response from Blender (not valid JSON)"
            )

    def _ack_request_result(self, operation_id: str) -> None:
        """Best-effort delivery acknowledgement; the observed result stays valid."""
        try:
            self._send_once(
                "ack_request_result",
                {"operation_id": operation_id},
                uuid.uuid4().hex,
                timeout_seconds=5,
            )
        except Exception:  # noqa: BLE001 - ACK failure must not hide delivered result
            pass

    @staticmethod
    def _connection_error(
        message: str,
        elapsed_ms: int = 0,
        request_id: str | None = None,
        status: str | None = None,
    ) -> dict:
        result = {
            "ok": False,
            "error": {"message": message},
            "elapsed_ms": elapsed_ms,
        }
        if request_id is not None:
            result["request_id"] = request_id
        if status is not None:
            result["status"] = status
        return result
