import json
import logging

logger = logging.getLogger("SRTYOLOUnified.File")

class FileLogger:
    def __init__(self, filepath):
        self.filepath = filepath
        # Clear file
        try:
            with open(self.filepath, 'w') as f:
                pass
        except Exception as e:
            logger.error(f"Error initializing log file: {e}")
            
    def log(self, metadata):
        try:
            with open(self.filepath, 'a') as f:
                f.write(json.dumps(metadata, default=str) + "\n")
        except Exception as e:
            logger.error(f"Error logging to file: {e}")
