from .models import InteractionLog


def log_interaction(
    *,
    user,
    user_id,
    mode,
    endpoint,
    user_message="",
    bot_response="",
    status=InteractionLog.STATUS_SUCCESS,
    metadata=None,
):
    InteractionLog.objects.create(
        user=user,
        external_user_id=user_id,
        mode=mode,
        endpoint=endpoint,
        user_message=user_message or "",
        bot_response=bot_response or "",
        status=status,
        metadata=metadata or {},
    )
