#!/usr/bin/env python3
"""Exercise production audio admission after an interrupted frame submission.

Extract Process's queue selection and hint reset unchanged. Queue, clock and
reusable decoder storage are host stand-ins; no sink or hardware is modeled.
"""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
audio = (ROOT / "xbmc/cores/VideoPlayer/VideoPlayerAudio.cpp").read_text()
process = audio.index("void CVideoPlayerAudio::Process()")
start = audio.index("    auto timeout =", process)
reset = "    onlyPrioMsgs = false;"
end = audio.index(reset, start) + len(reset)
selection = audio[start:end]
harness = r'''
#include <array>
#include <cassert>
#include <chrono>
#include <deque>
#include <iostream>
#include <memory>
using namespace std::chrono_literals;
constexpr int DVD_PLAYSPEED_NORMAL = 1000, DVD_PLAYSPEED_PAUSE = 0;
enum MsgQueueReturnCode { MSGQ_OK, MSGQ_TIMEOUT };
struct CDVDMsg { enum Type { PLAYER_REQUEST_STATE, DEMUXER_PACKET } type; };
struct IDVDStreamPlayer { enum { SYNC_STARTING, SYNC_WAITSYNC, SYNC_INSYNC }; };
struct Queue {
  std::deque<std::shared_ptr<CDVDMsg>> controls, packets;
  int requestedPriority = -1;
  std::chrono::milliseconds requestedTimeout{};
  void put(CDVDMsg::Type type) {
    auto message = std::make_shared<CDVDMsg>();
    message->type = type;
    (type == CDVDMsg::DEMUXER_PACKET ? packets : controls).push_back(message);
  }
  MsgQueueReturnCode Get(std::shared_ptr<CDVDMsg>& message,
                        std::chrono::milliseconds timeout, int& priority) {
    requestedPriority = priority;
    requestedTimeout = timeout;
    // Match DVDMessageQueue::Get's control-first selection and priority floor.
    auto& queue = priority > 0 || !controls.empty() ? controls : packets;
    if (queue.empty())
      return MSGQ_TIMEOUT;
    message = queue.front();
    queue.pop_front();
    priority = message->type == CDVDMsg::DEMUXER_PACKET ? 0 : 1;
    return MSGQ_OK;
  }
};
struct Clock { double GetClock() { return 0; } };
struct ProcessInfo { bool allowed = true; bool IsTempoAllowed(float) { return allowed; } };
struct Sink { double GetCacheTime() { return 0.1; } };
struct Player {
  int m_syncState = IDVDStreamPlayer::SYNC_STARTING;
  int m_speed = DVD_PLAYSPEED_NORMAL;
  bool m_paused = false, onlyPrioMsgs = true;
  double m_audioClock = 0;
  Clock clock;
  Clock* m_pClock = &clock;
  ProcessInfo m_processInfo;
  Sink m_audioSink;
  Queue m_messageQueue;
  struct Frame { int nb_frames = 4, framesOut = 0; } audioframe;
  MsgQueueReturnCode next(std::shared_ptr<CDVDMsg>& pMsg) {
@SELECTION@
    return ret;
  }
};

void retainedFrameSurvivesControl(int alreadySubmitted,
                                 int syncState = IDVDStreamPlayer::SYNC_INSYNC,
                                 int speed = DVD_PLAYSPEED_NORMAL) {
  Player player;
  player.m_syncState = syncState;
  player.m_speed = speed;
  player.m_processInfo.allowed = speed == DVD_PLAYSPEED_NORMAL;
  player.audioframe.framesOut = alreadySubmitted;
  player.m_messageQueue.put(CDVDMsg::PLAYER_REQUEST_STATE);
  player.m_messageQueue.put(CDVDMsg::DEMUXER_PACKET);
  std::array<unsigned char, 4> decoderStorage{0x10, 0x11, 0x12, 0x13};
  const auto original = decoderStorage;
  const auto* borrowed = decoderStorage.data();
  std::shared_ptr<CDVDMsg> message;

  assert(player.next(message) == MSGQ_OK);
  assert(message->type == CDVDMsg::PLAYER_REQUEST_STATE);
  assert(!player.onlyPrioMsgs);
  const auto next = player.next(message);
  if (next == MSGQ_OK) {
    // Feeding the next packet would reuse storage still borrowed by the frame.
    assert(message->type == CDVDMsg::DEMUXER_PACKET);
    decoderStorage.fill(0x20);
  }
  for (int i = alreadySubmitted; i < player.audioframe.nb_frames; ++i)
    assert(borrowed[i] == original[i]);
  assert(next == MSGQ_TIMEOUT);
  assert(player.m_messageQueue.requestedPriority == 1);
  assert(player.m_messageQueue.requestedTimeout == 0ms);
  assert(player.m_messageQueue.packets.size() == 1);

  player.audioframe.framesOut = player.audioframe.nb_frames;
  assert(player.next(message) == MSGQ_OK);
  assert(message->type == CDVDMsg::DEMUXER_PACKET);
  assert(player.m_messageQueue.requestedPriority == 0);
}

int main() {
  // Both an aborted first submission and a partial write retain decoder bytes.
  retainedFrameSurvivesControl(0);
  retainedFrameSurvivesControl(2);
  for (int submitted : {0, 2})
    for (int speed : {DVD_PLAYSPEED_PAUSE, -2000})
      retainedFrameSurvivesControl(submitted, IDVDStreamPlayer::SYNC_STARTING, speed);

  Player player;
  player.onlyPrioMsgs = false;
  std::shared_ptr<CDVDMsg> message;
  player.m_messageQueue.put(CDVDMsg::DEMUXER_PACKET);
  player.m_paused = true;
  assert(player.next(message) == MSGQ_TIMEOUT);
  assert(player.m_messageQueue.requestedTimeout == 100ms);
  // Resume without a fresh drain hint must still protect the pending view.
  player.m_paused = false;
  assert(player.next(message) == MSGQ_TIMEOUT);
  assert(player.m_messageQueue.requestedTimeout == 0ms);

  player.m_syncState = IDVDStreamPlayer::SYNC_INSYNC;
  player.m_processInfo.allowed = false;
  assert(player.next(message) == MSGQ_TIMEOUT);
  assert(player.m_messageQueue.requestedTimeout == 100ms);
  player.m_processInfo.allowed = true;
  assert(player.next(message) == MSGQ_TIMEOUT);
  assert(player.m_messageQueue.requestedTimeout == 0ms);

  player.m_syncState = IDVDStreamPlayer::SYNC_WAITSYNC;
  player.m_speed = DVD_PLAYSPEED_PAUSE;
  player.m_processInfo.allowed = false;
  assert(player.next(message) == MSGQ_TIMEOUT);
  assert(player.m_messageQueue.requestedTimeout == 0ms);
  player.audioframe.framesOut = player.audioframe.nb_frames;
  assert(player.next(message) == MSGQ_TIMEOUT);
  assert(player.m_messageQueue.requestedPriority == 1);
  player.m_syncState = IDVDStreamPlayer::SYNC_STARTING;
  player.audioframe.nb_frames = player.audioframe.framesOut = 0;
  assert(player.next(message) == MSGQ_OK);
  std::cout << "audio retained-frame admission and control interruption: PASS\n";
}
'''
harness = harness.replace("@SELECTION@", selection)
with tempfile.TemporaryDirectory(prefix="audio-frame-admission-") as temp:
    source = Path(temp) / "test.cpp"
    binary = Path(temp) / "test"
    source.write_text(harness)
    subprocess.run([os.environ.get("CXX", "g++"), "-std=c++17", "-Wall", "-Wextra",
                    "-Werror", "-fsanitize=address,undefined", "-fno-pie", "-no-pie",
                    str(source), "-o", str(binary)], check=True)
    env = os.environ.copy()
    env.setdefault("ASAN_OPTIONS", "detect_leaks=0")
    subprocess.run([str(binary)], check=True, env=env)
