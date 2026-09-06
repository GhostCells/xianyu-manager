"""Explicit cloud-only preparation deployment; never imports the application."""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess

CODE = Path("/home/ubuntu/apps/xianyu-manager")
MASTER = Path("/home/ubuntu/xianyu-rehearsal/data/manager-migration-rehearsal.db")
MASTER_HASH = "06dbd541931b392daaa139d76815a029ae7ca738749b057517d743a9ed17322d"
UNIT_ROOT = Path("/etc/systemd/system")
NAMES = [
    "xianyu-preparation",
    "xianyu-preparation-display",
    "xianyu-preparation-vnc",
    "xianyu-preparation-web",
]


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-head", required=True)
    args = parser.parse_args()
    assert os.geteuid() == 0 and CODE.is_dir()
    assert (
        subprocess.check_output(
            [
                "git",
                "-c",
                f"safe.directory={CODE}",
                "-C",
                str(CODE),
                "rev-parse",
                "HEAD",
            ],
            text=True,
        ).strip()
        == args.expected_head
    )
    assert not subprocess.check_output(
        [
            "git",
            "-c",
            f"safe.directory={CODE}",
            "-C",
            str(CODE),
            "status",
            "--porcelain",
        ],
        text=True,
    ).strip()
    assert sha(MASTER) == MASTER_HASH
    assert not any(
        Path(str(MASTER) + suffix).exists() for suffix in ["-wal", "-shm", "-journal"]
    )
    assert all(not (UNIT_ROOT / (name + ".service")).exists() for name in NAMES)
    assert not subprocess.check_output(
        ["ss", "-H", "-ltn", "sport = :8765"], text=True
    ).strip()
    assert not Path("/tmp/.X11-unix/X99").exists()
    for name in ["Xvfb", "xdpyinfo", "xauth", "x11vnc", "websockify"]:
        assert shutil.which(name), name
    user = pwd.getpwnam("ubuntu")
    os.umask(0o077)
    parent = Path("/home/ubuntu/xianyu-preparation")
    parent.mkdir(mode=0o700, exist_ok=True)
    run = parent / datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    run.mkdir(mode=0o700)
    data = run / "data"
    data.mkdir(mode=0o700)
    shutil.copyfile(MASTER, data / "manager.db")
    assert sha(data / "manager.db") == MASTER_HASH
    environment = {
        "PYTHONPATH": str(CODE / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "XIANYU_MANAGER_PREPARE_MODE": "true",
        "XIANYU_MANAGER_SAFE_MODE": "false",
        "XIANYU_MANAGER_LOGIN_AUTHORIZED": "false",
        "XIANYU_MANAGER_ACCOUNT_ID": "",
        "XIANYU_MANAGER_DATA_DIR": str(data),
        "XIANYU_PRODUCT_LIBRARY_DIR": "/home/ubuntu/xianyu-rehearsal/product-library",
        "XIANYU_PRODUCT_VALIDATOR_PATH": "/home/ubuntu/xianyu-rehearsal/private/validate_product_library.py",
        "XIANYU_BROWSER_EXECUTABLE": "/usr/bin/google-chrome-stable",
        "XIANYU_BROWSER_HEADLESS": "false",
        "DISPLAY": ":99",
        "XAUTHORITY": str(run / "Xauthority"),
    }
    (run / "preparation.env").write_text(
        "".join(f"{k}={v}\n" for k, v in environment.items())
    )
    (run / "PURPOSE.txt").write_text(
        "PREPARATION ONLY. Not final production data. No account bound. Login and business forbidden.\n"
    )
    subprocess.run(
        ["xauth", "-f", str(run / "Xauthority"), "source", "-"],
        input=f"add :99 . {secrets.token_hex(16)}\n",
        text=True,
        check=True,
        capture_output=True,
    )
    password = secrets.token_hex(
        4
    )  # RFB authentication uses eight characters; SSH encrypts transport.
    (run / "vnc-password.txt").write_text(password + "\n")
    subprocess.run(
        ["x11vnc", "-storepasswd", password, str(run / "vnc.auth")],
        check=True,
        capture_output=True,
    )
    del password
    for p in [parent, run, data, *run.iterdir(), data / "manager.db"]:
        os.chown(p, user.pw_uid, user.pw_gid)
        p.chmod(0o700 if p.is_dir() else 0o600)
    common = f"""[Service]
User=ubuntu
Group=ubuntu
UMask=0077
EnvironmentFile={run}/preparation.env
WorkingDirectory={CODE}
KillMode=control-group
TimeoutStopSec=45
Restart=on-failure
RestartSec=15
NoNewPrivileges=true
StandardOutput=null
StandardError=null
"""
    header = "[Unit]\nStartLimitIntervalSec=300\nStartLimitBurst=3\n"
    units = {
        "xianyu-preparation-display": header
        + "Description=Xianyu preparation display only\n"
        + common
        + f"ExecStart=/usr/bin/Xvfb :99 -screen 0 1440x900x24 -nolisten tcp -auth {run}/Xauthority\n[Install]\nWantedBy=multi-user.target\n",
        "xianyu-preparation": header
        + "Description=Xianyu PREPARATION - business forbidden\nRequires=xianyu-preparation-display.service\nAfter=xianyu-preparation-display.service\n"
        + common
        + f"ExecStartPre={CODE}/.venv/bin/python {CODE}/scripts/wait_preparation_display.py\nExecStart={CODE}/.venv/bin/python {CODE}/scripts/run_preparation_service.py\n[Install]\nWantedBy=multi-user.target\n",
        "xianyu-preparation-vnc": header
        + "Description=Temporary private preparation desktop\nRequires=xianyu-preparation-display.service\nAfter=xianyu-preparation-display.service\nPartOf=xianyu-preparation-display.service\n"
        + common
        + f"ExecStart=/usr/bin/x11vnc -display :99 -auth {run}/Xauthority -localhost -listen 127.0.0.1 -no6 -rfbport 5901 -rfbauth {run}/vnc.auth -forever -shared -nolookup -quiet -o /dev/null\n",
        "xianyu-preparation-web": header
        + "Description=Temporary loopback noVNC\nRequires=xianyu-preparation-vnc.service\nAfter=xianyu-preparation-vnc.service\nPartOf=xianyu-preparation-vnc.service\n"
        + common
        + "ExecStart=/usr/bin/websockify --web=/usr/share/novnc 127.0.0.1:6080 127.0.0.1:5901\n",
    }
    for name, content in units.items():
        target = UNIT_ROOT / (name + ".service")
        with target.open("x") as stream:
            stream.write(content)
        target.chmod(0o644)
    assert sha(MASTER) == MASTER_HASH
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(
        [
            "systemctl",
            "enable",
            "--now",
            "xianyu-preparation-display.service",
            "xianyu-preparation.service",
        ],
        check=True,
    )
    print(
        json.dumps(
            {
                "run_dir": str(run),
                "head": args.expected_head,
                "master_hash": sha(MASTER),
                "account_bound": False,
                "business_allowed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
