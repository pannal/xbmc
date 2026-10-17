/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <string>

/*!
 * \brief The listener's config.yaml over Kodi's render.yaml: the config a
 * stream plays, or why it plays render.yaml instead.
 *
 * Kodi writes render.yaml whole from its settings; the listener edits
 * config.yaml beside it, a partial config whose values win. Merging the two
 * and deciding what a listener may set are Kodi's: the engine is handed one
 * complete config, which it checks strictly before playing it
 * (orender_check_config, in the helper), and render.yaml plays when either
 * side refuses it.
 *
 * The rules, which the template config.yaml spells out for its owner:
 *  - `null` inherits: a key set to null is as if absent, so the template,
 *    every key null, changes nothing. A mapping merges key by key; anything
 *    else replaces what Kodi wrote, a list whole.
 *  - It applies whole or not at all, with the reason.
 *  - What Kodi owns is refused rather than ignored: the decoder bridge, the
 *    input and output, OSC, head tracking (Kodi has none), the HRIR grid
 *    cache, an output other than binaural, and channels handed back to the
 *    host - and keys that do nothing in Kodi, such as dialogue_gain_db.
 *    Whether a key exists, its type and its range are the engine's check.
 *  - Relative file paths start in config.yaml's folder.
 *
 * The YAML read is the block subset the template is written in: indented
 * mappings, plain and quoted scalars, comments, one-line { } and [ ]
 * collections, and "- " lists. Anything else - anchors, tags, multi-line
 * scalars, more than one document - is refused with the line it is on, as
 * is a tab in the indentation.
 */
struct OmniphonyListenerConfig
{
  enum class Status
  {
    None, //!< config.yaml sets nothing: render.yaml plays
    Applied, //!< \ref yaml plays
    Rejected, //!< render.yaml plays; \ref reason says why
  };
  Status status{Status::None};
  std::string reason;
  //! The merged config, YAML, when applied.
  std::string yaml;
  //! The room file config.yaml names, resolved, when the merge renders a
  //! room; empty otherwise.
  std::string room;
  //! config.yaml chooses the binaural response itself - an HRTF source other
  //! than a room, or a room file of its own - so the settings' room is not
  //! what plays while it applies.
  bool ownResponse{false};
  //! The merge renders cascaded on virtual speakers (not a room).
  bool cascade{false};
  //! config.yaml sets current_layout: Kodi's own layout must not win over it.
  bool layoutSet{false};
  //! config.yaml sets decode_thread: the engine follows the config (live)
  //! in place of Kodi's per-codec choice.
  bool decodeThreadSet{false};
};

//! The largest config.yaml read, bytes.
constexpr size_t OMNI_LISTENER_CONFIG_MAX = 256 * 1024;

/*!
 * \brief Merge \p listener, config.yaml's text, over \p base, render.yaml's.
 *
 * \param listenerDir the folder config.yaml's relative paths start in
 *
 * Reads no file: whether a room it names is one the engine should be given
 * directly is the caller's to check (\ref OmniphonyListenerConfig::room).
 */
OmniphonyListenerConfig OmniphonyMergeListenerConfig(const std::string& base,
                                                     const std::string& listener,
                                                     const std::string& listenerDir);
