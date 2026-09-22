#!/usr/bin/env python3
"""Host regression checks for DV L5 policy, without a CE image build.

Compile the production geometry/sample functions, watcher, subtitle setters and
FrameMove policy block with small settings/cache/overlay/sysfs stubs. This tests
policy decisions, not real Kodi threads, decoded pictures or HDMI output.
Requires Python 3 and g++; temporary build files are removed automatically.
"""

import pathlib
import re
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text()


def function(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def main():
    aml = read("xbmc/utils/AMLUtils.cpp")
    render = read("xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.cpp")
    render_h = read("xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.h")
    video_h = read("xbmc/cores/VideoPlayer/VideoPlayerVideo.h")
    frame = function(render, "void CRenderManager::FrameMove()")
    policy = frame[frame.index("  const int subsSignalMode ="):
                   frame.index("  m_playerPort->UpdateGuiRender")]
    # The GUI render pass must not overwrite the continuous policy with false.
    assert re.search(r"if \(subsSignalMode == 2\)\s+aml_dv_set_subtitles\(signalSubtitles\);",
                     function(render, "void CRenderManager::Render("))
    uninit = function(render, "void CRenderManager::UnInit()")
    reset = uninit[uninit.index("  m_subtitleEnabled.store(false);"):
                   uninit.index("  m_debugRenderer.Dispose();")]
    constants = aml[aml.index("static constexpr uint32_t AUTO_LB_AR_MAX"):
                    aml.index("static std::atomic<bool> s_autoLbWatchCancel")]
    verdicts = re.search(r"enum\s*\{\s*AUTO_LB_SAMPLE_OK.*?\};", aml, re.S).group()
    geometry = function(aml, "static bool _auto_letterbox_geometry(")
    sample = function(aml, "static int _auto_letterbox_duplicate_sample(")
    watcher = function(aml, "static void _auto_letterbox_watch_run(")
    renderer_setter = function(render_h, "void SetSubtitleEnabled(")
    video_setter = function(video_h, "void EnableSubtitle(").replace(" override", "")

    source = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <memory>
#include <thread>

struct Metadata {
  bool has_level5_metadata = true;
  uint16_t level5_active_area_top_offset = 0;
  uint16_t level5_active_area_bottom_offset = 0;
  uint16_t level5_active_area_left_offset = 0;
  uint16_t level5_active_area_right_offset = 0;
};
struct Cache {
  Metadata meta;
  Metadata GetVideoDoViFrameMetadata() { return meta; }
} cache;
struct CServiceBroker {
  static Cache& GetDataCacheCore() { return cache; }
  static bool IsServiceManagerUp() { return true; }
};
struct CSettings { static constexpr int SETTING_COREELEC_AMLOGIC_DV_L5_AUTO_LETTERBOX = 0; };
struct Settings { bool enabled = true; bool GetBool(int) { return enabled; } } config;
Settings* settings() { return &config; }
bool manual = false;
bool aml_dv_l5_override_active() { return manual; }
std::atomic<int> s_autoLbWidth{1918}, s_autoLbHeight{802};
std::atomic<bool> s_autoLbNativeDV{true}, s_autoLbAdditive{true}, s_autoLbWatchCancel{false};
int applied = 0;
void aml_dv_apply_l5_override_sysfs() { ++applied; }
constexpr int LOGINFO = 0;
struct CLog { template<class... Args> static void Log(Args...) {} };
''' + constants + verdicts + geometry + r'''
bool aml_dv_auto_letterbox_active() {
  uint16_t t, b, l, r;
  return _auto_letterbox_geometry(t, b, l, r);
}
''' + sample + watcher + r'''
int mode = 1;
bool signaled = false;
int writes = 0;
int aml_dv_l5_subs_signal_mode() { return mode; }
void aml_dv_set_subtitles(bool value) { signaled = value; ++writes; }
struct Overlays { bool present = false; bool HasOverlay(int) { return present; } };
struct Player { int count = 1; int GetSubtitleCount() const { return count; } };
struct Renderer {
  std::atomic_bool m_subtitleEnabled{false};
  Overlays m_overlays;
  int m_presentsource = 0;
  std::shared_ptr<Player> m_appPlayer = std::make_shared<Player>();
''' + renderer_setter + "\nvoid Tick() {\n" + policy + "\n}\nvoid Reset() {\n" + reset + r'''
}
};
struct Video {
  std::atomic_bool m_bRenderSubs{false};
  Renderer& m_renderManager;
''' + video_setter + r'''
};
int checks = 0;
void expect(bool value) { assert(value); ++checks; }
int classify(int w, int h, Metadata meta) {
  s_autoLbWidth = w; s_autoLbHeight = h; cache.meta = meta;
  uint16_t t, b, l, r, src[4]; const char* reason = nullptr;
  if (!_auto_letterbox_geometry(t, b, l, r)) return -1;
  return _auto_letterbox_duplicate_sample(t, b, l, r, w, h, src, reason);
}
int main() {
  constexpr int ok = AUTO_LB_SAMPLE_OK, suspect = AUTO_LB_SAMPLE_SUSPECT;
  expect(classify(1918, 802, {true, 0, 0, 0, 464}) == suspect);
  expect(classify(1918, 802, {true, 0, 0, 464, 0}) == suspect);
  expect(classify(3836, 1604, {true, 0, 0, 0, 928}) == suspect);
  expect(classify(1918, 802, {true, 0, 0, 4, 464}) == suspect);
  expect(classify(1918, 802, {true, 0, 0, 464, 464}) == ok);
  expect(classify(1918, 802, {true, 0, 0, 232, 232}) == ok);
  expect(classify(1918, 802, {true, 0, 0, 230, 234}) == ok);
  expect(classify(1918, 802, {true, 0, 0, 0, 4}) == ok);
  expect(classify(1918, 802, {true, 0, 0, 0, 38}) == ok);
  expect(classify(1918, 802, {true, 0, 0, 0, 39}) == suspect);
  expect(classify(1918, 802, {true, 0, 0, 60, 464}) == ok);
  expect(classify(1918, 802, {true, 20, 20, 0, 464}) == ok);
  expect(classify(1918, 802, {false, 0, 0, 0, 464}) == ok);
  expect(classify(1918, 802, {}) == ok);
  expect(classify(3840, 2024, {true, 208, 208, 0, 0}) == ok);
  expect(classify(3840, 2024, {true, 0, 208, 0, 0}) == ok);
  expect(classify(3840, 2024, {true, 68, 68, 0, 0}) == suspect);
  expect(classify(3840, 1600, {true, 280, 280, 0, 0}) == AUTO_LB_SAMPLE_IMPOSSIBLE);
  expect(classify(1918, 802, {true, 0, 0, 0, 1918}) == AUTO_LB_SAMPLE_IMPOSSIBLE);
  expect(classify(1800, 800, {true, 0, 0, 0, 400}) == ok); // unusual coded aspect
  expect(classify(1440, 1080, {true, 0, 200, 0, 0}) == suspect); // mirrored axis
  expect(classify(1440, 1080, {true, 100, 100, 0, 0}) == ok);
  expect(classify(1920, 1080, {true, 0, 0, 0, 464}) == -1); // not cropped
  manual = true;
  expect(classify(1918, 802, {true, 0, 0, 0, 464}) == -1);
  manual = false; config.enabled = false;
  expect(classify(1918, 802, {true, 0, 0, 0, 464}) == -1);
  config.enabled = true; s_autoLbNativeDV = false;
  expect(classify(1918, 802, {true, 0, 0, 0, 464}) == -1);
  s_autoLbNativeDV = true;
  classify(1918, 802, {true, 0, 0, 0, 464});
  uint16_t t, b, l, r;
  expect(_auto_letterbox_geometry(t, b, l, r) && t == 138 && b == 138 && l == 0 && r == 0);
  _auto_letterbox_watch_run(1918, 802); // real six-poll confirmation (~3 seconds)
  expect(!s_autoLbAdditive && applied == 1);

  Renderer renderer; Video video{false, renderer};
  video.EnableSubtitle(true); renderer.Tick();
  expect(signaled && video.m_bRenderSubs);
  for (int i = 0; i < 20; ++i) renderer.Tick(); // no overlays / no decoder activity
  expect(signaled);
  renderer.m_overlays.present = true; renderer.Tick(); expect(signaled);
  renderer.m_overlays.present = false;
  video.EnableSubtitle(false); renderer.Tick(); expect(!signaled);
  video.EnableSubtitle(true); renderer.Tick(); expect(signaled);
  mode = 0; renderer.Tick(); expect(!signaled);
  mode = 1; renderer.Tick(); expect(signaled);
  mode = 2; renderer.Tick(); expect(!signaled);
  renderer.m_overlays.present = true;
  int previousWrites = writes; renderer.Tick();
  expect(writes == previousWrites); // visible mode defers overlap decisions to Render
  mode = 1; renderer.Tick(); expect(signaled);
  renderer.m_appPlayer->count = 0; renderer.Tick(); expect(!signaled);
  renderer.m_appPlayer->count = 1; renderer.Tick(); expect(signaled);
  renderer.Reset(); renderer.Tick(); expect(!signaled && !renderer.m_subtitleEnabled);
  std::cout << checks << " DV L5 policy checks passed\n";
}
'''
    with tempfile.TemporaryDirectory(prefix="dv-l5-test-") as temp:
        cpp = pathlib.Path(temp) / "test.cpp"
        binary = pathlib.Path(temp) / "test"
        cpp.write_text(source)
        subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pthread",
                        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                        str(cpp), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    main()
