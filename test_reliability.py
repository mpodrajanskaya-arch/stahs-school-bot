import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import portal
import main


def response(status, **kwargs):
    return httpx.Response(status, request=httpx.Request('GET', 'https://school.example/api/message'), **kwargs)


class PortalTests(unittest.TestCase):
    def test_transient_gateway_failure_recovers(self):
        client = Mock()
        client.get.side_effect = [response(502), response(503), response(200, json={'data': []})]
        with patch.object(portal.time, 'sleep') as sleep:
            self.assertEqual(portal._get_with_retry(client, '/api/message').status_code, 200)
        self.assertEqual(sleep.call_count, 2)

    def test_persistent_failure_is_bounded(self):
        client = Mock()
        client.get.return_value = response(502)
        with patch.object(portal.time, 'sleep'), patch.object(portal, '_get_client', return_value=client):
            with self.assertRaises(httpx.HTTPStatusError):
                portal._request('/api/message')
        self.assertEqual(client.get.call_count, 3)

    def test_network_timeout_recovers(self):
        client = Mock()
        client.get.side_effect = [httpx.ReadTimeout('timeout'), response(200)]
        with patch.object(portal.time, 'sleep'):
            self.assertEqual(portal._get_with_retry(client, '/api/message').status_code, 200)

    def test_expired_session_refreshes(self):
        client = Mock()
        client.get.side_effect = [response(200, headers={'content-type': 'text/html'}, text='Login'), response(200, json={'data': []})]
        with patch.object(portal, '_get_client', return_value=client), patch.object(portal, 'invalidate_session') as invalidate:
            self.assertEqual(portal._request('/api/message').json(), {'data': []})
            invalidate.assert_called_once()

    def test_invalid_session_is_discarded(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'session.json'
            path.write_text('{}')
            client = Mock()
            with patch.object(portal, 'SESSION_FILE', path), patch.object(portal, '_cached_client', client):
                portal.invalidate_session()
                self.assertFalse(path.exists())
                self.assertIsNone(portal._cached_client)
                client.close.assert_called_once()


class DigestTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_send_keeps_last_seen_for_retry(self):
        app = Mock()
        app.bot.send_message = AsyncMock()
        with patch.object(main.portal, 'get_new_communications', return_value=[{'id': 'new'}]), patch.object(main, '_load_last_seen', return_value='old'), patch.object(main, '_save_last_seen') as save, patch.object(main, '_send_messages_and_events', new=AsyncMock(side_effect=RuntimeError('test delivery failure'))):
            await main._send_digest(app)
            save.assert_not_called()

    async def test_success_acknowledges_after_send(self):
        app = Mock()
        order = []
        async def deliver(*args):
            order.append('sent')
        with patch.object(main.portal, 'get_new_communications', return_value=[{'id': 'new'}]), patch.object(main, '_load_last_seen', return_value='old'), patch.object(main, '_save_last_seen', side_effect=lambda x: order.append(x)), patch.object(main, '_send_messages_and_events', side_effect=deliver):
            await main._send_digest(app)
        self.assertEqual(order, ['sent', 'new'])

    async def test_manual_and_scheduled_digests_do_not_overlap(self):
        active = 0
        maximum = 0
        async def digest(*args):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
        with patch.object(main, '_digest_lock', asyncio.Lock()), patch.object(main, '_send_digest', side_effect=digest):
            await asyncio.gather(main.send_digest(Mock()), main.send_digest(Mock()))
        self.assertEqual(maximum, 1)

if __name__ == '__main__':
    unittest.main()
