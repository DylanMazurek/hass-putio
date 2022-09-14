"""Put.io Constants."""
import logging

DOMAIN = "putio"
CONF_FILE_TYPES = "accepted_file_types"
CONF_MONITOR_FOLDERS = "monitor_folders"
CONF_RETRY_ATTEMPTS = "retry_attempts"
BASE_URL = "https://api.put.io/v2"
TRANSFER_COMPLETED_ID = "putio_transfer_completed"

LOGGER = logging.getLogger(__package__)
