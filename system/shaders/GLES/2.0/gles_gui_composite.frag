/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#version 100

// The LUTs hold values down to ~1e-6 (fp16 subnormals, flushed on Mali).
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif
varying vec2 v_tex;
uniform sampler2D u_samp;       // GUI FBO texture (premultiplied, gamma encoded)
uniform sampler2D u_lutDegamma; // GUI code -> linear, the route's SDR decode (0..1 = white)
uniform sampler2D u_lutTF;      // 2.2-encoded linear -> PQ at the GUI white
uniform sampler2D u_hdr;        // PQ-authored disc menu graphics,
                                // premultiplied, already in the output encoding
uniform float u_hasHdr;         // 1.0 when u_hdr holds content this frame

// BT.709 -> BT.2020 color space conversion matrix (applied in linear light)
const mat3 bt709_to_bt2020 = mat3(
  0.6274,  0.0691,  0.0164,
  0.3293,  0.9195,  0.0880,
  0.0433,  0.0114,  0.8956
);

// The LUTs hold one entry per 8-bit code, entry i at code i/255: sample at
// texel centres, or the ends are off by half a texel.
float lut(sampler2D s, float x)
{
  return texture2D(s, vec2(x * (255.0 / 256.0) + 0.5 / 256.0, 0.5)).r;
}

void main()
{
  vec4 gui = texture2D(u_samp, v_tex);
  vec4 hdr = vec4(0.0);
  if (u_hasHdr > 0.5)
    hdr = texture2D(u_hdr, v_tex);

  // Skip pixels nothing wrote to. Blend unit would still preserve video
  // bit-exactly via DST*(1-0)+garbage*0=DST, but discard makes it structural
  // and avoids the transfer math, BO read and BO write for those pixels.
  if (gui.a == 0.0 && hdr.a == 0.0)
    discard;

  vec3 result = vec3(0.0);
  if (gui.a > 0.0)
  {
    // Encode the premultiplied GUI value, as the hardware does with the back
    // buffer: a translucent GUI then keeps the brightness it has without a
    // disc menu.
    vec3 linear = vec3(lut(u_lutDegamma, gui.r),
                       lut(u_lutDegamma, gui.g),
                       lut(u_lutDegamma, gui.b));
    linear = max(bt709_to_bt2020 * linear, vec3(0.0));

    // Back to a gamma domain for the PQ LUT: PQ is steep near black, and a
    // LUT indexed in linear light has too few entries there.
    vec3 g = pow(min(linear, vec3(1.0)), vec3(1.0 / 2.2));
    result = vec3(lut(u_lutTF, g.r), lut(u_lutTF, g.g), lut(u_lutTF, g.b));

    // The hardware encoded the GUI to PQ at ~12 bits; this goes out at 8.
    // An ordered dither (4x4 Bayer, +-0.5 LSB) keeps gradients from banding.
    vec2 p = floor(gl_FragCoord.xy);
    vec2 p1 = mod(p, 2.0);
    vec2 p2 = mod(floor(p / 2.0), 2.0);
    float bayer = (4.0 * mod(2.0 * p1.x + 3.0 * p1.y, 4.0) +
                   mod(2.0 * p2.x + 3.0 * p2.y, 4.0) + 0.5) / 16.0;
    result = max(result + (bayer - 0.5) / 255.0, vec3(0.0));
  }

  // GUI over the disc menu graphics, premultiplied: the graphics are already
  // in the output encoding and pass through untransformed.
  result += hdr.rgb * (1.0 - gui.a);
  float alpha = gui.a + hdr.a * (1.0 - gui.a);

  // Limited-range encoding at the BO write boundary (premultiplied: the
  // offset scales with alpha).
#ifdef KODI_LIMITED_RANGE
  result = result * ((235.0 - 16.0) / 255.0) + (16.0 / 255.0) * alpha;
#endif

  gl_FragColor = vec4(result, alpha);
}
