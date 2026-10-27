#!/usr/bin/env python3
"""Exercise CEC source persistence across deferred GUI delivery and retirement.

Uses complete production callbacks, retirement and initial-configuration methods.
Native CEC, application effects and reconnect scheduling are explicit host adapters;
only the marker uses a real temporary file. This is not a device/HDMI test.
--source accepts a saved source file for regression controls.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def method(source, marker):
    start = source.index(marker)
    end = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


PRE = r'''

#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdio>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
using CCriticalSection = std::recursive_mutex;
namespace CEC
{
using cec_logical_address = int;
}
using namespace CEC;
constexpr int CECDEVICE_TV = 0, CECDEVICE_BROADCAST = 15, LOCALISED_ID_NONE = 0,
              LOCALISED_ID_PAUSE = 1, LOCALISED_ID_STOP = 2;
constexpr int WINDOW_SLIDESHOW = 99, ACTION_PAUSE = 1, ACTION_STOP = 2, TMSG_MEDIA_PAUSE = 1,
              TMSG_MEDIA_STOP = 2;
namespace ANNOUNCEMENT
{
constexpr int Other = 1;
}
static std::string marker;
static FILE* redirected_open(const char* path, const char* mode)
{
  assert(std::string(path) == "/run/kodi_cec_startup_done");
  return std::fopen(marker.c_str(), mode);
}
#define fopen redirected_open
struct CAction
{
  explicit CAction(int) {}
};
struct CGUIWindowSlideShow
{
  bool IsPlaying() { return false; }
  bool IsPaused() { return false; }
  void OnAction(CAction) {}
};
struct CApplicationPowerHandling
{
  bool IsInScreenSaver() { return false; }
  void WakeUpScreenSaverAndDPMS() {}
};
struct CApplicationPlayer
{
  bool IsPausedPlayback() { return false; }
};
struct Components
{
  template<class T>
  std::shared_ptr<T> GetComponent() const
  {
    return std::make_shared<T>();
  }
};
static int guiEffects = 0;
struct Announcements
{
  void Announce(int, const char*) { ++guiEffects; }
};
struct Messenger
{
  std::vector<std::function<void()>> queue;
  void PostCallback(std::function<void()> f) { queue.push_back(std::move(f)); }
  void SendMsg(int) {}
  void Drain()
  {
    auto work = std::move(queue);
    queue.clear();
    for (auto& f : work)
      f();
  }
};
struct WM
{
  int GetActiveWindow() { return 0; }
  template<class T>
  T* GetWindow(int)
  {
    return nullptr;
  }
};
struct GUI
{
  WM& GetWindowManager()
  {
    static WM w;
    return w;
  }
};
struct CServiceBroker
{
  static Components& GetAppComponents()
  {
    static Components c;
    return c;
  }
  static Messenger* GetAppMessenger()
  {
    static Messenger m;
    return &m;
  }
  static Announcements* GetAnnouncementManager()
  {
    static Announcements a;
    return &a;
  }
  static GUI* GetGUI()
  {
    static GUI g;
    return &g;
  }
};
struct cec_logical_addresses
{
  bool empty = true;
  void Clear() { empty = true; }
  void Set(int) { empty = false; }
  bool IsEmpty() const { return empty; }
  bool operator!=(const cec_logical_addresses& rhs) const { return empty != rhs.empty; }
};
struct Native
{
  bool active = false;
  int activations = 0, power = 0;
  bool IsLibCECActiveSource() { return active; }
  void SetActiveSource()
  {
    active = true;
    ++activations;
  }
  void PowerOnDevices(int) { ++power; }
  std::string GetDeviceOSDName(int) { return "TV"; }
};
static void DrainCecCallbacks(Native*)
{
}
struct Localize
{
  std::string Get(int) { return "TV"; }
} g_localizeStrings;
struct StringUtils
{
  template<class... T>
  static std::string Format(const char*, T...)
  {
    return "TV";
  }
};
class CPeripheralCecAdapter : public std::enable_shared_from_this<CPeripheralCecAdapter>
{
public:
  Native* m_cecAdapter;
  CCriticalSection m_lifecycleSection, m_critSection;
  std::atomic<bool> m_retired{false}, m_bStop{false};
  std::atomic<uint64_t> m_sourceEventGeneration{7};
  bool m_bPlaybackPaused = false, m_bOnPlayReceived = false, m_bGoingToStandby = false,
       m_bIsReady = false;
  std::chrono::steady_clock::time_point m_lastSourceDeactivatedTime;
  bool activateOnStartup = false;
  int GetSettingInt(const char*) { return LOCALISED_ID_NONE; }
  bool GetSettingBool(const char*) { return activateOnStartup; }
  static void CecSourceActivated(void*, CEC::cec_logical_address, uint8_t);
  void OnCecSourceActivated(CEC::cec_logical_address, uint8_t /*STATE_PARAMETER*/, uint64_t);
  void RetireCecCallbacks();
};
class CPeripheralCecAdapterUpdateThread
{
public:
  CPeripheralCecAdapter* m_adapter;
  struct
  {
    cec_logical_addresses wakeDevices;
  } m_configuration;
  CCriticalSection m_critSection;
  bool m_bIsUpdating = true;
  bool WaitReady() { return true; }
  void UpdateMenuLanguage() {}
  std::string UpdateAudioSystemStatus() { return {}; }
  bool SetInitialConfiguration();
};
'''

MAIN = r'''

static void WriteMarker(bool active)
{
  FILE* f = fopen("/run/kodi_cec_startup_done", "w");
  assert(f);
  fputs(active ? "1\n" : "0\n", f);
  fclose(f);
}
static int ReadMarker()
{
  FILE* f = fopen("/run/kodi_cec_startup_done", "r");
  assert(f);
  const int value = fgetc(f);
  fclose(f);
  return value - '0';
}
static void CheckReconnect(CPeripheralCecAdapter* owner, Native& native, bool expected)
{
  // Model a fresh native connection, retaining only the saved marker.
  // Production ResetMembers/Process clear the separate standby activation flag.
  native.active = false;
  native.activations = 0;
  CPeripheralCecAdapterUpdateThread query;
  query.m_adapter = owner;
  assert(query.SetInitialConfiguration());
  assert(native.activations == int(expected));
}
int main(int argc, char** argv)
{
  assert(argc == 2);
  marker = argv[1];
  for (bool active : {false, true})
  {
    for (int schedule = 0; schedule < 3; ++schedule)
    {
      WriteMarker(!active);
      Native native;
      native.active = active;
      auto owner = std::make_shared<CPeripheralCecAdapter>();
      owner->m_cecAdapter = &native;
      // Soft reconnect must follow the marker, regardless of cold-boot preference.
      owner->activateOnStartup = !active;
      const int effects = guiEffects;
      CPeripheralCecAdapter::CecSourceActivated(owner.get(), 4, active);
      if (schedule == 1)
        owner->RetireCecCallbacks();
      else if (schedule == 2)
        owner->m_bStop = true;
      CServiceBroker::GetAppMessenger()->Drain();
      assert(ReadMarker() == int(active));
      assert(guiEffects == effects + (schedule == 0 ? 1 : 0));
      CheckReconnect(owner.get(), native, active);
    }
    // A newer observation must survive each older queued GUI delivery.
    WriteMarker(!active);
    Native native;
    auto owner = std::make_shared<CPeripheralCecAdapter>();
    owner->m_cecAdapter = &native;
    native.active = !active;
    CPeripheralCecAdapter::CecSourceActivated(owner.get(), 4, !active);
    native.active = active;
    CPeripheralCecAdapter::CecSourceActivated(owner.get(), 4, active);
    auto callbacks = std::move(CServiceBroker::GetAppMessenger()->queue);
    CServiceBroker::GetAppMessenger()->queue.clear();
    assert(callbacks.size() == 2);
    for (auto& callback : callbacks)
    {
      callback();
      assert(ReadMarker() == int(active));
    }
    CheckReconnect(owner.get(), native, active);

    // Native observation survives destruction; weak GUI delivery must not run.
    const int effects = guiEffects;
    CPeripheralCecAdapter::CecSourceActivated(owner.get(), 4, active);
    owner.reset();
    CServiceBroker::GetAppMessenger()->Drain();
    assert(ReadMarker() == int(active));
    assert(guiEffects == effects);

    // Missing marker retains the existing cold-boot activation preference.
    assert(std::remove(marker.c_str()) == 0);
    auto cold = std::make_shared<CPeripheralCecAdapter>();
    cold->m_cecAdapter = &native;
    cold->activateOnStartup = active;
    CheckReconnect(cold.get(), native, active);
  }
  puts("PASS CEC source persistence: active/inactive, delivered/retired/stopped, "
       "newer observation, expired owner and cold-boot controls");
}
'''

def run(source_path):
    source = source_path.read_text()
    declarations = PRE
    handler = method(source, 'void CPeripheralCecAdapter::OnCecSourceActivated(')
    # Permit the pre-fix signature when running a saved negative control.
    state_parameter = ',bool' if 'bool isActive' in handler else ''
    declarations = declarations.replace('/*STATE_PARAMETER*/', state_parameter)
    markers = [
        'void CPeripheralCecAdapter::CecSourceActivated(',
        'void CPeripheralCecAdapter::OnCecSourceActivated(',
        'void CPeripheralCecAdapter::RetireCecCallbacks(',
        'bool CPeripheralCecAdapterUpdateThread::SetInitialConfiguration(',
    ]
    with tempfile.TemporaryDirectory(prefix='cec-source-state-') as directory:
        out = Path(directory)
        cpp = out / 'source-state.cpp'
        cpp.write_text(declarations + '\n'.join(method(source, m) for m in markers) + MAIN)
        exe = out / 'source-state'
        subprocess.run([
            os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
            '-Wno-unused-parameter', '-fsanitize=address,undefined', '-fno-pie', '-no-pie',
            str(cpp), '-o', str(exe),
        ], check=True, timeout=60)
        subprocess.run([str(exe), str(out / 'marker')], check=True, timeout=15,
                       env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0',
                            'UBSAN_OPTIONS': 'halt_on_error=1'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path,
                        default=ROOT / 'xbmc/peripherals/devices/PeripheralCecAdapter.cpp')
    args = parser.parse_args()
    run(args.source)
