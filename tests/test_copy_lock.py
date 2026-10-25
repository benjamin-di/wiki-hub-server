"""Exercise the actual OS lock, including on the native Windows CI runner."""

import subprocess
import sys

from wiki_hub.copy_lock import copy_lock


def test_another_process_is_excluded_and_an_existing_unheld_file_is_reusable(tmp_path):
    path = tmp_path / "copy.lock"
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from wiki_hub.copy_lock import copy_lock, CopyBusyError\n"
        "try:\n"
        " with copy_lock(Path(sys.argv[1])): pass\n"
        "except CopyBusyError:\n"
        " sys.exit(23)\n"
    )
    with copy_lock(path):
        blocked = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, timeout=15)
        assert blocked.returncode == 23, blocked.stderr
    assert path.exists()
    released = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, timeout=15)
    assert released.returncode == 0, released.stderr
