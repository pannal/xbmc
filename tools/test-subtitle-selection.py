#!/usr/bin/env python3
"""Host regression checks for subtitle selection, without a Kodi/CE build.

Compile the production predicates, stream sort and OpenDefaultStreams subtitle
block, runtime selection/visibility handlers and subtitle resync logic with
settings, demux, stream-opening and queue stubs. Actual playback, language
conversion and forced bitmap cues are outside this fixture.
Requires Python 3 and g++; temporary build files are removed automatically.
"""

import pathlib
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text()


def declaration(source, signature, include_return_type=False):
    start = source.index(signature)
    if include_return_type:
        start = source.rfind("\n", 0, start) + 1
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def main():
    player = read("xbmc/cores/VideoPlayer/VideoPlayer.cpp")
    player_h = read("xbmc/cores/VideoPlayer/VideoPlayer.h")
    download = declaration(read("xbmc/cores/IPlayer.h"), "struct SubtitleDownload") + ";\n"
    messages = read("xbmc/cores/VideoPlayer/DVDMessage.h")
    demux = read("xbmc/cores/VideoPlayer/DVDDemuxers/DVDDemux.h")
    info = read("xbmc/cores/VideoPlayer/Interface/StreamInfo.h")
    flags = declaration(info, "enum StreamFlags") + ";\n"
    sources = declaration(demux, "enum StreamSource") + ";\n"
    types = declaration(demux, "enum StreamType") + ";\n"
    source_mask = next(line for line in demux.splitlines()
                       if line.startswith("#define STREAM_SOURCE_MASK"))
    macro = player[player.index("#define PREDICATE_RETURN"):
                   player.index("class PredicateSubtitleFilter")]
    predicates = "\n".join(declaration(player, "class " + name) + ";"
                           for name in ("PredicateSubtitleFilter", "PredicateSubtitlePriority"))
    sort = declaration(player_h, "template<typename Compare>")
    identity = declaration(read("xbmc/cores/VideoSettings.h"), "struct SubtitleIdentity")
    identify = declaration(player, "SelectionStream::GetSubtitleIdentity(", include_return_type=True)
    setter = declaration(read("xbmc/cores/VideoSettings.cpp"),
                         "void CVideoSettingsLocked::SetSubtitleStream(")
    catalog = "\n".join(declaration(player, f"CSelectionStreams::{name}(", include_return_type=True)
                        .replace("CSelectionStreams::", "").replace("m_Streams", "streams")
                        for name in ("TypeIndexOf", "CountType", "Update", "Clear", "ReindexSubtitles",
                                     "OrderDownloadedSubtitles", "ResolveSubtitle"))
    startup = declaration(player, "void CVideoPlayer::OpenDefaultStreams(")
    startup = startup[startup.index("  // enable  or disable subtitles"):
                      startup.index("  // open teletext stream")]
    handlers = " else ".join(declaration(player, f"if (pMsg->IsType(CDVDMsg::{message}))")
                             for message in ("PLAYER_SET_AUDIOSTREAM", "PLAYER_SET_SUBTITLESTREAM",
                                             "PLAYER_SET_SUBTITLESTREAM_VISIBLE", "SUBTITLE_ADDFILE"))
    runtime = "\n".join(declaration(player, f"CVideoPlayer::{name}(", include_return_type=True)
                        .replace("CVideoPlayer::", "")
                        for name in ("SetEnableStream", "ReopenSubtitleStream", "SetSubtitle",
                                     "AdaptForcedSubtitles", "OrderDownloadedSubtitle"))

    source = r'''
#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <cstdint>
#include <deque>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <vector>
''' + flags + sources + types + source_mask + '\n' + identity + r''';

struct SelectionStream {
  int type_index = 0, id = 0, demuxerId = 0, source = STREAM_SOURCE_DEMUX;
  StreamFlags flags = FLAG_NONE;
  std::string language, filename;
  StreamType type = STREAM_SUBTITLE;
  bool isFileSidecar = false;
  SubtitleIdentity GetSubtitleIdentity() const;
};
struct CSettings {
  static constexpr int SETTING_LOCALE_SUBTITLELANGUAGE = 0;
  static constexpr int SETTING_ACCESSIBILITY_SUBHEARING = 1;
  std::string preference = "original";
  bool hearing = false;
  std::string GetString(int) const { return preference; }
  bool GetBool(int) const { return hearing; }
};
struct SettingsComponent {
  std::shared_ptr<CSettings> settings = std::make_shared<CSettings>();
  auto GetSettings() { return settings; }
} component;
struct CServiceBroker {
  static auto GetSettingsComponent() { return &component; }
};
struct StringUtils {
  static bool EqualsNoCase(std::string a, std::string b) {
    auto lower = [](unsigned char c) { return std::tolower(c); };
    std::transform(a.begin(), a.end(), a.begin(), lower);
    std::transform(b.begin(), b.end(), b.begin(), lower);
    return a == b;
  }
};
// Only the file-style URI schemes used by this fixture have a meaningful leaf.
// Keep query-like suffixes intact so the production identity builder must reject them.
struct URIUtils {
  static bool IsURL(const std::string& path) { return path.find("://") != std::string::npos; }
  static bool IsProtocol(const std::string& path, const std::string& scheme) {
    return path.compare(0, scheme.size() + 3, scheme + "://") == 0;
  }
  static bool IsSmb(const std::string& path) { return IsProtocol(path, "smb"); }
  static bool IsNfs(const std::string& path) { return IsProtocol(path, "nfs"); }
  static std::string GetFileName(const std::string& path) {
    return path.substr(path.find_last_of("/\\") + 1);
  }
};
struct LangInfo {
  std::string audio, subtitle;
  std::string GetAudioLanguage(bool fallback) const {
    return audio.empty() && fallback ? "eng" : audio;
  }
  std::string GetSubtitleLanguage(bool fallback) const {
    return subtitle.empty() && fallback ? "eng" : subtitle;
  }
} g_langInfo;
struct LangCodeExpander {
  // Tests supply canonical ISO codes; identical empty codes also compare equal.
  bool CompareISO639Codes(const std::string& a, const std::string& b) const {
    return StringUtils::EqualsNoCase(a, b);
  }
} g_LangCodeExpander;
''' + identify + '\n' + macro + predicates + r'''

struct SelectionStreams {
  std::vector<SelectionStream> streams;
  SelectionStream audio;
  std::vector<SelectionStream> audioTracks;
  std::vector<SelectionStream> Get(StreamType) { return streams; }
  SelectionStream invalid;
  SelectionStream& Get(StreamType type, int index) {
    if (type == STREAM_AUDIO) return audioTracks.empty() ? audio : audioTracks.at(index);
    if (index >= 0 && static_cast<size_t>(index) < streams.size()) return streams[index];
    invalid.source = STREAM_SOURCE_NONE;
    return invalid;
  }
''' + sort + catalog + r'''
};
struct VideoSettings {
  bool m_SubtitleOn = true;
  int m_SubtitleStream = -1;
  std::optional<SubtitleIdentity> m_subtitleIdentity = std::nullopt;
};
using CCriticalSection = std::recursive_mutex;
struct CVideoSettingsLocked {
  CCriticalSection& m_critSection;
  VideoSettings& m_videoSettings;
  void SetSubtitleStream(int, std::optional<SubtitleIdentity>);
};
''' + setter + r'''
struct ProcessInfo {
  VideoSettings settings;
  CCriticalSection lock;
  const VideoSettings& GetVideoSettings() const { return settings; }
  CVideoSettingsLocked GetVideoSettingsLocked() { return {lock, settings}; }
};
constexpr int DVDSTREAM_TYPE_DVD = 1, DVDSTREAM_TYPE_PVRMANAGER = 2;
struct InputStream {
  virtual ~InputStream() = default;
  bool IsStreamType(int) const { return false; }
};
struct CDVDInputStreamNavigator : InputStream {
  bool SetActiveSubtitleStream(int) { return false; }
  bool SetActiveAudioStream(int) { return false; }
};
struct CCurrentStream : SelectionStream { bool demuxDisabled = false; int syncState = 0; };
struct Demux {
  std::string GetFileName() const { return {}; }
  bool supportsDisable = true, delivering = false;
  bool EnableStream(int64_t, int, bool enable) {
    if (supportsDisable) delivering = enable;
    return supportsDisable;
  }
};
struct CDVDMsg {
  enum Type { PLAYER_SET_SUBTITLESTREAM, PLAYER_SET_SUBTITLESTREAM_VISIBLE, PLAYER_SEEK,
              PLAYER_SEEK_CHAPTER, PLAYER_SET_AUDIOSTREAM, SUBTITLE_ADDFILE, NONE };
  CDVDMsg(Type t) : type(t) {}
  Type type;
  bool IsType(Type t) const { return type == t; }
};
struct CDVDMsgPlayerSetAudioStream : CDVDMsg {
  int index;
  int GetStreamId() const { return index; }
};
template<typename T> struct CDVDMsgType : CDVDMsg {
  CDVDMsgType(Type type, const T& value) : CDVDMsg(type), m_value(value) {}
  T m_value;
};
''' + download + declaration(messages, "class CDVDMsgSubtitleAddFile") + ";\n" + declaration(
        messages, "class CDVDMsgPlayerSetSubtitleStream") + r''';
struct CDVDMsgBool : CDVDMsg { bool m_value; };
struct CDVDMsgPlayerSeek {
  struct CMode { double time; bool backward, accurate, trickplay, sync; };
  explicit CDVDMsgPlayerSeek(CMode) {}
};
struct Queue {
  CDVDMsg::Type pending = CDVDMsg::NONE;
  int seeks = 0;
  std::deque<std::shared_ptr<CDVDMsg>> messages;
  void Put(std::shared_ptr<CDVDMsg> message) { messages.push_back(message); }
  void PutIfNoMessages(std::shared_ptr<CDVDMsgPlayerSeek>, std::initializer_list<CDVDMsg::Type> types) {
    if (std::find(types.begin(), types.end(), pending) == types.end()) ++seeks;
  }
};
constexpr double DVD_NOPTS_VALUE = -1;
double DVD_TIME_TO_MSEC(double pts) { return pts / 1000; }
struct IDVDStreamPlayer { enum { SYNC_STARTING = 1 }; };
struct Player {
  std::shared_ptr<ProcessInfo> m_processInfo = std::make_shared<ProcessInfo>();
  std::shared_ptr<InputStream> m_pInputStream = std::make_shared<InputStream>();
  struct { std::string state; } m_playerOptions;
  SelectionStreams m_SelectionStreams;
  struct Content {
    CCriticalSection m_section;
    SelectionStreams m_selectionStreams;
    int m_subtitleIndex = -1;
  } m_content;
  void UpdateContent() { m_content.m_selectionStreams = m_SelectionStreams; }
  std::vector<std::string> sidecars;
  struct { std::string GetDynPath() const { return "episode.mkv"; } } m_item;
  std::map<int64_t, std::shared_ptr<Demux>> m_subtitleDemuxerMap;
  CCurrentStream m_CurrentSubtitle;
  std::shared_ptr<Demux> m_pDemuxer = std::make_shared<Demux>();
  Queue m_messenger;
  CCurrentStream m_CurrentVideo, m_CurrentAudio;
  bool m_audioRecoveryPending = false;
  struct { double dts = DVD_NOPTS_VALUE, time_offset = 0; } m_State;
  struct { int iSelectedSPUStream = -1, iSelectedAudioStream = -1; } m_dvd;
  int opens = 0;
  int publishedSubtitle = -1;
  void UpdateContentState() {
    publishedSubtitle = m_content.m_subtitleIndex = m_SelectionStreams.TypeIndexOf(
        STREAM_SUBTITLE, m_CurrentSubtitle.source, m_CurrentSubtitle.demuxerId, m_CurrentSubtitle.id);
  }
  int GetUpdatedTime() { return 0; }
  std::vector<int> failedStreams;
  bool visible = false;
  int GetAudioStream() { return m_CurrentAudio.id; }
  int GetSubtitle() { return publishedSubtitle; }
  enum SubtitleChange { FLAG_STREAMINFO_CHANGE = 1 };
  void NotifySubtitleUpdate(int) {}
  int AddSubtitleFile(const std::string& path, const std::string&, bool isFileSidecar) {
    if (path == "episode.nl.srt" || path == "episode.de.srt") {
      const bool dutch = path == "episode.nl.srt";
      SelectionStream sidecar;
      sidecar.source = STREAM_SOURCE_TEXT + (dutch ? 1 : 2);
      sidecar.language = dutch ? "dut" : "ger";
      sidecar.filename = path;
      sidecar.isFileSidecar = isFileSidecar;
      m_SelectionStreams.Update(sidecar);
      UpdateContent();
      return sidecar.type_index;
    } else if (path != "example.en.srt") {
      return -1;
    }
    UpdateContent();
    return static_cast<int>(m_SelectionStreams.streams.size()) - 1;
  }
  void Drain() {
    while (!m_messenger.messages.empty()) {
      auto message = m_messenger.messages.front();
      m_messenger.messages.pop_front();
      Handle(message);
    }
  }
  bool OpenStream(CCurrentStream& current, int64_t demuxerId, int id, int source) {
    if (std::find(failedStreams.begin(), failedStreams.end(), id) != failedStreams.end())
      return false;
    ++opens;
    current.id = id; current.demuxerId = demuxerId; current.source = source;
    current.demuxDisabled = false;
    m_pDemuxer->delivering = true;
    UpdateContentState();
    return true;
  }
  void CloseStream(CCurrentStream& current, bool) {
    current = {};
    current.source = STREAM_SOURCE_NONE;
    current.id = -1;
    m_pDemuxer->delivering = false;
  }
  void SetSubtitleVisibleInternal(bool value) { visible = value; }
''' + runtime + r'''
  void Handle(const std::shared_ptr<CDVDMsg>& pMsg) {
    do {
''' + handlers + r'''
    } while (false);
  }
  void QueueVisibility(bool value) {
    m_messenger.Put(std::make_shared<CDVDMsgBool>(
        CDVDMsgBool{{CDVDMsg::PLAYER_SET_SUBTITLESTREAM_VISIBLE}, value}));
  }
  void QueueExternalSrt(const std::string& path = "example.en.srt", bool downloaded = false) {
    m_messenger.Put(std::make_shared<CDVDMsgSubtitleAddFile>(path,
        downloaded ? SubtitleDownload{m_item.GetDynPath(), sidecars} : SubtitleDownload{}));
  }
  void Show(bool value) {
    QueueVisibility(value);
    Drain();
  }
  void Select(int index) {
    SetSubtitle(index);
    Show(true); // xbmc.Player.setSubtitleStream also requests visibility.
  }
  void Start() {
    bool valid = false;
''' + startup + r'''
  }
};

int checks = 0, failures = 0;
void expect(bool value, const std::string& label) {
  ++checks;
  if (!value) { ++failures; std::cerr << "FAIL: " << label << '\n'; }
}
SelectionStream sub(int id, std::string language, int flags = FLAG_NONE,
                    int source = STREAM_SOURCE_DEMUX) {
  SelectionStream result;
  result.type_index = result.id = id; result.language = language;
  result.flags = static_cast<StreamFlags>(flags); result.source = source;
  return result;
}
void configure(const std::string& preference, const std::string& audio, bool hearing) {
  component.settings->preference = preference;
  component.settings->hearing = hearing;
  g_langInfo.subtitle = preference == "eng" || preference == "jpn" ? preference : "";
  g_langInfo.audio = audio;
}
void check(const std::string& label, const std::string& preference, const std::string& played,
           bool on, std::vector<SelectionStream> streams, int selected, bool visible,
           bool hearing = false, int saved = -1, const std::string& audio = "",
           std::vector<int> failed = {}) {
  configure(preference, audio, hearing);
  // Track order must not override a unique eligible/default/forced winner.
  for (int reversed = 0; reversed < 2; ++reversed) {
    Player p;
    p.m_processInfo->settings = {on, saved};
    p.m_SelectionStreams.streams = streams;
    p.m_SelectionStreams.audio.language = played;
    p.failedStreams = failed;
    p.Start();
    expect((selected < 0 || p.m_CurrentSubtitle.id == selected) && p.visible == visible,
           label + " (selected=" + std::to_string(p.m_CurrentSubtitle.id) +
           ", visible=" + std::to_string(p.visible) + ")");
    expect(!visible || p.m_pDemuxer->delivering, label + " visible stream enabled");
    std::reverse(streams.begin(), streams.end());
  }
}
void checkOrdering() {
  std::vector<SelectionStream> streams;
  std::vector<int> flagSets{FLAG_NONE};
  for (int flag : {FLAG_DEFAULT, FLAG_ORIGINAL, FLAG_FORCED, FLAG_HEARING_IMPAIRED}) {
    const size_t count = flagSets.size();
    for (size_t i = 0; i < count; ++i)
      flagSets.push_back(flagSets[i] | flag);
  }
  for (const auto& lang : {"", "und", "eng", "jpn"})
    for (int flags : flagSets)
      for (int source : {STREAM_SOURCE_DEMUX, STREAM_SOURCE_TEXT, STREAM_SOURCE_VIDEOMUX})
        streams.push_back(sub(static_cast<int>(streams.size()), lang, flags, source));
  for (const auto& preference : {"original", "eng", "forced_only", "none"})
    for (bool on : {false, true})
      for (bool hearing : {false, true})
        for (int saved : {-1, 0}) {
          configure(preference, "", hearing);
          // Use the same production constructor as OpenDefaultStreams.
          SelectionStream as = sub(0, "eng");
          const bool visible = on;
          const int savedIndex = saved;
          /* PRIORITY_CONSTRUCTOR */
          const size_t n = streams.size();
          std::vector<std::vector<bool>> less(n, std::vector<bool>(n));
          for (size_t a = 0; a < n; ++a)
            for (size_t b = 0; b < n; ++b)
              less[a][b] = psp(streams[a], streams[b]);
          bool valid = true;
          for (size_t a = 0; a < n; ++a) {
            valid = valid && !less[a][a];
            for (size_t b = 0; b < n; ++b) {
              valid = valid && !(less[a][b] && less[b][a]);
              for (size_t c = 0; c < n; ++c) {
                valid = valid && !(less[a][b] && less[b][c] && !less[a][c]);
                if (!less[a][b] && !less[b][a] && !less[b][c] && !less[c][b])
                  valid = valid && !less[a][c] && !less[c][a];
              }
            }
          }
          expect(valid, std::string("strict weak ordering: ") + preference +
                 ", on=" + std::to_string(on) + ", hearing=" + std::to_string(hearing) +
                 ", saved=" + std::to_string(saved));
        }
}
void checkRuntimeVisibility() {
  for (bool supportsDisable : {false, true}) {
    for (auto pending : {CDVDMsg::NONE, CDVDMsg::PLAYER_SEEK, CDVDMsg::PLAYER_SEEK_CHAPTER}) {
      Player p;
      p.m_pDemuxer->supportsDisable = supportsDisable;
      p.m_messenger.pending = pending;
      const int source = STREAM_SOURCE_DEMUX + 1;
      p.OpenStream(p.m_CurrentSubtitle, 19, 7, source);
      p.Show(false);
      expect(!p.visible && p.m_pDemuxer->delivering == !supportsDisable, "hide respects demux support");
      p.Show(true);
      p.Show(true);
      expect(p.visible && p.m_pDemuxer->delivering, "show resumes packet delivery");
      expect(p.opens == (supportsDisable ? 2 : 1), "show reopens only a disabled stream, once");
      expect(p.m_CurrentSubtitle.id == 7 && p.m_CurrentSubtitle.demuxerId == 19 &&
             p.m_CurrentSubtitle.source == source, "reopen preserves stream identity");
      expect(p.m_messenger.seeks == (supportsDisable && pending == CDVDMsg::NONE ? 1 : 0),
             "queued time and chapter seeks take precedence over subtitle resync");
    }
  }

  Player pm4k;
  pm4k.m_pDemuxer->supportsDisable = false;
  auto track = sub(0, "eng");
  track.id = 7;
  pm4k.m_SelectionStreams.streams = {track};
  pm4k.OpenStream(pm4k.m_CurrentSubtitle, track.demuxerId, track.id, track.source);
  pm4k.UpdateContent();
  pm4k.Select(-1);
  pm4k.Select(0);
  expect(pm4k.visible && pm4k.opens == 2 && pm4k.m_messenger.seeks == 1,
         "PM4K explicit embedded reselect reopens and resyncs");

  Player sidecar;
  sidecar.OpenStream(sidecar.m_CurrentSubtitle, -1, 0, STREAM_SOURCE_TEXT);
  sidecar.Show(false);
  sidecar.Show(true);
  expect(sidecar.visible && sidecar.opens == 1 && sidecar.m_messenger.seeks == 0,
         "external text visibility leaves playback position alone");

  Player removed;
  removed.OpenStream(removed.m_CurrentSubtitle, 19, 7, STREAM_SOURCE_DEMUX);
  removed.Show(false);
  removed.failedStreams = {7};
  removed.Show(true);
  expect(removed.publishedSubtitle == -1 && removed.m_messenger.seeks == 0,
         "failed reopen clears the published track without seeking");
}

void checkParadiseAutoplay() {
  Player p;
  p.m_pDemuxer->supportsDisable = false; // FFmpeg continues to deliver hidden streams.
  p.m_SelectionStreams.audioTracks = {sub(0, "ger"), sub(1, "eng")};
  p.m_SelectionStreams.streams = {sub(0, "ger", FLAG_FORCED),
                                  sub(1, "eng", FLAG_NONE, STREAM_SOURCE_TEXT)};
  p.UpdateContent();
  p.OpenStream(p.m_CurrentSubtitle, 0, 0, STREAM_SOURCE_DEMUX);
  p.Show(true);

  // Both PM4K startup callbacks request the external SRT before the reused handler fixes audio.
  for (int callback = 0; callback < 2; ++callback) {
    p.QueueVisibility(false);
    p.QueueExternalSrt();
    p.QueueVisibility(true);
  }
  p.m_messenger.Put(std::make_shared<CDVDMsgPlayerSetAudioStream>(
      CDVDMsgPlayerSetAudioStream{{CDVDMsg::PLAYER_SET_AUDIOSTREAM}, 1}));
  p.Drain();
  expect(p.GetAudioStream() == 1 && p.m_audioRecoveryPending, "Paradise autoplay switches to English audio");
  expect(p.GetSubtitle() == 1 && p.m_CurrentSubtitle.source == STREAM_SOURCE_TEXT &&
         p.m_processInfo->settings.m_SubtitleStream == 1, "Paradise autoplay selects the external SRT");
  expect(p.visible, "Paradise autoplay keeps the external SRT visible after switching audio");

  p.QueueExternalSrt();
  p.SetSubtitle(0);
  p.QueueVisibility(false);
  p.Drain();
  expect(p.GetSubtitle() == 0 && p.m_processInfo->settings.m_SubtitleStream == 0,
         "a later manual track selection wins for playback and saved settings");
  expect(!p.visible, "a later manual hide wins over adding an external SRT");
}

void checkNativeDownloadReplay() {
  auto english = sub(0, "eng");
  english.id = 2; // MKV stream id, independent of its Kodi subtitle index.
  Player p;
  p.m_SelectionStreams.streams = {english};
  p.UpdateContent();
  p.sidecars = {"episode.nl.srt"};
  p.QueueExternalSrt("episode.nl.srt", true);
  p.Drain();
  const int saved = p.m_processInfo->settings.m_SubtitleStream;
  const SubtitleIdentity downloaded{true, "dut", "episode.nl.srt"};
  expect(p.m_processInfo->settings.m_subtitleIdentity == downloaded,
         "a native download saves the selected file identity across reordering");
  expect(saved == 0 && p.GetSubtitle() == saved && p.visible &&
         p.m_content.m_selectionStreams.Get(STREAM_SUBTITLE, saved).language == "dut",
         "native download publishes and saves the Dutch sidecar at its startup index");

  Player replay;
  auto dutch = sub(0, "dut", FLAG_NONE, STREAM_SOURCE_TEXT);
  dutch.isFileSidecar = true;
  dutch.filename = "episode.nl.srt";
  english.type_index = 1;
  replay.m_SelectionStreams.streams = {dutch, english}; // startup scans sidecars before the MKV
  replay.m_SelectionStreams.audio.language = "eng";
  replay.m_processInfo->settings = {true, -1, downloaded};
  configure("eng", "eng", false);
  replay.Start();
  expect(replay.visible && replay.m_CurrentSubtitle.source == STREAM_SOURCE_TEXT,
         "replaying the same file restores Dutch over preferred English");

  Player queued;
  english.type_index = 0;
  queued.m_SelectionStreams.streams = {english};
  queued.UpdateContent();
  queued.sidecars = p.sidecars;
  queued.QueueExternalSrt("episode.nl.srt", true);
  queued.SetSubtitle(0); // English as listed when the user made the choice.
  queued.Drain();
  expect(queued.m_CurrentSubtitle.id == english.id && queued.GetSubtitle() == 1 &&
         queued.m_processInfo->settings.m_SubtitleStream == 1,
         "a queued manual selection follows its track across native download numbering");

  // A directory can enumerate the new file before an older sidecar. A VobSub
  // file contributes multiple entries, which retain their internal order.
  auto vob0 = sub(0, "eng", FLAG_NONE, STREAM_SOURCE_DEMUX_SUB);
  vob0.filename = "episode.idx";
  auto vob1 = vob0;
  vob1.id = vob1.type_index = 1;
  english.type_index = 2;
  Player several;
  several.m_SelectionStreams.streams = {vob0, vob1, english};
  several.sidecars = {"episode.nl.srt", "episode.idx"};
  several.QueueExternalSrt("episode.nl.srt", true);
  several.Drain();
  const auto& listed = several.m_content.m_selectionStreams;
  expect(listed.streams.size() == 4 && listed.streams[0].language == "dut" &&
         listed.streams[1].filename == "episode.idx" && listed.streams[1].id == 0 &&
         listed.streams[2].filename == "episode.idx" && listed.streams[2].id == 1 &&
         listed.streams[3].source == STREAM_SOURCE_DEMUX,
         "multiple sidecars follow discovery order and keep VobSub substreams together");

  auto oldBitmap = sub(0, "dut", FLAG_NONE, STREAM_SOURCE_DEMUX_SUB);
  oldBitmap.filename = "episode.sup";
  oldBitmap.demuxerId = 17;
  auto replacement = oldBitmap;
  replacement.type_index = 2;
  replacement.demuxerId = 18;
  english.type_index = 1;
  Player redownload;
  redownload.m_SelectionStreams.streams = {oldBitmap, english, replacement};
  redownload.OpenStream(redownload.m_CurrentSubtitle, replacement.demuxerId,
                        replacement.id, replacement.source);
  expect(redownload.OrderDownloadedSubtitle(2, {replacement.filename}) == 0 &&
         redownload.m_SelectionStreams.streams.size() == 2 &&
         redownload.m_SelectionStreams.streams[0].demuxerId == replacement.demuxerId,
         "replacing a bitmap sidecar keeps a single copy at the replay index");

  Player advanced;
  advanced.m_messenger.Put(std::make_shared<CDVDMsgSubtitleAddFile>("episode.nl.srt",
      SubtitleDownload{"previous.mkv", p.sidecars}));
  advanced.Drain();
  expect(advanced.opens == 0 && advanced.m_SelectionStreams.streams.empty(),
         "a download finishing after the player advances cannot change the new episode");

  Player temporary;
  english.type_index = 0;
  temporary.m_SelectionStreams.streams = {english};
  temporary.QueueExternalSrt("episode.nl.srt", true);
  temporary.Drain();
  expect(temporary.GetSubtitle() == 1 && temporary.visible,
         "a temporary subtitle outside the startup scan remains appended");
  const SubtitleIdentity transient{true, {}, {}};
  expect(temporary.m_processInfo->settings.m_subtitleIdentity == transient,
         "a temporary download leaves a nonpositional external selection for replay");
  configure("eng", "", false);
  // Rebuilding the demux catalogue drops the embedded stream but keeps the attachment.
  auto& rebuilt = temporary.m_SelectionStreams;
  rebuilt.Clear(STREAM_NONE, STREAM_SOURCE_DEMUX);
  rebuilt.Update(english);
  expect(rebuilt.streams.size() == 2 && rebuilt.Get(STREAM_SUBTITLE, 0).type_index == 0 &&
         rebuilt.Get(STREAM_SUBTITLE, 0).source == STREAM_SOURCE_TEXT + 1 &&
         rebuilt.Get(STREAM_SUBTITLE, 1).type_index == 1 &&
         rebuilt.Get(STREAM_SUBTITLE, 1).source == STREAM_SOURCE_DEMUX,
         "demux rebuild gives the surviving attachment and re-added embedded stream distinct indices");
  temporary.UpdateContent();
  temporary.Start();
  expect(temporary.GetSubtitle() == 0 && temporary.visible &&
         temporary.m_CurrentSubtitle.source == STREAM_SOURCE_TEXT + 1,
         "same-session demux rebuild keeps the active temporary attachment at its new index");
  Player nextPlayback;
  nextPlayback.m_processInfo->settings = {true, -1, transient};
  nextPlayback.m_SelectionStreams.streams = temporary.m_SelectionStreams.streams;
  nextPlayback.Start();
  expect(nextPlayback.GetSubtitle() == 1 && nextPlayback.visible &&
         nextPlayback.m_CurrentSubtitle.source == STREAM_SOURCE_DEMUX,
         "fresh playback with the same candidates prefers English over a temporary attachment");
}

void checkSubtitleIdentity() {
  auto dutch = sub(0, "dut", FLAG_NONE, STREAM_SOURCE_TEXT);
  dutch.filename = "smb://user:password@host/private/episode.nl.srt";
  dutch.isFileSidecar = true;
  const SubtitleIdentity expected{true, "dut", "episode.nl.srt"};
  expect(dutch.GetSubtitleIdentity() == expected, "only the SMB sidecar leaf is saved");
  for (const auto& path : {"C:\\private\\episode.nl.srt", "/private/episode.nl.srt",
                           "nfs://host/private/episode.nl.srt", "file:///private/episode.nl.srt"}) {
    dutch.filename = path;
    expect(dutch.GetSubtitleIdentity() == expected, "file-backed sidecar basename");
  }
  for (const auto& path : {"https://host/123.srt?token=test", "plugin://addon/test.srt",
                           "smb://host/test.srt|secret", "/path/test.srt?secret",
                           "smb://host/episode.nl.srt?redirect=/session/value.srt",
                           "nfs://host/test.srt#secret"}) {
    dutch.filename = path;
    expect(dutch.GetSubtitleIdentity().filename.empty(), "no unsafe basename is persisted");
  }
  dutch.filename = "https://host/library/streams/123.srt?token=test";
  dutch.isFileSidecar = false;
  expect(dutch.GetSubtitleIdentity() == SubtitleIdentity{true, {}, {}},
         "a PM4K attachment persists neither its URL identifier nor stream language");

  dutch.filename = "episode.nl.srt";
  dutch.isFileSidecar = true;
  auto secondDutch = dutch;
  secondDutch.type_index = 1;
  secondDutch.filename = "episode.nl.sdh.srt";
  const auto embedded = sub(2, "dut");
  SelectionStreams catalog;
  catalog.streams = {dutch, secondDutch, embedded};
  expect(catalog.ResolveSubtitle(secondDutch.GetSubtitleIdentity()) == 1,
         "two same-language sidecars can be distinguished by basename");
  expect(catalog.ResolveSubtitle({true, "dut", "missing.srt"}) == -1,
         "an absent basename cannot choose between same-language sidecars");
  expect(catalog.ResolveSubtitle({false, "dut", {}}) == 2,
         "embedded and external tracks of the same language remain distinct");
  catalog.streams[1].filename = dutch.filename;
  expect(catalog.ResolveSubtitle(expected) == -1, "two exact matches remain ambiguous");
  catalog.streams[1].language = "eng";
  expect(catalog.ResolveSubtitle({true, "dut", "old.nl.srt"}) == 0,
         "a renamed sidecar can be restored by unique language within its source");
  catalog.streams[0].language = "und";
  expect(catalog.ResolveSubtitle({true, {}, dutch.filename}) == 0 &&
         catalog.ResolveSubtitle({true, {}, {}}) == -1,
         "unknown language needs an exact stable basename");
  catalog.streams[1] = sub(1, "dut");
  expect(catalog.ResolveSubtitle({false, "dut", {}}) == -1,
         "duplicate embedded languages cannot reuse a positional index");
  auto french = sub(2, "fre", FLAG_NONE, STREAM_SOURCE_DEMUX_SUB);
  french.filename = "/show/episode.idx";
  french.isFileSidecar = true;
  auto frenchCommentary = french;
  frenchCommentary.type_index = 3;
  frenchCommentary.id = 8;
  catalog.streams = {french, frenchCommentary};
  expect(catalog.ResolveSubtitle(french.GetSubtitleIdentity()) == -1,
         "same-language substreams of one VobSub file cannot be uniquely restored");

  Player replay;
  auto preferred = sub(0, "eng", FLAG_DEFAULT);
  auto staleSlot = sub(1, "jpn");
  replay.m_SelectionStreams.streams = {preferred, staleSlot};
  replay.m_SelectionStreams.audio.language = "eng";
  configure("eng", "", false);
  replay.m_processInfo->settings = {true, 1, expected};
  replay.Start();
  expect(replay.GetSubtitle() == 0 && replay.visible &&
         replay.m_processInfo->settings.m_subtitleIdentity == expected,
         "a missing identity uses normal selection and preserves the requested sidecar");
  dutch.type_index = 2;
  replay.m_SelectionStreams.streams.push_back(dutch);
  replay.Start();
  expect(replay.GetSubtitle() == 2, "the returning sidecar wins over a stale numerical slot");
  replay.UpdateContent();
  replay.Select(2);
  replay.failedStreams = {staleSlot.id};
  replay.Select(1);
  expect(replay.m_processInfo->settings.m_subtitleIdentity == expected &&
         replay.m_processInfo->settings.m_SubtitleStream == 2,
         "failed manual stream open leaves the previous saved identity and numeric value intact");
  replay.Select(-1);
  expect(!replay.m_processInfo->settings.m_subtitleIdentity &&
         replay.m_processInfo->settings.m_SubtitleStream == -1,
         "an explicit reset clears the saved identity and index");
}

void checkTwoDownloads() {
  const std::string dutch = "episode.nl.srt";
  const std::string german = "episode.de.srt";
  const auto embedded = sub(0, "eng");
  Player playing;
  playing.m_SelectionStreams.streams = {embedded};
  playing.sidecars = {dutch};
  playing.QueueExternalSrt(dutch, true);
  playing.Drain();
  playing.sidecars = {dutch, german};
  playing.QueueExternalSrt(german, true);
  playing.Drain();
  expect(playing.m_SelectionStreams.streams.size() == 3 &&
         playing.m_SelectionStreams.Get(STREAM_SUBTITLE, 0).filename == dutch &&
         playing.m_SelectionStreams.Get(STREAM_SUBTITLE, 1).filename == german &&
         playing.GetSubtitle() == 1 && playing.visible,
         "downloading Dutch then German installs each sidecar once and selects German");
  configure("eng", "", false);
  for (const auto& chosen : {german, dutch}) {
    if (chosen == dutch)
      playing.Select(0);
    const auto identity = playing.m_processInfo->settings.m_subtitleIdentity;
    expect(identity && identity->filename == chosen,
           "latest download or subsequent manual selection records the chosen sidecar");
    for (const auto& discovery : {std::vector<std::string>{german, dutch},
                                  std::vector<std::string>{dutch, german}}) {
      Player fresh;
      for (const auto& file : discovery)
        fresh.AddSubtitleFile(file, {}, true);
      auto english = embedded;
      fresh.m_SelectionStreams.Update(english);
      fresh.m_processInfo->settings = {true, -1, identity};
      fresh.Start();
      expect(fresh.GetSubtitle() >= 0 && fresh.visible &&
             fresh.m_SelectionStreams.Get(STREAM_SUBTITLE, fresh.GetSubtitle()).filename == chosen,
             "fresh playback honors the requested sidecar in either discovery order");
    }
  }
}

int main() {
  const auto regular = sub(0, "eng");
  const auto def = sub(1, "eng", FLAG_DEFAULT);
  const auto forced = sub(2, "eng", FLAG_FORCED);
  const auto sdh = sub(3, "eng", FLAG_HEARING_IMPAIRED);
  const auto cc = sub(4, "und", FLAG_NONE, STREAM_SOURCE_VIDEOMUX);
  check("foreign film default", "original", "jpn", true, {regular, def}, 1, true);
  check("foreign default beats same-language unflagged", "original", "jpn", true,
        {sub(0, "jpn"), def}, 1, true);
  check("default beats original-only", "original", "jpn", true,
        {sub(0, "jpn", FLAG_ORIGINAL), def}, 1, true);
  check("default SDH is still an authored default", "original", "jpn", true,
        {regular, sub(1, "eng", FLAG_DEFAULT | FLAG_HEARING_IMPAIRED)}, 1, true);
  check("original does not enable unflagged regular", "original", "eng", true,
        {regular}, 0, false);
  check("default alone does not override subtitles off", "original", "jpn", false,
        {def}, 1, false);
  check("original forced priority", "original", "eng", true, {def, forced}, 2, true);
  check("explicit language regular priority while on", "eng", "eng", true,
        {regular, forced}, 0, true);
  check("explicit language forced priority while off", "eng", "eng", false,
        {regular, forced}, 2, true);
  check("explicit language accepts sole forced", "eng", "eng", true, {forced}, 2, true);
  check("forced-only enables while off", "forced_only", "eng", false,
        {regular, forced}, 2, true);
  check("forced-only selects while on", "forced_only", "eng", true,
        {regular, forced}, 2, true);
  check("forced default tie breaker", "forced_only", "eng", false,
        {forced, sub(1, "eng", FLAG_FORCED | FLAG_DEFAULT)}, 1, true);
  check("forced-only rejects regular", "forced_only", "eng", true, {regular}, 0, false);
  check("foreign forced default stays rejected", "original", "eng", true,
        {sub(0, "jpn", FLAG_FORCED | FLAG_DEFAULT)}, 0, false);
  check("explicit missing language stays rejected", "eng", "jpn", true,
        {sub(0, "jpn", FLAG_DEFAULT)}, 0, false);
  check("forced follows preferred audio language", "forced_only", "jpn", false,
        {forced, sub(0, "jpn", FLAG_FORCED)}, 2, true, false, -1, "eng");
  check("none rejects even forced", "none", "eng", true, {forced}, 2, false);
  check("saved selection wins", "original", "jpn", true, {regular, def}, 0, true, false, 0);
  check("saved regular selection remains off despite forced", "original", "eng", false,
        {regular, forced}, 0, false, false, 0);
  check("saved selection overrides none", "none", "eng", true, {regular}, 0, true, false, 0);
  check("saved forced auto-enable retained", "none", "eng", false, {forced}, 2, true, false, 2);
  check("hearing preference beats forced", "original", "eng", true,
        {forced, sdh, def}, 3, true, true);
  check("hearing preference overrides forced-only", "forced_only", "eng", true,
        {forced, sdh}, 3, true, true);
  check("original hearing flag retained", "original", "jpn", true,
        {sub(0, "eng", FLAG_ORIGINAL | FLAG_HEARING_IMPAIRED), def}, 0, true, true);
  check("hearing falls back to regular", "eng", "eng", true, {regular}, 0, true, true);
  check("CC unknown language retained", "original", "eng", true, {cc}, 4, true);
  check("CC impaired requires preference", "original", "eng", true,
        {sub(4, "und", FLAG_HEARING_IMPAIRED, STREAM_SOURCE_VIDEOMUX)}, 4, false);
  check("CC impaired accepted with preference", "original", "eng", true,
        {sub(4, "und", FLAG_HEARING_IMPAIRED, STREAM_SOURCE_VIDEOMUX)}, 4, true, true);
  check("irrelevant external does not hide default", "original", "eng", true,
        {def, sub(0, "jpn", FLAG_NONE, STREAM_SOURCE_TEXT)}, 1, true);
  check("matching external preference retained", "eng", "eng", true,
        {def, sub(0, "eng", FLAG_NONE, STREAM_SOURCE_TEXT)}, 0, true);
  check("external regular preference retained while off", "eng", "eng", false,
        {forced, sub(5, "eng", FLAG_NONE, STREAM_SOURCE_TEXT)}, 5, false);
  check("matching external retained for original", "original", "eng", true,
        {sub(0, "eng", FLAG_NONE, STREAM_SOURCE_TEXT)}, 0, true);
  check("unknown external preference retained", "original", "eng", true,
        {def, sub(0, "und", FLAG_NONE, STREAM_SOURCE_TEXT)}, 0, true);
  check("matching forced external", "forced_only", "eng", false,
        {regular, sub(5, "eng", FLAG_FORCED, STREAM_SOURCE_TEXT)}, 5, true);
  check("failed forced open leaves regular fallback off", "eng", "eng", false,
        {forced, regular}, 0, false, false, -1, "", {2});
  check("failed default falls back to another default", "original", "eng", true,
        {def, sub(0, "jpn", FLAG_DEFAULT)}, 0, true, false, -1, "", {1});
  check("failed default cannot enable unflagged fallback", "original", "eng", true,
        {def, regular}, 0, false, false, -1, "", {1});
  checkOrdering();
  checkRuntimeVisibility();
  checkParadiseAutoplay();
  checkNativeDownloadReplay();
  checkSubtitleIdentity();
  checkTwoDownloads();
  std::cout << checks << " subtitle checks, " << failures << " failures\n";
  return failures ? EXIT_FAILURE : EXIT_SUCCESS;
}
'''
    ctor_start = startup.index("  PredicateSubtitlePriority psp(")
    ctor = startup[ctor_start:startup.index(";", ctor_start) + 1]
    source = source.replace("/* PRIORITY_CONSTRUCTOR */", ctor)
    with tempfile.TemporaryDirectory(prefix="subtitle-selection-test-") as temp:
        cpp = pathlib.Path(temp) / "test.cpp"
        binary = pathlib.Path(temp) / "test"
        cpp.write_text(source)
        subprocess.run(["g++", "-std=c++17", "-O1", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                        str(cpp), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    main()
