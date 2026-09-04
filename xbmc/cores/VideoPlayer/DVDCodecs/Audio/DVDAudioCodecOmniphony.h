/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "DVDAudioCodec.h"
#include "OmniphonyTimeline.h"
#include "cores/AudioEngine/Utils/AEAudioFormat.h"
#include "cores/AudioEngine/Utils/AELimiter.h"
#include "cores/AudioEngine/Utils/AEStreamInfo.h"
#include "threads/CriticalSection.h"
#include "threads/Event.h"
#include "threads/SystemClock.h"
#include "threads/Thread.h"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include <sys/types.h>

class CProcessInfo;
class COmniphonyPcmSource;

/*!
 * \brief Word the engine's HRIR report for Player.Process(omniphony.sofa).
 *
 * \param selector the set the engine says it is convolving with, as the helper
 *        passes it on: "saf" for its embedded KEMAR measurements, "sofa" for a
 *        file of the listener's.
 * \return "Built-in", "Personal", or empty for anything else - an engine too
 *         old to say included - rather than a guess.
 */
std::string OmniphonyDescribeHrir(const std::string& selector);

/*!
 * \brief Render audio to headphones binaurally, out of process.
 *
 * There are two ways in, and which one a stream takes is settled at open.
 *
 * Dolby Atmos and DTS:X carry objects, and objects exist only before decode -
 * so that path taps where Kodi already frames encoded audio for passthrough:
 * CAEStreamParser reassembles one complete access unit across demux packet
 * boundaries, and those bytes - before any MAT or IEC packing - are exactly
 * what the decoder bridge consumes.
 *
 * Everything else has to be decoded before it can be placed, and that is
 * \ref COmniphonyPcmSource: an ordinary ffmpeg decoder whose PCM is converted
 * to what the renderer's PCM bridge accepts and staged here rather than handed
 * to the sink. The renderer, the helper process and everything downstream of
 * them do not know which path fed them.
 *
 * The decode and the binaural render happen in a helper process rather than
 * here. CoreELEC runs a 32-bit userspace on a 64-bit kernel, and a shared
 * library takes the word size of whoever loads it; loaded by Kodi that is 32
 * bits, where the same work costs roughly twice as much. Measured on an S922X,
 * decoding Dolby Digital Plus Atmos and rendering its objects costs 0.957 of
 * realtime in 32-bit against 0.419 in 64-bit, and the process boundary itself
 * costs nothing.
 *
 * What comes back is ordinary interleaved stereo float, so ActiveAE and the
 * sink are untouched and nothing downstream can tell this apart from any other
 * decoder.
 */
class CDVDAudioCodecOmniphony : public CDVDAudioCodec
{
public:
  explicit CDVDAudioCodecOmniphony(CProcessInfo& processInfo);
  ~CDVDAudioCodecOmniphony() override;

  bool Open(CDVDStreamInfo& hints, CDVDCodecOptions& options) override;
  void Dispose() override;
  bool AddData(const DemuxPacket& packet) override;
  void GetData(DVDAudioFrame& frame) override;
  void Drain() override;
  void Reset() override;
  AEAudioFormat GetFormat() override;
  //! Once the software decoder has taken over it is the one doing the work, and
  //! saying otherwise puts "om-truehd" on screen over a plain downmix.
  std::string GetName() override { return m_fallback ? m_fallback->GetName() : m_codecName; }
  int GetBufferSize() override;

  //! \brief Times the bitstream parser lost sync mid-stream - see
  //! CAEStreamParser::GetSyncLostCount()
  unsigned int GetSyncLostCount() const override { return m_parser.GetSyncLostCount(); }

private:
  /*!
   * \brief The helper process and the pipes to it.
   *
   * Framed both ways with a sixteen-byte header, so a reader can always find
   * the next boundary without parsing what came before. Both pipes are
   * non-blocking and driven with poll(), because the helper blocks on its own
   * reads and writes: feeding it without draining it would fill a pipe and stop
   * both processes.
   *
   * That poll runs on a thread of its own, and that is the whole reason this
   * renderer can keep a reserve. Driven from the audio thread it could only
   * work while the player was inside AddData or GetData, and the player spends
   * almost all of its time elsewhere - parked in CAudioSinkAE::AddPackets
   * waiting for the sink to take audio. Rendering is what should fill that
   * time: sampled across whole films the render costs 0.42 of a core on Dolby
   * Digital Plus and 0.47 on TrueHD, so what it needs is not more speed but
   * somewhere to have put the surplus beforehand. A thread here turns the
   * player's idle time into exactly that.
   */
  class CHelper : private CThread
  {
  public:
    /*!
     * \brief Everything the helper has rendered and not yet been asked for.
     *
     * One structure rather than three vectors passed side by side: GetData
     * indexes all three in step and erases from the front of each, so a block
     * that lost its timestamp would take the front off an empty vector. Held
     * together, that cannot be expressed.
     */
    struct Rendered
    {
      std::vector<float> pcm; //!< interleaved stereo, oldest first
      std::vector<uint32_t> frames; //!< frames in each block
      std::vector<int64_t> enginePts; //!< engine microseconds, see \ref m_anchor
    };

    CHelper();
    ~CHelper() override;

    bool Start(const std::string& exe);
    void Stop();

    //! \brief Queue one command. False means the helper is gone.
    bool Send(uint8_t op, const void* payload, size_t len);

    /*!
     * \brief Take everything rendered so far.
     *
     * Appends to \p out and returns false once the helper has died or the
     * protocol has been broken. Waits up to \p timeoutMs for the first block;
     * zero takes what is there and returns.
     */
    bool Collect(Rendered& out, int timeoutMs);

    //! \brief Bytes queued for the helper that it has not taken off us yet.
    size_t Queued();

    /*!
     * \brief Drop everything rendered before a seek.
     *
     * The pipe can hold a second or more of audio that belongs to where the
     * film used to be, and OP_RESET does not flush it - the helper has already
     * written it. Clearing what has arrived is only half the job, so this also
     * arms \ref m_dropping for the rest.
     */
    void Resync();

    //! \brief Status text the helper reported, drained by the caller for the log.
    std::vector<std::string> TakeMessages();

  private:
    void Process() override;
    bool ParseFrames();
    void Reap();

    CCriticalSection m_lock;
    CEvent m_produced; //!< a block reached \ref m_ready, or the helper died

    pid_t m_pid{-1};
    int m_in{-1}; //!< our end of the helper's stdin
    int m_out{-1}; //!< our end of the helper's stdout
    std::vector<uint8_t> m_pending; //!< queued for the helper
    size_t m_pendingSent{0}; //!< how much of m_pending has been written already
    std::vector<uint8_t> m_acc; //!< partial frames, only ever touched by Process
    Rendered m_ready; //!< rendered and not yet collected
    size_t m_readyFrames{0};
    std::vector<std::string> m_messages;

    /*!
     * \brief The engine's clock, watched for the restart that OP_RESET causes.
     *
     * It counts output samples since the stream began and a reset puts it back
     * to zero, so the first block whose timestamp goes backwards is the first
     * one rendered from the new position. That makes the boundary exact
     * without adding anything to the protocol: everything before it is stale,
     * everything from it on belongs to where the film now is.
     */
    int64_t m_lastPts{-1};
    bool m_dropping{false};

    std::atomic<bool> m_broken{false};
  };

  /*!
   * \brief How the objects reach the ears.
   *
   * Cost is the same per convolved source either way - measured at 0.0156 of
   * realtime on this hardware, whether the source is an object or a virtual
   * speaker - so the cheaper mode is simply whichever convolves fewer. Direct
   * has no interpolation error at any angle, so it wins wherever it fits.
   */
  enum class RenderMode
  {
    Direct, //!< one HRTF pair per object; exact, and cost grows with the count
    Cascade //!< objects panned onto 12 virtual speakers, then those convolved
  };

  //! Above this many sources direct stops being affordable: it reaches 0.60 of
  //! one core at 24 and 0.72 at 32, while cascading stays flat at 0.430.
  static constexpr int OBJECT_LIMIT_FOR_DIRECT = 24;

  bool StartHelper(CDVDStreamInfo& hints);
  bool ReopenAs(RenderMode mode);

  /*!
   * \brief AddData for a stream being decoded here - see \ref m_pcm.
   *
   * Same contract as AddData itself, and the reason it is written out
   * separately: false has to keep meaning "this packet was not consumed, offer
   * it again", and on this path that is a much narrower claim than on the
   * bitstream one. See the ordering argument at the definition.
   */
  bool AddPcmData(const DemuxPacket& packet);
  //! \brief Convert everything the decoder will yield into \ref m_staging.
  //! False means the stream cannot be rendered and the caller must fall back.
  bool StagePcm();
  //! \brief Send as much of \ref m_staging to the helper as it has room for.
  //! False means the helper is gone.
  bool DrainStaging();
  /*!
   * \brief Return the bridge to expecting a header, for a new geometry.
   *
   * The bridge parses one header and then streams until it is reset, so a
   * stream that changes shape mid-film has to reset it to describe the new
   * shape. OP_RESET restarts the engine's sample counter along with it, which
   * is why the bank and the anchor go too - the same reason a seek does.
   */
  bool RestartBridge();
  //! \brief Re-arm the no-output backstop. Called wherever the bridge is reset.
  void ArmRecovery();
  //! \brief Tie the engine's clock to the demuxer's, once, off the first block
  //! appended since \p before.
  void AnchorNewBlocks(size_t before);
  //! \brief Take what the helper has rendered, waiting up to \p timeoutMs for
  //! the first block. False means the helper died and the caller must fall back.
  bool Collect(int timeoutMs);
  /*!
   * \brief Wait until there is room to send the helper more work.
   *
   * This is what keeps the player from outrunning the helper. False means the
   * helper died and the caller must fall back.
   */
  bool AwaitRoom();
  //! \brief Throw away rendered audio and the clock that described it.
  void DropRendered();
  //! \brief Start banking \p frames of audio again - see \ref m_priming.
  void StartPriming(int frames);
  void FallBack(const char* why);
  void UpdateName();
  /*!
   * \brief Put what is being rendered on the player process screen.
   *
   * Called only from GetData, and only once a block has actually been handed
   * over, which is later than it looks and deliberately so. Opening a codec
   * destroys the one it replaces - CVideoPlayerAudio assigns over the
   * unique_ptr that holds it - so a codec that published from Open() would be
   * wiped by the outgoing codec's Dispose() a moment later. Publishing from
   * the first block handed out puts this after that teardown in every path,
   * including an audio track change part-way through a film.
   */
  void PublishRenderInfo();
  //! \brief Take the three omniphony rows off the screen, because nothing is
  //! being rendered any more.
  void ClearRenderInfo();
  //! \brief What the engine was handed, worded for the screen. Empty until the
  //! first frame has been decoded, which is the earliest it can be truthful.
  std::string InputDescription() const;
  //! \brief Take the head model from a helper status line that reports one -
  //! see \ref m_sofa.
  void ReadHrirReport(const std::string& msg);
  bool WriteConfig(const std::string& bridge) const;
  static std::string HelperPath();
  static std::string ConfigPath();
  static std::string LayoutPath();
  static const char* CodecId(AVCodecID codec);

  CAEStreamParser m_parser;
  uint8_t* m_buffer{nullptr};
  unsigned int m_bufferSize{0};
  unsigned int m_dataSize{0};

  std::vector<uint8_t> m_backlog;
  //! Bytes the parser has taken since it was last reset, or on the PCM path the
  //! bytes of audio staged since the engine was: where in the stream a packet
  //! starts, for \ref m_timeline.
  uint64_t m_parsed{0};

  /*!
   * \brief The decoder, for a stream whose audio has to be decoded to be placed.
   *
   * Null on the object path, and that null is what routes every branch below:
   * the two paths share the helper, the bank, the clock and the limiter, and
   * differ only in what they put into the pipe.
   */
  std::unique_ptr<COmniphonyPcmSource> m_pcm;

  /*!
   * \brief Converted PCM waiting for room in the helper's input queue.
   *
   * This exists because AddData's false means "the packet was not consumed,
   * offer it again", and on this path the packet has already been through
   * ffmpeg by the time there could be anything to refuse. Returning false then
   * would replay it - duplicated audio on every bank-full event during normal
   * playback, not only when something goes wrong. So what cannot be sent yet
   * waits here, where it belongs to nobody's packet.
   */
  std::vector<uint8_t> m_staging;
  //! \brief Whether the bridge has been given a header since it was last reset.
  //! A second one arriving means the stream changed shape - see RestartBridge.
  bool m_headerSent{false};
  //! \brief Whether this helper has ever been given audio, and whether it has
  //! ever handed any back. Only the two together mean anything: see the priming
  //! backstop in GetData, which is all that reads them.
  bool m_fed{false};
  bool m_rendered{false};

  std::unique_ptr<CHelper> m_helper;
  CHelper::Rendered m_out;
  size_t m_pcmConsumed{0};

  /*!
   * \brief The one block the player is currently holding, on its own storage.
   *
   * CVideoPlayerAudio keeps frame.data[0] across several passes - the sink
   * takes a block in pieces and the remainder is offered again later, with
   * AddData called in between - so it cannot point into \ref m_out, which
   * moves under it every time the bank grows or is reclaimed. See GetData.
   */
  std::vector<float> m_handout;

  /*!
   * \brief Where the engine's timeline sits on the demuxer's, in DVD time.
   *
   * The engine stamps every block with its own timestamp, and that timestamp is
   * a count of output samples since the stream began: orender_ffi computes it
   * as `sample_pos * 1000000 / sample_rate`. So it is exact, gap-free, and
   * advances by precisely the audio handed back - no packet that decoded to
   * nothing can shift it. Adding one constant turns it into the demuxer's
   * timeline, and that constant is what this holds.
   *
   * Anchoring once is the whole point. The obvious alternative - stamp each
   * demux timestamp onto whichever block happens to arrive next - looks right
   * and is not: the helper answers later than it is asked, so those blocks are
   * the render of much older input, and every stamp drags the output clock
   * forward to wherever the demuxer has read to. In the field that ran the
   * audio clock 9.1 seconds ahead of the picture inside half a second.
   *
   * Once really does mean once. There used to be a drift check here that took
   * a fresh anchor whenever the two timelines disagreed by more than a second,
   * on the grounds that only the stream's own timeline could move that far.
   * That stopped being true when the render gained a reserve: a second or more
   * is now in flight by design, the disagreement is that pipeline rather than
   * the stream, and the check fired on every film - 2.15 seconds forward at
   * the first frame, swinging back four seconds later. A genuine mid-stream
   * discontinuity reaches this codec as Reset(), which re-arms the anchor
   * properly, with nothing in flight to confuse it. A gap too small for the
   * player to flush for is followed by \ref m_timeline, which moves this at
   * the point in the render where the gap falls.
   *
   * NOPTS until the first demux timestamp meets the first block, which is at
   * the start of a stream or straight after a seek - both moments when nothing
   * is in flight and the two genuinely do belong together.
   */
  double m_anchor{DVD_NOPTS_VALUE};

  //! \brief The demuxer's timestamp, held until a block turns up to anchor to.
  double m_pendingPts{DVD_NOPTS_VALUE};

  //! \brief Where the source's timestamps jump, on the engine's count - see
  //! COmniphonyTimeline. Reset with the anchor.
  COmniphonyTimeline m_timeline;

  /*!
   * \brief Keeps the render inside full scale.
   *
   * A binaural render is a fold to stereo that sums HRTF-filtered channels at
   * unity, so it leaves full scale behind whatever the source was: fifteen
   * objects and their beds convolved into two channels routinely peak above
   * 1.0. Nothing downstream catches that. The mixer's limiter is gated on
   * amplification, a disabled downmix normalisation or a float sink, and this
   * path is none of those, so the samples reach the sink's integer conversion
   * unbounded and wrap there.
   *
   * The engine's own auto_gain is the wrong tool and is left off, the same
   * choice and for the same reason as the in-process renderer: it lowers the
   * master gain permanently, so one loud transient quietens everything after
   * it. CAELimiter attenuates on the peak, holds, then releases back to unity.
   */
  CAELimiter m_limiter;

  /*!
   * \brief Bank rendered audio before letting the player have any.
   *
   * CVideoPlayerAudio holds the master clock back until the sink is three
   * quarters full, so as long as GetData answers nothing, nothing starts. The
   * renderer does not keep pace while it is warming up, and starting the clock
   * against a shortfall makes that shortfall permanent - ActiveAE then
   * "corrects" it by cutting audio out.
   *
   * This is the opening of the reserve rather than the whole of it: once
   * playback is running the pump thread keeps filling to OMNI_BANK_FRAMES on
   * its own. What priming adds is that the film does not start until the first
   * of it is in hand.
   *
   * Re-armed for a seek as well as an open. A seek empties the renderer as
   * completely as a fresh start does, so it faces the same warm-up, and the
   * audio has to be back in hand before the clock resumes.
   */
  bool m_priming{true};
  int m_primeFrames{0};
  XbmcThreads::EndTime<> m_primeDeadline;

  AEAudioFormat m_format;
  //! Replaced by UpdateName() during Open(), which is before anything can ask.
  std::string m_codecName{"om"};

  /*!
   * \brief Our own copy of the stream hints.
   *
   * A copy rather than a pointer, because CVideoPlayerAudio::OpenStream takes
   * its CDVDStreamInfo by value: the object the factory hands to Open() dies
   * when OpenStream returns, and the fallback decoder is opened much later
   * than that.
   */
  std::unique_ptr<CDVDStreamInfo> m_hints;

  /*!
   * \brief Render mode, chosen once from what the stream turns out to carry.
   *
   * The object count is only truthful after a frame has been rendered - the C
   * ABI says so, and the helper reports it in its first status frame - so the
   * choice cannot be made at open. Rather than switch mid-film and let the
   * imaging audibly change, this starts in Direct and re-opens once, within the
   * first blocks, if the count turns out to be more than Direct can carry.
   * \ref m_modeSettled makes that a one-shot: it can never oscillate.
   */
  RenderMode m_mode{RenderMode::Direct};
  bool m_modeSettled{false};
  /*!
   * \brief The listener pinned the mode, so the count does not get to choose.
   *
   * Separate from \ref m_modeSettled rather than folded into it: settling early
   * would also skip reading the object count, and the count is what the screen
   * reports. This only disables the switch.
   */
  bool m_modeForced{false};
  //! \brief How long the mode decision stays open - see OMNI_MODE_WINDOW_MS.
  XbmcThreads::EndTime<> m_modeWindow;
  /*!
   * \brief Objects in the last frame the engine reported on, or -1 for none yet.
   *
   * Tracked rather than latched, because the ABI says the object state is live
   * and "must not be latched": a report of zero means the frame just rendered
   * carried no object metadata, not that the soundtrack has none. Only a report
   * that actually carries objects reaches this.
   */
  int m_objectCount{-1};

  /*!
   * \brief The bed the objects sit over, as the engine labelled it.
   *
   * Comma separated and in render order - usually just "LFE", since a Dolby
   * Atmos presentation hands the renderer every other bed channel as an object
   * of its own. Empty for a soundtrack with no bed, and empty for an engine too
   * old to export orender_bed_layout; both mean "say nothing about a bed"
   * rather than "there is none", which is why the screen simply omits the
   * clause instead of writing one.
   */
  std::string m_bed;

  /*!
   * \brief The head model the engine is convolving with, worded for the screen.
   *
   * Taken from the engine's own report rather than from the setting or from the
   * file staged in the profile, so a personal file the engine could not load
   * reads "Built-in" - which is what the listener is actually hearing. Live:
   * the engine starts on its built-in set and swaps a configured file in once
   * it has been built, a moment into the stream. Empty until the first report.
   */
  std::string m_sofa;

  //! \brief Something the screen shows has changed and the cached wording below
  //! has not been rebuilt for it yet. See PublishRenderInfo.
  bool m_infoDirty{false};

  /*!
   * \brief The three rows as they are to appear, rebuilt only when they change.
   *
   * Held rather than rebuilt per block because they are re-published per block:
   * ActiveAE empties the whole audio player info when it reconfigures, so a row
   * written once does not stay written. PublishRenderInfo carries the argument.
   */
  std::string m_input;
  std::string m_render;

  /*!
   * \brief When the helper dies mid-stream, decode continues here.
   *
   * There is no way for a codec to ask the player to replace it:
   * CVideoPlayerAudio::SwitchCodecIfNeeded() runs on a sample-rate change or a
   * display reset, not on request. So the fallback lives inside this codec,
   * created only once something has actually gone wrong, and fed the original
   * demux packets rather than our framed ones.
   */
  std::unique_ptr<CDVDAudioCodec> m_fallback;
  //! \brief Whether the swap has been announced - see GetData.
  bool m_reportedFallback{false};
  CProcessInfo& m_processInfo;
  bool m_failed{false};
  //! True after the one end-of-stream FLUSH has been sent to this helper.
  bool m_drained{false};
};
