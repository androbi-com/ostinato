import time

import pytest

from ostinato.backends.fm1_sloop.backend import SloopBackend
from ostinato.backends.fm1_sloop.sim import SimFM1


def wait_for(cond, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    monkeypatch.setenv("OSTINATO_CONFIG_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def sloop(cfgdir):
    """The SLOOP backend on a simulated FM-1, read completely. -> (backend, simulator)"""
    sims = []

    def factory():
        s = SimFM1()
        sims.append(s)
        return s

    b = SloopBackend()
    b.start(None, factory)
    assert wait_for(lambda: b.connection.not_ready() is None, 60)
    yield b, sims[-1]
    b.stop()
