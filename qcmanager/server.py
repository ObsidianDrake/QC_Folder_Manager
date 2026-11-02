"""Bind once, register the actual listener, and clean up on normal shutdown."""
import signal
import socket

from .route_registration import RouteAPIError, RouteRegistration


def bind_listener(host, port):
    # A retained socket avoids the race inherent in probing and releasing a port.
    family, kind, protocol, _, address = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)[0]
    listener = socket.socket(family, kind, protocol)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        listener.bind(address)
        return listener
    except BaseException:
        listener.close()
        raise


def run_service(config, db, logger):
    from waitress import create_server
    from .web import create_app

    registration = RouteRegistration(config)
    app = None
    server = None
    listener = None
    serving = False
    stop_requested = False
    exit_code = 0

    def stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True
        # Finish bounded registration first, so a successful PUT can be deleted.
        if serving:
            raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        listener = bind_listener(config['server']['listen'], config['server']['port'])
        port = listener.getsockname()[1]
        app = create_app(config, db=db)
        server = create_server(app, sockets=[listener], threads=8,
                               max_request_body_size=65536, clear_untrusted_proxy_headers=True)
        if not stop_requested:
            registration.register(port)
            if registration.name:
                logger.info('Traefik-API route registered name=%s port=%d', registration.name, port)
            logger.info('service listening host=%s port=%d', config['server']['listen'], port)
        if not stop_requested:
            app.extensions['manager'].start_scanning(app.extensions['project_settings'])
            logger.info('service started version=0.1.0 projects=%d', len(config['projects']))
            serving = True
            if not stop_requested:
                server.run()
    except KeyboardInterrupt:
        logger.info('service stopping; waiting for active filesystem work')
    except (RouteAPIError, OSError) as error:
        message = str(error) if isinstance(error, RouteAPIError) else 'Listener/startup failure (%s, errno=%s)' % (
            type(error).__name__, getattr(error, 'errno', None))
        logger.error('%s', message)
        exit_code = 1
    finally:
        serving = False
        try:
            try:
                registration.unregister()
            except RouteAPIError as error:
                logger.error('Route cleanup failed: %s', error)
                exit_code = 1
            finally:
                try:
                    if server is not None:
                        server.close()
                finally:
                    if listener is not None:
                        listener.close()
                    if app is not None:
                        app.extensions['manager'].shutdown()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            logger.info('service stopped')
    return exit_code
