import hashlib
import hmac
from unittest.mock import patch, MagicMock
from urllib.parse import urlencode

from django.test import TestCase, RequestFactory, Client
from django.conf import settings

from authentication.models import ShopifyStore
from authentication.utils import validate_shop_url, verify_shopify_hmac, verify_shopify_webhook_hmac


class ValidateShopUrlTest(TestCase):
    def test_valid_shop_urls(self):
        self.assertTrue(validate_shop_url('my-store.myshopify.com'))
        self.assertTrue(validate_shop_url('store123.myshopify.com'))
        self.assertTrue(validate_shop_url('test.myshopify.com'))

    def test_invalid_shop_urls(self):
        self.assertFalse(validate_shop_url(''))
        self.assertFalse(validate_shop_url(None))
        self.assertFalse(validate_shop_url('not-shopify.com'))
        self.assertFalse(validate_shop_url('store.myshopify.com.evil.com'))
        self.assertFalse(validate_shop_url('.myshopify.com'))
        self.assertFalse(validate_shop_url('https://store.myshopify.com'))


class VerifyHmacTest(TestCase):
    def test_valid_hmac(self):
        secret = 'test-secret'
        params = {'shop': 'test.myshopify.com', 'timestamp': '1234567890', 'code': 'abc123'}

        # Build expected HMAC
        sorted_params = urlencode(sorted(params.items()))
        expected_hmac = hmac.new(
            secret.encode('utf-8'),
            sorted_params.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        params_with_hmac = {**params, 'hmac': expected_hmac}
        self.assertTrue(verify_shopify_hmac(params_with_hmac, secret))

    def test_invalid_hmac(self):
        params = {'shop': 'test.myshopify.com', 'hmac': 'invalid-hmac'}
        self.assertFalse(verify_shopify_hmac(params, 'test-secret'))

    def test_missing_hmac(self):
        params = {'shop': 'test.myshopify.com'}
        self.assertFalse(verify_shopify_hmac(params, 'test-secret'))


class ShopifyStoreModelTest(TestCase):
    def test_create_store(self):
        store = ShopifyStore.objects.create(
            shop_url='test-store.myshopify.com',
            access_token='shpat_test123',
            scope='read_products,read_orders',
        )
        self.assertEqual(store.shop_url, 'test-store.myshopify.com')
        self.assertTrue(store.is_active)
        self.assertIn('active', str(store))

    def test_get_api_headers(self):
        store = ShopifyStore.objects.create(
            shop_url='test.myshopify.com',
            access_token='shpat_test123',
        )
        headers = store.get_api_headers()
        self.assertEqual(headers['X-Shopify-Access-Token'], 'shpat_test123')
        self.assertEqual(headers['Content-Type'], 'application/json')


class ShopifyInstallViewTest(TestCase):
    def test_install_with_valid_shop(self):
        client = Client()
        response = client.get('/auth/shopify/install/', {'shop': 'test-store.myshopify.com'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('oauth/authorize', response.url)
        self.assertIn('test-store.myshopify.com', response.url)

        # Verify nonce was stored
        store = ShopifyStore.objects.get(shop_url='test-store.myshopify.com')
        self.assertTrue(len(store.nonce) > 0)

    def test_install_with_invalid_shop(self):
        client = Client()
        response = client.get('/auth/shopify/install/', {'shop': 'evil.com'})
        self.assertEqual(response.status_code, 400)

    def test_install_without_shop(self):
        client = Client()
        response = client.get('/auth/shopify/install/')
        self.assertEqual(response.status_code, 400)


class ShopifyCallbackViewTest(TestCase):
    def test_callback_rejects_invalid_hmac(self):
        ShopifyStore.objects.create(
            shop_url='test.myshopify.com',
            nonce='test-nonce',
        )
        client = Client()
        response = client.get('/auth/shopify/callback/', {
            'shop': 'test.myshopify.com',
            'code': 'abc123',
            'hmac': 'invalid',
            'state': 'test-nonce',
            'timestamp': '1234567890',
        })
        self.assertEqual(response.status_code, 400)

    def test_callback_rejects_invalid_shop(self):
        client = Client()
        response = client.get('/auth/shopify/callback/', {
            'shop': 'evil.com',
            'code': 'abc123',
            'hmac': 'test',
            'state': 'test-nonce',
        })
        self.assertEqual(response.status_code, 400)


class VerifySessionTokenViewTest(TestCase):
    def test_missing_auth_header(self):
        client = Client()
        response = client.post('/auth/shopify/verify-token/')
        self.assertEqual(response.status_code, 401)

    def test_invalid_token(self):
        client = Client()
        response = client.post(
            '/auth/shopify/verify-token/',
            HTTP_AUTHORIZATION='Bearer invalid-token',
        )
        self.assertEqual(response.status_code, 401)
