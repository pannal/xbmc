/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "DVDFactoryCodec.h"

#include "Audio/DVDAudioCodec.h"
#include "Audio/DVDAudioCodecFFmpeg.h"
#include "Audio/DVDAudioCodecPassthrough.h"
#include "DVDStreamInfo.h"
#include "Overlay/DVDOverlayCodec.h"
#include "Overlay/DVDOverlayCodecCCText.h"
#include "Overlay/DVDOverlayCodecFFmpeg.h"
#include "Overlay/DVDOverlayCodecSSA.h"
#include "Overlay/DVDOverlayCodecTX3G.h"
#include "Overlay/DVDOverlayCodecText.h"
#include "Overlay/OverlayCodecWebVTT.h"
#include "Video/AddonVideoCodec.h"
#include "Video/DVDVideoCodec.h"
#include "Video/DVDVideoCodecFFmpeg.h"
#include "ServiceBroker.h"
#include "addons/AddonProvider.h"
#include "cores/VideoPlayer/DVDCodecs/DVDCodecs.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"
#include "utils/StringUtils.h"
#include "utils/log.h"

#if defined(TARGET_LINUX) && !defined(TARGET_ANDROID)
// Built for Linux only - see Audio/CMakeLists.txt.
#include "Audio/DVDAudioCodecOmniphony.h"
#endif

#include <mutex>
#include <utility>

//------------------------------------------------------------------------------
// Video
//------------------------------------------------------------------------------

std::map<std::string, CreateHWVideoCodec> CDVDFactoryCodec::m_hwVideoCodecs;
std::map<std::string, CreateHWAudioCodec> CDVDFactoryCodec::m_hwAudioCodecs;

std::map<std::string, CreateHWAccel> CDVDFactoryCodec::m_hwAccels;

CCriticalSection videoCodecSection, audioCodecSection;

std::unique_ptr<CDVDVideoCodec> CDVDFactoryCodec::CreateVideoCodec(CDVDStreamInfo& hint,
                                                                   CProcessInfo& processInfo)
{
  std::unique_lock<CCriticalSection> lock(videoCodecSection);

  std::unique_ptr<CDVDVideoCodec> pCodec;
  CDVDCodecOptions options;

  // addon handler for this stream ?

  if (hint.externalInterfaces)
  {
    ADDON::AddonInfoPtr addonInfo;
    KODI_HANDLE parentInstance;
    hint.externalInterfaces->GetAddonInstance(ADDON::IAddonProvider::INSTANCE_VIDEOCODEC, addonInfo, parentInstance);
    if (addonInfo && parentInstance)
    {
      pCodec = std::make_unique<CAddonVideoCodec>(processInfo, addonInfo, parentInstance);
      if (pCodec->Open(hint, options))
      {
        return pCodec;
      }
    }
    return nullptr;
  }

  // platform specifig video decoders
  if (!(hint.codecOptions & CODEC_FORCE_SOFTWARE))
  {
    for (auto &codec : m_hwVideoCodecs)
    {
      pCodec = CreateVideoCodecHW(codec.first, processInfo);
      if (pCodec && pCodec->Open(hint, options))
      {
        return pCodec;
      }
    }
    if (!(hint.codecOptions & CODEC_ALLOW_FALLBACK))
      return nullptr;
  }

  pCodec = std::make_unique<CDVDVideoCodecFFmpeg>(processInfo);
  if (pCodec->Open(hint, options))
  {
    return pCodec;
  }

  return nullptr;
}

std::unique_ptr<CDVDVideoCodec> CDVDFactoryCodec::CreateVideoCodecHW(const std::string& id,
                                                                     CProcessInfo& processInfo)
{
  std::unique_lock<CCriticalSection> lock(videoCodecSection);

  auto it = m_hwVideoCodecs.find(id);
  if (it != m_hwVideoCodecs.end())
  {
    return it->second(processInfo);
  }

  return nullptr;
}

IHardwareDecoder* CDVDFactoryCodec::CreateVideoCodecHWAccel(const std::string& id,
                                                            CDVDStreamInfo& hint,
                                                            CProcessInfo& processInfo,
                                                            AVPixelFormat fmt)
{
  std::unique_lock<CCriticalSection> lock(videoCodecSection);

  auto it = m_hwAccels.find(id);
  if (it != m_hwAccels.end())
  {
    return it->second(hint, processInfo, fmt);
  }

  return nullptr;
}


void CDVDFactoryCodec::RegisterHWVideoCodec(const std::string& id, CreateHWVideoCodec createFunc)
{
  std::unique_lock<CCriticalSection> lock(videoCodecSection);

  m_hwVideoCodecs[id] = std::move(createFunc);
}

void CDVDFactoryCodec::ClearHWVideoCodecs()
{
  std::unique_lock<CCriticalSection> lock(videoCodecSection);

  m_hwVideoCodecs.clear();
}

std::vector<std::string> CDVDFactoryCodec::GetHWAccels()
{
  std::unique_lock<CCriticalSection> lock(videoCodecSection);

  std::vector<std::string> ret;
  ret.reserve(m_hwAccels.size());
  for (auto &hwaccel : m_hwAccels)
  {
    ret.push_back(hwaccel.first);
  }
  return ret;
}

void CDVDFactoryCodec::RegisterHWAccel(const std::string& id, CreateHWAccel createFunc)
{
  std::unique_lock<CCriticalSection> lock(videoCodecSection);

  m_hwAccels[id] = std::move(createFunc);
}

void CDVDFactoryCodec::ClearHWAccels()
{
  std::unique_lock<CCriticalSection> lock(videoCodecSection);

  m_hwAccels.clear();
}

//------------------------------------------------------------------------------
// Audio
//------------------------------------------------------------------------------

std::unique_ptr<CDVDAudioCodec> CDVDFactoryCodec::CreateAudioCodec(
    CDVDStreamInfo& hint,
    CProcessInfo& processInfo,
    bool allowpassthrough,
    bool allowdtshddecode,
    CAEStreamInfo::DataType ptStreamType)
{
  std::unique_ptr<CDVDAudioCodec> pCodec;
  CDVDCodecOptions options;

  if (allowpassthrough && ptStreamType != CAEStreamInfo::STREAM_TYPE_NULL)
    options.m_keys.emplace_back("ptstreamtype", StringUtils::SizeToString(ptStreamType));

  if (!allowdtshddecode)
    options.m_keys.emplace_back("allowdtshddecode", "0");

  // platform specifig audio decoders
  for (auto &codec : m_hwAudioCodecs)
  {
    pCodec = CreateAudioCodecHW(codec.first, processInfo);
    if (pCodec && pCodec->Open(hint, options))
    {
      return pCodec;
    }
  }

  // Audio rendered binaurally to headphones, decoded and rendered in a helper
  // process. Tried ahead of passthrough, but only when passthrough is not
  // actually going to happen: the two are mutually exclusive, and a listener
  // with an amplifier decoding for them is on speakers, not headphones. That
  // exclusion is enforced here rather than as a settings dependency, because
  // whether passthrough really applies depends on the stream as well as on the
  // setting - the same reason the branch below is guarded the same way.
  //
  // The stream is the whole of the test, and deliberately so. An earlier
  // version also asked IAE::HasStereoAudioChannelCount(), meaning to check that
  // a render made for two ears would not be handed to a speaker layout - but
  // that helper answers a different question than its name suggests. It is
  // false whenever the audio output is configured for more than two channels
  // AND false whenever the passthrough setting is on at all, because it exists
  // to decide whether a decoder should downmix. Either half silently turned
  // this feature off on an ordinary media-box profile: the listener switched
  // binaural on, the setting read as on, every stream decoded to the speakers,
  // and nothing anywhere said why.
  //
  // Neither half was load-bearing. The passthrough setting is settled twice
  // over already - at the settings level, where turning either of the two on
  // turns the other off (CActiveAESettings::EnforceExclusiveOutput), and here,
  // per stream, which is the answer that actually matters because a track this
  // sink cannot pass through is one the listener hears decoded whatever the
  // setting says. And the channel count decides where stereo lands, not whether
  // this may produce it: a stereo stream on a multichannel output plays through
  // the front pair, as every stereo stream on that output already does.
  //
  // So the rule is the one the setting itself promises: switch it on and, on
  // anything not actually being passed through, the render is binaural.
  //
  // Which leaves one state the settings-level exclusion cannot reach, because
  // it only runs when a setting is changed: a profile that already had both on
  // when the exclusion was added. That profile now gets binaural rather than
  // speakers, and that is the better of the two guesses - passthrough is on by
  // default and binaural is not, so the setting the listener actually went and
  // switched on is this one. Turning it off restores the amplifier.
  //
  // Open() also refuses unless the helper is installed beside kodi.bin, so on
  // an image without it this costs one access() and falls through.
#if defined(TARGET_LINUX) && !defined(TARGET_ANDROID)
  const bool passthroughWins = allowpassthrough && ptStreamType != CAEStreamInfo::STREAM_TYPE_NULL;
  if (!passthroughWins && CServiceBroker::GetSettingsComponent() &&
      CServiceBroker::GetSettingsComponent()->GetSettings()->GetBool(
          CSettings::SETTING_AUDIOOUTPUT_OMNIPHONY))
  {
    auto omni = std::make_unique<CDVDAudioCodecOmniphony>(processInfo);
    if (omni->Open(hint, options))
    {
      return omni;
    }
    // Open() logs why it refused; this says which decision led to asking, so a
    // log that ends in "ff-aac" can be read for whether binaural was skipped
    // before it was tried or gave up after.
    CLog::Log(LOGDEBUG, "CDVDFactoryCodec: binaural is on but the codec did not open this stream");
  }
#endif

  // we don't use passthrough if "sync playback to display" is enabled
  if (allowpassthrough && ptStreamType != CAEStreamInfo::STREAM_TYPE_NULL)
  {
    pCodec = std::make_unique<CDVDAudioCodecPassthrough>(processInfo, ptStreamType);
    if (pCodec->Open(hint, options))
    {
      return pCodec;
    }
  }

  pCodec = std::make_unique<CDVDAudioCodecFFmpeg>(processInfo);
  if (pCodec->Open(hint, options))
  {
    return pCodec;
  }

  return nullptr;
}

void CDVDFactoryCodec::RegisterHWAudioCodec(const std::string& id, CreateHWAudioCodec createFunc)
{
  std::unique_lock<CCriticalSection> lock(audioCodecSection);

  m_hwAudioCodecs[id] = std::move(createFunc);
}

void CDVDFactoryCodec::ClearHWAudioCodecs()
{
  std::unique_lock<CCriticalSection> lock(audioCodecSection);

  m_hwAudioCodecs.clear();
}

std::unique_ptr<CDVDAudioCodec> CDVDFactoryCodec::CreateAudioCodecHW(const std::string& id,
                                                                     CProcessInfo& processInfo)
{
  std::unique_lock<CCriticalSection> lock(audioCodecSection);

  auto it = m_hwAudioCodecs.find(id);
  if (it != m_hwAudioCodecs.end())
  {
    return it->second(processInfo);
  }

  return nullptr;
}

//------------------------------------------------------------------------------
// Overlay
//------------------------------------------------------------------------------

std::unique_ptr<CDVDOverlayCodec> CDVDFactoryCodec::CreateOverlayCodec(CDVDStreamInfo& hint)
{
  std::unique_ptr<CDVDOverlayCodec> pCodec;
  CDVDCodecOptions options;

  switch (hint.codec)
  {
    case AV_CODEC_ID_TEXT:
    {
      if (hint.source == STREAM_SOURCE_VIDEOMUX)
        pCodec = std::make_unique<CDVDOverlayCodecCCText>();
      else
        pCodec = std::make_unique<CDVDOverlayCodecText>();
      break;
    }
    case AV_CODEC_ID_SUBRIP:
      pCodec = std::make_unique<CDVDOverlayCodecText>();
      break;

    case AV_CODEC_ID_SSA:
    case AV_CODEC_ID_ASS:
      pCodec = std::make_unique<CDVDOverlayCodecSSA>();
      break;

    case AV_CODEC_ID_MOV_TEXT:
      pCodec = std::make_unique<CDVDOverlayCodecTX3G>();
      break;

    case AV_CODEC_ID_WEBVTT:
      pCodec = std::make_unique<COverlayCodecWebVTT>();
      break;

    default:
      pCodec = std::make_unique<CDVDOverlayCodecFFmpeg>();
      break;
  }

  if (pCodec->Open(hint, options))
    return pCodec;

  return nullptr;
}

