/*
 *      Copyright (C) 2010-2013 Team XBMC
 *      http://xbmc.org
 *
 *  This Program is free software; you can redistribute it and/or modify
 *  it under the terms of the GNU General Public License as published by
 *  the Free Software Foundation; either version 2, or (at your option)
 *  any later version.
 *
 *  This Program is distributed in the hope that it will be useful,
 *  but WITHOUT ANY WARRANTY; without even the implied warranty of
 *  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 *  GNU General Public License for more details.
 *
 *  You should have received a copy of the GNU General Public License
 *  along with XBMC; see the file COPYING.  If not, see
 *  <http://www.gnu.org/licenses/>.
 *
 */

#version 100

precision mediump float;
uniform sampler2D m_samp0;
// Large, changing glyph atlases need precise coordinates to keep stationary
// subtitle edges stable. Leave color arithmetic at its existing precision.
#ifdef GL_FRAGMENT_PRECISION_HIGH
varying highp vec4 m_cord0;
#else
varying mediump vec4 m_cord0;
#endif
varying lowp vec4 m_colour;
uniform float m_sdrPeak;

// Bitmap-subtitle glyph shader. Premultiplies in linear light so
// semi-transparent anti-aliased edges don't appear as a dark fringe
// under HDR/PQ output (and render more correctly under SDR too).
// Output is premultiplied — pair this shader with
// glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA).
void main ()
{
  float alpha = m_colour.a * texture2D(m_samp0, m_cord0.xy).a;
  vec3 rgbLinear = pow(m_colour.rgb, vec3(2.2));
  vec3 rgbPma = pow(rgbLinear * alpha, vec3(1.0 / 2.2));

#if defined(KODI_LIMITED_RANGE)
  rgbPma *= (235.0 - 16.0) / 255.0;
  rgbPma += alpha * (16.0 / 255.0);
#endif

#if defined(KODI_TRANSFER_PQ)
  rgbPma *= m_sdrPeak;
#endif

  gl_FragColor = vec4(rgbPma, alpha);
}
