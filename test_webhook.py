import requests
import json
import sys
import os
import django

# Setup Django for 'signal' mode
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myshopapp.settings')
try:
    django.setup()
    from chat.models import Message, Conversation
    from django.contrib.auth import get_user_model
    User = get_user_model()
    DJANGO_AVAILABLE = True
except Exception:
    DJANGO_AVAILABLE = False

def test_direct(url):
    print(f"Testing DIRECT webhook to: {url}")
    payload = {
        "id": 999,
        "conversation_id": 1,
        "sender": "tester",
        "content": "Hello from direct test script!",
        "is_ai": False,
        "timestamp": "2023-01-01T00:00:00Z"
    }
    try:
        response = requests.post(url, json=payload, timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Response: {response.text}")
    except Exception as e:
        print(f"Error: {e}")

def test_signal():
    if not DJANGO_AVAILABLE:
        print("Django not properly configured for signal test.")
        return
    
    print("Testing SIGNAL-triggered webhook...")
    try:
        user = User.objects.first()
        conv = Conversation.objects.first()
        if not user or not conv:
            print("Error: Need at least one User and one Conversation in the DB.")
            return

        msg = Message.objects.create(
            conversation=conv,
            sender=user,
            content="Testing signal webhook from script!",
            is_ai=False
        )
        print(f"Created message ID {msg.id}. Webhook should trigger in background.")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    from myshopapp.settings import N8N_WEBHOOK_URL
    
    mode = sys.argv[1] if len(sys.argv) > 1 else "direct"
    
    if mode == "direct":
        test_direct(N8N_WEBHOOK_URL)
    elif mode == "signal":
        test_signal()
    else:
        print("Usage: python test_webhook.py [direct|signal]")
