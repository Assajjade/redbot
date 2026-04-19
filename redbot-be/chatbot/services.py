import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


YES_INPUTS = {"yes", "y", "iya", "ya", "true", "1"}
NO_INPUTS = {"no", "n", "tidak", "false", "0"}


@dataclass
class ICSPayload:
    filename: str
    content_base64: str
    content_type: str = "text/calendar"


class ExternalAIServiceError(Exception):
    pass


class InputValidationError(Exception):
    pass


def normalize_yes_no(value: str):
    cleaned = (value or "").strip().lower()
    if cleaned in YES_INPUTS:
        return True
    if cleaned in NO_INPUTS:
        return False
    return None


def parse_ddmmyyyy(value: str):
    try:
        return datetime.strptime((value or "").strip(), "%d/%m/%Y").date()
    except ValueError as exc:
        raise InputValidationError(
            "Format tanggal tidak valid. Gunakan DD/MM/YYYY, contoh: 28/01/1970."
        ) from exc


def parse_hour_24(value: str):
    raw = (value or "").strip()
    if not raw.isdigit():
        raise InputValidationError(
            "Format jam tidak valid. Gunakan angka 24 jam dari 0 sampai 23 (contoh: 8, 16, 20)."
        )
    hour = int(raw)
    if hour < 0 or hour > 23:
        raise InputValidationError(
            "Jam di luar rentang. Gunakan angka 24 jam dari 0 sampai 23."
        )
    return hour


def get_period_end_date(last_period_start):
    return last_period_start + timedelta(days=5)


def generate_ics_payload(user_id: str, hour: int):
    now = timezone.now()
    start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if start <= now:
        start = start + timedelta(days=1)

    dtstamp = now.strftime("%Y%m%dT%H%M%SZ")
    dtstart = start.strftime("%Y%m%dT%H%M%S")
    uid = f"ttd-{user_id}-{int(now.timestamp())}@redbot"

    ics_content = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Redbot//TTD Reminder//ID",
            "CALSCALE:GREGORIAN",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;TZID={settings.TIME_ZONE}:{dtstart}",
            "RRULE:FREQ=DAILY;COUNT=90",
            "SUMMARY:Minum TTD",
            "DESCRIPTION:Pengingat harian minum TTD selama 90 hari",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )

    filename = f"ttd-reminder-{user_id}.ics"
    encoded = base64.b64encode(ics_content.encode("utf-8")).decode("utf-8")
    return ICSPayload(filename=filename, content_base64=encoded)


def ask_external_ai(prompt: str):
    if not settings.AI_API_URL:
        raise ExternalAIServiceError("AI_API_URL is not configured.")

    headers = {"Content-Type": "application/json"}
    if settings.AI_API_KEY:
        headers["Authorization"] = f"Bearer {settings.AI_API_KEY}"

    payload = {"prompt": prompt}

    try:
        response = requests.post(
            settings.AI_API_URL,
            json=payload,
            headers=headers,
            timeout=settings.AI_API_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.exception("Failed to call external AI API")
        raise ExternalAIServiceError("Failed to connect to external AI service.") from exc

    if response.status_code >= 400:
        logger.warning("External AI API returned error %s: %s", response.status_code, response.text)
        raise ExternalAIServiceError(
            f"External AI service returned HTTP {response.status_code}."
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise ExternalAIServiceError("External AI service response is not valid JSON.") from exc

    answer = data.get("response") or data.get("answer") or data.get("text")
    if not answer:
        raise ExternalAIServiceError(
            "External AI service response does not contain response/answer/text field."
        )

    return answer


def parse_webhook_mode_and_message(message_text: str):
    text = (message_text or "").strip()
    lowered = text.lower()
    if lowered.startswith("ai:"):
        return "ai_qna", text[3:].strip()
    return "preset_interaction", text


def extract_whatsapp_message(payload: dict):
    try:
        entry = payload.get("entry", [])[0]
        change = entry.get("changes", [])[0]
        value = change.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            raise InputValidationError("No WhatsApp message found in webhook payload.")
        message = messages[0]
        sender = message.get("from")
        text_body = ((message.get("text") or {}).get("body") or "").strip()
        if not sender:
            raise InputValidationError("Webhook payload missing sender id.")
        return {"user_id": sender, "message": text_body}
    except (IndexError, AttributeError, TypeError) as exc:
        raise InputValidationError("Invalid WhatsApp webhook payload format.") from exc

def send_whatsapp_message(to_number: str, message_text: str):
    phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
    access_token = settings.WHATSAPP_WEBHOOK_TOKEN
    
    if not phone_number_id or not access_token:
        logger.error("Kredensial WhatsApp belum dikonfigurasi di settings.")
        return

    url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message_text},
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        logger.info(f"Pesan WA berhasil dikirim ke {to_number}")
    except requests.RequestException as exc:
        logger.error(f"Gagal mengirim pesan WA ke {to_number}: {response.text if 'response' in locals() else exc}")