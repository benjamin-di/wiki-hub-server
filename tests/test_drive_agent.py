"""The Samba account agent applies desired device accounts with ordinary tools."""
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[1] / "examples" / "drive" / "drive-agent.sh"
pytestmark = pytest.mark.skipif(not shutil.which("sh") or os.name == "nt", reason="POSIX shell required")

# Each fake mirrors the BusyBox/Samba command line the agent uses and records it.
FAKES = {
    "addgroup": """
        args = sys.argv[1:]
        lines = read(GROUP)
        if args[0] == "-g":
            lines.append(f"{args[2]}:x:{args[1]}:")
        else:
            user, group = args
            for i, line in enumerate(lines):
                name, x, gid, members = line.split(":")
                if name == group:
                    members = ",".join([m for m in members.split(",") if m] + [user])
                    lines[i] = ":".join((name, x, gid, members))
        write(GROUP, lines)
    """,
    "adduser": """
        args = sys.argv[1:]
        uid, group, name = args[args.index("-u") + 1], args[args.index("-G") + 1], args[-1]
        gid = next(line.split(":")[2] for line in read(GROUP) if line.split(":")[0] == group)
        write(PASSWD, read(PASSWD) + [f"{name}:x:{uid}:{gid}::/nonexistent:/sbin/nologin"])
    """,
    "deluser": """
        write(PASSWD, [line for line in read(PASSWD) if line.split(":")[0] != sys.argv[1]])
    """,
    "delgroup": """
        write(GROUP, [line for line in read(GROUP) if line.split(":")[0] != sys.argv[1]])
    """,
    "smbpasswd": """
        args = sys.argv[1:]
        account, entries = args[-1], {line.split(":")[0]: line for line in read(PDB)}
        if "-a" in args:
            first, second = sys.stdin.read().split("\\n")[:2]
            assert first == second
            write(SECRETS, read(SECRETS) + [f"{account}={first}"])
            entries[account] = f"{account}:1:X:HASH:[U          ]:LCT-0:"
        elif "-d" in args or "-e" in args:
            flags = "[DU         ]" if "-d" in args else "[U          ]"
            fields = entries[account].split(":")
            fields[4] = flags
            entries[account] = ":".join(fields)
        write(PDB, list(entries.values()))
    """,
    "pdbedit": """
        args = sys.argv[1:]
        if "-L" in args:
            print("\\n".join(read(PDB)))
        elif "-x" in args:
            account = args[args.index("-u") + 1]
            write(PDB, [line for line in read(PDB) if line.split(":")[0] != account])
    """,
    "smbstatus": """
        print("Samba version 4.23.8")
        print("PID     Username     Group        Machine")
        print("-" * 40)
        print("\\n".join(read(SESSIONS)))
    """,
}
PRELUDE = """\
#!{python}
import os, sys
GROUP, PASSWD = os.environ["WIKI_DRIVE_GROUP_FILE"], os.environ["WIKI_DRIVE_PASSWD_FILE"]
PDB, SESSIONS, SECRETS = os.environ["FAKE_PDB"], os.environ["FAKE_SESSIONS"], os.environ["FAKE_SECRETS"]
LOG = os.environ["FAKE_LOG"]
def read(path):
    return [line for line in open(path).read().splitlines() if line] if os.path.exists(path) else []
def write(path, lines):
    open(path, "w").write("".join(line + "\\n" for line in lines))
open(LOG, "a").write(" ".join([os.path.basename(sys.argv[0])] + sys.argv[1:]) + "\\n")
"""


@pytest.fixture
def host(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKES.items():
        path = bin_dir / name
        path.write_text(PRELUDE.format(python=sys.executable) + textwrap.dedent(body))
        path.chmod(0o755)
    control = tmp_path / "control"
    (control / "secrets").mkdir(parents=True)
    files = {name: tmp_path / name for name in ("group", "passwd", "pdb", "sessions", "secrets", "log")}
    files["group"].write_text("root:x:0:\n")
    files["passwd"].write_text("root:x:0:0::/root:/bin/sh\nbd:x:1001:1002::/home/bd:/sbin/nologin\n")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "WIKI_DRIVE_CONTROL": str(control),
           "WIKI_DRIVE_SMB_CONF": str(tmp_path / "smb.conf"), "WIKI_DRIVE_GROUP_FILE": str(files["group"]),
           "WIKI_DRIVE_PASSWD_FILE": str(files["passwd"]), "FAKE_PDB": str(files["pdb"]),
           "FAKE_SESSIONS": str(files["sessions"]), "FAKE_SECRETS": str(files["secrets"]),
           "FAKE_LOG": str(files["log"]), "TMPDIR": str(tmp_path)}

    def run(**extra):
        return subprocess.run(["sh", str(AGENT), "once"], env={**env, **extra}, capture_output=True, text=True,
                              timeout=30)

    return control, files, run


def desired(control, generation, *lines):
    (control / "accounts.tsv").write_text("\n".join([f"generation\t{generation}", *lines]) + "\n")


def observed(control):
    rows = [line.split("\t") for line in (control / "observed.tsv").read_text().splitlines()]
    return rows[0][1], {row[1]: row[2:] for row in rows[1:]}


def test_creates_accounts_imports_one_time_password_and_reports(host):
    control, files, run = host
    desired(control, 3, "person\t700001\twp1", "device\t600001\twd1\t700001\tactive")
    (control / "secrets" / "wd1").write_text("A" * 43 + "\n")
    result = run()
    assert result.returncode == 0, result.stderr
    assert "wd1=" + "A" * 43 in files["secrets"].read_text()
    assert not (control / "secrets" / "wd1").exists()
    assert "wd1:x:600001:700001:" in files["passwd"].read_text()
    groups = files["group"].read_text()
    assert "wikidrive:x:699999:wd1" in groups and "wp1:x:700001:" in groups
    assert observed(control) == ("3", {"wd1": ["1", "1", "0"]})
    assert "bd:x:1001" in files["passwd"].read_text()


def test_disables_kicks_and_removes_devices(host):
    control, files, run = host
    desired(control, 1, "person\t700001\twp1", "device\t600001\twd1\t700001\tactive",
            "device\t600002\twd2\t700001\tactive")
    for account in ("wd1", "wd2"):
        (control / "secrets" / account).write_text("B" * 43 + "\n")
    assert run().returncode == 0
    sessions = [subprocess.Popen(["sleep", "60"]) for _ in range(2)]
    try:
        files["sessions"].write_text(f"{sessions[0].pid}  wd1  wp1  192.0.2.10\n"
                                     f"{sessions[1].pid}  wd2  wp1  192.0.2.11\n")
        assert observed(control)[1]["wd1"][2] == "0"
        desired(control, 2, "person\t700001\twp1", "device\t600001\twd1\t700001\tdisabled")
        late = subprocess.Popen(["sleep", "60"])
        sessions.append(late)
        # A session for an account that is already unknown still ends.
        files["sessions"].write_text(files["sessions"].read_text() + f"{late.pid}  wd7  wp1  192.0.2.12\n")
        result = run()
        assert result.returncode == 0, result.stderr
        assert all(process.wait(timeout=5) is not None for process in sessions)
        assert "wd2" not in files["pdb"].read_text() and "wd2:" not in files["passwd"].read_text()
        assert "[DU" in files["pdb"].read_text()
        generation, devices = observed(control)
        assert generation == "2" and set(devices) == {"wd1"} and devices["wd1"][1] == "0"
    finally:
        for process in sessions:
            process.kill()
    desired(control, 3)
    assert run().returncode == 0
    assert observed(control) == ("3", {})
    assert "wp1" not in files["group"].read_text() and "bd:x:1001" in files["passwd"].read_text()


def test_rejects_malformed_and_unlisted_entries(host):
    control, files, run = host
    desired(control, 4, "person\t1002\tbd", "device\t1001\tbd\t1002\tactive",
            "device\t600001\twd1;reboot\t700001\tactive", "person\t700001\twp1",
            "device\t600001\twd1\t700001\tactive")
    (control / "secrets" / "wd1").write_text("short\n")
    (control / "secrets" / "wd9").write_text("C" * 43 + "\n")
    result = run()
    assert result.returncode == 0, result.stderr
    log = files["log"].read_text()
    assert "bd" not in [line.split()[-1] for line in log.splitlines()] and "reboot" not in log
    assert not files["secrets"].exists()
    assert not (control / "secrets" / "wd1").exists()
    # A secret for an account that is not yet listed is kept for the application.
    assert (control / "secrets" / "wd9").exists()
    assert observed(control) == ("4", {})


def test_reports_previous_generation_when_an_account_cannot_be_applied(host):
    control, files, run = host
    files["passwd"].write_text(files["passwd"].read_text() + "wd1:x:600001:12::/x:/sbin/nologin\n")
    desired(control, 5, "person\t700001\twp1", "device\t600001\twd1\t700001\tactive")
    result = run()
    assert result.returncode != 0
    assert "unexpected IDs" in result.stderr
    assert observed(control)[0] == "-1"


def test_reports_go_to_a_separate_directory_without_following_links(host, tmp_path):
    control, files, run = host
    observed_dir = tmp_path / "observed"
    observed_dir.mkdir()
    target = tmp_path / "protected"
    target.write_text("unchanged\n")
    (observed_dir / "observed.tsv").symlink_to(target)
    desired(control, 1)
    result = run(WIKI_DRIVE_OBSERVED_DIR=str(observed_dir))
    assert result.returncode == 0, result.stderr
    assert target.read_text() == "unchanged\n"
    assert not (observed_dir / "observed.tsv").is_symlink()
    assert (observed_dir / "observed.tsv").read_text().startswith("generation\t1")
