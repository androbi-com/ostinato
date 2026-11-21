"""The single-instance lock of the device server."""
import subprocess
import sys
import time

from ostinato.owner import OwnerLock


def wait_for(cond, timeout=6.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_lock_is_exclusive_and_reentrant(tmp_path):
    a, b = OwnerLock(tmp_path, "a"), OwnerLock(tmp_path, "b")
    assert a.acquire() and a.acquire()  # (ours already: still true)
    assert not b.acquire()
    assert b.holder()["label"] == "a" and "pid" in b.holder_text()
    a.release()
    assert b.acquire() and not a.acquire()
    b.release()


def test_lock_dies_with_its_process(tmp_path):
    # another process takes the lock and is killed: the OS drops the lock
    code = ("import time; from ostinato.owner import OwnerLock; "
            "l = OwnerLock(%r, 'child'); print(l.acquire(), flush=True); time.sleep(60)") % str(tmp_path)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "True"
        mine = OwnerLock(tmp_path, "parent")
        assert not mine.acquire()
        p.kill()
        p.wait(5)
        assert wait_for(mine.acquire, 3)
        mine.release()
    finally:
        p.kill()
