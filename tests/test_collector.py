import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from router_collector.core import create_app, export_records
from router_collector.storage import StorageBudget


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.received = []
        async def upstream(request):
            self.received.append((request.headers, await request.read(), request.raw_path))
            if request.path == '/broken':
                response = web.StreamResponse(headers={'Content-Length': '100'})
                await response.prepare(request)
                await response.write(b'partial')
                request.transport.close()
                return response
            if request.path == '/redirect':
                return web.Response(status=307, headers={'Location': 'https://example.invalid'})
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Set-Cookie': 'secret', 'x-request-id': 'abc'})
            await response.prepare(request)
            await response.write(b'data: first\n\n')
            await response.write(b'data: last\n\n')
            await response.write_eof()
            return response
        app = web.Application(handler_args={'auto_decompress': False})
        app.router.add_route('*', '/{path:.*}', upstream)
        self.remote = TestServer(app)
        await self.remote.start_server()
        self.client = TestClient(TestServer(create_app('anthropic', self.root, 'run1', _upstream=str(self.remote.make_url('')).rstrip('/'))))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.remote.close()
        self.temp.cleanup()

    async def test_stream_and_auth_not_logged(self):
        payload = b'{"messages":[{"content":"hi"}]}'
        response = await self.client.post('/v1/messages?key=secretquery', data=payload,
                                          headers={'Authorization': 'Bearer secrettoken', 'x-api-key': 'secretkey'})
        result = await response.read()
        self.assertEqual(result, b'data: first\n\ndata: last\n\n')
        headers, body, path = self.received[0]
        self.assertEqual(headers['Authorization'], 'Bearer secrettoken')
        self.assertEqual(body, payload)
        record = next((self.root / 'records').iterdir())
        self.assertEqual(gzip.decompress((record / 'request.bin.gz').read_bytes()), payload)
        meta_text = (record / 'metadata.json').read_text()
        self.assertNotIn('secret', meta_text)
        meta = json.loads(meta_text)
        self.assertTrue(meta['bodies']['response']['complete'])
        self.assertEqual(meta['bodies']['response']['sha256'], hashlib.sha256(result).hexdigest())
        self.assertEqual(meta['run_id'], 'run1')

    async def test_limit_pauses_recording_but_forwards(self):
        await self.client.close()
        budget = StorageBudget(self.root, max_bytes=1, min_free_bytes=0)
        self.client = TestClient(TestServer(create_app('anthropic', self.root, 'run1',
            _upstream=str(self.remote.make_url('')).rstrip('/'), storage_budget=budget)))
        await self.client.start_server()
        for _ in range(2):
            response = await self.client.post('/v1/messages', data=b'payload')
            self.assertEqual(await response.read(), b'data: first\n\ndata: last\n\n')
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.received), 2)
        self.assertFalse((self.root / 'records').exists())
        self.assertEqual(list((self.root / 'active').iterdir()), [])
        health = await self.client.get('/health')
        self.assertEqual((await health.json())['storage']['recording'], 'paused')

    async def test_write_and_cleanup_failure_keeps_forwarding(self):
        with patch('router_collector.storage.CompressedWriter.write', side_effect=OSError('disk')), \
             patch('pathlib.Path.unlink', side_effect=OSError('cleanup')):
            response = await self.client.post('/v1/messages', data=b'payload')
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), b'data: first\n\ndata: last\n\n')
        self.assertEqual(self.received[0][1], b'payload')

    async def test_export_mixed_legacy_and_compressed_records(self):
        response = await self.client.post('/v1/messages', data=b'new')
        await response.read()
        legacy = self.root / 'records' / 'legacy'
        legacy.mkdir()
        (legacy / 'metadata.json').write_text('{}')
        (legacy / 'request.bin').write_bytes(b'old')
        (legacy / 'response.bin').write_bytes(b'old response')
        export_records(self.root, self.root / 'mixed.zip')
        with zipfile.ZipFile(self.root / 'mixed.zip') as archive:
            self.assertEqual(archive.read('records/legacy/request.bin'), b'old')
            new_name = next(n for n in archive.namelist() if n.endswith('request.bin.gz'))
            self.assertEqual(gzip.decompress(archive.read(new_name)), b'new')

    async def test_compressed_body_unchanged(self):
        payload = gzip.compress(b'hello world')
        response = await self.client.post('/v1/messages', data=payload, headers={'Content-Encoding': 'gzip'})
        await response.read()
        self.assertEqual(self.received[0][1], payload)

    async def test_redirect_not_followed(self):
        response = await self.client.post('/redirect', data=b'x', allow_redirects=False)
        await response.read()
        self.assertEqual(response.status, 307)
        self.assertEqual(len(self.received), 1)

    async def test_broken_upstream_keeps_partial_record(self):
        import aiohttp
        response = await self.client.post('/broken', data=b'x')
        with self.assertRaises(aiohttp.ClientError):
            await response.read()
        record = next((self.root / 'records').iterdir())
        meta = json.loads((record / 'metadata.json').read_text())
        self.assertFalse(meta['bodies']['response']['complete'])
        self.assertIn('error', meta)

    async def test_health_unlogged_and_export(self):
        response = await self.client.get('/health')
        self.assertEqual((await response.json())['run_id'], 'run1')
        self.assertFalse((self.root / 'records').exists())
        response = await self.client.post('/v1/messages', data=b'body')
        await response.read()
        (self.root / 'active').mkdir(exist_ok=True)
        (self.root / 'active' / 'unfinished').write_text('excluded')
        (self.root / 'credentials.json').write_text('excluded')
        (self.root / 'events').mkdir()
        (self.root / 'events' / 'event.json').write_text('{}')
        dest = self.root / 'export.zip'
        self.assertEqual(export_records(self.root, dest), 4)
        with zipfile.ZipFile(dest) as z:
            manifest = json.loads(z.read('manifest.json'))
            for info in z.infolist():
                if info.filename.endswith(".gz"):
                    self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
            self.assertFalse(any(n.startswith('active/') for n in z.namelist()))
            for name, digest in manifest['files'].items():
                self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(), digest)
        with self.assertRaises(ValueError):
            export_records(self.root, dest)

if __name__ == '__main__':
    unittest.main()
