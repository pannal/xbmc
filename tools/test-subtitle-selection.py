#!/usr/bin/env python3
"""Host regression checks for subtitle selection, without a Kodi/CE build.

Compile the production predicates, stream sort and OpenDefaultStreams subtitle
block with settings, language and stream-opening stubs. This checks selection
and startup visibility, not demuxing, language conversion or forced bitmap cues.
Requires Python 3 and g++; temporary build files are removed automatically.
"""

import pathlib
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text()


def declaration(source, signature):
    start = source.index(signature)
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
    startup = declaration(player, "void CVideoPlayer::OpenDefaultStreams(")
    startup = startup[startup.index("  // enable  or disable subtitles"):
                      startup.index("  // open teletext stream")]

    source = r'''
#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <string>
#include <vector>
''' + flags + sources + types + source_mask + r'''

struct SelectionStream {
  int type_index = 0, id = 0, demuxerId = 0, source = STREAM_SOURCE_DEMUX;
  StreamFlags flags = FLAG_NONE;
  std::string language;
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
''' + macro + predicates + r'''

struct SelectionStreams {
  std::vector<SelectionStream> streams;
  SelectionStream audio;
  std::vector<SelectionStream> Get(StreamType) { return streams; }
  SelectionStream Get(StreamType, int) { return audio; }
''' + sort + r'''
};
struct VideoSettings { bool m_SubtitleOn = true; int m_SubtitleStream = -1; };
struct ProcessInfo {
  VideoSettings settings;
  const VideoSettings& GetVideoSettings() const { return settings; }
};
struct InputStream { virtual ~InputStream() = default; };
struct CDVDInputStreamNavigator : InputStream {};
struct Player {
  std::shared_ptr<ProcessInfo> m_processInfo = std::make_shared<ProcessInfo>();
  std::shared_ptr<InputStream> m_pInputStream;
  struct { std::string state; } m_playerOptions;
  SelectionStreams m_SelectionStreams;
  SelectionStream m_CurrentSubtitle;
  std::vector<int> failedStreams;
  bool visible = false, enabled = false;
  int GetAudioStream() { return 0; }
  bool OpenStream(SelectionStream& current, int, int id, int source) {
    if (std::find(failedStreams.begin(), failedStreams.end(), id) != failedStreams.end())
      return false;
    current.id = id; current.source = source; enabled = true;
    return true;
  }
  void CloseStream(SelectionStream& current, bool) { current.id = -1; }
  void SetEnableStream(SelectionStream&, bool value) { enabled = value; }
  void SetSubtitleVisibleInternal(bool value) { visible = value; }
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
    expect(!visible || p.enabled, label + " visible stream enabled");
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
          Player p;
          p.m_processInfo->settings = {on, saved};
          auto& m_processInfo = p.m_processInfo;
          SelectionStream as = sub(0, "eng");
          const bool visible = on;
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
