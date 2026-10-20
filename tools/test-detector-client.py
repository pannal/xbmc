#!/usr/bin/env python3
"""Exercise the public example client with recording JSON-RPC boundaries."""
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class DetectorClientTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.replies = {}
        self.aborted = False
        def execute(raw):
            request = json.loads(raw)
            self.calls.append(request)
            method = request['method'].split('.')[-1]
            value = self.replies[method].pop(0)
            return value if isinstance(value, str) else json.dumps({'result': value})
        fake = SimpleNamespace(executeJSONRPC=execute, Monitor=lambda: SimpleNamespace(
            waitForAbort=lambda timeout: self.aborted))
        with patch.dict(sys.modules, xbmc=fake):
            self.client = runpy.run_path(str(ROOT/'tools/examples/detector-api.py'))
        self.replies = {'GetCapabilities': [{'version': 1, 'supported': True}],
                        'Start': [{'status': 'accepted', 'requestid': 'job'}]}

    def methods(self):
        return [r['method'].split('.')[-1] for r in self.calls]

    def test_cached_coordinates_return_unchanged_without_wait(self):
        cached = {'status': 'completed', 'cachehit': True,
                  'progress': {'completed': 0, 'total': 14},
                  'strict': {'valid': True, 'offsets': {'top': 0}},
                  'subtitle': {'valid': False}}
        self.replies['GetStatus'] = [cached]
        path = 'https://fake.invalid/quo"te,ü.mkv?token=fake|User-Agent=example'
        result = self.client['detect'](path, stream_index=3, allow_http=True,
                                       monitor=SimpleNamespace(waitForAbort=lambda n: self.fail('cache hit waited')))
        self.assertEqual(result, cached)
        self.assertEqual(self.calls[1]['params'], {'path': path, 'options': {'streamindex': 3, 'allowhttp': True}})
        self.assertEqual(self.methods(), ['GetCapabilities', 'Start', 'GetStatus'])

    def test_progress_then_timeout_is_terminal(self):
        self.replies['GetStatus'] = [{'status': 'running', 'progress': {'completed': 3, 'total': 14}},
                                     {'status': 'timed_out'}]
        progress = []
        self.assertEqual(self.client['detect']('video', on_progress=progress.append)['status'], 'timed_out')
        self.assertEqual(progress, [{'completed': 3, 'total': 14}])
        self.assertNotIn('Cancel', self.methods())

    def test_abort_requests_cancellation_without_waiting_for_blocked_backend(self):
        self.replies['GetStatus'] = [{'status': 'running'}]
        self.replies['Cancel'] = [{'status': 'cancelling'}]
        self.aborted = True
        self.assertEqual(self.client['detect']('video')['status'], 'cancelling')
        self.assertEqual(self.calls[-1]['params'], {'requestid': 'job'})

    def test_callback_exception_cancels_its_exact_job(self):
        self.replies['GetStatus'] = [{'status': 'running'}]
        self.replies['Cancel'] = [{'status': 'cancelling'}]
        def fail(progress):
            raise ValueError('consumer')
        with self.assertRaisesRegex(ValueError, 'consumer'):
            self.client['detect']('video', on_progress=fail)
        self.assertEqual(self.calls[-1]['params'], {'requestid': 'job'})
        self.assertEqual(self.methods().count('Cancel'), 1)

    def test_busy_or_http_excluded_never_poll(self):
        self.replies['Start'] = [{'status': 'busy'}]
        self.assertEqual(self.client['detect']('video')['status'], 'busy')
        self.assertNotIn('GetStatus', self.methods())

    def test_stock_malformed_unknown_version_and_unsupported(self):
        for capability in ('invalid JSON', {}, {'version': 2, 'supported': True},
                           {'version': 1, 'supported': False}):
            with self.subTest(capability=capability):
                self.calls[:] = []
                self.replies['GetCapabilities'] = [capability]
                self.assertEqual(self.client['detect']('video')['status'], 'unsupported')
                self.assertEqual(self.methods(), ['GetCapabilities'])


if __name__ == '__main__':
    unittest.main()
