# Background transitions

The background fade limit is enabled by default. A background control, and any
control showing an opaque image such as a poster, retains at most one outgoing
image and one incoming image. If selection changes while that pair is fading,
or while the current image is still fading in, it finishes that transition and
then loads the newest pending selection. Intermediate pending names are replaced without decoding,
uploading or retaining more textures. Returning to the current incoming image
withdraws a pending selection. Existing motion suppression remains independent.

The final background can therefore lag selection by the remaining transition
and image-loading time. The limit avoids a brightness jump from abruptly
removing a still-visible layer, and every outgoing image starts its fade at full
strength. Other foreground controls, whose images are translucent, keep their
existing fade behavior. Background classification uses the existing
declared-coverage and optional selection-image rules; this is not a measurement
of occluded pixels.

## Device comparison

Merge these GUI elements into advancedsettings.xml and restart Kodi:

```xml
<advancedsettings>
  <gui>
    <limitbackgroundfades>true</limitbackgroundfades>
    <singlepasscrossfade>true</singlepasscrossfade>
    <asynctextureupload>false</asynctextureupload>
    <afbcfanart>false</afbcfanart>
    <afbcimages>false</afbcimages>
  </gui>
</advancedsettings>
```

Both options work with ordinary textures, synchronous uploads and AFBC
textures. They do not change the JPEG cache or fanartres/imageres. Keep the
existing motion-delay options disabled when comparing actual crossfades;
otherwise those options can suppress the workload.

To let interrupted fades stack, set limitbackgroundfades to false. Compare one
switch at a time and keep the skin/view/input sequence unchanged. Check
perceived smoothness with debug logging off as well as collecting a short
capture with it on; logging and its statistics overlay add work.

## Single-pass crossfade

A crossfade normally draws the outgoing image and then the incoming image over
it, so every covered pixel is shaded, blended and written twice. When exactly
one outgoing copy remains, as the fade limit ensures for backgrounds, both
images are drawn by one quad instead: a GLES shader samples both textures and
outputs their composition premultiplied, blended with ONE, ONE_MINUS_SRC_ALPHA.
That equals the two separate SRC_ALPHA draws, including destination alpha.
Each layer keeps its own coordinates, clipping, orientation, tint, GUI colour,
limited-range and PQ adjustments. This applies to any image control, also on
non-Amlogic GLES builds, not only to backgrounds.

A translucent pair stays in the back-to-front pass with depth testing, so
posters drawn earlier in the opaque front-to-back pass still reject hidden
pixels. It needs no extra framebuffer. Texture fetches are unchanged, two per
pixel; the saving is the second fragment, blend and framebuffer write per
covered pixel.

Two opaque images dissolve instead: the outgoing image stays opaque under the
incoming one and the pair is drawn in the opaque front-to-back pass without
blending, so it hides what is behind it during the whole fade, like either image
alone. This relies on the fade limit keeping an opaque image to one pair whose
outgoing image was at full strength; with limitbackgroundfades off an opaque
pair keeps the separate translucent draws. A transition that stacked see-through
layers anyway, because an image with alpha took part, also keeps the separate
draws until it completes, so a partly faded outgoing image never jumps back to
full strength when the stack drops to one pair. Below a fading parent, such as a
window or a group faded behind a dialog, the pair is composed as at full opacity
and mixed with what is behind it by the parent's opacity in the back-to-front
pass, so its brightness follows the parent's fade continuously. A translucent
diffuse colour or an image with alpha keep the translucent crossfade.

Mismatched geometry, borders, diffuse or animated textures, three or more
layers and backends without the shader keep the separate draws.

The single pass is enabled by default. Set
`<singlepasscrossfade>false</singlepasscrossfade>` in the gui section to compare
the separate draws.

## Host validation

Run `python3 tools/background-fades/test-fades.py` to compile and exercise the
production image transition methods under ASan/UBSan. The fixture controls
image readiness and covers rapid selections for backgrounds and opaque images,
waiting for the current fade-in, reversals, pending cache policy, resource
retirement, existing motion suppression and disabled/translucent foreground
compatibility, including 2,000 retargets.

Run `python3 tools/background-fades/test-integration.py` to check the actual
image render dispatch: a single-pass pair with the outgoing image's history, a
declined pair, three layers, an unready image, the disabled setting and an image
without a fade.

Run `python3 tools/background-fades/test-crossfade.py` with EGL/GLES and Mesa.
It assembles the production shaders as CGLESShader does and compares the
production DrawCrossfade with the production separate Draw/BeginDraw/End path,
for all five GUI colour, limited-range and PQ shader variants and both C++
colour ranges. Half-float targets agree within 0.002 and RGBA8 targets within
3/255, the fixed-point rounding of one extra blend. It also checks one draw
instead of two. An opaque pair is compared with drawing the opaque outgoing
image and then the incoming one, with blending off, and below a fading parent
with that result mixed with the background by the parent's opacity. Seven
semantic mutations are rejected.