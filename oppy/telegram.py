"""Plain-text Telegram notifications. No Markdown or HTML interpretation."""

import json
import os
import re
import time
from datetime import date, datetime
from urllib.request import Request

from .http_client import RequestFailure, request
from .notification_text import ENGLISH
from .outbox import DeliveryUnavailable
from .runtime import LISTING_URL

TITLES = {
    "summary": "📋 Resumo do estado salvo",
    "baseline": "📋 Resumo inicial · Santarém",
    "new_selection": "📢 Nova oportunidade · Seleção publicada",
    "new_course": "🆕 Nova oportunidade · Curso publicado",
    "registration_announced": "🗓️ Nova oportunidade · Inscrições futuras",
    "registration_opened": "🟢 Inscrições abertas",
    "registration_reopened": "🔄 Inscrições reabertas",
    "registration_closed": "🔒 Alteração · Inscrições encerradas",
    "places_filled": "🈵 Alteração · Vagas preenchidas",
    "dates_changed": "📅 Alteração · Datas atualizadas",
    "new_document": "📄 Alteração · Novo documento",
    "amendment": "✏️ Alteração · Retificação",
    "monitoring_failure": "⚠️ Falha de monitoramento",
    "monitoring_recovered": "✅ Monitoramento restabelecido",
    "test": "🧪 Teste do Oppy",
}
DATES = {"registration_start": "Início das inscrições", "registration_end": "Fim das inscrições",
         "course_start": "Início do curso", "course_end": "Fim do curso"}
STATES = {"open": "Inscrições abertas", "closed": "Inscrições encerradas", "full": "Vagas preenchidas"}


def configured_language():
    return os.getenv("BEACON_NOTIFICATION_LANGUAGE", "").strip().lower() or "pt-br"


def _text(value, language):
    return ENGLISH.get(value, value) if language == "en" else value


def format_date(value, language="pt-br"):
    if "T" in value:
        return datetime.fromisoformat(value).strftime(_text("%d/%m/%Y às %H:%M", language))
    return date.fromisoformat(value).strftime(_text("%d/%m/%Y", language))


def course_block(course, notice, url, *, category=None, changes=None, language="pt-br"):
    labels = STATES | {"future": "Inscrições futuras"}
    mode = course.get("registration_mode")
    if mode and mode.casefold() in ("inscrição online", "inscrição presencial"):
        mode = _text(mode.split()[-1].lower(), language)
    fields = [("Curso", course.get("title")), ("Seleção", notice),
              ("Situação", _text(labels.get(category or course.get("registration")), language)),
              ("Modalidade da inscrição", mode), ("Local", course.get("venue"))]
    fields = [(_text(label, language), value) for label, value in fields]
    for key, label in DATES.items():
        label = _text(label, language)
        if changes is None:
            value = course.get(key)
            if value:
                fields.append((label, format_date(value, language)))
        elif key in changes[1]:
            old, new = changes[0].get(key), changes[1][key]
            if old != new:
                values = [format_date(v, language) for v in (old, new) if v]
                if values:
                    if old and not new:
                        label += _text(" (valor anterior)", language)
                    fields.append((label, " → ".join(values)))
    fields.append((_text("Página da seleção", language), url))
    return "\n".join(f"{label}: {value}" for label, value in fields if value)


def blocks(event, language="pt-br"):
    def text(value):
        return _text(value, language)

    kind = event["kind"]
    lines = [text(TITLES[kind])]
    if kind != "test":
        lines.append(text("Fonte: Senac Pará PSG · Santarém"))
    if event.get("detail_time"):
        stamp = datetime.fromisoformat(event["detail_time"])
        lines.append(text("Detalhes atualizados em ") + stamp.strftime(text("%d/%m/%Y às %H:%M") + " %z"))
    after = event.get("after")
    data = after if isinstance(after, dict) else {}
    summary = event.get("summary")
    if summary is not None:
        counts = summary["counts"]
        lines.append(text("Seleções: {} · Cursos: {}").format(summary["selections"], sum(counts.values())))
        lines.append(text("Abertas: {} · Futuras: {} · Encerradas: {}").format(
            counts["open"], counts["future"], counts["closed"]))
        lines.append(text("Vagas preenchidas: {} · Desconhecidas: {}").format(counts["full"], counts["unknown"]))
        return ["\n".join(lines),
                *(course_block(c, c["notice"], c["url"], category=c["category"], language=language)
                  for c in summary["eligible"]),
                text("Listagem geral: ") + LISTING_URL]
    if kind == "baseline":
        # Old pending baselines only stored totals and open titles.
        lines.append(text("Seleções: {} · Cursos: {}").format(data["selections"], data["courses"]))
        return ["\n".join(lines), *(text("Curso com inscrições abertas: ") + title for title in data["open_courses"]),
                text("Listagem geral: ") + LISTING_URL]
    if kind == "monitoring_failure":
        lines.append(text("Coletas consecutivas sem sucesso: {}.").format(data["consecutive"]))
        reason = data["reason"]
        if reason.startswith("Unresolved course identity:"):
            reason = text("Identidade de curso ambígua: ") + reason.partition(":")[2].strip()
        elif reason.startswith("HTTP ") and reason[5:].isdigit():
            reason = text("Resposta ") + reason
        else:
            reasons = {"HTTP timeout": "Tempo limite da consulta excedido",
                       "Truncated HTTP response": "Resposta HTTP incompleta",
                       "HTTP connection failed": "Falha na conexão",
                       "Santarém panel absent": "Painel de Santarém ausente"}
            reason = text(reasons.get(reason, "Coleta sem resultado confiável. Consulte o status local."))
        lines.append(text("Motivo: {}").format(reason))
    elif kind in ("new_document", "amendment") and data.get("label"):
        lines.append(data["label"])
    if kind in ("new_document", "amendment") and data.get("url"):
        lines.append(text("Documento: ") + data["url"])
    states = event.get("selection_courses")
    if states is None:
        state = event.get("course_state") or (data if kind in ("new_course", "new_selection") else {})
        state = dict(state)
        if event.get("title"):
            state["title"] = event["title"]
        if kind == "registration_announced" and isinstance(after, str):
            state["registration_start"] = after
        states = [state]
    changes = (event.get("before") or {}, data) if kind == "dates_changed" else None
    parts = [course_block(c, event.get("notice"), event.get("url"), changes=changes, language=language)
             for c in states]
    return ["\n".join(lines), *(part for part in parts if part)]


def message(event, language="pt-br"):
    return "\n\n".join(blocks(event, language))


def pages(event, language="pt-br"):
    parts = blocks(event, language)
    def size(text):
        return len(text.encode("utf-16-le")) // 2

    if size("\n\n".join(parts)) <= 4096:
        return ["\n\n".join(parts)]
    # Pack again only if numbering crosses a digit boundary (e.g. 9 to 10 pages).
    digits = 1
    while True:
        result = []
        current = ""
        for part in parts:
            prefix_size = len(str(len(result) + 1)) + digits + 2
            combined = current + "\n\n" + part if current else part
            if current and size(combined) + prefix_size > 4096:
                result.append(current)
                current = part
            else:
                current = combined
            if size(current) + len(str(len(result) + 1)) + digits + 2 > 4096:
                raise DeliveryUnavailable("Um bloco excede o limite de uma mensagem do Telegram")
        result.append(current)
        if len(str(len(result))) == digits:
            return [f"{i}/{len(result)}\n{text}" for i, text in enumerate(result, 1)]
        digits = len(str(len(result)))


class Telegram:
    def __init__(self, token, chat, *, transport=request, language="pt-br"):
        self._token = token
        self._chat = chat
        self._transport = transport
        self._language = language
        self.resume_at = 0

    @classmethod
    def configured(cls):
        token = os.getenv("BEACON_TELEGRAM_TOKEN", "").strip()
        chat = os.getenv("BEACON_TELEGRAM_CHAT_ID", "").strip()
        return cls(token, chat, language=configured_language()) if token and chat else None

    def send(self, event):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", self._token):
            raise DeliveryUnavailable("Configuração de credencial inválida")
        if time.monotonic() < self.resume_at:
            raise DeliveryUnavailable("Telegram solicitou uma pausa")
        for text in pages(event, self._language):
            self._send_page(text)
        return True

    def _send_page(self, text):
        body = json.dumps({"chat_id": self._chat, "text": text,
                           "link_preview_options": {"is_disabled": True}}).encode()
        req = Request("https://api.telegram.org/bot" + self._token + "/sendMessage",
                      data=body, headers={"Content-Type": "application/json"})
        try:
            raw, _ = self._transport(req, 64 * 1024)
            response = json.loads(raw)
            if (isinstance(response, dict) and response.get("ok") is True
                    and isinstance(response.get("result"), dict)
                    and isinstance(response["result"].get("message_id"), int)):
                return
        except RequestFailure as error:
            self.resume_at = time.monotonic() + error.retry_after
        except (ValueError, OSError):
            pass
        raise DeliveryUnavailable("Telegram não confirmou a entrega")
