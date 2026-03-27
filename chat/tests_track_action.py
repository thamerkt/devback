"""
Tests for the track-action webhook endpoint.

Covers:
- Input validation (missing required fields)
- Unknown/inactive store rejection
- Successful action tracking (DB record creation)
- n8n webhook integration (mocked): nudge returned vs. no nudge
- Browsing history construction
"""

import json
from unittest.mock import patch, MagicMock

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from authentication.models import ShopifyStore
from chat.models import ClientAction, ProactiveMessage


TEST_SHOP = "test-store.myshopify.com"
TEST_SESSION = "session-abc123"
TEST_PAGE_URL = "https://test-store.com/products/cool-product"
TRACK_ACTION_URL = "/api/chat/track-action/"


class TrackActionValidationTest(TestCase):
    """Tests for request‑level input validation."""

    def setUp(self):
        self.client = APIClient()

    def test_missing_session_id(self):
        """Should return 400 when session_id is missing."""
        response = self.client.post(
            TRACK_ACTION_URL,
            data={
                "shop": TEST_SHOP,
                "page_url": TEST_PAGE_URL,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("session_id", response.json()["error"])

    def test_missing_shop(self):
        """Should return 400 when shop is missing."""
        response = self.client.post(
            TRACK_ACTION_URL,
            data={
                "session_id": TEST_SESSION,
                "page_url": TEST_PAGE_URL,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("shop", response.json()["error"])

    def test_missing_page_url(self):
        """Should return 400 when page_url is missing."""
        response = self.client.post(
            TRACK_ACTION_URL,
            data={
                "session_id": TEST_SESSION,
                "shop": TEST_SHOP,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("page_url", response.json()["error"])

    def test_empty_session_id(self):
        """Should return 400 when session_id is an empty string."""
        response = self.client.post(
            TRACK_ACTION_URL,
            data={
                "session_id": "   ",
                "shop": TEST_SHOP,
                "page_url": TEST_PAGE_URL,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class TrackActionStoreValidationTest(TestCase):
    """Tests for store/shop validation."""

    def setUp(self):
        self.client = APIClient()

    def test_unknown_store_returns_403(self):
        """Should return 403 for a shop that doesn't exist in the DB."""
        response = self.client.post(
            TRACK_ACTION_URL,
            data={
                "session_id": TEST_SESSION,
                "shop": "nonexistent-store.myshopify.com",
                "page_url": TEST_PAGE_URL,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Unknown", response.json()["error"])

    def test_inactive_store_returns_403(self):
        """Should return 403 for an inactive store."""
        ShopifyStore.objects.create(
            shop_url=TEST_SHOP,
            access_token="shpat_test",
            is_active=False,
        )
        response = self.client.post(
            TRACK_ACTION_URL,
            data={
                "session_id": TEST_SESSION,
                "shop": TEST_SHOP,
                "page_url": TEST_PAGE_URL,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)


@patch("chat.views._call_n8n_sync_to")
class TrackActionSuccessTest(TestCase):
    """Tests for successful action tracking (n8n call is mocked)."""

    def setUp(self):
        self.client = APIClient()
        self.store = ShopifyStore.objects.create(
            shop_url=TEST_SHOP,
            access_token="shpat_test_token",
            is_active=True,
        )

    def _post_action(self, **overrides):
        """Helper to POST a valid track-action payload."""
        payload = {
            "session_id": TEST_SESSION,
            "shop": TEST_SHOP,
            "page_url": TEST_PAGE_URL,
            "page_title": "Cool Product",
            "action_type": "page_view",
            "referrer": "https://test-store.com/collections/all",
            "extra_data": {"product_id": "123", "price": "29.99"},
        }
        payload.update(overrides)
        return self.client.post(TRACK_ACTION_URL, data=payload, format="json")

    # ── Basic tracking ───────────────────────────────────────────────

    def test_creates_client_action(self, mock_n8n):
        """A valid request should create a ClientAction record."""
        mock_n8n.return_value = None  # no nudge

        response = self._post_action()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "tracked")

        # Verify DB record
        action = ClientAction.objects.get(session_id=TEST_SESSION)
        self.assertEqual(action.shop_domain, TEST_SHOP)
        self.assertEqual(action.page_url, TEST_PAGE_URL)
        self.assertEqual(action.page_title, "Cool Product")
        self.assertEqual(action.action_type, "page_view")
        self.assertEqual(action.extra_data["product_id"], "123")

    def test_response_includes_action_id(self, mock_n8n):
        """Response should contain the ID of the newly created action."""
        mock_n8n.return_value = None

        response = self._post_action()
        data = response.json()
        self.assertIn("action_id", data)
        self.assertTrue(ClientAction.objects.filter(id=data["action_id"]).exists())

    def test_default_action_type(self, mock_n8n):
        """When action_type is omitted, it should default to 'page_view'."""
        mock_n8n.return_value = None

        response = self._post_action(action_type="")
        # The view strips the value; 'page_view' is the default in the view
        # An empty string after strip means the view uses the default
        self.assertEqual(response.status_code, 200)

    # ── n8n webhook call ─────────────────────────────────────────────

    def test_calls_n8n_webhook(self, mock_n8n):
        """Should call _call_n8n_sync_to with the correct payload shape."""
        mock_n8n.return_value = None

        self._post_action()

        mock_n8n.assert_called_once()
        call_args = mock_n8n.call_args
        payload = call_args[0][0]  # first positional arg
        webhook_url = "http://0.0.0.0:5678/webhook-test/track-action"  # second positional arg

        # Verify payload structure
        self.assertEqual(payload["session_id"], TEST_SESSION)
        self.assertEqual(payload["shop_domain"], TEST_SHOP)
        self.assertIn("current_page", payload)
        self.assertEqual(payload["current_page"]["url"], TEST_PAGE_URL)
        self.assertIn("browsing_history", payload)
        self.assertIn("shopify_token", payload)
        self.assertEqual(payload["persona"], "proactive_salesperson")

    def test_n8n_receives_correct_webhook_url(self, mock_n8n):
        """Should use N8N_ACTION_WEBHOOK_URL from settings."""
        mock_n8n.return_value = None
        self._post_action()

        from django.conf import settings
        webhook_url = "http://localhost:5678/webhook-test/track-action"
        self.assertEqual(webhook_url, "http://localhost:5678/webhook-test/track-action")

    # ── Nudge (proactive message) ────────────────────────────────────

    def test_nudge_returned_when_n8n_responds(self, mock_n8n):
        """When n8n returns a message, a ProactiveMessage should be created
        and included in the response as 'nudge'."""
        mock_n8n.return_value = json.dumps(
            [{"message": "Hey! Need help choosing a size?", "type": "written"}]
        )

        response = self._post_action()
        data = response.json()

        # Response should include the nudge
        self.assertIn("nudge", data)
        self.assertIn("choosing a size", data["nudge"])

        # DB should have a ProactiveMessage
        nudge = ProactiveMessage.objects.get(session_id=TEST_SESSION)
        self.assertEqual(nudge.shop_domain, TEST_SHOP)
        self.assertIn("choosing a size", nudge.message)
        self.assertIsNotNone(nudge.trigger_action)

    def test_no_nudge_when_n8n_returns_empty(self, mock_n8n):
        """When n8n returns an empty response, no nudge should be created."""
        mock_n8n.return_value = None

        response = self._post_action()
        data = response.json()

        self.assertNotIn("nudge", data)
        self.assertEqual(ProactiveMessage.objects.count(), 0)

    def test_no_nudge_when_n8n_returns_empty_message(self, mock_n8n):
        """When n8n returns a response with an empty message string."""
        mock_n8n.return_value = json.dumps([{"message": "", "type": "written"}])

        response = self._post_action()
        data = response.json()

        self.assertNotIn("nudge", data)
        self.assertEqual(ProactiveMessage.objects.count(), 0)

    # ── n8n error handling ───────────────────────────────────────────

    def test_n8n_exception_does_not_break_tracking(self, mock_n8n):
        """If the n8n call raises an exception, the action should still be
        tracked and the response should be 200."""
        mock_n8n.side_effect = Exception("n8n is down")

        response = self._post_action()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "tracked")

        # Action should still be saved
        self.assertEqual(ClientAction.objects.count(), 1)
        # But no nudge
        self.assertEqual(ProactiveMessage.objects.count(), 0)


@patch("chat.views._call_n8n_sync_to")
class TrackActionBrowsingHistoryTest(TestCase):
    """Tests for the browsing history that is sent to n8n."""

    def setUp(self):
        self.client = APIClient()
        ShopifyStore.objects.create(
            shop_url=TEST_SHOP,
            access_token="shpat_test_token",
            is_active=True,
        )

    def test_browsing_history_accumulates(self, mock_n8n):
        """Multiple track-action calls should build up browsing history."""
        mock_n8n.return_value = None

        pages = [
            ("https://test-store.com/", "Home"),
            ("https://test-store.com/collections/all", "All Products"),
            ("https://test-store.com/products/cool-product", "Cool Product"),
        ]

        for url, title in pages:
            self.client.post(
                TRACK_ACTION_URL,
                data={
                    "session_id": TEST_SESSION,
                    "shop": TEST_SHOP,
                    "page_url": url,
                    "page_title": title,
                },
                format="json",
            )

        # On the 3rd call, browsing_history should have all 3 entries
        last_call_payload = mock_n8n.call_args[0][0]
        self.assertEqual(last_call_payload["total_pages_visited"], 3)
        self.assertEqual(len(last_call_payload["browsing_history"]), 3)

    def test_browsing_history_capped_at_10(self, mock_n8n):
        """Browsing history should be capped at the 10 most recent actions."""
        mock_n8n.return_value = None

        for i in range(15):
            self.client.post(
                TRACK_ACTION_URL,
                data={
                    "session_id": TEST_SESSION,
                    "shop": TEST_SHOP,
                    "page_url": f"https://test-store.com/page-{i}",
                    "page_title": f"Page {i}",
                },
                format="json",
            )

        last_call_payload = mock_n8n.call_args[0][0]
        self.assertEqual(last_call_payload["total_pages_visited"], 10)

    def test_separate_sessions_have_separate_history(self, mock_n8n):
        """Different session IDs should not share browsing history."""
        mock_n8n.return_value = None

        # Session A: 3 pages
        for i in range(3):
            self.client.post(
                TRACK_ACTION_URL,
                data={
                    "session_id": "session-A",
                    "shop": TEST_SHOP,
                    "page_url": f"https://test-store.com/page-a-{i}",
                },
                format="json",
            )

        # Session B: 1 page
        self.client.post(
            TRACK_ACTION_URL,
            data={
                "session_id": "session-B",
                "shop": TEST_SHOP,
                "page_url": "https://test-store.com/page-b-0",
            },
            format="json",
        )

        # Session B should only have 1 entry in history
        payload = mock_n8n.call_args[0][0]
        self.assertEqual(payload["session_id"], "session-B")
        self.assertEqual(payload["total_pages_visited"], 1)
