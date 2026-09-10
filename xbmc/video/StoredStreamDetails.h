/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <string>
#include <utility>
#include <vector>

/*!
 \file StoredStreamDetails.h
 \brief What a rewrite of a file's stream details keeps from the rows already stored.

 CVideoDatabase::SetStreamDetailsForFileId() replaces every row of a file. These helpers decide,
 from the rows read just before, what survives the rewrite. They work on plain values rather than
 on a database so the decisions can be tested on their own.
 */

namespace VIDEO
{
//! \brief An audio codec that another build stores as a plain name with a profile beside it.
struct SplitAudioCodec
{
  const char* plain; //!< the codec that build stores, e.g. "truehd"
  const char* profile; //!< the profile it stores beside it, e.g. "Dolby Atmos"
  const char* extended; //!< the one name this build uses for both, e.g. "truehd_atmos"
};

/*! \brief Every audio codec another build splits into a plain name and a profile.

 Each extended name is one StreamUtils::GetCodecName() produces itself.
 */
const std::vector<SplitAudioCodec>& GetSplitAudioCodecs();

/*! \brief The plain name another build stores for an audio codec this build names in full.
 \return the codec unchanged when no other build splits it
 */
std::string GetPlainAudioCodec(const std::string& codec);

//! \brief An audio stream row: the columns this build writes, and whatever else it carries.
struct AudioStreamRow
{
  std::string codec;
  int channels{0};
  std::string language;
  //! Columns this build does not write, each with its value as an SQL literal. Only columns
  //! that hold a value are listed.
  std::vector<std::pair<std::string, std::string>> otherColumns;
};

/*! \brief The audio rows to write in place of those stored.

 Every stream is written as it is now described, so a codec the file has been re-identified as
 always reaches the database. The columns another build keeps on a stored row travel with it when
 the stored rows still describe the same tracks in the same order, and only onto a stream whose
 codec is the one stored or the fuller name for it: a stream now named more plainly than before,
 or differently, is a different stream, and what was recorded about the old one does not apply.

 \param stored the audio rows of the file, in stored order
 \param streams the audio streams to write, in stream order
 \return one row per stream, in stream order
 */
std::vector<AudioStreamRow> PlanAudioRows(const std::vector<AudioStreamRow>& stored,
                                          const std::vector<AudioStreamRow>& streams);

//! \brief A video stream row as stored.
struct StoredVideoStream
{
  std::string codec;
  int width{0};
  int height{0};
  std::string hdrType;
  std::string hdrTypeAlt;
  std::string dvProfile;
};

//! \brief The HDR fields of a video stream.
struct HdrFields
{
  std::string hdrType;
  std::string hdrTypeAlt;
  std::string dvProfile;
};

/*! \brief Complete the HDR fields of a caller that cannot read the elementary stream.

 The player reports the primary HDR type from the container alone. It has no alternate type and
 no Dolby Vision profile, and it reports plain HDR10 where the scan, having probed a frame, found
 HDR10+. Those come from the stored row describing the same video - the same codec at the same
 size, of the same HDR type once the scan's refinement is set aside. Several rows can fit, which
 only matters when they disagree; then nothing is taken.

 \param stored the video rows of the file
 \param codec the codec of the stream being written
 \param width the width of the stream being written
 \param height the height of the stream being written
 \param reported the HDR fields as the caller reported them
 \return the reported fields, completed from the stored row where one fits
 */
HdrFields CompleteHdrFields(const std::vector<StoredVideoStream>& stored,
                            const std::string& codec,
                            int width,
                            int height,
                            HdrFields reported);

//! \brief Whether a column name can be written into a statement as it stands.
bool IsPlainColumnName(const std::string& name);
} // namespace VIDEO
