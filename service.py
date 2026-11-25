"""Windows service wrapper and installer for the antigravity-interface server.

The service listens on http://127.0.0.1:8790/v1 and must run as the Windows
account that signed in to agy: the OAuth token lives in that account's
Credential Manager entry ("gemini:antigravity"), which a SYSTEM service cannot
read. Install from an elevated shell:

    .venv\\Scripts\\python.exe service.py install
    .venv\\Scripts\\python.exe service.py start|stop|restart|status|uninstall
    .venv\\Scripts\\python.exe service.py run        (foreground, for debugging)

The installer prompts for the account password (Windows services cannot log on
interactively) and grants "Log on as a service". The account, its profile paths
and the proxy variables are captured into service.env.json so the service can
reach Google and agy's credential store even though it starts without a user
session.

When the Service Control Manager launches this file with no arguments, it hosts
AgiServerService through the pywin32 dispatcher.
"""
from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import socket
import sys
import threading
import time
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "service.env.json")
LOG_DIR = os.path.join(ROOT, "logs")
LOG_FILE = os.path.join(LOG_DIR, "agi-server.log")

SERVICE_NAME = "AntigravityOpenAIServer"
DISPLAY_NAME = "Antigravity OpenAI Server (8790)"
DESCRIPTION = "OpenAI-compatible Antigravity (agy) server on http://127.0.0.1:8790/v1"
HOST = "127.0.0.1"
PORT = 8790

# Variables the service process needs but does not inherit from a user session.
CAPTURED_ENV = (
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "HOME",
    "TEMP", "TMP",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


def find_agy() -> str:
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "agy", "bin", "agy.exe"))
    profile = os.environ.get("USERPROFILE")
    if profile:
        candidates.append(os.path.join(profile, "AppData", "Local", "agy", "bin", "agy.exe"))
    for path in candidates:
        if os.path.exists(path):
            return path
    return "agy"


def proxy_from_registry() -> str:
    """Fallback for when the installer runs without the user's env vars."""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return ""
    if not enabled or not server:
        return ""
    return server if "://" in server else "http://" + server


def load_environment() -> None:
    """Apply install-time variables, then the AGI_* settings."""
    saved: dict[str, str] = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            saved = {}
    for key in CAPTURED_ENV:
        value = saved.get(key)
        if value:
            os.environ[key] = value

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        proxy = proxy_from_registry()
    if proxy:
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            os.environ.setdefault(key, proxy)
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1,::1")

    os.environ["AGI_HOST"] = HOST
    os.environ["AGI_PORT"] = str(PORT)
    os.environ.setdefault("AGI_DEFAULT_MODEL", "Gemini 3.8 Flash (Medium)")
    # Managed agents (agy --agent) hit upstream HTTP 429 on some accounts; the
    # prompt-preamble fallback works there.
    os.environ["AGI_SYSTEM_PROMPT_VIA_AGENT"] = "false"
    agy = saved.get("agy_bin") or find_agy()
    os.environ["AGI_AGY_BIN"] = agy

    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)


# ── service body ────────────────────────────────────────────────────────────

import servicemanager  # noqa: E402
import win32event  # noqa: E402
import win32service  # noqa: E402
import win32serviceutil  # noqa: E402


class AgiServerService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = DISPLAY_NAME
    _svc_description_ = DESCRIPTION

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.server = None
        self.thread = None

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self.server is not None:
            self.server.should_exit = True
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self) -> None:
        servicemanager.LogInfoMsg(f"{SERVICE_NAME} starting on {HOST}:{PORT}")
        try:
            load_environment()
            os.makedirs(LOG_DIR, exist_ok=True)
            log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
            sys.stdout = log
            sys.stderr = log
            import uvicorn

            config = uvicorn.Config(
                "app.main:app", host=HOST, port=PORT, log_level="info"
            )
            self.server = uvicorn.Server(config)
            self.thread = threading.Thread(target=self.server.run, name="uvicorn", daemon=True)
            self.thread.start()
            while True:
                rc = win32event.WaitForSingleObject(self.stop_event, 1000)
                if rc == win32event.WAIT_OBJECT_0:
                    break
                if not self.thread.is_alive():
                    servicemanager.LogErrorMsg(
                        f"{SERVICE_NAME}: server exited unexpectedly; see {LOG_FILE}"
                    )
                    raise RuntimeError("server thread exited unexpectedly")
            self.server.should_exit = True
            self.thread.join(timeout=20)
            servicemanager.LogInfoMsg(f"{SERVICE_NAME} stopped")
        except Exception:
            servicemanager.LogErrorMsg(f"{SERVICE_NAME} failed:\n{traceback.format_exc()}")
            raise


# ── installer / control ─────────────────────────────────────────────────────

def require_admin() -> None:
    if not ctypes.windll.shell32.IsUserAnAdmin():
        print(
            "This command needs an elevated shell: right-click Terminal/PowerShell\n"
            'and choose "Run as administrator", then re-run it.',
            file=sys.stderr,
        )
        raise SystemExit(1)


def grant_logon_as_service(account: str) -> None:
    try:
        import win32security

        policy = win32security.LsaOpenPolicy(None, win32security.POLICY_ALL_ACCESS)
        try:
            sid, _, _ = win32security.LookupAccountName(None, account)
            win32security.LsaAddAccountRights(policy, sid, ("SeServiceLogonRight",))
        finally:
            win32security.LsaClose(policy)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not grant SeServiceLogonRight to {account}: {exc}", file=sys.stderr)


def port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, PORT)) == 0


def wait_healthy(timeout: float = 45.0) -> bool:
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    url = f"http://{HOST}:{PORT}/healthz"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    return False


def tail_log(lines: int = 20) -> str:
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])
    except OSError:
        return "(no log yet)"


def _open_scm(access: int):
    return win32service.OpenSCManager(None, None, access)


def _stop_if_running(scm) -> None:
    try:
        handle = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
    except win32service.error:
        return
    try:
        try:
            win32service.ControlService(handle, win32service.SERVICE_CONTROL_STOP)
        except win32service.error:
            pass
        for _ in range(30):
            if win32service.QueryServiceStatus(handle)[1] == win32service.SERVICE_STOPPED:
                break
            time.sleep(0.5)
    finally:
        win32service.CloseServiceHandle(handle)


def _delete_service() -> None:
    scm = _open_scm(win32service.SC_MANAGER_ALL_ACCESS)
    try:
        _stop_if_running(scm)
        try:
            handle = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
        except win32service.error:
            return
        try:
            win32service.DeleteService(handle)
        finally:
            win32service.CloseServiceHandle(handle)
        # The service object disappears once SCM drops its references.
        for _ in range(60):
            try:
                probe = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_QUERY_STATUS)
            except win32service.error:
                return
            win32service.CloseServiceHandle(probe)
            time.sleep(0.5)
    finally:
        win32service.CloseServiceHandle(scm)


def cmd_install(args: argparse.Namespace) -> None:
    require_admin()
    if port_in_use():
        print(
            f"Port {PORT} is already in use. Stop the running server first, then re-run install.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    account = args.account or f"{os.environ.get('COMPUTERNAME', '.')}\\{os.environ.get('USERNAME', '')}"
    password = args.password or getpass.getpass(f"Windows password for {account}: ")
    if not password:
        print("A password is required: Windows services cannot log on interactively.", file=sys.stderr)
        raise SystemExit(1)

    os.makedirs(LOG_DIR, exist_ok=True)
    saved = {key: os.environ.get(key, "") for key in CAPTURED_ENV}
    saved["account"] = account
    saved["agy_bin"] = find_agy()
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(saved, fh, indent=2)

    _delete_service()
    grant_logon_as_service(account)

    binary_path = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
    scm = _open_scm(win32service.SC_MANAGER_ALL_ACCESS)
    try:
        handle = win32service.CreateService(
            scm,
            SERVICE_NAME,
            DISPLAY_NAME,
            win32service.SERVICE_ALL_ACCESS,
            win32service.SERVICE_WIN32_OWN_PROCESS,
            win32service.SERVICE_AUTO_START,
            win32service.SERVICE_ERROR_NORMAL,
            binary_path,
            None,
            0,
            None,
            account,
            password,
        )
        try:
            win32service.ChangeServiceConfig2(
                handle, win32service.SERVICE_CONFIG_DESCRIPTION, DESCRIPTION
            )
        finally:
            win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(scm)

    print(f"Installed service {SERVICE_NAME} ({DISPLAY_NAME}), running as {account}.")
    _start_and_report()


def _start_and_report() -> None:
    scm = _open_scm(win32service.SC_MANAGER_ALL_ACCESS)
    try:
        handle = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
        try:
            win32service.StartService(handle, None)
        finally:
            win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(scm)

    print("Waiting for the HTTP endpoint...")
    if wait_healthy():
        print(f"Running: http://{HOST}:{PORT}/v1   (log: {LOG_FILE})")
        print('Manage it from services.msc under display name "%s".' % DISPLAY_NAME)
    else:
        print("Service did not answer /healthz in time. Last log lines:", file=sys.stderr)
        print(tail_log(), file=sys.stderr)
        print("Check services.msc for the exit code (a wrong password shows as 1069).", file=sys.stderr)
        raise SystemExit(1)


def cmd_start(_: argparse.Namespace) -> None:
    require_admin()
    _start_and_report()


def cmd_stop(_: argparse.Namespace) -> None:
    require_admin()
    scm = _open_scm(win32service.SC_MANAGER_ALL_ACCESS)
    try:
        handle = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
        try:
            win32service.ControlService(handle, win32service.SERVICE_CONTROL_STOP)
        finally:
            win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(scm)
    print("Stop requested.")


def cmd_restart(args: argparse.Namespace) -> None:
    cmd_stop(args)
    time.sleep(1)
    _start_and_report()


def cmd_status(_: argparse.Namespace) -> None:
    names = {
        win32service.SERVICE_STOPPED: "stopped",
        win32service.SERVICE_START_PENDING: "start pending",
        win32service.SERVICE_STOP_PENDING: "stop pending",
        win32service.SERVICE_RUNNING: "running",
        win32service.SERVICE_CONTINUE_PENDING: "continue pending",
        win32service.SERVICE_PAUSE_PENDING: "pause pending",
        win32service.SERVICE_PAUSED: "paused",
    }
    scm = _open_scm(win32service.SC_MANAGER_CONNECT)
    try:
        handle = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_QUERY_STATUS)
        try:
            state = win32service.QueryServiceStatus(handle)[1]
        finally:
            win32service.CloseServiceHandle(handle)
    except win32service.error:
        print(f"Service {SERVICE_NAME} is not installed.")
        return
    finally:
        win32service.CloseServiceHandle(scm)
    print(f"{DISPLAY_NAME}: {names.get(state, state)}")


def cmd_uninstall(_: argparse.Namespace) -> None:
    require_admin()
    _delete_service()
    print(f"Removed service {SERVICE_NAME}. service.env.json is kept.")


def cmd_run(_: argparse.Namespace) -> None:
    load_environment()
    os.makedirs(LOG_DIR, exist_ok=True)
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="create the service and start it")
    install.add_argument("--account", help="Windows account to run as (default: current user)")
    install.add_argument("--password", help="account password (prompted when omitted)")
    install.set_defaults(func=cmd_install)

    sub.add_parser("uninstall", help="stop and delete the service").set_defaults(func=cmd_uninstall)
    sub.add_parser("start", help="start the service").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop the service").set_defaults(func=cmd_stop)
    sub.add_parser("restart", help="restart the service").set_defaults(func=cmd_restart)
    sub.add_parser("status", help="print the service state").set_defaults(func=cmd_status)
    sub.add_parser("run", help="run the server in the foreground").set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # Launched by the Service Control Manager.
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(AgiServerService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        raise SystemExit(main(sys.argv[1:]))