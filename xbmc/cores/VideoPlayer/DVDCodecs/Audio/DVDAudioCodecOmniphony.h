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
 * \brief Name the spatial bed the renderer was handed, e.g. "7.1.4 + 5
 *        Objects" or, with nothing above it, "7.1 + 4 Heights".
 *
 * DTS:X hands the renderer a whole presentation rather than the sparse bed
 * Atmos does: a full floor layout, a quartet of fixed heights above it, and
 * the objects on top. "L, R, C, LFE, Ls, Rs, Lb, Rb, Tfl, Tfr, Tbl, Tbr + 12
 * Objects" is all of that, and says none of it - the reader has to count the
 * labels and know which ones are overhead. Naming the bed by its layout says
 * the same thing in the words a listener already uses.
 *
 * Which of the two forms is used depends on whether objects arrived, because
 * that decides what the row is really reporting. With objects the bed is the
 * context: it is written as the one compact number, "7.1.4", and the count
 * that matters follows it. With no objects the heights are the whole of the
 * news, so they are spelled out - "7.1 + 4 Heights" says a quartet was placed,
 * where "7.1.4" would read as a speaker layout the room is expected to have.
 *
 * A bed with no floor channel has no layout number to write; an Atmos mix's
 * LFE-only bed is exactly that, so it returns empty and the caller keeps
 * "LFE + 15 Objects", which is already the right sentence for a sparse bed.
 * Every form puts the object count last, so the rows read the same way round.
 *
 * \param bed comma-separated channel labels: the engine's, as the helper packs
 *        them, or Kodi's names for the same positions, as OmniphonyPcmDescribe
 *        writes them for the PCM path. The two spell every overhead position
 *        alike but for case - "Tfl" and "TFL" - and both are read.
 * \param objectCount objects carried alongside, or <= 0 for a bed-only
 *        presentation, whose heights are still worth naming.
 * \return the description, or empty when \p bed has nothing overhead and there
 *         are no objects over a floor to write its layout for: a plain channel
 *         list, which says nothing a skin's own layout row does not.
 */
std::string OmniphonyDescribeSpatialBed(const std::string& bed, int objectCount);

/*!
 * \brief Turn the decoder's source label into the player-row description.
 *
 * The ABI label names the presentation the renderer actually decoded. Auro
 * and DTS:X labels carry enough structure to say that more usefully than the
 * raw bed does, so these recognized forms outrank
 * OmniphonyDescribeSpatialBed(): "DTS-HD MA + DTS:X 7.1.4" becomes
 * "7.1 + 4 Heights", and "DTS-HD MA + Auro-3D 11.1" becomes the
 * "Auro 11.1" layout listeners know.
 *
 * Other source labels name only the codec Kodi already displays, so returning
 * them here would duplicate that row. Malformed or future labels are likewise
 * left to the bed-derived fallback rather than guessed at.
 *
 * \param sourceLabel the decoded presentation label exported by the ABI.
 * \return the description, or empty when the label has no recognized spatial
 *         form and the caller should use the bed-derived fallback.
 */
std::string OmniphonyDescribeSourceLabel(const std::string& sourceLabel);

/*!
 * \brief The Kodi log level for one line the helper wrote to stderr.
 *
 * The engine logs through env_logger's default layout, "[<time> LEVEL
 * target] message". Its WARN and ERROR lines keep their weight, so a decoder
 * that cannot read a stream's extension says so in kodi.log; INFO and below
 * go to the debug log beside the helper's status lines. Anything else on
 * stderr - a panic, a bridge writing without a host sink - was not meant to
 * appear in normal running and is logged as a warning.
 *
 * \param line one line of stderr, without its newline.
 * \return LOGERROR, LOGWARNING or LOGDEBUG.
 */
int OmniphonyHelperLogLevel(const std::string& line);

/*!
 * \brief The rate used when the stream does not say what it is.
 *
 * Not a preference: the renderer builds its head model at whatever rate it is
 * given - hrir_len() scales the kernel and the measured set resamples itself -
 * so nothing here wants 48 kHz in particular. It is the rate almost all film
 * audio arrives at, and therefore the least surprising thing to assume when
 * the demuxer has not said.
 */
constexpr unsigned int OMNI_DEFAULT_RATE = 48000;

/*!
 * \brief The highest rate this will render at, above which a source is
 * resampled down to it.
 *
 * The binaural convolution is a direct dot product whose kernel grows with the
 * rate - 128 taps at 48 kHz, 256 at 96, 512 at 192 - so its cost is the rate
 * times the kernel and rises with the square of the rate. Against a render
 * already measured at a third to a half of one core, 96 kHz is roughly four
 * times the work and 192 kHz sixteen, which this hardware does not have.
 *
 * The ceiling costs a listener nothing they can hear from the render itself:
 * the head model is measured at 48 kHz and resampled upwards, so a higher rate
 * carries no more spatial information. What it buys is one fewer conversion
 * for a 88.2 or 96 kHz source, which is why the ceiling is here rather than at
 * 48.
 */
constexpr unsigned int OMNI_MAX_RATE = 96000;

//! The narrowest rate worth believing from a demuxer hint.
constexpr unsigned int OMNI_MIN_RATE = 8000;

//! \brief What the rate an engine reports means for the stream in progress.
enum class OmniphonyRateVerdict
{
  Agrees, //!< it is the rate already being rendered at, or the engine has not said
  NotOnPcmPath, //!< the two cannot disagree on that path, so this is a bug here
  Midstream, //!< as Retune, after the format has been published - see m_formatPublished
  Unrenderable, //!< a real rate, but outside what this path can open at
  Retune, //!< re-open at it - the engine is decoding something else
};

/*!
 * \brief Read the rate an engine reports against the one it was opened at.
 *
 * The counterpart to the parser check this class also makes. A parser reads a
 * header, and a header is a prediction: for DTS it is the core's sync word, and
 * a DTS-HD MA track carrying a 96 kHz XLL extension over a 48 kHz core decodes
 * at 96 while its core says 48. An engine that has decoded a frame is not
 * predicting anything, and it answers for every codec rather than for the two
 * whose headers Kodi's parser can read.
 *
 * Free rather than a member so the decision can be tested on its own: it is
 * five branches with an audible consequence, and the class around it needs a
 * helper process to construct.
 *
 * \param reported        the engine's rate, or 0 for "it has not decoded yet"
 * \param opened          the rate the engine was created at
 * \param pcmPath         this codec decoded and resampled the audio itself
 * \param formatPublished a block has been handed to ActiveAE
 */
OmniphonyRateVerdict OmniphonyRateCheck(unsigned int reported,
                                        unsigned int opened,
                                        bool pcmPath,
                                        bool formatPublished);

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
   * time: sampled once a second across twenty-one TrueHD titles the render
   * costs 0.67 of a core in the median title, so what it needs is not more
   * speed but somewhere to have put the surplus beforehand. A thread here turns
   * the player's idle time into exactly that.
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

    //! \brief The rate in the latest stream report not yet taken, or 0 if
    //! there is none. Leaves the reports where they are - see SettleRate.
    unsigned int ReportedRate();

    /*!
     * \brief Drop everything rendered before a seek, and reset the helper.
     *
     * The pipe can hold a second or more of audio that belongs to where the
     * film used to be, and OP_RESET does not flush it - the helper has already
     * written it. This clears what has arrived; the rest, still in flight, is
     * dropped as it is parsed - see \ref m_resets.
     *
     * The OP_RESET is sent from in here rather than by the caller afterwards,
     * because emptying the bank and arming the drop have to be one operation
     * against the pump thread. False means the helper is gone.
     */
    bool Resync();

    //! \brief Status text the helper reported, drained by the caller for the log.
    std::vector<std::string> TakeMessages();

  private:
    //! \brief \ref Send, for a caller that already holds \ref m_lock.
    bool SendLocked(uint8_t op, const void* payload, size_t len);

    void Process() override;
    bool ParseFrames();
    /*!
     * \brief Log what the helper wrote to stderr, one complete line at a time.
     *
     * The engine and its bridges report decode trouble - an extension they
     * cannot read, a dropped layer, a panic - on stderr rather than in the
     * status protocol. \p final logs a trailing partial line as well.
     */
    void DrainDiagnostics(bool final);
    void Reap();

    CCriticalSection m_lock;
    CEvent m_produced; //!< a block reached \ref m_ready, or the helper died

    pid_t m_pid{-1};
    int m_in{-1}; //!< our end of the helper's stdin
    int m_out{-1}; //!< our end of the helper's stdout
    int m_err{-1}; //!< our end of the helper's stderr - see DrainDiagnostics
    std::string m_errLine; //!< unterminated stderr text; the pump's, then Stop's once joined
    std::vector<uint8_t> m_pending; //!< queued for the helper
    size_t m_pendingSent{0}; //!< how much of m_pending has been written already
    std::vector<uint8_t> m_acc; //!< partial frames, only ever touched by Process
    Rendered m_ready; //!< rendered and not yet collected
    size_t m_readyFrames{0};
    std::vector<std::string> m_messages;

    /*!
     * \brief Resets sent that the helper has not yet marked in its output.
     *
     * The pipe can hold a second of audio belonging to where the film used to
     * be, so a seek needs a boundary in the stream rather than a moment in
     * time. The helper draws one: it answers every OP_RESET with a status
     * frame, having finished writing the audio before it and before writing
     * any after it. So this counts up as resets go out and down as those
     * frames are parsed, and audio is stale for exactly as long as it is not
     * zero. A count rather than a flag because a listener scrubbing can
     * outrun the helper, and the second seek's audio is no less stale than
     * the first's while its own boundary is still to come.
     */
    unsigned int m_resets{0};

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

  /*!
   * \brief Restart the helper, optionally at a different rate.
   *
   * \param mode  the render mode the new helper opens in
   * \param rate  the rate to render at; pass \ref m_rate to keep the current
   *              one. A different value also moves the format, the limiter and
   *              every duration derived from the rate, and is only safe before
   *              the format has been published - see OmniphonyRateCheck.
   */
  bool ReopenAs(RenderMode mode, unsigned int rate);

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

  /*!
   * \brief Wait for the helper to report that the engine is built and open.
   *
   * The engine's head model is built at the rate it is opened at, and at any
   * rate but 48 kHz that takes seconds - so this has to finish before anything
   * downstream starts timing the renderer, or construction is charged against
   * the priming budget and no stream off 48 kHz can ever fill it. False means
   * the helper died, refused the open, or never answered.
   */
  bool AwaitOpen();

  /*!
   * \brief Whether the stream really is at the rate the engine was opened at.
   *
   * Asked once, of the first complete access unit, and only on the bitstream
   * path: there the engine's rate comes from the demuxer while the bridge
   * decodes at whatever the stream actually is, and nothing downstream would
   * report the difference - it is heard rather than logged. False means fall
   * back. See the definition for which codecs the parser can answer for.
   */
  bool RateAgrees();

  /*!
   * \brief Wait for the engine to say what rate it decoded at, and follow it.
   *
   * For a player that asks for the format once, after the first packets, and
   * plays the whole stream at the answer. See the definition.
   */
  void SettleRate();

  //! \brief Sample-frames of render in \p ms milliseconds, at the rate this
  //! stream settled on. The bank and priming sizes are durations, not counts:
  //! a second and a half of reserve has to stay a second and a half at 96 kHz.
  int FramesFor(unsigned int ms) const;

  /*!
   * \brief Decide the rate this stream will be rendered at.
   *
   * The renderer works at any rate, so the only reason not to use the source's
   * own is cost - see OMNI_MAX_RATE. A source at or below the ceiling is
   * rendered at its own rate and never resampled; one above it is resampled
   * down, which only the PCM path can do.
   *
   * \param hinted   the demuxer's sample rate, or 0 if it did not say
   * \param objects  true for the bitstream path, which cannot resample: it is
   *                 refused a rate it would have to change, because the engine
   *                 would then be told one rate while the bridge decodes at
   *                 another, and the render would play at the wrong speed
   * \return the rate to open at, or 0 if this stream cannot be taken
   */
  static unsigned int ChooseRate(int hinted, bool objects);

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
  //! \brief Throw away rendered audio and the clock that described it, and
  //! reset the helper if there is one. False means the helper is gone; a caller
  //! without one gets true, having nothing to reset and nothing to fail.
  bool DropRendered();
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
  static const char* CodecId(const CDVDStreamInfo& hints);

  /*!
   * \brief The rate everything downstream of the helper runs at.
   *
   * The source's own wherever that is affordable, so a 96 kHz album is neither
   * resampled here nor resampled back by the sink. Settled at open and moved
   * only by ReopenAs, when the engine reports another, and always true of the
   * config the helper was given, the format handed to ActiveAE, the limiter,
   * the bank sizes and every duration below.
   */
  unsigned int m_rate{OMNI_DEFAULT_RATE};

  //! \brief \ref RateAgrees has had its one look at the stream.
  bool m_rateChecked{false};

  //! \brief The engine has reported its rate, or \ref SettleRate has waited
  //! for it once already.
  bool m_rateSettled{false};

  /*!
   * \brief A block has been handed over, so \ref m_format is ActiveAE's now.
   *
   * What a change of rate costs. Before it, the engine can be re-opened at
   * whatever the stream turns out to be and nothing downstream has seen a
   * format yet. After it a re-open is a format change the player has to follow
   * - CVideoPlayerAudio reopens its sink for one, as it does for any decoder
   * whose output changes - and the reserve goes with the old helper, so it is
   * a gap once rather than a rate that is wrong for the rest of the film.
   * Latched for the life of the codec rather than per helper: what matters is
   * whether anyone has been told, and a seek does not untell them.
   */
  bool m_formatPublished{false};

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
   * playback is running the pump thread keeps filling to OMNI_BANK_MS on
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
   * \brief Objects the stream is currently carrying, or -1 when it is not.
   *
   * Tracked rather than latched, because the ABI says the object state is live
   * and "must not be latched". Which cuts both ways, and the two directions are
   * not symmetrical: the first zeros of a stream mean only that nothing has
   * been asked yet and are ignored, while a zero after objects have been seen
   * is the stream having stopped carrying them and returns this to -1. The
   * distinction is made by this member's own value - see AddData.
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
   * \brief The decoded presentation label exported by the renderer ABI.
   *
   * Examples are "DTS-HD MA + Auro-3D 11.1" and
   * "DTS-HD HRA + DTS:X 7.1.4". This is a description of what the decoder
   * produced, not proof that every extension frame was understood; a decoder
   * failure remains visible as a failure.
   *
   * An explicitly empty source_label clears the previous value because the
   * field is live. It is also empty for an older engine or helper that cannot
   * report it. Both mean "derive the description from the bed", not that the
   * presentation has no spatial content.
   */
  std::string m_sourceLabel;

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

  /*!
   * \brief Empty answers given since the last block reached the player.
   *
   * The one lever this codec has over how often CVideoPlayerAudio comes back
   * to its message queue, and so over how much input arrives per block that
   * leaves - see GetData, which explains what the ratio buys and what bounds
   * it.
   */
  unsigned int m_yieldsSinceServe{0};

  //! \brief When GetData may next report the reserve - see OMNI_RESERVE_LOG_MS.
  XbmcThreads::EndTime<> m_reserveLogged;

  /*!
   * \brief Whether a packet arrived since the last empty answer.
   *
   * Whether the lever above is connected to anything: an empty answer only
   * works if the player answers it with a packet. See GetData. Starts true so
   * the first one is tried.
   */
  bool m_fedSinceYield{true};
};
