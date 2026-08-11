"""중복 실행 방지 락(common.acquire_lock) — 2026-08-11 rescan 5중첩 사고 재발 방지.
살아있는 동일 실행이 있으면 거부하고, 죽은 프로세스의 잔여 lock은 덮어쓴다."""
import os

import pytest

from gonggu.common import ROOT, acquire_lock


def _lockfile(name):
    return ROOT / f'data/output/.{name}.lock'


def _rm(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def test_refuses_when_holder_alive():
    name = '_test_lock_alive'
    lf = _lockfile(name)
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text(str(os.getpid()))          # 현재 프로세스 = 확실히 살아있음
    try:
        with pytest.raises(SystemExit):
            acquire_lock(name)
    finally:
        _rm(lf)


def test_overwrites_dead_holder():
    name = '_test_lock_dead'
    lf = _lockfile(name)
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text('2147483646')              # 존재할 리 없는 pid = 죽은 lock
    try:
        acquire_lock(name)                   # 죽은 lock이면 조용히 덮어쓰고 통과
        assert lf.read_text().strip() == str(os.getpid())
    finally:
        _rm(lf)


def test_acquires_when_free():
    name = '_test_lock_free'
    lf = _lockfile(name)
    _rm(lf)
    try:
        acquire_lock(name)                   # lock 없으면 새로 잡는다
        assert lf.exists() and lf.read_text().strip() == str(os.getpid())
    finally:
        _rm(lf)
