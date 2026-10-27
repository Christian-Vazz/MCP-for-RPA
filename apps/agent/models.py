from django.conf import settings
from django.db import models
from django.utils import timezone


class Conversation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations")
    title = models.CharField(max_length=120, default="Nova conversa")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10)  # user | assistant
    content = models.TextField()
    run = models.ForeignKey("Run", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    feedback = models.SmallIntegerField(default=0)  # -1 | 0 | 1
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class Run(models.Model):
    ACTIVE = ("queued", "running", "waiting_approval")
    STATUS = [(s, s) for s in ("queued", "running", "waiting_approval", "done", "error", "cancelled")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="runs")
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="runs")
    user_message = models.ForeignKey(Message, null=True, on_delete=models.SET_NULL, related_name="+")
    prompt = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS, default="queued", db_index=True)
    answer = models.TextField(blank=True)
    partial = models.TextField(blank=True)          # texto em streaming (lido pelo polling)
    error = models.TextField(blank=True)
    cached = models.BooleanField(default=False)
    cache_key = models.CharField(max_length=80, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cache_read_tokens = models.PositiveIntegerField(default=0)   # economia do prompt caching da API
    saved_tokens = models.PositiveIntegerField(default=0)        # economia do cache de resposta
    steps = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["user", "created_at"])]


class RunEvent(models.Model):
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="events")
    seq = models.PositiveIntegerField()
    kind = models.CharField(max_length=20)  # context|text|tool_call|tool_result|approval|error|done
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("run", "seq")]
        ordering = ["seq"]


class Approval(models.Model):
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="approvals")
    call_id = models.CharField(max_length=80)
    tool = models.CharField(max_length=60)
    args = models.JSONField(default=dict)
    decision = models.CharField(max_length=5, blank=True)  # '' | allow | deny
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)


class ActionLog(models.Model):
    """Cada execução de ferramenta (pelo agente ou direta). Base do dashboard e da auditoria."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="actions")
    run = models.ForeignKey(Run, null=True, blank=True, on_delete=models.SET_NULL, related_name="actions")
    tool = models.CharField(max_length=60)
    category = models.CharField(max_length=20)
    args_preview = models.CharField(max_length=240, blank=True)
    ok = models.BooleanField(default=True)
    duration_ms = models.PositiveIntegerField(default=0)
    source = models.CharField(max_length=10, default="agent")  # agent | direct | mcp
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["user", "created_at"])]


class TrainingSample(models.Model):
    """Amostras para melhorar o agente. Só são criadas se o usuário consentiu (Profile.allow_learning)."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="samples")
    run = models.OneToOneField(Run, on_delete=models.CASCADE, related_name="sample")
    prompt = models.TextField()
    answer = models.TextField()
    tools = models.JSONField(default=list)
    rating = models.SmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
