"""Production entry point. The worker owns scheduled jobs in split deployments."""
import os

os.environ.setdefault("POSTENZA_EMBEDDED_SCHEDULER", "false")

from webhook import app

if os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
