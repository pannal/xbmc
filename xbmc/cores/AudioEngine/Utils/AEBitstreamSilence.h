/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <stdint.h>
#include <vector>

class CAEStreamInfo;

/*!
 * \brief Builds decodable silent AC-3, E-AC-3 and DTS core packets.
 *
 * Some receivers flush their decoder when a passthrough bitstream stops, so the
 * audio they still held at that moment is never heard. Sending silent frames of
 * the same format while paused keeps the decoder running. The silent packet
 * keeps the size and header of the source packet so the receiver sees no format
 * change, and AC-3 and E-AC-3 frames are filled with skip data because some
 * receivers also mute frames that are mostly empty. A DTS-HD packet becomes a
 * silent copy of its core frame, without the extension substream.
 */
class CAEBitstreamSilence
{
public:
  /*!
   * \brief Generate a silent packet that matches a passthrough packet.
   * \param info stream info of the packet
   * \param data the packet
   * \param size packet size in bytes
   * \param out receives the silent packet, which has the same size as the packet, or the
   *            size of its core frame for DTS-HD
   * \return false with an empty out if the packet is not supported
   */
  static bool Generate(const CAEStreamInfo& info,
                       const uint8_t* data,
                       unsigned int size,
                       std::vector<uint8_t>& out);
};
