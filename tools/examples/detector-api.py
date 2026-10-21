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


def bind(result, playback_path, stream_index=-1, same_video=False):
    """Freeze trusted final data; cache enablement is intentionally irrelevant."""
    capabilities = rpc('GetCapabilities')
    if capabilities.get('version') != 1 or capabilities.get('resultbinding', 0) < 1:
        return {'status': 'unsupported'}
    if result.get('status') != 'completed' or not result.get('requestid'):
        return {'status': 'unusable_result'}
    return rpc('Bind', {'requestid': result['requestid'], 'path': playback_path,
                       'options': {'streamindex': stream_index,
                                   'association': 'same_video' if same_video else 'source'}})


def play_detected(source_path, playback_path=None, stream_index=-1,
                  target_stream_index=-1, same_video=False, on_progress=None):
    """Run on a script/worker. Also handles a completed geometry-only no-scan job.

    No global settings are changed. With remember-results OFF this scans and
    binds transient data; with it ON a validated cache hit follows the same path.
    For remux URLs, declare same_video=True and the absolute target stream index.
    """
    import xbmcgui
    target = playback_path or source_path
    result = detect(source_path, stream_index=stream_index, on_progress=on_progress)
    binding = bind(result, target, target_stream_index, same_video)
    if binding.get('status') != 'ready':
        return binding
    item = xbmcgui.ListItem(path=target)
    item.setProperty(binding['property'], binding['bindingid'])
    try:
        xbmc.Player().play(target, item)
    except BaseException:
        rpc('ReleaseBinding', {'bindingid': binding['bindingid']})
        raise
    # Submission is not proof of application. Keep the handle and poll it.
    return {'status': 'accepted', 'bindingid': binding['bindingid'], 'result': result}


def apply_detected(result, playback_path, stream_index=-1, same_video=False):
    """Explicitly target the current ready playback; never apply on completion alone."""
    playback = rpc('GetPlayback')
    if playback.get('status') != 'ready':
        return playback
    binding = bind(result, playback_path, stream_index, same_video)
    if binding.get('status') != 'ready':
        return binding
    reply = rpc('Apply', {'bindingid': binding['bindingid'],
                          'playbackid': playback['playbackid'],
                          'selectionid': playback['selectionid']})
    if reply.get('status') not in ('accepted', 'applied'):
        rpc('ReleaseBinding', {'bindingid': binding['bindingid']})
    return reply


def application_status(binding_id):
    """Read applied/stale/incompatible/failed and available consumer effects."""
    return rpc('GetBinding', {'bindingid': binding_id})
