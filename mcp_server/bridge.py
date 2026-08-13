import json
import os
import socket
import tempfile

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
        token = self._read_token()
        payload = (
            json.dumps(
                {
                    "token": token,
                    "command": command,
                    "params": params or {},
                }
            )
            + "\n"
        )

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30)
        try:
            sock.connect((self._host, self._port))
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
            return json.loads(line.decode("utf-8"))
        except ConnectionRefusedError:
            return self._connection_error(
                "Cannot connect to Blender. Is the bridge addon enabled?"
            )
        except TimeoutError:
            return self._connection_error(
                "Connection to Blender timed out (30s)", elapsed_ms=30000
            )
        except json.JSONDecodeError:
            return self._connection_error(
                "Invalid response from Blender (not valid JSON)"
            )
        finally:
            sock.close()

    @staticmethod
    def _connection_error(message: str, elapsed_ms: int = 0) -> dict:
        return {
            "ok": False,
            "error": {"message": message},
            "elapsed_ms": elapsed_ms,
        }
