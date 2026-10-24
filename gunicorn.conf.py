import os

bind = "0.0.0.0:" + os.getenv("PORT", "8000")
# Legacy JSON is shared on one local volume. Keep one web process and one job worker.
workers = 1
worker_class = "gthread"
threads = 4
timeout = 120
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
# Omit query strings: OAuth authorization codes must not enter access logs.
access_log_format = '%(h)s %(m)s %(U)s %(s)s %(L)s'
