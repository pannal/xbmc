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

#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif
uniform sampler2D m_samp0;
varying vec4 m_cord0;
uniform float m_sdrPeak;
uniform float m_pma;

#if !defined(KODI_PQ_TO_SDR)
uniform float m_sdrBrightness;
uniform float m_sdrSaturation;
#endif

#if defined(KODI_PQ_TO_SDR)
uniform float m_pqRefNits;
uniform float m_pqSaturation;
uniform float m_pqTonemap;
uniform float m_pqMode;
vec3 pqToSdr(vec3 pq)
{
  const float ST2084_m1 = 2610.0 / (4096.0 * 4.0);
  const float ST2084_m2 = (2523.0 / 4096.0) * 128.0;
  const float ST2084_c1 = 3424.0 / 4096.0;
  const float ST2084_c2 = (2413.0 / 4096.0) * 32.0;
  const float ST2084_c3 = (2392.0 / 4096.0) * 32.0;
  pq = clamp(pq, vec3(0.0), vec3(1.0));
  vec3 p = pow(pq, vec3(1.0 / ST2084_m2));
  vec3 linear = pow(max(p - ST2084_c1, vec3(0.0)) / (ST2084_c2 - ST2084_c3 * p),
                    vec3(1.0 / ST2084_m1));
  linear = linear * (10000.0 / m_pqRefNits);
  vec3 srgb;
  float luma;
  if (m_pqMode < 0.5)
  {
    // Classic: BT.2020->BT.709 gamut + per-channel tonemap
    const mat3 bt2020_to_bt709 = mat3(
       1.660496, -0.124546, -0.018154,
      -0.587656,  1.132895, -0.100597,
      -0.072840, -0.008348,  1.118751);
    linear = max(bt2020_to_bt709 * linear, vec3(0.0));
    linear = mix(min(linear, vec3(1.0)), linear / (vec3(1.0) + linear), m_pqTonemap);
    srgb = pow(clamp(linear, vec3(0.0), vec3(1.0)), vec3(1.0 / 2.3));
    luma = dot(srgb, vec3(0.2126, 0.7152, 0.0722));
  }
  else if (m_pqMode < 1.5)
  {
    // Linear: BT.2020->BT.709 gamut + luminance-based tonemap
    const mat3 bt2020_to_bt709 = mat3(
       1.660496, -0.124546, -0.018154,
      -0.587656,  1.132895, -0.100597,
      -0.072840, -0.008348,  1.118751);
    linear = max(bt2020_to_bt709 * linear, vec3(0.0));
    float lum = dot(linear, vec3(0.2126, 0.7152, 0.0722));
    float lum_tm = mix(min(lum, 1.0), lum / (1.0 + lum), m_pqTonemap);
    linear = linear * (lum_tm / max(lum, 1e-6));
    srgb = pow(clamp(linear, vec3(0.0), vec3(1.0)), vec3(1.0 / 2.3));
    luma = dot(srgb, vec3(0.2126, 0.7152, 0.0722));
  }
  else
  {
    // Luma: no gamut conversion, BT.2020 luma + luminance-based tonemap
    float lum = dot(linear, vec3(0.2627, 0.6780, 0.0593));
    float lum_tm = mix(min(lum, 1.0), lum / (1.0 + lum), m_pqTonemap);
    linear = linear * (lum_tm / max(lum, 1e-6));
    srgb = pow(clamp(linear, vec3(0.0), vec3(1.0)), vec3(1.0 / 2.3));
    luma = dot(srgb, vec3(0.2627, 0.6780, 0.0593));
  }
  srgb = clamp(mix(vec3(luma), srgb, m_pqSaturation), vec3(0.0), vec3(1.0));
  return srgb;
}
#endif

void main ()
{
  vec4 rgb;

  rgb = texture2D(m_samp0, m_cord0.xy);

#if defined(KODI_PQ_TO_SDR)
  // Un-premultiply alpha before non-linear PQ conversion, then re-premultiply
  if (rgb.a > 0.0)
  {
    rgb.rgb /= rgb.a;
    rgb.rgb = pqToSdr(rgb.rgb);
    rgb.rgb *= rgb.a;
  }
#endif

#if !defined(KODI_PQ_TO_SDR)
  // Normal bitmap textures encode linear-light premultiplied RGB with gamma 2.2.
  // Luma mixing and gain are homogeneous, so they also work before dividing by
  // alpha. Clamp in linear light to alpha for PMA, then restore the encoding.
  // Bypass neutral values exactly to preserve the existing appearance.
  if (m_sdrBrightness != 1.0 || m_sdrSaturation != 1.0)
  {
    vec3 linear = pow(max(rgb.rgb, vec3(0.0)), vec3(2.2));
    float luma = dot(linear, vec3(0.2126, 0.7152, 0.0722));
    float ceiling = mix(1.0, rgb.a, m_pma);
    linear = clamp(mix(vec3(luma), linear, m_sdrSaturation) * m_sdrBrightness,
                   vec3(0.0), vec3(ceiling));
    rgb.rgb = pow(linear, vec3(1.0 / 2.2));
  }
#endif

#if defined(KODI_LIMITED_RANGE)
  rgb.rgb *= (235.0 - 16.0) / 255.0;
  rgb.rgb += mix(1.0, rgb.a, m_pma) * 16.0 / 255.0;
#endif

#if defined(KODI_TRANSFER_PQ)
  rgb.rgb *= m_sdrPeak;
#endif

  gl_FragColor = rgb;
}
