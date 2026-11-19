"""Plain-text Telegram notifications. No Markdown or HTML interpretation."""

import json
import os
import re
import time
from datetime import date, datetime
from urllib.request import Request

from .http_client import RequestFailure, request
from .outbox import DeliveryUnavailable
from .runtime import LISTING_URL

TITLES = {
    "baseline": "Resumo inicial · Santarém",
    "new_selection": "Nova oportunidade · Seleção publicada",
    "new_course": "Nova oportunidade · Curso publicado",
    "registration_announced": "Nova oportunidade · Inscrições futuras",
    "registration_opened": "Inscrições abertas",
    "registration_reopened": "Inscrições reabertas",
    "registration_closed": "Alteração · Inscrições encerradas",
    "places_filled": "Alteração · Vagas preenchidas",
    "dates_changed": "Alteração · Datas atualizadas",
    "new_document": "Alteração · Novo documento",
    "amendment": "Alteração · Retificação",
    "monitoring_failure": "Falha de monitoramento",
    "monitoring_recovered": "Monitoramento restabelecido",
    "test": "Teste do OpportunityBeacon",
}
DATES = {"registration_start": "Início das inscrições", "registration_end": "Fim das inscrições",
         "course_start": "Início do curso", "course_end": "Fim do curso"}
STATES = {"open": "Inscrições abertas", "closed": "Inscrições encerradas", "full": "Vagas preenchidas"}


def format_date(value):
    if "T" in value:
        return datetime.fromisoformat(value).strftime("%d/%m/%Y às %H:%M")
    return date.fromisoformat(value).strftime("%d/%m/%Y")


def message(event):
    kind = event["kind"]
    lines = [TITLES[kind]]
    if kind != "test":
        lines.append("Fonte: Senac Pará PSG · Santarém")
    after = event.get("after")
    data = after if isinstance(after, dict) else {}
    state = event.get("course_state") or (data if kind in ("new_course", "new_selection") else {})
    for label, value in (("Seleção", event.get("notice")), ("Curso", event.get("title") or state.get("title")),
                         ("Situação", STATES.get(state.get("registration"))),
                         ("Inscrição", state.get("registration_mode"))):
        if value:
            lines.append(f"{label}: {value}")
    if kind == "baseline":
        lines.append(f"{data['selections']} seleção(ões), {data['courses']} curso(s).")
        if data["open_courses"]:
            lines.append("Com inscrições abertas: " + ", ".join(data["open_courses"]))
    elif kind == "monitoring_failure":
        lines.append(f"Coletas consecutivas sem sucesso: {data['consecutive']}.")
        reason = data["reason"]
        if reason.startswith("Unresolved course identity:"):
            reason = "Identidade de curso ambígua: " + reason.partition(":")[2].strip()
        elif reason.startswith("HTTP ") and reason[5:].isdigit():
            reason = "Resposta " + reason
        else:
            reason = {"HTTP timeout": "Tempo limite da consulta excedido",
                      "Truncated HTTP response": "Resposta HTTP incompleta",
                      "HTTP connection failed": "Falha na conexão",
                      "Santarém panel absent": "Painel de Santarém ausente"}.get(
                          reason, "Coleta sem resultado confiável. Consulte o status local.")
        lines.append(f"Motivo: {reason}")
    elif kind in ("new_document", "amendment") and data.get("label"):
        lines.append(data["label"])
    for field, label in DATES.items():
        value = state.get(field)
        if kind == "dates_changed" and field in data:
            old = (event.get("before") or {}).get(field)
            values = [format_date(v) if v else "não informado" for v in (old, data[field])]
            lines.append(f"{label}: {' → '.join(values)}")
        elif value:
            lines.append(f"{label}: {format_date(value)}")
    if kind == "registration_announced" and not state and isinstance(after, str):
        lines.append(f"Início das inscrições: {format_date(after)}")
    link = event.get("url") or (LISTING_URL if kind == "baseline" else None)
    if link:
        lines.append(link)
    # One message per event, including unusually large baseline summaries.
    text = "\n".join(lines)
    utf16 = text.encode("utf-16-le")
    if len(utf16) > 8192:
        return utf16[:8190].decode("utf-16-le", errors="ignore") + "…"
    return text


class Telegram:
    def __init__(self, token, chat, *, transport=request):
        self._token = token
        self._chat = chat
        self._transport = transport
        self.resume_at = 0

    @classmethod
    def configured(cls):
        token = os.getenv("BEACON_TELEGRAM_TOKEN", "").strip()
        chat = os.getenv("BEACON_TELEGRAM_CHAT_ID", "").strip()
        return cls(token, chat) if token and chat else None

    def send(self, event):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", self._token):
            raise DeliveryUnavailable("Configuração de credencial inválida")
        if time.monotonic() < self.resume_at:
            raise DeliveryUnavailable("Telegram solicitou uma pausa")
        body = json.dumps({"chat_id": self._chat, "text": message(event),
                           "link_preview_options": {"is_disabled": True}}).encode()
        req = Request("https://api.telegram.org/bot" + self._token + "/sendMessage",
                      data=body, headers={"Content-Type": "application/json"})
        try:
            raw, _ = self._transport(req, 64 * 1024)
            response = json.loads(raw)
            if (isinstance(response, dict) and response.get("ok") is True
                    and isinstance(response.get("result"), dict)
                    and isinstance(response["result"].get("message_id"), int)):
                return True
        except RequestFailure as error:
            self.resume_at = time.monotonic() + error.retry_after
        except (ValueError, OSError):
            pass
        raise DeliveryUnavailable("Telegram não confirmou a entrega")
