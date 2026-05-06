import base64
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests
from openai import OpenAI
from django.conf import settings
from django.utils import timezone


logger = logging.getLogger(__name__)


YES_INPUTS = {"yes", "y", "iya", "ya", "true", "1"}
NO_INPUTS = {"no", "n", "tidak", "false", "0"}

ANEMIA_MENSTRUASI_SYSTEM_PROMPT = (
    "Kamu adalah asisten kesehatan reproduksi yang hanya boleh membahas dua topik: "
    "anemia dan menstruasi. Selalu jawab dalam Bahasa Indonesia yang jelas, ramah, dan mudah dipahami. "
    "Jika pertanyaan di luar topik anemia/menstruasi, tolak dengan sopan dan arahkan pengguna "
    "untuk bertanya seputar anemia atau menstruasi. "
    "Berikan edukasi umum yang aman dan tidak menggantikan diagnosis dokter. "
    "Jika ada gejala berat/berbahaya, sarankan segera konsultasi ke tenaga kesehatan."
)


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


def _extract_openai_text(response) -> str:
    output_text = (getattr(response, "output_text", "") or "").strip()
    if output_text:
        return output_text

    output = getattr(response, "output", []) or []
    chunks = []
    for item in output:
        for content in getattr(item, "content", []) or []:
            if getattr(content, "type", None) == "output_text":
                text = (getattr(content, "text", "") or "").strip()
                if text:
                    chunks.append(text)
    return "\n".join(chunks).strip()


def ask_external_ai(prompt: str):
    if not settings.OPENAI_API_KEY:
        raise ExternalAIServiceError("OPENAI_API_KEY is not configured.")

    client = OpenAI(
        api_key=settings.OPENAI_API_KEY, timeout=settings.OPENAI_API_TIMEOUT
    )

    try:
        response = client.responses.create(
            model=settings.OPENAI_MODEL,
            input=[
                {
                    "role": "system",
                    "content": [
                        {"type": "input_text", "text": ANEMIA_MENSTRUASI_SYSTEM_PROMPT}
                    ],
                },
                {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
            ],
        )
    except Exception as exc:
        logger.exception("Failed to call OpenAI API")
        raise ExternalAIServiceError("Failed to connect to OpenAI service.") from exc

    answer = _extract_openai_text(response)
    if not answer:
        raise ExternalAIServiceError(
            "OpenAI response does not contain any answer text."
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
    fonnte_token = os.getenv("FONNTE_TOKEN")
    
    if not fonnte_token:
        logger.error("Kredensial FONNTE_TOKEN belum dikonfigurasi di file .env.")
        return

    url = "https://api.fonnte.com/send"
    headers = {
        "Authorization": fonnte_token
    }
    payload = {
        "target": to_number,
        "message": message_text
    }

    try:
        response = requests.post(url, data=payload, headers=headers, timeout=10)
        response.raise_for_status()
        logger.info(f"Pesan Fonnte berhasil dikirim ke {to_number}")
    except requests.RequestException as exc:
        logger.error(f"Gagal mengirim pesan Fonnte ke {to_number}: {exc}")


def send_whatsapp_document(to_number: str, filename: str, content_base64: str, mime_type: str = "text/calendar"):
    fonnte_token = os.getenv("FONNTE_TOKEN")
    
    if not fonnte_token:
        logger.error("Kredensial FONNTE_TOKEN belum dikonfigurasi.")
        return

    url = "https://api.fonnte.com/send"
    headers = {
        "Authorization": fonnte_token
    }
    
    # Dekode base64 agar menjadi bentuk file biner
    import base64
    file_bytes = base64.b64decode(content_base64)
    
    payload = {
        "target": to_number,
    }
    files = {
        "file": (filename, file_bytes, mime_type)
    }
    
    try:
        # Fonnte bisa menerima dokumen via upload form-data
        response = requests.post(url, headers=headers, data=payload, files=files, timeout=15)
        response.raise_for_status()
        logger.info(f"Dokumen ICS Fonnte berhasil dikirim ke {to_number}")
    except requests.RequestException as exc:
        logger.error(f"Gagal mengirim dokumen Fonnte ke {to_number}: {exc}")