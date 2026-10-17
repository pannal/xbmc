/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <string>

namespace ActiveAE
{

/*!
 * \brief The profile's cache of what the binaural renderer is given in place
 * of the listener's SOFA files: special://profile/omniphony/cache/.
 *
 * A chosen file is kept there under its own name, without its extension, so
 * that the folder says which is which:
 *
 *  - an HRTF set as `<name>.sofa`, a copy the engine can open wherever the
 *    file was chosen from, and its finished HRIR grids as
 *    `<name>-<kHz>.grid`, one per stream rate (`<name>-48.grid`);
 *  - a measured room as `<name>.room`, the room prepared from the file.
 *
 * Grids and rooms carry the identity of the SOFA file they were made from,
 * and are of one format, which the engine reads and no other: the engine
 * says what a file is (`omniphony-helper --describe`), so a file chosen
 * again is matched against what is kept for it, and nothing is made again
 * that is already there. A file of the same name chosen from elsewhere, with
 * other contents, replaces what is kept under that name.
 *
 * Nothing is discarded when the listener switches between responses: going
 * back to a file costs nothing. The two most recently used HRTF sets and the
 * two most recently used rooms are kept, each with what belongs to it, and
 * an older one is removed when a third is chosen - see Trim.
 */
class COmniphonyCache
{
public:
  //! HRTF sets, and rooms, kept.
  static constexpr unsigned int KEEP = 2;

  //! \brief The cache folder, a Kodi path ending in '/', created if missing.
  static std::string Folder();

  //! \brief The name a chosen file is kept under: its file name without its
  //! last extension (`bbcrdlr_systemG.sofa` is kept as `bbcrdlr_systemG`).
  static std::string Name(const std::string& chosen);

  //! \brief The Kodi path of `<name><suffix>` in the cache.
  static std::string File(const std::string& name, const std::string& suffix);

  //! \brief The same, as a local path for the engine.
  static std::string LocalFile(const std::string& name, const std::string& suffix);

  //! \brief Mark \p path as used now, for Trim.
  static void Touch(const std::string& path);

  /*!
   * \brief Keep the KEEP most recently used entries of a kind and remove the
   * rest, each with everything kept for it.
   *
   * \param main `.sofa` for HRTF sets (with their grids), `.room` for rooms
   */
  static void Trim(const std::string& main);

  //! \brief Remove everything kept under \p name for the kind \p main.
  static void Forget(const std::string& name, const std::string& main);

  /*!
   * \brief Put \p made, finished, in the place of \p file: one rename,
   * which replaces \p file atomically, so that \p file is never left
   * missing - it is what plays until this succeeds.
   *
   * \return false, \p made removed and \p file as it was, when it fails
   */
  static bool Promote(const std::string& made, const std::string& file);

  //! \brief Whether \p file is one of the grids kept for \p name:
  //! `<name>-<digits>.grid`, with or without `.part` or `.next` after it.
  static bool IsGridOf(const std::string& file, const std::string& name);
};

} // namespace ActiveAE
