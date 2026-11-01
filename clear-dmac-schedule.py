from __future__ import annotations  # Enables X | None syntax in Python 3.7 - 3.9

import csv
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

import yaml

COMMANDS = [
    "clear mac address-table dynamic",
    "configure terminal",
    "feature scheduler",
    "scheduler logfile size 1024",
    "scheduler job name MAC-CLEAR",
    "show clock >> bootflash:mac_clear_history.txt ; show mac address-table count >> bootflash:mac_clear_history.txt ; clear mac address-table dynamic ; show mac address-table count >> bootflash:mac_clear_history.txt",
    "exit",
    "scheduler schedule name MAC-CLEAR-DAILY",
    "job name MAC-CLEAR",
    "time daily 15:30",
    "exit",
    "end",
    "copy running-config startup-config",
]

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "switches.yaml"   # Jump server & default login settings
CSV_FILE = BASE_DIR / "bamdad.csv"          # Switch hostnames / IPs


class NativeSSHChannel:
    """Wraps macOS native /usr/bin/ssh in a PTY with the same interface as a Paramiko Channel."""

    def __init__(self, master_fd: int, proc: subprocess.Popen):
        self.master_fd = master_fd
        self.proc = proc
        self.closed = False

    def recv_ready(self) -> bool:
        if self.closed or self.master_fd < 0:
            return False
        r, _, _ = select.select([self.master_fd], [], [], 0)
        return bool(r)

    def recv(self, bufsize: int = 65536) -> bytes:
        if self.closed or self.master_fd < 0:
            return b""
        try:
            return os.read(self.master_fd, min(bufsize, 65536))
        except OSError:
            return b""

    def send(self, data: str) -> int:
        if self.closed or self.master_fd < 0:
            return 0
        return os.write(self.master_fd, data.encode("utf-8"))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


def verify_ssh_agent() -> None:
    """Verify ssh-agent using native macOS ssh-add."""
    res = subprocess.run(["ssh-add", "-l"], capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        print(f"{datetime.now()} ⚠️  WARNING: No keys found in SSH agent! (check 'ssh-add -L').")
    else:
        lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
        print(f"{datetime.now()} 🔐 Native SSH Agent active. Found {len(lines)} key(s).")


def load_config() -> tuple[Optional[dict], list[tuple[str, dict]]]:
    """
    Read jump/auth defaults from switches.yaml and switch hostnames from CSV (or fallback to YAML hosts).
    Automatically sanitizes hosts (strips CIDR masks like /64 and ignores header rows).
    """
    config = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    defaults = config.get("auth") or {}

    raw_hosts = []
    if CSV_FILE.exists():
        with open(CSV_FILE, mode="r", encoding="utf-8-sig", newline="") as f:
            sample = f.read(2048)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            reader = csv.reader(f, dialect)
            for row in reader:
                for cell in row:
                    if cell:
                        raw_hosts.append(cell.strip())
    else:
        for entry in config.get("hosts") or []:
            if isinstance(entry, str):
                raw_hosts.append(entry.strip())
            elif isinstance(entry, dict) and "host" in entry:
                raw_hosts.append(str(entry["host"]).strip())

    ignored_words = ("ip", "address", "hostname", "host", "switch", "device", "ip address")
    switches: list[tuple[str, dict]] = []

    for raw in raw_hosts:
        if not raw or raw.startswith("#"):
            continue

        # Strip CIDR mask (e.g., /64) if present
        host = raw.split("/")[0].strip()

        # Skip header labels
        if host.lower() in ignored_words or "ip address" in host.lower():
            continue

        merged_auth = {**defaults}
        if not merged_auth.get("username"):
            raise ValueError(
                f"Missing 'username' in {CONFIG_FILE.name}. Please set 'username' under the 'auth' section."
            )
        switches.append((host, merged_auth))

    return config.get("jump"), switches


def _build_ssh_base_args(auth: dict) -> list[str]:
    """Common OpenSSH flags supporting both modern servers and legacy Cisco switches."""
    args = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=10",
        "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
        "-o", "HostKeyAlgorithms=+ssh-rsa,ssh-dss",
        "-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1,diffie-hellman-group-exchange-sha1",
        "-o", "Ciphers=+aes128-cbc,aes256-cbc,3des-cbc",
    ]
    raw_keys = auth.get("key_files") or []
    if isinstance(raw_keys, str):
        raw_keys = [raw_keys]
    for k in raw_keys:
        expanded = os.path.expanduser(k)
        if os.path.exists(expanded):
            args.extend(["-i", expanded])
    return args


def _spawn_pty_ssh(cmd: list[str]) -> NativeSSHChannel:
    """Spawn native /usr/bin/ssh inside a wide PTY with local echo disabled."""
    master_fd, slave_fd = pty.openpty()

    # Disable local PTY echo so we only read actual output from the remote device
    attrs = termios.tcgetattr(slave_fd)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)

    # Set terminal width to 512 cols so long NX-OS scheduler commands never wrap
    winsize = struct.pack("HHHH", 50, 512, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

    proc = subprocess.Popen(
        cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)
    return NativeSSHChannel(master_fd, proc)


def read_until_prompt(
    chan: NativeSSHChannel,
    timeout: float = 15.0,
    password: Optional[str] = None,
) -> str:
    """
    Continuously read from the channel until a Cisco prompt ('#' or '>'), '[confirm]',
    or 'Destination filename' appears at the end of the output, or until timeout expires.
    """
    output = ""
    password_sent = False
    deadline = time.time() + timeout

    while time.time() < deadline:
        if chan.recv_ready():
            chunk = chan.recv(65536).decode(errors="ignore")
            if not chunk and chan.proc.poll() is not None:
                break
            output += chunk
            stripped = output.strip()
            lower_stripped = stripped.lower()

            # Automatically supply password if prompted and configured in YAML
            if password and not password_sent and lower_stripped.endswith("password:"):
                chan.send(password + "\n")
                password_sent = True
                output = ""
                continue

            if (
                stripped.endswith("#")
                or stripped.endswith(">")
                or lower_stripped.endswith("[confirm]")
                or "destination filename" in lower_stripped.splitlines()[-1:]
            ):
                time.sleep(0.1)
                if not chan.recv_ready():
                    break
        else:
            if chan.proc.poll() is not None and not chan.recv_ready():
                break
            time.sleep(0.15)
    return output


@contextmanager
def jump_session(jump_auth: Optional[dict]) -> Iterator[Optional[dict]]:
    """
    Open a persistent native OpenSSH ControlMaster connection to the jump server.
    Guarantees clean logout when done.
    """
    if not jump_auth:
        yield None
        return

    host = jump_auth["host"]
    user = jump_auth["username"]
    port = int(jump_auth.get("port", 22))
    # Short path in /tmp avoids macOS 104-char Unix socket path limit
    control_path = f"/tmp/ssh-jump-{os.getpid()}.sock"

    print(f"{datetime.now()} 🔑 Logging in to jump server {host} via native SSH")

    cmd = [
        "ssh",
        "-M",
        "-S", control_path,
        "-o", "ControlPersist=30m",
        "-p", str(port),
        *_build_ssh_base_args(jump_auth),
        f"{user}@{host}",
        "echo __JUMP_READY__",
    ]

    chan = _spawn_pty_ssh(cmd)
    try:
        output = ""
        password_sent = False
        deadline = time.time() + 25.0
        while time.time() < deadline:
            if chan.recv_ready():
                chunk = chan.recv(65536).decode(errors="ignore")
                output += chunk
                if jump_auth.get("password") and not password_sent and output.strip().lower().endswith("password:"):
                    chan.send(jump_auth["password"] + "\n")
                    password_sent = True
                    continue
                if "__JUMP_READY__" in output:
                    break
            elif chan.proc.poll() is not None:
                break
            else:
                time.sleep(0.15)

        if "__JUMP_READY__" not in output and not os.path.exists(control_path):
            raise RuntimeError(f"Jump server connection failed: {output.strip()}")

        chan.close()
        yield {"host": host, "user": user, "port": port, "control_path": control_path}

    finally:
        chan.close()
        if os.path.exists(control_path):
            subprocess.run(
                ["ssh", "-S", control_path, "-O", "exit", f"{user}@{host}"],
                capture_output=True,
            )
        print(f"{datetime.now()} 🔒 Logged out of jump server {host}")


@contextmanager
def switch_shell(host: str, auth: dict, jump_info: Optional[dict]) -> Iterator[NativeSSHChannel]:
    """Context manager for a single switch SSH session using native macOS SSH."""
    user = auth["username"]
    port = int(auth.get("port", 22))

    print(f"{datetime.now()} 🔑 Logging in to {host}")

    cmd = ["ssh", "-tt", "-p", str(port), *_build_ssh_base_args(auth)]
    if jump_info is not None:
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        proxy_cmd = (
            f"ssh -S {jump_info['control_path']} "
            f"-p {jump_info['port']} {jump_info['user']}@{jump_info['host']} -W {target}"
        )
        cmd.extend(["-o", f"ProxyCommand={proxy_cmd}"])

    cmd.append(f"{user}@{host}")
    chan = _spawn_pty_ssh(cmd)

    try:
        initial_output = read_until_prompt(chan, timeout=20.0, password=auth.get("password"))
        if not initial_output.strip().endswith(("#", ">")):
            chan.send("\n")
            initial_output += read_until_prompt(chan, timeout=5.0)

        if not initial_output.strip().endswith(("#", ">")):
            raise RuntimeError(f"Did not reach switch prompt. Output: {initial_output.strip()}")

        yield chan

    finally:
        try:
            if not chan.closed:
                chan.send("end\n")
                time.sleep(0.1)
                chan.send("exit\n")
                time.sleep(0.2)
        except Exception:
            pass
        chan.close()
        print(f"{datetime.now()} 🔒 Logged out of {host}")


def configure_mac_scheduler(chan: NativeSSHChannel, host: str) -> None:
    """Clear dynamic MAC table immediately, configure NX-OS daily scheduler, and save config."""
    while chan.recv_ready():
        chan.recv(65536)

    for cmd in COMMANDS:
        # Saving config on Nexus switches can take 20-45 seconds
        cmd_timeout = 60.0 if cmd.startswith("copy ") else 15.0

        chan.send(cmd + "\n")
        output = read_until_prompt(chan, timeout=cmd_timeout)

        # Handle confirmation prompts if any appear
        lower_out = output.lower()
        if "[confirm]" in lower_out or "destination filename" in lower_out:
            chan.send("\n")
            output += read_until_prompt(chan, timeout=cmd_timeout)

        # Fallback if 'clear mac address-table dynamic' requires hyphenated syntax on older platforms
        if cmd == "clear mac address-table dynamic" and "% invalid" in output.lower():
            alt_cmd = "clear mac-address-table dynamic"
            chan.send(alt_cmd + "\n")
            output = read_until_prompt(chan, timeout=cmd_timeout)

        # Check for CLI error indicators (ignoring benign NX-OS '[#####]' progress bars)
        has_error = any(
            line.strip().startswith("%") or "% invalid" in line.lower() or "syntax error" in line.lower()
            for line in output.splitlines()
        )

        if has_error or not output.strip().endswith("#"):
            print(f"{datetime.now()} ❌ {host} rejected command '{cmd}':\n{output.strip()}")
            return

    print(f"{datetime.now()} ✅ Dynamic MAC table cleared & 'MAC-CLEAR-DAILY' scheduled (15:30) on {host}")


def _handle_sigterm(signum, frame):
    print(f"\n{datetime.now()} 🛑 Termination signal received. Closing active SSH sessions...")
    sys.exit(1)


signal.signal(signal.SIGINT, _handle_sigterm)
signal.signal(signal.SIGTERM, _handle_sigterm)


def main() -> None:
    verify_ssh_agent()

    try:
        jump_auth, switches = load_config()
    except Exception as e:
        print(f"{datetime.now()} 📢 Configuration error: {type(e).__name__}: {e}")
        sys.exit(1)

    if not switches:
        print(f"{datetime.now()} ⚠️  No valid switches found. Check your CSV or YAML file. Exiting.")
        return

    try:
        with jump_session(jump_auth) as jump_info:
            for host, auth in switches:
                try:
                    with switch_shell(host, auth, jump_info) as chan:
                        configure_mac_scheduler(chan, host)
                except Exception as e:
                    print(f"{datetime.now()} 📢 {host} failed: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"{datetime.now()} 📢 Jump server failed: {type(e).__name__}: {e}")
        sys.exit(1)

    print(f"{datetime.now()} 🏁 All switches processed. All SSH sessions closed.")


if __name__ == "__main__":
    main()
