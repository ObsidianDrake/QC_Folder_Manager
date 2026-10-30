"""Strict administrator-owned configuration; never accept paths from HTTP requests."""
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
        config = yaml.load(source.read_text(), Loader=UniqueLoader)
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError("Cannot read YAML configuration") from e
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
    app.setdefault("port", 9080)
    app.setdefault("workers", 2)
    app.setdefault("session_hours", 8)
    for name, lo, hi in (("port", 1, 65535), ("workers", 1, 8), ("session_hours", 1, 24)):
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
        if host["transport"] == "ssh":
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", host.get("hostname", "")):
                raise ConfigError("Invalid SSH hostname/alias")
            ssh = host.setdefault("ssh", {})
            if not isinstance(ssh, dict) or set(ssh) - {"user_mode", "python", "timeout_seconds"}:
                raise ConfigError("SSH supports user_mode, python and timeout_seconds only")
            if ssh.setdefault("user_mode", "current_user") != "current_user":
                raise ConfigError("SSH must use the current service user")
            executable = ssh.setdefault("python", "python3")
            if not isinstance(executable, str) or not re.fullmatch(r"/?[A-Za-z0-9_./-]+", executable) or executable.startswith("-"):
                raise ConfigError("Invalid remote Python executable")
            timeout = ssh.setdefault("timeout_seconds", 1800)
            if type(timeout) is not int or not 10 <= timeout <= 86400:
                raise ConfigError("Invalid SSH timeout")
    projects = config.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ConfigError("At least one project is required")
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
    config["gitea"].setdefault("client_id_env", "QC_FOLDER_GITEA_CLIENT_ID")
    config["gitea"].setdefault("client_secret_env", "QC_FOLDER_GITEA_CLIENT_SECRET")
    config["gitea"].setdefault("scopes", "read:user read:organization")
    config["drone"].setdefault("token_env", "QC_FOLDER_DRONE_TOKEN")
    for section, keys in (("gitea", ("client_id_env", "client_secret_env")), ("drone", ("token_env",))):
        for key in keys:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config[section][key]):
                raise ConfigError("Invalid credential environment variable name")
    return config
