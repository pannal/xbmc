/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#pragma once

#include <atomic>
#include <cstdint>
#include <initializer_list>
#include <string>

namespace AudioDiagnostics
{
// Scalar observations only. Never pass media paths, URLs, packet data or user settings.
#define AUDIO_DIAGNOSTIC_METRICS(X) \
  X(DecoderReadBeginBytes, "decoder.read_begin_bytes") \
  X(DecoderReadUs, "decoder.read_us") \
  X(DecoderRequestedBytes, "decoder.requested_bytes") \
  X(DecoderReadBytes, "decoder.read_bytes") \
  X(DecoderResult, "decoder.result") \
  X(DecoderEmpty, "decoder.empty_read") \
  X(DecoderRingBytes, "decoder.ring_bytes") \
  X(DecoderStatus, "decoder.status") \
  X(DecoderRate, "decoder.source_hz") \
  X(DecoderChannels, "decoder.channels") \
  X(DecoderBits, "decoder.bits") \
  X(DecoderConsumeBytes, "decoder.consume_bytes") \
  X(DecoderDataError, "decoder.data_error") \
  X(PlayerLoopGapUs, "player.loop_gap_us") \
  X(PlayerLoopUs, "player.loop_us") \
  X(PlayerSpeed, "player.speed") \
  X(PlayerSpaceBytes, "player.ae_space_bytes") \
  X(PlayerReadySamples, "player.ready_samples") \
  X(PlayerWantFrames, "player.want_frames") \
  X(PlayerAddedFrames, "player.added_frames") \
  X(PlayerAddUs, "player.add_us") \
  X(PlayerAddBeginFrames, "player.add_begin_frames") \
  X(PlayerShortFrames, "player.short_frames") \
  X(PlayerNoData, "player.no_data_with_space") \
  X(PlayerNoSpace, "player.no_space") \
  X(PlayerStatus, "player.decoder_status") \
  X(PlayerPositionMs, "player.position_ms") \
  X(PlayerStarted, "player.started") \
  X(PlayerGainPpm, "player.replay_gain_ppm") \
  X(PlayerPeakPpm, "player.replay_peak_ppm") \
  X(PlayerCrossfadeMs, "player.crossfade_ms") \
  X(AeLoopGapUs, "ae.loop_gap_us") \
  X(AeState, "ae.state") \
  X(AeSignal, "ae.signal") \
  X(AeStreamCount, "ae.streams") \
  X(AeActiveStreams, "ae.active_streams") \
  X(AeBufferingStreams, "ae.buffering_streams") \
  X(AeEmptyStreams, "ae.empty_processing_outputs") \
  X(AeCacheUs, "ae.stream_cache_us") \
  X(AeWaterUs, "ae.water_us") \
  X(AeTargetUs, "ae.target_us") \
  X(AeOutputFrames, "ae.output_frames") \
  X(AeInputFrames, "ae.input_frames") \
  X(AeMode, "ae.mode") \
  X(AeQuality, "ae.resample_quality") \
  X(AeNativeOptIn, "ae.native_opt_in") \
  X(AeLowLatency, "ae.low_latency") \
  X(AeNormalize, "ae.normalize_levels") \
  X(AeStereoUpmix, "ae.stereo_upmix") \
  X(AeRatioPpm, "ae.resample_ratio_ppm") \
  X(StreamAddUs, "stream.add_us") \
  X(StreamAddBeginFrames, "stream.add_begin_frames") \
  X(StreamRaw, "stream.raw") \
  X(StreamRequestedFrames, "stream.requested_frames") \
  X(StreamAcceptedFrames, "stream.accepted_frames") \
  X(StreamWaitTimeout, "stream.wait_timeout") \
  X(StreamPartial, "stream.partial") \
  X(StreamId, "stream.id") \
  X(SinkGapUs, "sink.delivery_gap_us") \
  X(SinkCallUs, "sink.add_packets_us") \
  X(SinkBeginFrames, "sink.add_begin_frames") \
  X(SinkWantFrames, "sink.want_frames") \
  X(SinkWrittenFrames, "sink.written_frames") \
  X(SinkZero, "sink.zero") \
  X(SinkInvalid, "sink.invalid_result") \
  X(SinkPooled, "sink.pooled_payload") \
  X(SinkDelayUs, "sink.reported_delay_us") \
  X(SinkRaw, "sink.raw") \
  X(SinkState, "sink.state") \
  X(SinkSignal, "sink.signal") \
  X(AlsaWriteUs, "alsa.write_us") \
  X(AlsaPcmProbeBytes, "alsa.pcm_probe_bytes") \
  X(AlsaPcmNonzeroProbes, "alsa.pcm_nonzero_probes") \
  X(AlsaPcmZeroProbeBlocks, "alsa.pcm_zero_probe_blocks") \
  X(AlsaWriteBeginFrames, "alsa.write_begin_frames") \
  X(AlsaRecoverUs, "alsa.recover_us") \
  X(AlsaWantFrames, "alsa.want_frames") \
  X(AlsaWrittenFrames, "alsa.written_frames") \
  X(AlsaShortFrames, "alsa.short_frames") \
  X(AlsaZero, "alsa.zero") \
  X(AlsaWriteError, "alsa.write_errno") \
  X(AlsaRecoverResult, "alsa.recover_result") \
  X(AlsaHandleError, "alsa.handle_errno") \
  X(AlsaPrepareResult, "alsa.prepare_result") \
  X(AlsaResumeResult, "alsa.resume_result") \
  X(AlsaStartResult, "alsa.start_result") \
  X(AlsaForwardResult, "alsa.forward_result") \
  X(AlsaDrainResult, "alsa.drain_result") \
  X(AlsaNoPcm, "alsa.no_pcm") \
  X(AlsaDropResult, "alsa.drop_result") \
  X(AlsaCloseResult, "alsa.close_result") \
  X(AlsaNonblockResult, "alsa.nonblock_result") \
  X(AlsaDelayResult, "alsa.delay_result") \
  X(AlsaDelayFrames, "alsa.delay_frames") \
  X(AlsaState, "alsa.state") \
  X(AlsaRequestedRate, "alsa.requested_hz") \
  X(AlsaActualRate, "alsa.actual_hz") \
  X(AlsaRequestedChannels, "alsa.requested_channels") \
  X(AlsaActualChannels, "alsa.actual_channels") \
  X(AlsaRequestedFormat, "alsa.requested_format_enum") \
  X(AlsaActualFormat, "alsa.actual_format_enum") \
  X(AlsaPeriod, "alsa.period_frames") \
  X(AlsaBuffer, "alsa.buffer_frames") \
  X(AlsaPassthrough, "alsa.passthrough")

enum class Metric
{
#define AUDIO_DIAGNOSTIC_ENUM(id, name) id,
  AUDIO_DIAGNOSTIC_METRICS(AUDIO_DIAGNOSTIC_ENUM)
#undef AUDIO_DIAGNOSTIC_ENUM
  Count
};

inline constexpr const char* METRIC_NAMES[] = {
#define AUDIO_DIAGNOSTIC_NAME(id, name) name,
    AUDIO_DIAGNOSTIC_METRICS(AUDIO_DIAGNOSTIC_NAME)
#undef AUDIO_DIAGNOSTIC_NAME
};
#undef AUDIO_DIAGNOSTIC_METRICS

struct Observation
{
  Metric metric;
  int64_t value;
};

extern std::atomic<bool> enabled;
inline bool Enabled() noexcept { return enabled.load(std::memory_order_relaxed); }

// Start reads audio-diagnostic.enable in Kodi's temp directory once. No marker
// means no thread, file output or clocks in the instrumented hot paths.
void Start(const std::string& directory);
void Stop();
int64_t NowUs() noexcept; // CLOCK_MONOTONIC on Linux, same domain as collector.
inline int64_t Begin() noexcept { return Enabled() ? NowUs() : 0; }
// Decoder/player preparation has a separate aggregate bank, so prefetching a
// later track cannot hide a starvation observation on a started stream.
void Record(std::initializer_list<Observation> values, bool preparation = false) noexcept;
void Scaled(Metric metric, double value, bool preparation = false) noexcept;
void Gap(Metric metric) noexcept;
void Event(const std::string& text) noexcept; // bounded, sanitized configuration/lifecycle
std::string SafeEndpoint(const std::string& endpoint);
} // namespace AudioDiagnostics
