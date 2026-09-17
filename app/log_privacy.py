"""Do not put OAuth authorization codes/state in the default Uvicorn access log."""

import logging


class OAuthQueryFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, target, protocol, status = record.args
            if isinstance(target, str) and target.split("?", 1)[0].endswith("/auth/github/callback"):
                record.args = (client, method, target.split("?", 1)[0], protocol, status)
        return True


def install():
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, OAuthQueryFilter) for item in logger.filters):
        logger.addFilter(OAuthQueryFilter())
