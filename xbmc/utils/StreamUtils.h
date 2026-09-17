/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <cstdint>
#include <string>

static constexpr int MP4_BOX_HEADER_SIZE = 8;

class StreamUtils
{
public:
  static int GetCodecPriority(const std::string& codec);

  /*!
   * \brief Make a FourCC code as unsigned integer value
   * \param c1 The first FourCC char
   * \param c2 The second FourCC char
   * \param c3 The third FourCC char
   * \param c4 The fourth FourCC char
   * \return The FourCC as unsigned integer value
   */
  static constexpr uint32_t MakeFourCC(char c1, char c2, char c3, char c4)
  {
    return ((static_cast<uint32_t>(c1) << 24) | (static_cast<uint32_t>(c2) << 16) |
            (static_cast<uint32_t>(c3) << 8) | (static_cast<uint32_t>(c4)));
  }

  /*!
   * \brief Get the codec name translated from ffmpeg codec id and profile
   * \param codecId The ffmpeg codec id
   * \param profile The ffmpeg codec profile
   * \return The codec name
   */
  static std::string GetCodecName(int codecId, int profile);

  /*!
   * \brief Whether a profile names a DTS:X stream, under either of its names
   *
   * DTS:X IMAX is DTS:X with a badge on it, so anything asking what shape the
   * presentation is - a bed with heights over it, and objects if the stream
   * declared any - has to take both.
   *
   * \param profile The ffmpeg codec profile
   * \return True for DTS:X and DTS:X IMAX, false for everything else
   */
  static bool IsDTSXProfile(int profile);

  /*!
   * \brief Get the number of audio objects a DTS:X stream declares
   *
   * The stream states it itself, in the alternate-profile syncword at the end of
   * its XLL frame. 0xF14000Dn is the first four bytes of the type-241 object
   * metadata element, and after that element's 28-bit fixed header the next four
   * bits are its declaration count minus one - the low nibble of that last byte.
   * So D0 declares one object, D1 two, through D4 at five, by construction
   * rather than by correlation.
   *
   * The ffmpeg this runs against reports that nibble in the level and leaves the
   * profile naming the codec, which is why nothing else here changes: DTS:X with
   * two objects and DTS:X with five are the same codec, so GetCodecName() above,
   * passthrough routing and every other place that enumerates the DTS-HD MA
   * family by profile go on seeing exactly what they saw before. The level says
   * which variant of a codec, and a later Auro-3D layout can say so in the same
   * field without disturbing this, because the profile is asked first.
   *
   * A stream that declares nothing reports nothing rather than zero, and that is
   * very nearly every DTS:X release in the wild: the older 0x02000850 form has
   * no such nibble, so the level stays at the AV_LEVEL_UNKNOWN it started at and
   * both object labels are left empty. Unlike Atmos, where a bed-only mix
   * genuinely declares zero objects and saying so is an answer, such a stream
   * has said nothing about objects at all.
   *
   * The level's low nibble carries it; the bits above are the heights'
   * business, below.
   *
   * \param profile The ffmpeg codec profile
   * \param level The ffmpeg codec level
   * \return The number of objects declared, or -1 when the stream declares none
   */
  static int GetDTSXObjectCount(int profile, int level);

  /*!
   * \brief Get the number of heights a DTS:X stream puts over its bed
   *
   * Not something the object count can say. D0 declares one object over 7.1
   * with the four heights, and one object over 5.1 with none at all. What does
   * say it is the object element's payload, whose protected prefix ends in a
   * byte naming the channel sets that follow, and this tree's dca_xll patch
   * reports that byte above the nibble: 0x10 when it was read, 0x20 when the
   * height quartet is among the sets.
   *
   * A stream with no object element, the 0x02000850 form that nearly every
   * DTS:X release carries, has the four heights: the matrix it carries places
   * the top front and top back pairs.
   *
   * \param profile The ffmpeg codec profile
   * \param level The ffmpeg codec level
   * \return 4 or 0, or -1 when the stream has not said - so that a caller can
   *         leave the heights out rather than assume them
   */
  static int GetDTSXHeightCount(int profile, int level);
};
