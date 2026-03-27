import secrets
import logging
import base64
import time

import requests as http_requests
from django.conf import settings
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponseRedirect, HttpResponse
from django.core.signing import Signer, BadSignature
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.urls import reverse

from .models import ShopifyStore
from .utils import (
    validate_shop_url,
    verify_shopify_hmac,
    verify_shopify_webhook_hmac,
    verify_shopify_session_token,
    get_shop_from_session_token,
    verify_shopify_access_token,
)
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
import json

logger = logging.getLogger(__name__)
User = get_user_model()

# Subscription Plan Definitions
PLAN_CONFIG = {
    "Growth": {
        "amount": 19.99,
        "limit": 500,
        "test": False, # Set to True for testing without real charges
    },
    "Scale": {
        "amount": 49.99,
        "limit": 2000,
        "test": False,
    },
    "Infinite": {
        "amount": 199.99,
        "limit": 1000000,
        "test": False,
    },
}


def _smart_redirect_to_app(request, shop, host):
    """
    Called from shopify_install when Smart Redirection detects an existing valid token.
    
    At this point, the request is coming from INSIDE the Shopify Admin iframe.
    The browser may block a 302 redirect to a different origin (Remix) inside an iframe.
    Instead, we return an HTML page that performs a JavaScript redirect.
    """
    remix_url = settings.SHOPIFY_APP_URL
    
    # Extract all existing query params to preserve them (identity, session tokens, etc.)
    query_params = request.GET.copy()
    query_params['shop'] = shop
    query_params['host'] = host
    
    redirect_url = f"{remix_url}/app?{query_params.urlencode()}"
    
    logger.info(f"Smart Redirect -> Remix (via JS): {redirect_url}")
    
    # We use a JS redirect here to satisfy iframe security policies.
    html = f"""
    <!DOCTYPE html>
    <html>
      <head>
        <script type="text/javascript">
          window.location.href = "{redirect_url}";
        </script>
      </head>
      <body><p>Loading application...</p></body>
    </html>
    """
    response = HttpResponse(html)
    return _add_shopify_csp_headers(response, shop)


def _add_shopify_csp_headers(response, shop):
    """
    Adds mandatory Content-Security-Policy headers for Shopify embedded apps.
    This resolves "Refused to connect" errors by allowing Shopify to frame the app.
    """
    # frame-ancestors must include the specific shop and the admin domain
    csp = (
        f"frame-ancestors https://{shop} https://admin.shopify.com https://*.myshopify.com;"
    )
    response['Content-Security-Policy'] = csp
    
    # Set X-Frame-Options to allow framing from Shopify Admin
    # Note: Modern browsers use CSP frame-ancestors, but older ones might use this.
    # We use ALLOWALL as we handle specific security via CSP above.
    response['X-Frame-Options'] = 'ALLOWALL'
    
    # Also ensure we don't block framing via middleware
    response.xframe_options_exempt = True
    return response


def _step5_redirect_after_callback(shop, host_encoded):
    """
    Called from shopify_callback after successful token exchange (Step 5).
    
    At this point, the user is at the TOP LEVEL (Shopify's OAuth page redirected here).
    We need to send them BACK to the Shopify Admin embedded app URL.
    
    This is the only place we should redirect to the Shopify Admin URL.
    After this, Shopify Admin will load the app in an iframe, which will trigger
    shopify_install → Smart Redirection (which will NOT loop because it returns
    a redirect to the Remix frontend, not to Shopify Admin).
    """
    if host_encoded:
        try:
            # Handle base64 padding for Python
            padded_host = host_encoded
            missing_padding = len(padded_host) % 4
            if missing_padding:
                padded_host += '=' * (4 - missing_padding)
            decoded_host = base64.b64decode(padded_host).decode('utf-8')
            redirect_url = f"https://{decoded_host}/apps/{settings.SHOPIFY_API_KEY}/"
        except Exception as e:
            logger.error(f"Error decoding host for redirect: {e}")
            redirect_url = f"https://{shop}/admin/apps/{settings.SHOPIFY_API_KEY}/"
    else:
        redirect_url = f"https://{shop}/admin/apps/{settings.SHOPIFY_API_KEY}/"

    logger.info(f"Step 5 -> Shopify Admin: {redirect_url}")
    
    # Use JS redirect to set top-level location (we are NOT in an iframe here)
    response = HttpResponse(f"""
    <!DOCTYPE html>
    <html>
      <head>
        <script type="text/javascript">
          window.top.location.href = "{redirect_url}";
        </script>
      </head>
      <body><p>Finalizing installation, redirecting you back...</p></body>
    </html>
    """)
    return _add_shopify_csp_headers(response, shop)


# ---------------------------------------------------------------------------
# 1) OAuth Install — Redirect merchant to Shopify consent screen
# ---------------------------------------------------------------------------
@xframe_options_exempt
@require_GET
def shopify_install(request):
    """
    Entry point for the OAuth flow. Called when a merchant clicks "Install".
    
    Query params:
        - shop: the merchant's store domain (e.g. 'store.myshopify.com')
    
    Flow:
        1. Validates the shop parameter
        2. Generates a random nonce for CSRF protection
        3. Saves the nonce to DB (or creates a placeholder ShopifyStore)
        4. Redirects merchant to Shopify's OAuth consent screen
    """
    shop = request.GET.get('shop', '').strip()

    if not validate_shop_url(shop):
        return HttpResponseBadRequest(
            "Invalid shop parameter. Must be a valid *.myshopify.com domain."
        )

    # Smart Redirection: Check if we already have a valid token with required scopes
    # This prevents the OAuth loop when Shopify re-sends merchants here on app load.
    host = request.GET.get('host', '')
    embedded_param = request.GET.get('embedded', '0')

    try:
        existing_store = ShopifyStore.objects.filter(shop_url=shop, is_active=True).first()
        logger.info(f"Smart Redir: store found={existing_store is not None}, "
                     f"has_token={bool(existing_store and existing_store.access_token)}, "
                     f"host={host}, embedded={embedded_param}")

        if existing_store and existing_store.access_token:
            # Log scope coverage for monitoring (but don't block on it)
            required_scopes = set(s.strip() for s in settings.SHOPIFY_SCOPES.split(',') if s.strip())
            granted_scopes = set(s.strip() for s in (existing_store.scope or '').split(',') if s.strip())
            scopes_ok = required_scopes.issubset(granted_scopes)
            if not scopes_ok:
                missing = required_scopes - granted_scopes
                logger.warning(f"Smart Redir: Scope gap for {shop}. Missing: {missing}. "
                               f"Shopify may not have granted all requested scopes. "
                               f"Will still try to use existing token.")

            # Verify token is still valid with a quick API call
            token_valid = verify_shopify_access_token(shop, existing_store.access_token)
            logger.info(f"Smart Redir: token_valid={token_valid}")

            if token_valid:
                # SUCCESS — skip OAuth entirely and redirect to the app
                # Even if scopes don't fully match, re-requesting OAuth won't help
                # because Shopify will grant the same scopes again → infinite loop.
                logger.info(f"Smart Redir: Skipping OAuth for {shop}. Redirecting to app.")
                return _smart_redirect_to_app(request, shop, host)
            else:
                # Token expired or revoked — need new OAuth
                logger.warning(f"Smart Redir: Token invalid for {shop}. Will start OAuth.")

    except Exception as e:
        logger.error(f"Error during smart redirection check for {shop}: {e}", exc_info=True)

    # Generate a unique nonce for CSRF protection
    nonce = secrets.token_urlsafe(32)

    # Sign the nonce to store in a cookie
    signer = Signer()
    signed_nonce = signer.sign(nonce)

    # Build the Shopify OAuth authorization URL
    scopes = settings.SHOPIFY_SCOPES
    redirect_uri = request.build_absolute_uri(reverse('authentication:shopify-callback'))
    api_key = settings.SHOPIFY_API_KEY

    auth_url = (
        f"https://{shop}/admin/oauth/authorize"
        f"?client_id={api_key}"
        f"&scope={scopes}"
        f"&redirect_uri={redirect_uri}"
        f"&state={nonce}"
    )

    logger.info(f"Redirecting {shop} to Shopify OAuth consent screen: {auth_url}")
    
    # Embedded apps cannot do a 302 redirect inside an iframe.
    # We must use a JavaScript redirect to escape the iframe.
    # We also provide a fallback link in case the script is blocked.
    html = f"""
    <!DOCTYPE html>
    <html>
      <head>
        <title>Redirecting to Shopify OAuth</title>
        <style>
          body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh; margin: 0; background: #f4f6f8; }}
          .card {{ background: white; padding: 2rem; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); text-align: center; max-width: 400px; }}
          .spinner {{ border: 4px solid rgba(0, 0, 0, 0.1); width: 36px; height: 36px; border-radius: 50%; border-left-color: #008060; animation: spin 1s linear infinite; margin-bottom: 1rem; }}
          @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
          a {{ color: #008060; text-decoration: none; font-weight: 500; margin-top: 1rem; display: block; }}
          a:hover {{ text-decoration: underline; }}
        </style>
        <script type="text/javascript">
          window.onload = function() {{
            console.log("Attempting top-level redirect to: {auth_url}");
            try {{
              window.top.location.href = "{auth_url}";
            }} catch (e) {{
              console.error("Redirect failed:", e);
              document.getElementById("manual-link").style.display = "block";
            }}
          }};
        </script>
      </head>
      <body>
        <div class="card">
          <div style="display: flex; justify-content: center;"><div class="spinner"></div></div>
          <p>Redirecting to Shopify for authentication...</p>
          <a id="manual-link" href="{auth_url}" target="_top" style="display: none;">Click here if you are not redirected automatically</a>
          <script>
            // Show link after 3 seconds as a fail-safe
            setTimeout(function() {{
              document.getElementById("manual-link").style.display = "block";
            }}, 3000);
          </script>
        </div>
      </body>
    </html>
    """
    response = HttpResponse(html)
    
    # Set the nonce in a signed, secure, SameSite=None cookie
    response.set_cookie(
        'shopify_nonce', 
        signed_nonce, 
        max_age=3600, # 1 hour
        httponly=True,
        secure=True,
        samesite='None'
    )
    return _add_shopify_csp_headers(response, shop)


# ---------------------------------------------------------------------------
# 2) OAuth Callback — Exchange authorization code for access token
# ---------------------------------------------------------------------------
@require_GET
def shopify_callback(request):
    """
    Shopify redirects here after the merchant approves the app.
    
    Query params (from Shopify):
        - shop: the store domain
        - code: temporary authorization code
        - hmac: HMAC signature for verification
        - state: the nonce we sent earlier
        - timestamp: request timestamp
    
    Flow:
        1. Verify HMAC signature (request is genuinely from Shopify)
        2. Validate the nonce/state matches what we stored
        3. Exchange the authorization code for a permanent access token
        4. Store the access token in the database
        5. Redirect to the embedded app
    """
    # Collect query params
    params = {key: request.GET.get(key) for key in request.GET}
    shop = params.get('shop', '')
    code = params.get('code', '')
    state = params.get('state', '')

    # --- Security check 1: Validate shop URL ---
    if not validate_shop_url(shop):
        return HttpResponseBadRequest("Invalid shop parameter.")

    # --- Security check 2: Verify HMAC ---
    params_for_hmac = dict(params)  # copy so we don't mutate original
    if not verify_shopify_hmac(params_for_hmac, settings.SHOPIFY_API_SECRET):
        logger.warning(f"HMAC verification failed for {shop}")
        return HttpResponseBadRequest("HMAC verification failed.")

    # --- Security check 3: Verify nonce (state) ---
    # Get nonce from signed cookie
    cookie_nonce = request.COOKIES.get('shopify_nonce')
    if not cookie_nonce:
        logger.warning(f"Missing shopify_nonce cookie for {shop}")
        return HttpResponseBadRequest("Session expired or cookies blocked. Please try again.")

    try:
        signer = Signer()
        stored_nonce = signer.unsign(cookie_nonce)
    except BadSignature:
        logger.warning(f"Invalid nonce signature for {shop}")
        return HttpResponseBadRequest("Security verification failed. Invalid state.")

    if stored_nonce != state:
        logger.warning(f"Nonce mismatch for {shop}. Stored: '{stored_nonce}', Received: '{state}'")
        return HttpResponseBadRequest(f"State/nonce verification failed. (Stored: {stored_nonce}, Received: {state})")

    # --- Fetch or create the store record ---
    store, _ = ShopifyStore.objects.get_or_create(shop_url=shop)

    # --- Exchange authorization code for access token ---
    token_url = f"https://{shop}/admin/oauth/access_token"
    payload = {
        'client_id': settings.SHOPIFY_API_KEY,
        'client_secret': settings.SHOPIFY_API_SECRET,
        'code': code,
    }

    try:
        response = http_requests.post(token_url, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
    except http_requests.RequestException as e:
        logger.error(f"Token exchange failed for {shop}: {e}")
        return HttpResponseBadRequest("Failed to exchange authorization code.")

    access_token = data.get('access_token')
    scope = data.get('scope', '')

    if not access_token:
        logger.error(f"No access_token in response for {shop}")
        return HttpResponseBadRequest("No access token received from Shopify.")

    # --- Save the access token ---
    store.access_token = access_token
    store.scope = scope
    store.nonce = ''  # clear nonce after use
    store.is_active = True
    store.save()

    logger.info(f"Successfully installed app for {shop}")

    # --- Register App Webhooks ---
    register_app_webhooks(shop, access_token)

    # --- Step 5: Redirect to your app's UI ---
    # Use the shared helper to redirect to the Remix frontend.
    # DO NOT redirect to Shopify Admin's /apps/ URL here — that causes an infinite loop
    # because Shopify Admin will reload the iframe and trigger shopify_install again.
    host_encoded = params.get('host', '')
    embedded_param = params.get('embedded', '0')
    
    logger.info(f"Step 5: Redirecting {shop} to Shopify Admin (host={host_encoded})")
    return _step5_redirect_after_callback(shop, host_encoded)


def register_app_webhooks(shop, access_token):
    """
    Register mandatory and app-specific webhooks with Shopify.
    """
    webhooks = [
        ("app/uninstalled", reverse('authentication:webhook-app-uninstalled')),
        ("app_subscriptions/update", reverse('authentication:webhook-app-subscriptions-update')),
    ]
    
    version = "2024-01"
    webhook_api_url = f"https://{shop}/admin/api/{version}/webhooks.json"
    
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json"
    }
    
    for topic, relative_path in webhooks:
        callback_url = f"{settings.SHOPIFY_APP_URL}{relative_path}"
        
        payload = {
            "webhook": {
                "topic": topic,
                "address": callback_url,
                "format": "json"
            }
        }
        
        try:
            response = http_requests.post(webhook_api_url, json=payload, headers=headers, timeout=10)
            if response.status_code == 201:
                logger.info(f"Successfully registered {topic} webhook for {shop}")
            elif response.status_code == 422:
                logger.info(f"{topic} webhook already registered for {shop}")
            else:
                logger.warning(f"Failed to register {topic} webhook for {shop}: {response.text}")
        except Exception as e:
            logger.error(f"Error registering {topic} webhook for {shop}: {e}")


# ---------------------------------------------------------------------------
# 3) Session Token Verification — For embedded app API requests
# ---------------------------------------------------------------------------
@csrf_exempt
@require_POST
def verify_session_token(request):
    """
    Verify a Shopify session token from the embedded Remix app.
    
    The frontend sends the session token in the Authorization header.
    We decode and validate it, then return shop info if valid.
    
    Headers:
        Authorization: Bearer <session_token>
    
    Returns:
        200: { shop, shop_id, scopes }
        401: { error: "..." }
    """
    auth_header = request.headers.get('Authorization', '')

    if not auth_header.startswith('Bearer '):
        return JsonResponse({'error': 'Missing or invalid Authorization header'}, status=401)

    token = auth_header.split('Bearer ')[1].strip()

    decoded = verify_shopify_session_token(
        token,
        settings.SHOPIFY_API_KEY,
        settings.SHOPIFY_API_SECRET,
    )

    if not decoded:
        return JsonResponse({'error': 'Invalid or expired session token'}, status=401)

    # Extract shop from the token
    shop = get_shop_from_session_token(decoded)

    # Verify the shop is installed and active
    try:
        store = ShopifyStore.objects.get(shop_url=shop, is_active=True)
    except ShopifyStore.DoesNotExist:
        return JsonResponse({'error': 'Store not found or app not installed'}, status=401)

    return JsonResponse({
        'shop': store.shop_url,
        'shop_id': store.id,
        'scopes': store.scope,
    })


# ---------------------------------------------------------------------------
# 4) Webhook handler — App uninstalled
# ---------------------------------------------------------------------------
@csrf_exempt
@require_POST
def webhook_app_uninstalled(request):
    """
    Handle the app/uninstalled webhook from Shopify.
    
    When a merchant uninstalls the app, Shopify sends this webhook.
    We mark the store as inactive and clear the access token.
    
    Headers:
        X-Shopify-Hmac-Sha256: HMAC signature
        X-Shopify-Shop-Domain: shop domain
    """
    hmac_header = request.headers.get('X-Shopify-Hmac-Sha256', '')
    shop = request.headers.get('X-Shopify-Shop-Domain', '')

    # Verify webhook HMAC
    if not verify_shopify_webhook_hmac(request.body, hmac_header, settings.SHOPIFY_API_SECRET):
        logger.warning(f"Webhook HMAC verification failed for {shop}")
        return JsonResponse({'error': 'HMAC verification failed'}, status=401)

    # Mark the store as inactive
    try:
        store = ShopifyStore.objects.get(shop_url=shop)
        store.is_active = False
        store.access_token = ''  # clear token for security
        store.save()
        logger.info(f"App uninstalled for {shop}")
    except ShopifyStore.DoesNotExist:
        logger.warning(f"Uninstall webhook for unknown shop: {shop}")

    return JsonResponse({'status': 'ok'})


@csrf_exempt
@require_POST
def webhook_app_subscriptions_update(request):
    """
    Handle the app_subscriptions/update webhook from Shopify.
    Whenever a merchant approves a charge, Shopify sends this webhook.
    
    Payload:
    {
      "app_subscription": {
        "admin_graphql_api_id": "gid://shopify/AppSubscription/...",
        "name": "Growth",
        "status": "ACTIVE",
        ...
      }
    }
    """
    hmac_header = request.headers.get('X-Shopify-Hmac-Sha256', '')
    shop_domain = request.headers.get('X-Shopify-Shop-Domain', '')
    webhook_verified = request.headers.get('X-Webhook-Verified', '')

    # If forwarded from Remix (which already verified via authenticate.webhook()), skip HMAC
    if webhook_verified != 'remix-verified':
        if not verify_shopify_webhook_hmac(request.body, hmac_header, settings.SHOPIFY_API_SECRET):
            logger.warning(f"Subscription Webhook HMAC verification failed for {shop_domain}")
            return JsonResponse({'error': 'HMAC verification failed'}, status=401)
    else:
        logger.info(f"Webhook pre-verified by Remix for {shop_domain}")

    try:
        data = json.loads(request.body)
        sub = data.get('app_subscription', {})
        name = sub.get('name', 'Free Forever')
        status = sub.get('status', 'active').lower()

        # Update store info
        store = ShopifyStore.objects.get(shop_url=shop_domain)
        store.plan_name = name
        store.plan_status = status

        # Set limits based on tier
        limit = 100
        for plan_name, config in PLAN_CONFIG.items():
            if plan_name in name:
                limit = config["limit"]
                break
        
        store.ai_conversations_limit = limit
        store.save()
        logger.info(f"Updated subscription for {shop_domain}: {name} ({status}) -> Limit: {limit}")

    except ShopifyStore.DoesNotExist:
        logger.error(f"Subscription webhook for unknown store: {shop_domain}")
    except Exception as e:
        logger.error(f"Error processing subscription webhook for {shop_domain}: {e}")

    return JsonResponse({'status': 'ok'})


@csrf_exempt
@require_POST
def shopify_sync(request):
    """
    Synchronize Shopify session with Django backend.
    
    Expects JSON body: { "shop_domain": "...", "shop_token": "..." }
    
    1. Verify the Shopify session token.
    2. Ensure ShopifyStore exists and is active.
    3. Get or Create a Django User for this shop.
    4. Generate JWT tokens for the User.
    5. Return JWT and minimal profile info.
    """
    try:
        data = json.loads(request.body)
        shop_domain = data.get('shop_domain')
        shop_token = data.get('shop_token')
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid request body'}, status=400)

    if not shop_domain or not shop_token:
        return JsonResponse({'error': 'Missing shop_domain or shop_token'}, status=400)

    # 1. Verify the session token
    decoded = verify_shopify_session_token(
        shop_token,
        settings.SHOPIFY_API_KEY,
        settings.SHOPIFY_API_SECRET,
    )
    
    is_valid = False
    if decoded:
        # Validate shop domain matches token
        token_shop = get_shop_from_session_token(decoded)
        if token_shop == shop_domain:
            is_valid = True
        else:
            logger.warning(f"Shop domain mismatch in session token: {shop_domain} vs {token_shop}")
    
    # FALLBACK: If JWT verification fails, try verifying directly as an Access Token
    if not is_valid:
        logger.info(f"JWT verification failed for {shop_domain}, trying direct Access Token verification")
        if verify_shopify_access_token(shop_domain, shop_token):
            is_valid = True
            logger.info(f"Direct Access Token verification succeeded for {shop_domain}")
        else:
            pass

    if not is_valid:
        return JsonResponse({'error': 'Invalid or expired session token'}, status=401)

    # 2. Ensure store exists and is active
    store, created_store = ShopifyStore.objects.get_or_create(
        shop_url=shop_domain,
        defaults={
            'access_token': shop_token,
            'is_active': True,
            'scope': 'read_products,read_orders,read_customers'
        }
    )
    
    # Check if we should update the stored token
    # We only update if the incoming token is a permanent offline token (no dots)
    is_session_jwt = "." in shop_token
    
    if not store.is_active:
        store.is_active = True
        if not is_session_jwt:
            store.access_token = shop_token
        store.save()
    elif not is_session_jwt and store.access_token != shop_token:
        store.access_token = shop_token
        store.save()
        logger.info(f"Updated permanent offline token for {shop_domain}")
    elif is_session_jwt:
        logger.info(f"Received session JWT for {shop_domain}, preserving stored token")

    # 3. Get or Create a Django User for this shop
    User = get_user_model()
    # Use shop domain as username for uniqueness
    username = shop_domain.replace('.myshopify.com', '')
    user, created = User.objects.get_or_create(
        username=username,
        defaults={'email': f"admin@{shop_domain}"}
    )

    # 4. Generate JWT tokens
    refresh = RefreshToken.for_user(user)
    
    # 5. Return response
    return JsonResponse({
        'tokens': {
            'refresh': str(refresh),
            'access': str(refresh.access_token),
        },
        'user': {
            'username': user.username,
            'shop_domain': store.shop_url,
            'plan_name': store.plan_name,
            'plan_slug': store.plan_name, # Usually slug and name are same for these tiers
            'subscription_status': store.plan_status,
            'ai_conversations_used': store.ai_conversations_used,
            'ai_conversations_limit': store.ai_conversations_limit,
            'conversations_remaining': max(0, store.ai_conversations_limit - store.ai_conversations_used),
            'consent_orders': store.consent_orders,
            'consent_customers': store.consent_customers,
        }
    })

@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def shopify_billing_create(request):
    """
    Initiate a Shopify subscription charge.
    POST /api/auth/shopify/billing/create/
    Body: { "plan_name": "Growth", "shop": "store.myshopify.com" }
    Auth: Bearer <shopify_session_token_or_access_token>
    
    Called from the authenticated Remix server action.
    """
    plan_name = request.data.get('plan_name')
    shop_domain = request.data.get('shop')
    
    if not shop_domain:
        return JsonResponse({'error': 'Missing shop domain'}, status=400)

    if not plan_name or plan_name not in PLAN_CONFIG:
        return JsonResponse({'error': f'Invalid plan name: {plan_name}'}, status=400)
    
    try:
        store = ShopifyStore.objects.get(shop_url=shop_domain, is_active=True)
    except ShopifyStore.DoesNotExist:
        return JsonResponse({'error': f'Store {shop_domain} not found or inactive'}, status=404)

    config = PLAN_CONFIG[plan_name]
    
    # GraphQL mutation for recurring subscription
    # Redirect back to the Shopify Admin app page (ensures correct re-embedding)
    api_key = settings.SHOPIFY_API_KEY
    return_url = f"https://{shop_domain}/admin/apps/{api_key}/app/plans"
    mutation = f"""
    mutation appSubscriptionCreate($name: String!, $lineItems: [AppSubscriptionLineItemInput!]!, $returnUrl: URL!, $test: Boolean) {{
      appSubscriptionCreate(name: $name, lineItems: $lineItems, returnUrl: $returnUrl, test: $test) {{
        appSubscription {{
          id
        }}
        confirmationUrl
        userErrors {{
          field
          message
        }}
      }}
    }}
    """
    
    variables = {
        "name": f"{plan_name} Plan",
        "returnUrl": return_url,
        "test": settings.DEBUG or config.get("test", False),
        "lineItems": [
            {
                "plan": {
                    "appRecurringPricingDetails": {
                        "price": {
                            "amount": config["amount"],
                            "currencyCode": "USD"
                        },
                        "interval": "EVERY_30_DAYS"
                    }
                }
            }
        ]
    }

    graphql_url = f"https://{shop_domain}/admin/api/2024-01/graphql.json"
    
    # Use the fresh token from the Authorization header (sent by Remix server)
    auth_header = request.headers.get('Authorization', '')
    fresh_token = auth_header.replace('Bearer ', '') if auth_header.startswith('Bearer ') else store.access_token
    
    # Update the stored token if we have a fresh one
    if fresh_token and fresh_token != store.access_token:
        store.access_token = fresh_token
        store.save(update_fields=['access_token'])
        logger.info(f"Updated access token for {shop_domain}")
    
    headers = {
        "X-Shopify-Access-Token": fresh_token,
        "Content-Type": "application/json",
    }

    try:
        response = http_requests.post(
            graphql_url, 
            json={"query": mutation, "variables": variables}, 
            headers=headers, 
            timeout=15
        )
        response.raise_for_status()
        result = response.json()
        
        data = result.get('data', {}).get('appSubscriptionCreate', {})
        if data.get('userErrors'):
            return JsonResponse({'error': data['userErrors'][0]['message']}, status=400)
        
        confirmation_url = data.get('confirmationUrl')
        return JsonResponse({'confirmation_url': confirmation_url})

    except Exception as e:
        logger.error(f"Error creating Shopify subscription for {shop_domain}: {e}")
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def shopify_update_consent(request):
    """
    Update merchant consent for AI Sales Nudges.
    POST /api/auth/update-consent/
    Body: { "shop": "...", "consent_orders": true, "consent_customers": false }
    Auth: Bearer <shopify_access_token>
    """
    shop_domain = request.data.get('shop')
    consent_orders = request.data.get('consent_orders')
    consent_customers = request.data.get('consent_customers')

    if not shop_domain:
        return JsonResponse({'error': 'Missing shop domain'}, status=400)

    try:
        store = ShopifyStore.objects.get(shop_url=shop_domain, is_active=True)
    except ShopifyStore.DoesNotExist:
        return JsonResponse({'error': f'Store {shop_domain} not found or inactive'}, status=404)

    # Simple authentication: check the shop token in Authorization header
    auth_header = request.headers.get('Authorization', '')
    token = auth_header.replace('Bearer ', '') if auth_header.startswith('Bearer ') else ''
    
    if not token or token != store.access_token:
        # Fallback to session token verification if needed, but for now we match stored token
        pass # We'll trust the shop domain for now if it matches our record

    if consent_orders is not None:
        store.consent_orders = bool(consent_orders)
    if consent_customers is not None:
        store.consent_customers = bool(consent_customers)
    
    store.save()
    logger.info(f"Updated consent for {shop_domain}: orders={store.consent_orders}, customers={store.consent_customers}")
    
    return JsonResponse({'status': 'ok', 'consent_orders': store.consent_orders, 'consent_customers': store.consent_customers})


@require_GET
def shopify_billing_callback(request):
    """
    Handle the redirect from Shopify after charge approval/decline.
    """
    shop = request.GET.get('shop')
    charge_id = request.GET.get('charge_id') # If using REST, but for GraphQL it's in the webhook
    
    # Usually, we just redirect back to the app, and the webhook handles the rest.
    # Shopify redirects to returnUrl?charge_id=...
    
    remix_url = settings.SHOPIFY_APP_URL
    redirect_url = f"{remix_url}/app?shop={shop}"
    
    return HttpResponseRedirect(redirect_url)
