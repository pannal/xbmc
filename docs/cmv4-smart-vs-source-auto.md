# CMv4.0 Smart versus Auto (source peak)

Smart makes a more adaptive decision: it uses the current RPU's Level 1 peak and
allows configurable headroom above the display peak. Auto (source peak) uses the
stream's source-peak metadata and a display or fixed threshold, so its decision
normally stays consistent throughout a title. Neither is proven to produce a
better picture on every display.

For display-led playback, Smart is a reasonable default when adaptive appending
is wanted. Auto is preferable when a more consistent metadata version is wanted.
For player-led playback, I would favor a more consistent append policy until
dynamic Smart switching is validated. The R10 donor deliberately prevents its
equivalent dynamic mode from switching metadata versions there. See its codec
safeguard below; this is not a demonstrated failure in our current build.

This comparison describes p3i's code at `6a19454cab` and the R10 Kodi donor
`e0614d7a6713369bbcfe42152223f1cdaec3dbf1` used by the port. It distinguishes
R10's **Auto** from its separate **Auto 2** mode.

## What these modes change

Both decide whether to append synthetic CMv4.0 metadata to an RPU without the
CMv4.0 Level 254 marker. They use libdovi's
`dovi_rpu_add_cmv40_safe_default_metadata()`. Neither creates a newly authored
Dolby Vision grade or measures the actual image pixels.

Existing authored CMv4.0 metadata is left alone by the append operation. If
CMv4.0 stripping is enabled, stripping takes precedence over either append mode.
In the discussion below, “keep CMv2.9” means bypassing synthetic append, not
stripping an authored CMv4.0 stream.

The decision concerns metadata supplied to the downstream Dolby processor. It
does not select the loaded module, set the output to player-led/display-led, or
change Kodi's presentation clock. The original/newer backend routing is separate;
see [dovi5 loading and routing](dovi5-loading.md).

## Decision rules in p3i

Assume a valid, editable CMv2.9 RPU with L2 trims and usable peak information.
The rules are:

```text
Smart:
  append when current L1 peak <= display peak × (100 + headroom %) / 100

Auto (source peak):
  append when source peak <= selected threshold
```

Smart's default headroom is **20%**, configurable from 0 to 50%. Auto's threshold
defaults to **Display peak**; its other choices are **1000, 2000, 4000 and 10000
nits**. The percentage setting has no effect on Auto. Equality appends in both
modes.

| Property | Smart | Auto (source peak) |
| --- | --- | --- |
| p3i setting value | 3; current default | 4 |
| Content input | `dm_data.level1->max_pq` | `source_max_pq` |
| Interpretation | Dynamic Level 1 peak for the current RPU | Source-peak metadata, usually stable across the title |
| Threshold | Configured display peak plus percentage headroom | Display peak or the selected fixed value |
| Decision timing | Each changed RPU; identical input can reuse cached output | Each changed RPU; usually the same decision if source peak/L2 state stays unchanged |
| No L2 trims | Attempt append regardless of peak comparison | Same |
| Missing input | Missing L1 or nonpositive display input: attempt append | Missing/invalid source PQ or nonpositive selected threshold: attempt append |
| Existing Level 254 | Preserve existing CMv4.0 | Same |
| Strip enabled | Strip takes precedence | Same |

“Attempt append” still requires valid parsed metadata and successful libdovi
serialization. A fallback decision to append does not make malformed metadata
editable.

Neither mode reads Level 6 mastering-display maximum or MaxCLL for this decision.
The source-peak field is not a fresh pixel measurement and must not be described
as guaranteed actual content peak. Level 1 also relies on the authored metadata.

The display input is Kodi's configured Dolby/VSVDB maximum-luminance value. It
can be populated from the display capability or changed through configuration;
it is not a live measurement of panel brightness or the final downstream
tone-mapping target.

## An example where they differ

Take a display input of **1000 nits**, Smart headroom of **20%**, and Auto set to
**Display peak**. Smart's threshold is then **1200 nits**. These illustrative
values are the peaks after conversion to nits; assume L2 is present.

| Source peak | Current L1 peak | Smart | Auto |
| --- | --- | --- | --- |
| 4000 nits | 600 nits | Append | Keep CMv2.9 |
| 4000 nits | 1100 nits | Append | Keep CMv2.9 |
| 4000 nits | 1500 nits | Keep CMv2.9 | Keep CMv2.9 |
| 1000 nits | 900 nits | Append | Append |

Smart can append in lower-peak scenes of a high-source-peak title while retaining
CMv2.9 in higher-peak scenes. Auto treats that title more uniformly. If Auto's
threshold is changed to 4000 nits, the first three rows all append: that threshold
is a user-selected policy, even though the configured display input remains
1000 nits.

Smart has no hysteresis or minimum dwell time. Metadata crossing its threshold
can change the append decision on successive RPUs. The 20% headroom moves the
threshold; it does not smooth those changes. A scene's metadata may persist over
many frames, so this does not mean the version necessarily changes every frame.

## What we inherited from R10 Auto

R10 Auto reads `source_max_pq` and compares it inclusively with the display peak
or the same four fixed thresholds. It also appends when L2 is absent and leaves
existing Level 254 metadata alone. p3i retains that central policy as **Auto
(source peak)**. The donor calls Auto value 3; p3i keeps its existing Smart at 3
and assigns source Auto value 4. R10's overall append default is Off, whereas
p3i's is Smart. These numeric defaults are source defaults, not saved user
selections. See the pinned [R10 decision code](https://github.com/avdvplus/xbmc/blob/e0614d7a6713369bbcfe42152223f1cdaec3dbf1/xbmc/utils/BitstreamConverterDoVi.cpp),
[mode definitions](https://github.com/avdvplus/xbmc/blob/e0614d7a6713369bbcfe42152223f1cdaec3dbf1/xbmc/utils/BitstreamConverter.h)
and [settings](https://github.com/avdvplus/xbmc/blob/e0614d7a6713369bbcfe42152223f1cdaec3dbf1/system/settings/settings.xml).

The port is not identical in edge cases. p3i explicitly treats absent/invalid
source-peak or threshold data as an append candidate. R10 directly converts and
compares its source field, without those same validation gates.

## R10 Auto 2 is the closer counterpart to Smart

Auto 2 uses L1 with percentage headroom, like Smart. R10 compares in PQ space
after converting the threshold; p3i converts the L1 peak to integer nits first.
They can differ near rounding boundaries. Missing display data also differs:
R10 Auto 2 suppresses append with L2 present, while p3i Smart attempts append.
[R10 decision implementation](https://github.com/avdvplus/xbmc/blob/e0614d7a6713369bbcfe42152223f1cdaec3dbf1/xbmc/utils/BitstreamConverterDoVi.cpp).

More significantly, R10's codec pins Auto 2 to **Always** during player-led
output. Its source comment/log gives sink DV relatching from per-frame version
changes as the reason. p3i Smart currently has no equivalent pinning on its
eligible player-led routes. This donor safeguard is evidence of a compatibility
concern, not proof that our current hardware suffers the same failure.
[R10 codec guard](https://github.com/avdvplus/xbmc/blob/e0614d7a6713369bbcfe42152223f1cdaec3dbf1/xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp).

## Which is better?

**Smart is more selective; Auto is more predictable. Picture-quality superiority
has not been established.**

Smart has a useful rationale: it can retain CMv2.9 with its existing L2 trims in
high-peak scenes while appending synthetic CMv4.0 in lower-peak scenes. However,
peak alone does not tell us whether the downstream processor will produce better
highlights, midtones or color with either version. That needs matched visual or
measurement evidence. The 20% threshold is a heuristic, not a Dolby quality rule.

| Goal or situation | My recommendation |
| --- | --- |
| Adaptive appending on validated display-led playback | Keep Smart at 20% as the current default; it uses the more local content input. |
| Stable metadata-version policy on CMv2.9 titles | Use Auto with Display peak, accepting its broader rejection of high-source-peak titles. |
| Player-led output while dynamic-switch behavior is unverified | Prefer source Auto for conditional appending, or Always if consistent synthetic CMv4.0 is the explicit goal. R10's Auto 2 guard warrants testing before recommending dynamic Smart there. |
| Preserve the original authored metadata without synthetic additions | Use Off. |
| Original CMv4.0 title with stripping disabled | Smart versus Auto makes no append difference. |

These are engineering recommendations based on the source policies, not device
picture-quality rankings. Source Auto usually reduces version changes but is not
a formal guarantee of none: source metadata and L2 presence can change.

Both modes share p3i's native-source, output-mode, VP and capability gates.
Supported player-led controls require registered newer-backend capability; that
does not by itself prove the newer backend is applied to every frame or output
route. Neither mode creates CMv4.0 processing support in an unsupported decoder,
module or display.

## Evidence and limits

The p3i implementation is in [BitstreamConverter.cpp](../xbmc/utils/BitstreamConverter.cpp),
[its mode/setter definitions](../xbmc/utils/BitstreamConverter.h),
[the AML codec's settings and runtime gates](../xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp),
[the display input setup](../xbmc/windowing/amlogic/DolbyVisionAML.cpp) and
[settings.xml](../system/settings/settings.xml).

`tools/test-dv-cmv4.py` exercises the production decision methods, settings
propagation and cache invalidation with host stubs. It does not execute the real
libdovi parser or prove GUI visibility, HDMI behavior or picture quality.

The older [Smart implementation guide](dv-cmv4-smart-bypass.md) describes
`source_max_pq` as Smart's input and makes broad picture-quality claims. Current
Smart uses L1; those historical claims should not override this source-backed
comparison. No runtime setting or algorithm is changed by this document.
