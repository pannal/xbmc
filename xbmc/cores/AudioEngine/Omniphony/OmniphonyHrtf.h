/*
 *  Copyright (C) 2005-2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "OmniphonyTool.h"

#include <string>

namespace ActiveAE
{

//! \brief Which binaural response the render uses. Order matches settings.xml.
enum OmniphonyHrtfMode
{
  OMNI_HRTF_BUILTIN = 0,
  OMNI_HRTF_PERSONAL = 1, //!< "Custom HRTF (HRIR)": a SOFA HRTF set of the listener's
  OMNI_HRTF_ROOM = 2, //!< "Custom Room (BRIR)": a measured room - see COmniphonyRoom
};

/*!
 * \brief The listener's own HRTF set, kept in the cache - see COmniphonyCache.
 *
 * A head-related transfer function describes how a particular head and pair
 * of ears colour sound arriving from each direction. The engine ships one
 * measured set and can be pointed at another, supplied as a SOFA file.
 *
 * The engine opens a set by its path with the C library, so it cannot read a
 * Kodi path, and a set on a share would have to be there whenever a film
 * starts. So a chosen set is copied into the cache as `<name>.sofa`, and that
 * copy is what plays; the engine keeps its finished HRIR grids beside it,
 * `<name>-<kHz>.grid`, one per stream rate, and builds the 48 kHz one when
 * the set is chosen. The engine is asked what the copy holds before it is
 * kept, and a set already kept under the name - the same file chosen again -
 * is recognised by its identity and left as it is.
 */
class COmniphonyHrtf
{
public:
  //! The stream rate whose grid is built when a set is chosen.
  static constexpr unsigned int GRID_RATE = 48000;

  enum class Result
  {
    Ok, //!< copied into the cache and checked
    Cached, //!< already in the cache: the same file was chosen before
    NotFound, //!< the file could not be opened
    Unusable, //!< the engine cannot read it as a SOFA file - see Outcome::detail
    WrongConvention, //!< a SOFA file neither binaural stage takes
    RoomResponse, //!< a measured room, which belongs under Custom Room (BRIR)
    CopyFailed, //!< could not be copied into the cache
    TooLarge, //!< more than this device has free memory for
    ReadFailed, //!< the file ended before the size it stated
    Unavailable, //!< the engine could not be asked
    //! The engine cannot load it: it said so when asked for its grid, or
    //! stopped on it - see Outcome::detail. The set kept before stays.
    EngineFailed,
    Cancelled, //!< the listener stopped it
  };

  struct Outcome
  {
    Result result{Result::Unavailable};
    OmniphonySofaInfo contents;
    bool described{false}; //!< \ref contents holds what the engine found
    std::string name; //!< what the set is kept as in the cache
    bool gridKept{false}; //!< its GRID_RATE grid is in the cache
    std::string detail; //!< the engine's words, for Unusable, TooLarge and EngineFailed
  };

  /*!
   * \brief Check \p chosen and keep it in the cache, behind busy dialogs that
   * can cancel it. A cancel, or a file refused, leaves the cache as it was.
   * Only the GUI thread may call this.
   */
  static Outcome Choose(const std::string& chosen);

  //! \brief What became of the copy of a set for a stream - see ForStream.
  enum class Copy
  {
    Kept, //!< in the cache as it was
    Again, //!< gone from the cache, and copied again
    Gone, //!< gone from the cache, and the file it was copied from too
    Refused, //!< gone from the cache, and the file is no longer a set
  };

  /*!
   * \brief The local path of the copy of \p chosen in the cache, for a
   * stream about to start, or empty when there is none and it cannot be
   * copied again: the built-in set plays then. A copy missing from the cache
   * is made again, without a dialog; its grids are the engine's to build.
   * \p copy says which.
   */
  static std::string ForStream(const std::string& chosen, Copy& copy);

  //! \brief Where the engine keeps the grids of \p chosen: `<name>-{khz}.grid`
  //! in the cache, local.
  static std::string GridPath(const std::string& chosen);

  //! \brief Localised, user-facing account of \p outcome.
  static std::string Explain(const Outcome& outcome);
};

} // namespace ActiveAE
