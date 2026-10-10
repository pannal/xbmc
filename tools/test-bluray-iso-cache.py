#!/usr/bin/env python3
"""Compile the actual ISO cache and exercise deterministic positioned I/O.

Host callbacks substitute storage and global operator new injects genuine cache
allocation failures; this checks bytes/lifetime contracts, not CE
linkage, libbluray integration, storage performance or device playback.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

FIXTURE = r'''
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <list>
#include <memory>
#include <mutex>
#include <thread>
#include <unordered_map>
#include <vector>
// Access changes only visibility. Actual production methods and layout stay intact.
#define private public
#include "BlurayIsoCache.h"
#undef private
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <future>
#include <limits>
#include <map>
#include <new>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#define EXPECT(x) do { if (!(x)) { std::fprintf(stderr, "assertion failed: %s:%d: %s\n", __FILE__, __LINE__, #x); std::abort(); } } while (0)
using namespace std::chrono_literals;
namespace AllocationFault
{
std::atomic_bool armed{false};
std::atomic<int> countdown{0}, failures{0};
std::thread::id demandThread;
bool workerOnly{false};
std::mutex mutex;
std::condition_variable changed;
void Arm(int allocation, bool worker)
{
  demandThread = std::this_thread::get_id();
  workerOnly = worker;
  countdown = allocation;
  failures = 0;
  armed.store(true, std::memory_order_release);
}
void Disarm() { armed.store(false, std::memory_order_release); }
void Check()
{
  if (!armed.load(std::memory_order_acquire)) return;
  const bool isDemand = std::this_thread::get_id() == demandThread;
  if (workerOnly == isDemand) return;
  if (countdown.fetch_sub(1) == 1)
  {
    Disarm();
    ++failures;
    changed.notify_all();
    throw std::bad_alloc();
  }
}
void Wait()
{
  std::unique_lock lock(mutex);
  EXPECT(changed.wait_for(lock, 5s, [] { return failures.load() == 1; }));
}
}
__attribute__((noinline)) void* operator new(std::size_t bytes)
{
  AllocationFault::Check();
  if (void* memory = std::malloc(bytes ? bytes : 1)) return memory;
  throw std::bad_alloc();
}
__attribute__((noinline)) void* operator new[](std::size_t bytes) { return ::operator new(bytes); }
__attribute__((noinline)) void operator delete(void* memory) noexcept { std::free(memory); }
__attribute__((noinline)) void operator delete[](void* memory) noexcept { std::free(memory); }
__attribute__((noinline)) void operator delete(void* memory, std::size_t) noexcept { std::free(memory); }
__attribute__((noinline)) void operator delete[](void* memory, std::size_t) noexcept { std::free(memory); }
struct Backend
{
  std::vector<uint8_t> bytes;
  std::mutex io, mutex;
  std::condition_variable changed;
  std::map<int64_t, std::deque<int64_t>> actions;
  std::map<int64_t, int> calls;
  size_t partial{std::numeric_limits<size_t>::max()};
  int64_t blockedOffset{-1};
  bool entered{false}, released{false};
  explicit Backend(size_t size = 131072) : bytes(size)
  {
    for (size_t i = 0; i < size; ++i)
      bytes[i] = static_cast<uint8_t>((i * 17) ^ (i >> 7) ^ (i >> 13));
  }
  int64_t Read(int64_t offset, uint8_t* destination, size_t requested)
  {
    // This lock is the real callback contract: one complete seek/read pair.
    std::lock_guard serialized(io);
    std::unique_lock state(mutex);
    ++calls[offset];
    if (offset == blockedOffset && !released)
    {
      entered = true;
      changed.notify_all();
      EXPECT(changed.wait_for(state, 5s, [this] { return released; }));
    }
    if (offset < 0 || static_cast<uint64_t>(offset) > bytes.size())
      return -1;
    size_t count = std::min({requested, partial, bytes.size() - static_cast<size_t>(offset)});
    auto& responses = actions[offset];
    if (!responses.empty())
    {
      int64_t response = responses.front();
      responses.pop_front();
      if (response <= 0)
        return response;
      count = std::min(count, static_cast<size_t>(response));
    }
    std::copy_n(bytes.data() + offset, count, destination);
    return static_cast<int64_t>(count);
  }
  CBlurayIsoCache::ReadCallback Callback()
  { return [this](int64_t at, uint8_t* out, size_t count) { return Read(at, out, count); }; }
  void WaitBlocked()
  {
    std::unique_lock lock(mutex);
    EXPECT(changed.wait_for(lock, 5s, [this] { return entered; }));
  }
  void Release()
  { std::lock_guard lock(mutex); released = true; changed.notify_all(); }
  int Count(int64_t at)
  { std::lock_guard lock(mutex); return calls[at]; }
  int Total()
  { std::lock_guard lock(mutex); int n = 0; for (auto& item : calls) n += item.second; return n; }
};
CBlurayIsoCache::Config Config(size_t pages = 4, size_t ahead = 0)
{ CBlurayIsoCache::Config c; c.pageSize = 4096; c.maxBytes = pages * c.pageSize; c.forwardPrefetchPages = ahead; return c; }
void ReadExact(CBlurayIsoCache& cache, Backend& b, int lba, int blocks)
{
  std::vector<uint8_t> out(static_cast<size_t>(blocks) * 2048, 0xcd);
  EXPECT(cache.ReadBlocks(out.data(), lba, blocks) == blocks);
  EXPECT(std::equal(out.begin(), out.end(), b.bytes.begin() + static_cast<size_t>(lba) * 2048));
}
void ByteReads()
{
  Backend b;
  CBlurayIsoCache cache(b.bytes.size(), b.Callback(), Config());
  ReadExact(cache, b, 1, 5); // In-page start and two page crossings.
  ReadExact(cache, b, 2, 1);
  EXPECT(b.Total() == 3); // Cached re-read performs no I/O.
  ReadExact(cache, b, 0, 1);
  EXPECT(b.Total() == 3);
}
void Partials()
{
  Backend b; b.partial = 337;
  CBlurayIsoCache cache(b.bytes.size(), b.Callback(), Config());
  ReadExact(cache, b, 1, 5);
  int old = b.Total();
  ReadExact(cache, b, 1, 5);
  EXPECT(b.Total() == old);
}
void ShortGap()
{
  Backend b; b.actions[0] = {1024}; b.actions[1024] = {0};
  CBlurayIsoCache cache(b.bytes.size(), b.Callback(), Config());
  ReadExact(cache, b, 0, 4);
  EXPECT(b.Count(1024) == 2); // Exact missing byte, never next-page start.
  ReadExact(cache, b, 0, 1);
  EXPECT(b.Count(0) == 2); // A transient short page must not become cached EOF.
}
void ShortInPage()
{
  Backend b; b.actions[0] = {1024}; b.actions[1024] = {0};
  CBlurayIsoCache cache(b.bytes.size(), b.Callback(), Config());
  ReadExact(cache, b, 1, 2);
  EXPECT(b.Count(2048) == 1); // Direct fallback begins at the requested in-page byte.
}
void ZeroAndError()
{
  Backend b;
  CBlurayIsoCache cache(b.bytes.size(), b.Callback(), Config());
  uint8_t out[2048]{};
  b.actions[0] = {0, 0};
  EXPECT(cache.ReadBlocks(out, 0, 1) == 0);
  ReadExact(cache, b, 0, 1);
  cache.ResetAccessPattern();
  b.actions[8192] = {-1, -1}; // Positioned backend failed seek/read, not cached.
  EXPECT(cache.ReadBlocks(out, 4, 1) == -1);
  ReadExact(cache, b, 4, 1);
  Backend invalid;
  CBlurayIsoCache bad(invalid.bytes.size(), [](int64_t, uint8_t*, size_t n) { return static_cast<int64_t>(n) + 1; }, Config());
  EXPECT(bad.ReadBlocks(out, 0, 1) == -1);
}
void Direct()
{
  for (int mode = 0; mode < 2; ++mode)
  {
    Backend b; b.partial = 511;
    auto config = Config(); config.enabled = mode != 0;
    CBlurayIsoCache cache(mode ? -1 : static_cast<int64_t>(b.bytes.size()), b.Callback(), config);
    ReadExact(cache, b, 3, 4);
    int count = b.Total();
    ReadExact(cache, b, 3, 4);
    EXPECT(b.Total() > count); // Disabled and unknown-length paths remain direct.
  }
}
void Bounds()
{
  Backend b(8192 + 517);
  CBlurayIsoCache cache(b.bytes.size(), b.Callback(), Config());
  uint8_t out[16384];
  EXPECT(cache.ReadBlocks(nullptr, 0, 1) == -1);
  EXPECT(cache.ReadBlocks(out, -1, 1) == -1);
  EXPECT(cache.ReadBlocks(out, 0, 0) == -1);
  EXPECT(cache.ReadBlocks(out, 0, -1) == -1);
  EXPECT(cache.ReadBlocks(out, 0, std::numeric_limits<int>::max()) == -1);
  EXPECT(cache.ReadBlocks(out, std::numeric_limits<int>::max(), 1) == 0);
  EXPECT(cache.ReadBlocks(out, 5, 1) == 0);
  EXPECT(b.Total() == 0);
  std::fill(std::begin(out), std::end(out), 0xcd);
  EXPECT(cache.ReadBlocks(out, 3, 4) == 1);
  EXPECT(std::equal(out, out + 2048 + 517, b.bytes.begin() + 6144));
  EXPECT(out[2048 + 517] == 0xcd);
  EXPECT(cache.ReadBlocks(out, 0, std::numeric_limits<int>::max() / 2048) == 4);
  auto weird = Config(); weird.pageSize = 0; weird.maxBytes = 0; weird.forwardPrefetchPages = 99;
  CBlurayIsoCache minimum(b.bytes.size(), b.Callback(), weird);
  ReadExact(minimum, b, 0, 1);
  EXPECT(b.Count(0) == 2);
}
void Eviction()
{
  Backend b;
  CBlurayIsoCache cache(b.bytes.size(), b.Callback(), Config(2));
  ReadExact(cache, b, 0, 1);
  ReadExact(cache, b, 2, 1);
  ReadExact(cache, b, 0, 1); // Touch page zero; page one is least-recently-used.
  ReadExact(cache, b, 4, 1);
  ReadExact(cache, b, 0, 1);
  EXPECT(b.Count(0) == 1);
  ReadExact(cache, b, 2, 1);
  EXPECT(b.Count(4096) == 2);
}
void Prefetch()
{
  Backend b; b.blockedOffset = 4096;
  CBlurayIsoCache cache(b.bytes.size(), b.Callback(), Config(4, 1));
  ReadExact(cache, b, 0, 1);
  b.WaitBlocked(); // Backend read is evidence of actual worker prefetch.
  b.Release();
  ReadExact(cache, b, 2, 1); // Waits for load; then serves its cached bytes.
  EXPECT(b.Count(4096) == 1);
  cache.Stop();
}
void Generation()
{
  Backend b; b.blockedOffset = 4096;
  CBlurayIsoCache cache(b.bytes.size(), b.Callback(), Config(4, 1));
  ReadExact(cache, b, 0, 1);
  b.WaitBlocked();
  cache.ResetAccessPattern();
  b.Release();
  ReadExact(cache, b, 2, 1);
  EXPECT(b.Count(4096) == 2); // The pre-reset load was not published.
  ReadExact(cache, b, 12, 1); // Non-sequential read supersedes prefetch window.
  ReadExact(cache, b, 1, 1); // Backward seek remains byte-exact.
  cache.Stop();
}
void CancelSimple()
{
  Backend b;
  CBlurayIsoCache cache(b.bytes.size(), b.Callback(), Config());
  cache.Cancel();
  uint8_t out[2048];
  EXPECT(cache.ReadBlocks(out, 0, 1) == -1);
  EXPECT(b.Total() == 0);
  cache.Stop();
}
void StopBlocked()
{
  Backend b; b.blockedOffset = 4096;
  CBlurayIsoCache cache(b.bytes.size(), b.Callback(), Config(4, 1));
  ReadExact(cache, b, 0, 1);
  b.WaitBlocked();
  auto cancellation = std::async(std::launch::async, [&cache] { cache.Cancel(); });
  EXPECT(cancellation.wait_for(1s) == std::future_status::ready);
  cancellation.get();
  auto stopped = std::async(std::launch::async, [&cache] { cache.Stop(); });
  EXPECT(stopped.wait_for(50ms) == std::future_status::timeout);
  b.Release();
  EXPECT(stopped.wait_for(2s) == std::future_status::ready);
  stopped.get();
  uint8_t out[2048];
  EXPECT(cache.ReadBlocks(out, 0, 1) == -1);
  CBlurayIsoCache fresh(b.bytes.size(), b.Callback(), Config());
  ReadExact(fresh, b, 0, 1);
  EXPECT(b.Count(0) == 2);
}
void CheckCacheIndex(CBlurayIsoCache& cache)
{
  std::lock_guard lock(cache.m_mutex);
  EXPECT(cache.m_pages.size() == cache.m_lru.size());
  EXPECT(cache.m_pages.size() <= cache.m_maxPages);
  for (auto it = cache.m_lru.begin(); it != cache.m_lru.end(); ++it)
  {
    auto found = cache.m_pages.find(*it);
    EXPECT(found != cache.m_pages.end());
    EXPECT(found->second.lru == it);
  }
}
void Prewarm(Backend& b)
{
  // Remove fixture/backend allocations from the armed cache-read window.
  for (int64_t offset = 0; offset < static_cast<int64_t>(b.bytes.size()); offset += 4096)
  {
    b.calls[offset] = 0;
    b.actions[offset] = {};
  }
}
void DemandAllocation()
{
  // GNU host allocation order: page control block, page bytes, LRU node,
  // unordered-map node, first bucket array. Each really throws from operator new.
  for (int allocation = 1; allocation <= 5; ++allocation)
  {
    Backend b;
    Prewarm(b);
    CBlurayIsoCache cache(b.bytes.size(), b.Callback(), Config(2));
    std::vector<uint8_t> out(2048, 0xcd);
    bool propagated = false;
    int count = -99;
    AllocationFault::Arm(allocation, false);
    try { count = cache.ReadBlocks(out.data(), 0, 1); }
    catch (const std::bad_alloc&) { propagated = true; }
    AllocationFault::Disarm();
    EXPECT(AllocationFault::failures.load() == 1);
    EXPECT(!propagated);
    EXPECT(count == 1);
    EXPECT(std::equal(out.begin(), out.end(), b.bytes.begin()));
    CheckCacheIndex(cache); // Failed map publication must roll back the LRU node.
    EXPECT(cache.m_pages.empty());
    ReadExact(cache, b, 0, 1);
    EXPECT(b.Count(0) == 2);
    ReadExact(cache, b, 2, 1);
    ReadExact(cache, b, 4, 1);
    ReadExact(cache, b, 6, 1);
    CheckCacheIndex(cache);
    EXPECT(cache.m_pages.size() == 2);
  }
}
void WorkerAllocation()
{
  // Page zero already owns the initial map bucket array; prefetch allocation
  // order therefore has four allocations, including real map-node publication.
  for (int allocation = 1; allocation <= 4; ++allocation)
  {
    Backend b;
    Prewarm(b);
    CBlurayIsoCache cache(b.bytes.size(), b.Callback(), Config(4, 1));
    AllocationFault::Arm(allocation, true);
    ReadExact(cache, b, 0, 1);
    AllocationFault::Wait();
    AllocationFault::Disarm();
    ReadExact(cache, b, 2, 1);
    cache.Stop(); // Joins successful completion after the injected worker failure.
    CheckCacheIndex(cache);
    EXPECT(AllocationFault::failures.load() == 1);
    EXPECT(b.Count(4096) == (allocation <= 2 ? 1 : 2));
  }
}
void Concurrent()
{
  Backend b;
  CBlurayIsoCache cache(b.bytes.size(), b.Callback(), Config(4, 1));
  std::vector<std::future<void>> readers;
  for (int i = 0; i < 6; ++i)
    readers.push_back(std::async(std::launch::async, [&, i] {
      for (int j = 0; j < 10; ++j) ReadExact(cache, b, (i * 3 + j * 7) % 40, 3);
    }));
  for (auto& reader : readers) reader.get();
  cache.Stop();
}
int main(int argc, char** argv)
{
  const std::map<std::string, void(*)()> tests = {{"bytes", ByteReads}, {"partials", Partials},
    {"short-gap", ShortGap}, {"short-inpage", ShortInPage}, {"zero-error", ZeroAndError},
    {"direct", Direct}, {"bounds", Bounds}, {"eviction", Eviction}, {"prefetch", Prefetch},
    {"generation", Generation}, {"cancel", CancelSimple}, {"stop-blocked", StopBlocked},
    {"concurrent", Concurrent}, {"allocation-demand", DemandAllocation}, {"allocation-worker", WorkerAllocation}};
  if (argc == 2) { EXPECT(tests.count(argv[1])); tests.at(argv[1])(); }
  else { for (auto& test : tests) { std::fprintf(stderr, "case: %s\n", test.first.c_str()); test.second(); } }
  std::puts("PASS: production ISO cache runtime assertions");
}
'''

MUTANTS = [
    ("page-allocation-propagates", "allocation-demand", "return {}; // Optional caching must not prevent a direct demand read.", "throw; // Unsafe optional-cache allocation failure propagation."),
    ("lru-allocation-propagates", "allocation-demand", "catch (const std::bad_alloc&)\n    {\n      return page;\n    }", "catch (const std::bad_alloc&)\n    {\n      throw;\n    }"),
    ("map-publication-no-rollback", "allocation-demand", "m_lru.pop_front();\n      return page;", "return page;"),
    ("map-allocation-propagates", "allocation-demand", "m_lru.pop_front();\n      return page;", "m_lru.pop_front();\n      throw;"),
    ("missing-gap", "short-gap", "ReadAt(offset + static_cast<int64_t>(copied), buffer + copied, bytes - copied)", "ReadAt((index + 1) * static_cast<int64_t>(m_config.pageSize), buffer + copied, bytes - copied)"),
    ("retain-short-page", "short-gap", "page->size() == size && generation == m_generation", "generation == m_generation"),
    ("wrong-inpage-fallback", "short-inpage", "ReadAt(position, buffer + copied, bytes - copied)", "ReadAt(index * static_cast<int64_t>(m_config.pageSize), buffer + copied, bytes - copied)"),
    ("cancel-ignored", "cancel", "m_cancelled = true;", "m_cancelled = false;"),
    ("stale-generation-published", "generation", "page->size() == size && generation == m_generation", "page->size() == size"),
    ("prefetch-disabled", "prefetch", "if (m_config.enabled && m_config.forwardPrefetchPages)", "if (false && m_config.enabled && m_config.forwardPrefetchPages)"),
    ("stop-detaches-io", "stop-blocked", "if (m_worker.joinable())\n    m_worker.join();", "if (m_worker.joinable())\n    m_worker.detach();"),
    ("oversized-backend-count", "zero-error", "count < 0 || static_cast<uint64_t>(count) > bytes - total", "count < 0"),
    ("prefetch-not-retained", "prefetch", "if (page->size() == size && generation == m_generation)", "if (false && page->size() == size && generation == m_generation)"),
    ("budget-exceeded", "eviction", "m_pages.size() > m_maxPages", "m_pages.size() > m_maxPages + 1"),
    ("wrong-lru-eviction", "eviction", "m_pages.erase(m_lru.back());\n      m_lru.pop_back();", "m_pages.erase(m_lru.front());\n      m_lru.pop_front();"),
    ("invalid-request-size", "bounds", "numBlocks > static_cast<int>(std::numeric_limits<int>::max() / BLOCK_SIZE)", "false"),
    ("direct-offset-lost", "direct", "const int64_t count = ReadAt(offset, buffer, bytes);", "const int64_t count = ReadAt(0, buffer, bytes);"),
    ("partial-offset-lost", "partials", "m_read(offset + static_cast<int64_t>(total), buffer + total, bytes - total)", "m_read(offset, buffer + total, bytes - total)"),
]

def run(command, path, env=None):
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=40, env=env)
    path.write_text(result.stdout)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--logs", type=Path)
    parser.add_argument("--negative-controls", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    logs = args.logs.resolve() if args.logs else Path(tempfile.mkdtemp(prefix="bluray-iso-cache-"))
    logs.mkdir(parents=True, exist_ok=True)
    source_dir = root / "xbmc/cores/VideoPlayer/DVDInputStreams"
    source = (source_dir / "BlurayIsoCache.cpp").read_text()
    header = (source_dir / "BlurayIsoCache.h").read_text()
    report = {"root": str(root), "compiler": subprocess.check_output(["g++", "--version"], text=True).splitlines()[0], "fixture_sha256": hashlib.sha256(FIXTURE.encode()).hexdigest(), "positive_scenarios": 15, "allocation_injection": "Genuine global operator-new bad_alloc: five demand and four worker cache allocation points; backend maps prewarmed, host GNU allocation order checked by actual failure assertions.", "whitebox_checks": "Fixture-only access visibility checks actual LRU/map consistency after allocation failures. No production layout, methods or source changed.", "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "header_sha256": hashlib.sha256(header.encode()).hexdigest(), "limits": "Host storage callbacks plus genuine operator-new allocation failures; no CE linkage, libbluray integration, device playback or performance measurement. Stop tests prefetch I/O; caller/session ownership must retain any foreground ReadBlocks until it returns.", "cases": []}
    variants = [("production", None, source)]
    if args.negative_controls:
        for name, case, before, after in MUTANTS:
            if source.count(before) != 1:
                raise RuntimeError(f"{name}: mutation anchor count {source.count(before)}, expected one")
            variants.append((name, case, source.replace(before, after, 1)))
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:abort_on_error=1", UBSAN_OPTIONS="halt_on_error=1")
    for name, case, variant in variants:
        directory = logs / name
        directory.mkdir(exist_ok=True)
        (directory / "fixture.cpp").write_text(FIXTURE)
        (directory / "BlurayIsoCache.cpp").write_text(variant)
        (directory / "BlurayIsoCache.h").write_text(header)
        command = ["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1", "-g", "-fno-omit-frame-pointer", "-fsanitize=address,undefined", "-pthread", str(directory / "fixture.cpp"), str(directory / "BlurayIsoCache.cpp"), "-o", str(directory / "fixture")]
        (directory / "compile-command.json").write_text(json.dumps(command, indent=2) + "\n")
        compiled = run(command, directory / "compile.log")
        if compiled.returncode:
            raise RuntimeError(f"{name}: compilation failed; see {directory / 'compile.log'}")
        command = [str(directory / "fixture")] + ([case] if case else [])
        (directory / "run-command.json").write_text(json.dumps(command, indent=2) + "\n")
        result = run(command, directory / "run.log", env)
        sanitizer = any(marker in result.stdout for marker in ("AddressSanitizer", "runtime error:", "UndefinedBehaviorSanitizer"))
        if case:
            passed = result.returncode in (-6, 134) and "assertion failed:" in result.stdout and not sanitizer
        else:
            passed = result.returncode == 0 and "PASS: production ISO cache runtime assertions" in result.stdout and not sanitizer
        report["cases"].append({"name": name, "case": case, "compiled": True, "returncode": result.returncode, "passed": passed})
        (logs / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        if not passed:
            raise RuntimeError(f"{name}: runtime criterion failed; see {directory / 'run.log'}")
        print(f"PASS: {name}", flush=True)
    print(f"Evidence: {logs / 'report.json'}")

if __name__ == "__main__":
    main()
