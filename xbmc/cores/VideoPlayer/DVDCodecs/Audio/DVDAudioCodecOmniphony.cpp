/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "DVDAudioCodecOmniphony.h"

#include "DVDAudioCodecFFmpeg.h"
#include "DVDCodecs/DVDCodecs.h"
#include "DVDStreamInfo.h"
#include "OmniphonyPcmSource.h"
#include "ServiceBroker.h"
#include "cores/AudioEngine/Omniphony/OmniphonyHrtf.h"
#include "cores/AudioEngine/Utils/AEUtil.h"
#include "cores/VideoPlayer/Interface/DemuxPacket.h"
#include "cores/VideoPlayer/Process/ProcessInfo.h"
#include "filesystem/Directory.h"
#include "filesystem/SpecialProtocol.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"
#include "threads/SystemClock.h"
#include "utils/StringUtils.h"
#include "utils/log.h"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstring>
#include <signal.h>

#include <fcntl.h>
#include <poll.h>
#include <sys/wait.h>
#include <unistd.h>

namespace
{
constexpr uint8_t OP_OPEN = 1;
constexpr uint8_t OP_FEED = 2;
constexpr uint8_t OP_FLUSH = 3;
constexpr uint8_t OP_RESET = 4;
constexpr uint8_t OP_CLOSE = 5;

//! The helper writes this only after all drain output has crossed the pipe.
constexpr const char* OMNI_FLUSH_MARK = "flush";

//! A drain is bounded so a broken helper cannot park the audio thread at EOF.
constexpr unsigned int OMNI_FLUSH_TIMEOUT_MS = 5000;

constexpr size_t OMNI_HDR_LEN = 16;

//! The helper renders to stereo; anything else means we misunderstood it.
constexpr unsigned int OMNI_OUT_CHANNELS = 2;
constexpr unsigned int OMNI_OUT_RATE = 48000;

//! Beyond this the helper is not keeping up and we stop queueing for it rather
//! than growing without bound.
constexpr size_t OMNI_MAX_PENDING = 4u << 20;

//! How long one wait inside AddData may take. Bounded because the audio thread
//! cannot service a flush or a seek while it is in here, and generous enough to
//! cover one access unit of the heaviest codec on a badly loaded box.
constexpr unsigned int OMNI_FEED_BUDGET_MS = 100;

//! One slice of that wait. Short enough that a helper which answers promptly is
//! never held back by the granularity.
constexpr int OMNI_PUMP_SLICE_MS = 20;

//! How long the pump thread sits in poll() with nothing to do. It only matters
//! when there is neither output to read nor input to write, so all it bounds is
//! how late a command queued by another thread starts being written.
constexpr int OMNI_PUMP_IDLE_MS = 20;

/*!
 * \brief How far ahead of the player the render is allowed to work.
 *
 * The reserve exists to cover the dips, and the dips are short. Sampling the
 * helper's own CPU once a second across two whole films puts the render at
 * 0.42 of a core on Dolby Digital Plus and 0.47 on TrueHD, never above 0.82
 * for more than the odd second, so the work always has somewhere to go. What
 * varies is whether it gets the chance: on Dolby Digital Plus the reserve
 * fills and this limit is what stops it, while on TrueHD it stays near empty
 * because the player only feeds at the speed it plays. Something over a second
 * covers the dips with room to refill afterwards. Larger would not cover more
 * - a renderer that is short on average empties any reserve eventually - and
 * it would put more audio between a seek and what is heard.
 *
 * Do not read the pump thread's own throughput as the render's cost. It sleeps
 * once the bank is full, so it reads about one times realtime whatever the
 * render is doing; that is what made an earlier draft of this comment quote a
 * figure of 0.65 that measured nothing.
 *
 * Feeding stops here rather than rendering: the helper is left with nothing
 * queued, so it idles instead of running on into audio nobody has asked for.
 */
constexpr int OMNI_BANK_FRAMES = static_cast<int>(OMNI_OUT_RATE) * 3 / 2;

/*!
 * \brief Input queued for a helper that has stopped taking it.
 *
 * The bank alone is not enough of a brake. Once it is full the helper stops
 * reading, and what the player keeps feeding piles up here instead - and
 * reaching OMNI_MAX_PENDING is fatal, it breaks the helper and drops the film
 * onto the software decoder. A tenth of that is where waiting starts, so the
 * fatal limit is never the one that arrives first.
 */
constexpr size_t OMNI_FEED_QUEUE_MAX = OMNI_MAX_PENDING / 10;

/*!
 * \brief Sample-frames of decoded PCM in one write to the helper.
 *
 * The object path's write size chooses itself - one access unit, whatever that
 * comes to. Decoded PCM has no such unit, so it needs a number, and this one is
 * about 21 ms: 48 KB at twelve channels, comfortably under the helper's 1 MiB
 * payload cap, and small enough that the feed budget is never spent inside a
 * single write.
 *
 * Chunks need not fall on a sample-frame boundary and after a header they do
 * not, since the header is fourteen bytes plus one per channel. The bridge
 * parses a byte stream and buffers whatever is left over, so a split anywhere
 * is the case it is built for.
 */
constexpr size_t OMNI_PCM_WRITE_FRAMES = 1024;

/*!
 * \brief The most the pump thread will hold before it stops reading the helper.
 *
 * Nothing should ever reach this: feeding stops at OMNI_BANK_FRAMES, so the
 * helper runs out of work long before. It is here because the pump thread runs
 * whether or not anyone is collecting from it, and a codec that stopped
 * collecting - the player parked in the sink for several seconds is the case
 * that prompted all of this - must not be able to grow this without bound.
 */
constexpr size_t OMNI_PUMP_HOLD_FRAMES = static_cast<size_t>(OMNI_OUT_RATE) * 10;

/*!
 * \brief Rendered audio to bank before letting the player have any.
 *
 * Half a second, because the sink is already holding some. ActiveAE reports
 * totalcache 0.800 and CVideoPlayerAudio starts the clock once it is about
 * three fifths full, so roughly half a second of audio is downstream of this
 * before a single frame is heard. Priming adds to that rather than replacing
 * it, and the two together are what covers the warm-up.
 *
 * It used to be a full second. Field logs across eighteen playbacks then
 * showed the bank reaching zero within the first four seconds of most of them
 * and playback continuing undisturbed, which is the sink doing its half of the
 * job - so the second half of that second was buying startup delay and not
 * much else.
 */
constexpr int OMNI_PRIME_FRAMES = static_cast<int>(OMNI_OUT_RATE) / 2;

/*!
 * \brief The same, for a seek, where far less is needed.
 *
 * Most of what a cold start pays for is one-off and survives a seek: the helper
 * process, the engine loaded into it, the HRIR set, the allocated buffers and
 * the pages behind them. orender_reset flushes decoder and renderer state and
 * keeps everything else, so what a seek faces is refilling filter state, not
 * warming up from nothing. Banking a full second again would put the whole
 * cold-start delay on every scrub for a deficit that is not there. Trimmed
 * again with the sink's own 0.8 s counted: what a seek has to cover is the
 * moment before ActiveAE has refilled, not a warm-up.
 */
constexpr int OMNI_PRIME_FRAMES_SEEK = static_cast<int>(OMNI_OUT_RATE) * 15 / 100;

//! The longest priming may hold playback. A helper that cannot fill the bank in
//! this long is not going to, and silence is worse than starting short.
constexpr unsigned int OMNI_PRIME_TIMEOUT_MS = 2500;

/*!
 * \brief How long the render mode stays open to what the stream turns out to be.
 *
 * The object count is only truthful once a frame has been rendered, and after a
 * mid-film resume the first frames are often bed-only - so the count that
 * decides between per-object rendering and the virtual layout can arrive a
 * little after the stream starts. This is how long we keep listening for it.
 *
 * Generous against that delay and mean against the alternative: a restart costs
 * the reserve and a re-prime, and changes the imaging audibly, so it has to
 * land while the film is still starting rather than an hour in. Past this the
 * mode is settled for good and a late count is logged instead of acted on.
 */
constexpr unsigned int OMNI_MODE_WINDOW_MS = 5000;

/*!
 * \brief Level the render is asked for, in dB, before the downmix correction.
 *
 * Before the correction is the whole of the point: this is not the gain the
 * engine receives. WriteConfig subtracts OMNI_NORMALIZED_DOWNMIX_DB from it
 * whenever Kodi is normalising its own stereo fold, which is the default, so
 * -3 here reaches the renderer as -12.5 for a 5.1 film. Reading this number as
 * the render gain overstates it by 9.5 dB and makes the headroom look far worse
 * than it is - which is a mistake worth naming, because it has been made.
 *
 * Two channel-count terms move it further still, and in the other direction for
 * a narrow source: a stereo album gets neither the downmix match, there being no
 * downmix, nor the summing match a wide layout gets, so the same -3 reaches the
 * renderer as about +1.8. See WriteConfig, where both terms are derived. This
 * default is the number for 5.1, where both terms are zero by construction.
 *
 * Chosen to match that fold rather than to satisfy the limiter: what this
 * render has to sound like is Kodi's own downmix of the same film, at the same
 * loudness, so that turning the feature on is not also turning the volume up.
 *
 * The limiter below is the wrong thing to rely on, so the headroom is checked
 * rather than assumed. CAELimiter has no attack: the sample that exceeds full
 * scale is the sample the gain drops on, instantly, and it then holds 25ms and
 * releases over 100. A gain step that abrupt is broadband distortion, worst at
 * the top of the band. So it is meant to catch the rare transient, not to run
 * the level.
 *
 * Six Atmos streams rendered through this configuration at the gain this
 * default actually produces peaked at -5.27, -11.27, -9.04, -7.21, -6.49 and
 * +0.84 dBFS. Only the last goes over at all, on 0.006% of its samples - a
 * demo-disc torture clip, and the rare transient the limiter exists for. On
 * the device the limiter has not been seen to engage at this setting.
 *
 * Turning "maintain original volume" on removes the correction from both this
 * path and the matrix downmix together, so both sit 9.5 dB hotter and both are
 * likelier to reach their limiter. That is what the setting means, and the two
 * paths moving together is the reason to match the fold in the first place.
 *
 * Only the fallback for a settings component that is not there: the number
 * normally comes from the setting, whose default is the same.
 */
constexpr double OMNI_LEVEL_DB = -3.0;

//! The range the level setting declares - see settings.xml.
constexpr double OMNI_LEVEL_MIN_DB = -20.0;
constexpr double OMNI_LEVEL_MAX_DB = 10.0;

/*!
 * \brief How much louder the LFE channels are asked to be, in dB.
 *
 * Unity by default, which is what the render has always done: the engine feeds
 * the LFE to both ears without the +10 dB the channel is monitored with in a
 * cinema, matching the untouched routing a speaker layout would give it. The
 * setting exists because that is the one level a listener cannot compensate for
 * anywhere else - there is no LFE speaker on a pair of headphones to turn up,
 * and the level control above moves the whole render together.
 *
 * The ceiling is the +10 dB monitoring convention rather than an arbitrary
 * limit, so the top of the slider restores that relationship rather than
 * exceeding it.
 *
 * Half-decibel steps: the engine carries a bed entry's gain as a float with
 * 0.1 dB resolution, so a fraction survives the whole way to the render gain
 * rather than being rounded off somewhere the listener could not see it happen.
 * Half of a dB is the finer end of what is audible on this one channel, and
 * twenty steps is still a slider that can be dragged to a particular value.
 *
 * Only the fallback for a settings component that is not there: the number
 * normally comes from the setting, whose default is the same.
 */
constexpr double OMNI_LFE_DB = 0.0;

//! The range the LFE setting declares - see settings.xml.
constexpr double OMNI_LFE_MIN_DB = 0.0;
constexpr double OMNI_LFE_MAX_DB = 10.0;

//! Defaults matching settings.xml, used only when the settings component is
//! not there to ask.
constexpr double OMNI_DISTANCE_M = 2.0;
constexpr double OMNI_DISTANCE_MIN_M = 1.0;
constexpr double OMNI_DISTANCE_MAX_M = 6.0;
constexpr int OMNI_REVERB_PERCENT = 10;

//! The room presets - see RoomFor. Order matches the options in settings.xml.
enum RoomPreset
{
  ROOM_OFF = 0,
  ROOM_SMALL = 1,
  ROOM_MEDIUM = 2,
  ROOM_LARGE = 3,
};

//! Shoebox dimensions in metres; a zero width means no room simulation.
struct Room
{
  double width;
  double depth;
  double height;
};

/*!
 * \brief The shoebox each preset stands for.
 *
 * Three rooms rather than a pair of dimension sliders, because what a listener
 * can judge is whether the space sounds right, not whether it is 4.2 m across.
 * Carried over unchanged from the in-process renderer, where they were tuned.
 */
const Room& RoomFor(int preset)
{
  static const Room rooms[] = {
      {0.0, 0.0, 0.0}, // off
      {3.0, 3.5, 2.4}, // small
      {4.0, 5.0, 2.7}, // medium
      {6.0, 8.0, 3.2}, // large
  };
  if (preset < ROOM_OFF || preset > ROOM_LARGE)
    preset = ROOM_MEDIUM;
  return rooms[preset];
}

void PutU32(uint8_t* p, uint32_t v)
{
  std::memcpy(p, &v, sizeof(v));
}

uint32_t GetU32(const uint8_t* p)
{
  uint32_t v;
  std::memcpy(&v, p, sizeof(v));
  return v;
}

int64_t GetI64(const uint8_t* p)
{
  int64_t v;
  std::memcpy(&v, p, sizeof(v));
  return v;
}
} // namespace

//==============================================================================
// CHelper
//==============================================================================

CDVDAudioCodecOmniphony::CHelper::CHelper() : CThread("OmniphonyPump")
{
}

CDVDAudioCodecOmniphony::CHelper::~CHelper()
{
  Stop();
}

bool CDVDAudioCodecOmniphony::CHelper::Start(const std::string& exe)
{
  int toChild[2];
  int fromChild[2];
  if (pipe(toChild) != 0)
    return false;
  if (pipe(fromChild) != 0)
  {
    close(toChild[0]);
    close(toChild[1]);
    return false;
  }

  const pid_t pid = fork();
  if (pid < 0)
  {
    close(toChild[0]);
    close(toChild[1]);
    close(fromChild[0]);
    close(fromChild[1]);
    return false;
  }

  if (pid == 0)
  {
    dup2(toChild[0], STDIN_FILENO);
    dup2(fromChild[1], STDOUT_FILENO);
    close(toChild[0]);
    close(toChild[1]);
    close(fromChild[0]);
    close(fromChild[1]);
    // A helper that inherited Kodi's SIGPIPE disposition would survive us
    // closing the pipe; restore the default so it dies with the read end.
    signal(SIGPIPE, SIG_DFL);
    execl(exe.c_str(), exe.c_str(), static_cast<char*>(nullptr));
    _exit(127);
  }

  close(toChild[0]);
  close(fromChild[1]);
  m_pid = pid;
  m_in = toChild[1];
  m_out = fromChild[0];
  fcntl(m_in, F_SETFL, O_NONBLOCK);
  fcntl(m_out, F_SETFL, O_NONBLOCK);

  // Last, so the loop cannot see a half-built helper. Everything it touches is
  // set above and nothing changes it again until Stop has joined the thread.
  Create();
  return true;
}

void CDVDAudioCodecOmniphony::CHelper::Reap()
{
  if (m_pid <= 0)
    return;

  // Give it a moment to leave on its own, then insist. Either way the child is
  // waited for, so nothing is left as a zombie.
  for (int i = 0; i < 50; ++i)
  {
    int status = 0;
    const pid_t r = waitpid(m_pid, &status, WNOHANG);
    if (r == m_pid || (r < 0 && errno == ECHILD))
    {
      m_pid = -1;
      return;
    }
    usleep(2000);
  }

  kill(m_pid, SIGKILL);
  int status = 0;
  waitpid(m_pid, &status, 0);
  m_pid = -1;
}

void CDVDAudioCodecOmniphony::CHelper::Stop()
{
  // Before the descriptors go: the loop polls them, and a closed descriptor is
  // reused by the next thing this process opens.
  StopThread(true);

  if (m_in >= 0)
  {
    // Closing our write end is what tells the helper to leave; it treats a
    // clean end of stdin as a shutdown rather than an error.
    close(m_in);
    m_in = -1;
  }
  Reap();
  if (m_out >= 0)
  {
    close(m_out);
    m_out = -1;
  }
  m_pending.clear();
  m_pendingSent = 0;
  m_acc.clear();
  m_ready = Rendered{};
  m_readyFrames = 0;
}

bool CDVDAudioCodecOmniphony::CHelper::Send(uint8_t op, const void* payload, size_t len)
{
  std::unique_lock<CCriticalSection> lock(m_lock);

  if (m_broken || m_in < 0)
    return false;
  if (m_pending.size() - m_pendingSent + OMNI_HDR_LEN + len > OMNI_MAX_PENDING)
  {
    m_broken = true;
    return false;
  }

  uint8_t hdr[OMNI_HDR_LEN] = {'O', 'M', 'N', 'C'};
  hdr[4] = op;
  hdr[5] = 0;
  hdr[6] = 0;
  hdr[7] = 0;
  PutU32(hdr + 8, static_cast<uint32_t>(len));
  PutU32(hdr + 12, 0);

  m_pending.insert(m_pending.end(), hdr, hdr + OMNI_HDR_LEN);
  if (len)
  {
    const uint8_t* p = static_cast<const uint8_t*>(payload);
    m_pending.insert(m_pending.end(), p, p + len);
  }
  return true;
}

bool CDVDAudioCodecOmniphony::CHelper::ParseFrames()
{
  std::unique_lock<CCriticalSection> lock(m_lock);

  size_t off = 0;
  bool produced = false;
  while (m_acc.size() - off >= OMNI_HDR_LEN)
  {
    const uint8_t* h = m_acc.data() + off;
    if (std::memcmp(h, "OMNI", 4) == 0)
    {
      const uint32_t frames = GetU32(h + 4);
      const size_t bytes = static_cast<size_t>(frames) * OMNI_OUT_CHANNELS * sizeof(float);
      if (m_acc.size() - off < OMNI_HDR_LEN + bytes)
        break;
      // A block of no frames is the helper saying a packet decoded to nothing.
      // Queuing it would hand CVideoPlayerAudio a frame it divides by.
      if (frames)
      {
        const int64_t pts = GetI64(h + 8);
        // See m_lastPts: the engine's clock going backwards is the seek, and
        // everything up to it was rendered from where the film used to be.
        //
        // Not equal to it either, which is the case that bites. The clock
        // restarts at exactly zero, so a seek taken while the only block ever
        // rendered was the first one - pts zero - leaves nothing to step back
        // from, and a strict comparison never fires: the drop stays armed and
        // every block after it is discarded until the next seek happens to
        // clear it. Two scrubs in quick succession reach the same state.
        // Within a stream the clock is strictly increasing, since it counts
        // output samples and a block of none never gets here, so equality can
        // only mean it restarted.
        if (m_dropping && pts <= m_lastPts)
          m_dropping = false;
        m_lastPts = pts;

        if (!m_dropping)
        {
          // Copy rather than cast: a status payload carries an arbitrary byte
          // count, so the block after one starts wherever that count leaves it
          // and the samples need not be float-aligned. Reading them through a
          // float* would be undefined however well it happens to work here.
          const size_t oldSize = m_ready.pcm.size();
          m_ready.pcm.resize(oldSize + static_cast<size_t>(frames) * OMNI_OUT_CHANNELS);
          std::memcpy(m_ready.pcm.data() + oldSize, h + OMNI_HDR_LEN, bytes);
          m_ready.frames.push_back(frames);
          m_ready.enginePts.push_back(pts);
          m_readyFrames += frames;
          produced = true;
        }
      }
      off += OMNI_HDR_LEN + bytes;
    }
    else if (std::memcmp(h, "OMNS", 4) == 0)
    {
      const uint32_t len = GetU32(h + 8);
      if (m_acc.size() - off < OMNI_HDR_LEN + len)
        break;
      m_messages.emplace_back(reinterpret_cast<const char*>(h + OMNI_HDR_LEN), len);
      off += OMNI_HDR_LEN + len;
    }
    else
    {
      // A private protocol between our own processes: resynchronising here
      // would silently accept corruption, so this is fatal instead.
      m_broken = true;
      return false;
    }
  }

  if (off)
    m_acc.erase(m_acc.begin(), m_acc.begin() + off);
  if (produced)
    m_produced.Set();
  return true;
}

void CDVDAudioCodecOmniphony::CHelper::Process()
{
  // Both directions every time round: the helper blocks on its own writes, so
  // pushing input without draining output is how this deadlocks.
  while (!m_bStop && !m_broken)
  {
    bool haveWork;
    bool room;
    {
      std::unique_lock<CCriticalSection> lock(m_lock);
      haveWork = m_in >= 0 && m_pendingSent < m_pending.size();
      room = m_readyFrames < OMNI_PUMP_HOLD_FRAMES;
    }

    struct pollfd fds[2];
    int n = 0;

    // Not reading is how the helper is told to stop: its pipe fills, its write
    // blocks, and it stops decoding until there is somewhere to put the result.
    int outIdx = -1;
    if (room)
    {
      outIdx = n;
      fds[n].fd = m_out;
      fds[n].events = POLLIN;
      fds[n].revents = 0;
      n++;
    }

    int inIdx = -1;
    if (haveWork)
    {
      inIdx = n;
      fds[n].fd = m_in;
      fds[n].events = POLLOUT;
      fds[n].revents = 0;
      n++;
    }

    if (n == 0)
    {
      // Nothing to wait on at all - the hold is full and there is nothing to
      // send. Sleep the same slice poll would have.
      CThread::Sleep(std::chrono::milliseconds(OMNI_PUMP_IDLE_MS));
      continue;
    }

    const int rc = poll(fds, n, OMNI_PUMP_IDLE_MS);
    if (rc < 0)
    {
      if (errno == EINTR)
        continue;
      m_broken = true;
      break;
    }
    if (rc == 0)
      continue;

    if (outIdx >= 0 && (fds[outIdx].revents & POLLIN))
    {
      uint8_t buf[65536];
      const ssize_t got = read(m_out, buf, sizeof(buf));
      if (got > 0)
      {
        m_acc.insert(m_acc.end(), buf, buf + got);
        if (!ParseFrames())
          break;
      }
      else if (got == 0)
      {
        m_broken = true; // the helper closed stdout: it has gone
        break;
      }
      else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)
      {
        m_broken = true;
        break;
      }
    }
    else if (outIdx >= 0 && (fds[outIdx].revents & (POLLHUP | POLLERR)))
    {
      m_broken = true;
      break;
    }

    if (inIdx >= 0 && (fds[inIdx].revents & POLLOUT))
    {
      std::unique_lock<CCriticalSection> lock(m_lock);
      const ssize_t put =
          write(m_in, m_pending.data() + m_pendingSent, m_pending.size() - m_pendingSent);
      if (put > 0)
      {
        m_pendingSent += static_cast<size_t>(put);
        if (m_pendingSent == m_pending.size())
        {
          m_pending.clear();
          m_pendingSent = 0;
        }
        // A helper that stays behind never lets the queue empty, and what has
        // already gone out was then kept for as long as that lasted - the
        // limit on the queue counts only what is still waiting. Dropped once
        // there is at least as much of it as is waiting, so no byte is moved
        // more often than a byte is written.
        else if (m_pendingSent >= (1u << 18) && m_pendingSent >= m_pending.size() - m_pendingSent)
        {
          m_pending.erase(m_pending.begin(),
                          m_pending.begin() + static_cast<std::ptrdiff_t>(m_pendingSent));
          m_pendingSent = 0;
        }
      }
      else if (put < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)
      {
        m_broken = true;
        break;
      }
    }
    else if (inIdx >= 0 && (fds[inIdx].revents & (POLLHUP | POLLERR)))
    {
      m_broken = true;
      break;
    }
  }

  // Whoever is waiting on a block is waiting for one that is never coming.
  if (m_broken)
    m_produced.Set();
}

bool CDVDAudioCodecOmniphony::CHelper::Collect(Rendered& out, int timeoutMs)
{
  if (timeoutMs > 0)
  {
    bool wait;
    {
      std::unique_lock<CCriticalSection> lock(m_lock);
      wait = m_ready.frames.empty() && !m_broken;
    }
    if (wait)
      m_produced.Wait(std::chrono::milliseconds(timeoutMs));
  }

  std::unique_lock<CCriticalSection> lock(m_lock);
  if (!m_ready.frames.empty())
  {
    out.pcm.insert(out.pcm.end(), m_ready.pcm.begin(), m_ready.pcm.end());
    out.frames.insert(out.frames.end(), m_ready.frames.begin(), m_ready.frames.end());
    out.enginePts.insert(out.enginePts.end(), m_ready.enginePts.begin(), m_ready.enginePts.end());
    // Emptied, not replaced: this happens several times a second and the
    // storage it has grown into is exactly the storage it needs next time.
    m_ready.pcm.clear();
    m_ready.frames.clear();
    m_ready.enginePts.clear();
    m_readyFrames = 0;
  }
  return !m_broken;
}

size_t CDVDAudioCodecOmniphony::CHelper::Queued()
{
  std::unique_lock<CCriticalSection> lock(m_lock);
  return m_pending.size() - m_pendingSent;
}

void CDVDAudioCodecOmniphony::CHelper::Resync()
{
  std::unique_lock<CCriticalSection> lock(m_lock);
  m_ready.pcm.clear();
  m_ready.frames.clear();
  m_ready.enginePts.clear();
  m_readyFrames = 0;
  m_messages.clear();
  // Only if there is a clock to have gone backwards from. Before the first
  // block there is nothing stale in flight and nothing to recognise it by.
  m_dropping = m_lastPts >= 0;
}

std::vector<std::string> CDVDAudioCodecOmniphony::CHelper::TakeMessages()
{
  std::unique_lock<CCriticalSection> lock(m_lock);
  std::vector<std::string> taken;
  taken.swap(m_messages);
  return taken;
}

//==============================================================================
// CDVDAudioCodecOmniphony
//==============================================================================

CDVDAudioCodecOmniphony::CDVDAudioCodecOmniphony(CProcessInfo& processInfo)
  : CDVDAudioCodec(processInfo), m_processInfo(processInfo)
{
}

CDVDAudioCodecOmniphony::~CDVDAudioCodecOmniphony()
{
  Dispose();
}

std::string CDVDAudioCodecOmniphony::HelperPath()
{
  return CSpecialProtocol::TranslatePath("special://xbmcbin/omniphony/omniphony-helper");
}

std::string CDVDAudioCodecOmniphony::LayoutPath()
{
  // Twelve spatialized positions. Only consulted in cascaded mode - direct
  // convolves the objects themselves and never looks at a layout, which is why
  // its cost was identical across two different layouts when measured.
  return CSpecialProtocol::TranslatePath("special://xbmcbin/omniphony/cascade-12.yaml");
}

std::string CDVDAudioCodecOmniphony::ConfigPath()
{
  // Beside the staged HRTF, in the directory this feature already owns.
  //
  // It used to sit in special://temp, which put a YAML file next to kodi.log
  // and had at least one person read it as a log. The folder name is the only
  // thing telling a reader whose the file is, so it may as well say so.
  //
  // Neither location makes it a file to edit: it is emitted whole from the
  // settings every time a stream opens, and twice if the render mode settles
  // the other way, so an edit survives until the next play. That is also why
  // it is not written to the top of userdata beside guisettings.xml, where it
  // would read as something the user maintains.
  return CSpecialProtocol::TranslatePath("special://masterprofile/omniphony/render.yaml");
}

/*!
 * \brief Emit the engine's config for this stream.
 *
 * The engine takes its render parameters from a YAML file, and the C ABI has no
 * runtime equivalent - orender_set_option defines no keys at this ABI and
 * answers -1 to everything. So a setting that is going to reach the renderer
 * has to be written here, before the helper opens it.
 *
 * The file is owned completely rather than merged, so no YAML parser is needed
 * on this side - only string emission.
 */
bool CDVDAudioCodecOmniphony::WriteConfig(const std::string& bridge) const
{
  const auto settings = CServiceBroker::GetSettingsComponent();

  int room = ROOM_MEDIUM;
  double distance = OMNI_DISTANCE_M;
  int reverb = OMNI_REVERB_PERCENT;
  if (settings)
  {
    room = settings->GetSettings()->GetInt(CSettings::SETTING_AUDIOOUTPUT_OMNIPHONYROOM);
    distance = std::clamp(
        settings->GetSettings()->GetNumber(CSettings::SETTING_AUDIOOUTPUT_OMNIPHONYDISTANCE),
        OMNI_DISTANCE_MIN_M, OMNI_DISTANCE_MAX_M);
    reverb = std::clamp(
        settings->GetSettings()->GetInt(CSettings::SETTING_AUDIOOUTPUT_OMNIPHONYREVERB), 0, 100);
  }

  // Empty unless the listener has supplied their own measurement and it
  // survived staging - see COmniphonyHrtf, and StageIfChanged below for when.
  const std::string sofa = ActiveAE::COmniphonyHrtf::StagedPath();

  /*
   * One setting, two terms that depend on the channel count, so that -3 dB
   * means the same loudness whatever the film turns out to be.
   *
   * What this render has to sound like is Kodi's own stereo fold of the same
   * material, at the same loudness, so that turning the feature on is not also
   * turning the volume up.
   *
   * The first term matches Kodi's downmix normalisation: with "maintain
   * original volume" off the matrix downmix is normalised, a measured 9.5 dB
   * quieter for 5.1 to stereo. None of that reaches here - the flag goes to a
   * resampler this path does not use - so it is applied to the render instead.
   * Matching it is also what keeps the limiter idle: left at full level against
   * a normalised downmix the render would sit 9.5 dB hot and the limiter would
   * be working on almost every film rather than on the rare transient.
   *
   * The second term matches the render's own summing gain, which grows with the
   * source count where a normalised fold does not - but only downwards, for
   * layouts wider than the reference. Below it the term was an extrapolation
   * and listening went against it, so a narrow layout takes the first term
   * alone and lands exactly where 5.1 does.
   *
   * Both are derived in OmniphonyPcmLevelMatch, which is where the rules and
   * their limits are written out and where they are tested. Only the layout is
   * decided here, and only the PCM path has one to offer: the object path's
   * loudness follows the object count, which nothing knows at open and which no
   * channel formula describes, so it keeps exactly the behaviour it has always
   * had - the full normalisation match, no summing term.
   *
   * The layout comes from the decoder rather than from the demuxer's hint. It
   * is still only what is known before anything has been decoded, which is all
   * the engine's config can be written from, but ffmpeg has already reconciled
   * the hint's mask and count by then and defaulted a layout where the mask was
   * missing. Where even that is unknown the match falls back to the reference
   * layout, which is also the correction the object path always takes.
   */
  uint64_t sourceMask = 0;
  int sourceChannels = 0;
  if (m_pcm)
    m_pcm->OpenLayout(sourceMask, sourceChannels);

  const OmniphonyLevelMatch match = OmniphonyPcmLevelMatch(sourceMask, sourceChannels);
  const double matchA = match.downmixDb;
  const double matchB = match.summingDb;

  // Clamped to the range the setting declares rather than trusted: a profile
  // written before this existed takes the declared default, but a hand-edited
  // one need not be in range. The two match terms are applied after the
  // clamp, because they are corrections rather than anything a listener chose.
  double level = OMNI_LEVEL_DB;
  if (settings)
  {
    level = std::clamp(
        settings->GetSettings()->GetNumber(CSettings::SETTING_AUDIOOUTPUT_OMNIPHONYLEVEL),
        OMNI_LEVEL_MIN_DB, OMNI_LEVEL_MAX_DB);
    level += matchB;
    if (!settings->GetSettings()->GetBool(CSettings::SETTING_AUDIOOUTPUT_MAINTAINORIGINALVOLUME))
      level -= matchA;
  }

  if (m_pcm)
    CLog::Log(LOGDEBUG,
              "CDVDAudioCodecOmniphony: {} source channels, level {:.2f} dB "
              "(downmix match {:.2f}, summing match {:+.2f})",
              sourceChannels, level, matchA, matchB);

  // The LFE trim is deliberately not folded into the level above. That one
  // matches the render to Kodi's own stereo fold and moves the whole mix; this
  // one changes the LFE against the rest of it, which is the only reason to
  // have a second number at all. Clamped for the same reason as the level.
  double lfe = OMNI_LFE_DB;
  if (settings)
  {
    lfe =
        std::clamp(settings->GetSettings()->GetNumber(CSettings::SETTING_AUDIOOUTPUT_OMNIPHONYLFE),
                   OMNI_LFE_MIN_DB, OMNI_LFE_MAX_DB);
  }

  std::string yaml = "render:\n";
  yaml += "  bridge_path: \"" + bridge + "\"\n";
  yaml += "  master_gain: " + StringUtils::Format("{:.2f}", level) + "\n";
  // The LFE trim rides on the generic placement layout's per-channel gain,
  // which the engine stamps onto the channel's render gain - summed with whatever gain the
  // stream itself carries - for a direct-routed channel as much as a
  // spatialized one. In binaural there is no LFE speaker to trim, so this is
  // the only thing that reaches it.
  //
  // Only the LFE rows are written. A bed entry is looked up per channel label
  // and a label with no entry keeps its built-in pose and unity gain, so naming
  // these two leaves every other channel exactly as the engine would place it.
  // `spatialize: false` is not decoration: an entry defaults it to true, and
  // omitting it would move the LFE off its direct route onto the panner.
  //
  // LFE2 mirrors LFE rather than being left out. It is a real second LFE - an
  // OAMD speaker label the decoder can emit - and near-nonexistent in practice;
  // mirroring costs one line and stops the one stream that does carry it from
  // having half its sub-bass trimmed and half not.
  //
  // Written at unity too rather than omitted there, so the file states the
  // level outright instead of leaning on the engine's default matching ours.
  // Do not set a generic placement mode. Each source family then keeps its
  // upstream default (DTS/Auro Sphere; Dolby/PCM/generic Room), while all of
  // them inherit these two direct-routed LFE entries. Writing the legacy
  // virtual_bed key would migrate to generic Manual and override those family
  // defaults.
  yaml += "  placement:\n";
  yaml += "    generic:\n";
  yaml += "      layout:\n";
  yaml += "        speakers:\n";
  const std::string lfe_db = StringUtils::Format("{:.1f}", lfe);
  yaml += "          - { name: LFE, coord_mode: cartesian, x: 0, y: 1, z: 0, "
          "spatialize: false, gain_db: " +
          lfe_db + " }\n";
  yaml += "          - { name: LFE2, coord_mode: cartesian, x: 0, y: 1, z: 0, "
          "spatialize: false, gain_db: " +
          lfe_db + " }\n";
  // Deliberately off: it is a one-way reduction of the master gain that never
  // comes back, so one loud transient would quieten everything after it. The
  // limiter in GetData does this job instead, and releases.
  yaml += "  auto_gain: false\n";
  yaml += "  binaural:\n";
  yaml += "    output_mode: binaural\n";
  if (m_mode == RenderMode::Cascade)
    yaml += "    mode: cascaded\n";
  if (sofa.empty())
  {
    yaml += "    hrir_source: saf\n";
  }
  else
  {
    yaml += "    hrir_source: sofa\n";
    yaml += "    hrtf_sofa_path: \"" + sofa + "\"\n";
  }
  yaml += "    unit_scale_m: " + StringUtils::Format("{:.2f}", distance) + "\n";
  // Not exposed: the head model and the wall absorption are not things a
  // listener can judge by ear in isolation, and the tuned values are better
  // than a guess. The same reasoning the in-process renderer used.
  yaml += "    head_radius_m: 0.0875\n";
  yaml += "    air_absorption: true\n";
  // A measured set carries the colouration of the head it was measured on -
  // the engine puts the embedded KEMAR's own diffuse-field response at 9 dB
  // between 300 Hz and 12 kHz. A loudspeaker listener's ears imprint that on
  // everything and the brain discounts it; on headphones it is heard on top of
  // the listener's own, as timbre rather than as space. Dividing the set by
  // that response is the standard remedy, and the engine leaves it off only
  // because it cannot know it is feeding headphones. This path always is.
  //
  // Free, and safe for the level: the filter is folded into every kernel at
  // build time rather than run per sample, both ears get the same one so every
  // interaural difference survives intact, and it is applied before the set is
  // level-normalised - so the loudness the binaural level setting is matched
  // against does not move.
  yaml += "    diffuse_field_eq: true\n";

  const Room& r = RoomFor(room);
  if (r.width <= 0.0)
  {
    yaml += "    reflections: { enabled: false }\n";
  }
  else
  {
    yaml += "    reflections: { enabled: true, room_width_m: " +
            StringUtils::Format("{:.2f}", r.width) +
            ", room_depth_m: " + StringUtils::Format("{:.2f}", r.depth) + ",\n";
    yaml += "                   room_height_m: " + StringUtils::Format("{:.2f}", r.height) +
            ", level: 0.5 }\n";
  }

  // Reverb is the tail, reflections are the room; the tail without the room
  // sounds like an effect rather than a place, so it follows the room away.
  if (reverb <= 0 || r.width <= 0.0)
  {
    yaml += "    reverb: { enabled: false }\n";
  }
  else
  {
    yaml += "    reverb: { enabled: true, level: " + StringUtils::Format("{:.2f}", reverb / 100.0) +
            ", rt60_s: 0.35, predelay_ms: 20 }\n";
  }

  // The HRTF staging creates this directory too, but only when a personal head
  // model is actually chosen - which most streams will not have done.
  const std::string dir = "special://masterprofile/omniphony/";
  if (!XFILE::CDirectory::Exists(dir) && !XFILE::CDirectory::Create(dir))
  {
    CLog::Log(LOGERROR, "CDVDAudioCodecOmniphony: could not create {}", dir);
    return false;
  }

  const std::string path = ConfigPath();
  FILE* f = fopen(path.c_str(), "wb");
  if (!f)
  {
    CLog::Log(LOGERROR, "CDVDAudioCodecOmniphony: cannot write {}", path);
    return false;
  }
  const bool ok = fwrite(yaml.data(), 1, yaml.size(), f) == yaml.size();
  fclose(f);
  return ok;
}

const char* CDVDAudioCodecOmniphony::CodecId(AVCodecID codec)
{
  switch (codec)
  {
    case AV_CODEC_ID_AC3:
      return "ac3";
    case AV_CODEC_ID_EAC3:
      return "eac3";
    case AV_CODEC_ID_TRUEHD:
      return "truehd";
    case AV_CODEC_ID_DTS:
      return "dts";
    default:
      return nullptr;
  }
}

bool CDVDAudioCodecOmniphony::StartHelper(CDVDStreamInfo& hints)
{
  // The object bridge needs the codec to interpret undecoded bytes. The PCM
  // bridge needs the original codec for a different reason: it already gets
  // exact channel labels in OPCM, while input_codec selects the upstream
  // Dolby/DTS/PCM placement family. Keep that fact in OPEN/configuration rather
  // than consuming OPCM's reserved byte or changing its wire format.
  const char* codec = m_pcm ? OmniphonyPcmCodecId(hints.codec) : CodecId(hints.codec);
  if (!m_pcm && !codec)
    return false;

  const std::string exe = HelperPath();
  if (access(exe.c_str(), X_OK) != 0)
  {
    CLog::Log(LOGDEBUG, "CDVDAudioCodecOmniphony: no helper at {}", exe);
    return false;
  }

  m_helper = std::make_unique<CHelper>();
  if (!m_helper->Start(exe))
  {
    CLog::Log(LOGERROR, "CDVDAudioCodecOmniphony: could not start {}", exe);
    m_helper.reset();
    return false;
  }

  const std::string dir = CSpecialProtocol::TranslatePath("special://xbmcbin/omniphony");
  const std::string bridge = dir + (m_pcm ? "/libpcm_bridge.so" : "/libharletty_bridge.so");
  if (!WriteConfig(bridge))
  {
    m_helper.reset();
    return false;
  }

  std::string open = "lib=" + dir + "/liborender.so\n" + "bridge=" + bridge + "\n" +
                     "config=" + ConfigPath() + "\n";
  if (codec)
    open += "codec=" + std::string(codec) + "\n";
  if (m_mode == RenderMode::Cascade)
    open += "layout=" + LayoutPath() + "\n";

  // A helper that has just started has been given nothing, has rendered
  // nothing, and its bridge is waiting for a header - whichever bridge it is.
  ArmRecovery();
  m_headerSent = false;
  m_staging.clear();

  const size_t before = m_out.frames.size();
  if (!m_helper->Send(OP_OPEN, open.data(), open.size()) || !m_helper->Collect(m_out, 0))
  {
    CLog::Log(LOGERROR, "CDVDAudioCodecOmniphony: the helper refused to open the stream");
    m_helper.reset();
    return false;
  }
  AnchorNewBlocks(before);

  for (const auto& msg : m_helper->TakeMessages())
    CLog::Log(LOGDEBUG, "CDVDAudioCodecOmniphony: helper: {}", msg);

  return true;
}

void CDVDAudioCodecOmniphony::ArmRecovery()
{
  /*
   * Re-arm the no-output backstop, which every reset of the bridge must do.
   *
   * These two say "the renderer has been given audio and has produced none",
   * and GetData falls back when priming expires with both of them true. Latched
   * for the life of the helper they would answer for the film's opening seconds
   * and nothing after: a stream that plays, is seeked, and is then rejected for
   * every packet from the new position would be silent to the end, because
   * something had once rendered. A reset is exactly the boundary where that
   * history stops describing the present, so it is where the question is asked
   * again. The helper resets its own counterpart on the same command.
   */
  m_fed = false;
  m_rendered = false;
}

void CDVDAudioCodecOmniphony::AnchorNewBlocks(size_t before)
{
  if (m_pendingPts == DVD_NOPTS_VALUE || before >= m_out.enginePts.size())
    return;

  // The engine counts in microseconds and so does DVD time, so the engine's
  // number crosses into Kodi's units unchanged.
  m_anchor = m_pendingPts - static_cast<double>(m_out.enginePts[before]);
  m_pendingPts = DVD_NOPTS_VALUE;
}

void CDVDAudioCodecOmniphony::DropRendered()
{
  // The pump thread holds a reserve of its own and keeps filling it, so
  // clearing only what has reached here would leave a second of the old
  // position queued up behind it.
  if (m_helper)
    m_helper->Resync();

  m_out = CHelper::Rendered{};
  m_pcmConsumed = 0;
  // Every caller either restarts the helper or seeks it, and both restart the
  // engine's sample counter - so an anchor onto the old count is meaningless
  // and a held demux timestamp belongs to audio that will never arrive.
  m_anchor = DVD_NOPTS_VALUE;
  m_pendingPts = DVD_NOPTS_VALUE;
  // Every caller that goes on parsing resets the parser alongside; the PCM path
  // has none, and counts what it stages from here.
  m_timeline.Reset();
  m_parsed = 0;
  // Every caller has just emptied the renderer, so the bank has to be rebuilt
  // before the clock is allowed to run against it again. The cold-start size is
  // the safe default; Reset lowers it straight after, being the warm case.
  StartPriming(OMNI_PRIME_FRAMES);
}

void CDVDAudioCodecOmniphony::StartPriming(int frames)
{
  m_priming = true;
  m_primeFrames = frames;
  m_primeDeadline.Set(std::chrono::milliseconds(OMNI_PRIME_TIMEOUT_MS));
}

bool CDVDAudioCodecOmniphony::Collect(int timeoutMs)
{
  const size_t had = m_out.frames.size();
  if (!m_helper->Collect(m_out, timeoutMs))
    return false;
  // Latched until the bridge is next reset, not for the life of the helper:
  // what reads it asks whether the renderer is working *now*, and a reset is
  // where "now" starts again. See GetData, and ArmRecovery.
  if (m_out.frames.size() > had)
    m_rendered = true;
  AnchorNewBlocks(had);
  return true;
}

bool CDVDAudioCodecOmniphony::AwaitRoom()
{
  // CVideoPlayerAudio has exactly one throttle: it waits for the next demux
  // packet only as long as the sink already has audio to play (its Process()
  // passes m_audioSink.GetCacheTime() as the message queue timeout). A
  // synchronous decoder fills that sink on the way past, so the wait paces the
  // whole player. This codec answers later than it is asked, so on an empty
  // sink the wait is zero, the demuxer is drained as fast as storage allows,
  // and the helper is buried - in the field its queue reached OMNI_MAX_PENDING
  // in under a second and object audio died. Waiting here puts the throttle
  // back where the sink cannot.
  //
  // What it waits for is the input queue, and only the input queue. Waiting
  // for a block - one back per access unit in, which is what this did first -
  // matched the two rates exactly, and that turned out to be the problem: it
  // also stopped the renderer ever getting ahead, because the player only
  // feeds at the speed it plays. Whole films ran with nothing banked and every
  // dip in render rate audible.
  //
  // A full bank is not waited for at all - see AddData. It drains through
  // GetData, on this same thread, so a wait for it here could only ever wait
  // out the budget.
  XbmcThreads::EndTime<> budget{std::chrono::milliseconds(OMNI_FEED_BUDGET_MS)};
  while (m_helper->Queued() > OMNI_FEED_QUEUE_MAX && !budget.IsTimePast())
  {
    if (!Collect(OMNI_PUMP_SLICE_MS))
      return false;
  }
  return true;
}

void CDVDAudioCodecOmniphony::UpdateName()
{
  /*
   * CVideoPlayerAudio hands GetName() to CProcessInfo::SetAudioDecoderName,
   * which is what Player.Process(audiodecoder) shows. It answers the one
   * question that label asks - which decoder is running - and nothing else:
   * what the stream turned out to carry and how it is being rendered are
   * separate facts with infolabels of their own, and crowding them into this
   * one produced names like "omniphony-truehd-direct-15obj" that no other
   * decoder in Kodi resembles.
   *
   * The suffix is the ffmpeg decoder's own name rather than a table of our
   * own, so that "om-dca" and "ff-dca" are provably the same stream decoded
   * two ways. Taking it from ffmpeg is what makes that true by construction:
   * a table here would be a second spelling to keep in step, and it is the
   * decoder name rather than the codec name that differs (DTS decodes as
   * "dca"), which is exactly the sort of detail a hand-written table gets
   * wrong.
   */
  const AVCodec* decoder = m_hints ? avcodec_find_decoder(m_hints->codec) : nullptr;
  // Open() refuses anything ffmpeg has no decoder for - on the PCM path by
  // failing to open one - so the fallbacks below cannot be reached in practice.
  // They are here because concatenating a null pointer onto a std::string is
  // undefined, which is too sharp an edge to leave unguarded.
  const char* codec = decoder ? decoder->name : (m_hints ? CodecId(m_hints->codec) : nullptr);
  m_codecName = std::string("om-") + (codec ? codec : "?");
}

std::string OmniphonyDescribeHrir(const std::string& selector)
{
  // These two words match the setting's own option labels 39326 and 39327, but
  // they are written out rather than taken from them. The setting is
  // translated and this screen is not, and a label that changed language while
  // everything around it stayed English would look like a bug rather than a
  // courtesy.
  if (selector == "saf")
    return "Built-in";
  if (selector == "sofa")
    return "Personal";
  return {};
}

void CDVDAudioCodecOmniphony::ReadHrirReport(const std::string& msg)
{
  // "stream ... hrir=sofa ...": one word, ended by a space. A line without the
  // field - an older helper, or anything but a stream report - changes nothing.
  if (!StringUtils::StartsWith(msg, "stream "))
    return;
  const size_t at = msg.find(" hrir=");
  if (at == std::string::npos)
    return;
  const size_t from = at + 6;
  const size_t end = msg.find(' ', from);
  std::string sofa = OmniphonyDescribeHrir(
      msg.substr(from, end == std::string::npos ? std::string::npos : end - from));
  if (sofa != m_sofa)
  {
    m_sofa = std::move(sofa);
    m_infoDirty = true;
  }
}

std::string CDVDAudioCodecOmniphony::InputDescription() const
{
  /*
   * Objects, or nothing at all. Below zero means the helper has not reported
   * yet and zero means the soundtrack carries no objects, and neither has an
   * answer worth printing:
   *
   *  - The count is only truthful once a frame has been decoded, so before that
   *    anything said here would be a guess about the film that just started.
   *  - For channel-based audio there is no layout to report. CAEStreamInfo
   *    carries one unsigned int of channel information and it is a count, not a
   *    map - which is why the passthrough codec builds its on-screen layout by
   *    appending AE_CH_RAW once per channel and the screen reads "RAW, RAW,
   *    RAW...". Printing "6 Channels" here would add nothing that
   *    Player.Process(audiochannels) does not already say better.
   *
   * Empty rather than a placeholder so a skin can test IsEmpty and fall back to
   * its own layout label. This row exists to say what the object renderer was
   * handed; when it was handed no objects, it has nothing to say.
   *
   * On the PCM path there is a layout to report, and it is worth reporting for
   * the reason the second bullet gives: the channel row Kodi already has counts
   * the render's own two channels, so nothing on the screen would otherwise say
   * whether the film arrived as 5.1 or as stereo. The labels are the ones sent
   * to the bridge, in the order they were sent, so this row and the wire cannot
   * disagree. Empty until the first frame, which is the earliest anything true
   * can be said.
   */
  if (m_pcm)
    return OmniphonyPcmDescribe(m_pcm->Labels());

  if (m_objectCount <= 0)
    return {};

  // English, not a localised string, because the whole player process screen is
  // English: it sits beside "om-truehd", "48000" and "RAW, RAW, RAW", and not
  // one of the Player.Process labels in CPlayerGUIInfo is translated. A
  // translated word here would be the only one on the panel.
  std::string input = std::to_string(m_objectCount);
  input += m_objectCount == 1 ? " Object" : " Objects";
  if (!m_bed.empty())
  {
    // "15 Objects + LFE". The helper packs the bed without spaces so that it
    // cannot be mistaken for the end of the status line; they go back in here.
    std::string bed = m_bed;
    StringUtils::Replace(bed, ",", ", ");
    input += " + " + bed;
  }
  return input;
}

void CDVDAudioCodecOmniphony::PublishRenderInfo()
{
  // Building the strings is the part worth avoiding, and it only has to happen
  // when something changed: the open, the object count arriving, a switch to
  // the virtual layout.
  if (m_infoDirty)
  {
    m_infoDirty = false;
    m_input = InputDescription();
    m_render = m_mode == RenderMode::Cascade ? "Cascade 12" : "Direct";
  }

  /*
   * Writing them, though, happens on every block, because something else
   * empties them behind our back.
   *
   * CDataCacheCore::ResetAudioCache() assigns a default-constructed struct over
   * the whole of the audio player info, and ActiveAE calls it from Configure()
   * whenever the internal format changes - which is exactly what opening this
   * stream does. The order is fixed and unfavourable: this codec hands over its
   * first block and publishes, the player then opens the sink with that block's
   * format, ActiveAE reconfigures and wipes, and only afterwards - once the
   * sink is three quarters full - does CVideoPlayerAudio publish the decoder
   * name and channel count. So the stock fields come back and anything
   * published once, earlier, does not.
   *
   * Re-publishing each block is the cheap half of the fix and the honest one:
   * it heals from that reset without this codec having to know when it
   * happened, and from any other wipe nobody has found yet. Three short string
   * assignments under an uncontended lock, thirty or so times a second, against
   * a render that costs a third of a core.
   */
  m_processInfo.SetOmniphonyInput(m_input);
  m_processInfo.SetOmniphonyRender(m_render);
  m_processInfo.SetOmniphonySofa(m_sofa);
}

bool CDVDAudioCodecOmniphony::ReopenAs(RenderMode mode)
{
  if (!m_hints)
    return false;

  CLog::Log(LOGINFO,
            "CDVDAudioCodecOmniphony: {} objects is more than direct rendering can carry; "
            "restarting on the {}-speaker virtual layout",
            m_objectCount, 12);

  if (m_helper)
  {
    m_helper->Stop();
    m_helper.reset();
  }
  DropRendered();
  m_parser.Reset();
  m_backlog.clear();

  m_mode = mode;
  if (!StartHelper(*m_hints))
    return false;

  // The name does not change with the mode any more - Player.Process(omniphony.render)
  // carries that now - but the screen still has to be told the mode moved.
  m_infoDirty = true;
  return true;
}

bool CDVDAudioCodecOmniphony::Open(CDVDStreamInfo& hints, CDVDCodecOptions& options)
{
  /*
   * The routing, and the only place it happens.
   *
   * A codec the bridge can decode keeps the object path, byte for byte as it
   * was. Anything else is decoded here and sent as labelled PCM - which is
   * every codec ffmpeg has a decoder for, so the refusal below is ffmpeg's
   * rather than ours, and a stream it cannot decode falls through to the
   * factory's next candidate exactly as it did before.
   *
   * The objects setting is the second half of the test rather than a switch of
   * its own. Turning it off does not turn binaural off for an Atmos film; it
   * says place the channels this soundtrack was mixed into and skip the objects
   * - which is the consistent reading of a master that is still on, and the
   * cheaper render for a device that cannot keep up with the objects.
   */
  bool objects = true;
  if (const auto settings = CServiceBroker::GetSettingsComponent())
    objects = settings->GetSettings()->GetBool(CSettings::SETTING_AUDIOOUTPUT_OMNIPHONYOBJECTS);

  if (!objects || !CodecId(hints.codec))
  {
    auto pcm = std::make_unique<COmniphonyPcmSource>(m_processInfo);
    if (!pcm->Open(hints, options))
      return false;
    m_pcm = std::move(pcm);
  }

  m_hints = std::make_unique<CDVDStreamInfo>(hints);
  m_mode = RenderMode::Direct;
  m_modeSettled = false;
  m_modeForced = false;
  m_modeWindow.Set(std::chrono::milliseconds(OMNI_MODE_WINDOW_MS));
  m_objectCount = -1;
  m_bed.clear();
  m_parser.Reset();
  m_backlog.clear();
  DropRendered();
  m_failed = false;
  m_drained = false;
  m_reportedFallback = false;
  m_limiter.SetSamplerate(OMNI_OUT_RATE);
  m_limiter.Reset();

  if (const auto settings = CServiceBroker::GetSettingsComponent())
  {
    /*
     * Cascading, because the listener asked for it rather than because the
     * stream needs it.
     *
     * Marked forced rather than simply settled, so the object count is still
     * read and still reported on screen - it is only the automatic switch that
     * is skipped. It is worth having as a switch because cascading is the
     * cheaper mode on this hardware for the object counts we actually see -
     * measured 0.430 against 0.470 - and the render has almost no margin over
     * realtime, so a listener whose sound breaks up has something to try.
     */
    if (!m_pcm && settings->GetSettings()->GetBool(CSettings::SETTING_AUDIOOUTPUT_OMNIPHONYCASCADE))
    {
      m_mode = RenderMode::Cascade;
      m_modeForced = true;
    }

    /*
     * Before WriteConfig, which asks what came of it. Normally a no-op - it
     * only does work the first time a newly chosen file is used.
     *
     * Built-in stages the empty path, which clears the copy held in the
     * profile. That is what makes the choice reversible: WriteConfig asks
     * COmniphonyHrtf what is staged, so leaving an old copy in place would go
     * on using it however the setting read.
     */
    const bool personal =
        settings->GetSettings()->GetInt(CSettings::SETTING_AUDIOOUTPUT_OMNIPHONYHRTFMODE) ==
        ActiveAE::OMNI_HRTF_PERSONAL;
    const std::string chosen =
        personal ? settings->GetSettings()->GetString(CSettings::SETTING_AUDIOOUTPUT_OMNIPHONYHRTF)
                 : std::string();
    const auto result = ActiveAE::COmniphonyHrtf::StageIfChanged(chosen);
    if (result != ActiveAE::COmniphonyHrtf::Result::Ok)
      CLog::Log(LOGWARNING, "CDVDAudioCodecOmniphony: {} - rendering with the built-in head model",
                ActiveAE::COmniphonyHrtf::Explain(result));
  }

  /*
   * The staging above decides which file the engine is offered; whether it
   * could load it is the engine's to say. So the head model row says nothing
   * until the engine reports what it is convolving with - see ReadHrirReport.
   */
  m_sofa.clear();
  m_infoDirty = true;

  /*
   * A channel bed settles the mode at open, where an object stream cannot.
   *
   * The choice between the two modes is a choice about how many sources have to
   * be convolved, and the object path has to wait for a rendered frame to learn
   * that. Here it is known already and it is small: the widest layout this
   * source accepts is 7.1.4, at twelve, which is what Cascade would reduce
   * anything to anyway. So Direct is not merely affordable, it is the same
   * work without the panning error - and settling it here means the mode
   * window, the restart and the object-count reading are all skipped rather
   * than left to decide nothing.
   */
  if (m_pcm)
  {
    m_mode = RenderMode::Direct;
    m_modeSettled = true;
  }

  if (!StartHelper(hints))
    return false;

  m_format.m_dataFormat = AE_FMT_FLOAT;
  m_format.m_sampleRate = OMNI_OUT_RATE;
  m_format.m_channelLayout = CAEChannelInfo(AE_CH_LAYOUT_2_0);
  m_format.m_frameSize = sizeof(float) * OMNI_OUT_CHANNELS;

  UpdateName();
  // The mode is named here because nothing else in the log carries it: GetName
  // reaches the screen but never the log, so a log from the field could not be
  // read for which mode it ran in. The codec name comes from UpdateName rather
  // than from CodecId, which is null for everything the PCM path carries.
  CLog::Log(LOGINFO, "CDVDAudioCodecOmniphony: rendering {} to headphones out of process, {}{}",
            m_pcm ? m_codecName + " (decoded here)" : m_codecName + " objects",
            m_mode == RenderMode::Cascade ? "cascade-12" : "direct",
            m_modeForced ? " (pinned by setting)" : "");
  return true;
}

void CDVDAudioCodecOmniphony::Dispose()
{
  if (m_helper)
  {
    m_helper->Send(OP_CLOSE, nullptr, 0);
    m_helper->Stop();
    m_helper.reset();
  }
  if (m_pcm)
  {
    m_pcm->Dispose();
    m_pcm.reset();
  }
  if (m_fallback)
  {
    m_fallback->Dispose();
    m_fallback.reset();
  }
  m_staging.clear();
  free(m_buffer);
  m_buffer = nullptr;
  m_bufferSize = 0;
  m_dataSize = 0;
  m_backlog.clear();
  DropRendered();

  // Nothing is being rendered any more, so the three omniphony rows go away.
  // Safe to do unconditionally even when this teardown is the one caused by a
  // replacement codec opening: PublishRenderInfo waits for a block to be handed
  // out, and the replacement cannot have handed one out before this runs.
  ClearRenderInfo();
}

void CDVDAudioCodecOmniphony::ClearRenderInfo()
{
  // The cached copies as well as the screen, so that a later publish cannot put
  // back a description of a render that has stopped.
  m_infoDirty = false;
  m_input.clear();
  m_render.clear();
  m_sofa.clear();
  m_processInfo.SetOmniphonyInput({});
  m_processInfo.SetOmniphonyRender({});
  m_processInfo.SetOmniphonySofa({});
}

void CDVDAudioCodecOmniphony::FallBack(const char* why)
{
  if (m_failed)
    return;
  m_failed = true;

  CLog::Log(LOGERROR, "CDVDAudioCodecOmniphony: {} - falling back to software decode", why);

  if (m_helper)
  {
    // The helper's own account of what went wrong, which is otherwise lost:
    // Stop() takes the pipe with it, and nothing else drains these. On a
    // protocol disagreement this is the line that says which one.
    for (const auto& msg : m_helper->TakeMessages())
      CLog::Log(LOGERROR, "CDVDAudioCodecOmniphony: helper: {}", msg);
    m_helper->Stop();
    m_helper.reset();
  }
  DropRendered();
  m_staging.clear();
  // After DropRendered, which arms it: the software decoder fills the sink at
  // its own pace and there is no render left here to bank.
  m_priming = false;

  /*
   * On the PCM path the replacement is already here, already open on this
   * stream, and already holding the frame that provoked the switch.
   *
   * That is the whole reason this codec derives its source from
   * CDVDAudioCodecFFmpeg rather than owning one. Handing it over costs no
   * second decoder, no re-open, and - because COmniphonyPcmSource::GetData
   * serves a retained frame before receiving another - not one frame of audio
   * at the moment the listener is switched across. The caller must not offer
   * the packet again if this decoder has already taken it; AddPcmData carries
   * that argument.
   */
  if (m_pcm)
  {
    m_fallback = std::move(m_pcm);
    return;
  }

  // The player has no way to swap a codec on request, so the replacement lives
  // here. It is fed the demux packets, not our framed ones, because that is
  // what it expects.
  if (!m_hints)
    return;
  auto ffmpeg = std::make_unique<CDVDAudioCodecFFmpeg>(m_processInfo);
  CDVDCodecOptions options;
  if (ffmpeg->Open(*m_hints, options))
    m_fallback = std::move(ffmpeg);
  else
    CLog::Log(LOGERROR, "CDVDAudioCodecOmniphony: the software decoder would not open either");
}

bool CDVDAudioCodecOmniphony::DrainStaging()
{
  if (m_staging.empty())
    return true;

  const size_t frame = m_pcm ? std::max<size_t>(m_pcm->FrameSize(), 1) : 1;
  const size_t chunk = frame * OMNI_PCM_WRITE_FRAMES;

  size_t sent = 0;
  while (sent < m_staging.size())
  {
    /*
     * The brake, and the only reason this can return with work left over.
     *
     * Sending regardless would not remove the limit, it would move it: the
     * bytes would pile up in the helper's own input queue instead, where
     * OMNI_MAX_PENDING is fatal rather than merely full. Left here they cost
     * nothing and are sent as soon as the helper has taken what it has.
     */
    if (m_helper->Queued() > OMNI_FEED_QUEUE_MAX)
      break;

    const size_t len = std::min(chunk, m_staging.size() - sent);
    if (!m_helper->Send(OP_FEED, m_staging.data() + sent, len))
      return false;
    m_fed = true;
    sent += len;
  }

  if (sent == m_staging.size())
    m_staging.clear();
  else if (sent)
    m_staging.erase(m_staging.begin(), m_staging.begin() + sent);
  return true;
}

bool CDVDAudioCodecOmniphony::RestartBridge()
{
  CLog::Log(LOGDEBUG, "CDVDAudioCodecOmniphony: the stream changed shape - describing it again");

  /*
   * What is staged was converted for the geometry the bridge is about to stop
   * expecting, so it cannot be sent: after the reset those bytes would be read
   * as a header. The resampler's tail goes with it - Convert has already
   * replaced the resampler by the time a new header appears, so there is
   * nothing of the old one left to drain. That is a fraction of a millisecond
   * at a change of stream geometry.
   */
  m_staging.clear();

  // The bank goes for the same reason a seek's does: OP_RESET restarts the
  // engine's sample counter, so every timestamp already banked describes a
  // clock that will not exist a moment from now, and the anchor with them.
  DropRendered();
  ArmRecovery();
  m_headerSent = false;
  return m_helper->Send(OP_RESET, nullptr, 0);
}

bool CDVDAudioCodecOmniphony::StagePcm()
{
  for (;;)
  {
    std::vector<uint8_t> pcm;
    double pts = DVD_NOPTS_VALUE;
    if (!m_pcm->Convert(pcm, pts))
    {
      // False means both "the decoder has nothing ready" and "this stream
      // cannot be rendered", and only the second is an answer to anything.
      return !m_pcm->Unsupported();
    }

    if (m_pcm->HeaderPending())
    {
      // A header when one has already gone out is the stream changing shape
      // under us - a different layout, rate or sample format out of the same
      // decoder. The bridge parses one header and then streams, so it has to be
      // told to expect another before it can be given one.
      if (m_headerSent && !RestartBridge())
        return false;

      const std::vector<uint8_t>& header = m_pcm->Header();
      m_staging.insert(m_staging.end(), header.begin(), header.end());
      m_pcm->TakeHeader();
      m_headerSent = true;

      // The layout the screen shows comes from these labels, so the row is
      // rebuilt exactly when they are - at the first frame, and at any change
      // of geometry after it.
      m_infoDirty = true;
    }

    /*
     * The decoded frame's timestamp, not the packet's.
     *
     * A decoder answers later than it is asked - it holds packets while it has
     * nothing to emit and emits several from one - so the packet timestamp
     * reaching AddData describes input rather than the output about to be
     * staged. The anchor pairs a demuxer timestamp with the engine's own clock
     * and has to be exact, which is why m_anchor is taken once; taking it from
     * the wrong end of the decoder would put the audio clock ahead of the
     * picture by the decoder's latency for the rest of the film.
     *
     * The rest of the condition is the object path's, unchanged and for its
     * reason: a demux timestamp only describes the block coming out when
     * nothing is in flight between them, which is true once per stream and
     * once per seek.
     */
    if (pts != DVD_NOPTS_VALUE && m_pendingPts == DVD_NOPTS_VALUE && m_anchor == DVD_NOPTS_VALUE)
      m_pendingPts = pts;

    // Gaps are followed as the object path follows them, a block standing for
    // an access unit - see COmniphonyTimeline. Its timestamp is its own, so it
    // marks exactly the bytes it is fed as.
    const size_t frames = pcm.size() / std::max<size_t>(m_pcm->FrameSize(), 1);
    if (frames > 0)
    {
      m_timeline.Mark(m_parsed, pts);
      m_timeline.Feed(m_parsed, static_cast<double>(frames) * 1000000.0 / m_pcm->Rate());
      m_parsed += pcm.size();
    }

    m_staging.insert(m_staging.end(), pcm.begin(), pcm.end());
  }
}

bool CDVDAudioCodecOmniphony::AddPcmData(const DemuxPacket& packet)
{
  /*
   * The order of what follows is the whole of the no-replay contract.
   *
   * AddData's false means "this packet was not consumed, offer it again", and
   * CVideoPlayerAudio does exactly that. On the object path the refusal is
   * decided before the parser has touched anything, so it is always true. Here
   * ffmpeg takes the packet the moment it is offered, and a refusal afterwards
   * would have the player replay audio that was already decoded - not when
   * something goes wrong, but on every bank-full event during ordinary
   * playback. So everything that can refuse happens before the decoder is
   * offered anything, and nothing after it is allowed to refuse.
   */
  if (!DrainStaging())
  {
    FallBack("the helper stopped accepting data");
    return m_fallback ? m_fallback->AddData(packet) : false;
  }

  // Still holding audio the helper had no room for. Nothing of this packet has
  // been touched, so refusing it here is the honest kind.
  if (!m_staging.empty())
    return false;

  if (!m_priming && GetBufferSize() >= OMNI_BANK_FRAMES)
    return false;

  /*
   * From here the packet is inside the decoder, so every exit reports whether
   * ffmpeg took it and none of them re-offers it. That is safe even when the
   * exit is a fallback, because on this path FallBack hands over this very
   * decoder: whatever it swallowed, the fallback is holding.
   */
  const bool consumed = m_pcm->AddData(packet);

  if (!StagePcm())
  {
    const bool refused = m_pcm->Unsupported();
    FallBack(refused ? "this stream cannot be rendered binaurally"
                     : "the helper stopped accepting data");
    return consumed;
  }

  if (!DrainStaging())
  {
    FallBack("the helper stopped accepting data");
    return consumed;
  }

  // Only if the helper has stopped taking what it is being sent - the brake for
  // the case the bank cannot catch, see AwaitRoom.
  if (!AwaitRoom())
  {
    FallBack("the helper died mid-stream");
    return consumed;
  }

  // Logged, and read for the head model and nothing else. There are no objects
  // to count on this path, and no mode to choose from a count that will always
  // be zero.
  for (const auto& msg : m_helper->TakeMessages())
  {
    CLog::Log(LOGDEBUG, "CDVDAudioCodecOmniphony: helper: {}", msg);
    ReadHrirReport(msg);
  }

  return consumed;
}

bool CDVDAudioCodecOmniphony::AddData(const DemuxPacket& packet)
{
  if (m_fallback)
    return m_fallback->AddData(packet);
  // Nothing is left to decode this: the helper has gone and FallBack found no
  // software decoder to replace it. Refusing would have the player offer the
  // packet again forever, and hold the whole stream behind it; taking it lets
  // the film play on without sound.
  if (m_failed || !m_helper)
    return true;

  // Take what the pump thread has rendered since the last call, so the decision
  // below is made against the bank as it stands and not as it was.
  if (!Collect(0))
  {
    FallBack("the helper died mid-stream");
    return m_fallback ? m_fallback->AddData(packet) : false;
  }

  // Everything above is common to both paths - the fallback, the bank, the
  // clock. What a packet turns into is where they part.
  if (m_pcm)
    return AddPcmData(packet);

  /*
   * The bank is full - refuse the packet rather than wait for room.
   *
   * It only empties through GetData, which the player calls on this same
   * thread, so a wait here would be a wait for something that cannot happen
   * until this returns. Refusing is the player's own idiom for a decoder with
   * nowhere to put more: CVideoPlayerAudio puts the packet back, comes round
   * through GetData and the sink, and offers it again once it has taken some
   * audio off us. That is also what makes the reserve possible - the wait it
   * replaces is what used to hold the render down to the speed of playback.
   *
   * Not while priming, where nothing is handed over at all and refusing would
   * leave the player with neither audio nor anywhere to put the packet. The
   * bank cannot reach this while priming anyway: priming ends at
   * OMNI_PRIME_FRAMES, which is the smaller number.
   */
  if (!m_priming && GetBufferSize() >= OMNI_BANK_FRAMES)
    return false;

  /*
   * Held until blocks actually come back, then stamped onto the first of them -
   * and only while there is no anchor yet.
   *
   * That last part is what makes it truthful. A demux timestamp only describes
   * the block coming out if nothing is in flight between them, which is true
   * exactly once per stream and once per seek. With a reserve there is always
   * a second or more in flight afterwards, so a timestamp taken later reads
   * that far ahead of the audio it gets stamped on. Anchoring off one of those
   * put the audio clock 2.15 seconds ahead of the picture at the first frame
   * of every film.
   */
  if (packet.pts != DVD_NOPTS_VALUE && m_pendingPts == DVD_NOPTS_VALUE &&
      m_anchor == DVD_NOPTS_VALUE)
    m_pendingPts = packet.pts;

  // Reassemble complete access units the way the passthrough codec does, then
  // hand them straight to the helper. Anything the parser could not consume
  // stays here until the rest of it arrives.
  const uint8_t* data = packet.pData;
  int size = packet.iSize;
  if (!data || size <= 0)
    return true;

  // The packet starts after everything the parser has taken and whatever is
  // still waiting for it.
  m_timeline.Mark(m_parsed + m_backlog.size(), packet.pts);

  if (!m_backlog.empty())
  {
    m_backlog.insert(m_backlog.end(), data, data + size);
    data = m_backlog.data();
    size = static_cast<int>(m_backlog.size());
  }

  int offset = 0;
  while (offset < size)
  {
    m_dataSize = m_bufferSize;
    const unsigned int used = m_parser.AddData(const_cast<uint8_t*>(data) + offset, size - offset,
                                               &m_buffer, &m_dataSize);
    m_bufferSize = std::max(m_bufferSize, m_dataSize);
    if (used == 0)
      break;
    offset += static_cast<int>(used);
    m_parsed += used;

    if (m_dataSize)
    {
      if (!m_helper->Send(OP_FEED, m_buffer, m_dataSize))
      {
        FallBack("the helper stopped accepting data");
        return m_fallback ? m_fallback->AddData(packet) : false;
      }
      m_fed = true;
      // The unit just framed is the last thing to have left the parser, so it
      // ends where what the parser still holds begins.
      const uint64_t end = m_parsed - m_parser.GetBufferSize();
      m_timeline.Feed(end - m_dataSize, OmniphonyAccessUnitUs(m_parser.GetStreamInfo()));
    }
  }

  if (offset < size)
  {
    std::vector<uint8_t> rest(data + offset, data + size);
    m_backlog.swap(rest);
  }
  else
  {
    m_backlog.clear();
  }

  // Only if the helper has stopped taking what it is being sent - the brake for
  // the case the bank cannot catch, see AwaitRoom.
  if (!AwaitRoom())
  {
    FallBack("the helper died mid-stream");
    return m_fallback ? m_fallback->AddData(packet) : false;
  }

  for (const auto& msg : m_helper->TakeMessages())
  {
    CLog::Log(LOGDEBUG, "CDVDAudioCodecOmniphony: helper: {}", msg);

    // Before the object gate below: the head model is reported on the same
    // line whether or not the frame carried objects.
    ReadHrirReport(msg);

    // "stream objects=N spatial=S channels=C bed=L,R,LFE", sent whenever what
    // the engine is being handed changes. Not once: the ABI is explicit that
    // the object state is "a live, observable fact about the stream" that "may
    // flip in either direction mid-stream and must not be latched", and
    // orender_object_count reports the last rendered frame rather than the
    // stream. Reading only the first report is what made a resumed film show
    // nothing - see below.
    const size_t at = msg.find("objects=");
    if (at == std::string::npos)
      continue;

    const int objects = std::atoi(msg.c_str() + at + 8);

    /*
     * Zero is not "this soundtrack has no objects". It is "the frame just
     * rendered carried no object metadata", which the engine supports
     * deliberately - bed-only and pre-metadata frames render through the
     * channel path - and which is exactly what the first frames after a
     * mid-film resume tend to be. Treating that first zero as the answer left
     * the label empty for the rest of the film.
     *
     * So a zero report is not evidence of anything and is passed over, for the
     * render mode as much as for the screen. A soundtrack that genuinely
     * carries no objects reports zero forever, m_objectCount stays -1, and
     * InputDescription says nothing - which is the same outcome by a route
     * that cannot be confused with "we asked too early".
     */
    if (objects <= 0)
      continue;

    m_objectCount = objects;

    // Last on the line and free of spaces by construction, so the rest of the
    // line is the whole value. Absent from an older helper, which is why its
    // absence leaves m_bed empty rather than being treated as a broken message:
    // the object count on its own is still worth showing.
    const size_t bedAt = msg.find("bed=");
    if (bedAt != std::string::npos)
    {
      m_bed = msg.substr(bedAt + 4);
      StringUtils::Trim(m_bed);
    }

    m_infoDirty = true;

    // Everything above updates for the life of the stream. Everything below
    // happens once, because it is a different kind of decision.
    if (m_modeSettled)
      continue;

    /*
     * The mode is chosen from the first report that actually carries objects,
     * and only while the film is still starting.
     *
     * Both halves matter. Choosing from the first report of any kind is what
     * this used to do, and on a resume that report is a zero - so a stream
     * needing the virtual layout would have stayed on direct rendering.
     * Choosing without a deadline is the opposite mistake: now that reports
     * arrive whenever the count changes, a soundtrack that reveals more objects
     * an hour in could restart the helper mid-film, which drops the reserve and
     * re-primes, and the imaging would audibly change. The window is generous
     * enough for a resume to settle and short enough that a restart inside it
     * is still part of starting up.
     */
    if (m_modeWindow.IsTimePast())
    {
      m_modeSettled = true;
      if (!m_modeForced && m_mode == RenderMode::Direct && objects > OBJECT_LIMIT_FOR_DIRECT)
        CLog::Log(LOGWARNING,
                  "CDVDAudioCodecOmniphony: {} objects is more than direct rendering can carry, "
                  "but the stream only said so {}ms in - staying on direct rather than restarting "
                  "the render mid-film",
                  objects, OMNI_MODE_WINDOW_MS);
      continue;
    }

    m_modeSettled = true;

    if (!m_modeForced && m_mode == RenderMode::Direct && objects > OBJECT_LIMIT_FOR_DIRECT)
    {
      // Still inside the opening blocks, so this is a restart at the start of
      // the stream rather than a switch part-way through a film. Settled first,
      // so a failure here cannot send us round again.
      if (!ReopenAs(RenderMode::Cascade))
      {
        FallBack("could not restart on the virtual layout");
        return m_fallback ? m_fallback->AddData(packet) : false;
      }
      return true;
    }
  }

  return true;
}

void CDVDAudioCodecOmniphony::GetData(DVDAudioFrame& frame)
{
  if (m_fallback)
  {
    m_fallback->GetData(frame);

    // CVideoPlayerAudio publishes the decoder name and the channel count once,
    // when the stream first syncs, and has no message for "the codec you are
    // holding just became a different one". Without this the screen goes on
    // reporting binaural object audio while ffmpeg decodes a plain
    // multichannel downmix - which is exactly how this failure was first
    // described. The format is only true once ffmpeg has decoded something,
    // hence here rather than where the swap happens.
    if (frame.nb_frames)
    {
      if (!m_reportedFallback)
      {
        m_reportedFallback = true;
        // Those three rows describe a binaural render that has stopped
        // happening, and nothing else is going to take them down.
        ClearRenderInfo();
      }
      // Every block, not once, and for the reason PublishRenderInfo gives:
      // swapping to the software decoder changes the output format, so ActiveAE
      // reconfigures and empties the whole audio player info - and this is
      // mid-film, long past the one sync transition where CVideoPlayerAudio
      // would have written the decoder name again. Published once, the screen
      // would go blank shortly after the swap instead of naming ffmpeg.
      m_processInfo.SetAudioDecoderName(m_fallback->GetName());
      m_processInfo.SetAudioChannels(frame.format.m_channelLayout);
    }
    return;
  }

  frame.nb_frames = 0;

  // FallBack stopped the helper and found no software decoder to take over, so
  // there is nothing to collect from - see AddData.
  if (!m_helper)
    return;

  // This is the call that keeps the bank topped up once AddData starts refusing
  // packets: from then until the player has taken enough audio, it is the only
  // one being made.
  if (!Collect(0))
  {
    FallBack("the helper died mid-stream");
    return;
  }

  // Hold everything back until the bank is full. The player reads this as "no
  // audio yet" and leaves the clock stopped, which is exactly the point.
  if (m_priming)
  {
    if (GetBufferSize() < m_primeFrames && !m_primeDeadline.IsTimePast())
      return;
    m_priming = false;

    /*
     * Priming that expires with a partial bank is a renderer that is slow.
     * Priming that expires with nothing at all, from a helper that has been
     * given audio and has never handed a single block back, is a renderer that
     * is not working - and without this that is silence for the length of the
     * film, because the test below returns and the player waits for audio that
     * is never coming.
     *
     * Both halves of that are load-bearing. A stream whose picture starts
     * before its sound reaches this with nothing rendered too, and it is
     * perfectly healthy - it has simply not been asked to render anything yet.
     *
     * It is the backstop for a disagreement neither side can report. The helper
     * calls a rejected packet a decode error and carries on, which is right for
     * a damaged file; a bridge refusing every packet it is given looks exactly
     * the same from there, until you notice that nothing has ever come out. The
     * helper now says so itself when it can (see its FEED handler), and this
     * catches whatever it cannot - including an engine that accepts everything
     * and renders nothing.
     */
    if (m_fed && !m_rendered && GetBufferSize() == 0)
    {
      FallBack("the renderer produced no audio at all");
      return;
    }

    CLog::Log(LOGDEBUG, "CDVDAudioCodecOmniphony: primed {}ms of render{}",
              GetBufferSize() * 1000 / static_cast<int>(OMNI_OUT_RATE),
              m_primeDeadline.IsTimePast() ? " (gave up waiting for more)" : "");
  }

  if (m_out.frames.empty())
    return;

  const uint32_t frames = m_out.frames.front();
  const size_t samples = static_cast<size_t>(frames) * OMNI_OUT_CHANNELS;
  if (m_out.pcm.size() - m_pcmConsumed < samples)
    return;

  frame.passthrough = false;
  frame.format = m_format;
  frame.framesize = m_format.m_frameSize;
  frame.nb_frames = frames;
  frame.framesOut = 0;
  frame.planes = 1;
  frame.bits_per_sample = CAEUtil::DataFormatToBits(m_format.m_dataFormat);
  frame.duration = (static_cast<double>(frames) * DVD_TIME_BASE) / OMNI_OUT_RATE;

  /*
   * Copied out of the bank rather than pointed at inside it.
   *
   * What CVideoPlayerAudio does with frame.data[0] is hold it across several
   * passes: CAudioSinkAE::AddPackets can take part of a block, and the rest is
   * offered again later from the same pointer, with AddData called in between.
   * So the pointer has to outlive anything that moves the bank - and by then
   * everything moves it. Collect appends to m_out.pcm, which reallocates; the
   * reclaim below erases from its front, which shifts every byte after it. In
   * the field the first of those was a segfault inside the sink's own memcpy,
   * every time, and the second was silent - it played whatever had shifted
   * into place.
   *
   * One buffer, reused: GetData is only called once the player has finished
   * with the last frame, which is what the nb_frames <= framesOut test in
   * CVideoPlayerAudio::ProcessDecoderOutput means, so nothing is ever reading
   * this while it is being written.
   */
  const float* const src = m_out.pcm.data() + m_pcmConsumed;
  m_handout.assign(src, src + samples);

  // Keep the render inside full scale before anyone downstream sees it - see
  // m_limiter.
  float* const block = m_handout.data();
  float* plane[AE_CH_MAX] = {block};
  for (uint32_t i = 0; i < frames; ++i)
  {
    const unsigned int at = i * OMNI_OUT_CHANNELS;
    const float gain = m_limiter.Run(plane, OMNI_OUT_CHANNELS, at, false);
    if (gain != 1.0f)
    {
      block[at] *= gain;
      block[at + 1] *= gain;
    }
  }
  frame.data[0] = reinterpret_cast<uint8_t*>(block);

  // The engine's own timestamp, moved onto the demuxer's timeline. It counts
  // output samples, so consecutive blocks are exactly their duration apart and
  // nothing here has to accumulate anything - except where the source itself
  // jumped, which the timeline hands over when the count gets there.
  const double shift = m_timeline.Take(static_cast<double>(m_out.enginePts.front()));
  if (m_anchor != DVD_NOPTS_VALUE)
    m_anchor += shift;
  frame.hasTimestamp = m_anchor != DVD_NOPTS_VALUE;
  frame.pts = frame.hasTimestamp ? m_anchor + static_cast<double>(m_out.enginePts.front())
                                 : static_cast<double>(DVD_NOPTS_VALUE);

  // A block is on its way out, so this codec is unambiguously the one being
  // heard - which is the condition PublishRenderInfo waits for. Every block
  // rather than only on a change, because the screen is emptied behind us; see
  // PublishRenderInfo.
  PublishRenderInfo();

  m_pcmConsumed += samples;
  m_out.frames.erase(m_out.frames.begin());
  m_out.enginePts.erase(m_out.enginePts.begin());

  // Reclaim once the consumed head is worth moving, rather than on every block.
  if (m_pcmConsumed > (1u << 18))
  {
    m_out.pcm.erase(m_out.pcm.begin(), m_out.pcm.begin() + m_pcmConsumed);
    m_pcmConsumed = 0;
  }
}

void CDVDAudioCodecOmniphony::Drain()
{
  if (m_fallback)
  {
    m_fallback->Drain();
    return;
  }
  if (m_drained || m_failed || !m_helper)
    return;

  m_drained = true;
  // A short stream may end before the normal reserve target is reached. EOF is
  // proof that no more input is coming, so withholding its tail for the
  // priming deadline would only add silence.
  m_priming = false;

  XbmcThreads::EndTime<> deadline{std::chrono::milliseconds(OMNI_FLUSH_TIMEOUT_MS)};

  if (m_pcm)
  {
    // First empty FFmpeg itself. The helper's FLUSH can only drain samples that
    // have crossed into the renderer, so sending it before delayed decoder
    // frames and staged OPCM would turn a correct bridge drain into a truncated
    // PCM stream.
    if (!StagePcm() || !m_pcm->AddData(DemuxPacket()) || !StagePcm())
    {
      CLog::Log(LOGERROR,
                "CDVDAudioCodecOmniphony: could not drain the PCM decoder at end of stream");
      return;
    }

    for (;;)
    {
      std::vector<uint8_t> tail;
      if (deadline.IsTimePast() || !m_pcm->DrainResampler(tail))
      {
        CLog::Log(LOGERROR,
                  "CDVDAudioCodecOmniphony: could not drain the PCM resampler at end of stream");
        return;
      }
      if (tail.empty())
        break;
      m_staging.insert(m_staging.end(), tail.begin(), tail.end());
    }

    while (!m_staging.empty() && !deadline.IsTimePast())
    {
      if (!DrainStaging() || !Collect(OMNI_PUMP_SLICE_MS))
      {
        CLog::Log(LOGERROR,
                  "CDVDAudioCodecOmniphony: helper died while accepting end-of-stream PCM");
        return;
      }
    }
    if (!m_staging.empty())
    {
      CLog::Log(LOGERROR,
                "CDVDAudioCodecOmniphony: helper did not accept all end-of-stream PCM in {}ms",
                OMNI_FLUSH_TIMEOUT_MS);
      return;
    }
  }

  if (!m_helper->Send(OP_FLUSH, nullptr, 0))
  {
    CLog::Log(LOGERROR, "CDVDAudioCodecOmniphony: could not send the end-of-stream flush");
    return;
  }

  bool acknowledged = false;
  while (!deadline.IsTimePast() && !acknowledged)
  {
    const size_t before = m_out.frames.size();
    const bool alive = m_helper->Collect(m_out, OMNI_PUMP_SLICE_MS);
    AnchorNewBlocks(before);

    for (const auto& msg : m_helper->TakeMessages())
    {
      CLog::Log(LOGDEBUG, "CDVDAudioCodecOmniphony: helper: {}", msg);
      if (StringUtils::StartsWith(msg, OMNI_FLUSH_MARK))
      {
        acknowledged = true;
        continue;
      }

      // A drain-only first frame can be the first truthful stream report.
      // Preserve the same object/bed information AddData consumes so the tail
      // is not rendered with an empty player description.
      ReadHrirReport(msg);
      const size_t at = msg.find("objects=");
      if (at == std::string::npos)
        continue;
      const int objects = std::atoi(msg.c_str() + at + 8);
      if (objects <= 0)
        continue;
      m_objectCount = objects;
      const size_t bedAt = msg.find("bed=");
      if (bedAt != std::string::npos)
      {
        m_bed = msg.substr(bedAt + 4);
        StringUtils::Trim(m_bed);
      }
      m_infoDirty = true;
    }

    if (!alive && !acknowledged)
    {
      CLog::Log(LOGERROR, "CDVDAudioCodecOmniphony: helper died while draining at end of stream");
      return;
    }
  }

  if (!acknowledged)
    CLog::Log(LOGERROR,
              "CDVDAudioCodecOmniphony: helper did not finish the end-of-stream flush in {}ms",
              OMNI_FLUSH_TIMEOUT_MS);
}

void CDVDAudioCodecOmniphony::Reset()
{
  if (m_fallback)
  {
    m_fallback->Reset();
    return;
  }

  m_drained = false;

  m_parser.Reset();
  m_backlog.clear();

  if (m_pcm)
  {
    // Flushes ffmpeg and drops the resampler, which re-arms the header: what
    // that resampler is holding belongs to where the film used to be, and the
    // bridge is about to be reset and will want describing again.
    m_pcm->Reset();
    m_staging.clear();
    m_headerSent = false;
  }

  DropRendered();
  StartPriming(OMNI_PRIME_FRAMES_SEEK);
  // The OP_RESET below returns the bridge to expecting a header, so whether the
  // renderer works is an open question again - see ArmRecovery.
  ArmRecovery();
  // A seek is a discontinuity: carrying the limiter's attack/hold/release
  // across it would attenuate the new position because of a peak in the old.
  m_limiter.Reset();

  if (m_helper)
  {
    // Nothing is collected here on purpose. Whatever the helper is holding
    // belongs to the old position, and DropRendered has already told the pump
    // thread to throw it away as it arrives.
    if (!m_helper->Send(OP_RESET, nullptr, 0))
      FallBack("the helper did not survive a seek");
  }
}

AEAudioFormat CDVDAudioCodecOmniphony::GetFormat()
{
  if (m_fallback)
    return m_fallback->GetFormat();
  return m_format;
}

int CDVDAudioCodecOmniphony::GetBufferSize()
{
  if (m_fallback)
    return m_fallback->GetBufferSize();
  return static_cast<int>((m_out.pcm.size() - m_pcmConsumed) / OMNI_OUT_CHANNELS);
}
