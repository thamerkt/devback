from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()

class Conversation(models.Model):
    external_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    participants = models.ManyToManyField(User, related_name='conversations')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Conversation {self.id}"

class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_messages')
    content = models.TextField()
    is_ai = models.BooleanField(default=False)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['timestamp']

    def __str__(self):
        return f"{self.sender.username}: {self.content[:20]}"


class ClientAction(models.Model):
    """Tracks a storefront visitor's page navigation and actions."""
    ACTION_TYPES = [
        ('page_view', 'Page View'),
        ('product_view', 'Product View'),
        ('collection_view', 'Collection View'),
        ('cart_update', 'Cart Update'),
        ('search', 'Search'),
    ]

    session_id = models.CharField(max_length=255, db_index=True)
    shop_domain = models.CharField(max_length=255, db_index=True)
    action_type = models.CharField(max_length=50, choices=ACTION_TYPES, default='page_view')
    page_url = models.URLField(max_length=2048)
    page_title = models.CharField(max_length=500, blank=True, default='')
    referrer = models.URLField(max_length=2048, blank=True, default='')
    extra_data = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        verbose_name = 'Client Action'
        verbose_name_plural = 'Client Actions'

    def __str__(self):
        return f"[{self.action_type}] {self.session_id} → {self.page_url[:60]}"


class ProactiveMessage(models.Model):
    """AI-generated proactive sales messages for storefront visitors."""
    session_id = models.CharField(max_length=255, db_index=True)
    shop_domain = models.CharField(max_length=255)
    message = models.TextField()
    trigger_action = models.ForeignKey(
        ClientAction, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='triggered_messages',
        help_text='The action that triggered this proactive message'
    )
    is_delivered = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Proactive Message'
        verbose_name_plural = 'Proactive Messages'

    def __str__(self):
        return f"Nudge for {self.session_id}: {self.message[:40]}"
