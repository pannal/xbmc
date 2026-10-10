/* SPDX-License-Identifier: GPL-2.0-or-later */
#if defined(KODI_GUI_COLOUR)
uniform float m_guiTuning;
uniform float m_guiPeak;
uniform float m_guiSaturation;

vec3 guiColour(vec3 rgb)
{
  // Default/unset and neutral controls preserve the old output exactly.
  if (m_guiTuning < 0.5 || (m_guiPeak == 1.0 && m_guiSaturation == 1.0))
    return rgb;
  if (m_guiSaturation != 1.0)
  {
    vec3 linear = pow(max(rgb, vec3(0.0)), vec3(2.2));
    float luma = dot(linear, vec3(0.2126, 0.7152, 0.0722));
    linear = clamp(mix(vec3(luma), linear, m_guiSaturation), vec3(0.0), vec3(1.0));
    rgb = pow(linear, vec3(1.0 / 2.2));
  }
  return rgb * m_guiPeak;
}

#endif
