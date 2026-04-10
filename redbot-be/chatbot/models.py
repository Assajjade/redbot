from django.db import models


class PresetState(models.TextChoices):
    NOT_STARTED = "not_started", "Not Started"
    AWAITING_MENSTRUATING = "awaiting_menstruating", "Awaiting Menstruating Answer"
    AWAITING_LAST_PERIOD_DATE = "awaiting_last_period_date", "Awaiting Last Period Date"
    AWAITING_HAS_TTD = "awaiting_has_ttd", "Awaiting Has TTD Answer"
    AWAITING_REMINDER_HOUR = "awaiting_reminder_hour", "Awaiting Reminder Hour"
    COMPLETED = "completed", "Completed"


class ChatbotUser(models.Model):
    user_id = models.CharField(max_length=64, unique=True)
    preset_state = models.CharField(
        max_length=64,
        choices=PresetState.choices,
        default=PresetState.NOT_STARTED,
    )
    invalid_input_count = models.PositiveSmallIntegerField(default=0)

    is_currently_menstruating = models.BooleanField(null=True, blank=True)
    last_period_start_date = models.DateField(null=True, blank=True)
    period_end_date = models.DateField(null=True, blank=True)
    has_ttd_pill = models.BooleanField(null=True, blank=True)
    reminder_hour_24 = models.PositiveSmallIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.user_id


class InteractionLog(models.Model):
    MODE_AI_QNA = "ai_qna"
    MODE_PRESET = "preset_interaction"

    STATUS_SUCCESS = "success"
    STATUS_ERROR = "error"

    MODE_CHOICES = [
        (MODE_AI_QNA, "AI QnA"),
        (MODE_PRESET, "Preset Interaction"),
    ]

    STATUS_CHOICES = [
        (STATUS_SUCCESS, "Success"),
        (STATUS_ERROR, "Error"),
    ]

    user = models.ForeignKey(
        ChatbotUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="interaction_logs",
    )
    external_user_id = models.CharField(max_length=64)
    mode = models.CharField(max_length=32, choices=MODE_CHOICES)
    endpoint = models.CharField(max_length=128)
    user_message = models.TextField(blank=True)
    bot_response = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.external_user_id} - {self.mode} - {self.status}"
