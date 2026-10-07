/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "OmniphonyTool.h"

#include <memory>
#include <string>

namespace ActiveAE
{

/*!
 * \brief A measured listening room (BRIR) the listener has chosen, kept in
 * the cache - see COmniphonyCache.
 *
 * A room set is the responses at a dummy head's ears from every loudspeaker
 * of a real array, at every head orientation measured: the BBC's 7.1.4 room
 * is 274 MB. Without head tracking, which Kodi has none of, a session renders
 * only the orientation nearest straight ahead, so the set is prepared once,
 * when it is chosen, into a room of that orientation alone: a few MB that
 * loads in milliseconds (orender_brir_prepare). It is kept in the cache as
 * `<name>.room`, and that is what plays.
 *
 * The room is the engine's, of one format and carrying the identity of the
 * file it was made from. Choosing the same file again is recognised by that
 * identity and prepares nothing. A room the engine no longer reads - an
 * update of the engine that prepares rooms differently - or one gone from the
 * cache is prepared again from its file as a film starts, while the built-in
 * set plays (see CDVDAudioCodecOmniphony); with the file gone too, the
 * listener is told to choose the room again.
 */
class COmniphonyRoom
{
public:
  enum class Result
  {
    Ok, //!< prepared into the cache
    Cached, //!< already in the cache: the same file was chosen before
    NotFound, //!< the file could not be opened
    Unusable, //!< the engine cannot read it as a SOFA file - see Outcome::detail
    NotARoom, //!< a SOFA file the room stage does not take (an HRTF set, say)
    TooLarge, //!< not enough memory to prepare it
    ReadFailed, //!< could not be read to the end
    WriteFailed, //!< the room could not be written
    Unavailable, //!< the engine could not be asked
    EngineFailed, //!< the engine failed otherwise - see Outcome::detail
    Cancelled, //!< the listener stopped it
  };

  struct Outcome
  {
    Result result{Result::Unavailable};
    OmniphonySofaInfo contents; //!< what the file holds, when \ref described
    bool described{false};
    std::string name; //!< what the room is kept as in the cache
    std::string detail; //!< the engine's words, when it failed
  };

  /*!
   * \brief Check \p chosen and prepare it into the cache, in one pass behind
   * a busy dialog that can cancel it. A cancel, or a file refused, leaves the
   * cache as it was, and a file the room kept under its name was prepared
   * from is not prepared again. Only the GUI thread may call this.
   */
  static Outcome Choose(const std::string& chosen);

  //! What PrepareAgain came to.
  enum class Again
  {
    Prepared, //!< in place of the room in the cache
    Failed,
    Picked, //!< the room was picked in the settings meanwhile; the pick's stays
  };

  /*!
   * \brief Prepare the room of \p chosen again from that file, without a
   * dialog: what a stream does when the engine cannot use the room it was
   * given. \p tool, made by Preparer into AgainPath, is run here, and can be
   * cancelled from another thread. What it prepares is put in place of the
   * room only if the room has not been picked in the settings since \p epoch,
   * the COmniphonyCache::PickEpoch the stream took before it read \p chosen
   * from its settings: a file of the same name picked from elsewhere since is
   * the listener's newer choice.
   */
  static Again PrepareAgain(const std::string& chosen, COmniphonyTool& tool, uint64_t epoch);

  //! \brief The local path PrepareAgain's tool prepares into, beside the
  //! room of \p chosen.
  static std::string AgainPath(const std::string& chosen);

  //! \brief The tool that prepares \p source into \p out, a local path,
  //! unless \p kept (a local path, or empty) is the room already prepared
  //! from it: what PrepareAgain runs, into AgainPath, made by a caller that
  //! wants to cancel it.
  static std::unique_ptr<COmniphonyTool> Preparer(const std::string& out,
                                                  const std::string& source,
                                                  const std::string& kept = {});

  //! \brief The local path of the room of \p chosen in the cache, whether or
  //! not it is there.
  static std::string RoomPath(const std::string& chosen);

  /*!
   * \brief Whether \p path is a prepared room, by its magic: for a room a
   * listener's config.yaml names directly, which is read whole at every start
   * unless it is.
   */
  static bool IsPrepared(const std::string& path);

  //! \brief Localised, user-facing account of \p outcome.
  static std::string Explain(const Outcome& outcome);
};

} // namespace ActiveAE
