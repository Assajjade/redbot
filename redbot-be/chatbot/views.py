from django.conf import settings
from django.db import transaction
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import ChatbotUser, InteractionLog, PresetState
from .serializers import ModeDispatchSerializer, WhatsAppWebhookPayloadSerializer
from .services import (
    ExternalAIServiceError,
    InputValidationError,
    ask_external_ai,
    extract_whatsapp_message,
    generate_ics_payload,
    get_period_end_date,
    normalize_yes_no,
    parse_ddmmyyyy,
    parse_hour_24,
    parse_webhook_mode_and_message,
)
from .utils import log_interaction


RESET_COMMANDS = {"reset", "restart"}
RESET_HINT_MESSAGE = (
    "Kamu sudah 3x salah input. Untuk memulai ulang percakapan, "
    "ketik 'reset' atau 'restart'."
)


def reset_preset_user(user: ChatbotUser):
    user.preset_state = PresetState.AWAITING_MENSTRUATING
    user.invalid_input_count = 0
    user.is_currently_menstruating = None
    user.last_period_start_date = None
    user.period_end_date = None
    user.has_ttd_pill = None
    user.reminder_hour_24 = None
    user.save(
        update_fields=[
            "preset_state",
            "invalid_input_count",
            "is_currently_menstruating",
            "last_period_start_date",
            "period_end_date",
            "has_ttd_pill",
            "reminder_hour_24",
            "updated_at",
        ]
    )


def handle_ai_qna(user_id: str, prompt: str, endpoint_name: str):
    user, _ = ChatbotUser.objects.get_or_create(user_id=user_id)

    try:
        answer = ask_external_ai(prompt)
    except ExternalAIServiceError as exc:
        log_interaction(
            user=user,
            user_id=user_id,
            mode=InteractionLog.MODE_AI_QNA,
            endpoint=endpoint_name,
            user_message=prompt,
            bot_response=str(exc),
            status=InteractionLog.STATUS_ERROR,
        )
        return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    log_interaction(
        user=user,
        user_id=user_id,
        mode=InteractionLog.MODE_AI_QNA,
        endpoint=endpoint_name,
        user_message=prompt,
        bot_response=answer,
        status=InteractionLog.STATUS_SUCCESS,
    )
    return Response({"mode": "ai_qna", "response": answer}, status=status.HTTP_200_OK)


def advance_preset_flow(user: ChatbotUser, message: str):
    if user.preset_state == PresetState.AWAITING_MENSTRUATING:
        answer = normalize_yes_no(message)
        if answer is None:
            raise InputValidationError("Jawaban tidak valid. Balas dengan yes/no atau ya/tidak.")

        user.is_currently_menstruating = answer
        if answer:
            user.preset_state = PresetState.COMPLETED
            user.save(update_fields=["is_currently_menstruating", "preset_state", "updated_at"])
            return {
                "mode": "preset_interaction",
                "state": user.preset_state,
                "response": "Baik, silakan mulai minum TTD setelah menstruasimu selesai ya",
            }

        user.preset_state = PresetState.AWAITING_LAST_PERIOD_DATE
        user.save(update_fields=["is_currently_menstruating", "preset_state", "updated_at"])
        return {
            "mode": "preset_interaction",
            "state": user.preset_state,
            "response": "Kapan hari pertama haid terakhir kamu? (format DD/MM/YYYY, contoh: 28/01/1970)",
        }

    if user.preset_state == PresetState.AWAITING_LAST_PERIOD_DATE:
        last_period_start = parse_ddmmyyyy(message)
        user.last_period_start_date = last_period_start
        user.period_end_date = get_period_end_date(last_period_start)
        user.preset_state = PresetState.AWAITING_HAS_TTD
        user.save(
            update_fields=[
                "last_period_start_date",
                "period_end_date",
                "preset_state",
                "updated_at",
            ]
        )
        return {
            "mode": "preset_interaction",
            "state": user.preset_state,
            "response": "Apakah kamu punya pil TTD saat ini?",
        }

    if user.preset_state == PresetState.AWAITING_HAS_TTD:
        has_ttd = normalize_yes_no(message)
        if has_ttd is None:
            raise InputValidationError("Jawaban tidak valid. Balas dengan yes/no atau ya/tidak.")

        user.has_ttd_pill = has_ttd
        user.preset_state = PresetState.AWAITING_REMINDER_HOUR
        user.save(update_fields=["has_ttd_pill", "preset_state", "updated_at"])

        reminder_question = (
            "Siap, Aku akan atur jadwal minum TTD-mu, mau diingatkan jam berapa? (format angka 24 jam, contoh: 16 atau 8 atau 20)"
        )
        response_text = (
            reminder_question
            if has_ttd
            else "Kamu bisa dapatkan TTD di Puskesmas/Posyandu ya! " + reminder_question
        )
        return {
            "mode": "preset_interaction",
            "state": user.preset_state,
            "response": response_text,
        }

    if user.preset_state == PresetState.AWAITING_REMINDER_HOUR:
        reminder_hour = parse_hour_24(message)
        user.reminder_hour_24 = reminder_hour
        user.preset_state = PresetState.COMPLETED
        user.save(update_fields=["reminder_hour_24", "preset_state", "updated_at"])

        ics_payload = generate_ics_payload(user.user_id, reminder_hour)
        return {
            "mode": "preset_interaction",
            "state": user.preset_state,
            "response": "Pengingat TTD berhasil dibuat untuk 90 hari ke depan.",
            "ics_file": {
                "filename": ics_payload.filename,
                "content_type": ics_payload.content_type,
                "content_base64": ics_payload.content_base64,
            },
            "saved_data": {
                "user_id": user.user_id,
                "period_end_date": user.period_end_date.strftime("%d/%m/%Y")
                if user.period_end_date
                else None,
                "has_ttd_pill": user.has_ttd_pill,
                "reminder_hour_24": user.reminder_hour_24,
            },
        }

    user.preset_state = PresetState.AWAITING_MENSTRUATING
    user.save(update_fields=["preset_state", "updated_at"])
    return {
        "mode": "preset_interaction",
        "state": user.preset_state,
        "response": "Apakah kamu sedang menstruasi sekarang?",
    }


def handle_preset_interaction(user_id: str, message: str | None, endpoint_name: str):
    raw_message = (message or "").strip()
    normalized_message = raw_message.lower()

    with transaction.atomic():
        user, _ = ChatbotUser.objects.get_or_create(user_id=user_id)

        if normalized_message in RESET_COMMANDS:
            reset_preset_user(user)
            bot_message = "Data kamu sudah direset. Yuk mulai lagi dari awal: Apakah kamu sedang menstruasi sekarang?"
            response_payload = {
                "mode": "preset_interaction",
                "state": user.preset_state,
                "response": bot_message,
                "action": "reset",
            }
            log_interaction(
                user=user,
                user_id=user_id,
                mode=InteractionLog.MODE_PRESET,
                endpoint=endpoint_name,
                user_message=raw_message,
                bot_response=bot_message,
                status=InteractionLog.STATUS_SUCCESS,
                metadata={
                    "state": user.preset_state,
                    "invalid_input_count": user.invalid_input_count,
                    "action": "reset",
                },
            )
            return Response(response_payload, status=status.HTTP_200_OK)

        if user.preset_state in {PresetState.NOT_STARTED, PresetState.COMPLETED}:
            user.preset_state = PresetState.AWAITING_MENSTRUATING
            user.invalid_input_count = 0
            user.save(update_fields=["preset_state", "invalid_input_count", "updated_at"])
            bot_message = "Apakah kamu sedang menstruasi sekarang?"
            log_interaction(
                user=user,
                user_id=user_id,
                mode=InteractionLog.MODE_PRESET,
                endpoint=endpoint_name,
                user_message=raw_message,
                bot_response=bot_message,
                status=InteractionLog.STATUS_SUCCESS,
                metadata={"state": user.preset_state},
            )
            return Response(
                {
                    "mode": "preset_interaction",
                    "state": user.preset_state,
                    "response": bot_message,
                },
                status=status.HTTP_200_OK,
            )

        try:
            response_payload = advance_preset_flow(user, raw_message)
            if user.invalid_input_count != 0:
                user.invalid_input_count = 0
                user.save(update_fields=["invalid_input_count", "updated_at"])
            interaction_status = InteractionLog.STATUS_SUCCESS
        except InputValidationError as exc:
            user.invalid_input_count += 1
            user.save(update_fields=["invalid_input_count", "updated_at"])
            response_payload = {
                "mode": "preset_interaction",
                "state": user.preset_state,
                "error": str(exc),
            }
            if user.invalid_input_count >= 3:
                response_payload["hint"] = RESET_HINT_MESSAGE
            interaction_status = InteractionLog.STATUS_ERROR

        log_interaction(
            user=user,
            user_id=user_id,
            mode=InteractionLog.MODE_PRESET,
            endpoint=endpoint_name,
            user_message=raw_message,
            bot_response=response_payload.get("response") or response_payload.get("error", ""),
            status=interaction_status,
            metadata={
                "state": user.preset_state,
                "invalid_input_count": user.invalid_input_count,
            },
        )

        http_status = (
            status.HTTP_400_BAD_REQUEST
            if interaction_status == InteractionLog.STATUS_ERROR
            else status.HTTP_200_OK
        )
        return Response(response_payload, status=http_status)


class ModeDispatchAPIView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    endpoint_name = "mode-dispatch"
    throttle_scope = "chatbot_general"

    @extend_schema(
        request=ModeDispatchSerializer,
        responses={
            200: OpenApiResponse(description="Successful chatbot response"),
            400: OpenApiResponse(description="Input validation error"),
            401: OpenApiResponse(description="Unauthorized"),
            502: OpenApiResponse(description="External AI service error"),
        },
        examples=[
            OpenApiExample(
                "AI Mode Request",
                value={
                    "mode": "ai_qna",
                    "user_id": "628123456789",
                    "prompt": "Apa itu siklus menstruasi normal?",
                },
                request_only=True,
            ),
            OpenApiExample(
                "AI Mode Response",
                value={
                    "mode": "ai_qna",
                    "response": "Siklus menstruasi normal biasanya 21-35 hari.",
                },
                response_only=True,
                status_codes=["200"],
            ),
            OpenApiExample(
                "Preset Start Request",
                value={
                    "mode": "preset_interaction",
                    "user_id": "628123456789",
                },
                request_only=True,
            ),
            OpenApiExample(
                "Preset Start Response",
                value={
                    "mode": "preset_interaction",
                    "state": "awaiting_menstruating",
                    "response": "Apakah kamu sedang menstruasi sekarang?",
                },
                response_only=True,
                status_codes=["200"],
            ),
            OpenApiExample(
                "Preset Final Response with ICS",
                value={
                    "mode": "preset_interaction",
                    "state": "completed",
                    "response": "Pengingat TTD berhasil dibuat untuk 90 hari ke depan.",
                    "ics_file": {
                        "filename": "ttd-reminder-628123456789.ics",
                        "content_type": "text/calendar",
                        "content_base64": "QkVHSU46VkNBTEVOREFSLi4u",
                    },
                    "saved_data": {
                        "user_id": "628123456789",
                        "period_end_date": "06/04/2026",
                        "has_ttd_pill": True,
                        "reminder_hour_24": 20,
                    },
                },
                response_only=True,
                status_codes=["200"],
            ),
            OpenApiExample(
                "Preset Reset Command",
                value={
                    "mode": "preset_interaction",
                    "user_id": "628123456789",
                    "message": "reset",
                },
                request_only=True,
            ),
            OpenApiExample(
                "Preset Reset Response",
                value={
                    "mode": "preset_interaction",
                    "state": "awaiting_menstruating",
                    "response": "Data kamu sudah direset. Yuk mulai lagi dari awal: Apakah kamu sedang menstruasi sekarang?",
                    "action": "reset",
                },
                response_only=True,
                status_codes=["200"],
            ),
            OpenApiExample(
                "Preset Validation Error",
                value={
                    "mode": "preset_interaction",
                    "state": "awaiting_last_period_date",
                    "error": "Format tanggal tidak valid. Gunakan DD/MM/YYYY, contoh: 28/01/1970.",
                },
                response_only=True,
                status_codes=["400"],
            ),
        ],
        tags=["Chatbot"],
    )
    def post(self, request):
        serializer = ModeDispatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        mode = serializer.validated_data["mode"]
        user_id = serializer.validated_data["user_id"]

        if mode == InteractionLog.MODE_AI_QNA:
            prompt = serializer.validated_data.get("prompt", "")
            return handle_ai_qna(user_id=user_id, prompt=prompt, endpoint_name=self.endpoint_name)

        message = serializer.validated_data.get("message")
        return handle_preset_interaction(
            user_id=user_id,
            message=message,
            endpoint_name=self.endpoint_name,
        )


class WhatsAppWebhookAPIView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "whatsapp_webhook"
    endpoint_name = "whatsapp-webhook"

    @extend_schema(
        request=None,
        responses={
            200: OpenApiResponse(description="Webhook verification success"),
            403: OpenApiResponse(description="Webhook verification failed"),
        },
        tags=["WhatsApp Webhook"],
        examples=[
            OpenApiExample(
                "Meta Verify Query",
                value={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "<verify-token>",
                    "hub.challenge": "123456",
                },
                request_only=True,
            )
        ],
    )
    def get(self, request):
        mode = request.query_params.get("hub.mode")
        verify_token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge")

        if mode == "subscribe" and verify_token and verify_token == settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN:
            return Response(challenge or "", status=status.HTTP_200_OK)

        return Response({"error": "Webhook verification failed."}, status=status.HTTP_403_FORBIDDEN)

    @extend_schema(
        request=WhatsAppWebhookPayloadSerializer,
        responses={
            200: OpenApiResponse(description="Inbound webhook processed"),
            400: OpenApiResponse(description="Invalid payload"),
            401: OpenApiResponse(description="Invalid webhook bearer token"),
        },
        examples=[
            OpenApiExample(
                "Webhook Inbound Payload",
                value={
                    "entry": [
                        {
                            "changes": [
                                {
                                    "value": {
                                        "messages": [
                                            {
                                                "from": "628123456789",
                                                "text": {
                                                    "body": "ai: apakah menstruasi normal 5 hari?"
                                                },
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    ]
                },
                request_only=True,
            ),
            OpenApiExample(
                "Webhook Processed Response",
                value={
                    "status": "processed",
                    "provider": "whatsapp",
                    "inbound": {
                        "user_id": "628123456789",
                        "mode": "ai_qna",
                        "message": "apakah menstruasi normal 5 hari?",
                    },
                    "chatbot_response": {
                        "mode": "ai_qna",
                        "response": "Ya, durasi 3-7 hari masih dalam rentang umum.",
                    },
                },
                response_only=True,
                status_codes=["200"],
            ),
        ],
        tags=["WhatsApp Webhook"],
    )
    def post(self, request):
        expected_token = settings.WHATSAPP_WEBHOOK_TOKEN
        provided_header = request.headers.get("Authorization", "")
        if expected_token and provided_header != f"Bearer {expected_token}":
            return Response({"error": "Invalid webhook bearer token."}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            extracted = extract_whatsapp_message(request.data)
        except InputValidationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        mode, normalized_text = parse_webhook_mode_and_message(extracted.get("message", ""))
        user_id = extracted["user_id"]

        if mode == InteractionLog.MODE_AI_QNA:
            if not normalized_text:
                return Response(
                    {"error": "AI prompt is empty. Use format: ai: <pertanyaan>"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            chatbot_response = handle_ai_qna(user_id=user_id, prompt=normalized_text, endpoint_name=self.endpoint_name)
        else:
            chatbot_response = handle_preset_interaction(
                user_id=user_id,
                message=normalized_text,
                endpoint_name=self.endpoint_name,
            )

        return Response(
            {
                "status": "processed",
                "provider": "whatsapp",
                "inbound": {
                    "user_id": user_id,
                    "mode": mode,
                    "message": normalized_text,
                },
                "chatbot_response": chatbot_response.data,
            },
            status=chatbot_response.status_code,
        )
