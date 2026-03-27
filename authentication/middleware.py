import json
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from django.contrib.auth import get_user_model
import logging

logger = logging.getLogger(__name__)
User = get_user_model()

@database_sync_to_async
def get_user_from_token(token_string):
    try:
        # Validate the access token
        access_token = AccessToken(token_string)
        user_id = access_token.payload.get('user_id')
        if not user_id:
            return AnonymousUser()
        
        return User.objects.get(id=user_id)
    except (InvalidToken, TokenError, User.DoesNotExist) as e:
        logger.warning(f"WebSocket JWT Auth failed: {str(e)}")
        return AnonymousUser()
    except Exception as e:
        logger.error(f"Unexpected error in WebSocket JWT Auth: {str(e)}")
        return AnonymousUser()

class TokenAuthMiddleware:
    """
    Custom middleware for Django Channels to authenticate users via JWT in the query string.
    Example: ws://localhost:8000/ws/chat/1/?token=<JWT_TOKEN>
    """
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        # Extract token from query string
        query_string = scope.get('query_string', b'').decode()
        token = None
        
        if 'token=' in query_string:
            # Simple split to find token value
            parts = query_string.split('token=')
            if len(parts) > 1:
                token = parts[1].split('&')[0]

        if token:
            scope['user'] = await get_user_from_token(token)
        else:
            scope['user'] = AnonymousUser()

        return await self.inner(scope, receive, send)
