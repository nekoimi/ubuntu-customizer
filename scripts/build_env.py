"""Read build inputs and private runtime presets without evaluating shell code."""
import os
from pathlib import Path
import shlex
import sys
import urllib.request
from urllib.parse import unquote, urlsplit

if __package__:
    from .ntfy_notify import validate_settings
else:
    from ntfy_notify import validate_settings

LOCAL_ENV = Path(__file__).resolve().parents[1] / "config/local.env"
PROXY_KEYS = ("http_proxy", "https_proxy", "no_proxy")
NTFY_KEYS = ("NTFY_URL", "NTFY_TOPIC", "NTFY_TOKEN")
ALLOWED_KEYS = {"UBUNTU_INITIAL_PASSWORD", *PROXY_KEYS, *(key.upper() for key in PROXY_KEYS), *NTFY_KEYS}


def read_local_env(path):
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, separator, raw = line.partition("=")
            key = key.strip()
            if not separator or key not in ALLOWED_KEYS:
                raise ValueError("local.env accepts only password, HTTP_PROXY / HTTPS_PROXY / NO_PROXY and NTFY_URL / NTFY_TOPIC / NTFY_TOKEN settings")
            words = shlex.split(raw, comments=True)
            if len(words) > 1:
                raise ValueError("local.env values must be single shell-quoted values")
            values[key] = words[0] if words else ""
    return values


def password_input(env, path):
    value = env.get("UBUNTU_INITIAL_PASSWORD") or read_local_env(path).get("UBUNTU_INITIAL_PASSWORD", "")
    if not value or any(char in value for char in "\r\n\x00"):
        raise ValueError("Provide a nonempty single-line initial password via Secret or local.env")
    return value


def ntfy_settings(env, path=LOCAL_ENV):
    local = read_local_env(path)
    values = {key.removeprefix("NTFY_").lower(): env.get(key, local.get(key, "")) for key in NTFY_KEYS}
    return validate_settings(values) if any(values.values()) else None


def proxy_environment(env, path):
    local = read_local_env(path)
    result = {}
    for key in PROXY_KEYS:
        # An exported empty value disables this setting. Lowercase wins within a source.
        value = next((source[name] for source in (env, local)
                      for name in (key, key.upper()) if name in source), "")
        if any(char in value for char in "\r\n\x00"):
            raise ValueError("Proxy settings must be single-line values")
        if value and key != "no_proxy":
            try:
                parsed = urlsplit(value)
                valid = (parsed.scheme == "http" and parsed.hostname and parsed.port != 0
                         and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
                         and not any(char.isspace() for char in value))
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Use an http://host:port HTTP proxy URL (v2rayA HTTP listener); SOCKS is unsupported")
        result[key] = result[key.upper()] = value
    return result


def configure_proxy(env=os.environ, path=LOCAL_ENV):
    values = proxy_environment(env, path)
    env.update(values)
    proxies = {scheme: values[scheme + "_proxy"] for scheme in ("http", "https")
               if values[scheme + "_proxy"]}
    # Avoid stale discovery and OS-level proxy settings; local.env / env are authoritative.
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler(proxies)))
    return values


def proxy_secrets(values):
    secrets = set()
    for key in ("http_proxy", "https_proxy"):
        value = values.get(key)
        if value:
            secrets.add(value)
            parsed = urlsplit(value)
            for credential in (parsed.username, parsed.password):
                if credential:
                    secrets.update((credential, unquote(credential)))
    return secrets


def main():
    # setup-host.sh uses this launcher so APT sees the same settings as build.py.
    if len(sys.argv) < 3 or sys.argv[1] != "--exec":
        raise ValueError("Usage: python3 scripts/build_env.py --exec COMMAND [ARG ...]")
    configure_proxy()
    os.environ.pop("UBUNTU_INITIAL_PASSWORD", None)
    for key in NTFY_KEYS:
        os.environ.pop(key, None)
    os.execvp(sys.argv[2], sys.argv[2:])


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        sys.exit(str(error))
