/*
 *  Copyright (C) 2006-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <assert.h>
#include <atomic>
#include <memory>
#include <stdexcept>
#include <utility>
#include <vector>

enum DVDOverlayType
{
  DVDOVERLAY_TYPE_NONE    = -1,
  DVDOVERLAY_TYPE_SPU     = 1,
  DVDOVERLAY_TYPE_TEXT    = 2,
  DVDOVERLAY_TYPE_IMAGE   = 3,
  DVDOVERLAY_TYPE_SSA     = 4,
  DVDOVERLAY_TYPE_GROUP   = 5,
};

class CDVDOverlay : public std::enable_shared_from_this<CDVDOverlay>
{
public:
  explicit CDVDOverlay(DVDOverlayType type)
  {
    m_type = type;

    iPTSStartTime = 0LL;
    iPTSStopTime = 0LL;
    bForced = false;
    replace = false;
    m_enableTextAlign = false;
    m_overlayContainerFlushable = true;
    m_setForcedMargins = false;
  }

  CDVDOverlay(const CDVDOverlay& src) : std::enable_shared_from_this<CDVDOverlay>(src)
  {
    m_type        = src.m_type;
    iPTSStartTime = src.iPTSStartTime;
    iPTSStopTime  = src.iPTSStopTime;
    bForced       = src.bForced;
    replace = src.replace;
    m_enableTextAlign = src.m_enableTextAlign;
    m_overlayContainerFlushable = src.m_overlayContainerFlushable;
    m_setForcedMargins = src.m_setForcedMargins;
    m_discMenuOverlay = src.m_discMenuOverlay;
  }

  // Content versions are replaced by construction/publication, not assigned
  // between builders (which would also copy another builder's publication).
  CDVDOverlay& operator=(const CDVDOverlay&) = delete;

  virtual ~CDVDOverlay() = default;

  bool IsOverlayType(DVDOverlayType type) const { return (m_type == type); }

  /**
   * return a copy to VideoPlayerSubtitle in order to have hw resources cleared
   * after rendering
   */
  virtual std::shared_ptr<CDVDOverlay> Clone() { return shared_from_this(); }

  // Builders are published under their producer/container lock. Unchanged
  // content reuses this version; after an in-place content edit the owner must
  // explicitly republish. Copies of builders start without a cached version.
  std::shared_ptr<const CDVDOverlay> GetPublishedRenderContent() const
  {
    if (m_isRenderContent)
      return shared_from_this();
    if (!m_renderContent)
      CacheRenderContent();
    return m_renderContent;
  }

  void PublishRenderContent() { CacheRenderContent(); }

  /*
   * \brief Enable the use of text alignment (left/center/right).
   */
  virtual void SetTextAlignEnabled(bool enable)
  {
    throw std::logic_error("EnableTextAlign method not implemented.");
  }

  /*
   * \brief Return true if the text alignment (left/center/right) is enabled otherwise false.
   */
  bool IsTextAlignEnabled() const { return m_enableTextAlign; }

  /*
   * \brief Allow/Disallow the overlay container to flush the overlay.
   */
  void SetOverlayContainerFlushable(bool isFlushable) { m_overlayContainerFlushable = isFlushable; }

  /*
   * \brief Return true when the overlay container can flush the overlay on flush events.
   */
  bool IsOverlayContainerFlushable() const { return m_overlayContainerFlushable; }

  /*
   * \brief Specify if the margins are handled by the subtitle codec/parser.
   */
  void SetForcedMargins(bool setForcedMargins) { m_setForcedMargins = setForcedMargins; }

  /*
   * \brief Return true if the margins are handled by the subtitle codec/parser.
   */
  bool IsForcedMargins() const { return m_setForcedMargins; }

  void SetDiscMenuOverlay(bool discMenu) { m_discMenuOverlay = discMenu; }
  bool IsDiscMenuOverlay() const { return m_discMenuOverlay; }

  double iPTSStartTime;
  double iPTSStopTime;
  bool bForced; // display, no matter what
  bool replace; // replace by next nomatter what stoptime it has

protected:
  virtual std::shared_ptr<CDVDOverlay> CreateRenderContent() const
  {
    return std::make_shared<CDVDOverlay>(*this);
  }

  DVDOverlayType m_type;
  bool m_enableTextAlign;
  bool m_overlayContainerFlushable;
  bool m_setForcedMargins;
  bool m_discMenuOverlay{false};

private:
  void CacheRenderContent() const
  {
    auto content = CreateRenderContent();
    content->m_isRenderContent = true;
    m_renderContent = std::move(content);
  }

  bool m_isRenderContent{false};
  mutable std::shared_ptr<const CDVDOverlay> m_renderContent;
};

using VecOverlays = std::vector<std::shared_ptr<CDVDOverlay>>;
using VecRenderOverlays = std::vector<std::shared_ptr<const CDVDOverlay>>;

class CDVDOverlayGroup : public CDVDOverlay
{
public:
  ~CDVDOverlayGroup() override = default;

  CDVDOverlayGroup()
    : CDVDOverlay(DVDOVERLAY_TYPE_GROUP)
  {
  }

  CDVDOverlayGroup(const CDVDOverlayGroup& src) : CDVDOverlay(src), m_overlays(src.m_overlays) {}
  VecRenderOverlays m_overlays;

protected:
  std::shared_ptr<CDVDOverlay> CreateRenderContent() const override
  {
    auto content = std::make_shared<CDVDOverlayGroup>(*this);
    for (auto& overlay : content->m_overlays)
      overlay = overlay->GetPublishedRenderContent();
    return content;
  }
};
