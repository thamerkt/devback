import json
import asyncio
import aiohttp
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import Message, Conversation
from .utils import parse_n8n_response
from django.contrib.auth import get_user_model
import logging

logger = logging.getLogger(__name__)

User = get_user_model()



class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.conversation_id = self.scope['url_route']['kwargs']['conversation_id']
        self.room_group_name = f'chat_{self.conversation_id}'
        self.user = self.scope.get('user')
        
        # Get shop from query params (for storefront)
        query_params = dict(x.split('=') for x in self.scope['query_string'].decode().split('&') if '=' in x)
        self.shopify_domain = query_params.get('shop')

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        if (not self.user or self.user.is_anonymous) and not self.shopify_domain:
            # Reject connection if not authenticated AND no shop domain provided
            logger.warning(f"WebSocket connection rejected: AnonymousUser and no shop for conversation {self.conversation_id}")
            await self.accept()
            await self.close(code=4001)
            return

        if self.user and not self.user.is_anonymous:
            self.shopify_domain = f"{self.user.username}.myshopify.com"

        await self.accept()
        logger.info(f"WebSocket connected: {'User ' + self.user.username if self.user and not self.user.is_anonymous else 'Storefront'} for {self.shopify_domain}")

    async def disconnect(self, close_code):
        # Leave room group
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    # Receive message from WebSocket
    async def receive(self, text_data):
        text_data_json = json.loads(text_data)
        message_content = text_data_json['message']
        client_message_id = text_data_json.get('client_message_id')
        sender_id = text_data_json.get('sender_id')
        products = text_data_json.get('products', [])
        total_products = text_data_json.get('total_products', 0)
        total_customers = text_data_json.get('total_customers', 0)
        total_orders = text_data_json.get('total_orders', 0)
        locations = text_data_json.get('locations', [])
        shop_details = text_data_json.get('shop_details', {})
        shop_faqs = text_data_json.get('shop_faqs', [])
        
        # Use the shop domain identified during connect
        shopify_domain = self.shopify_domain
        sender_name = self.user.username if self.user and not self.user.is_anonymous else "Guest"
        
        # Fetch the verified token from DB
        store = await self.get_store(shopify_domain)
        shopify_token = store.access_token if store else None

        logger.info(f"Received message from {sender_name}. Products: {len(products)}, Orders: {total_orders}")
        if shopify_token:
            logger.info(f"Verified Shopify Token for {shopify_domain}: PRESENT")
        else:
            logger.warning(f"Verified Shopify Token for {shopify_domain}: MISSING")

        # Save user message to database
        message = await self.save_message(sender_id, message_content, is_ai=False)

        # Send user message to room group
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'message': message_content,
                'sender': sender_name,
                'is_ai': False,
                'timestamp': str(message.timestamp),
                'token': shopify_token,
                'domain': shopify_domain,
                'client_message_id': client_message_id
            }
        )

        # Send immediate typing status so the user knows the AI is thinking
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'message': "Thinking...",
                'sender': "AI Assistant",
                'is_ai': True,
                'msg_type': 'typing',
                'timestamp': str(asyncio.get_event_loop().time())
            }
        )

        # Check plan limits before calling AI
        if store:
            if store.ai_conversations_used >= store.ai_conversations_limit:
                logger.warning(f"Plan limit reached for {shopify_domain}: {store.ai_conversations_used}/{store.ai_conversations_limit}")
                limit_message = "⚠️ You have reached your AI conversation limit for this month. Please upgrade your plan in the 'Plans' section to continue using the AI Assistant."
                
                # Send limit warning to room group
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'chat_message',
                        'message': limit_message,
                        'sender': "System",
                        'is_ai': True,
                        'timestamp': str(asyncio.get_event_loop().time()),
                        'client_message_id': f"limit_{client_message_id}"
                    }
                )
                return

        # Call n8n webhook for AI response with full store context and credentials
        await self.handle_ai_response(message, products, total_products, total_customers, total_orders, locations, shop_details, shop_faqs, shopify_token, shopify_domain)

    @database_sync_to_async
    def get_store(self, shop_domain):
        from authentication.models import ShopifyStore
        return ShopifyStore.objects.filter(shop_url=shop_domain, is_active=True).first()

    async def handle_ai_response(self, user_message, products, total_products, total_customers, total_orders, locations, shop_details, shop_faqs, shopify_token, shopify_domain):
        from django.conf import settings

        payload = {
            "id": user_message.id,
            "conversation_id": user_message.conversation.id,
            "sender": user_message.sender.username,
            "content": user_message.content,
            "is_ai": False,
            "timestamp": str(user_message.timestamp),
            "products": products,
            "total_products": total_products,
            "total_customers": total_customers,
            "total_orders": total_orders,
            "locations": locations,
            "shop_details": shop_details,
            "shop_faqs": shop_faqs,
            "shopify_token": shopify_token,
            "shopify_domain": shopify_domain,
            "persona": "human_witty_serious"
        }

        webhook_url = settings.N8N_WEBHOOK_URL
        headers = {
            "X-N8N-API-KEY": settings.N8N_WEBHOOK_SECRET
        }

        logger.info(f"Calling n8n webhook at: {webhook_url}")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    webhook_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as response:
                    if response.status == 200:
                        ai_content = await response.text()
                        logger.info(f"AI response received from n8n ({len(ai_content)} chars)")

                        if ai_content:
                            await self.save_and_broadcast_ai_message(ai_content)
                    else:
                        error_text = await response.text()
                        logger.error(f"n8n returned non-200 status: {response.status}. Response: {error_text[:200]}")

        except asyncio.TimeoutError:
            logger.error("Connection to n8n timed out after 15 seconds.")
        except aiohttp.ClientError as e:
            logger.error(f"Failed to connect to n8n: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in n8n call: {e}")

    async def save_and_broadcast_ai_message(self, content):
        import random

        try:
            # Attempt to parse n8n response using robust helper
            messages = parse_n8n_response(content)
            logger.info(f"Parsed AI response: {messages}")
        except Exception as e:
            logger.error(f"Failed to parse n8n response: {e}")
            # Fallback for plain text or failures
            messages = [{"message": content, "type": "written"}]

        for msg_data in messages:
            msg_text = msg_data.get("message", "")
            msg_type = msg_data.get("type", "written")

            if not msg_text:
                continue

            # 1. Handle "typing" status if requested
            if msg_type == "typing":
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'chat_message',
                        'message': msg_text,
                        'sender': "AI Assistant",
                        'is_ai': True,
                        'msg_type': 'typing',
                        'timestamp': str(asyncio.get_event_loop().time())
                    }
                )
                # Small delay to let the "typing" message be seen
                await asyncio.sleep(2.0)
                # After delay, we don't continue anymore. 
                # We want this message to "solidify" as a regular written message.
                msg_type = "written" 

            # 2. Handle final "written" messages (or solidified typing messages)
            # Human-like delay based on message length (for realism)
            delay = min(len(msg_text) * 0.02, 2.0) + random.uniform(0.5, 1.0)
            await asyncio.sleep(delay)

            # Save AI message to database
            ai_message = await self.save_message(None, msg_text, is_ai=True)

            # Broadcast the actual message
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'chat_message',
                    'message': ai_message.content,
                    'sender': "AI Assistant",
                    'is_ai': True,
                    'msg_type': 'written',
                    'timestamp': str(ai_message.timestamp)
                }
            )

    # Receive message from room group
    async def chat_message(self, event):
        # Send message to WebSocket
        await self.send(text_data=json.dumps({
            'message': event['message'],
            'sender': event['sender'],
            'is_ai': event['is_ai'],
            'msg_type': event.get('msg_type', 'written'),
            'timestamp': event['timestamp'],
            'client_message_id': event.get('client_message_id')
        }))

    @database_sync_to_async
    def save_message(self, sender_id, content, is_ai=False):
        # Handle numeric vs random string conversation IDs
        if str(self.conversation_id).isdigit():
            conversation, _ = Conversation.objects.get_or_create(id=int(self.conversation_id))
        else:
            # Storefront guest - use external_id to uniquely identify the guest session
            conversation, _ = Conversation.objects.get_or_create(external_id=self.conversation_id)

        if is_ai:
            sender = conversation.participants.first() or User.objects.first()
        else:
            try:
                if sender_id and str(sender_id).isdigit():
                    sender = User.objects.get(id=int(sender_id))
                elif self.user and not self.user.is_anonymous:
                    sender = self.user
                else:
                    # Storefront guest - use a special placeholder user
                    sender, _ = User.objects.get_or_create(username='storefront_guest')
                    if not conversation.participants.filter(id=sender.id).exists():
                        conversation.participants.add(sender)
            except Exception:
                sender = User.objects.first()
            
        msg = Message.objects.create(
            conversation=conversation,
            sender=sender,
            content=content,
            is_ai=is_ai
        )

        # Increment AI usage counter if this is an AI response
        if is_ai:
            shopify_domain = self.shopify_domain
            from authentication.models import ShopifyStore
            store = ShopifyStore.objects.filter(shop_url=shopify_domain).first()
            if store:
                store.ai_conversations_used += 1
                store.save()
                logger.info(f"Incremented AI usage for {shopify_domain}: {store.ai_conversations_used}/{store.ai_conversations_limit}")

        return msg
