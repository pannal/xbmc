/*
 *  Copyright (C) 2007-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "cores/VideoPlayer/VideoRenderers/BaseRenderer.h"

#include <cstdint>
#include <memory>

class CAMLCodec;

class CRendererAML : public CBaseRenderer
{
public:
  CRendererAML();
  virtual ~CRendererAML();

  // Registration
  static CBaseRenderer* Create(CVideoBuffer *buffer);
  static bool Register();

  virtual bool RenderCapture(int index, CRenderCapture* capture) override;
  virtual void AddVideoPicture(const VideoPicture &picture, int index) override;
  virtual void ReleaseBuffer(int idx) override;
  virtual bool Configure(const VideoPicture &picture, float fps, unsigned int orientation) override;
  virtual bool IsConfigured() override { return m_bConfigured; };
  virtual bool ConfigChanged(const VideoPicture &picture) { return false; };
  virtual CRenderInfo GetRenderInfo() override;
  virtual void UnInit() override {};
  virtual void Update() override {};
  virtual void RenderUpdate(int index, int index2, bool clear, unsigned int flags, unsigned int alpha) override;
  bool RenderUpdateVideo(int index, int index2, bool clear, unsigned int flags,
                         unsigned int alpha) override;
  virtual bool SupportsMultiPassRendering()override { return false; };
  virtual bool Flush(bool saveBuffers) override;

  // Player functions
  virtual bool IsGuiLayer() override { return false; };

  // Feature support
  virtual bool Supports(ESCALINGMETHOD method) const override { return false; };
  virtual bool Supports(ERENDERFEATURE feature) const override;

  std::shared_ptr<CAMLCodec> PresenterCodec() const { return m_pollCodec; }
  CVideoBuffer* PresentationBuffer(int index) const { return m_buffers[index].videoBuffer; }
  int PreviousPts() const { return m_prevVPts; }
  void ReleasePresentationReferences();
  void SetPresentationEpoch(uint64_t epoch) { m_pollEpoch = epoch; }
  void ResumeIndependentPresentation(int pts)
  {
    m_prevVPts = pts;
    m_resumeControl = true;
  }
  uint64_t PrepareIndependentControl(CRect& source, CRect& destination);

private:
  bool m_resumeControl{false};
  uint64_t m_controlGeneration{0};
  std::string m_controlKey;
  // Submission rectangles only. Buffer/codec and final display conversion
  // remain live under the existing synchronous ownership and generation gates.
  struct PreparedVideoGeometry
  {
    CRect source;
    CRect destination;
  };

  // Called synchronously, in order, by RenderUpdate on its calling thread.
  PreparedVideoGeometry PrepareVideoLayer();

  void Reset();

  static const int m_numRenderBuffers = NUM_BUFFERS;

  struct BUFFER
  {
    BUFFER() : videoBuffer(nullptr) {};
    CVideoBuffer *videoBuffer;
    int duration;
  } m_buffers[m_numRenderBuffers];

  // Empty-slot redraws still poll, but only the original admitted codec epoch.
  std::shared_ptr<CAMLCodec> m_pollCodec;
  uint64_t m_pollEpoch{0};
  int m_prevVPts;
  bool m_bConfigured;
};
