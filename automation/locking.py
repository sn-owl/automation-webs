"""Advisory locks for cooperating Core writers; never lock user input files."""
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

_THREAD_LOCK = RLock()


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK, path.open('a+b') as stream:
        try:
            import fcntl
        except ImportError:
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
