import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(config):
    """Bound operational logs separately from the durable deletion audit trail."""
    options = config['logging']
    path = Path(options['directory'])
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Apply reduced retention on restart as well; touch only our numbered backups.
    for old_backup in path.glob('qc-folder-manager.log.*'):
        match = re.fullmatch(r'qc-folder-manager\.log\.([1-9][0-9]*)',old_backup.name)
        if match and int(match.group(1)) > options['backup_count']:
            old_backup.unlink(missing_ok=True)
    logger = logging.getLogger('qcmanager')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for old in logger.handlers[:]:
        old.close()
        logger.removeHandler(old)
    handler = RotatingFileHandler(path/'qc-folder-manager.log', maxBytes=options['max_bytes'],
                                  backupCount=options['backup_count'], encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    # Waitress exception logs use this same bounded handler; no raw access logs.
    server_logger = logging.getLogger('waitress')
    server_logger.handlers.clear()
    server_logger.addHandler(handler)
    server_logger.propagate = False
    server_logger.setLevel(logging.WARNING)
    return logger
