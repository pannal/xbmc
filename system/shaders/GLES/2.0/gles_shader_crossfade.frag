/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#version 100

precision mediump float;
uniform sampler2D m_samp0;
uniform sampler2D m_samp1;
varying vec4 m_cord0;
varying vec4 m_cord1;
varying lowp vec4 m_colour;
uniform lowp vec4 m_unicol;
uniform float m_sdrPeak;

// Draws an incoming image (m_samp0, m_unicol) over an outgoing one (m_samp1,
// m_colour) in one pass. Each layer gets the output adjustments of
// gles_shader_texture.frag. The result is premultiplied and blended with
// ONE, ONE_MINUS_SRC_ALPHA, which equals drawing the outgoing and then the
// incoming image with SRC_ALPHA, ONE_MINUS_SRC_ALPHA.
void main ()
{
  vec4 incoming = texture2D(m_samp0, m_cord0.xy) * m_unicol;
  vec4 outgoing = texture2D(m_samp1, m_cord1.xy) * m_colour;

#if defined(KODI_GUI_COLOUR)
  incoming.rgb = guiColour(incoming.rgb);
  outgoing.rgb = guiColour(outgoing.rgb);
#endif
#if defined(KODI_LIMITED_RANGE)
  incoming.rgb *= (235.0 - 16.0) / 255.0;
  incoming.rgb += 16.0 / 255.0;
  outgoing.rgb *= (235.0 - 16.0) / 255.0;
  outgoing.rgb += 16.0 / 255.0;
#endif
#if defined(KODI_TRANSFER_PQ)
  incoming.rgb *= m_sdrPeak;
  outgoing.rgb *= m_sdrPeak;
#endif

  float under = outgoing.a * (1.0 - incoming.a);
  gl_FragColor = vec4(incoming.rgb * incoming.a + outgoing.rgb * under, incoming.a + under);
}
