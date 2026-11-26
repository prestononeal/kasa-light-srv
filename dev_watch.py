import signal
import subprocess
from threading import Event


def file_versions(paths):
    versions = []
    for path in paths:
        try:
            info = path.stat()
            versions.append((info.st_ino, info.st_mtime_ns, info.st_size))
        except FileNotFoundError:
            versions.append(None)
    return versions


def run(command, paths):
    stopping = Event()

    def stop(_signal, _frame):
        stopping.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    versions = file_versions(paths)
    while not stopping.is_set():
        process = subprocess.Popen(command, start_new_session=True)
        try:
            while not stopping.wait(0.5):
                current = file_versions(paths)
                if current != versions:
                    versions = current
                    print("Development files changed. Restarting the server.", flush=True)
                    break
                # Reap a crashed server and wait for the next edit to retry.
                process.poll()
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
