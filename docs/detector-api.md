# Detector controls and addon API, version 1

These optional p3i extensions use the existing conservative FFmpeg detector on
Amlogic builds. They do not change its uncertainty or subtitle-placement rules.
Check capabilities; stock Kodi can return a JSON-RPC method-not-found error.

## Skip automatic detection for one video request

Any addon can publish the Home-window property immediately before submitting a
video request:

```python
import xbmc
import xbmcgui

xbmcgui.Window(10000).setProperty('p3i.detector.skip_next', '1')
xbmc.Player().play(path, video_list_item)
```

Only the exact string `"1"` arms the signal. `Window(Home).Property(p3i.detector.skip_next)`
reads it. Kodi consumes it into the submitted request before asynchronous opening,
so clearing or publishing another flag cannot change an already queued decision.
A captured false decision also protects a queued request from later publications.
The veto survives seeks, resets, decoder reopen and selected-stream changes in
that playback. The next unflagged request detects normally.

A veto bypasses automatic scanner/cache-identity work, detector pause and progress
for both subtitle and native DV L5 detection. It does not disable manual overrides,
authored metadata or geometry-generated padding. Explicit API jobs do not consume
this signal.

Known audio, picture and game requests leave the signal alone. Untyped media URLs
and plugin requests are video candidates: their submission consumes the signal
even if opening subsequently fails or resolves to another media type. Playlist
containers that are not yet typed defer consumption until a concrete candidate
is selected. Typed video-playlist requests capture at submission and apply the
snapshot to their first selected item, including after shuffle; it is not retained
on reusable playlist entries. A failed selected item does not transfer its veto
to the next item.

The property is global, not scoped to an addon. Another caller submitting between
publication and your request can consume it. There is no atomic window-property
write plus player submission. Do preparation first, then publish immediately
before calling play. Once submitted, do not clear the global property from a late
playback callback.

For abandoned submissions, optional ownership helpers avoid clearing a newer
publication:

- `P3I.Detector.SetSkipNext` returns `{"version":1,"status":"armed","token":"..."}`.
- `P3I.Detector.ClearSkipNext` with `{"token":"..."}` returns `cleared` only for
  that still-current, unconsumed publication; otherwise `unchanged`.

A successful Python `Player.play(path, listitem)` captures before returning. Keep
its token only until submission succeeds; on an exception use the owned clear.
These helpers use the same generic Home property. They do not grant caller
exclusivity. PM4K uses them with its final `metadata.isTranscoded` decision,
including Direct Streaming with copied video and transcoded audio. Direct Play,
including HTTP or mapped paths, does not receive that veto.

## Shared controls

Under Subtitles, independently enabled subtitle and native DV L5 detection share:

| Setting | Default | Automatic playback | Explicit jobs |
| --- | --- | --- | --- |
| Remember results | On | Validate identity/stream and reuse; save successes | Same validated cache; a hit requires no sampling |
| Use A53 cores | Off | Use the available separate A53 CPUs or skip | Same CPU policy |
| Pause for detection | On | Wait for startup seeks/current video synchronization, then hold | Never holds or changes playback |
| Skip HTTP(S) | Off | Resolved HTTP(S), including supported wrappers, bypasses detection | Respected unless `allowhttp:true` |
| Time per position | 5 seconds, 1–60 | Timeout stops this playback's automatic scan, including resets | Timeout terminates this job |

HTTP classification uses the resolved input, not Plex identity or the original
library URL. Local files and mapped NFS/SMB paths remain eligible. Admission and
timeout decisions are frozen for each new playback/job. Cache enablement is read
at admission; disabling it before completion also prevents saving the new result.
CPU isolation is checked when a worker starts.

The position budget starts after deliberate admission/pre-position buffer waits
and spans seeking, reads, decoding and all retries for that position. Expiry stops
the whole scan without partial publication or caching. It is not a total-run or
initial-open/probe deadline. HTTP Curl reads poll cancellation/deadline on their
own thread (normally at most 50 ms waits; its no-descriptor fallback uses 100 ms).
Synchronous VFS/decoder calls can return after the deadline; checks after return
then prevent later work. No worker is detached or forcibly killed. Cancellation
and shutdown can therefore remain pending while a backend call is blocked.

## Asynchronous JSON-RPC jobs and usable results

Use structured JSON arguments through `xbmc.executeJSONRPC` or another JSON-RPC
transport. Do not interpolate a path into a command or log the request/URL.
The [example client](../tools/examples/detector-api.py) polls and cancels a job.

`P3I.Detector.GetCapabilities` returns:

```json
{"version":1,"supported":true,"skipproperty":"p3i.detector.skip_next","ownedskip":true,"maxjobs":16,"maxscanners":1,"cache":"shared_validated","geometryresults":true,"resultbinding":1,"bindingproperty":"p3i.detector.binding","maxbindings":64}
```

The methods remain registered on builds without the scanner: capabilities report
`supported:false`, and operations return `status:"unsupported"`.

Start with:

```json
{"jsonrpc":"2.0","id":1,"method":"P3I.Detector.Start","params":{"path":"/media/movie.mkv","options":{"streamindex":-1,"allowhttp":false}}}
```

`streamindex` is the absolute container stream index, not the Nth video stream;
`-1` selects only an unambiguous non-attached video stream. Cover images are
ignored. Explicit selection supports multi-video inputs. Unknown option names
are rejected. Paths containing commas, quotes, Unicode and Kodi URL options stay
structured and round-trip unchanged. In-process addons retain their existing VFS
access. Other transports require a source allowed by Kodi's remote-sharing policy.

Start returns `accepted` with an opaque, process-local `requestid`, or a typed
`busy`, `http_excluded`, `path_not_shared`, `invalid_input`, `shutdown`, `failed` or
`unsupported` status. Accepted means ownership of an asynchronous job, not success.
Media probing and cache validation run on the worker, not the RPC/GUI thread.

Call `P3I.Detector.GetStatus` with `{"requestid":"..."}`. States are `queued`,
`running`, `completed`, `skipped`, `timed_out`, `cancelling`, `cancelled`, `failed`,
`busy` or `not_found`. Progress has `completed` and `total` (currently 14 requested
positions, not retry count). A validated cache hit has `cachehit:true` and zero
sample positions. The cache setting controls both reuse and saving; disabled
cache means neither lookup nor identity I/O. Unavailable identity is a cache miss.
HTTP validation still needs a usable modification timestamp or strong ETag.
An unspecified stream can reuse a record only when its original probe established
one non-attached video stream; selected-stream jobs require an exact match.
Cache format/detector changes invalidate and clean obsolete records automatically.
Format 5 also retains display aspect and geometry-only provenance for playback binding.

Completed replies also include `provenance` (`scanned`, `cached` or `pre_cropped`),
`sampled`, and `displayaspect`. A completed sampled result can look like:

```json
{"version":1,"requestid":"1","status":"completed","cachehit":false,"progress":{"completed":14,"total":14},"outcome":3,"streamindex":0,"strict":{"valid":true,"units":"source_pixels","width":1920,"height":1080,"confidence":"invariant","offsets":{"top":132,"bottom":132,"left":0,"right":0},"rectangle":{"x1":0,"y1":132,"x2":1920,"y2":948,"bounds":"exclusive_end"}},"subtitle":{"valid":false}}
```

Coordinates refer to this input's coded source dimensions, not display/GUI pixels
or padded output. Origin is top left; rectangle end bounds are exclusive.
`valid:true` with all zero offsets is a valid full-frame observation. `valid:false`
is no result. Terminal cancellation/timeout/busy replies have no usable geometry.

`strict` is conservative file-wide geometry. Its `invariant` confidence denotes
the detector's high-confidence invariant result, not a measured probability.
`conservative` has not met that stronger contract. `subtitle` has `placement_only`
confidence and describes the separate subtitle envelope; it must not become an
invariant native crop. A completed job may contain only subtitle evidence.
`outcome` preserves existing detector states: 0 failed, 1 native geometry
inapplicable, 2 native geometry withheld (possibly subtitle-only), 3 strict result,
4 skipped/inconclusive, 5 inactive, 6 running, 7 timed out. Status is the job's
terminal/lifecycle interpretation; do not treat every nonzero outcome as success.

Explicit jobs derive dimensions and metadata from their own selected input, work
without an active player, and never apply results merely by scanning. Deliberate
binding/application is a separate operation, described below. They
ignore automatic detector enable switches and the one-shot veto, preserve the
existing unsupported-input/conservative detection rules, and share one scanner
reservation with automatic probing. A miss that loses that reservation finishes
with `busy`; it never cancels another owner. Cache hits need no scanner reservation.

Call `P3I.Detector.Cancel` with the ID to request cancellation without joining I/O.
`cancelling` retains ownership until actual worker cleanup; completed jobs retain
their terminal state. One explicit job may be active; at most 16 result records
are retained (not a chronological last-16 guarantee). An evicted/unknown ID is
`not_found`. Old completion or cancellation cannot alter another job or the
current playback source.

Permissions are `ReadData` for capabilities/status, `ReadData` plus `ControlSystem`
for start/cancel, and `ControlPlayback` for owned skip helpers. This version does
not provide persistent jobs. Results reach playback only through deliberate
request binding or an explicitly fenced Apply operation. RPC detector
payloads are omitted from diagnostic logs; explicit request/worker scopes also
silence CLog during backend operations that may print query credentials/headers.
The scope is thread-local; unrelated playback diagnostics remain enabled.


## Geometry-only completion

On containers with complete header metadata, a selected video with explicitly
square pixels and proven non-16:9 coded dimensions can complete before decoder
creation, frame decoding or sample seeks. Unknown/conflicting/anamorphic aspect
is not pre-crop proof. Dynamically discovered stream tables cannot establish
unambiguous default selection or seed unique-stream cache discovery.

A 3832×1600 square-pixel source returns `status:"completed"`,
`provenance:"pre_cropped"`, `sampled:false`, zero completed positions,
`strict.valid:false`, `subtitle.valid:false`, and:

```json
{"geometry":{"valid":true,"kind":"pre_cropped","confidence":"geometry","units":"source_pixels","width":3832,"height":1600,"offsets":{"top":0,"bottom":0,"left":0,"right":0},"rectangle":{"x1":0,"y1":0,"x2":3832,"y2":1600,"bounds":"exclusive_end"}},"padding":{"units":"padding_reference_pixels","width":3832,"height":2155,"top":277,"bottom":277,"left":0,"right":0}}
```

The full coded frame and implied output-padding reference are separate spaces.
The integer reference preserves the existing auto-letterbox calculation (including
its rounding); it is not the actual renderer destination or proof of bars inside
the coded image. Geometry certainty is not sampled invariant L5 confidence.
Ordinary renderer mapping and native padding rules handle the playback conversion,
including sources without authored L5. A cached geometry-only result keeps this
`geometry` meaning but reports `provenance:"cached"`.

Standalone media header opening has a separate ten-second soft deadline. After
that header step, an ordinary scan's stream-info discovery remains outside the
per-position budget. Cache identity/validation is also outside those budgets.
Synchronous backend calls can overrun before returning; cancellation/retirement
keeps ownership until they return. Metadata timeout reports `phase:"metadata"`;
position timeout reports `phase:"positions"`. Neither produces usable coordinates.

## Bind results to an upcoming request

Check `version == 1` and `resultbinding >= 1` in capabilities. Existing version-1
PM4K `ownedskip` negotiation remains unchanged. Successful completed data must be
bound while its job-history entry is still available:

```json
{"jsonrpc":"2.0","id":1,"method":"P3I.Detector.Bind","params":{"requestid":"job-id","path":"/media/movie.mkv","options":{"association":"source","streamindex":-1}}}
```

A `ready` reply supplies `bindingid` and `property:"p3i.detector.binding"`. The
snapshot is copied from trusted completed job data; arbitrary supplied rectangles
or confidence flags are not accepted. Cancelled, timed-out, failed or uncertain
results without usable geometry cannot be bound. Binding requires usable selected
stream/codec/dimensions/aspect. Zero offsets remain a legitimate strict result.
The default `source` association requires the scanned URI and selected stream.

Attach the token to the specific video ListItem immediately before submission:

```python
item.setProperty(binding['property'], binding['bindingid'])
xbmc.Player().play(playback_path, item)
```

Python play and other concrete video submission boundaries consume the token once
and retain an immutable request-owned snapshot before queued opening. Reusing the
ListItem or advancing a playlist does not transfer it to a following request.
Use concrete target inputs rather than an unresolved playlist container. Resolved
playback must match the declared target URI; URL aliases/substitutions should be
resolved before binding. Invalid/already-consumed tokens provide no result.

For an original-file scan followed by a remux/Direct Streaming URL, set that exact
playback URL as `path`, explicitly set `association:"same_video"`, and supply the
absolute target `streamindex`. This is the addon's declaration that the selected
video is the same content; Kodi validates the actual target stream, codec, coded
size and display aspect. URL equality or matching dimensions alone is not an
implicit association. Changed codec, coded size, display aspect or selected stream
is rejected; no automatic rescaling is provided. The declaration is for unchanged coded video,
such as remuxing. Transformations that preserve those descriptors cannot be
inferred from metadata; do not declare transformed or unrelated video to be the
same video.

Persistent caching is independent: with remember-results OFF, detection returns
fresh data and this same binding path uses it without cache lookup/write or a
second scan. A valid bound result takes precedence over automatic detector
admission, including the generic veto and HTTP exclusion. It bypasses automatic
startup hold, progress and scanner I/O. These policies still govern ordinary
unbound automatic detection.

Bound results deliberately do not require automatic detection to be enabled.
Subtitle results reach existing placement getters. Native DV use still requires
supported native DV/L5 processing and preserves manual overrides, authored L5
protection, high-confidence replacement, geometry padding and OSD/subtitle
suppression. Subtitle-only evidence never becomes native strict crop. A geometry-only
binding uses existing coded-frame/padding paths, never fabricated invariant zeros.

## Apply during matching playback

`P3I.Detector.GetPlayback` returns `ready` with `playbackid`, `selectionid`, actual
stream index and coded dimensions, or `opening`/`inactive`. Once a completed job
has a binding for that target, explicitly apply it using both identities:

```json
{"jsonrpc":"2.0","id":1,"method":"P3I.Detector.Apply","params":{"bindingid":"binding-id","playbackid":"playback-id","selectionid":"selection-id"}}
```

The RPC validates/enqueues CPU data. It never reads media, joins blocked scanner
I/O or writes native parameters on the caller thread. The owned application
worker uses existing publication/native admission. An old queued request cannot
stop a newer playback's automatic scan. `accepted` only acknowledges the handoff;
poll `P3I.Detector.GetBinding` with `bindingid` to observe `applied`, `stale`,
`incompatible` or `failed`. `effects.subtitle`, `effects.native_l5` and
`effects.geometry` describe consumer availability after application, not a physical
HDMI measurement. Manual/authored/padding authority can mean that an accepted
strict result does not become the native owner.

Wrong playback/selection identities return `stale` without consuming the binding;
opening/unsupported routes return `not_ready`. Wrong source/stream/codec/size/aspect
returns `incompatible`. A token already consumed returns `already_used`. Missing
handles return `not_found`. Dispatcher launch/publication failure is terminal
`failed`; later bindings can still be applied. No automatic application follows
job completion alone.

## Lifetime and limits

Same-stream seeks/resets and temporary decoder teardown preserve the immutable
snapshot and revalidate it on reconfiguration. During temporary suspension the
binding returns to `accepted` with unavailable effects, then becomes `applied`
after successful reconfiguration. Genuine stream/geometry changes
retire incompatible data. Stop, terminal failure, replacement and next playback
end that session; stale completion cannot revive an ended generation or alter a
newer one. Existing automatic-timeout and user-pause behavior remain independent.

Job history retains at most 16 entries. Binding/status storage has at most 64
entries; terminal history may be evicted. `Bind` returns `busy` when all slots are
still outstanding. Use `P3I.Detector.ReleaseBinding` with `bindingid` to abandon an
unused token or release its public status handle. A captured request/active
playback retains its copied data even when the original job expires, terminal
binding history is evicted, or its handle is released. These are process-local
handles, not persistent settings. Failed/abandoned preparations lose their lease
and report stale; no cleanup callback clears another binding or generic skip flag.

Bindings require `ReadData` and `ControlPlayback`; Apply/release require
`ControlPlayback`; binding/playback status requires `ReadData`. External transports
also require the declared target to pass Kodi's existing sharing policy. The API
never returns source credentials. The example client demonstrates detect→bind→play
and explicit active application, with the shared cache either ON or OFF.
