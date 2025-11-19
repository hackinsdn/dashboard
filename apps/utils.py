# -*- encoding: utf-8 -*-
"""
Copyright (c) 2015 - HackInSDN team
"""
import datetime
import re
import os
import unicodedata

from flask import g
from flask_login import current_user

from apps import cache
from apps.home.models import LabInstances

_filename_ascii_strip_re = re.compile(r"[^A-Za-z0-9_.-]")


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def update_running_labs_stats():
    """Update statistics on running labs and make them available on Flask 'g' variable,
    so that it can be easily accessed from Jinja template"""
    user_id = current_user.id if hasattr(current_user, "id") else "all"
    running_labs = cache.get(f"running_labs-{user_id}")
    if running_labs is None:
        running_labs = LabInstances.query.filter_by(is_deleted=False)
        if user_id != "all":
            running_labs = running_labs.filter_by(user_id=user_id)
        running_labs = running_labs.count()
        cache.set(f"running_labs-{user_id}", running_labs)
    g.running_labs = running_labs


def format_duration(delta: datetime.timedelta) -> str:
    """
    Enhanced timedelta format duration from kubernetes.utils.duration
    """

    # Short-circuit if we have a zero delta.
    if delta == datetime.timedelta(0):
        return "0s"

    # Check range early.
    if delta < datetime.timedelta(0):
        return "--"

    # After that, do the usual div & mod tree to take seconds and get days 
    # hours, minutes, and seconds from it.
    secs = int(delta.total_seconds())

    output: List[str] = []

    if delta.days >= 2:
        output.append(f"{delta.days}d")
        if delta.days > 6:
            secs = 0
        else:
            secs -= delta.days * 86400

    hours = secs // 3600
    if hours > 0:
        output.append(f"{hours}h")
        if delta.days > 1 or hours > 3:
            secs = 0
        else:
            secs -= hours * 3600

    minutes = secs // 60
    if minutes > 0:
        output.append(f"{minutes}m")
        if minutes > 4:
            secs = 0
        else:
            secs -= minutes * 60

    if secs > 0:
        output.append(f"{secs}s")

    return "".join(output)


def parse_lab_expiration(expiration):
    if expiration == "0":
        return None
    exp_date = utcnow() + datetime.timedelta(hours=int(expiration))
    return int(exp_date.timestamp())


def datetime_from_ts(timestamp):
    try:
        dt = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
        return dt.isoformat()
    except:
        return None


def secure_filename(filename: str) -> str:
    r"""Pass it a filename and it will return a secure version of it.  This
    filename can then safely be stored on a regular file system and passed
    to :func:`os.path.join`.  The filename returned is an ASCII only string
    for maximum portability.

    On windows systems the function also makes sure that the file is not
    named after one of the special device files.

    >>> secure_filename("My cool movie.mov")
    'My_cool_movie.mov'
    >>> secure_filename("../../../etc/passwd")
    'etc/passwd'
    >>> secure_filename("xpto/a b c/../../../../../../etc/passwd")
    'xpto/a_b_c/etc/passwd'
    >>> secure_filename('i contain cool \xfcml\xe4uts.txt')
    'i_contain_cool_umlauts.txt'

    The function might return an empty filename.  It's your responsibility
    to ensure that the filename is unique and that you abort or
    generate a random filename if the function returned an empty one.

    Update: this was slightly modified from its original implementation
    (from werkzeug.utils) to allow relative paths.

    .. versionadded:: 0.6

    :param filename: the filename to secure
    """
    filename = unicodedata.normalize("NFKD", filename)
    filename = filename.encode("ascii", "ignore").decode("ascii")

    for sep in os.sep, os.path.altsep:
        if sep:
            filename = filename.replace(sep, "\0")

    parts = []
    for part in filename.split("\0"):
        part = str(re.sub(r"\s+", "_", part))
        part = str(_filename_ascii_strip_re.sub("", part)).strip("._")
        if part:
            parts.append(part)

    filename = "/".join(parts)

    # on nt a couple of special files are present in each folder.  We
    # have to ensure that the target file is not such a filename.  In
    # this case we prepend an underline
    if (
        os.name == "nt"
        and filename
        and filename.split(".")[0].upper() in _windows_device_files
    ):
        filename = f"_{filename}"

    return filename


def list_files(folder, ignore_prefix=""):
    """List files in a folder recursively. Params:
       - ignore_prefix: files matching this string will be ignored
    """
    current_files = set()
    for root, dirs, files in os.walk(folder):
        relpath = root.removeprefix(folder)
        for file in files:
            if ignore_prefix and file.startswith(ignore_prefix):
                continue
            current_files.add(os.path.join(relpath, file))
    return current_files


def remove_empty_folders(folder):
    """Remove empty folders inside a particular folder."""
    folders = list(os.walk(mydir))[1:]
    for folder in folders:
        if not folder[2]:
            os.rmdir(folder[0])
