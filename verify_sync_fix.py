import json
import unittest
from unittest.mock import patch, MagicMock
import os
import django
import sys

# Setup Django environment
sys.path.append('/home/digitalberry/backend/myshopaap/myshopapp')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myshopapp.settings')
django.setup()

from django.test import RequestFactory
from authentication.views import shopify_sync
from authentication.models import ShopifyStore
from django.contrib.auth import get_user_model

class SyncVerificationTest(unittest.TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.shop_domain = "test-store.myshopify.com"
        self.shop_token = "shpat_test_token_123"
        
        # Ensure test store exists
        ShopifyStore.objects.update_or_create(
            shop_url=self.shop_domain,
            defaults={'access_token': self.shop_token, 'is_active': True}
        )

    @patch('authentication.views.verify_shopify_session_token')
    @patch('authentication.views.verify_shopify_access_token')
    def test_sync_with_access_token_fallback(self, mock_verify_access, mock_verify_session):
        # Case 1: JWT fails, but Access Token verification succeeds
        mock_verify_session.return_value = None
        mock_verify_access.return_value = True
        
        payload = {
            "shop_domain": self.shop_domain,
            "shop_token": self.shop_token
        }
        request = self.factory.post(
            '/api/auth/shopify-sync/',
            data=json.dumps(payload),
            content_type='application/json'
        )
        
        response = shopify_sync(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn('tokens', data)
        self.assertEqual(data['user']['shop_domain'], self.shop_domain)
        print("SUCCESS: Sync with Access Token fallback verified.")

    @patch('authentication.views.verify_shopify_session_token')
    @patch('authentication.views.verify_shopify_access_token')
    def test_sync_failure(self, mock_verify_access, mock_verify_session):
        # Case 2: Both fail
        mock_verify_session.return_value = None
        mock_verify_access.return_value = False
        
        payload = {
            "shop_domain": self.shop_domain,
            "shop_token": "invalid_token"
        }
        request = self.factory.post(
            '/api/auth/shopify-sync/',
            data=json.dumps(payload),
            content_type='application/json'
        )
        
        response = shopify_sync(request)
        self.assertEqual(response.status_code, 401)
        print("SUCCESS: Sync failure verified for invalid token.")

if __name__ == "__main__":
    unittest.main()
