import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from paravent import ia, ranger

SCHEMA = {"type": "object", "properties": {"nom": {"type": "string"}, "liste": {"type": "array"}},
          "required": ["nom"]}


@pytest.fixture
def ollama():
    """A fake Ollama server on 127.0.0.1 that replays the queued chat answers."""
    answers: list[str] = []
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._reply({"models": [{"name": "petit:4b"}]})

        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self._reply({"message": {"role": "assistant", "content": answers.pop(0)}})

        def _reply(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = ia.Config(f"http://127.0.0.1:{server.server_port}", "petit:4b", timeout=5)
    yield config, answers, received
    server.shutdown()


def test_no_config_means_no_ai(tmp_path):
    assert ia.load_config(tmp_path / "absent.toml") is None
    (tmp_path / "vide.toml").write_text("[autre]\n")
    assert ia.load_config(tmp_path / "vide.toml") is None


def test_config_is_read(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[ia]\nurl = "http://localhost:11434/"\nmodele = "qwen3.5:4b-16k"\n')
    assert ia.load_config(path) == ia.Config("http://localhost:11434", "qwen3.5:4b-16k")


def test_incomplete_config_is_an_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[ia]\nurl = "http://localhost:11434"\n')
    with pytest.raises(ia.IAError):
        ia.load_config(path)


@pytest.mark.parametrize("url", ["http://localhost:11434", "http://127.0.0.1:11434", "http://192.168.1.1:11434",
                                 "http://10.0.0.5", "http://[::1]:11434"])
def test_local_servers_are_accepted(url):
    ia.check_local(url)


@pytest.mark.parametrize("url", ["http://8.8.8.8:11434", "https://1.1.1.1", "ftp://localhost", "localhost:11434"])
def test_public_or_odd_servers_are_refused(url):
    with pytest.raises(ia.IAError):
        ia.check_local(url)


def test_hostname_resolving_to_public_address_is_refused(monkeypatch):
    public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: public)
    with pytest.raises(ia.IAError, match="adresse publique"):
        ia.check_local("https://ia.exemple.org")


def test_ask_returns_checked_json(ollama):
    config, answers, received = ollama
    answers.append('{"nom": "Dupont", "liste": ["a"]}')
    assert ia.ask(config, "consigne", "texte", SCHEMA) == {"nom": "Dupont", "liste": ["a"]}
    request = received[0]
    assert request["think"] is False and request["options"]["temperature"] == 0
    assert '"nom"' in request["messages"][0]["content"]  # expected keys spelled out


def test_wrong_answer_is_sent_back_once(ollama):
    config, answers, received = ollama
    answers.extend(['{"name": "Dupont"}', '{"nom": "Dupont"}'])
    assert ia.ask(config, "consigne", "texte", SCHEMA) == {"nom": "Dupont"}
    retry = received[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": '{"name": "Dupont"}'}
    assert "« nom » absent" in retry[-1]["content"]


def test_unusable_answers_raise(ollama):
    config, answers, _ = ollama
    answers.extend(["pas du json", '{"nom": 3}'])
    with pytest.raises(ia.IAError, match="texte attendu"):
        ia.ask(config, "consigne", "texte", SCHEMA)


def test_unreachable_server_raises():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(ia.IAError, match="injoignable"):
        ia.installed_models(ia.Config(f"http://127.0.0.1:{port}", "x", timeout=2))


def test_installed_models(ollama):
    config, _, _ = ollama
    assert ia.installed_models(config) == ["petit:4b"]


def proposal(ollama, answer: dict, folders=("CAF", "Banque/Relevés")):
    config, answers, _ = ollama
    answers.append(json.dumps(answer))
    return ranger.propose(config, "---\noriginal: x\n---\n\n<!-- page 1 · text -->\n\nBonjour", list(folders))


def test_filing_proposal_matches_existing_folder(ollama):
    result = proposal(ollama, {"type_document": "Attestation", "emetteur": "CAF", "date": "2026-03-12",
                               "titre": "Attestation de paiement", "dossier": "caf"})
    assert (result.folder, result.new_folder) == ("CAF", False)
    assert result.name == "2026-03-12 Attestation de paiement"


def test_filing_proposal_flags_new_folder_and_drops_bad_date(ollama):
    result = proposal(ollama, {"type_document": "Facture", "emetteur": "EDF", "date": "12 mars",
                               "titre": "", "dossier": "Énergie/EDF/"})
    assert (result.folder, result.new_folder) == ("Énergie/EDF", True)
    assert result.date is None and result.name == "Facture"


def test_filing_proposal_rejects_implausible_year(ollama):
    result = proposal(ollama, {"type_document": "Courrier", "emetteur": "X", "date": "2226-01-01",
                               "titre": "Courrier", "dossier": ""})
    assert result.date is None
    assert (result.folder, result.new_folder) == ("", False)


def test_prompt_sees_text_without_markup(ollama):
    config, _, received = ollama
    proposal(ollama, {"type_document": "a", "emetteur": "b", "date": "", "titre": "c", "dossier": "CAF"})
    prompt = received[0]["messages"][1]["content"]
    assert "Bonjour" in prompt and "<!--" not in prompt and "original:" not in prompt
    assert "- Banque/Relevés" in prompt
