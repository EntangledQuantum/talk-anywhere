"""One tray process. The shortcut command talks to it over a local socket."""

from __future__ import annotations

import os
import socket
import threading

from talk_anywhere.paths import socket_path


def send_command(command: str, timeout: float = 1.0) -> str | None:
    """Return the reply, or None when the tray app is not running."""
    path = socket_path()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
        client.sendall((command + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = client.recv(512)
            if not chunk:
                break
            data += chunk
    except (OSError, socket.timeout):
        return None
    finally:
        client.close()
    return data.decode(errors="replace").strip() or "ok"


class CommandServer:
    def __init__(self, callback) -> None:
        path = socket_path()
        self.path = path
        try:
            if path.exists():
                probe = send_command("status", timeout=0.3)
                if probe is not None:
                    raise RuntimeError("already-running")
                path.unlink()
        except FileNotFoundError:
            pass
        self.callback = callback
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        os.chmod(path, 0o600)
        self.sock.listen(8)
        self.sock.settimeout(None)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            data = b""
            conn.settimeout(1.0)
            while b"\n" not in data and len(data) < 1024:
                chunk = conn.recv(256)
                if not chunk:
                    break
                data += chunk
            command = data.decode(errors="replace").strip()
            try:
                reply = self.callback(command) or "ok"
            except Exception as exc:
                reply = f"error {exc}"
            conn.sendall((str(reply).splitlines()[0] + "\n").encode())
        except OSError:
            pass
        finally:
            conn.close()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            self.path.unlink()
        except OSError:
            pass
