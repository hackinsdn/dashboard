# -*- encoding: utf-8 -*-
"""DB-backed pylti1p3 tool configuration (lti_config table)."""

from flask import current_app
from pylti1p3.tool_config import ToolConfDict

from apps.lti.keys import abs_key_path
from apps.lti.models import LtiConfig


class DbToolConf(ToolConfDict):
    """pylti1p3 ToolConf built from the lti_config table.

    Instantiated per request: cheap, and it means every gunicorn worker
    picks up registrations made through another worker's /lti/register/
    call without a restart."""

    def __init__(self):
        rows = [
            row for row in LtiConfig.query.filter_by(is_deleted=False).all()
            if row.config
        ]
        super().__init__({row.issuer: row.config for row in rows})
        for row in rows:
            for reg in row.config:
                client_id = reg.get("client_id")
                for field, setter in (
                    ("private_key_file", self.set_private_key),
                    ("public_key_file", self.set_public_key),
                ):
                    if not reg.get(field):
                        continue
                    try:
                        with open(abs_key_path(reg[field])) as f:
                            setter(row.issuer, f.read(), client_id=client_id)
                    except OSError as exc:
                        current_app.logger.error(
                            f"LTI key file unreadable for issuer={row.issuer} "
                            f"client_id={client_id}: {exc}"
                        )
