"""Validated administrator-owned configuration."""
import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml


class ConfigError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key, value in node.value:
        name = loader.construct_object(key, deep=deep)
        if name in result:
            raise ConfigError("Duplicate YAML key: %s" % name)
        result[name] = loader.construct_object(value, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def url(value, name):
    if not isinstance(value, str):
        raise ConfigError("%s must be an HTTP(S) URL" % name)
    p = urlsplit(value)
    if (p.scheme not in ("http", "https") or not p.hostname or p.username or
            p.password or p.query or p.fragment or re.search(r"[\s\\]", value) or
            any(x in (".", "..") for x in p.path.split("/"))):
        raise ConfigError("Invalid %s" % name)
    return value.rstrip("/")


def load_config(path):
    source = Path(path).resolve()
    try:
        text = source.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ConfigError("Cannot read configuration %s: %s (errno=%s)" %
                          (ascii(str(source)), ascii(e.strerror or str(e)), e.errno)) from e
    except UnicodeError as e:
        raise ConfigError("Configuration %s must be UTF-8 encoded" %
                          ascii(str(source))) from e
    try:
        config = yaml.load(text, Loader=UniqueLoader)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        location = (" at line %d, column %d" % (mark.line + 1, mark.column + 1)) if mark else ""
        # Do not print the YAML source excerpt: it may contain credentials.
        problem = getattr(e, "problem", None) or "invalid YAML syntax"
        raise ConfigError("Invalid YAML in %s%s: %s" %
                          (ascii(str(source)), location, ascii(problem))) from e
    config = validate_config(config, source)
    config['_source_path'] = str(source)
    config['_source_digest'] = __import__('hashlib').sha256(text.encode('utf-8')).hexdigest()
    return config


def validate_config(config, source):
    source = Path(source).resolve()
    if not isinstance(config, dict) or type(config.get("schema_version")) is not int or config.get("schema_version") != 1:
        raise ConfigError("schema_version must be 1")
    for key in ("gitea", "drone", "hosts"):
        if not isinstance(config.get(key), dict):
            raise ConfigError("Missing mapping: " + key)
    for key in ("gitea", "drone"):
        config[key]["base_url"] = url(config[key].get("base_url"), key + ".base_url")
    app = config.setdefault("server", {})
    if not isinstance(app, dict):
        raise ConfigError("server must be a mapping")
    app["public_url"] = url(app.get("public_url"), "server.public_url")
    app.setdefault("listen", "127.0.0.1")
    # Bind port zero directly; the OS chooses and reserves an available port.
    if app.get("port") is None:
        app["port"] = 0
    app.setdefault("workers", 2)
    app.setdefault("session_hours", 8)
    app.setdefault("scan_interval_seconds", 300)
    for name, lo, hi in (("port", 0, 65535), ("workers", 1, 8), ("session_hours", 1, 24), ("scan_interval_seconds", 60, 86400)):
        if type(app[name]) is not int or not lo <= app[name] <= hi:
            raise ConfigError("Invalid server." + name)
    if not isinstance(app["listen"], str):
        raise ConfigError("server.listen must be an address")
    data = app.get("data_dir")
    if not isinstance(data, str) or not data:
        raise ConfigError("server.data_dir is required")
    app["data_dir"] = str((source.parent / data).resolve())
    app["base_path"] = urlsplit(app["public_url"]).path.rstrip("/")
    if not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*", app["base_path"]):
        raise ConfigError("public_url path supports letters, digits, hyphens and underscores")
    validate_traefik(config)
    logs = config.setdefault('logging', {})
    if not isinstance(logs, dict):
        raise ConfigError('logging must be a mapping')
    if set(logs) - {'directory','max_bytes','backup_count'}:
        raise ConfigError('Unknown logging setting')
    logs.setdefault('directory', str(Path(app['data_dir']) / 'logs'))
    if not isinstance(logs['directory'], str):
        raise ConfigError('logging.directory must be a path')
    logs['directory'] = str((source.parent / logs['directory']).resolve())
    logs.setdefault('max_bytes', 10 * 1024 * 1024)
    logs.setdefault('backup_count', 5)
    for name, lo, hi in (('max_bytes', 1024, 1024 * 1024 * 1024), ('backup_count', 1, 100)):
        if type(logs[name]) is not int or not lo <= logs[name] <= hi:
            raise ConfigError('Invalid logging.' + name)
    hosts = config["hosts"]
    for name, host in hosts.items():
        if not isinstance(name, str) or not isinstance(host, dict):
            raise ConfigError("Invalid host")
        if host.get("transport") not in ("local", "ssh"):
            raise ConfigError("Host transport must be local or ssh")
        if not isinstance(host.get('environment'), str) or not host['environment']:
            raise ConfigError('Host environment is required')
        dust = host.setdefault('dust', {})
        if not isinstance(dust, dict) or set(dust) - {'executable', 'threads', 'timeout_seconds'}:
            raise ConfigError('Unknown dust setting')
        executable = dust.setdefault('executable', 'dust')
        if (not isinstance(executable, str) or not executable or executable.startswith('-') or
                any(c in executable for c in ('\x00', '\r', '\n')) or
                ('/' in executable and not executable.startswith('/'))):
            raise ConfigError('dust.executable must be an absolute path or a command name')
        dust.setdefault('threads', 4)
        dust.setdefault('timeout_seconds', 1800)
        for key, low, high in (('threads', 1, 64), ('timeout_seconds', 1, 86400)):
            if type(dust[key]) is not int or not low <= dust[key] <= high:
                raise ConfigError('Invalid dust.' + key)
        if host["transport"] == "ssh":
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", host.get("hostname", "")):
                raise ConfigError("Invalid SSH hostname/alias")
            ssh = host.setdefault("ssh", {})
            if not isinstance(ssh, dict) or set(ssh) - {"python", "timeout_seconds", "password", "host_key_checking", "known_hosts"}:
                raise ConfigError("Unknown SSH setting")
            if 'password' in ssh:
                password = ssh['password']
                if not isinstance(password, str) or not password or any(c in password for c in ('\x00', '\r', '\n')):
                    raise ConfigError('ssh.password must be a non-empty single-line string')
            policy = ssh.setdefault('host_key_checking', 'strict')
            if policy not in ('strict', 'accept-new'):
                raise ConfigError('ssh.host_key_checking must be strict or accept-new')
            if policy == 'accept-new':
                ssh.setdefault('known_hosts', str(Path(app['data_dir'])/'ssh_known_hosts'))
            if 'known_hosts' in ssh:
                known = ssh['known_hosts']
                if not isinstance(known,str) or not known.strip() or any(c in known for c in ('\x00','\r','\n','"','%')):
                    raise ConfigError('ssh.known_hosts must be a file path')
                known = (source.parent / known).resolve()
                if str(known).startswith('/dev/'):
                    raise ConfigError('ssh.known_hosts must be a persistent file, not a device')
                ssh['known_hosts'] = str(known)
            executable = ssh.setdefault("python", "python3")
            if not isinstance(executable, str) or not re.fullmatch(r"/?[A-Za-z0-9_./-]+", executable) or executable.startswith("-"):
                raise ConfigError("Invalid remote Python executable")
            timeout = ssh.setdefault("timeout_seconds", 1800)
            if type(timeout) is not int or not 10 <= timeout <= 86400:
                raise ConfigError("Invalid SSH timeout")
    projects = config.get("projects")
    if not isinstance(projects, list):
        raise ConfigError("projects must be a list")
    ids, roots = set(), []
    for p in projects:
        if not isinstance(p, dict):
            raise ConfigError("Invalid project")
        for name in ("id", "organization", "repository", "prefix"):
            value = p.get(name)
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
                raise ConfigError("Invalid project " + name)
        if p["id"] in ids or p.get("host") not in hosts:
            raise ConfigError("Duplicate project ID or unknown host")
        ids.add(p["id"])
        root = p.get("run_folder")
        if (not isinstance(root, str) or not root.startswith("/") or root == "/" or
                any(x in ("", ".", "..") for x in root[1:].split("/")) or
                any(ord(x) < 32 for x in root)):
            raise ConfigError("run_folder must be a normalized absolute directory")
        h = hosts[p["host"]]
        target = (h["transport"], h.get("hostname", "local"))
        for other_target, other_root in roots:
            if target == other_target and (root == other_root or root.startswith(other_root + "/") or other_root.startswith(root + "/")):
                raise ConfigError("Overlapping management roots")
        if h["transport"] == "local":
            # Audit state must not live inside a managed tree (or vice versa).
            local_root = os.path.realpath(root)
            common = os.path.commonpath([local_root, app["data_dir"]])
            if common in (local_root, app["data_dir"]):
                raise ConfigError("data_dir and managed roots must be separate")
            if os.path.commonpath([local_root, logs['directory']]) == local_root:
                raise ConfigError('Logs must not live under a managed root')
        roots.append((target, root))
    config["gitea"].setdefault("scopes", "read:user read:organization")
    for section, keys in (("gitea", ("client_id", "client_secret")), ("drone", ("token",))):
        for key in keys:
            value = config[section].get(key)
            if not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value:
                raise ConfigError("%s.%s must be a non-empty, single-line string" % (section, key))
    return config


def validate_traefik(config):
    route = config.setdefault('traefik_api', {})
    if not isinstance(route, dict):
        raise ConfigError('traefik_api must be a mapping')
    if set(route) - {'enable', 'base_url', 'token', 'route_name', 'upstream_host', 'timeout_seconds'}:
        raise ConfigError('Unknown traefik_api setting')
    route.setdefault('enable', False)
    if type(route['enable']) is not bool:
        raise ConfigError('traefik_api.enable must be true or false')
    if not route['enable']:
        return
    try:
        route['base_url'] = url(route.get('base_url'), 'traefik_api.base_url')
        if urlsplit(route['base_url']).port == 0:
            raise ValueError()
    except ValueError:
        raise ConfigError('Invalid traefik_api.base_url or port') from None
    token = route.get('token')
    if not isinstance(token, str) or not token.strip() or any(ord(c) < 32 or ord(c) > 126 for c in token):
        raise ConfigError('traefik_api.token must be a non-empty ASCII single-line string')
    route.setdefault('route_name', 'qc-folder-manager')
    if not isinstance(route['route_name'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', route['route_name']):
        raise ConfigError('Invalid traefik_api.route_name (lowercase letters, digits and hyphens; max 63)')
    route.setdefault('timeout_seconds', 30)
    if type(route['timeout_seconds']) is not int or not 1 <= route['timeout_seconds'] <= 300:
        raise ConfigError('Invalid traefik_api.timeout_seconds (1-300)')
    dns = r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*'
    route.setdefault('upstream_host', config['server']['listen'])
    host = route['upstream_host']
    if not isinstance(host, str) or not host:
        raise ConfigError('Invalid traefik_api.upstream_host')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(dns, host.lower()) or host == '*':
            raise ConfigError('traefik_api.upstream_host must be a hostname or IP without a port') from None
    else:
        if address.is_unspecified or address.is_multicast or '%' in host:
            raise ConfigError('traefik_api.upstream_host must be a reachable hostname or IP, not a wildcard')
    public_host = urlsplit(config['server']['public_url']).hostname
    if not re.fullmatch(dns, public_host.lower().rstrip('.')):
        raise ConfigError('Traefik-API requires a DNS name or IPv4 host in server.public_url')
