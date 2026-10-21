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
        self.play_calls = []
        def execute(raw):
            request = json.loads(raw)
            self.calls.append(request)
            method = request['method'].split('.')[-1]
            value = self.replies[method].pop(0)
            return value if isinstance(value, str) else json.dumps({'result': value})
        fake = SimpleNamespace(executeJSONRPC=execute, Monitor=lambda: SimpleNamespace(
            waitForAbort=lambda timeout: self.aborted),
            Player=lambda: SimpleNamespace(play=lambda path,item: self.play_calls.append((path,item))))
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

    def test_complete_geometry_no_scan_flows_to_its_item_without_settings(self):
        result = {'status': 'completed', 'requestid': 'job', 'cachehit': False,
                  'provenance': 'pre_cropped', 'sampled': False,
                  'geometry': {'valid': True, 'width': 3832, 'height': 1600},
                  'strict': {'valid': False}, 'subtitle': {'valid': False}}
        self.replies['GetCapabilities'] += [{'version': 1, 'resultbinding': 1}]
        self.replies['GetStatus'] = [result]
        self.replies['Bind'] = [{'status': 'ready', 'bindingid': 'bound',
                                'property': 'p3i.detector.binding'}]
        class Item:
            def __init__(self, path):
                self.path, self.properties = path, {}
            def setProperty(self, key, value):
                self.properties[key] = value
        with patch.dict(sys.modules, xbmcgui=SimpleNamespace(ListItem=Item)):
            reply = self.client['play_detected']('/cropped.mkv')
        self.assertEqual(reply['status'], 'accepted')
        self.assertEqual(reply['result'], result)
        path, item = self.play_calls[0]
        self.assertEqual(path, '/cropped.mkv')
        self.assertEqual(item.properties, {'p3i.detector.binding': 'bound'})
        self.assertEqual(self.methods(), ['GetCapabilities', 'Start', 'GetStatus', 'GetCapabilities', 'Bind'])
        self.assertFalse(any('Settings' in m for m in self.methods()))

    def test_remux_binding_uses_explicit_association_and_target_stream(self):
        self.replies['GetCapabilities'] = [{'version': 1, 'resultbinding': 1}]
        self.replies['Bind'] = [{'status': 'ready', 'bindingid': 'bound'}]
        result = {'status': 'completed', 'requestid': 'source-job'}
        self.assertEqual(self.client['bind'](result, 'https://remux.invalid/play', 2, True)['status'], 'ready')
        self.assertEqual(self.calls[-1]['params'], {'requestid': 'source-job',
            'path': 'https://remux.invalid/play', 'options': {'streamindex': 2, 'association': 'same_video'}})

    def test_active_application_fences_both_ids_and_releases_rejected_binding(self):
        self.replies['GetPlayback'] = [{'status': 'ready', 'playbackid': 'session', 'selectionid': 'stream'}]
        self.replies['GetCapabilities'] = [{'version': 1, 'resultbinding': 1}]
        self.replies['Bind'] = [{'status': 'ready', 'bindingid': 'bound'}]
        self.replies['Apply'] = [{'status': 'stale'}]
        self.replies['ReleaseBinding'] = [{'status': 'released'}]
        reply = self.client['apply_detected']({'status': 'completed', 'requestid': 'job'}, '/video.mkv')
        self.assertEqual(reply['status'], 'stale')
        self.assertEqual(self.calls[-2]['params'], {'bindingid': 'bound', 'playbackid': 'session', 'selectionid': 'stream'})
        self.assertEqual(self.calls[-1]['params'], {'bindingid': 'bound'})
        self.assertEqual(self.play_calls, [])

    def test_active_application_waits_for_ready_without_binding_or_playing(self):
        self.replies['GetPlayback'] = [{'status': 'opening'}]
        self.assertEqual(self.client['apply_detected']({'status': 'completed'}, '/video.mkv')['status'], 'opening')
        self.assertEqual(self.methods(), ['GetPlayback'])
        self.assertEqual(self.play_calls, [])

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
