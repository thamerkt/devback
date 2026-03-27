from django.db import models


class ShopifyStore(models.Model):
    """
    Represents a merchant's Shopify store that has installed the app.
    Stores the OAuth access token for making API calls on their behalf.
    """
    shop_url = models.CharField(
        max_length=255,
        unique=True,
        help_text="The merchant's Shopify domain, e.g. 'store.myshopify.com'"
    )
    access_token = models.CharField(
        max_length=255,
        help_text="OAuth access token for API calls"
    )
    scope = models.TextField(
        blank=True,
        default='',
        help_text="Comma-separated list of granted OAuth scopes"
    )
    nonce = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text="CSRF nonce used during OAuth flow"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether the app is currently installed on this store"
    )
    plan_name = models.CharField(
        max_length=100,
        default='Free Forever',
        help_text="Current subscription plan name"
    )
    plan_status = models.CharField(
        max_length=50,
        default='active',
        help_text="Current subscription status (e.g., active, frozen, cancelled)"
    )
    charge_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Shopify recurring charge GID (e.g., 'gid://shopify/AppSubscription/123')"
    )
    plan_type = models.CharField(
        max_length=50,
        default='Free',
        help_text="Type of plan (e.g., Free, Recurring)"
    )
    ai_conversations_used = models.IntegerField(
        default=0,
        help_text="Number of AI conversations used in the current billing cycle"
    )
    ai_conversations_limit = models.IntegerField(
        default=100,
        help_text="Maximum AI conversations allowed per billing cycle"
    )
    consent_orders = models.BooleanField(
        default=False,
        help_text="Whether the merchant has consented to AI reading orders"
    )
    consent_customers = models.BooleanField(
        default=False,
        help_text="Whether the merchant has consented to AI reading customers"
    )
    installed_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Shopify Store"
        verbose_name_plural = "Shopify Stores"
        ordering = ['-installed_at']

    def __str__(self):
        return f"{self.shop_url} ({'active' if self.is_active else 'inactive'})"

    def get_api_headers(self):
        """Return headers for authenticated Shopify API requests."""
        return {
            'X-Shopify-Access-Token': self.access_token,
            'Content-Type': 'application/json',
        }
