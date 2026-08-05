/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "DVDFileInfo.h"

#include "DVDInputStreams/DVDInputStream.h"
#include "DVDStreamInfo.h"
#include "FileItem.h"
#include "ServiceBroker.h"
#include "filesystem/StackDirectory.h"
#include "guilib/Texture.h"
#include "pictures/Picture.h"
#include "settings/AdvancedSettings.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"
#include "utils/MemUtils.h"
#include "utils/URIUtils.h"
#include "utils/log.h"
#include "video/VideoInfoTag.h"
#ifdef HAVE_LIBBLURAY
#include "DVDInputStreams/DVDInputStreamBluray.h"
#endif
#include "DVDInputStreams/DVDFactoryInputStream.h"
#include "DVDDemuxers/DVDDemux.h"
#include "DVDDemuxers/DVDDemuxUtils.h"
#include "DVDDemuxers/DVDFactoryDemuxer.h"
#include "DVDCodecs/DVDFactoryCodec.h"
#include "DVDCodecs/Video/DVDVideoCodec.h"
#include "DVDCodecs/Video/DVDVideoCodecFFmpeg.h"
#include "DVDDemuxers/DVDDemuxVobsub.h"
#include "Process/ProcessInfo.h"

#include "filesystem/File.h"
#include "cores/FFmpeg.h"
#include "TextureCache.h"
#include "Util.h"
#include "utils/LangCodeExpander.h"

#include <cstdlib>
#include <memory>
#include <string>
#include <vector>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libswscale/swscale.h>
}

bool CDVDFileInfo::GetFileDuration(const std::string &path, int& duration)
{
  std::unique_ptr<CDVDDemux> demux;

  CFileItem item(path, false);
  auto input = CDVDFactoryInputStream::CreateInputStream(NULL, item);
  if (!input)
    return false;

  if (!input->Open())
    return false;

  demux.reset(CDVDFactoryDemuxer::CreateDemuxer(input, true));
  if (!demux)
    return false;

  duration = demux->GetStreamLength();
  if (duration > 0)
    return true;
  else
    return false;
}

int DegreeToOrientation(int degrees)
{
  switch(degrees)
  {
    case 90:
      return 5;
    case 180:
      return 2;
    case 270:
      return 7;
    default:
      return 0;
  }
}

std::unique_ptr<CTexture> CDVDFileInfo::ExtractThumbToTexture(const CFileItem& fileItem,
                                                              int chapterNumber)
{
  if (!CanExtract(fileItem))
    return {};

  const std::string redactPath = CURL::GetRedacted(fileItem.GetPath());
  auto start = std::chrono::steady_clock::now();

  CFileItem item(fileItem);
  item.SetMimeTypeForInternetFile();
  auto pInputStream = CDVDFactoryInputStream::CreateInputStream(NULL, item);
  if (!pInputStream)
  {
    CLog::Log(LOGERROR, "InputStream: Error creating stream for {}", redactPath);
    return {};
  }

  if (!pInputStream->Open())
  {
    CLog::Log(LOGERROR, "InputStream: Error opening, {}", redactPath);
    return {};
  }

  std::unique_ptr<CDVDDemux> demuxer{CDVDFactoryDemuxer::CreateDemuxer(pInputStream, true)};
  if (!demuxer)
  {
    CLog::LogF(LOGERROR, "Error creating demuxer");
    return {};
  }

  int nVideoStream = -1;
  int64_t demuxerId = -1;
  for (CDemuxStream* pStream : demuxer->GetStreams())
  {
    if (pStream)
    {
      // ignore if it's a picture attachment (e.g. jpeg artwork)
      if (pStream->type == STREAM_VIDEO && !(pStream->flags & AV_DISPOSITION_ATTACHED_PIC))
      {
        nVideoStream = pStream->uniqueId;
        demuxerId = pStream->demuxerId;
      }
      else
        demuxer->EnableStream(pStream->demuxerId, pStream->uniqueId, false);
    }
  }

  int packetsTried = 0;

  std::unique_ptr<CTexture> result{};
  if (nVideoStream != -1)
  {
    std::unique_ptr<CProcessInfo> pProcessInfo(CProcessInfo::CreateInstance());
    std::vector<AVPixelFormat> pixFmts;
    pixFmts.push_back(AV_PIX_FMT_YUV420P);
    pProcessInfo->SetPixFormats(pixFmts);

    CDVDStreamInfo hint(*demuxer->GetStream(demuxerId, nVideoStream), true);
    hint.codecOptions = CODEC_FORCE_SOFTWARE;

    std::unique_ptr<CDVDVideoCodec> pVideoCodec =
        CDVDFactoryCodec::CreateVideoCodec(hint, *pProcessInfo);

    if (pVideoCodec)
    {
      int nTotalLen = demuxer->GetStreamLength();

      bool seekToChapter = chapterNumber > 0 && demuxer->GetChapterCount() > 0;
      int64_t nSeekTo =
          seekToChapter ? demuxer->GetChapterPos(chapterNumber) * 1000 : nTotalLen / 3;

      // Seek to chapter @ 0 not likley to be a very useful result, use 5 sec instead.
      if (seekToChapter && (nSeekTo == 0)) nSeekTo = 5000;

      CLog::LogF(LOGDEBUG, "seeking to pos {}ms (total: {}ms) in {}", nSeekTo, nTotalLen,
                 redactPath);

      if (demuxer->SeekTime(static_cast<double>(nSeekTo), true))
      {
        CDVDVideoCodec::VCReturn iDecoderState = CDVDVideoCodec::VC_NONE;
        VideoPicture picture;
        picture.Reset();

        // num streams * 160 frames, should get a valid frame, if not abort.
        int abort_index = demuxer->GetNrOfStreams() * 160;
        do
        {
          DemuxPacket* pPacket = demuxer->Read();
          packetsTried++;

          if (!pPacket)
            break;

          if (pPacket->iStreamId != nVideoStream || pPacket->isELPackage)
          {
            CDVDDemuxUtils::FreeDemuxPacket(pPacket);
            continue;
          }

          pVideoCodec->AddData(*pPacket);
          CDVDDemuxUtils::FreeDemuxPacket(pPacket);

          iDecoderState = CDVDVideoCodec::VC_NONE;
          while (iDecoderState == CDVDVideoCodec::VC_NONE)
          {
            picture.Reset();
            iDecoderState = pVideoCodec->GetPicture(&picture);
          }

          if (iDecoderState == CDVDVideoCodec::VC_PICTURE)
          {
            if (!(picture.iFlags & DVP_FLAG_DROPPED))
              break;
          }

        } while (abort_index--);

        if (iDecoderState == CDVDVideoCodec::VC_PICTURE && !(picture.iFlags & DVP_FLAG_DROPPED))
        {
          unsigned int nWidth =
              std::min(picture.iDisplayWidth,
                       CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->m_imageRes);
          double aspect = (double)picture.iDisplayWidth / (double)picture.iDisplayHeight;
          if (hint.forced_aspect && hint.aspect != 0)
            aspect = hint.aspect;
          unsigned int nHeight = (unsigned int)((double)nWidth / aspect);

          result = CTexture::CreateTexture(nWidth, nHeight);
          result->SetAlpha(false);
          struct SwsContext* context =
              sws_getContext(picture.iWidth, picture.iHeight, AV_PIX_FMT_YUV420P, nWidth, nHeight,
                             AV_PIX_FMT_BGRA, SWS_FAST_BILINEAR, NULL, NULL, NULL);

          if (context)
          {
            uint8_t* planes[YuvImage::MAX_PLANES];
            int stride[YuvImage::MAX_PLANES];
            picture.videoBuffer->GetPlanes(planes);
            picture.videoBuffer->GetStrides(stride);
            uint8_t* src[4] = {planes[0], planes[1], planes[2], 0};
            int srcStride[] = {stride[0], stride[1], stride[2], 0};
            uint8_t* dst[] = {result->GetPixels(), 0, 0, 0};
            int dstStride[] = {static_cast<int>(result->GetPitch()), 0, 0, 0};
            result->SetOrientation(DegreeToOrientation(hint.orientation));
            sws_scale(context, src, srcStride, 0, picture.iHeight, dst, dstStride);
            sws_freeContext(context);
          }
        }
        else
        {
          CLog::LogF(LOGDEBUG, "decode failed in {} after {} packets.", redactPath, packetsTried);
        }
        picture.Reset();
      }
    }
  }

  auto end = std::chrono::steady_clock::now();
  auto duration = std::chrono::duration_cast<std::chrono::milliseconds>(end - start);
  CLog::LogF(LOGDEBUG, "measured {} ms to extract thumb from file <{}> in {} packets. ",
             duration.count(), redactPath, packetsTried);

  return result;
}

bool CDVDFileInfo::CanExtract(const CFileItem& fileItem)
{
  if (fileItem.m_bIsFolder)
    return false;

  if (fileItem.IsLiveTV() ||
      // Due to a pvr addon api design flaw (no support for multiple concurrent streams
      // per addon instance), pvr recording thumbnail extraction does not work (reliably).
      URIUtils::IsPVRRecording(fileItem.GetDynPath()) ||
      // plugin path not fully resolved
      URIUtils::IsPlugin(fileItem.GetDynPath()) ||
      URIUtils::IsUPnP(fileItem.GetPath()) ||
      (fileItem.IsInternetStream() && // For internet protocol streams - if it is HTTP or FTP and on lan then ok to extract, otherwise not ok.
       (!((URIUtils::IsFTP(fileItem.GetPath()) || URIUtils::IsHTTP(fileItem.GetPath())) && URIUtils::IsOnLAN(fileItem.GetPath())))) ||
      fileItem.IsDiscStub() ||
      fileItem.IsPlayList())
    return false;

  // mostly can't extract from discs and files from discs.
  if (URIUtils::IsBluray(fileItem.GetPath()) || fileItem.IsBDFile() || fileItem.IsDVD() ||
      fileItem.IsDiscImage() || fileItem.IsDVDFile(false, true))
    return false;

  // For HTTP/FTP we only allow extraction when on a LAN
  if (URIUtils::IsRemote(fileItem.GetPath()) && !URIUtils::IsOnLAN(fileItem.GetPath()) &&
      (URIUtils::IsFTP(fileItem.GetPath()) || URIUtils::IsHTTP(fileItem.GetPath())))
    return false;

  return true;
}

/**
 * \brief Open the item pointed to by pItem and extract streamdetails
 * \return true if the stream details have changed
 */
bool CDVDFileInfo::GetFileStreamDetails(CFileItem *pItem)
{
  if (!pItem)
    return false;

  if (!CanExtract(*pItem))
    return false;

  std::string strFileNameAndPath;
  if (pItem->HasVideoInfoTag())
    strFileNameAndPath = pItem->GetVideoInfoTag()->m_strFileNameAndPath;

  if (strFileNameAndPath.empty())
    strFileNameAndPath = pItem->GetDynPath();

  std::string playablePath = strFileNameAndPath;
  if (URIUtils::IsStack(playablePath))
    playablePath = XFILE::CStackDirectory::GetFirstStackedFile(playablePath);

  CFileItem item(playablePath, false);
  item.SetMimeTypeForInternetFile();
  auto pInputStream = CDVDFactoryInputStream::CreateInputStream(NULL, item);
  if (!pInputStream)
    return false;

  if (pInputStream->IsStreamType(DVDSTREAM_TYPE_PVRMANAGER))
  {
    return false;
  }

  if (pInputStream->IsStreamType(DVDSTREAM_TYPE_DVD) || !pInputStream->Open())
  {
    return false;
  }

  CDVDDemux *pDemuxer = CDVDFactoryDemuxer::CreateDemuxer(pInputStream, true);
  if (pDemuxer)
  {
    bool retVal = DemuxerToStreamDetails(pInputStream, pDemuxer, pItem->GetVideoInfoTag()->m_streamDetails, strFileNameAndPath);
    ProcessExternalSubtitles(pItem);
    delete pDemuxer;
    return retVal;
  }
  else
  {
    return false;
  }
}

bool CDVDFileInfo::DemuxerToStreamDetails(const std::shared_ptr<CDVDInputStream>& pInputStream,
                                          CDVDDemux* pDemuxer,
                                          const std::vector<CStreamDetailSubtitle>& subs,
                                          CStreamDetails& details)
{
  bool result = DemuxerToStreamDetails(pInputStream, pDemuxer, details);
  for (unsigned int i = 0; i < subs.size(); i++)
  {
    CStreamDetailSubtitle* sub = new CStreamDetailSubtitle();
    sub->m_strLanguage = subs[i].m_strLanguage;
    details.AddStream(sub);
    result = true;
  }
  return result;
}

/* Decodes a frame to pick up the details that are only visible in the elementary stream:
   the Dolby Vision enhancement layer type (MEL/FEL) and HDR10+ dynamic metadata.
   Returns true if a frame was decoded. */
static bool GetHdrDetailsFromFrame(CDemuxStreamVideo* stream,
                                   CDVDDemux* demuxer,
                                   CStreamDetailVideo& vDetail)
{
  std::unique_ptr<CProcessInfo> pProcessInfo(CProcessInfo::CreateInstance());
  std::vector<AVPixelFormat> pixFmts;
  pixFmts.push_back(AV_PIX_FMT_YUV420P);
  pProcessInfo->SetPixFormats(pixFmts);

  CDVDStreamInfo hint(*stream, true);
  hint.codecOptions = CODEC_FORCE_SOFTWARE;

  std::unique_ptr<CDVDVideoCodec> pVideoCodec =
      CDVDFactoryCodec::CreateVideoCodec(hint, *pProcessInfo);
  if (!pVideoCodec)
  {
    CLog::LogF(LOGERROR, "unable to create video codec to retrieve HDR details");
    return false;
  }

  int nTotalLen = demuxer->GetStreamLength();
  int nSeekTo = nTotalLen / 5;

  CLog::LogF(LOGDEBUG, "seeking to pos {}ms (total: {}ms) for HDR details", nSeekTo, nTotalLen);

  if (!demuxer->SeekTime(static_cast<double>(nSeekTo), true))
  {
    CLog::LogF(LOGERROR, "unable to seek to pos {}ms", nSeekTo);
    return false;
  }

  CDVDVideoCodec::VCReturn iDecoderState = CDVDVideoCodec::VC_NONE;
  VideoPicture picture;
  picture.Reset();

  // num streams * 160 frames, should get a valid frame, if not abort.
  int abort_index = demuxer->GetNrOfStreams() * 160;
  do
  {
    DemuxPacket* pPacket = demuxer->Read();

    if (!pPacket)
      break;

    // the enhancement layer of a dual track stream cannot be fed to the base layer decoder
    if (pPacket->iStreamId != stream->uniqueId || pPacket->isELPackage)
    {
      CDVDDemuxUtils::FreeDemuxPacket(pPacket);
      continue;
    }

    pVideoCodec->AddData(*pPacket);
    CDVDDemuxUtils::FreeDemuxPacket(pPacket);

    iDecoderState = CDVDVideoCodec::VC_NONE;
    int maxTries = 50;
    while (maxTries-- > 0 && iDecoderState == CDVDVideoCodec::VC_NONE)
    {
      picture.Reset();
      iDecoderState = pVideoCodec->GetPicture(&picture);
    }

    if (iDecoderState == CDVDVideoCodec::VC_PICTURE)
    {
      if (!(picture.iFlags & DVP_FLAG_DROPPED))
        break;
    }

  } while (abort_index--);

  if (iDecoderState != CDVDVideoCodec::VC_PICTURE || (picture.iFlags & DVP_FLAG_DROPPED))
  {
    CLog::LogF(LOGDEBUG, "decoder couldn't find a valid picture for HDR details");
    picture.Reset();
    return false;
  }

  // the frame resolved which kind of enhancement layer it is, so refine "EL"
  if (!picture.strDVELType.empty() && !vDetail.m_strDvProfile.empty())
  {
    const size_t el = vDetail.m_strDvProfile.rfind(" EL");
    if (el != std::string::npos && el == vDetail.m_strDvProfile.size() - 3)
      vDetail.m_strDvProfile.erase(el);
    vDetail.m_strDvProfile += " " + picture.strDVELType;
  }

  if (picture.hasHdr10Plus)
  {
    if (vDetail.m_strHdrType == "dolbyvision")
      // dynamic metadata actually present in the stream outranks the base layer type
      vDetail.m_strHdrTypeAlt = CStreamDetails::HdrTypeToString(StreamHdrType::HDR_TYPE_HDR10PLUS);
    else
      vDetail.m_strHdrType = CStreamDetails::HdrTypeToString(StreamHdrType::HDR_TYPE_HDR10PLUS);
  }

  picture.Reset();
  return true;
}

/* returns true if details have been added */
bool CDVDFileInfo::DemuxerToStreamDetails(const std::shared_ptr<CDVDInputStream>& pInputStream,
                                          CDVDDemux* pDemux,
                                          CStreamDetails& details,
                                          const std::string& path)
{
  bool retVal = false;
  details.Reset();

  const CURL pathToUrl(path);
  for (CDemuxStream* stream : pDemux->GetStreams())
  {
    if (stream->type == STREAM_VIDEO && !(stream->flags & AV_DISPOSITION_ATTACHED_PIC))
    {
      CStreamDetailVideo *p = new CStreamDetailVideo();
      CDemuxStreamVideo* vstream = static_cast<CDemuxStreamVideo*>(stream);
      p->m_iWidth = vstream->iWidth;
      p->m_iHeight = vstream->iHeight;
      p->m_fAspect = static_cast<float>(vstream->fAspect);
      if (p->m_fAspect == 0.0f && p->m_iHeight > 0)
        p->m_fAspect = (float)p->m_iWidth / p->m_iHeight;
      p->m_strCodec = pDemux->GetStreamCodecName(stream->demuxerId, stream->uniqueId);
      p->m_iDuration = pDemux->GetStreamLength();
      p->m_strStereoMode = vstream->stereo_mode;
      p->m_strLanguage = vstream->language;
      p->m_strHdrType = CStreamDetails::HdrTypeToString(vstream->hdr_type);

      if (vstream->hdr_type == StreamHdrType::HDR_TYPE_DOLBYVISION && vstream->dovi.dv_profile != 0)
      {
        // profile.compatibility, e.g. "5.0", "7.6", "8.1" (HDR10 base), "8.4" (HLG base)
        p->m_strDvProfile =
            std::to_string(static_cast<int>(vstream->dovi.dv_profile)) + "." +
            std::to_string(static_cast<int>(vstream->dovi.dv_bl_signal_compatibility_id));

        // an enhancement layer is stated by the configuration record; whether it is a
        // minimal or a full one only shows in the elementary stream, so mark it plainly
        // here and let the frame probe below refine "EL" into "MEL" or "FEL"
        if (vstream->dovi.el_present_flag)
          p->m_strDvProfile += " EL";

        if (vstream->dovi.dv_bl_signal_compatibility_id == 4)
          p->m_strHdrTypeAlt = CStreamDetails::HdrTypeToString(StreamHdrType::HDR_TYPE_HLG);
      }

      // The enhancement layer type and HDR10+ are only visible in the elementary stream, so
      // finding either costs a frame decode. Dolby Vision is always examined: profile 5 is
      // single layer IPT-PQ and has nothing to find, but for the rest MEL/FEL is not
      // obtainable any other way. Plain HDR10 is examined as well, but can be turned off -
      // it is the common case and HDR10+ is comparatively rare, so on a large library over
      // network storage that is a lot of decoding for comparatively few hits.
      const bool probeDolbyVision = vstream->hdr_type == StreamHdrType::HDR_TYPE_DOLBYVISION &&
                                    vstream->dovi.dv_profile != 5;
      const bool probeHdr10 =
          vstream->hdr_type == StreamHdrType::HDR_TYPE_HDR10 &&
          CServiceBroker::GetSettingsComponent()->GetSettings()->GetBool(
              CSettings::SETTING_MYVIDEOS_EXTRACTHDR10PLUS);

      if (probeDolbyVision || probeHdr10)
      {
        if (!GetHdrDetailsFromFrame(vstream, pDemux, *p))
          CLog::LogF(LOGDEBUG, "no additional HDR details found");
      }

      // stack handling
      if (URIUtils::IsStack(path))
      {
        CFileItemList files;
        XFILE::CStackDirectory stack;
        stack.GetDirectory(pathToUrl, files);

        // skip first path as we already know the duration
        for (int i = 1; i < files.Size(); i++)
        {
           int duration = 0;
           if (CDVDFileInfo::GetFileDuration(files[i]->GetDynPath(), duration))
             p->m_iDuration = p->m_iDuration + duration;
        }
      }

      // finally, calculate seconds
      if (p->m_iDuration > 0)
        p->m_iDuration = p->m_iDuration / 1000;

      details.AddStream(p);
      retVal = true;
    }

    else if (stream->type == STREAM_AUDIO)
    {
      CStreamDetailAudio *p = new CStreamDetailAudio();
      p->m_iChannels = static_cast<CDemuxStreamAudio*>(stream)->iChannels;
      p->m_strLanguage = stream->language;
      p->m_strCodec = pDemux->GetStreamCodecName(stream->demuxerId, stream->uniqueId);
      details.AddStream(p);
      retVal = true;
    }

    else if (stream->type == STREAM_SUBTITLE)
    {
      CStreamDetailSubtitle *p = new CStreamDetailSubtitle();
      p->m_strLanguage = stream->language;
      details.AddStream(p);
      retVal = true;
    }
  }  /* for iStream */

  details.DetermineBestStreams();
#ifdef HAVE_LIBBLURAY
  // correct bluray runtime. we need the duration from the input stream, not the demuxer.
  if (pInputStream->IsStreamType(DVDSTREAM_TYPE_BLURAY))
  {
    if (std::static_pointer_cast<CDVDInputStreamBluray>(pInputStream)->GetTotalTime() > 0)
    {
      const CStreamDetailVideo* dVideo = static_cast<const CStreamDetailVideo*>(details.GetNthStream(CStreamDetail::VIDEO, 0));
      CStreamDetailVideo* detailVideo = const_cast<CStreamDetailVideo*>(dVideo);
      if (detailVideo)
        detailVideo->m_iDuration = std::static_pointer_cast<CDVDInputStreamBluray>(pInputStream)->GetTotalTime() / 1000;
    }
  }
#endif
  return retVal;
}

void CDVDFileInfo::ProcessExternalSubtitles(CFileItem* item)
{
  std::vector<std::string> externalSubtitles;
  const std::string videoPath = item->GetDynPath();

  CUtil::ScanForExternalSubtitles(videoPath, externalSubtitles);

  for (const auto& externalSubtitle : externalSubtitles)
  {
    // if vobsub subtitle:
    if (URIUtils::GetExtension(externalSubtitle) == ".idx")
    {
      std::string subFile;
      if (CUtil::FindVobSubPair(externalSubtitles, externalSubtitle, subFile))
        AddExternalSubtitleToDetails(videoPath, item->GetVideoInfoTag()->m_streamDetails,
                                     externalSubtitle, subFile);
    }
    else
    {
      if (!CUtil::IsVobSub(externalSubtitles, externalSubtitle))
      {
        AddExternalSubtitleToDetails(videoPath, item->GetVideoInfoTag()->m_streamDetails,
                                     externalSubtitle);
      }
    }
  }
}

bool CDVDFileInfo::AddExternalSubtitleToDetails(const std::string &path, CStreamDetails &details, const std::string& filename, const std::string& subfilename)
{
  std::string ext = URIUtils::GetExtension(filename);
  std::string vobsubfile = subfilename;
  if(ext == ".idx")
  {
    if (vobsubfile.empty())
      vobsubfile = URIUtils::ReplaceExtension(filename, ".sub");

    CDVDDemuxVobsub v;
    if (!v.Open(filename, STREAM_SOURCE_NONE, vobsubfile))
      return false;

    for(CDemuxStream* stream : v.GetStreams())
    {
      CStreamDetailSubtitle *dsub = new CStreamDetailSubtitle();
      std::string lang = stream->language;
      dsub->m_strLanguage = g_LangCodeExpander.ConvertToISO6392B(lang);
      details.AddStream(dsub);
    }
    return true;
  }
  if(ext == ".sub")
  {
    std::string strReplace(URIUtils::ReplaceExtension(filename,".idx"));
    if (XFILE::CFile::Exists(strReplace))
      return false;
  }

  CStreamDetailSubtitle *dsub = new CStreamDetailSubtitle();
  ExternalStreamInfo info = CUtil::GetExternalStreamDetailsFromFilename(path, filename);
  dsub->m_strLanguage = g_LangCodeExpander.ConvertToISO6392B(info.language);
  details.AddStream(dsub);

  return true;
}

