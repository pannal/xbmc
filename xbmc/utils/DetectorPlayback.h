/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include <array>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <string>

// CPU-only result ownership. No persistence, GUI, VFS or native calls here.
namespace DETECTOR_PLAYBACK
{
inline constexpr const char* PROPERTY = "p3i.detector.binding";
enum class State { READY, ACCEPTED, APPLIED, STALE, INCOMPATIBLE, FAILED };
inline const char* Name(State state)
{
  switch (state)
  {
    case State::READY: return "ready";
    case State::ACCEPTED: return "accepted";
    case State::APPLIED: return "applied";
    case State::STALE: return "stale";
    case State::INCOMPATIBLE: return "incompatible";
    case State::FAILED: return "failed";
  }
  return "stale";
}
inline uint64_t NextIdentity()
{
  static std::atomic<uint64_t> next{0};
  return ++next;
}
struct Result
{
  int width{0}, height{0}, streamIndex{-1}, codec{0};
  double displayAspect{0.0};
  bool hasStrict{false}, hasSubtitle{false}, highConfidence{false}, preCropped{false};
  std::array<uint16_t, 4> strict{}, subtitle{}; // top, bottom, left, right
  bool Valid() const
  {
    const auto bounds = [this](const auto& value) {
      return value[0] + value[1] < height && value[2] + value[3] < width;
    };
    return width >= 64 && height >= 64 && width <= 65535 && height <= 65535 &&
        codec > 0 && streamIndex >= 0 && std::isfinite(displayAspect) && displayAspect > 0.0 &&
        (hasStrict || hasSubtitle || preCropped) && (!highConfidence || hasStrict) &&
        (!hasStrict || bounds(strict)) && (!hasSubtitle || bounds(subtitle)) &&
        (!preCropped || (!hasStrict && !hasSubtitle && !highConfidence &&
         std::abs(displayAspect * height - width) <= 1.0 &&
         ((width * 9 / 16 - height) / 2 > 20 || (height * 16 / 9 - width) / 2 > 20)));
  }
};
struct Binding
{
  Binding(std::shared_ptr<const Result> result, std::string target, int stream)
    : id(std::to_string(NextIdentity())), result(std::move(result)), target(std::move(target)), stream(stream) {}
  const std::string id;
  const std::shared_ptr<const Result> result;
  const std::string target;
  const int stream;
  std::atomic<State> state{State::READY};
  std::atomic<uint64_t> playback{0}, selection{0};
  std::atomic<bool> subtitleAvailable{false}, nativeAvailable{false}, geometryAvailable{false};
};
struct Registry
{
  std::mutex mutex;
  std::map<std::string, std::shared_ptr<Binding>> bindings;
};
inline Registry& Store()
{
  static Registry store;
  return store;
}
inline bool Register(const std::shared_ptr<Binding>& value)
{
  auto& store = Store();
  std::lock_guard<std::mutex> guard(store.mutex);
  if (store.bindings.size() >= 64)
  {
    for (auto it = store.bindings.begin(); it != store.bindings.end(); ++it)
    {
      const auto state = it->second->state.load();
      if ((state != State::READY && state != State::ACCEPTED) ||
          (state == State::ACCEPTED && it->second->playback == 0 && it->second.use_count() == 1))
      {
        store.bindings.erase(it);
        break;
      }
    }
  }
  if (store.bindings.size() >= 64)
    return false;
  store.bindings.emplace(value->id, value);
  return true;
}
inline std::shared_ptr<Binding> Find(const std::string& id)
{
  auto& store = Store();
  std::lock_guard<std::mutex> guard(store.mutex);
  const auto found = store.bindings.find(id);
  return found == store.bindings.end() ? nullptr : found->second;
}
inline std::shared_ptr<Binding> Consume(const std::string& id, const std::string& target)
{
  const auto binding = Find(id);
  if (!binding)
    return {};
  auto ready = State::READY;
  if (!binding->state.compare_exchange_strong(ready, State::ACCEPTED))
    return {};
  if (binding->target != target || !binding->result || !binding->result->Valid())
  {
    binding->state = State::INCOMPATIBLE;
    return {};
  }
  return binding;
}
inline bool Release(const std::string& id)
{
  auto& store = Store();
  std::lock_guard<std::mutex> guard(store.mutex);
  const auto found = store.bindings.find(id);
  if (found == store.bindings.end())
    return false;
  auto ready = State::READY;
  found->second->state.compare_exchange_strong(ready, State::STALE);
  // Captured requests retain their data even after the public handle is released.
  store.bindings.erase(found);
  return true;
}
inline void Retire(const std::shared_ptr<Binding>& binding)
{
  if (binding && binding->state != State::INCOMPATIBLE)
  {
    binding->state = State::STALE;
    binding->subtitleAvailable = false;
    binding->nativeAvailable = false;
    binding->geometryAvailable = false;
  }
}
} // namespace DETECTOR_PLAYBACK
