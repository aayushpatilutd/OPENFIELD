"""Production entry point.

Starts the agent scheduler as a side effect of import, which is what gunicorn
needs — there is no __main__ under a WSGI server. Run exactly one worker, or
every scheduled agent fires once per worker.
"""

from app import app
from core import agents

agents.start_scheduler(app)
