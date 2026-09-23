#!/usr/bin/env python3
"""Exercise AML presentation/close ordering without hardware or a CE build.

Compile the production open/close gates, SetVideoRect entry guard and complete
ReleaseFrame/GetPresentationGeneration methods with a fake video device. The
remaining SetVideoRect and decoder lifecycle effects are stubs: this verifies
their synchronization boundary, not geometry, Kodi scheduling or HDMI recovery.
Use --baseline REV with a pre-fix revision to demonstrate the late-show failure.
Requires Python 3 and g++; temporary compiler outputs are removed automatically.
"""

import argparse
import pathlib
import subprocess
import tempfile
import textwrap


ROOT = pathlib.Path(__file__).resolve().parents[1]
CODEC = "xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp"


def function(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", nargs="?", const="HEAD", metavar="REV",
                        help="test pre-fix source at REV (defaults to HEAD)")
    args = parser.parse_args()
    source = (subprocess.check_output(["git", "show", f"{args.baseline}:{CODEC}"],
                                      cwd=ROOT, text=True)
              if args.baseline else (ROOT / CODEC).read_text())
    opened = function(source, "bool CAMLCodec::OpenDecoder()")
    publish = opened.split("  SetPollDevice(am_private->vcodec.cntl_handle);", 1)[1]
    publish = publish[:publish.rindex("  return true;")]
    closed = function(source, "void CAMLCodec::CloseDecoder()")
    close_gate = closed.split('  CLog::Log(LOGINFO, "CAMLCodec::CloseDecoder");', 1)[1]
    close_gate = close_gate.split("  // Make sure the green-flash hold", 1)[0]
    rect = function(source, "void CAMLCodec::SetVideoRect(")
    rect_gate = rect[rect.index("{") + 1:rect.index("  // this routine gets called")]
    release = function(source, "int CAMLCodec::ReleaseFrame(")
    if args.baseline:
        release = release.replace("const uint32_t index, bool drop",
                                  "const uint32_t index, uint64_t generation, bool drop")
        generation = "uint64_t CAMLCodec::GetPresentationGeneration() { return 0; }"
    else:
        generation = function(source, "uint64_t CAMLCodec::GetPresentationGeneration()")

    harness = r'''
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <functional>
#include <future>
#include <iostream>
#include <linux/videodev2.h>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>

using namespace std::chrono_literals;
void check(bool ok, const char* message) {
  if (!ok) throw std::runtime_error(message);
}
constexpr int LOGDEBUG = 0, LOGVIDEO = 0, LOGERROR = 0;
struct CLog { template<class... T> static void Log(T&&...) {} };
struct Device {
  std::atomic<int> queued{0}, dropped{0}, shows{0};
  std::atomic<bool> visible{false}, closed{true};
  std::function<void()> beforeQueue;
  int IOControl(int, v4l2_buffer* buffer) {
    if (beforeQueue) beforeQueue();
    check(!closed, "frame returned to closed device");
    ++queued;
    if (buffer->flags & V4L2_BUF_FLAG_DONE) ++dropped;
    return 0;
  }
};
class CAMLCodec {
public:
  std::mutex m_presentationMutex;
  bool m_presentationActive = false;
  uint64_t m_presentationGeneration = 0;
  std::shared_ptr<Device> device = std::make_shared<Device>();
  std::shared_ptr<Device> m_amlVideoFile;
  std::function<void()> beforeShow;
  std::atomic<bool> teardownUnlocked{false};
  void Open(bool success = true) {
    if (!success) return; // the production publication is after all open failures
    device->closed = false;
    m_amlVideoFile = device;
    @PUBLISH@
  }
  void Close() {
    @CLOSE@
    // DV teardown must be outside this mutex (graphics/DV lock ordering).
    teardownUnlocked = m_presentationMutex.try_lock();
    if (teardownUnlocked) m_presentationMutex.unlock();
    device->closed = true;
    device->visible = false;
    m_amlVideoFile.reset();
  }
  void SetVideoRect(uint64_t generation) {
    @RECT@
    if (beforeShow) beforeShow();
    ++device->shows;
    device->visible = true;
  }
  int ReleaseFrame(uint32_t index, uint64_t generation, bool drop = false);
  uint64_t GetPresentationGeneration();
};
@RELEASE@
@GENERATION@

void test_close_first() {
  CAMLCodec codec;
  codec.Open();
  const auto token = codec.GetPresentationGeneration();
  codec.Close();
  codec.SetVideoRect(token);
  check(!codec.device->visible, "late SetVideoRect re-enabled video after close");
  codec.ReleaseFrame(1, token);
  codec.ReleaseFrame(2, token, true);
  check(codec.device->queued == 0, "closed decoder accepted a buffered frame");
  check(codec.teardownUnlocked, "presentation mutex held during teardown");
}

void test_normal_and_split_close() {
  CAMLCodec codec;
  codec.Open();
  auto token = codec.GetPresentationGeneration();
  codec.ReleaseFrame(1, token);
  codec.SetVideoRect(token);
  codec.ReleaseFrame(2, token, true);
  check(codec.device->visible && codec.device->queued == 2 && codec.device->dropped == 1,
        "normal presentation or buffer drop broken");
  // Close can occur between RenderUpdate's two separately guarded calls.
  codec.ReleaseFrame(3, token);
  codec.Close();
  codec.SetVideoRect(token);
  check(!codec.device->visible, "close between release and show was ineffective");
}

void test_reopen() {
  CAMLCodec codec;
  codec.Open();
  auto old = codec.GetPresentationGeneration();
  codec.Close();
  codec.Open();
  auto current = codec.GetPresentationGeneration();
  check(current != old, "reopen reused presentation generation");
  codec.ReleaseFrame(1, old, true);
  codec.SetVideoRect(old);
  check(codec.device->queued == 0 && !codec.device->visible,
        "old frame affected reopened decoder");
  codec.ReleaseFrame(1, current);
  codec.SetVideoRect(current);
  check(codec.device->queued == 1 && codec.device->visible, "new generation rejected");
  codec.Close();
  codec.Open(false);
  codec.SetVideoRect(current);
  check(!codec.device->visible, "failed reopen enabled presentation");
}

void test_close_waits(bool releasing) {
  CAMLCodec codec;
  codec.Open();
  auto token = codec.GetPresentationGeneration();
  std::promise<void> entered, proceed, closing;
  auto proceedFuture = proceed.get_future().share();
  auto block = [&] { entered.set_value(); proceedFuture.wait(); };
  if (releasing) codec.device->beforeQueue = block;
  else codec.beforeShow = block;
  auto render = std::async(std::launch::async, [&] {
    if (releasing) codec.ReleaseFrame(1, token);
    else codec.SetVideoRect(token);
  });
  entered.get_future().wait();
  auto close = std::async(std::launch::async, [&] {
    closing.set_value();
    codec.Close();
  });
  closing.get_future().wait();
  // Bound the negative check; always release the fake operation before asserting.
  bool waiting = close.wait_for(50ms) == std::future_status::timeout;
  proceed.set_value();
  render.get();
  close.get();
  check(waiting, "close did not drain presentation already in flight");
  check(codec.device->closed && !codec.device->visible && codec.teardownUnlocked,
        "in-flight operation outlived teardown or teardown retained mutex");
}

void test_old_codec_cannot_touch_new() {
  CAMLCodec oldCodec, newCodec;
  newCodec.device = oldCodec.device; // hardware plane is shared across sessions
  oldCodec.Open();
  auto old = oldCodec.GetPresentationGeneration();
  oldCodec.Close();
  newCodec.Open();
  oldCodec.ReleaseFrame(1, old, true);
  oldCodec.SetVideoRect(old);
  check(newCodec.device->queued == 0 && !newCodec.device->visible,
        "closed codec touched next session's hardware plane");
}

int main() {
  try {
    test_close_first();
    test_normal_and_split_close();
    test_reopen();
    test_close_waits(false);
    test_close_waits(true);
    test_old_codec_cannot_touch_new();
    std::cout << "PASS: 6 AML presentation lifecycle scenarios\n";
  } catch (const std::exception& e) {
    std::cerr << "FAIL: " << e.what() << '\n';
    return 1;
  }
}
'''
    for key, value in [("PUBLISH", publish), ("CLOSE", close_gate), ("RECT", rect_gate),
                       ("RELEASE", release), ("GENERATION", generation)]:
        if key in ("PUBLISH", "CLOSE", "RECT"):
            value = textwrap.indent(textwrap.dedent(value), "    ")
        harness = harness.replace(f"@{key}@", value)
    with tempfile.TemporaryDirectory(prefix="aml-presentation-") as temp:
        cpp = pathlib.Path(temp) / "test.cpp"
        exe = pathlib.Path(temp) / "test"
        cpp.write_text(harness)
        subprocess.run(["g++", "-std=c++17", "-pthread", "-Wall", "-Wextra", "-Werror",
                        "-Wno-unused-parameter", "-fsanitize=address,undefined", "-g",
                        str(cpp), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True, timeout=15)


if __name__ == "__main__":
    main()
