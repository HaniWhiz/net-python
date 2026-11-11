"""
net-deploy.py - general purpose command deployer for Cisco switches (NX-OS / IOS).

Nothing device-specific lives in this file. The login settings, jump server,
input CSV file and the commands to deploy are all read from a YAML job file.

    python net-deploy.py                      # uses net-deploy.yaml next to this script
    python net-deploy.py -c vlan-job.yaml     # any other job file
    python net-deploy.py -c vlan-job.yaml --dry-run
"""
import argparse
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

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_JOB_FILE = BASE_DIR / "net-deploy.yaml"

# A question from the switch that waits for an answer instead of returning to the prompt
QUESTION_RE = re.compile(
    r"(\[confirm\]|\[y/n\]|\(y/n\)|\[yes/no\]|\(yes/no\)|destination filename.*\?).{0,10}$",
    re.IGNORECASE,
)
YES_NO_RE = re.compile(r"(\[y/n\]|\(y/n\)|\[yes/no\]|\(yes/no\))", re.IGNORECASE)
# Syslog lines such as '%SYS-5-CONFIG_I: ...' start with '%' but are not CLI errors
SYSLOG_RE = re.compile(r"^%[A-Z0-9_]+-\d-")


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------------------
# Job file
# ---------------------------------------------------------------------------
def _resolve(path_str: str, job_dir: Path) -> Path:
    """Relative paths in the YAML are relative to the YAML file itself."""
    p = Path(os.path.expanduser(str(path_str)))
    return p if p.is_absolute() else job_dir / p


def _read_csv_hosts(csv_path: Path) -> list[str]:
    """Return the first column of every row. Delimiter may be ',', ';' or TAB."""
    with open(csv_path, mode="r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        return [row[0].strip() for row in csv.reader(f, dialect) if row and row[0].strip()]


def _parse_commands(raw_commands, default_timeout: float) -> list[dict]:
    commands: list[dict] = []
    for i, item in enumerate(raw_commands or [], start=1):
        if isinstance(item, str):
            item = {"cmd": item}
        if not isinstance(item, dict) or not str(item.get("cmd") or "").strip():
            raise ValueError(f"commands[{i}] must be a text line or a block with 'cmd:'")
        unknown = set(item) - {"cmd", "ignore_errors", "timeout", "expect", "answer", "show", "secret"}
        if unknown:
            raise ValueError(f"commands[{i}] has unknown option(s): {', '.join(sorted(unknown))}")
        expect = item.get("expect") or []
        if isinstance(expect, str):
            expect = [expect]
        commands.append({
            "cmd": str(item["cmd"]).strip(),
            "ignore_errors": bool(item.get("ignore_errors", False)),
            "timeout": float(item.get("timeout") or default_timeout),
            "expect": [str(e) for e in expect],
            "answer": None if item.get("answer") is None else str(item["answer"]),
            "show": bool(item.get("show", False)),
            "secret": bool(item.get("secret", False)),
        })
    if not commands:
        raise ValueError("No commands found. Add them under 'commands:' in the job file.")
    return commands


def load_job(job_file: Path) -> dict:
    """Read one YAML job file and return everything the run needs."""
    if not job_file.exists():
        raise FileNotFoundError(f"Job file not found: {job_file}")
    with open(job_file, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    job_dir = job_file.resolve().parent

    defaults = config.get("auth") or {}
    if not defaults.get("username"):
        raise ValueError("Missing 'username' under the 'auth' section.")

    raw_settings = config.get("settings") or {}
    settings = {
        "command_timeout": float(raw_settings.get("command_timeout") or 15),
        "stop_on_error": bool(raw_settings.get("stop_on_error", True)),
        "disable_paging": bool(raw_settings.get("disable_paging", True)),
        "log_dir": str(raw_settings.get("log_dir") or ""),
    }
    if settings["log_dir"]:
        settings["log_dir"] = str(_resolve(settings["log_dir"], job_dir))

    # Hosts: CSV file (first column) and/or the inline 'hosts' list
    raw_hosts: list[tuple[str, dict]] = []
    hosts_csv = config.get("hosts_csv")
    if hosts_csv:
        csv_path = _resolve(hosts_csv, job_dir)
        if not csv_path.exists():
            raise FileNotFoundError(f"hosts_csv file not found: {csv_path}")
        raw_hosts += [(h, {}) for h in _read_csv_hosts(csv_path)]
    for entry in config.get("hosts") or []:
        if isinstance(entry, dict) and "host" in entry:
            raw_hosts.append((str(entry["host"]).strip(), {k: v for k, v in entry.items() if k != "host"}))
        elif entry is not None:
            raw_hosts.append((str(entry).strip(), {}))

    ignored_words = ("ip", "address", "hostname", "host", "switch", "device", "ip address")
    switches: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for raw, overrides in raw_hosts:
        if not raw or raw.startswith("#"):
            continue
        host = raw.split("/")[0].strip()          # strip CIDR mask such as /24
        if host.lower() in ignored_words or host in seen:
            continue
        seen.add(host)
        switches.append((host, {**defaults, **overrides}))

    return {
        "name": str(config.get("name") or job_file.stem),
        "jump": config.get("jump"),
        "switches": switches,
        "settings": settings,
        "commands": _parse_commands(config.get("commands"), settings["command_timeout"]),
    }


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------
def verify_ssh_agent() -> None:
    """Report whether ssh-agent has keys loaded (e.g. a YubiKey)."""
    agent = paramiko.Agent()
    try:
        keys = agent.get_keys()
        if not keys:
            log("⚠️  No keys found in SSH agent (check 'ssh-add -L'). Key files / password from the YAML will be used.")
        else:
            log(f"🔐 SSH agent active with {len(keys)} key(s)")
    finally:
        agent.close()


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


def read_until_prompt(chan: Channel, timeout: float = 15.0) -> str:
    """
    Read until the switch is waiting for input: a '#' or '>' prompt, or a question
    such as '[confirm]' / '(y/n)'. Returns whatever was received when the timeout expires.
    """
    output = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if chan.recv_ready():
            output += chan.recv(65535).decode(errors="ignore")
            stripped = output.strip()
            if not stripped:
                continue
            last_line = stripped.splitlines()[-1].strip()

            # Ignore the '[####   ] 45%' progress bar of 'copy running-config startup-config'
            is_progress_bar = last_line.startswith("[") and "#" in last_line

            if not is_progress_bar and (last_line.endswith(("#", ">")) or QUESTION_RE.search(last_line)):
                time.sleep(0.2)
                if not chan.recv_ready():
                    break
        else:
            time.sleep(0.1)
    return output


@contextmanager
def jump_session(jump_auth: dict | None) -> Iterator[paramiko.SSHClient | None]:
    """Context manager for the jump host. Guarantees logout when done."""
    if not jump_auth:
        yield None
        return

    host = jump_auth["host"]
    log(f"🔑 Logging in to jump server {host}")
    jump_client = open_ssh_client(host, jump_auth)
    try:
        yield jump_client
    finally:
        try:
            jump_client.close()
        except Exception:
            pass
        log(f"🔒 Logged out of jump server {host}")


@contextmanager
def switch_shell(host: str, auth: dict, jump_client: paramiko.SSHClient | None) -> Iterator[Channel]:
    """Context manager for a single switch SSH session with strict cleanup."""
    sock = None
    client: paramiko.SSHClient | None = None
    chan: Channel | None = None

    log(f"🔑 Logging in to {host}")
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
        initial_output = read_until_prompt(chan, timeout=12.0)
        if not initial_output.strip().endswith(("#", ">")):
            chan.send("\n")
            read_until_prompt(chan, timeout=5.0)

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

        log(f"🔒 Logged out of {host}")


# ---------------------------------------------------------------------------
# Running commands
# ---------------------------------------------------------------------------
def has_cli_error(output: str) -> bool:
    """Return True if the output contains a Cisco CLI error message."""
    for line in output.splitlines():
        text = line.strip()
        low = text.lower()
        if text.startswith("%") and not SYSLOG_RE.match(text):
            return True
        if (
            low.startswith("error:")
            or "invalid command" in low
            or "invalid input" in low
            or "syntax error" in low
            or "incomplete command" in low
            or "ambiguous command" in low
        ):
            return True
    return False


def run_command(chan: Channel, command: dict) -> tuple[bool, str, str]:
    """
    Send one command and wait for the prompt.
    Returns (ok, output, reason). 'reason' explains a failure in plain words.
    """
    cmd, timeout = command["cmd"], command["timeout"]
    chan.send(cmd + "\n")
    output = read_until_prompt(chan, timeout=timeout)
    reason = ""

    # Answer up to 3 questions ('[confirm]', '(y/n)', 'Destination filename ...?')
    for _ in range(3):
        stripped = output.strip()
        last_line = stripped.splitlines()[-1] if stripped else ""
        if not QUESTION_RE.search(last_line):
            break
        if command["answer"] is not None:
            reply = command["answer"]
        elif YES_NO_RE.search(last_line):
            # Never guess 'yes' on the user's behalf
            reply = "n"
            reason = "switch asked a yes/no question and the command has no 'answer:' (replied 'n')"
        else:
            reply = ""                                  # plain ENTER for [confirm] / filename
        chan.send(reply + "\n")
        output += read_until_prompt(chan, timeout=timeout)

    if command["secret"]:
        output = output.replace(cmd, "******")

    if not reason:
        if not output.strip().endswith(("#", ">")):
            reason = f"no prompt came back within {timeout:g}s"
        elif has_cli_error(output):
            reason = "switch returned an error"
        else:
            body = "\n".join(output.splitlines()[1:])   # skip the echoed command line
            missing = [e for e in command["expect"] if e.lower() not in body.lower()]
            if missing:
                reason = "expected text not found in output: " + ", ".join(repr(m) for m in missing)

    return (not reason), output, reason


def deploy_to_switch(chan: Channel, host: str, job: dict, session_log: list[str]) -> tuple[bool, str]:
    """Run every command of the job on one switch. Returns (ok, short failure text)."""
    settings = job["settings"]

    # Flush whatever is left from the login banner
    time.sleep(0.3)
    while chan.recv_ready():
        chan.recv(65535)

    if settings["disable_paging"]:
        _, out, _ = run_command(chan, {"cmd": "terminal length 0", "timeout": 10.0, "expect": [],
                                       "answer": None, "secret": False})
        session_log.append(out)

    failures: list[str] = []
    total = len(job["commands"])
    for n, command in enumerate(job["commands"], start=1):
        shown = "****** (secret)" if command["secret"] else command["cmd"]
        ok, output, reason = run_command(chan, command)
        session_log.append(output)

        if ok:
            log(f"   ✅ [{n}/{total}] {shown}")
            if command["show"]:
                print(output.strip() + "\n", flush=True)
            continue

        if command["ignore_errors"]:
            log(f"   ➖ [{n}/{total}] {shown}  (ignored: {reason})")
            continue

        log(f"   ❌ [{n}/{total}] {shown}  ({reason})")
        print(output.strip() + "\n", flush=True)
        failures.append(shown)
        if settings["stop_on_error"]:
            chan.send("end\n")                           # leave config mode before logging out
            session_log.append(read_until_prompt(chan, timeout=5.0))
            return False, f"stopped at command {n}/{total}: {shown}"

    if failures:
        return False, f"{len(failures)} command(s) failed: " + "; ".join(failures)
    return True, ""


def write_session_log(log_dir: str, job_name: str, host: str, session_log: list[str]) -> None:
    if not log_dir:
        return
    folder = Path(log_dir)
    folder.mkdir(parents=True, exist_ok=True)
    safe_host = re.sub(r"[^A-Za-z0-9_.-]", "_", host)
    path = folder / f"{job_name}_{safe_host}_{datetime.now():%Y%m%d-%H%M%S}.log"
    path.write_text("".join(session_log).replace("\r\n", "\n"), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _handle_signal(signum, frame):
    print()
    log("🛑 Stop requested. Closing SSH sessions...")
    sys.exit(130)


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy the commands of a YAML job file to Cisco switches.")
    parser.add_argument("-c", "--config", default=str(DEFAULT_JOB_FILE),
                        help="YAML job file (default: net-deploy.yaml next to this script)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the switches and commands, connect to nothing")
    args = parser.parse_args()

    try:
        job = load_job(Path(args.config))
    except Exception as e:
        log(f"📢 Job file error: {type(e).__name__}: {e}")
        return 2

    switches = job["switches"]
    if not switches:
        log("⚠️  No switches found. Set 'hosts_csv' or 'hosts' in the job file.")
        return 2

    log(f"📋 Job '{job['name']}': {len(job['commands'])} command(s) for {len(switches)} switch(es)")

    if args.dry_run:
        print("\nSwitches:")
        for host, auth in switches:
            print(f"  - {host}  (user {auth['username']})")
        print("\nCommands:")
        for n, c in enumerate(job["commands"], start=1):
            opts = [k for k in ("ignore_errors", "show", "secret") if c[k]]
            if c["expect"]:
                opts.append(f"expect={c['expect']}")
            if c["answer"] is not None:
                opts.append(f"answer={c['answer']!r}")
            shown = "****** (secret)" if c["secret"] else c["cmd"]
            print(f"  {n:>2}. {shown}" + (f"    [{', '.join(opts)}]" if opts else ""))
        print("\nDry run only. Nothing was sent.")
        return 0

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    verify_ssh_agent()

    results: list[tuple[str, bool, str]] = []
    try:
        with jump_session(job["jump"]) as jump_client:
            for host, auth in switches:
                session_log: list[str] = []
                try:
                    with switch_shell(host, auth, jump_client) as chan:
                        ok, detail = deploy_to_switch(chan, host, job, session_log)
                except Exception as e:
                    ok, detail = False, f"{type(e).__name__}: {e}"
                    log(f"📢 {host} failed: {detail}")
                results.append((host, ok, detail))
                try:
                    write_session_log(job["settings"]["log_dir"], job["name"], host, session_log)
                except OSError as e:
                    log(f"⚠️  Could not write log for {host}: {e}")
    except Exception as e:
        log(f"📢 Jump server failed: {type(e).__name__}: {e}")
        return 1

    failed = [r for r in results if not r[1]]
    print()
    log(f"🏁 Finished: {len(results) - len(failed)} OK, {len(failed)} FAILED")
    for host, _, detail in failed:
        print(f"   ❌ {host}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
