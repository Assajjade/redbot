from rest_framework import serializers


class ModeDispatchSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=["ai_qna", "preset_interaction"])
    user_id = serializers.CharField(max_length=64)
    message = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    prompt = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs):
        mode = attrs.get("mode")
        if mode == "ai_qna" and not attrs.get("prompt"):
            raise serializers.ValidationError({"prompt": "Prompt is required for ai_qna mode."})
        return attrs


class WhatsAppWebhookPayloadSerializer(serializers.Serializer):
    entry = serializers.ListField(child=serializers.DictField(), required=True)
