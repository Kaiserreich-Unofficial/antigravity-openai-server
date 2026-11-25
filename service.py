"""Windows service wrapper and installer for the antigravity-interface server.

The service listens on http://127.0.0.1:8790/v1. Install it from an elevated
shell:

    .venv\\Scripts\\python.exe service.py install          # service runs as SYSTEM
    .venv\\Scripts\\python.exe service.py install --account .\\Administrator
    .venv\\Scripts\\python.exe service.py start|stop|restart|status|uninstall
    .venv\\Scripts\\python.exe service.py run              # foreground debugging

Account model
-------------
agy keeps its Google sign-in in the Credential Manager entry
"gemini:antigravity", which only exists for the Windows user that signed in, and
it needs that user's HTTP(S)_PROXY. A service running as SYSTEM cannot read
either. So by default the service runs as SYSTEM and, once a user session is
available, takes that user's token with WTSQueryUserToken and starts the actual
server process under it (CreateProcessAsUser). No Windows password is needed.

With --account the service is instead configured to log on as that user, which
requires the account password (prompted) and "Log on as a service"; that path
exists for machines without an interactive session.

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
import win32api  # noqa: E402
import win32con  # noqa: E402
import win32event  # noqa: E402
import win32process  # noqa: E402
import win32profile  # noqa: E402
import win32security  # noqa: E402
import win32service  # noqa: E402
import win32serviceutil  # noqa: E402
import win32ts  # noqa: E402


def is_system() -> bool:
    try:
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        info = win32security.GetTokenInformation(token, win32security.TokenUser)
        sid = info[0] if isinstance(info, tuple) else info
        return win32security.ConvertSidToStringSid(sid) == "S-1-5-18"
    except Exception:  # noqa: BLE001
        return False


def logged_on_session() -> tuple[int, str] | None:
    """Best interactive session that has a user: (session_id, user_name)."""
    try:
        sessions = win32ts.WTSEnumerateSessions()
    except Exception:  # noqa: BLE001
        return None
    ranked = []
    for session in sessions:
        sid = session.get("SessionId", 0)
        state = session.get("State")
        if not sid:
            continue
        try:
            user = win32ts.WTSQuerySessionInformation(None, sid, win32ts.WTSUserName)
        except Exception:  # noqa: BLE001
            continue
        if not user:
            continue
        rank = 0 if state == win32ts.WTSActive else 1 if state == win32ts.WTSDisconnected else 2
        ranked.append((rank, sid, user))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    _, sid, user = ranked[0]
    return sid, user


def spawn_server_as_user() -> int | None:
    """Start ``service.py run --service-child`` with the logged-on user's token."""
    found = logged_on_session()
    if found is None:
        servicemanager.LogInfoMsg("no logged-on user session yet; will retry")
        return None
    session_id, user = found
    token = None
    primary = None
    try:
        token = win32ts.WTSQueryUserToken(session_id)
        primary = win32security.DuplicateTokenEx(
            token,
            win32security.SecurityImpersonation,
            win32security.TOKEN_ALL_ACCESS,
            win32security.TokenPrimary,
            None,
        )
        env = win32profile.CreateEnvironmentBlock(primary, False)
        startup = win32process.STARTUPINFO()
        command = f'"{sys.executable}" "{os.path.abspath(__file__)}" run --service-child'
        process, thread, pid, _ = win32process.CreateProcessAsUser(
            primary,
            sys.executable,
            command,
            None,
            None,
            0,
            win32con.CREATE_UNICODE_ENVIRONMENT | win32con.CREATE_NO_WINDOW,
            env,
            ROOT,
            startup,
        )
        win32api.CloseHandle(thread)
        servicemanager.LogInfoMsg(
            f"server started as {user} (session {session_id}, pid {pid})"
        )
        return process
    except Exception:  # noqa: BLE001
        servicemanager.LogErrorMsg(f"could not start server as user: {traceback.format_exc()}")
        if primary is not None:
            win32api.CloseHandle(primary)
        return None
    finally:
        if token is not None:
            win32api.CloseHandle(token)


class AgiServerService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = DISPLAY_NAME
    _svc_description_ = DESCRIPTION

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.server = None
        self.thread = None
        self.child = None

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self.server is not None:
            self.server.should_exit = True
        if self.child is not None:
            try:
                win32api.TerminateProcess(self.child, 0)
            except Exception:  # noqa: BLE001
                pass
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self) -> None:
        servicemanager.LogInfoMsg(f"{SERVICE_NAME} starting on {HOST}:{PORT}")
        try:
            load_environment()
            os.makedirs(LOG_DIR, exist_ok=True)
            if is_system():
                self._supervise_user_child()
            else:
                self._run_inline()
            servicemanager.LogInfoMsg(f"{SERVICE_NAME} stopped")
        except Exception:
            servicemanager.LogErrorMsg(f"{SERVICE_NAME} failed:\n{traceback.format_exc()}")
            raise

    def _supervise_user_child(self) -> None:
        """SYSTEM: keep a user-context server child alive until Stop."""
        while True:
            if win32event.WaitForSingleObject(self.stop_event, 3000) == win32event.WAIT_OBJECT_0:
                break
            if self.child is not None:
                if win32event.WaitForSingleObject(self.child, 0) == win32event.WAIT_OBJECT_0:
                    win32api.CloseHandle(self.child)
                    self.child = None
                    servicemanager.LogWarningMsg("server child exited; restarting")
            if self.child is None:
                self.child = spawn_server_as_user()
        if self.child is not None:
            win32api.TerminateProcess(self.child, 0)
            win32event.WaitForSingleObject(self.child, 5000)
            win32api.CloseHandle(self.child)
            self.child = None

    def _run_inline(self) -> None:
        """User-account service: host uvicorn in this process."""
        import uvicorn

        log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        sys.stdout = log
        sys.stderr = log
        config = uvicorn.Config("app.main:app", host=HOST, port=PORT, log_level="info")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="uvicorn", daemon=True)
        self.thread.start()
        while True:
            rc = win32event.WaitForSingleObject(self.stop_event, 1000)
            if rc == win32event.WAIT_OBJECT_0:
                break
            if not self.thread.is_alive():
                servicemanager.LogErrorMsg(f"{SERVICE_NAME}: server exited unexpectedly; see {LOG_FILE}")
                raise RuntimeError("server thread exited unexpectedly")
        self.server.should_exit = True
        self.thread.join(timeout=20)


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

    account = args.account
    password = None
    if account:
        password = args.password or getpass.getpass(f"Windows password for {account}: ")
        if not password:
            print("A password is required when installing with --account.", file=sys.stderr)
            raise SystemExit(1)

    os.makedirs(LOG_DIR, exist_ok=True)
    saved = {key: os.environ.get(key, "") for key in CAPTURED_ENV}
    saved["account"] = account or "LocalSystem"
    saved["agy_bin"] = find_agy()
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(saved, fh, indent=2)

    _delete_service()
    if account:
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

    who = account if account else "LocalSystem (server runs as the logged-on user)"
    print(f"Installed service {SERVICE_NAME} ({DISPLAY_NAME}); account: {who}.")
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
        print("Check services.msc for the state (a bad account password shows as 1069).", file=sys.stderr)
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


def cmd_run(args: argparse.Namespace) -> None:
    load_environment()
    os.makedirs(LOG_DIR, exist_ok=True)
    if args.service_child:
        log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        sys.stdout = log
        sys.stderr = log
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="create the service and start it")
    install.add_argument("--account", help="run the service as this user (needs --password)")
    install.add_argument("--password", help="account password (prompted when omitted)")
    install.set_defaults(func=cmd_install)

    sub.add_parser("uninstall", help="stop and delete the service").set_defaults(func=cmd_uninstall)
    sub.add_parser("start", help="start the service").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop the service").set_defaults(func=cmd_stop)
    sub.add_parser("restart", help="restart the service").set_defaults(func=cmd_restart)
    sub.add_parser("status", help="print the service state").set_defaults(func=cmd_status)

    run = sub.add_parser("run", help="run the server in the foreground")
    run.add_argument("--service-child", action="store_true", help=argparse.SUPPRESS)
    run.set_defaults(func=cmd_run)

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