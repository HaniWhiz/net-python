#!/usr/bin/env python3
"""
clear_arp.py - daily ARP clear for Cisco Nexus 3000 (NX-OS) switches, over SSH with Netmiko.

For each switch in devices.csv, one at a time:
    connect -> count ARP entries -> clear ARP -> count again -> disconnect

Files (always next to this script):
    devices.csv                inventory, one column named "host"
    logs/clear_arp.log         rotating log (5 MB x 5 backups): timestamp, host, step, message
    arp_clear_results.csv      one row per device per LIVE run (this is the trend data)
    arp_clear_dryrun.csv       one row per device per --dry-run (kept apart so trends stay clean)

Exit code: 0 only if every device succeeded, otherwise 1 (Task Scheduler then shows 0x1).

Usage:
    python clear_arp.py                live run against every host in devices.csv
    python clear_arp.py --dry-run      connect and count only; the clear command is never sent
    python clear_arp.py --only HOST    limit the run to one host that is listed in devices.csv

Credentials come from the NET_USERNAME and NET_PASSWORD environment variables. They are never
written to the log or CSV; the password is also scrubbed from every log line as a safety net.

Requires: Python 3.10+ and Netmiko 4.x (the only third-party package).
"""

import argparse
import csv
import logging
import os
import platform
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from netmiko import (
    ConnectHandler,
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
    ReadTimeout,
)
from netmiko.base_connection import BaseConnection

# =============================================================================
# CONFIG - edit this section only.
# Anything in [square brackets] is unknown or unverified. The script refuses to
# run while a value it needs still contains a bracket.
# =============================================================================

# Paths are relative to this file, never to the working directory: Task
# Scheduler usually starts jobs in C:\Windows\System32.
SCRIPT_DIR: Path = Path(__file__).resolve().parent
INVENTORY_CSV: Path = SCRIPT_DIR / "devices.csv"
RESULTS_CSV: Path = SCRIPT_DIR / "arp_clear_results.csv"
DRY_RUN_CSV: Path = SCRIPT_DIR / "arp_clear_dryrun.csv"
LOG_FILE: Path = SCRIPT_DIR / "logs" / "clear_arp.log"
LOG_MAX_BYTES: int = 5 * 1024 * 1024  # 5 MB
LOG_BACKUPS: int = 5

# For the record only (written to the log, not used in any logic).
NXOS_VERSION: str = "[version]"

# Commands. Set each one EXACTLY as your switch accepts it (check with "?"),
# then remove the brackets. Candidates from Cisco documentation, NOT verified
# on your switches:
#   default VRF only : "show ip arp summary"          + "clear ip arp vrf default"
#   all VRFs         : "show ip arp summary vrf all"  + "clear ip arp vrf all"
# Plain "clear ip arp ..." clears with refresh. Adding " force-delete" removes
# entries without refresh; they are re-learned when traffic needs them.
SUMMARY_COMMAND: str = "[show ip arp summary]"
CLEAR_COMMAND: str = "[clear ip arp vrf default]"

# Count parsing. Cisco's documented "show ip arp summary" output has exactly one
# line like "  Total      : 6". Anything else is recorded as "unknown" and the
# raw output is logged.
TOTAL_REGEX: re.Pattern[str] = re.compile(r"^\s*Total\s*:\s*(\d+)\s*$", re.MULTILINE)
# Optional breakdown lines. Logged when present; not required, not in the CSV.
DETAIL_REGEX: re.Pattern[str] = re.compile(
    r"^\s*(Resolved|Incomplete|Unknown)\s*:\s*(\d+)", re.MULTILINE
)

# Confirmation prompts. Cisco's examples show "clear ip arp" going straight back
# to the prompt, so this is a safety net. [Prompt wording unverified.] If the
# last output line matches, CONFIRM_ANSWER is sent ONCE. A second prompt is not
# answered: it runs into the read timeout and the device is marked failed.
CONFIRM_REGEX: str = r"(?i:\[confirm\]|\(y/n\)|\[y/n\]|\(yes/no\)|\[yes/no\])"
CONFIRM_ANSWER: str = "y"

# Verification policy.
RECOUNT_DELAY_SEC: float = 0  # wait between the clear and the second count
REQUIRE_COUNT_DROP: bool = False  # True: "failed" if entries_after >= entries_before (>0)

# SSH and timeouts (seconds).
SSH_PORT: int = 22
CONN_TIMEOUT: int = 15  # TCP connect
AUTH_TIMEOUT: int = 20  # authentication reply
BANNER_TIMEOUT: int = 20  # SSH banner
SHOW_READ_TIMEOUT: float = 30  # max wait for the prompt after the summary command
CLEAR_READ_TIMEOUT: float = 60  # [tune] max wait for the prompt after the clear
STRICT_HOST_KEYS: bool = False  # True: only connect to hosts in ~/.ssh/known_hosts

# Pacing and retry.
PAUSE_BETWEEN_DEVICES_SEC: float = 5
RETRY_DELAY_SEC: float = 10
RETRY_ON_AUTH_FAILURE: bool = True  # False if repeated failed logins can lock the account

# Optional failure e-mail (standard library smtplib; unauthenticated relay assumed).
EMAIL_ON_FAILURE: bool = False
SMTP_HOST: str = "[smtp.example.local]"
SMTP_PORT: int = 25
SMTP_STARTTLS: bool = False
MAIL_FROM: str = "[nxos-arp-clear@example.local]"
MAIL_TO: list[str] = ["[netops@example.local]"]

# =============================================================================
# End of config
# =============================================================================

CSV_FIELDS: list[str] = ["timestamp", "host", "status", "entries_before", "entries_after", "error"]
UNKNOWN: str = "unknown"  # a count was attempted but no number could be read
LOGGER: logging.Logger = logging.getLogger("clear_arp")
_SECRETS: list[str] = []  # scrubbed from every log line and CSV cell


class StepError(Exception):
    """A step failed in a way this script detected itself (CLI error, unparsable output...)."""


@dataclass
class DeviceResult:
    """Outcome of one device in one run; becomes one CSV row."""

    host: str
    timestamp: str
    status: str = "failed"  # "ok" or "failed"
    entries_before: str = ""  # a number, "unknown", or "" when the step never ran
    entries_after: str = ""
    error: str = ""

    def as_row(self) -> dict[str, str]:
        return {
            "timestamp": self.timestamp,
            "host": self.host,
            "status": self.status,
            "entries_before": self.entries_before,
            "entries_after": self.entries_after,
            "error": one_line(self.error),
        }


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------
def scrub(text: str) -> str:
    """Replace credential strings with ********."""
    for secret in _SECRETS:
        text = text.replace(secret, "********")
    return text


def one_line(text: str, limit: int = 300) -> str:
    """Collapse newlines/whitespace and cap the length (for CSV cells)."""
    flat = " ".join(scrub(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def clip(text: str, limit: int = 2000) -> str:
    """Cap raw device output before it goes into the log."""
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def first_line(exc: BaseException) -> str:
    """Netmiko error messages are long; keep the first meaningful line."""
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    return lines[0] if lines else type(exc).__name__


def now_iso() -> str:
    """Local time with UTC offset, e.g. 2026-10-27T03:00:05+03:30."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def cli_errors(output: str) -> list[str]:
    """Netmiko does not raise on NX-OS CLI errors, so any line starting with '%' is one."""
    return [line.strip() for line in output.splitlines() if line.lstrip().startswith("%")]


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
class _ContextFilter(logging.Filter):
    """Makes sure every record has host/step fields and scrubs credentials from it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "host"):
            record.host = "-"
        if not hasattr(record, "step"):
            record.step = "-"
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not crash the run
            message = str(record.msg)
        record.msg, record.args = scrub(message), None
        return True


def setup_logging() -> None:
    """Rotating file log (always) plus console output (when a console exists)."""
    for handler in list(LOGGER.handlers):  # makes repeated calls safe
        LOGGER.removeHandler(handler)
        handler.close()
    LOGGER.filters.clear()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s host=%(host)s step=%(step)s %(message)s"
    )
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
    ]
    if sys.stdout is not None:  # pythonw.exe has no stdout
        handlers.append(logging.StreamHandler(sys.stdout))
    for handler in handlers:
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)
    LOGGER.addFilter(_ContextFilter())
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    # Keep library debug chatter out of the output.
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    logging.getLogger("netmiko").setLevel(logging.WARNING)


def log(level: int, host: str, step: str, msg: str, *args: object) -> None:
    """Log one line tagged with host and step."""
    LOGGER.log(level, msg, *args, extra={"host": host, "step": step})


# -----------------------------------------------------------------------------
# Config, inventory, CSV, e-mail
# -----------------------------------------------------------------------------
def _bracketed(value: str) -> bool:
    return "[" in value or "]" in value


def config_problems(dry_run: bool) -> list[str]:
    """Reasons the config is unsafe or incomplete. An empty list means OK to run."""
    problems: list[str] = []
    summary, clear = SUMMARY_COMMAND.strip(), CLEAR_COMMAND.strip()

    if _bracketed(summary):
        problems.append("SUMMARY_COMMAND still contains [brackets]; set the command you verified")
    elif not summary.startswith("show ") or any(ch in summary for ch in ";|"):
        problems.append("SUMMARY_COMMAND must be a single read-only 'show' command (no ';' or '|')")

    if _bracketed(clear):
        if not dry_run:  # a dry run never sends it, so a placeholder is acceptable there
            problems.append(
                "CLEAR_COMMAND still contains [brackets]; set it from 'clear ip arp ?' output"
            )
    elif (
        not re.match(r"clear ip arp(\s|$)", clear)
        or any(ch in clear for ch in ";|")
        or "statistics" in clear.split()
    ):
        problems.append(
            "CLEAR_COMMAND must be one 'clear ip arp ...' command (no ';', '|' or 'statistics')"
        )

    if not CONFIRM_ANSWER.strip():
        problems.append("CONFIRM_ANSWER must not be empty")
    if EMAIL_ON_FAILURE and any(_bracketed(v) for v in (SMTP_HOST, MAIL_FROM, *MAIL_TO)):
        problems.append("EMAIL_ON_FAILURE is on but the SMTP/mail settings still have [brackets]")
    return problems


def read_inventory(path: Path) -> list[str]:
    """Hosts from the 'host' column. Blank rows, '#' comments and duplicates are skipped.

    Raises ValueError for a missing 'host' column or a [placeholder] host.
    """
    with path.open(newline="", encoding="utf-8-sig") as fh:  # utf-8-sig: tolerate Excel's BOM
        reader = csv.DictReader(fh)
        column = next(
            (f for f in reader.fieldnames or [] if f and f.strip().lower() == "host"), None
        )
        if column is None:
            raise ValueError('the header row must contain a column named "host"')
        hosts: list[str] = []
        seen: set[str] = set()
        for row in reader:
            host = (row.get(column) or "").strip()
            if not host or host.startswith("#") or host.lower() in seen:
                continue
            if _bracketed(host):
                raise ValueError(f"placeholder host {host!r}: replace it with a real hostname or IP")
            seen.add(host.lower())
            hosts.append(host)
    return hosts


def append_result(path: Path, result: DeviceResult) -> bool:
    """Append one CSV row (header first if the file is new). False if the write failed."""
    try:
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(result.as_row())
        return True
    except OSError as exc:  # typically: the CSV is open in Excel, which locks it
        log(logging.ERROR, result.host, "csv", "cannot write %s: %s", path, exc)
        return False


def send_failure_email(failed: list[DeviceResult], total: int) -> None:
    """Optional notification. Errors are logged, never raised."""
    msg = EmailMessage()
    msg["Subject"] = f"NX-OS ARP clear: {len(failed)} of {total} device(s) failed"
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(MAIL_TO)
    lines = [f"{r.host}: {one_line(r.error)}" for r in failed]
    lines += ["", f"Runner: {platform.node()}", f"Log: {LOG_FILE}", f"CSV: {RESULTS_CSV}"]
    msg.set_content(scrub("\n".join(lines)))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            if SMTP_STARTTLS:
                smtp.starttls()
            smtp.send_message(msg)
        log(logging.INFO, "-", "notify", "failure e-mail sent to %s", ", ".join(MAIL_TO))
    except (OSError, smtplib.SMTPException) as exc:
        log(logging.ERROR, "-", "notify", "could not send failure e-mail: %s", exc)


# -----------------------------------------------------------------------------
# Device operations
# -----------------------------------------------------------------------------
def connect(host: str, username: str, password: str) -> BaseConnection:
    """Open the SSH session. On failure, wait RETRY_DELAY_SEC and try exactly once more."""
    params: dict[str, Any] = {
        "device_type": "cisco_nxos",
        "host": host,
        "username": username,
        "password": password,
        "port": SSH_PORT,
        "conn_timeout": CONN_TIMEOUT,
        "auth_timeout": AUTH_TIMEOUT,
        "banner_timeout": BANNER_TIMEOUT,
        "ssh_strict": STRICT_HOST_KEYS,
        "system_host_keys": STRICT_HOST_KEYS,
    }
    for attempt in (1, 2):
        last_try = attempt == 2
        try:
            conn = ConnectHandler(**params)
            log(logging.INFO, host, "connect", "connected (attempt %d)", attempt)
            return conn
        except NetmikoAuthenticationException as exc:
            log(logging.ERROR, host, "connect", "authentication failed (attempt %d): %s",
                attempt, first_line(exc))
            if last_try or not RETRY_ON_AUTH_FAILURE:
                raise
        except NetmikoTimeoutException as exc:
            log(logging.ERROR, host, "connect", "timeout/unreachable (attempt %d): %s",
                attempt, first_line(exc))
            if last_try:
                raise
        except Exception as exc:  # noqa: BLE001 - any other connect error also gets one retry
            log(logging.ERROR, host, "connect", "connect error (attempt %d): %s: %s",
                attempt, type(exc).__name__, first_line(exc))
            if last_try:
                raise
        log(logging.INFO, host, "connect", "retrying in %s s", RETRY_DELAY_SEC)
        time.sleep(RETRY_DELAY_SEC)
    raise RuntimeError("not reached")


def count_arp(conn: BaseConnection, prompt_re: str, host: str, step: str) -> int | None:
    """Send SUMMARY_COMMAND and return its Total, or None when it can't be parsed.

    Raises StepError when the switch rejects the command (a line starting with '%').
    """
    log(logging.INFO, host, step, "sending: %s", SUMMARY_COMMAND)
    output = str(
        conn.send_command(SUMMARY_COMMAND, expect_string=prompt_re, read_timeout=SHOW_READ_TIMEOUT)
    )
    errors = cli_errors(output)
    if errors:
        log(logging.ERROR, host, step, "raw output: %r", clip(output))
        raise StepError(f"'{SUMMARY_COMMAND}' rejected: {errors[0]}")

    totals = TOTAL_REGEX.findall(output)
    if len(totals) != 1:
        log(logging.WARNING, host, step,
            "expected exactly one 'Total : N' line, found %d; raw output: %r",
            len(totals), clip(output))
        return None
    total = int(totals[0])
    details = " ".join(f"{name}={value}" for name, value in DETAIL_REGEX.findall(output))
    log(logging.INFO, host, step, "ARP entries: Total=%d %s", total, details)
    return total


def run_clear(conn: BaseConnection, prompt: str, host: str) -> None:
    """Send CLEAR_COMMAND and answer at most one confirmation prompt.

    Raises StepError when the switch rejects the command (a line starting with '%').
    """
    step = "clear"
    prompt_re = re.escape(prompt)
    log(logging.INFO, host, step, "sending: %s", CLEAR_COMMAND)
    output = str(
        conn.send_command(
            CLEAR_COMMAND,
            expect_string=f"{prompt_re}|{CONFIRM_REGEX}",  # stop at the prompt OR a question
            read_timeout=CLEAR_READ_TIMEOUT,
            strip_prompt=False,  # keep the last line so we can see what ended the read
        )
    )
    lines = output.rstrip().splitlines()
    last_line = lines[-1].strip() if lines else ""
    if not last_line.endswith(prompt) and re.search(CONFIRM_REGEX, last_line):
        log(logging.WARNING, host, step, "switch asked for confirmation: %r; answering %r",
            last_line, CONFIRM_ANSWER)
        output += str(
            conn.send_command(
                CONFIRM_ANSWER,
                expect_string=prompt_re,  # only the prompt ends this read
                read_timeout=CLEAR_READ_TIMEOUT,
                strip_prompt=False,
                cmd_verify=False,  # the switch may not echo the answer
            )
        )
    errors = cli_errors(output)
    if errors:
        log(logging.ERROR, host, step, "raw output: %r", clip(output))
        raise StepError(f"'{CLEAR_COMMAND}' rejected: {errors[0]}")
    log(logging.INFO, host, step, "accepted (no output line starts with '%')")


def abort_pending(conn: BaseConnection, host: str, step: str) -> None:
    """After a read timeout, send Ctrl-C (best effort) to cancel a stuck command or prompt."""
    try:
        conn.write_channel("\x03")
        time.sleep(1)
        conn.read_channel()
    except Exception as exc:  # noqa: BLE001
        log(logging.WARNING, host, step, "could not send Ctrl-C: %s", first_line(exc))


def disconnect(conn: BaseConnection, host: str) -> None:
    """Netmiko sends 'exit' and closes the SSH transport. Errors here are only logged."""
    try:
        conn.disconnect()
        log(logging.INFO, host, "disconnect", "disconnected")
    except Exception as exc:  # noqa: BLE001
        log(logging.WARNING, host, "disconnect", "error while disconnecting (ignored): %s",
            first_line(exc))


def process_device(host: str, username: str, password: str, dry_run: bool) -> DeviceResult:
    """Run every step for one switch. Never raises: every error ends up in the result."""
    result = DeviceResult(host=host, timestamp=now_iso())
    conn: BaseConnection | None = None
    step = "connect"
    try:
        conn = connect(host, username, password)

        step = "prompt"
        prompt = conn.find_prompt()  # e.g. "N3K-01#"; marks the end of each command's output
        prompt_re = re.escape(prompt)

        step = "count-before"
        result.entries_before = UNKNOWN  # attempted; replaced by the number on success
        before = count_arp(conn, prompt_re, host, step)
        if before is None:
            raise StepError("could not read the ARP count, so the clear was skipped")
        result.entries_before = str(before)

        if dry_run:
            step = "dry-run"
            log(logging.INFO, host, step, "DRY RUN: would send '%s', then '%s' again",
                CLEAR_COMMAND, SUMMARY_COMMAND)
            result.status = "ok"
        else:
            step = "clear"
            run_clear(conn, prompt, host)
            if RECOUNT_DELAY_SEC > 0:
                time.sleep(RECOUNT_DELAY_SEC)

            step = "count-after"
            result.entries_after = UNKNOWN
            after = count_arp(conn, prompt_re, host, step)
            if after is None:
                raise StepError("the clear was sent, but the ARP count afterwards could not be read")
            result.entries_after = str(after)

            step = "verify"
            log(logging.INFO, host, step, "before=%d after=%d change=%+d",
                before, after, after - before)
            if before > 0 and after >= before:
                note = f"ARP count did not drop (before={before}, after={after})"
                if REQUIRE_COUNT_DROP:
                    raise StepError(note)
                log(logging.WARNING, host, step,
                    "%s; expected with a refresh-type clear or fast re-learning", note)
            result.status = "ok"

    except NetmikoAuthenticationException:
        result.error = f"{step}: authentication failed"
    except NetmikoTimeoutException as exc:
        result.error = f"{step}: timeout or host unreachable ({first_line(exc)})"
    except ReadTimeout:
        result.error = f"{step}: no prompt from the switch within the read timeout"
        if step == "clear":
            result.error += " (the clear may or may not have run)"
        if conn is not None:
            abort_pending(conn, host, step)
    except StepError as exc:
        result.error = f"{step}: {exc}"
    except Exception as exc:  # noqa: BLE001 - one bad device must never stop the run
        result.error = f"{step}: unexpected {type(exc).__name__}: {first_line(exc)}"
        LOGGER.error("unexpected error", exc_info=True, extra={"host": host, "step": step})
    finally:
        if conn is not None:
            disconnect(conn, host)

    if result.status == "ok":
        log(logging.INFO, host, "result", "ok (before=%s after=%s)",
            result.entries_before, result.entries_after or "-")
    else:
        log(logging.ERROR, host, "result", "FAILED: %s", result.error)
    return result


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clear dynamic ARP entries on the NX-OS switches listed in devices.csv."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="connect and count only; never send the clear command")
    parser.add_argument("--only", metavar="HOST",
                        help="run against this one host (must be listed in devices.csv)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse uses exit code 2; keep the 0/1 contract
        return 0 if exc.code in (0, None) else 1

    setup_logging()
    mode = "DRY-RUN" if args.dry_run else "LIVE"
    log(logging.INFO, "-", "start", "run started: mode=%s inventory=%s nxos=%s",
        mode, INVENTORY_CSV, NXOS_VERSION)

    problems = config_problems(args.dry_run)
    for problem in problems:
        log(logging.ERROR, "-", "config", problem)
    if problems:
        return 1
    if args.dry_run and _bracketed(CLEAR_COMMAND):
        log(logging.WARNING, "-", "config", "CLEAR_COMMAND is still a placeholder (OK for a dry run)")

    username = os.environ.get("NET_USERNAME", "")
    password = os.environ.get("NET_PASSWORD", "")
    if not username or not password:
        log(logging.ERROR, "-", "config", "NET_USERNAME and NET_PASSWORD must both be set")
        return 1
    _SECRETS[:] = [password]

    try:
        hosts = read_inventory(INVENTORY_CSV)
    except (OSError, ValueError, csv.Error) as exc:
        log(logging.ERROR, "-", "inventory", "cannot read %s: %s", INVENTORY_CSV, exc)
        return 1
    if args.only:
        wanted = args.only.strip().lower()
        hosts = [h for h in hosts if h.lower() == wanted]
        if not hosts:
            log(logging.ERROR, "-", "inventory", "--only %s: host is not listed in %s",
                args.only, INVENTORY_CSV.name)
            return 1
    if not hosts:
        log(logging.ERROR, "-", "inventory", "no hosts found in %s", INVENTORY_CSV)
        return 1

    results_csv = DRY_RUN_CSV if args.dry_run else RESULTS_CSV
    results: list[DeviceResult] = []
    csv_ok = True
    for index, host in enumerate(hosts):
        if index:
            time.sleep(PAUSE_BETWEEN_DEVICES_SEC)
        result = process_device(host, username, password, args.dry_run)
        results.append(result)
        csv_ok = append_result(results_csv, result) and csv_ok

    failed = [r for r in results if r.status != "ok"]
    log(logging.ERROR if failed else logging.INFO, "-", "summary",
        "%d device(s): %d ok, %d failed%s", len(results), len(results) - len(failed),
        len(failed), f" ({', '.join(r.host for r in failed)})" if failed else "")
    if failed and EMAIL_ON_FAILURE and not args.dry_run:
        send_failure_email(failed, len(results))

    exit_code = 0 if not failed and csv_ok else 1
    log(logging.INFO, "-", "end", "exit code %d", exit_code)
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - last resort: record the crash, keep exit code 1
        LOGGER.error("unhandled error", exc_info=True, extra={"host": "-", "step": "crash"})
        sys.exit(1)
