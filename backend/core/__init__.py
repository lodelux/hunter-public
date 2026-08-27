"""Core backend modules."""

import os

# Keep third-party browser reporting disabled for every core import path.
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
os.environ["BROWSER_USE_VERSION_CHECK"] = "false"
os.environ["BH_TELEMETRY"] = "false"
os.environ["BROWSER_HARNESS_TELEMETRY"] = "false"
