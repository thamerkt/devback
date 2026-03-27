import hashlib
import hmac
import re
import time
import base64
import json
import logging
import requests as http_requests
from urllib.parse import urlencode

logger = logging.getLogger(__name__)


def validate_shop_url(shop):
    """
    Validate that the shop URL is a legitimate Shopify domain.
    Must match the pattern: {store-name}.myshopify.com
    Returns True if valid, False otherwise.
    """
    if not shop:
        return False
    # Pattern recommended by Shopify docs for robustness
    pattern = r'^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$'
    return bool(re.match(pattern, shop))


def verify_shopify_hmac(query_params, secret):
    """
    Verify the HMAC signature on Shopify OAuth callback requests.
    
    Shopify signs requests with HMAC-SHA256 using the app's API secret.
    We must verify this to ensure the request genuinely came from Shopify.
    """
    if 'hmac' not in query_params:
        return False

    received_hmac = query_params.pop('hmac')

    # Sort remaining params and join as key=value&key=value
    # We do NOT use urlencode here because Shopify calculates HMAC on the unescaped values 
    # of the query parameters in the specific order.
    sorted_params = "&".join([f"{k}={v}" for k, v in sorted(query_params.items())])

    computed_hmac = hmac.new(
        secret.encode('utf-8'),
        sorted_params.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(computed_hmac, received_hmac)


def verify_shopify_webhook_hmac(body, hmac_header, secret):
    """
    Verify the HMAC signature on incoming Shopify webhook requests.
    
    Webhooks use the X-Shopify-Hmac-Sha256 header with a base64-encoded
    HMAC-SHA256 digest of the raw request body.
    
    Args:
        body: raw request body (bytes)
        hmac_header: value of X-Shopify-Hmac-Sha256 header
        secret: the SHOPIFY_API_SECRET

    Returns:
        True if HMAC is valid, False otherwise
    """
    if not hmac_header:
        return False

    computed_hmac = base64.b64encode(
        hmac.new(
            secret.encode('utf-8'),
            body,
            hashlib.sha256
        ).digest()
    ).decode('utf-8')

    return hmac.compare_digest(computed_hmac, hmac_header)


def verify_shopify_session_token(token, api_key, secret):
    """
    Verify and decode a Shopify session token (JWT) from an embedded app.
    
    Shopify embedded apps use App Bridge to generate session tokens.
    These are JWTs signed with the app's API secret.
    
    Args:
        token: the JWT session token string
        api_key: SHOPIFY_API_KEY
        secret: SHOPIFY_API_SECRET
    
    Returns:
        dict with decoded payload if valid, None if invalid
    """
    try:
        import jwt

        decoded = jwt.decode(
            token,
            secret,
            algorithms=['HS256'],
            audience=api_key,
        )

        # Validate required claims
        required_claims = ['iss', 'dest', 'sub', 'exp', 'nbf', 'iat', 'jti']
        for claim in required_claims:
            if claim not in decoded:
                logger.warning(f"Session token missing required claim: {claim}")
                return None

        # Verify issuer matches destination (ignoring /admin suffix)
        iss_host = decoded['iss'].replace('https://', '').replace('http://', '').rstrip('/')
        if iss_host.endswith('/admin'):
            iss_host = iss_host[:-6]
            
        dest_host = decoded['dest'].replace('https://', '').replace('http://', '').rstrip('/')
        
        if iss_host != dest_host:
            logger.warning(f"Session token iss/dest mismatch: {iss_host} vs {dest_host}")
            return None

        return decoded

    except jwt.ExpiredSignatureError:
        logger.warning("Session token has expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid session token (Secret length {len(secret)}): {e}")
        return None
    except Exception as e:
        logger.error(f"Error verifying session token: {type(e).__name__}: {e}")
        return None


def get_shop_from_session_token(decoded_token):
    """
    Extract the shop domain from a decoded Shopify session token.
    
    Args:
        decoded_token: decoded JWT payload dict
    
    Returns:
        shop domain string (e.g. 'store.myshopify.com')
    """
    dest = decoded_token.get('dest', '')
    return dest.replace('https://', '').replace('http://', '')
def verify_shopify_access_token(shop, access_token):
    """
    Verify a Shopify Access Token (shpat_... or shpss_...) by calling
    the shop.json API endpoint.
    
    Args:
        shop: the shop domain (e.g. 'store.myshopify.com')
        access_token: the Shopify Access Token
    
    Returns:
        bool: True if valid, False otherwise
    """
    if not access_token or not shop:
        return False
    
    # CRITICAL: If the token contains dots, it's a JWT (Session Token), 
    # not a permanent offline access token. We must reject it here
    # to force the OAuth flow to provide a real offline token.
    if "." in access_token:
        logger.warning(f"Rejecting JWT as permanent access token for {shop}")
        return False
    
    # We use a simple but effective check: try to fetch shop details
    # using the provided access token.
    api_url = f"https://{shop}/admin/api/2024-01/shop.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json"
    }
    
    try:
        # We only need a quick check, so use a short timeout
        logger.info(f"Checking Shopify Access Token for {shop} at {api_url}")
        response = http_requests.get(api_url, headers=headers, timeout=10)
        logger.info(f"Shopify API responded with status {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            # Double check the domain from the API response matches what was requested
            api_shop_domain = data.get('shop', {}).get('myshopify_domain')
            logger.info(f"Shop domain in API: {api_shop_domain}")
            if api_shop_domain == shop:
                return True
            else:
                logger.warning(f"Shop domain mismatch during token verification: {shop} vs {api_shop_domain}")
                return False
        else:
            logger.warning(f"Shopify token verification failed for {shop} (status {response.status_code}): {response.text}")
            return False
    except Exception as e:
        logger.error(f"Error verifying Shopify access token for {shop}: {e}")
        return False
