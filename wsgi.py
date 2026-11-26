"""Production entry point. One web process runs the app and the embedded scheduler."""

import os

os.environ.setdefault("POSTENZA_EMBEDDED_SCHEDULER", "true")

from webhook import app

if os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true" or "DYNO" in os.environ:
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
