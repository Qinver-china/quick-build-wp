import base64
import re
import time
from collections.abc import Callable

import paramiko
from sqlalchemy.orm import Session

from app.services.log_publisher import publish_log

# 内部探测/轮询命令，不向用户展示
_QUIET_COMMAND_MARKERS = (
    "pgrep -af",
    "tail -c +",
    "wc -c <",
    "ps -p ",
    "printf '%s'",
    "base64 -d > /tmp/qbw_",
    "curl -fsS --connect-timeout",
    "curl -s -o /dev/null",
    "curl -s -w '%{http_code}'",
    "grep -q ",
    "wp core is-installed",
    "wp --info >/dev/null",
    "wp plugin is-active",
    "cat /tmp/qbw_panel_",
)

_QUIET_COMMAND_PATTERNS = (
    re.compile(r"\btest\s+-[fdx]\b"),
    re.compile(r"^if\s+\[\s+-f\b.*\btail\s+-"),
    re.compile(r"^rm\s+-f\s+/www/server/panel/vhost/nginx/"),
    re.compile(r"^chown\s+-R\s+www:www\b"),
    re.compile(r"^mkdir\s+-p\b"),
    re.compile(r"^nginx\s+-t\b"),
    re.compile(r"^/etc/init\.d/nginx\s+reload"),
    re.compile(r"^cd\s+/tmp\s+&&\s+rm\s+-f\s+wp-cli"),
    re.compile(r"curl\s+-sO\s+--connect-timeout"),
    re.compile(r"^timeout\s+\d+\s+/usr/local/bin/wp\s+core\s+download"),
)

# 无信息量的命令输出（成功时隐藏）
_NOISE_OUTPUT_RE = re.compile(
    r"^(OK|EXISTS|FRESH|GITHUB_OK|DOWNLOAD_OK|EXTRACT_OK|DATABASE_OK|"
    r"SITE_PANEL_OK|SITE_PANEL_SKIP|DB_PANEL_OK|DB_PANEL_SKIP|DB_PANEL_SYNC_OK|"
    r"SSL_OK|PANEL_PY_MISSING|PLUGIN_ACTIVE=[01]|Ready check passed|"
    r"\d+)$"
)


def _is_quiet_command(command: str) -> bool:
    cmd = command.strip()
    if any(marker in cmd for marker in _QUIET_COMMAND_MARKERS):
        return True
    return any(pattern.search(cmd) for pattern in _QUIET_COMMAND_PATTERNS)


def _is_noise_output(line: str, exit_code: int) -> bool:
    text = line.strip()
    if not text:
        return True
    if exit_code != 0:
        return False
    if _NOISE_OUTPUT_RE.match(text):
        return True
    if text.startswith(("SITE_PANEL_FAIL:", "DB_PANEL_FAIL:", "SSL_FAIL:")):
        return False
    return False


class SSHClient:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        task_id: str | None = None,
        log_phase: str = "ssh",
        db: Session | None = None,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.task_id = task_id
        self.log_phase = log_phase
        self._db = db
        self._client: paramiko.SSHClient | None = None

    def connect(self, timeout: int = 20) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        self._client = client

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self) -> "SSHClient":
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _log(self, message: str) -> None:
        if self.task_id and message and message.strip():
            publish_log(self.task_id, self.log_phase, message, self._db)

    @staticmethod
    def redact(text: str, secrets: list[str]) -> str:
        result = text
        for s in secrets:
            if s and len(s) >= 3:
                result = result.replace(s, "***")
        return result

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        mode: int = 0o644,
    ) -> None:
        """通过 SFTP 上传本地文件到远程服务器。"""
        if not self._client:
            raise RuntimeError("SSH not connected")
        self._log(f"上传文件 -> {remote_path}")
        sftp = self._client.open_sftp()
        try:
            sftp.put(local_path, remote_path)
            sftp.chmod(remote_path, mode)
        finally:
            sftp.close()

    def run(
        self,
        command: str,
        timeout: int = 600,
        secrets: list[str] | None = None,
        *,
        quiet: bool | None = None,
    ) -> tuple[int, str, str]:
        if not self._client:
            raise RuntimeError("SSH not connected")
        secrets = secrets or [self.password]
        is_quiet = _is_quiet_command(command) if quiet is None else quiet
        if not is_quiet:
            self._log(f"$ {command[:200]}{'...' if len(command) > 200 else ''}")
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        del stdin
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        combined = (out + err).strip()
        if combined:
            for line in combined.splitlines()[-20:]:
                redacted = self.redact(line, secrets)
                if _is_noise_output(redacted, exit_code):
                    continue
                self._log(redacted)
        return exit_code, out, err

    def run_script(self, script: str, timeout: int = 3600, secrets: list[str] | None = None) -> tuple[int, str, str]:
        """通过 base64 写入临时脚本执行，避免 heredoc/引号在 bash -lc 中被破坏。"""
        task_tag = re.sub(r"[^a-zA-Z0-9_-]", "_", self.task_id or "adhoc")
        script_path = f"/tmp/qbw_script_{task_tag}.sh"
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        wrapper = (
            f"printf '%s' '{encoded}' | base64 -d > {script_path} "
            f"&& chmod +x {script_path} && bash {script_path}"
        )
        return self.run(wrapper, timeout=timeout, secrets=secrets)

    def tail_until_ready(
        self,
        log_file: str,
        poll_interval: int = 10,
        max_wait: int = 3600,
        ready_check: Callable[[], bool] | None = None,
        secrets: list[str] | None = None,
    ) -> tuple[int, str]:
        """仅跟踪已有后台任务的日志，直到就绪或进程结束（不启动新命令）。"""
        secrets = secrets or [self.password]
        last_pos = 0
        elapsed = 0

        while elapsed < max_wait:
            if ready_check and ready_check():
                return 0, "ready"

            _, tail_out, _ = self.run(
                f"if [ -f {log_file} ]; then tail -c +{last_pos + 1} {log_file} 2>/dev/null; wc -c < {log_file}; fi",
                timeout=30,
                secrets=secrets,
            )
            lines = tail_out.strip().splitlines()
            if len(lines) >= 1:
                try:
                    new_pos = int(lines[-1])
                    content = "\n".join(lines[:-1])
                    if content:
                        for line in content.splitlines():
                            self._log(self.redact(line, secrets))
                    last_pos = new_pos
                except ValueError:
                    pass

            _, ps_out, _ = self.run(
                r"pgrep -af 'install_panel\.sh|install-ubuntu_6\.0\.sh|install_6\.0\.sh|install_soft\.sh' "
                r"2>/dev/null | grep -v pgrep | head -1",
                timeout=15,
                secrets=secrets,
            )
            if not ps_out.strip() and ready_check and ready_check():
                return 0, "ready"
            if not ps_out.strip() and not ready_check:
                _, final_out, _ = self.run(
                    f"cat {log_file} 2>/dev/null | tail -30",
                    timeout=30,
                    secrets=secrets,
                )
                return 0, final_out

            time.sleep(poll_interval)
            elapsed += poll_interval

        if ready_check and ready_check():
            return 0, "ready"
        raise TimeoutError(f"Wait timed out after {max_wait}s")

    def run_background_tail(
        self,
        command: str,
        log_file: str,
        poll_interval: int = 10,
        max_wait: int = 3600,
        ready_check: Callable[[], bool] | None = None,
        secrets: list[str] | None = None,
    ) -> tuple[int, str]:
        """Run command in background, tail log until done or ready_check passes."""
        secrets = secrets or [self.password]
        bg_cmd = f"nohup bash -lc {repr(command)} > {log_file} 2>&1 & echo $!"
        code, out, _ = self.run(bg_cmd, timeout=60, secrets=secrets)
        if code != 0:
            raise RuntimeError(f"Failed to start background command: {out}")

        pid = out.strip().splitlines()[-1] if out.strip() else ""
        self._log(f"Background process started (pid={pid})")

        last_pos = 0
        elapsed = 0
        while elapsed < max_wait:
            # Tail new log content
            _, tail_out, _ = self.run(
                f"if [ -f {log_file} ]; then tail -c +{last_pos + 1} {log_file} 2>/dev/null; wc -c < {log_file}; fi",
                timeout=30,
                secrets=secrets,
            )
            lines = tail_out.strip().splitlines()
            if len(lines) >= 1:
                try:
                    new_pos = int(lines[-1])
                    content = "\n".join(lines[:-1])
                    if content:
                        for line in content.splitlines():
                            self._log(self.redact(line, secrets))
                    last_pos = new_pos
                except ValueError:
                    pass

            # Check if process still running
            if pid:
                _, ps_out, _ = self.run(f"ps -p {pid} > /dev/null 2>&1; echo $?", timeout=10, secrets=secrets)
                running = ps_out.strip().endswith("0")
            else:
                running = True

            if ready_check and ready_check():
                return 0, "ready"

            if not running:
                if ready_check and ready_check():
                    return 0, "ready"
                _, final_out, _ = self.run(f"cat {log_file} 2>/dev/null | tail -30", timeout=30, secrets=secrets)
                return 0, final_out

            time.sleep(poll_interval)
            elapsed += poll_interval

        if ready_check and ready_check():
            return 0, "ready"
        raise TimeoutError(f"Command timed out after {max_wait}s")

    def test_connection(self) -> tuple[bool, str, str | None]:
        try:
            self.connect()
            code, out, err = self.run("uname -a && id", timeout=30)
            if code != 0:
                return False, err or out or "SSH command failed", None
            if "uid=0" not in out and "root" not in self.username:
                return False, "需要 root 权限或具备 sudo 的账号", out.strip()
            return True, "连接成功", out.strip()
        except Exception as e:
            return False, str(e), None
        finally:
            self.close()


def is_domain(value: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z]{2,})+$", value))
