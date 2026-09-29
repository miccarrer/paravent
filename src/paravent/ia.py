"""Optional local AI (Ollama), used only when configured.

Nothing depends on it: every feature works without it, just without the
AI's suggestion. The AI receives *non-anonymized* text, so the configured
server must resolve to this machine or the local network; a public address
is refused on every call, with no way around it.

The AI only proposes. Callers check its answers in code (see the design
notes, § 6 ter): the JSON shape here, then dates, folders or the presence of
a proposed entity in the source text.

Configuration, in ``config.toml`` in the user's config directory::

    [ia]
    url = "http://localhost:11434"
    modele = "gemma4:8b-16k"
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 120
ATTEMPTS = 2


class IAError(RuntimeError):
    pass


@dataclass
class Config:
    url: str
    model: str
    timeout: float = DEFAULT_TIMEOUT


def config_path() -> Path:
    if override := os.environ.get("PARAVENT_CONFIG"):
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "paravent" / "config.toml"


def load_config(path: Path | None = None) -> Config | None:
    """Return the AI configuration, or None when no AI is configured."""
    path = path or config_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise IAError(f"Configuration illisible ({path}) : {error}") from error
    section = data.get("ia")
    if not section:
        return None
    try:
        return Config(str(section["url"]).rstrip("/"), str(section["modele"]),
                      float(section.get("delai", DEFAULT_TIMEOUT)))
    except (KeyError, ValueError) as error:
        raise IAError(f"Section [ia] incomplète dans {path} : il faut « url » et « modele ».") from error


def check_local(url: str) -> None:
    """Refuse any server that is not on this machine or the local network."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise IAError(f"Adresse de l'IA invalide : {url}")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise IAError(f"Serveur d'IA introuvable : {parsed.hostname} ({error})") from error
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not (address.is_loopback or address.is_private or address.is_link_local):
            raise IAError(
                f"L'IA doit tourner sur cette machine ou sur le réseau local : {parsed.hostname} "
                f"désigne {address}, une adresse publique. Les documents ne sont jamais envoyés hors du réseau local."
            )


def installed_models(config: Config) -> list[str]:
    data = _request(config, "/api/tags", None)
    return [model["name"] for model in data.get("models", [])]


def ask(config: Config, system: str, prompt: str, schema: dict) -> dict:
    """Ask for a JSON object matching ``schema`` (flat: string and string-array fields).

    Ollama does not enforce ``format`` with every model (qwen3.5 ignores it),
    so the expected keys are also spelled out in the instructions, and a
    wrong answer is sent back once with what was wrong about it.
    """
    template = json.dumps({key: [] if spec.get("type") == "array" else "…"
                           for key, spec in schema.get("properties", {}).items()}, ensure_ascii=False)
    messages = [
        {"role": "system", "content": f"{system}\nRéponds uniquement par un objet JSON de la forme {template}, "
                                      "avec exactement ces clés."},
        {"role": "user", "content": prompt},
    ]
    problem = ""
    for _ in range(ATTEMPTS):
        payload = {"model": config.model, "stream": False, "think": False, "options": {"temperature": 0},
                   "format": schema, "messages": messages}
        content = _request(config, "/api/chat", payload).get("message", {}).get("content", "")
        try:
            answer = json.loads(content)
        except json.JSONDecodeError:
            problem = "réponse qui n'est pas du JSON"
        else:
            problem = _shape_problem(answer, schema)
            if not problem:
                return answer
        messages = [*messages, {"role": "assistant", "content": content},
                    {"role": "user", "content": f"Réponse refusée : {problem}. Recommence, sous la forme {template}."}]
    raise IAError(f"Réponse de l'IA inutilisable ({problem}).")


def _shape_problem(answer: object, schema: dict) -> str:
    if not isinstance(answer, dict):
        return "pas un objet JSON"
    properties = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in answer:
            return f"champ « {key} » absent"
    for key, spec in properties.items():
        if key not in answer:
            continue
        value = answer[key]
        if spec.get("type") == "string" and not isinstance(value, str):
            return f"champ « {key} » : texte attendu"
        if spec.get("type") == "array" and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            return f"champ « {key} » : liste de textes attendue"
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect could lead off the local network, past check_local().
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirection refusée", headers, fp)


_opener = urllib.request.build_opener(_NoRedirect)


def _request(config: Config, path: str, payload: dict | None) -> dict:
    check_local(config.url)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(config.url + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with _opener.open(request, timeout=config.timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        raise IAError(f"Le serveur d'IA a répondu {error.code} : {detail}") from error
    except (OSError, ValueError) as error:
        raise IAError(f"Serveur d'IA injoignable ({config.url}) : {error}") from error
