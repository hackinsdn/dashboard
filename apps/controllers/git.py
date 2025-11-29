"""Git Controller."""

import git
import os
import time
import glob
from pathlib import Path

from flask import current_app
from apps.config import app_config


class GitController:
    """Manage files from Git repositories."""

    def __init__(self):
        self.last_update = {}

    def update_repo(self, repo_url, target_dir, refresh_interval=300):
        """Clone or pull changes from repo into dir"""
        now = time.time()
        if now - self.last_update.get(f"git:{target_dir}", 0) < refresh_interval:
            return True
        self.last_update[f"git:{target_dir}"] = now
        os.makedirs(os.path.dirname(target_dir.strip("/")), exist_ok=True)
        try:
            if not os.path.exists(target_dir):
                git.Repo.clone_from(repo_url, target_dir, kill_after_timeout=app_config.GIT_UPDATE_TIMEOUT)
                return True
            repo = git.Repo(target_dir)
            origin = repo.remotes.origin
            origin.pull(kill_after_timeout=app_config.GIT_UPDATE_TIMEOUT)
            return True
        except git.GitCommandError as exc:
            current_app.logger.error(f"Failed to run GIT operation: {exc}")
        except Exception as exc:
            current_app.logger.error(f"Failed to run GIT operation: {exc}")
        return False

    def list_files(self, folder, pattern="*.*", recursive=True):
        return glob.glob(pattern, recursive=recursive, root_dir=folder)

    def get_file(self, folder, filename):
        """Get file content."""
        file = Path(folder) / filename
        if not file.exists():
            return False, 'Template not found'
        try:
            return True, file.read_text()
        except Exception as e:
            current_app.logger.error(f"Failed to read template file: {e}")
            return False, "Failed to read template file, check with administrator."
