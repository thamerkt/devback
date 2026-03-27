from django.urls import path
from . import views

app_name = 'authentication'

urlpatterns = [
    # OAuth flow
    path('shopify/install/', views.shopify_install, name='shopify-install'),
    path('shopify/callback/', views.shopify_callback, name='shopify-callback'),

    # Session token verification (for embedded app)
    path('shopify/verify-token/', views.verify_session_token, name='shopify-verify-token'),
    path('shopify-sync/', views.shopify_sync, name='shopify-sync'),

    # Webhooks
    path('shopify/webhooks/app-uninstalled/', views.webhook_app_uninstalled, name='webhook-app-uninstalled'),
    path('shopify/webhooks/app-subscriptions-update/', views.webhook_app_subscriptions_update, name='webhook-app-subscriptions-update'),

    # Consent
    path('update-consent/', views.shopify_update_consent, name='shopify-update-consent'),

    # Billing
    path('shopify/billing/create/', views.shopify_billing_create, name='shopify-billing-create'),
    path('shopify/billing/callback/', views.shopify_billing_callback, name='shopify-billing-callback'),
]
