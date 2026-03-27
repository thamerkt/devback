import json
import logging
import aiohttp
import asyncio

from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from asgiref.sync import async_to_sync

from .models import Message, Conversation
from .utils import parse_n8n_response

logger = logging.getLogger(__name__)
User = get_user_model()


async def _call_n8n(payload):
    """Async helper: POST to n8n webhook and return raw response text."""
    webhook_url = settings.N8N_WEBHOOK_URL
    headers = {"X-N8N-API-KEY": settings.N8N_WEBHOOK_SECRET}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                webhook_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status == 200:
                    return await response.text()
                else:
                    error_text = await response.text()
                    logger.error(f"n8n returned {response.status}: {error_text[:200]}")
                    return None
    except asyncio.TimeoutError:
        logger.error("n8n call timed out after 15s")
    except aiohttp.ClientError as e:
        logger.error(f"n8n connection error: {e}")
    except Exception as e:
        logger.error(f"Unexpected n8n error: {e}")
    return None


def _call_n8n_sync(payload):
    """Synchronous wrapper around the async n8n call."""
    return async_to_sync(_call_n8n)(payload)


@api_view(["POST"])
@permission_classes([AllowAny])
def storefront_chat_message(request):
    """
    REST endpoint for the storefront chat widget.
    Accepts a message, proxies to n8n, and returns the AI response.

    POST /api/chat/message/
    Body: { "message": "...", "shop": "store.myshopify.com", "conversation_id": "..." }
    """
    print(f"request.data: {request.data}")
    message_text = request.data.get("message", "").strip()

    shop_domain = request.data.get("shop", "").strip()
    conversation_id = request.data.get("conversation_id", "").strip()
    session_id = request.data.get("session_id", "").strip()

    if not message_text:
        return Response({"error": "message is required"}, status=400)
    if not shop_domain:
        return Response({"error": "shop is required"}, status=400)
    if not conversation_id:
        return Response({"error": "conversation_id is required"}, status=400)

    # Validate shop
    from authentication.models import ShopifyStore
    store = ShopifyStore.objects.filter(shop_url=shop_domain, is_active=True).first()
    if not store:
        return Response({"error": "Unknown or inactive store"}, status=403)

    # Check plan limits
    if store.ai_conversations_used >= store.ai_conversations_limit:
        return Response(
            {
                "error": "limit_reached",
                "message": "AI conversation limit reached. Please upgrade your plan.",
            },
            status=429,
        )

    # Get or create conversation
    conversation, _ = Conversation.objects.get_or_create(external_id=conversation_id)

    # Get or create the storefront guest user
    guest_user, _ = User.objects.get_or_create(username="storefront_guest")
    if not conversation.participants.filter(id=guest_user.id).exists():
        conversation.participants.add(guest_user)

    # Save user message
    user_msg = Message.objects.create(
        conversation=conversation,
        sender=guest_user,
        content=message_text,
        is_ai=False,
    )

    # Build n8n payload
    shopify_token = store.access_token if store else None
    payload = {
        "id": user_msg.id,
        "conversation_id": conversation.id,
        "session_id": session_id,
        "sender": "storefront_guest",
        "content": message_text,
        "is_ai": False,
        "timestamp": str(user_msg.timestamp),
        "products": [],
        "total_products": 0,
        "total_customers": 0,
        "total_orders": 0,
        "locations": [],
        "shop_details": {},
        "shop_faqs": [],
        "shopify_token": shopify_token,
        "shopify_domain": shop_domain,
        "persona": "human_witty_serious",
    }

    # Call n8n
    raw_response = _call_n8n_sync(payload)

    if not raw_response:
        return Response(
            {"error": "AI service unavailable", "messages": []}, status=502
        )

    # Parse response
    try:
        parsed = parse_n8n_response(raw_response)
    except Exception as e:
        logger.error(f"Failed to parse n8n response: {e}")
        parsed = [{"message": raw_response, "type": "written"}]

    # Save AI messages and build response
    ai_messages = []
    for msg_data in parsed:
        msg_text = msg_data.get("message", "")
        if not msg_text:
            continue

        ai_msg = Message.objects.create(
            conversation=conversation,
            sender=guest_user,
            content=msg_text,
            is_ai=True,
        )

        # Increment usage
        store.ai_conversations_used += 1
        store.save()

        ai_messages.append(
            {
                "message": msg_text,
                "type": msg_data.get("type", "written"),
                "timestamp": str(ai_msg.timestamp),
            }
        )

    return Response({"messages": ai_messages})


@api_view(["POST"])
@permission_classes([AllowAny])
def track_action(request):
    """
    REST endpoint to track storefront visitor actions (page views, etc.).
    Sends browsing context to n8n and returns any proactive AI nudge.

    POST /api/chat/track-action/
    Body: {
        "session_id": "abc123",
        "shop": "store.myshopify.com",
        "page_url": "https://store.com/products/...",
        "page_title": "Cool Product",
        "action_type": "page_view",
        "referrer": "https://store.com/collections/all",
        "extra_data": { "product_id": "123", "price": "29.99" }
    }
    """
    session_id = request.data.get("session_id", "").strip()
    shop_domain = request.data.get("shop", "").strip()
    page_url = request.data.get("page_url", "").strip()
    page_title = request.data.get("page_title", "").strip()
    action_type = request.data.get("action_type", "page_view").strip()
    referrer = request.data.get("referrer", "").strip()
    extra_data = request.data.get("extra_data", {})

    if not session_id:
        return Response({"error": "session_id is required"}, status=400)
    if not shop_domain:
        return Response({"error": "shop is required"}, status=400)
    if not page_url:
        return Response({"error": "page_url is required"}, status=400)

    # Validate shop
    from authentication.models import ShopifyStore
    store = ShopifyStore.objects.filter(shop_url=shop_domain, is_active=True).first()
    if not store:
        return Response({"error": "Unknown or inactive store"}, status=403)

    # Save the action
    from .models import ClientAction, ProactiveMessage
    action = ClientAction.objects.create(
        session_id=session_id,
        shop_domain=shop_domain,
        action_type=action_type,
        page_url=page_url,
        page_title=page_title,
        referrer=referrer,
        extra_data=extra_data if isinstance(extra_data, dict) else {},
    )

    # Build browsing context: last 10 actions for this session
    recent_actions = ClientAction.objects.filter(
        session_id=session_id,
        shop_domain=shop_domain,
    ).order_by('-timestamp')[:10]

    browsing_history = [
        {
            "action_type": a.action_type,
            "page_url": a.page_url,
            "page_title": a.page_title,
            "extra_data": a.extra_data,
            "timestamp": str(a.timestamp),
        }
        for a in recent_actions
    ]

    # Build n8n payload
    payload = {
        "session_id": session_id,
        "shop_domain": shop_domain,
        "current_page": {
            "url": page_url,
            "title": page_title,
            "action_type": action_type,
            "extra_data": extra_data,
        },
        "browsing_history": browsing_history,
        "total_pages_visited": len(browsing_history),
        "shopify_token": store.access_token,
        "persona": "proactive_salesperson",
    }

    # Call the action webhook
    webhook_url = settings.N8N_ACTION_WEBHOOK_URL
    logger.info(f"Calling n8n action webhook at: {webhook_url} for session {session_id}")

    nudge_message = None
    try:
        raw_response = _call_n8n_sync_to(payload, webhook_url)

        if raw_response:
            parsed = parse_n8n_response(raw_response)
            for msg_data in parsed:
                msg_text = msg_data.get("message", "").strip()
                if msg_text:
                    nudge = ProactiveMessage.objects.create(
                        session_id=session_id,
                        shop_domain=shop_domain,
                        message=msg_text,
                        trigger_action=action,
                    )
                    nudge_message = msg_text
                    logger.info(f"Proactive nudge created for session {session_id}: {msg_text[:60]}")
                    break  # Only use the first message as the nudge

    except Exception as e:
        logger.error(f"Error calling action webhook: {e}")

    response_data = {"status": "tracked", "action_id": action.id}
    if nudge_message:
        response_data["nudge"] = nudge_message

    return Response(response_data)


@api_view(["GET"])
@permission_classes([AllowAny])
def get_nudges(request):
    """
    Returns undelivered proactive messages for a session.

    GET /api/chat/nudges/?session_id=abc123&shop=store.myshopify.com
    """
    session_id = request.query_params.get("session_id", "").strip()
    shop_domain = request.query_params.get("shop", "").strip()

    if not session_id or not shop_domain:
        return Response({"error": "session_id and shop are required"}, status=400)

    from .models import ProactiveMessage
    nudges = ProactiveMessage.objects.filter(
        session_id=session_id,
        shop_domain=shop_domain,
        is_delivered=False,
    )

    messages = []
    for nudge in nudges:
        messages.append({
            "id": nudge.id,
            "message": nudge.message,
            "created_at": str(nudge.created_at),
        })
        nudge.is_delivered = True
        nudge.save()

    return Response({"nudges": messages})


def _call_n8n_sync_to(payload, webhook_url):
    """Synchronous n8n call to a specific webhook URL."""
    import aiohttp as _aiohttp

    async def _call():
        headers = {"X-N8N-API-KEY": settings.N8N_WEBHOOK_SECRET}
        try:
            async with _aiohttp.ClientSession() as session:
                async with session.post(
                    webhook_url,
                    json=payload,
                    headers=headers,
                    timeout=_aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status == 200:
                        return await response.text()
                    else:
                        error_text = await response.text()
                        logger.error(f"n8n returned {response.status}: {error_text[:200]}")
                        return None
        except asyncio.TimeoutError:
            logger.error("n8n action call timed out after 15s")
        except _aiohttp.ClientError as e:
            logger.error(f"n8n action connection error: {e}")
        except Exception as e:
            logger.error(f"Unexpected n8n action error: {e}")
        return None

    return async_to_sync(_call)()
