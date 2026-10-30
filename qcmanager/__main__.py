import argparse
import fcntl
import json
import logging
import os
import signal
import sys
from pathlib import Path

from .clients import Drone, http_json
from .config import ConfigError, load_config
from .database import Database
from .logging_setup import setup_logging
from .storage import Storage


def main(argv=None):
    parser = argparse.ArgumentParser(description='QC Folder Manager — Gitea / Drone / local and SSH storage')
    parser.add_argument('--config',required=True,help='Path to config.yml')
    parser.add_argument('--check-config',action='store_true',help='Validate configuration without network or filesystem mutations')
    parser.add_argument('--preflight',action='store_true',help='Read-only checks for Drone and all configured roots')
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        config = load_config(args.config)
        if args.check_config:
            print('Configuration valid. %d projects; operational log budget %d bytes.' % (
                len(config['projects']), config['logging']['max_bytes']*(config['logging']['backup_count']+1)))
            return 0
        if args.preflight:
            failed = False
            token = os.environ.get(config['drone']['token_env'])
            try:
                if not token:
                    raise ValueError('Missing token')
                user = http_json(config['drone']['base_url']+'/api/user',token)
                if not isinstance(user,dict) or not user.get('login'):
                    raise ValueError('Invalid user')
                print('Drone service token: OK')
            except Exception:
                print('Drone service token: FAILED (network, token or permission)')
                failed = True
            storage = Storage(config)
            for project in config['projects']:
                try:
                    storage.request(project,'probe')
                    print(project['id']+': root and helper OK (deletion not attempted)')
                except Exception:
                    print(project['id']+': FAILED (SSH/Python/root/permissions)')
                    failed = True
            return 1 if failed else 0
        required = [config['gitea']['client_id_env'],config['gitea']['client_secret_env'],config['drone']['token_env']]
        missing = [key for key in required if not os.environ.get(key)]
        if missing:
            raise ConfigError('Missing environment variables: '+', '.join(missing))
        data = Path(config['server']['data_dir'])
        data.mkdir(parents=True,exist_ok=True,mode=0o700)
        # Only one instance may recover jobs, rotate this log, or modify this inventory.
        with open(data/'service.lock','a') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConfigError('Another service instance is already using this data_dir')
            logger = setup_logging(config)
            db = Database(data/'qc-folder-manager.sqlite3')
            db.recover()
            from .web import create_app
            from waitress import create_server
            app = create_app(config,db=db)
            server = create_server(app,host=config['server']['listen'],port=config['server']['port'],
                                   threads=8,max_request_body_size=65536,clear_untrusted_proxy_headers=True)
            def stop(signum, frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM,stop)
            logger.info('service started version=0.1.0 projects=%d',len(config['projects']))
            try:
                server.run()
            except KeyboardInterrupt:
                logger.info('service stopping; waiting for active filesystem work')
            finally:
                server.close()
                app.extensions['manager'].shutdown()
                logger.info('service stopped')
                logging.shutdown()
        return 0
    except ConfigError as error:
        print('Configuration error: '+str(error),file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
