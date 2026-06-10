import os
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600

NUCLEI_COMMAND = os.getenv("NUCLEI_COMMAND", "nuclei")
DEFAULT_TEMPLATES_DIR = os.getenv("NUCLEI_TEMPLATES_DIR", "")

SEVERITY_ORDER = {
        "unknown": 0,
        "info": 1,
        "low": 2,
        "medium": 3,
        "high": 4,
        "critical": 5,
    }