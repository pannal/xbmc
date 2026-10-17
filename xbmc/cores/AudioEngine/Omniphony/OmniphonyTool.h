/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "threads/CriticalSection.h"
#include "threads/IRunnable.h"

#include <cstdint>
#include <string>
#include <vector>

namespace ActiveAE
{

/*!
 * \brief What a SOFA file or a prepared room holds, as the engine reads it.
 *
 * The fields of the line `omniphony-helper --describe` prints after
 * "described", and `--prepare-brir` after its word - see orender_sofa_describe
 * and orender_brir_prepare in the engine's ABI.md. Of a SOFA file only the
 * shape and the geometry are read to answer, so a room set of hundreds of MB
 * is described in about the time it takes to hand it over; a prepared room, a
 * few MB, is read whole.
 */
struct OmniphonySofaInfo
{
  bool hrtf{false}; //!< the HRTF stage takes it: one direction per measurement
  bool room{false}; //!< the room stage takes it: loudspeakers measured in a room
  bool prepared{false}; //!< a prepared room rather than a SOFA file
  std::string conventions; //!< the SOFA convention, e.g. MultiSpeakerBRIR
  unsigned int measurements{0};
  unsigned int receivers{0};
  unsigned int emitters{0}; //!< loudspeakers sounding in each measurement
  unsigned int samples{0}; //!< length of each response
  unsigned int rate{0};
  unsigned int orientations{0}; //!< head orientations measured, for a room
  unsigned int speakers{0}; //!< loudspeakers, for a room
  std::string names; //!< their labels, comma separated
  //! The identity of the SOFA file - for a prepared room, of the file it was
  //! prepared from: what a file chosen again is matched against the room or
  //! set kept for it by. `<length>-<hash>`, the hash FNV-1a of its bytes.
  std::string source;
  std::string reason; //!< why the stage that does not take it refuses it, in English
};

/*!
 * \brief Read the line the helper prints after "described ".
 *
 * Space-separated key=value pairs, except reason=, which runs to the end of
 * the line because it is a sentence. Unknown keys are skipped, so a newer
 * engine that says more is still understood.
 *
 * \return false when neither stage is named, which no description lacks.
 */
bool OmniphonyParseSofaInfo(const std::string& line, OmniphonySofaInfo& info);

/*!
 * \brief Split a one-off command's answer into its word, reason and detail.
 *
 * "prepared <summary>", "described <fields>" or "failed reason=<word>
 * <detail>": \p word is the first word, \p reason the failure's word (empty
 * otherwise) and \p detail the rest of the line.
 */
void OmniphonyParseToolAnswer(const std::string& line,
                              std::string& word,
                              std::string& reason,
                              std::string& detail);

//! \brief One localised sentence saying what \p info describes.
std::string OmniphonyDescribeSofaInfo(const OmniphonySofaInfo& info);

/*!
 * \brief Run one of the helper's one-off commands on a file Kodi can read.
 *
 * The engine cannot open a Kodi path, and the helper is the only 64-bit
 * process there is to load it in, so the file goes to the helper the way the
 * stream does: through its stdin, from wherever Kodi reads it, a share
 * included. The helper answers with one line on stdout and exits.
 *
 * An IRunnable so that CGUIDialogBusy can run it with a cancel button: a room
 * set is hundreds of MB, and reading one from a share takes long enough to
 * want one. Cancelling kills the helper, which writes nothing in place until
 * it has finished - see orender_brir_prepare.
 *
 * Only where the codec is built (see DVDCodecs/Audio/CMakeLists.txt);
 * elsewhere every run reports Unavailable.
 */
class COmniphonyTool : public IRunnable
{
public:
  enum class Status
  {
    Done, //!< the helper answered - see Word(), Reason() and Detail()
    Unavailable, //!< no helper here, or it could not be started
    Unreadable, //!< the input could not be opened, or holds nothing
    Cancelled,
    //! The helper ran and left without an answer: the engine stopped on the
    //! file, out of memory or worse. Not something a later try does better.
    Failed,
  };

  /*!
   * \param args  what follows the helper's name; the input's size is appended,
   *              last - see Arguments
   * \param input any Kodi path, whose bytes are the helper's stdin; empty for
   *              a command on a local file the helper opens itself, which is
   *              handed neither bytes nor a size
   */
  COmniphonyTool(std::vector<std::string> args, std::string input);

  void Run() override;
  void Cancel() override;

  //! \brief Run here, or behind a cancellable busy dialog when \p interactive,
  //! which only the GUI thread may ask for.
  Status Execute(bool interactive);

  //! \brief What follows the helper's name for an input of \p size bytes (0
  //! for none): the command's own arguments, then the size, last, where every
  //! command with an input takes it.
  std::vector<std::string> Arguments(int64_t size) const;

  Status GetStatus() const { return m_status; }
  const std::string& Word() const { return m_word; }
  const std::string& Reason() const { return m_reason; }
  const std::string& Detail() const { return m_detail; }

  //! \brief The helper the codec runs, and the engine it loads.
  static std::string HelperPath();
  static std::string EnginePath();

  /*!
   * \brief Describe the file at \p path.
   *
   * \param failure the helper's reason word when it answered "failed" -
   *        "unusable" for bytes that are neither a SOFA file nor a prepared
   *        room this engine reads, "memory", "input", "engine" or "internal"
   *        (see the helper's --describe) - or "internal" for an answer it
   *        cannot read; the helper's words are then in \p info's reason
   * \return Done with \p info filled, or Done with \p failure set, or why the
   *         helper could not be asked
   */
  static Status Describe(const std::string& path,
                         bool interactive,
                         OmniphonySofaInfo& info,
                         std::string& failure);

private:
  std::vector<std::string> m_args;
  std::string m_input;

  CCriticalSection m_lock;
  int m_pid{-1}; //!< the running helper, for Cancel
  bool m_cancel{false};

  Status m_status{Status::Unavailable};
  std::string m_word;
  std::string m_reason;
  std::string m_detail;
};

} // namespace ActiveAE
