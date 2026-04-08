from django.urls import path

from .views import ModeDispatchAPIView, WhatsAppWebhookAPIView

urlpatterns = [
    path("mode/", ModeDispatchAPIView.as_view(), name="mode-dispatch"),
    path("webhooks/whatsapp/", WhatsAppWebhookAPIView.as_view(), name="whatsapp-webhook"),
]
