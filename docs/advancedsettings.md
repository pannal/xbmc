# Kodi p3i advancedsettings.xml reference

These 18 settings were introduced or ported in p3i after the CPM/U3k base. They are additions to stock CoreELEC and Kodi 21.3 Omega.

Edit `advancedsettings.xml` in your Kodi profile and restart Kodi. On CoreELEC the main profile uses `/storage/.kodi/userdata/advancedsettings.xml`. A separate profile uses its own file if present and falls back to the main profile's file otherwise.

The example below keeps the Blu-ray ISO page cache enabled and disables its forward prefetch. If your file already has a `<blurayisocache>` section, add the entry there. Use a single `<advancedsettings>` root. Child element names are case-sensitive. The tables give source defaults.

```xml
<advancedsettings>
  <blurayisocache>
    <forwardprefetchpages>0</forwardprefetchpages>
  </blurayisocache>
</advancedsettings>
```

## Video playback

Place these entries under `<video>`.

| Element | Default | Values and effect |
| --- | --- | --- |
| `menudomainqueuetimesize` | `1` s | 0–16 seconds, decimals allowed. Caps audio and video read-ahead during Blu-ray menu segments. Zero or a value at least as large as the normal queue disables the cap. Sustained starvation can also release it. |
| `bdboundarydrain` | `true` | Let the queued end of a Blu-ray playlist play out at a natural transition to the next playlist. |
| `amlindependentpresenter` | `true` | Let an independent worker present frames during eligible monoscopic Amlogic hardware playback. Set to `false` to use the ordinary presentation path. |
| `livehevcborderscale` | automatic (omit) | Use `2` for half-width, half-height border analysis or `4` for quarter-width, quarter-height. This affects the live Amlogic HEVC border detector only. Automatic uses `2` for 3840-wide, fixed-rate video at a known rate up to 30 fps with the finer 4K scan enabled, and `4` otherwise. |
| `dvCustomVsvdb` | empty | Chroma override for the **EPSON LS12000** Dolby Vision VSVDB colour-space preset (value 10). Enter all 12 colon-separated hexadecimal bytes from a Vertex2 payload. Kodi uses the final four for the primaries. An empty or invalid value uses the preset's calibrated primaries. |
| `decoderdraintimeout` | `5` s | 1–60 whole seconds. Wait for buffered pictures while draining the Amlogic decoder. Draining can continue while frames are still arriving. |

## Blu-ray ISO cache

Use `<blurayisocache>` directly under `<advancedsettings>` for the page cache that serves libbluray disc-image reads. Enter byte counts as whole numbers.

| Element | Default | Values and effect |
| --- | --- | --- |
| `enabled` | `true` | Cache disc-image blocks in memory when the image length is known. Set to `false` for direct reads. |
| `pagesize` | `262144` (256 KiB) | 2048–1048576 bytes per page. Kodi rounds down to a multiple of the 2048-byte disc block. |
| `maxbytes` | `67108864` (64 MiB) | 262144–1073741824 bytes. The cache holds whole pages and always allows at least one page. |
| `forwardprefetchpages` | `1` | 0–16 pages to read ahead on sequential access, limited to the page capacity minus one. Zero keeps demand caching and disables prefetch. |

## GUI rendering

Place these entries under `<gui>`. AFBC (Arm Frame Buffer Compression) can apply independently to stored artwork and to the completed GUI frame sent to the display.

| Element | Default | Values and effect |
| --- | --- | --- |
| `afbcfanart` | `true` | Compress eligible opaque, widescreen background fanart on the upload worker. The broader `afbcimages` policy can also admit fanart. |
| `afbcimages` | `true` | Compress eligible artwork including posters, covers and large transparent images. Disable both artwork switches to turn off artwork compression. Both require async upload, a supported Mali driver and the kernel ION cache fix. |
| `afbcscanout` | `true` | Use compressed display buffers for eligible whole-frame GUI redraws on supported Amlogic hardware. Kodi uses linear buffers when the display or rendering path requires them. |
| `highrefreshvideoguifallback` | `true` | With Native 4K GUI enabled on the Amlogic 4.9 path, use a 1080p GUI during Kodi video playback at output modes of at least 3840×2160 above 30 Hz. The video output mode stays at its selected resolution. |
| `limitbackgroundfades` | `true` | When a background or opaque image is already crossfading, keep the latest pending selection and finish the current pair before starting its replacement. |
| `singlepasscrossfade` | `true` | Draw a compatible pair of images in one pass during a crossfade. Set to `false` to draw the fading images separately. |

For artwork eligibility, hardware requirements and fallbacks, see [asynchronous texture upload](ASYNC_TEXTURE_UPLOAD.md) and [AFBC artwork](../tools/afbc-fanart/README.md).

## Script shutdown

Place these elements directly under `<advancedsettings>`.

| Element | Default | Values and effect |
| --- | --- | --- |
| `addonscriptstoptimeout` | `30` s | 1–120 whole seconds for a background add-on script to exit gracefully after cancellation. This also sets Kodi's process shutdown watchdog deadline. |
| `pythonscriptterminatetimeout` | `5000` ms | 1000–30000 milliseconds for the Python invoker to stop after the abort request before Kodi proceeds with forced termination. |
