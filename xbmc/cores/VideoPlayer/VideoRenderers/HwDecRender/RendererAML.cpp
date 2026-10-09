/*
 *  Copyright (C) 2007-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "RendererAML.h"
#include "settings/DisplaySettings.h"
#include "utils/StringUtils.h"

#include "cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.h"
#include "cores/VideoPlayer/DVDCodecs/Video/AMLCodec.h"
#include "utils/log.h"
#include "utils/AMLUtils.h"
#include "utils/ScreenshotAML.h"
#include "settings/MediaSettings.h"
#include "cores/VideoPlayer/VideoRenderers/RenderCapture.h"
#include "cores/VideoPlayer/VideoRenderers/RenderFactory.h"
#include "cores/VideoPlayer/VideoRenderers/RenderFlags.h"
#include "settings/AdvancedSettings.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"
#include "windowing/GraphicContext.h"
#include "windowing/WinSystem.h"

CRendererAML::CRendererAML()
 : m_prevVPts(-1)
 , m_bConfigured(false)
{
  CLog::Log(LOGINFO, "Constructing CRendererAML");
}

CRendererAML::~CRendererAML()
{
  Reset();
}

CBaseRenderer* CRendererAML::Create(CVideoBuffer *buffer)
{
  if (buffer && dynamic_cast<CAMLVideoBuffer*>(buffer))
    return new CRendererAML();
  return nullptr;
}

bool CRendererAML::Register()
{
  VIDEOPLAYER::CRendererFactory::RegisterRenderer("amlogic", CRendererAML::Create);
  return true;
}

bool CRendererAML::Configure(const VideoPicture &picture, float fps, unsigned int orientation)
{
  if (auto* buffer = dynamic_cast<CAMLVideoBuffer*>(picture.videoBuffer))
  {
    m_pollCodec = buffer->Codec();
    m_pollEpoch = buffer->OperationEpoch();
  }
  m_sourceWidth = picture.iWidth;
  m_sourceHeight = picture.iHeight;
  m_renderOrientation = orientation;

  m_iFlags = GetFlagsChromaPosition(picture.chroma_position) |
             GetFlagsColorMatrix(picture.color_space, picture.iWidth, picture.iHeight) |
             GetFlagsColorPrimaries(picture.color_primaries) |
             GetFlagsStereoMode(picture.stereoMode);

  // Calculate the input frame aspect ratio.
  CalculateFrameAspectRatio(picture.iDisplayWidth, picture.iDisplayHeight);
  SetViewMode(m_videoSettings.m_ViewMode);
  ManageRenderArea();

  m_bConfigured = true;

  return true;
}

CRenderInfo CRendererAML::GetRenderInfo()
{
  CRenderInfo info;
  info.max_buffer_size = m_numRenderBuffers;
  info.opaque_pointer = (void *)this;
  return info;
}

bool CRendererAML::RenderCapture(int index, CRenderCapture* capture)
{
  if (!capture)
    return false;
  auto* buffer = index >= 0 && index < m_numRenderBuffers
                     ? dynamic_cast<CAMLVideoBuffer*>(m_buffers[index].videoBuffer)
                     : nullptr;
  auto codec = buffer ? buffer->Codec() : m_pollCodec;
  const auto epoch = buffer ? buffer->OperationEpoch() : m_pollEpoch;
  auto permit = [&]() -> CAMLSession::Permit {
    if (!codec)
      return {};
    auto presentation = codec->AcquirePresentation(epoch);
    if (presentation)
      return presentation;
    return codec->AcquireMainControl(epoch, true);
  }();
  if (!permit)
  {
    capture->SetState(CAPTURESTATE_FAILED);
    return false;
  }
  capture->BeginRender();
  if (!CScreenshotAML::CaptureVideoFrame(static_cast<unsigned char*>(capture->GetRenderBuffer()),
                                       capture->GetWidth(), capture->GetHeight(), false))
  {
    capture->SetState(CAPTURESTATE_FAILED);
    return false;
  }
  capture->EndRender();
  return true;
}

void CRendererAML::AddVideoPicture(const VideoPicture &picture, int index)
{
  ReleaseBuffer(index);

  BUFFER &buf(m_buffers[index]);
  if (picture.videoBuffer)
  {
    buf.videoBuffer = picture.videoBuffer;
    buf.videoBuffer->Acquire();
    if (auto* buffer = dynamic_cast<CAMLVideoBuffer*>(buf.videoBuffer))
    {
      m_pollCodec = buffer->Codec();
      m_pollEpoch = buffer->OperationEpoch();
    }
  }
}

void CRendererAML::ReleaseBuffer(int idx)
{
  BUFFER &buf(m_buffers[idx]);
  if (buf.videoBuffer)
  {
    CAMLVideoBuffer *amli(dynamic_cast<CAMLVideoBuffer*>(buf.videoBuffer));
    if (amli)
    {
      amli->Drop();
      amli->Release();
    }
    buf.videoBuffer = nullptr;
  }
}

bool CRendererAML::Supports(ERENDERFEATURE feature) const
{
  if (feature == RENDERFEATURE_ZOOM ||
      feature == RENDERFEATURE_CONTRAST ||
      feature == RENDERFEATURE_BRIGHTNESS ||
      feature == RENDERFEATURE_NONLINSTRETCH ||
      feature == RENDERFEATURE_VERTICAL_SHIFT ||
      feature == RENDERFEATURE_STRETCH ||
      feature == RENDERFEATURE_PIXEL_RATIO ||
      feature == RENDERFEATURE_ROTATION)
    return true;

  return false;
}

void CRendererAML::Reset()
{
  std::array<int, 2> reset_arr[m_numRenderBuffers];
  m_prevVPts = -1;

  for (int i = 0 ; i < m_numRenderBuffers ; ++i)
  {
    reset_arr[i][0] = i;

    if (m_buffers[i].videoBuffer)
      reset_arr[i][1] = dynamic_cast<CAMLVideoBuffer *>(m_buffers[i].videoBuffer)->m_bufferIndex;
    else
      reset_arr[i][1] = 0;
  }

  std::sort(std::begin(reset_arr), std::end(reset_arr),
    [](const std::array<int, 2>& u, const std::array<int, 2>& v)
    {
      return u[1] < v[1];
    });

  for (int i = 0; i < m_numRenderBuffers; ++i)
  {
    if (m_buffers[reset_arr[i][0]].videoBuffer)
    {
      m_buffers[reset_arr[i][0]].videoBuffer->Release();
      m_buffers[reset_arr[i][0]].videoBuffer = nullptr;
    }
  }

  CServiceBroker::GetWinSystem()->GetGfxContext().SetTransferPQ(false);
}

bool CRendererAML::Flush(bool saveBuffers)
{
  if (!saveBuffers)
    Reset();
  return saveBuffers;
};

void CRendererAML::ReleasePresentationReferences()
{
  // The imported presenter frames own these references now. ReleaseBuffer
  // would consume their outstanding native submission as a drop.
  for (auto& buffer : m_buffers)
  {
    if (buffer.videoBuffer)
    {
      buffer.videoBuffer->Release();
      buffer.videoBuffer = nullptr;
    }
  }
}

void CRendererAML::RenderUpdate(int index, int index2, bool clear, unsigned int flags, unsigned int alpha)
{
  RenderUpdateVideo(index, index2, clear, flags, alpha);
}

bool CRendererAML::RenderUpdateVideo(int index, int index2, bool clear, unsigned int flags,
                                    unsigned int alpha)
{
  auto* buffer = dynamic_cast<CAMLVideoBuffer*>(m_buffers[index].videoBuffer);
  auto permit = buffer ? buffer->AcquirePresentation() :
      (m_pollCodec ? m_pollCodec->AcquirePresentation(m_pollEpoch) : CAMLSession::Permit{});
  if (!permit)
  {
    // Display/control fences can reopen, but reset/close invalidates the old
    // frame permanently. Let an obsolete selection retire so a new epoch can
    // reach selection/configuration, even when no explicit discard was sent.
    const auto codec = buffer ? buffer->Codec() : m_pollCodec;
    return !codec || codec->IsOperationInvalidated(buffer ? buffer->OperationEpoch() : m_pollEpoch);
  }
  const PreparedVideoGeometry geometry = PrepareVideoLayer();
  if (buffer)
  {
    // A handoff may retain a QBUF-consumed frame whose main control was
    // still pending. Resume its control before polling without another QBUF.
    if (m_resumeControl && buffer->WasSubmitted())
      buffer->ApplyGeometry(permit, geometry.source, geometry.destination);
    else
      buffer->Commit(permit, geometry.source, geometry.destination, m_prevVPts);
    m_resumeControl = false;
    buffer->Poll(permit);
  }
  else
    m_pollCodec->PollFrame(permit);
  return true;
}

CRendererAML::PreparedVideoGeometry CRendererAML::PrepareVideoLayer()
{
  ManageRenderArea();
  return {m_sourceRect, m_destRect};
}

uint64_t CRendererAML::PrepareIndependentControl(CRect& source, CRect& destination)
{
  const auto geometry = PrepareVideoLayer();
  source = geometry.source;
  destination = geometry.destination;
  auto& gfx = CServiceBroker::GetWinSystem()->GetGfxContext();
  const auto info = gfx.GetResInfo();
  const std::string key = StringUtils::Format(
      "{} {} {} {} {} {} {} {} {} {} {} {} {} {} {} {} {}",
      source.x1, source.y1, source.x2, source.y2,
      destination.x1, destination.y1, destination.x2, destination.y2,
      m_videoSettings.m_ViewMode, static_cast<int>(gfx.GetStereoMode()),
      static_cast<int>(gfx.GetStereoView()), static_cast<int>(gfx.GetVideoResolution()),
      info.iWidth, info.iHeight, info.iScreenWidth, info.iScreenHeight,
      CDisplaySettings::GetInstance().IsNonLinearStretched());
  if (key != m_controlKey)
  {
    m_controlKey = key;
    ++m_controlGeneration;
  }
  return m_controlGeneration;
}
