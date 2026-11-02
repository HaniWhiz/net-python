import csv
import os
import re
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import paramiko
import yaml
from paramiko.channel import Channel

# ---------------------------------------------------------------------------
# NX-OS scheduler settings
# ---------------------------------------------------------------------------
JOB_NAME = "MAC-CLEAR"
SCHEDULE_NAME = "MAC-CLEAR-DAILY"
HISTORY_FILE = "bootflash:mac_clear_history.txt"
DEFAULT_TIME = "04:05"          # Switch local time (check with 'show clock')
CLEAR_CMD = "clear mac address-table dynamic"


def job_commands(with_history: bool) -> list[str]:
    """Commands stored inside the scheduler job (entered one per line in config-job mode)."""
    if not with_history:
        return [CLEAR_CMD]
    return [
        f"show clock >> {HISTORY_FILE}",
        f"show mac address-table count >> {HISTORY_FILE}",
        CLEAR_CMD,
        f"show mac address-table count >> {HISTORY_FILE}",
    ]

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "switches.yaml"   # Jump server & default login settings
CSV_FILE = BASE_DIR / "forough.csv"       # Switch hostnames / IPs


def verify_ssh_agent() -> None:
    """Verify that ssh-agent is running and has at least one key (e.g., YubiKey) loaded."""
    agent = paramiko.Agent()
    try:
        keys = agent.get_keys()
        if not keys:
            print(f"{datetime.now()} ⚠️  WARNING: No keys found in SSH agent! Make sure your YubiKey is loaded (check 'ssh-add -L').")
        else:
            key_names = ", ".join(k.get_name() for k in keys)
            print(f"{datetime.now()} 🔐 SSH Agent active. Found {len(keys)} key(s): [{key_names}]")
    finally:
        agent.close()


def load_config() -> tuple[dict | None, list[tuple[str, dict]], dict]:
    """
    Read jump/auth/scheduler settings from switches.yaml and switch hostnames from CSV
    (or fall back to the YAML 'hosts' list). Hosts are deduplicated so the script never
    connects to the same switch twice. Per-host settings in YAML override 'auth'.
    """
    config = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    defaults = config.get("auth") or {}
    if not defaults.get("username"):
        raise ValueError(
            f"Missing 'username' in {CONFIG_FILE.name}. Please set 'username' under the 'auth' section."
        )

    sched = config.get("scheduler") or {}
    sched_time = str(sched.get("time") or DEFAULT_TIME).strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", sched_time)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"Invalid scheduler time '{sched_time}' in {CONFIG_FILE.name}. Use HH:MM (24h).")
    sched_settings = {
        "time": f"{int(m.group(1)):02d}:{m.group(2)}",
        "aaa_password": str(sched.get("aaa_password") or ""),
    }

    raw_hosts: list[tuple[str, dict]] = []
    if CSV_FILE.exists():
        with open(CSV_FILE, mode="r", encoding="utf-8-sig", newline="") as f:
            sample = f.read(2048)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            for row in csv.reader(f, dialect):
                if row and row[0].strip():
                    raw_hosts.append((row[0].strip(), {}))
    else:
        for entry in config.get("hosts") or []:
            if isinstance(entry, str):
                raw_hosts.append((entry.strip(), {}))
            elif isinstance(entry, dict) and "host" in entry:
                overrides = {k: v for k, v in entry.items() if k != "host"}
                raw_hosts.append((str(entry["host"]).strip(), overrides))

    ignored_words = ("ip", "address", "hostname", "host", "switch", "device", "ip address")
    switches: list[tuple[str, dict]] = []
    seen_hosts: set[str] = set()

    for raw, overrides in raw_hosts:
        if not raw or raw.startswith("#"):
            continue

        # Strip CIDR mask (e.g., /24) if present
        host = raw.split("/")[0].strip()

        # Skip header labels and duplicate entries
        if host.lower() in ignored_words or "ip address" in host.lower() or host in seen_hosts:
            continue

        seen_hosts.add(host)
        switches.append((host, {**defaults, **overrides}))

    return config.get("jump"), switches, sched_settings


def open_ssh_client(host: str, auth: dict, sock=None) -> paramiko.SSHClient:
    """Open and return a single SSHClient connection. Cleans up immediately if connect fails."""
    raw_keys = auth.get("key_files") or []
    if isinstance(raw_keys, str):
        raw_keys = [raw_keys]
    key_files = [os.path.expanduser(k) for k in raw_keys]
    use_agent = auth.get("use_agent", True)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            host,
            port=int(auth.get("port", 22)),
            username=auth["username"],
            key_filename=key_files or None,
            passphrase=auth.get("passphrase") or None,
            allow_agent=use_agent,
            password=auth.get("password") or None,
            look_for_keys=False,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
            sock=sock,
        )
        transport = ssh.get_transport()
        if transport:
            transport.set_keepalive(10)
        return ssh
    except Exception:
        ssh.close()
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        raise


def read_until_prompt(chan: Channel, timeout: float = 15.0, allow_user_prompt: bool = False) -> str:
    """
    Continuously read from the channel until a Cisco '#' prompt or '[confirm]'
    appears at the end of the output, or until timeout expires.
    """
    output = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if chan.recv_ready():
            chunk = chan.recv(99999).decode(errors="ignore")
            output += chunk
            stripped = output.strip()
            if not stripped:
                continue

            last_line = stripped.splitlines()[-1].strip()
            lower_last = last_line.lower()

            # Ignore NX-OS '[####...]' progress bar during copy running-config startup-config
            is_progress_bar = last_line.startswith("[") and "#" in last_line

            if not is_progress_bar and (
                last_line.endswith("#")
                or (allow_user_prompt and last_line.endswith(">"))
                or lower_last.endswith("[confirm]")
                or "destination filename" in lower_last
            ):
                time.sleep(0.2)
                if not chan.recv_ready():
                    break
        else:
            time.sleep(0.15)
    return output


@contextmanager
def jump_session(jump_auth: dict | None) -> Iterator[paramiko.SSHClient | None]:
    """Context manager for the jump host. Guarantees logout when done."""
    if not jump_auth:
        yield None
        return

    host = jump_auth["host"]
    print(f"{datetime.now()} 🔑 Logging in to jump server {host}")
    jump_client = open_ssh_client(host, jump_auth)
    try:
        yield jump_client
    finally:
        try:
            jump_client.close()
        except Exception:
            pass
        print(f"{datetime.now()} 🔒 Logged out of jump server {host}")


@contextmanager
def switch_shell(host: str, auth: dict, jump_client: paramiko.SSHClient | None) -> Iterator[Channel]:
    """Context manager for a single switch SSH session with strict cleanup."""
    sock = None
    client: paramiko.SSHClient | None = None
    chan: Channel | None = None

    print(f"{datetime.now()} 🔑 Logging in to {host}")
    try:
        if jump_client is not None:
            transport = jump_client.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("Jump server connection is no longer active")
            sock = transport.open_channel(
                "direct-tcpip", (host, int(auth.get("port", 22))), ("127.0.0.1", 0), timeout=15
            )

        client = open_ssh_client(host, auth, sock=sock)
        chan = client.invoke_shell(width=512, height=50)
        chan.settimeout(30)

        # Wait dynamically for the initial login banner and prompt ('#' or '>')
        initial_output = read_until_prompt(chan, timeout=12.0, allow_user_prompt=True)
        if not initial_output.strip().endswith(("#", ">")):
            chan.send("\n")
            read_until_prompt(chan, timeout=5.0, allow_user_prompt=True)

        yield chan

    finally:
        if chan is not None:
            try:
                if not chan.closed:
                    chan.send("end\n")
                    time.sleep(0.1)
                    chan.send("exit\n")
                    time.sleep(0.2)
            except Exception:
                pass
            try:
                chan.close()
            except Exception:
                pass

        if client is not None:
            try:
                client.close()
            except Exception:
                pass

        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

        print(f"{datetime.now()} 🔒 Logged out of {host}")


def _has_cli_error(output: str) -> bool:
    """Return True if output contains an NX-OS CLI error message."""
    for line in output.splitlines():
        low = line.strip().lower()
        if (
            low.startswith("%")
            or low.startswith("error:")
            or "invalid command" in low
            or "invalid input" in low
            or "syntax error" in low
            or "incomplete command" in low
        ):
            return True
    return False


def run_cmd(chan: Channel, cmd: str, timeout: float = 15.0, log_cmd: str | None = None) -> tuple[bool, str]:
    """
    Send one command and wait for the prompt.
    Returns (ok, output). ok is False on a CLI error or if no '#' prompt came back.
    """
    chan.send(cmd + "\n")
    output = read_until_prompt(chan, timeout=timeout)

    lower_out = output.lower()
    if "[confirm]" in lower_out or "destination filename" in lower_out:
        chan.send("\n")
        output += read_until_prompt(chan, timeout=timeout)

    if log_cmd is not None:
        output = output.replace(cmd, log_cmd)

    ok = not _has_cli_error(output) and output.strip().endswith("#")
    return ok, output


def _strip_echo(output: str, cmd: str) -> str:
    """Remove the echoed command line and trailing prompt so only real output is checked."""
    lines = [ln for ln in output.splitlines() if cmd not in ln]
    if lines and lines[-1].strip().endswith("#"):
        lines = lines[:-1]
    return "\n".join(lines)


def clear_mac_table(chan: Channel, host: str, sched: dict) -> None:
    # Small pause + flush so any trailing login prompt bytes are cleared
    time.sleep(0.3)
    while chan.recv_ready():
        chan.recv(99999)

    # Disable paging so 'show scheduler ...' output is not cut by --More--
    run_cmd(chan, "terminal length 0")

    # 1. Immediate dynamic MAC clear
    ok, output = run_cmd(chan, CLEAR_CMD)
    if not ok:
        print(f"{datetime.now()} ❌ {host} did not accept '{CLEAR_CMD}':\n{output.strip()}")
        return
    print(f"{datetime.now()} ✅ Dynamic MAC address table cleared on {host}")

    # 2. Configure the NX-OS scheduler (idempotent: old job/schedule are removed first)
    def fail(cmd: str, out: str) -> None:
        print(f"{datetime.now()} ❌ {host} did not accept '{cmd}':\n{out.strip()}")
        run_cmd(chan, "end")

    for cmd in ("configure terminal", "feature scheduler", "scheduler logfile size 1024"):
        ok, out = run_cmd(chan, cmd)
        if not ok:
            return fail(cmd, out)

    # Needed when you log in with TACACS+/RADIUS: the job runs as the user who created it
    if sched["aaa_password"]:
        cmd = f"scheduler aaa-authentication password 0 {sched['aaa_password']}"
        ok, out = run_cmd(chan, cmd, log_cmd="scheduler aaa-authentication password 0 ******")
        if not ok:
            return fail("scheduler aaa-authentication password 0 ******", out)

    # Job commands cannot be edited on NX-OS. Re-entering an existing job APPENDS
    # commands, so delete schedule + job first. Errors here just mean "did not exist".
    run_cmd(chan, f"no scheduler schedule name {SCHEDULE_NAME}")
    run_cmd(chan, f"no scheduler job name {JOB_NAME}")

    cmd = f"scheduler job name {JOB_NAME}"
    ok, out = run_cmd(chan, cmd)
    if not ok:
        return fail(cmd, out)

    with_history = True
    for job_cmd in job_commands(with_history=True):
        if ">>" in job_cmd and not with_history:
            continue
        ok, out = run_cmd(chan, job_cmd)
        if not ok and ">>" in job_cmd and job_cmd.startswith("show clock"):
            # First '>>' line rejected: this release has no '>>' in job mode. Use clear only.
            with_history = False
            print(f"{datetime.now()} ℹ️  {host} does not support '>>' in scheduler jobs; job will only run '{CLEAR_CMD}'.")
            continue
        if not ok:
            return fail(job_cmd, out)

    for cmd in (
        "exit",
        f"scheduler schedule name {SCHEDULE_NAME}",
        f"job name {JOB_NAME}",
        f"time daily {sched['time']}",
        "exit",
        "end",
    ):
        ok, out = run_cmd(chan, cmd)
        if not ok:
            return fail(cmd, out)

    # 3. Verify what the switch actually stored before saving
    cmd = f"show scheduler job name {JOB_NAME}"
    ok, out = run_cmd(chan, cmd)
    job_out = _strip_echo(out, cmd)
    clear_count = job_out.lower().count(CLEAR_CMD)
    if not ok or clear_count != 1:
        print(f"{datetime.now()} ❌ {host}: job '{JOB_NAME}' check failed "
              f"(found '{CLEAR_CMD}' {clear_count} time(s), expected 1):\n{out.strip()}")
        return

    cmd = f"show scheduler schedule name {SCHEDULE_NAME}"
    ok, out = run_cmd(chan, cmd)
    sched_out = _strip_echo(out, cmd)
    if not ok or SCHEDULE_NAME not in sched_out or JOB_NAME not in sched_out.replace(SCHEDULE_NAME, ""):
        print(f"{datetime.now()} ❌ {host}: schedule '{SCHEDULE_NAME}' is missing or has no job attached:\n{out.strip()}")
        return

    # 4. Save config
    cmd = "copy running-config startup-config"
    ok, out = run_cmd(chan, cmd, timeout=90.0)
    if not ok or "copy complete" not in out.lower():
        print(f"{datetime.now()} ❌ {host}: '{cmd}' did not complete:\n{out.strip()}")
        return

    note = "" if with_history else " (no history file)"
    print(f"{datetime.now()} ✅ Scheduler '{SCHEDULE_NAME}' ({sched['time']} switch time) "
          f"configured, verified and saved on {host}{note}")


def _handle_sigterm(signum, frame):
    print(f"\n{datetime.now()} 🛑 Termination signal received. Closing active SSH sessions...")
    sys.exit(1)


signal.signal(signal.SIGINT, _handle_sigterm)
signal.signal(signal.SIGTERM, _handle_sigterm)


def main() -> None:
    verify_ssh_agent()

    try:
        jump_auth, switches, sched = load_config()
    except Exception as e:
        print(f"{datetime.now()} 📢 Configuration error: {type(e).__name__}: {e}")
        sys.exit(1)

    if not sched["aaa_password"]:
        print(f"{datetime.now()} ℹ️  No 'scheduler.aaa_password' set. If you log in via TACACS+/RADIUS, "
              "the job will fail at run time (check 'show scheduler logfile').")

    if not switches:
        print(f"{datetime.now()} ⚠️  No valid switches found. Check your CSV or YAML file. Exiting.")
        return

    try:
        with jump_session(jump_auth) as jump_client:
            for host, auth in switches:
                try:
                    with switch_shell(host, auth, jump_client) as chan:
                        clear_mac_table(chan, host, sched)
                except Exception as e:
                    print(f"{datetime.now()} 📢 {host} failed: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"{datetime.now()} 📢 Jump server failed: {type(e).__name__}: {e}")
        sys.exit(1)

    print(f"{datetime.now()} 🏁 All switches processed. All SSH sessions closed.")


if __name__ == "__main__":
    main()
