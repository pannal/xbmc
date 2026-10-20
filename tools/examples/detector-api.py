# SPDX-License-Identifier: GPL-2.0-or-later
"""Copy into an addon; call detect(path) from a script/worker, not a GUI callback."""
import json
import xbmc


def rpc(method, params=None):
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'P3I.Detector.' + method}
    if params is not None:
        request['params'] = params
    try:
        reply = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
        value = reply.get('result')
        return value if isinstance(value, dict) else {'status': 'unsupported'}
    except (TypeError, ValueError):
        return {'status': 'unsupported'}


def detect(path, stream_index=-1, allow_http=False, on_progress=None, monitor=None):
    capabilities = rpc('GetCapabilities')
    if capabilities.get('version') != 1 or not capabilities.get('supported'):
        return {'status': 'unsupported'}
    reply = rpc('Start', {'path': path, 'options': {
        'streamindex': stream_index, 'allowhttp': allow_http}})
    if reply.get('status') != 'accepted':
        return reply
    request_id = reply['requestid']
    monitor = monitor or xbmc.Monitor()
    try:
        while True:
            reply = rpc('GetStatus', {'requestid': request_id})
            if reply.get('status') not in ('queued', 'running', 'cancelling'):
                return reply
            if on_progress:
                on_progress(reply.get('progress', {}))
            if monitor.waitForAbort(0.1):
                return rpc('Cancel', {'requestid': request_id})
    except BaseException:
        rpc('Cancel', {'requestid': request_id})
        raise
