import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import Message, Conversation
from .utils import parse_n8n_response
from django.contrib.auth import get_user_model

User = get_user_model()



class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.conversation_id = self.scope['url_route']['kwargs']['conversation_id']
        self.room_group_name = f'chat_{self.conversation_id}'

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

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
        shopify_token = text_data_json.get('shopify_token')
        shopify_domain = text_data_json.get('shopify_domain')
        
        print(f"DEBUG: Received message. Products: {len(products)}, Orders: {total_orders}, Customers: {total_customers}")
        print(f"DEBUG: Shopify Domain: {shopify_domain}")
        if shopify_token:
            print(f"DEBUG: Shopify Token: PRESENT ({shopify_token})")
        else:
            print("DEBUG: Shopify Token: MISSING (NULL/EMPTY)")
        # Save user message to database
        message = await self.save_message(sender_id, message_content, is_ai=False)

        # Send user message to room group
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'message': message_content,
                'sender': message.sender.username,
                'is_ai': False,
                'timestamp': str(message.timestamp),
                'token': shopify_token,
                'domain': shopify_domain,
                'client_message_id': client_message_id
            }
        )

        # Call n8n webhook for AI response with full store context and credentials
        await self.handle_ai_response(message, products, total_products, total_customers, total_orders, locations, shop_details, shop_faqs, shopify_token, shopify_domain)

    async def handle_ai_response(self, user_message, products, total_products, total_customers, total_orders, locations, shop_details, shop_faqs, shopify_token, shopify_domain):
        import requests
        from django.conf import settings
        import threading
        import asyncio

        # Prepare the payload in the async context to avoid lazy-loading issues in threads
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

        # Capture the current event loop to use it in the thread
        loop = asyncio.get_event_loop()
        webhook_url = settings.N8N_WEBHOOK_URL

        def call_n8n():
            print(f"DEBUG: Attempting to call n8n webhook at: {webhook_url}")
            try:
                headers = {
                    "X-N8N-API-KEY": settings.N8N_WEBHOOK_SECRET
                }
                print(f"DEBUG: Sending to n8n with API key: {settings.N8N_WEBHOOK_SECRET[:3]}...***")
                response = requests.post(webhook_url, json=payload, headers=headers, timeout=15)
                print(f"DEBUG: n8n response status: {response.text}")
                
                if response.status_code == 200:
                    ai_content = response.text
                    print(f"DEBUG: AI response received from n8n ({len(ai_content)} chars)")
                    
                    if ai_content:
                        # Schedule saving and broadcasting the AI message using the captured loop
                        asyncio.run_coroutine_threadsafe(
                            self.save_and_broadcast_ai_message(ai_content),
                            loop
                        )
                else:
                    print(f"ERROR: n8n returned non-200 status: {response.status_code}. Response: {response.text[:200]}")
                    
            except requests.exceptions.Timeout:
                print("ERROR: Connection to n8n timed out after 15 seconds.")
            except requests.exceptions.RequestException as e:
                print(f"ERROR: Failed to connect to n8n: {e}")
            except Exception as e:
                print(f"ERROR: Unexpected error in n8n call thread: {e}")

        # Run in a separate thread to avoid blocking the main async loop
        threading.Thread(target=call_n8n).start()

    async def save_and_broadcast_ai_message(self, content):
        import asyncio
        import random

        try:
            # Attempt to parse n8n response using robust helper
            messages = parse_n8n_response(content)
            print(f"DEBUG: Parsed AI response: {messages}")
        except Exception as e:
            print(f"ERROR: Failed to parse n8n response: {e}")
            # Fallback for plain text or failures
            messages = [{"message": content, "type": "written"}]

        for msg_data in messages:
            msg_text = msg_data.get("message", "")
            msg_type = msg_data.get("type", "written")

            if not msg_text:
                continue

            # 1. Handle "typing" status
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
                await asyncio.sleep(1.5)
                continue

            # 2. Handle "written" messages
            # Human-like delay based on message length
            delay = min(len(msg_text) * 0.05, 3.0) + random.uniform(0.5, 1.5)
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
        conversation = Conversation.objects.get(id=self.conversation_id)
        if is_ai:
            # For AI, we can either set a specific AI user or use None if the model allows
            # Here we'll just use the first user of the conversation or first user overall
            sender = conversation.participants.first() or User.objects.first()
        else:
            try:
                # Try to get user by ID if it's a number, otherwise fallback to first user
                if sender_id and str(sender_id).isdigit():
                    sender = User.objects.get(id=int(sender_id))
                else:
                    sender = User.objects.first()
            except (User.DoesNotExist, ValueError):
                sender = User.objects.first()
            
        return Message.objects.create(
            conversation=conversation,
            sender=sender,
            content=content,
            is_ai=is_ai
        )
