# redbot

Backend REST API for a WhatsApp chatbot focused on menstrual cycle conversations.

## Project Structure

- `redbot-be/`: Django + Django REST Framework backend

## Quick Start (`macOS`)

1. Go to backend folder:
   - `cd redbot-be`
2. Install dependencies:
   - `python3 -m pip install -r requirements.txt`
3. Configure environment:
   - `cp .env.example .env`
   - Fill required env vars (AI + webhook + throttle)
4. Run migrations:
   - `python3 manage.py migrate`
5. Run server:
   - `python3 manage.py runserver`

## API Docs (Swagger/OpenAPI)

- OpenAPI schema JSON: `GET /api/schema/`
- Swagger UI: `GET /api/docs/swagger/`
- Optional static export:
  - `python3 manage.py spectacular --file openapi-schema.yaml`

## Auth

No login/logout is required for chatbot endpoints in this project.

- `POST /api/chatbot/mode/` is public (rate-limited)
- `GET /api/chatbot/webhooks/whatsapp/` is public for Meta verification
- `POST /api/chatbot/webhooks/whatsapp/` uses a shared bearer token (`WHATSAPP_WEBHOOK_TOKEN`)

## Rate Limiting (Production)

Enabled throttles:
- anon/user throttles
- scope throttles for chatbot mode endpoint and webhook

Env-configurable rates in `.env`:
- `THROTTLE_ANON`
- `THROTTLE_USER`
- `THROTTLE_CHATBOT_GENERAL`
- `THROTTLE_AI_QNA`
- `THROTTLE_PRESET`
- `THROTTLE_WHATSAPP_WEBHOOK`

## API Endpoints

Base URL: `http://127.0.0.1:8000/api/chatbot/`

- `POST /mode/` (single chatbot endpoint for all modes, including reset command in preset flow)
- `GET /webhooks/whatsapp/` (Meta webhook verification)
- `POST /webhooks/whatsapp/` (provider webhook inbound)

---

## Mode Endpoint (`POST /api/chatbot/mode/`)

This is the single public chatbot endpoint.

### AI QnA Example

Request:
- `{ "mode": "ai_qna", "user_id": "628123456789", "prompt": "Apa itu siklus menstruasi normal?" }`

Response:
- `{ "mode": "ai_qna", "response": "Siklus menstruasi normal biasanya 21-35 hari." }`

### Preset Interaction Example (step-by-step)

1) Start interaction
- Request:
  - `{ "mode": "preset_interaction", "user_id": "628123456789" }`
- Response:
  - `{ "mode": "preset_interaction", "state": "awaiting_menstruating", "response": "Apakah kamu sedang menstruasi sekarang?" }`

2) User answers no
- Request:
  - `{ "mode": "preset_interaction", "user_id": "628123456789", "message": "no" }`
- Response:
  - `{ "mode": "preset_interaction", "state": "awaiting_last_period_date", "response": "Kapan hari pertama haid terakhir kamu? (format DD/MM/YYYY, contoh: 28/01/1970)" }`

3) User gives last period date
- Request:
  - `{ "mode": "preset_interaction", "user_id": "628123456789", "message": "01/04/2026" }`
- Response:
  - `{ "mode": "preset_interaction", "state": "awaiting_has_ttd", "response": "Apakah kamu punya pil TTD saat ini?" }`

4) User has no pill
- Request:
  - `{ "mode": "preset_interaction", "user_id": "628123456789", "message": "no" }`
- Response:
  - `{ "mode": "preset_interaction", "state": "awaiting_reminder_hour", "response": "Kamu bisa dapatkan TTD di Puskesmas/Posyandu ya! Siap, Aku akan atur jadwal minum TTD-mu, mau diingatkan jam berapa? (format angka 24 jam, contoh: 16 atau 8 atau 20)" }`

5) User sets reminder hour
- Request:
  - `{ "mode": "preset_interaction", "user_id": "628123456789", "message": "20" }`
- Response:
  - `{ "mode": "preset_interaction", "state": "completed", "response": "Pengingat TTD berhasil dibuat untuk 90 hari ke depan.", "ics_file": { "filename": "ttd-reminder-628123456789.ics", "content_type": "text/calendar", "content_base64": "..." }, "saved_data": { "user_id": "628123456789", "period_end_date": "06/04/2026", "has_ttd_pill": false, "reminder_hour_24": 20 } }`

### Preset Validation Error Example

Request:
- `{ "mode": "preset_interaction", "user_id": "628123456789", "message": "2026-04-01" }`

Response (`400`):
- `{ "mode": "preset_interaction", "state": "awaiting_last_period_date", "error": "Format tanggal tidak valid. Gunakan DD/MM/YYYY, contoh: 28/01/1970." }`

### Preset Reset Command Example (within mode endpoint)

Users can restart preset flow by sending special messages:
- `reset`
- `restart`

Request:
- `{ "mode": "preset_interaction", "user_id": "628123456789", "message": "reset" }`

Response:
- `{ "mode": "preset_interaction", "state": "awaiting_menstruating", "response": "Data kamu sudah direset. Yuk mulai lagi dari awal: Apakah kamu sedang menstruasi sekarang?", "action": "reset" }`

## WhatsApp Webhook (Provider Style)

### GET verification

`GET /api/chatbot/webhooks/whatsapp/?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=12345`

- If token matches `WHATSAPP_WEBHOOK_VERIFY_TOKEN`, returns `12345`.

### POST inbound message

Endpoint: `POST /api/chatbot/webhooks/whatsapp/`

Header:
- `Authorization: Bearer <WHATSAPP_WEBHOOK_TOKEN>`

Payload shape (WhatsApp Cloud API style):
- `entry[0].changes[0].value.messages[0].from`
- `entry[0].changes[0].value.messages[0].text.body`

Example payload:
- `{ "entry": [{ "changes": [{ "value": { "messages": [{ "from": "628123456789", "text": { "body": "ai: apakah menstruasi normal 5 hari?" } }] } }] }] }`

Mode routing in webhook message text:
- If message starts with `ai:` -> routed to AI mode
- Otherwise -> routed to preset interaction mode

## Error Handling

Validation errors handled for:
- invalid `yes/no` answer
- invalid date format (must be `DD/MM/YYYY`)
- invalid reminder hour (must be integer `0..23`)
- repeated invalid input in preset flow (`hint` appears after 3 consecutive mistakes and suggests typing `reset` or `restart`)
- invalid webhook payload
- invalid/missing webhook bearer token

## Data Storage

Stored in SQLite through Django models:
- `ChatbotUser` for user state and collected data
- `InteractionLog` for audit trail of user interactions

## Logging

Two logging mechanisms are implemented:

1. **Database interaction logs** (`InteractionLog`) for analysis.
2. **File logging** to `redbot-be/chatbot.log` for runtime observability.

## Testing

Run tests:
- `python3 manage.py test`