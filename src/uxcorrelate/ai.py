"""The self-hosted AI service the chat will use (OpenAI-style chat API).

The AI never reads Aternity and never counts: it picks one of our reviewed
building blocks (`chat.TOOLS`), our code counts, and the AI words the answer.

Assumed, until `uxcorrelate verify --ai` has run against the live services:
the service speaks the OpenAI chat API (POST <base_url>/chat/completions,
GET <base_url>/models, tool calls in `choices[0].message.tool_calls`). Paths,
extra query values and the login header are settings (`AiService`), so a
service that differs needs config, not code. No token limit or temperature
is sent: some models refuse them.
"""

from __future__ import annotations

import json

import httpx

from .config import AiService
from .http import RestClient

LOGINS = ("bearer", "api-key")  # "Authorization: Bearer <key>" or an "api-key: <key>" header

class AiClient:
    def __init__(self, cfg: AiService, key: str = "", login: str = "",
                 transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.http = RestClient(f"AI service {cfg.name}", cfg.base_url, timeout=cfg.timeout, retries=1,
                               transport=transport)
        login = login or cfg.login or LOGINS[0]
        self.headers = {} if not key else (
            {"api-key": key} if login == "api-key" else {"Authorization": f"Bearer {key}"})

    def models(self) -> list[str]:
        """The model names the service lists."""
        data = self.http.get_json(self.cfg.models_path, params=self.cfg.query, headers=self.headers)
        items = data if isinstance(data, list) else next(
            (data[k] for k in ("data", "models") if isinstance(data.get(k), list)), [])
        names = [i if isinstance(i, str) else (i.get("id") or i.get("name")) for i in items
                 if isinstance(i, (str, dict))]
        return sorted(str(n) for n in names if n)

    def chat_raw(self, messages: list[dict], tools: list[dict] | None = None):
        """The service's whole reply, as sent."""
        body: dict = {"model": self.cfg.model, "messages": messages}
        if tools:
            body["tools"] = tools
        return self.http.post_json(self.cfg.chat_path, body, params=self.cfg.query, headers=self.headers)

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """The AI's reply message ({"content": ..., "tool_calls": [...]})."""
        return reply_message(self.chat_raw(messages, tools)) or {}


def reply_message(data) -> dict | None:
    """The reply message in a chat answer (None when the layout is not the OpenAI one)."""
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None
    return message if isinstance(message, dict) else None


def tool_call(message: dict) -> tuple[str, dict, str] | None:
    """The first tool the AI asked for: its name, its settings and the call's id."""
    calls = message.get("tool_calls") or []
    if not calls or not isinstance(calls[0], dict):
        return None
    fn = calls[0].get("function") or {}
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except ValueError:
            args = {"(not JSON)": args[:80]}
    return str(fn.get("name") or ""), args if isinstance(args, dict) else {}, str(calls[0].get("id") or "")
