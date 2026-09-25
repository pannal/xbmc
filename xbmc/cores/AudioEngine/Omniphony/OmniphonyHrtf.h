/*
 *  Copyright (C) 2005-2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <string>

namespace ActiveAE
{

//! \brief Which head model the render uses. Order matches settings.xml.
enum OmniphonyHrtfMode
{
  OMNI_HRTF_BUILTIN = 0,
  OMNI_HRTF_PERSONAL = 1,
};

/*!
 * \brief The listener's own HRTF measurement, if they have chosen one.
 *
 * A head-related transfer function describes how a particular head and pair
 * of ears colour sound arriving from each direction. The engine ships one
 * measured set and can be pointed at another, supplied as a SOFA file.
 *
 * Kodi keeps exactly one such file, copied into the user profile, and treats
 * its presence as the whole of the answer to "is a personal HRTF in use". That
 * has three consequences worth stating, because they are the reason this class
 * exists rather than the setting being handed straight to the engine:
 *
 *  - The engine opens the path with the C library, so it cannot read a Kodi
 *    virtual path. A file chosen from a network share has to be copied before
 *    it can be used at all.
 *  - The copy is checked before it is kept. The engine's reader accepts a
 *    file whose structure it recognises and only warns about the rest, so a
 *    file of the wrong kind can load and render nonsense rather than failing.
 *  - Once staged, the file is local and its path is fixed, so nothing later in
 *    playback depends on a share still being mounted.
 */
class COmniphonyHrtf
{
public:
  //! \brief Why a file was refused, or Ok.
  enum class Result
  {
    Ok,
    NotFound, //!< the file could not be opened
    TooSmall, //!< far too small to hold a set of impulse responses
    NotSofa, //!< not an HDF5 container, which every SOFA file is
    Unreadable, //!< an HDF5 layout this engine's reader does not accept
    NoImpulseResponses, //!< no Data.IR: frequency domain or filter coefficients
    WrongConvention, //!< not SimpleFreeFieldHRIR
    CopyFailed, //!< could not be copied into the profile
  };

  /*!
   * \brief Check a file the way the engine's reader will.
   *
   * Screens for the things that reader requires and does not report: the
   * container format, an HDF5 revision it can parse, time-domain impulse
   * responses, and the free-field HRIR convention. A file that passes will
   * load; a file that fails would either be refused or, worse, accepted and
   * rendered as noise.
   *
   * \param path A Kodi path. Reading is sequential, so a large file on a slow
   *             share is slow to check - prefer checking the local copy.
   */
  static Result Validate(const std::string& path);

  /*!
   * \brief Copy a chosen file into the profile, replacing any previous one.
   *
   * The copy is validated before it replaces what is already there, so a bad
   * choice costs the user nothing: the previous file, if any, survives.
   *
   * \return Ok when the file is staged and in use from the next stream on.
   */
  static Result Stage(const std::string& path);

  /*!
   * \brief Stage \p path only if it is not what is already staged.
   *
   * Every stream open asks for the same file, and copying it each time would
   * fetch a measurement kept on a share once per film. A note beside the copy
   * records where it came from, so the usual answer is to do nothing.
   */
  static Result StageIfChanged(const std::string& path);

  //! \brief Discard the staged file and return to the engine's own set.
  static void Clear();

  /*!
   * \brief Absolute path of the staged file, or empty when there is none.
   *
   * Translated out of special:// because it is handed to the engine, which
   * knows nothing about Kodi paths.
   */
  static std::string StagedPath();

  /*!
   * \brief Whether a personal file is staged in the profile.
   *
   * A fact about the copy, and only about the copy: whether the engine could
   * load it is the engine's to say, and the player reports that from what the
   * engine says it is convolving with - see CDVDAudioCodecOmniphony.
   */
  static bool IsStaged();

  //! \brief Localised, user-facing explanation of a result.
  static std::string Explain(Result result);
};

} // namespace ActiveAE
