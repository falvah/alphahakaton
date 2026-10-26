"""Exercise actual Gunicorn against a disposable DB, inside the test container."""
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


def main():
    with tempfile.TemporaryDirectory(prefix="alpha-http-") as directory:
        env = dict(os.environ, DATA_DIR=directory, SANDBOX_MODE="true", DEBUG="false")
        for command in [("migrate", "--noinput"), ("collectstatic", "--noinput"), ("seed_workspace",)]:
            subprocess.run([sys.executable, "manage.py", *command], env=env, check=True,
                           stdout=subprocess.DEVNULL)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        with open(Path(directory) / "gunicorn.log", "w") as log:
            server = subprocess.Popen([sys.executable, "-m", "gunicorn", "config.wsgi:application",
                                       "--bind", f"127.0.0.1:{port}", "--workers", "1",
                                       "--threads", "4", "--no-control-socket"],
                                      env=env, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    try:
                        with urlopen(base + "/health/", timeout=1):
                            break
                    except OSError:
                        if server.poll() is not None:
                            raise RuntimeError("Gunicorn exited before readiness")
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Gunicorn readiness timeout")
                subprocess.run([sys.executable, "-m", "tests.live_http", "--base", base,
                                "--access", str(Path(directory) / "access.txt"),
                                "--output", str(Path(directory) / "results.json")],
                               env=env, check=True)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
        env.update(SANDBOX_MODE="false", SECRET_KEY=secrets.token_urlsafe(64))
        subprocess.run([sys.executable, "manage.py", "check", "--deploy"], env=env, check=True)


if __name__ == "__main__":
    main()
