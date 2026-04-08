from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import override_settings
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .models import ChatbotUser, InteractionLog, PresetState


class AuthenticatedAPIMixin:
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="tester", password="secret123")
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")


class PresetInteractionAPITests(AuthenticatedAPIMixin, APITestCase):
    def test_preset_flow_generates_ics_and_saves_user_data(self):
        url = "/api/chatbot/mode/"
        user_id = "user-123"

        response_1 = self.client.post(url, {"mode": "preset_interaction", "user_id": user_id}, format="json")
        self.assertEqual(response_1.status_code, status.HTTP_200_OK)
        self.assertEqual(response_1.data["state"], PresetState.AWAITING_MENSTRUATING)

        response_2 = self.client.post(
            url,
            {"mode": "preset_interaction", "user_id": user_id, "message": "no"},
            format="json",
        )
        self.assertEqual(response_2.status_code, status.HTTP_200_OK)
        self.assertEqual(response_2.data["state"], PresetState.AWAITING_LAST_PERIOD_DATE)

        response_3 = self.client.post(
            url,
            {"mode": "preset_interaction", "user_id": user_id, "message": "01/04/2026"},
            format="json",
        )
        self.assertEqual(response_3.status_code, status.HTTP_200_OK)
        self.assertEqual(response_3.data["state"], PresetState.AWAITING_HAS_TTD)

        response_4 = self.client.post(
            url,
            {"mode": "preset_interaction", "user_id": user_id, "message": "yes"},
            format="json",
        )
        self.assertEqual(response_4.status_code, status.HTTP_200_OK)
        self.assertEqual(response_4.data["state"], PresetState.AWAITING_REMINDER_HOUR)

        response_5 = self.client.post(
            url,
            {"mode": "preset_interaction", "user_id": user_id, "message": "20"},
            format="json",
        )
        self.assertEqual(response_5.status_code, status.HTTP_200_OK)
        self.assertEqual(response_5.data["state"], PresetState.COMPLETED)
        self.assertIn("ics_file", response_5.data)
        self.assertIn("content_base64", response_5.data["ics_file"])

        user = ChatbotUser.objects.get(user_id=user_id)
        self.assertEqual(user.reminder_hour_24, 20)
        self.assertTrue(user.has_ttd_pill)
        self.assertEqual(user.preset_state, PresetState.COMPLETED)

        self.assertGreaterEqual(InteractionLog.objects.filter(external_user_id=user_id).count(), 5)

    def test_preset_flow_returns_validation_error_for_invalid_date(self):
        url = "/api/chatbot/mode/"
        user_id = "user-date-invalid"

        self.client.post(url, {"mode": "preset_interaction", "user_id": user_id}, format="json")
        self.client.post(
            url,
            {"mode": "preset_interaction", "user_id": user_id, "message": "no"},
            format="json",
        )
        response = self.client.post(
            url,
            {"mode": "preset_interaction", "user_id": user_id, "message": "2026-04-01"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.data)

    def test_protected_endpoint_rejects_unauthorized(self):
        self.client.credentials()
        response = self.client.post(
            "/api/chatbot/mode/",
            {"mode": "preset_interaction", "user_id": "u"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class AIQnAAPITests(AuthenticatedAPIMixin, APITestCase):
    @override_settings(AI_API_URL="https://example.com/ai")
    @patch("chatbot.services.requests.post")
    def test_ai_qna_success(self, mocked_post):
        mocked_post.return_value.status_code = 200
        mocked_post.return_value.json.return_value = {"response": "Ini jawaban AI"}

        response = self.client.post(
            "/api/chatbot/mode/",
            {
                "mode": "ai_qna",
                "user_id": "user-ai-1",
                "prompt": "Apa itu menstruasi?",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["response"], "Ini jawaban AI")
        self.assertEqual(
            InteractionLog.objects.filter(external_user_id="user-ai-1", mode=InteractionLog.MODE_AI_QNA).count(),
            1,
        )

    def test_ai_qna_returns_error_when_ai_api_is_not_configured(self):
        response = self.client.post(
            "/api/chatbot/mode/",
            {"mode": "ai_qna", "user_id": "user-ai-2", "prompt": "Halo"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertIn("error", response.data)


class WhatsAppWebhookAPITests(APITestCase):
    @override_settings(WHATSAPP_WEBHOOK_VERIFY_TOKEN="verify-token")
    def test_webhook_verification(self):
        response = self.client.get(
            "/api/chatbot/webhooks/whatsapp/?hub.mode=subscribe&hub.verify_token=verify-token&hub.challenge=12345"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, "12345")

    @override_settings(
        AI_API_URL="https://example.com/ai",
        WHATSAPP_WEBHOOK_TOKEN="wh-token",
    )
    @patch("chatbot.services.requests.post")
    def test_webhook_post_processes_inbound_message(self, mocked_post):
        mocked_post.return_value.status_code = 200
        mocked_post.return_value.json.return_value = {"response": "Jawaban webhook AI"}

        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "628123456789",
                                        "text": {"body": "ai: apakah menstruasi normal 5 hari?"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }

        response = self.client.post(
            "/api/chatbot/webhooks/whatsapp/",
            payload,
            format="json",
            HTTP_AUTHORIZATION="Bearer wh-token",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "processed")
        self.assertEqual(response.data["inbound"]["mode"], "ai_qna")
