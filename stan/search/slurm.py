"""Shared SLURM job submission and polling via paramiko SSH."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path, PurePosixPath

import paramiko

logger = logging.getLogger(__name__)


class SlurmError(Exception):
    """Raised when a SLURM job fails, is cancelled, or times out."""


class SlurmClient:
    """SSH client for SLURM job submission and monitoring on Hive.

    Usage::

        with SlurmClient(host="hive.ucdavis.edu", user="bphinney") as slurm:
            job_id = slurm.submit_job(script_content, remote_dir)
            state = slurm.poll_completion(job_id)
    """

    def __init__(
        self,
        host: str,
        user: str,
        key_path: str | None = None,
        port: int = 22,
    ) -> None:
        self._host = host
        self._user = user
        self._key_path = key_path
        self._port = port
        self._client: paramiko.SSHClient | None = None

    def __enter__(self) -> SlurmClient:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def connect(self) -> None:
        """Establish SSH connection to Hive."""
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict = {
            "hostname": self._host,
            "username": self._user,
            "port": self._port,
        }
        if self._key_path:
            key_path = Path(self._key_path).expanduser()
            connect_kwargs["key_filename"] = str(key_path)

        try:
            self._client.connect(**connect_kwargs)
            logger.info("SSH connected to %s@%s", self._user, self._host)
        except paramiko.SSHException:
            logger.exception("SSH connection failed to %s@%s", self._user, self._host)
            raise

    def close(self) -> None:
        """Close the SSH connection."""
        if self._client:
            self._client.close()
            self._client = None
            logger.info("SSH connection closed")

    def run_command(self, cmd: str) -> tuple[str, str]:
        """Execute a command over SSH. Returns (stdout, stderr)."""
        if not self._client:
            raise RuntimeError("Not connected — call connect() first")

        logger.debug("SSH exec: %s", cmd)
        _, stdout, stderr = self._client.exec_command(cmd)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        if err:
            logger.debug("SSH stderr: %s", err)
        return out, err

    def submit_job(self, script_content: str, remote_dir: str) -> str:
        """Write a SLURM script to the remote host and submit it via sbatch.

        Args:
            script_content: Full SLURM batch script content.
            remote_dir: Directory on Hive to write the script to.

        Returns:
            SLURM job ID as a string.
        """
        if not self._client:
            raise RuntimeError("Not connected — call connect() first")

        remote_script = str(PurePosixPath(remote_dir) / "stan_job.sh")

        # Ensure remote directory exists
        self.run_command(f"mkdir -p {remote_dir}")

        # Write script via SFTP
        sftp = self._client.open_sftp()
        try:
            with sftp.open(remote_script, "w") as f:
                f.write(script_content)
            sftp.chmod(remote_script, 0o755)
        finally:
            sftp.close()

        logger.info("Wrote SLURM script to %s", remote_script)

        # Submit via sbatch
        out, err = self.run_command(f"sbatch {remote_script}")

        # Parse job ID from "Submitted batch job 12345"
        match = re.search(r"Submitted batch job (\d+)", out)
        if not match:
            raise SlurmError(f"Failed to parse job ID from sbatch output: {out} {err}")

        job_id = match.group(1)
        logger.info("Submitted SLURM job %s", job_id)
        return job_id

    def poll_completion(
        self,
        job_id: str,
        poll_interval: int = 30,
        timeout: int = 7200,
    ) -> str:
        """Poll sacct until the job reaches a terminal state.

        Args:
            job_id: SLURM job ID to monitor.
            poll_interval: Seconds between polls.
            timeout: Maximum seconds to wait before raising.

        Returns:
            Final job state string (e.g. "COMPLETED").

        Raises:
            SlurmError: If job fails, is cancelled, or times out.
        """
        terminal_states = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL"}
        start = time.time()

        while True:
            elapsed = time.time() - start
            if elapsed > timeout:
                raise SlurmError(f"Job {job_id} timed out after {timeout}s")

            out, _ = self.run_command(
                f"sacct -j {job_id} --format=State --noheader --parsable2"
            )

            # sacct may return multiple lines (job + job steps)
            states = {line.strip().rstrip("+") for line in out.splitlines() if line.strip()}

            for state in states:
                if state in terminal_states:
                    if state == "COMPLETED":
                        logger.info("Job %s completed", job_id)
                        return state
                    raise SlurmError(f"Job {job_id} ended with state: {state}")

            logger.debug("Job %s still running (states: %s)", job_id, states)
            time.sleep(poll_interval)
