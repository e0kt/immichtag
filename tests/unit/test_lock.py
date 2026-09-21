import subprocess
import sys
import time

import pytest

from animetag.library import LockBusy, TaggingLock


def test_second_acquire_in_same_process_is_refused(tmp_path):
    with TaggingLock(tmp_path):
        with pytest.raises(LockBusy) as info:
            TaggingLock(tmp_path).acquire()
        assert "another tagging run is active" in str(info.value)
        assert "pid" in str(info.value)
    # released on exit
    with TaggingLock(tmp_path):
        pass


def test_lock_released_when_holder_dies(tmp_path):
    code = (
        "import sys, time\n"
        "from animetag.library import TaggingLock\n"
        f"lock = TaggingLock({str(tmp_path)!r}); lock.acquire()\n"
        "print('locked', flush=True)\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(LockBusy) as info:
            TaggingLock(tmp_path).acquire()
        assert str(child.pid) in str(info.value)
    finally:
        child.kill()
        child.wait()
    deadline = time.time() + 5
    while True:
        try:
            with TaggingLock(tmp_path):
                break
        except LockBusy:
            if time.time() > deadline:
                raise
            time.sleep(0.05)
