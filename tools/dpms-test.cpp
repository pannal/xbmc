/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "powermanagement/DPMSControl.h"
#include "windowing/amlogic/AMLDPMSSupport.h"
#include "utils/log.h"
#include <atomic>
#include <cassert>
#include <filesystem>
#include <fstream>
#include <memory>
#include <string>
#include <thread>
#include <type_traits>
#include <vector>
#include <unistd.h>

// Recording dependencies for the production ApplicationPowerHandling methods.
namespace ANNOUNCEMENT { enum { GUI }; }
struct CVariant
{
  enum { VariantTypeObject };
  bool value{false};
  explicit CVariant(int) {}
  CVariant& operator[](const char*) { return *this; }
  CVariant& operator=(bool b) { value = b; return *this; }
};
struct Announcements
{
  std::vector<std::string> events;
  bool shuttingDown{false};
  std::function<void(const std::string&)> onAnnounce;
  void Announce(int, const char* name)
  {
    events.emplace_back(name);
    if (onAnnounce) onAnnounce(name);
  }
  void Announce(int, const char* name, CVariant data)
  { events.emplace_back(name); shuttingDown = data.value; }
};
struct Inhibitor
{
  bool active{false};
  explicit operator bool() const { return active; }
  void Release() { active = false; }
};
struct ScreenSaver { Inhibitor CreateInhibitor() { return {true}; } };
struct CSettings
{
  enum { SETTING_SCREENSAVER_MODE, SETTING_POWERMANAGEMENT_DISPLAYSOFF,
         SETTING_SCREENSAVER_DISABLEFORAUDIO, SETTING_SCREENSAVER_TIME,
         SETTING_SCREENSAVER_USEDIMONPAUSE };
  bool disableForAudio{false};
  int timeout{1};
  std::string mode{"screensaver.xbmc.builtin.dim"};
  std::string GetString(int) { return mode; }
  int GetInt(int key) { return key == SETTING_POWERMANAGEMENT_DISPLAYSOFF ? timeout : 5; }
  bool GetBool(int key) { return key == SETTING_SCREENSAVER_DISABLEFORAUDIO && disableForAudio; }
};
struct SettingsComponent
{
  std::shared_ptr<CSettings> settings = std::make_shared<CSettings>();
  std::shared_ptr<CSettings> GetSettings() { return settings; }
};
struct CApplicationPlayer
{
  bool video{false}, audio{false}, paused{false};
  bool opening{false}, retiring{false}, pendingOpen{false};
  std::function<void()> afterVideoCheck;
  std::function<void()> afterPausedCheck;
  bool IsPlaying() { return video || audio || opening; }
  bool HasPlayer() { return IsPlaying() || retiring; }
  bool HasPendingOpen() { return pendingOpen; }
  bool IsPlayingVideo()
  {
    const bool result = video;
    if (afterVideoCheck) afterVideoCheck();
    return result;
  }
  bool IsPlayingAudio() { return audio; }
  bool IsPaused()
  {
    const bool result = paused;
    if (afterPausedCheck) afterPausedCheck();
    return result;
  }
};
class CApplicationPowerHandling;
struct Components
{
  std::shared_ptr<CApplicationPlayer> player = std::make_shared<CApplicationPlayer>();
  std::shared_ptr<CApplicationPowerHandling> power;
  template<class T> std::shared_ptr<T> GetComponent() const
  {
    if constexpr (std::is_same_v<T, CApplicationPlayer>) return player;
    else return power;
  }
};
constexpr int WINDOW_SCREENSAVER = 1, WINDOW_LOGIN_SCREEN = 2;
struct WindowManager
{
  bool IsWindowActive(int) { return false; }
  int GetActiveWindow() { return 0; }
  bool HasModalDialog(bool) { return false; }
  void ActivateWindow(int) {}
};
// Recording add-on/PVR dependencies for the full production ActivateScreenSaver.
namespace ADDON
{
enum class AddonType { SCREENSAVER };
enum class OnlyEnabled { CHOICE_YES };
struct CAddon
{
  virtual ~CAddon() = default;
  std::string path{"screensaver.py"};
  std::string LibPath() const { return path; }
};
using AddonPtr = std::shared_ptr<CAddon>;
}
struct AddonManager
{
  bool GetAddon(const std::string& id, ADDON::AddonPtr& addon,
                ADDON::AddonType, ADDON::OnlyEnabled)
  {
    addon = std::make_shared<ADDON::CAddon>();
    if (id != "screensaver.python") addon->path = "screensaver.so";
    return true;
  }
};
namespace PVR::GUI { struct Channels { bool IsRunningChannelScan() { return false; } }; }
struct PVRManager
{
  template<class T> T Get() { return {}; }
};
struct CScriptInvocationManager
{
  static CScriptInvocationManager& GetInstance()
  { static CScriptInvocationManager instance; return instance; }
  bool HasLanguageInvoker(const std::string& path) { return path == "screensaver.py"; }
  bool Stop(const std::string&) { return false; }
  void ExecuteAsync(const std::string&, ADDON::AddonPtr) {}
};
constexpr char SCRIPT_ALARM[] = "sssssscreensaver";
struct AlarmClock { void Stop(const char*, bool) {} };
static AlarmClock g_alarmClock;
struct GUI
{
  WindowManager manager;
  WindowManager& GetWindowManager() { return manager; }
};
struct Window
{
  std::shared_ptr<CDPMSSupport> dpms;
  ScreenSaver screenSaver;
  bool hasScreenSaver{false};
  std::shared_ptr<CDPMSSupport> GetDPMSManager() { return dpms; }
  ScreenSaver* GetOSScreenSaver() { return hasScreenSaver ? &screenSaver : nullptr; }
};
struct CServiceBroker
{
  static inline Window window;
  static inline Announcements announcements;
  static inline SettingsComponent settings;
  static inline Components components;
  static inline GUI gui;
  static inline AddonManager addons;
  static inline PVRManager pvr;
  static Window* GetWinSystem() { return &window; }
  static Announcements* GetAnnouncementManager() { return &announcements; }
  static SettingsComponent* GetSettingsComponent() { return &settings; }
  static Components& GetAppComponents() { return components; }
  static GUI* GetGUI() { return &gui; }
  static AddonManager& GetAddonMgr() { return addons; }
  static PVRManager& GetPVRManager() { return pvr; }
};
class CApplicationPowerHandling
{
public:
  bool ToggleDPMS(bool);
  bool WakeDPMS();
  void ProcessDPMS();
  bool ResetDPMS();
  void ReleaseAutomaticDPMS();
  bool ReleaseDPMS(const std::shared_ptr<CDPMSSupport>& dpms);
  bool WakeUpScreenSaverAndDPMS(bool bPowerOffKeyPressed = false);
  void AnnounceScreenSaverDeactivated(bool);
  void CheckScreenSaverAndDPMS();
  bool PlaybackPreventsDPMS() const;
  bool IsDPMSActive() const { return m_dpmsControl.GetState().active; }
  bool IsPythonScreenSaverActive() const { return m_pythonScreenSaver != nullptr; }
  void SetRenderGUI(bool b) { render = b; }
  bool WakeUpScreenSaver(bool = false)
  {
    m_screensaverActive = false;
    m_pythonScreenSaver.reset();
    return true;
  }
  void CheckOSScreenSaverInhibitionSetting();
  void ResetScreenSaverTimer() { ++timerResets; m_screenSaverTimer.StartZero(); }
  void ActivateScreenSaver(bool forceType = false);
  CDPMSControl m_dpmsControl;
  bool m_screensaverActive{false};
  ADDON::AddonPtr m_pythonScreenSaver;
  bool m_screensaverStartedWithPlayback{false};
  int m_iScreenSaveLock{0};
  std::string m_screensaverIdInUse;
  bool render{true};
  int timerResets{0};
  bool m_bResetScreenSaver{false}, m_bInhibitScreenSaver{false}, m_bScreenSaverWakingUp{false};
  Inhibitor m_screensaverInhibitor;
  Inhibitor m_globalScreensaverInhibitor;
  struct Timer
  {
    float elapsed{61.0f};
    void StartZero() { elapsed = 0.0f; }
    bool IsRunning() { return true; }
    float GetElapsedSeconds() { return elapsed; }
  } m_screenSaverTimer;
};
CDPMSSupport::CDPMSSupport() = default;
bool CDPMSSupport::IsModeSupported(PowerSavingMode mode) const
{ return std::find(m_supportedModes.begin(), m_supportedModes.end(), mode) != m_supportedModes.end(); }
// Admission/native retirement are recording stand-ins in this cleanup fixture.
// Actual session/window lifecycle behavior has separate production-method tests.
struct CAMLDisplayLifecycle
{
  struct Mutation
  {
    explicit Mutation(CAMLDisplayLifecycle&) {}
    explicit operator bool() const { return true; }
  };
};
struct CWinSystemAmlogicGLESContext
{
  bool PrepareForShutdown();
  void GuiABFinish(const char*) {}
  void SetNativeGuiWait(bool) {}
  bool RetireNativeTransactions()
  {
    assert(CServiceBroker::components.power->IsDPMSActive() == expectUnreleased);
    ++retired;
    return true;
  }
  bool m_shutdownRequested{false};
  bool expectUnreleased{false};
  int retired{0};
  CAMLDisplayLifecycle m_displayLifecycle;
  std::unique_ptr<CAMLDisplayLifecycle::Mutation> m_shutdownAdmission;
};
struct Backend : CDPMSSupport
{
  CApplicationPowerHandling& app;
  bool ready{true}, enableOK{true}, disableOK{true}, inhibited{false};
  int enabled{0}, disabled{0}, refreshed{0};
  std::function<void()> duringEnable;
  std::function<void()> duringDisable;
  explicit Backend(CApplicationPowerHandling& a) : app(a) { m_supportedModes.push_back(OFF); }
  bool CanEnablePowerSaving() const override { return ready; }
  bool EnablePowerSaving(PowerSavingMode) override
  {
    assert(app.render && !app.IsDPMSActive());
    ++enabled;
    if (duringEnable) duringEnable();
    return inhibited = enableOK;
  }
  bool DisablePowerSaving() override
  {
    if (app.IsDPMSActive()) assert(!app.render);
    ++disabled;
    if (duringDisable) duringDisable();
    if (!disableOK) return false;
    inhibited = false;
    return true;
  }
  bool HasPersistentPowerSaving() const override { return true; }
  bool PowerSavingInterruptsAudio() const override { return true; }
  void OnPowerSavingDisabled() override
  {
    assert(app.render && !app.IsDPMSActive() && !inhibited);
    ++refreshed;
  }
};
struct LegacyBackend : Backend
{
  using Backend::Backend;
  // Exercise the actual capability defaults used by existing backends.
  bool HasPersistentPowerSaving() const override
  { return CDPMSSupport::HasPersistentPowerSaving(); }
  bool PowerSavingInterruptsAudio() const override
  { return CDPMSSupport::PowerSavingInterruptsAudio(); }
  bool CanEnablePowerSaving() const override
  { return CDPMSSupport::CanEnablePowerSaving(); }
  void OnPowerSavingDisabled() override
  { CDPMSSupport::OnPowerSavingDisabled(); }
};

static void ReviewRegression(const std::string& caseName)
{
  // Reuse the same queue scenarios with both backend policies. Preserve the
  // existing legacy/legacy-opening case names for focused invocations.
  const bool legacy = caseName == "legacy" || caseName.rfind("legacy-", 0) == 0;
  const std::string name = legacy && caseName != "legacy" && caseName != "legacy-opening"
                               ? caseName.substr(7) : caseName;
  CApplicationPowerHandling app;
  CServiceBroker::components.player = std::make_shared<CApplicationPlayer>();
  CServiceBroker::settings.settings = std::make_shared<CSettings>();
  auto backend = legacy ?
      std::static_pointer_cast<Backend>(std::make_shared<LegacyBackend>(app)) :
      std::make_shared<Backend>(app);
  if (legacy)
  {
    assert(!backend->HasPersistentPowerSaving());
    assert(!backend->PowerSavingInterruptsAudio());
  }
  CServiceBroker::window.dpms = backend;
  CServiceBroker::window.hasScreenSaver = true;
  CServiceBroker::announcements = {};
  auto& events = CServiceBroker::announcements.events;
  app.CheckOSScreenSaverInhibitionSetting();
  assert(app.m_globalScreensaverInhibitor);

  if (name == "manual-queued-entry" || name == "manual-deferred-ready" ||
      name == "manual-deferred-not-ready" || name == "manual-explicit-order")
  {
    assert(app.ToggleDPMS(false));
    if (name != "manual-queued-entry")
    {
      backend->ready = false;
      app.ProcessDPMS();
      assert(!app.IsDPMSActive() && backend->enabled == 0);
    }
    assert(app.ToggleDPMS(true));
    if (name == "manual-explicit-order")
    {
      assert(app.ToggleDPMS(true));
      assert(app.ToggleDPMS(true));
    }
    if (name == "manual-deferred-not-ready")
    {
      app.ProcessDPMS();
      assert(!app.IsDPMSActive() && backend->enabled == 0 && events.empty());
    }
    backend->ready = true;
    app.ProcessDPMS();
    assert(app.IsDPMSActive() && app.m_dpmsControl.GetState().manual);
    assert(!app.WakeUpScreenSaverAndDPMS());
    if (name == "manual-explicit-order")
    {
      app.ProcessDPMS(); assert(!app.IsDPMSActive());
      app.ProcessDPMS(); assert(app.IsDPMSActive() && app.m_dpmsControl.GetState().manual);
      assert(backend->enabled == 2 && backend->disabled == 1);
    }
    else
      assert(backend->enabled == 1 && backend->disabled == 0);
    app.ProcessDPMS(); // No stale automatic entry or wake remains.
    assert(app.IsDPMSActive() && !app.m_dpmsControl.HasRequests());
    return;
  }
  if (name == "manual-inflight-entry")
  {
    backend->duringEnable = [&] {
      std::thread input([&] { assert(app.ToggleDPMS(true)); });
      input.join();
    };
    assert(app.ToggleDPMS(false)); app.ProcessDPMS();
    assert(app.IsDPMSActive());
    backend->duringEnable = {};
    app.ProcessDPMS();
    assert(!app.IsDPMSActive() && !app.m_dpmsControl.HasRequests());
    assert(backend->enabled == 1 && backend->disabled == 1);
    return;
  }
  if (name == "manual-cancel-wake-entry-failure")
  {
    assert(app.ToggleDPMS(false));
    assert(!app.WakeUpScreenSaverAndDPMS()); // Queued no-op wake replaces idle entry.
    assert(app.ToggleDPMS(true));
    backend->enableOK = false;
    app.ProcessDPMS();
    assert(!app.IsDPMSActive() && !app.m_dpmsControl.HasRequests());
    assert(backend->enabled == 1 && events.empty());
    backend->enableOK = true;
    assert(app.ToggleDPMS(false)); // Removed wake must not strand the idle barrier.
    app.ProcessDPMS();
    assert(app.IsDPMSActive());
    return;
  }

  if (name == "manual-order")
  {
    assert(legacy);
    // Acceptance must not be confused with completion, even for the existing
    // backends. Three explicit toggles commit in order across FrameMove calls.
    assert(app.ToggleDPMS(true));
    assert(app.ToggleDPMS(true));
    assert(app.ToggleDPMS(true));
    assert(!app.IsDPMSActive() && app.render && events.empty());
    assert(backend->enabled == 0 && backend->disabled == 0);
    app.ProcessDPMS(); assert(app.IsDPMSActive() && !app.render);
    assert(!app.WakeUpScreenSaverAndDPMS());
    app.ProcessDPMS(); assert(!app.IsDPMSActive() && app.render);
    app.ProcessDPMS(); assert(app.IsDPMSActive() && !app.render);
    assert(events == (std::vector<std::string>{"OnDPMSActivated", "OnDPMSDeactivated",
                                               "OnDPMSActivated"}));
    assert(!app.WakeDPMS());
    const auto count = events.size();
    const int attempts = backend->disabled;
    const int refreshes = backend->refreshed;
    assert(app.ResetDPMS());
    assert(app.IsDPMSActive() && !app.render && !app.m_globalScreensaverInhibitor);
    assert(backend->disabled == attempts && backend->refreshed == refreshes);
    assert(events.size() == count && !app.m_dpmsControl.HasRequests());
    assert(app.ToggleDPMS(true));
    backend->disableOK = false;
    app.ProcessDPMS();
    assert(app.IsDPMSActive() && !app.render && events.size() == count);
    assert(!app.m_globalScreensaverInhibitor && backend->refreshed == refreshes);
    assert(!app.WakeUpScreenSaverAndDPMS()); // Manual policy survives failure.
    app.ProcessDPMS(); assert(backend->disabled == attempts + 1); // No retry loop.
    backend->disableOK = true;
    assert(app.ToggleDPMS(true)); app.ProcessDPMS();
    assert(!app.IsDPMSActive() && app.render && app.m_globalScreensaverInhibitor);
    assert(events.size() == count + 1 && events.back() == "OnDPMSDeactivated");
    return;
  }
  if (name == "entry-failure")
  {
    assert(legacy);
    backend->enableOK = false;
    assert(app.ToggleDPMS(false)); app.ProcessDPMS();
    assert(!app.IsDPMSActive() && app.render && events.empty());
    assert(app.m_globalScreensaverInhibitor && app.timerResets == 1);
    app.ProcessDPMS(); assert(backend->enabled == 1); // No retry loop.
    backend->enableOK = true;
    assert(app.ToggleDPMS(false)); app.ProcessDPMS();
    assert(app.IsDPMSActive() && !app.render);
    assert(app.WakeUpScreenSaverAndDPMS()); app.ProcessDPMS();
    assert(!app.IsDPMSActive() && app.render && app.m_globalScreensaverInhibitor);
    assert(!app.WakeUpScreenSaverAndDPMS()); // Subsequent navigation is delivered.
    return;
  }
  if (name == "unsupported")
  {
    assert(legacy);
    struct UnsupportedBackend : LegacyBackend
    {
      explicit UnsupportedBackend(CApplicationPowerHandling& power) : LegacyBackend(power)
      { m_supportedModes.clear(); }
    };
    auto unsupported = std::make_shared<UnsupportedBackend>(app);
    CServiceBroker::window.dpms = unsupported;
    assert(!app.ToggleDPMS(true) && !app.ToggleDPMS(false));
    assert(!app.m_dpmsControl.HasRequests() && events.empty());
    assert(unsupported->enabled == 0 && unsupported->disabled == 0);
    CServiceBroker::window.dpms.reset();
    assert(!app.ToggleDPMS(true) && !app.ToggleDPMS(false));
    assert(!app.m_dpmsControl.HasRequests() && events.empty());
    // Losing support after acceptance must not fabricate a successful entry.
    CServiceBroker::window.dpms = backend;
    assert(app.ToggleDPMS(false));
    CServiceBroker::window.dpms = unsupported;
    app.ProcessDPMS();
    assert(!app.IsDPMSActive() && app.render && events.empty());
    assert(!app.m_dpmsControl.HasRequests());
    assert(unsupported->enabled == 0 && unsupported->disabled == 0);
    CServiceBroker::window.dpms = backend;
    assert(app.ToggleDPMS(false)); app.ProcessDPMS();
    assert(app.IsDPMSActive());
    assert(app.WakeUpScreenSaverAndDPMS()); app.ProcessDPMS();
    assert(!app.IsDPMSActive() && app.render);
    return;
  }

  if (name == "resume-before-entry")
  {
    auto player = CServiceBroker::components.player;
    player->video = true;
    player->paused = true;
    bool resume = true;
    player->afterPausedCheck = [&] {
      if (resume)
      {
        resume = false;
        player->paused = false;
        assert(!app.WakeDPMS());
      }
    };
    app.CheckScreenSaverAndDPMS();
    player->afterPausedCheck = {};
    app.ProcessDPMS();
    assert(!app.IsDPMSActive() && backend->enabled == 0 && app.render);
    // Also cover a queued request that was deferred before playback resumed.
    player->paused = true;
    assert(app.ToggleDPMS(false));
    backend->ready = false;
    app.ProcessDPMS();
    player->paused = false;
    backend->ready = true;
    app.ProcessDPMS();
    assert(!app.IsDPMSActive() && backend->enabled == 0);
    assert(app.ToggleDPMS(true));
    app.ProcessDPMS();
    assert(app.IsDPMSActive()); // Explicit manual sleep still overrides playback.
    return;
  }

  if (name == "screensaver-activation")
  {
    auto player = CServiceBroker::components.player;
    auto settings = CServiceBroker::settings.settings;
    settings->mode = "screensaver.python";
    // Capture before announcing activation: an add-on can react to the event.
    player->audio = true;
    CServiceBroker::announcements.onAnnounce = [&](const std::string& event) {
      if (event == "OnScreensaverActivated") player->audio = false;
    };
    app.ActivateScreenSaver();
    assert(app.IsPythonScreenSaverActive() && app.m_screensaverStartedWithPlayback);
    CServiceBroker::announcements.onAnnounce = {};
    app.WakeUpScreenSaver();
    assert(!app.IsPythonScreenSaverActive());
    // A fresh idle activation must overwrite the previous playback snapshot.
    app.ActivateScreenSaver();
    assert(app.IsPythonScreenSaverActive() && !app.m_screensaverStartedWithPlayback);
    app.WakeUpScreenSaver();
    player->pendingOpen = true; // No owned player, but accepted playback exists.
    app.ActivateScreenSaver();
    assert(app.m_screensaverStartedWithPlayback);
    app.WakeUpScreenSaver();
    player->pendingOpen = false;
    player->retiring = true; // Stopped player still owned during retirement.
    app.ActivateScreenSaver();
    assert(app.m_screensaverStartedWithPlayback);
    app.WakeUpScreenSaver();
    CServiceBroker::components.player.reset();
    app.ActivateScreenSaver();
    assert(app.IsPythonScreenSaverActive() && !app.m_screensaverStartedWithPlayback);
    app.WakeUpScreenSaver();
    CServiceBroker::components.player = player;
    settings->mode = "screensaver.binary";
    app.ActivateScreenSaver(true);
    assert(!app.IsPythonScreenSaverActive() && app.m_screensaverStartedWithPlayback);
    app.WakeUpScreenSaver();
    player->retiring = false;
    settings->mode = "screensaver.xbmc.builtin.dim";
    app.ActivateScreenSaver();
    assert(!app.IsPythonScreenSaverActive() && !app.m_screensaverStartedWithPlayback);
    return;
  }

  if (name == "screensaver-video-advance" || name == "screensaver-video-classification" ||
      name == "python-screensaver-video-advance" || name == "python-screensaver-video-classification")
  {
    auto player = CServiceBroker::components.player;
    player->audio = true;
    const bool python = name.find("python-") == 0;
    if (python) CServiceBroker::settings.settings->mode = "screensaver.python";
    app.m_screenSaverTimer.elapsed = 301.0f;
    app.CheckScreenSaverAndDPMS();
    app.ProcessDPMS();
    assert(app.m_screensaverActive && app.IsPythonScreenSaverActive() == python);
    auto staysAwake = [&] {
      app.CheckScreenSaverAndDPMS();
      app.ProcessDPMS();
      assert(!app.IsDPMSActive() && backend->enabled == 0 && app.render);
      assert(app.timerResets == 0 && app.m_screensaverActive);
    };
    // Audio advances into a real video while a visual screensaver remains
    // active. No user input or second callback accompanies stream discovery.
    player->audio = false;
    player->retiring = true;
    staysAwake();
    player->pendingOpen = true;
    player->paused = true;
    staysAwake();
    player->retiring = false;
    staysAwake();
    player->pendingOpen = false;
    player->opening = true;
    player->paused = false;
    assert(!app.WakeDPMS());
    if (name.find("classification") != std::string::npos)
      player->afterVideoCheck = [&] { player->video = true; };
    staysAwake();
    player->afterVideoCheck = {};
    player->opening = false;
    player->video = true;
    staysAwake();
    // A request admitted while paused must also be rejected if playback resumes
    // before it commits, even with the visual screensaver still active.
    player->paused = true;
    app.CheckScreenSaverAndDPMS();
    assert(app.m_dpmsControl.HasRequests());
    player->paused = false;
    app.ProcessDPMS();
    assert(!app.IsDPMSActive() && backend->enabled == 0 && app.timerResets == 0);
    player->video = false;
    app.CheckScreenSaverAndDPMS();
    app.ProcessDPMS();
    assert(app.IsDPMSActive()); // A genuine stop remains sleep-eligible.
    return;
  }

  if (name == "opening-audio" || name == "opening-video")
  {
    auto player = CServiceBroker::components.player;
    // Prior unpaused audio permits a visual screensaver and leaves idle time
    // beyond the display-off timeout. Starting the next item must not reset it.
    player->audio = true;
    if (name == "opening-audio") app.m_screenSaverTimer.elapsed = 301.0f;
    app.CheckScreenSaverAndDPMS();
    assert(!app.m_dpmsControl.HasRequests() && app.timerResets == 0);
    if (name == "opening-audio") assert(app.m_screensaverActive);
    player->audio = false;
    player->opening = true;
    assert(!app.WakeDPMS());
    app.ProcessDPMS();
    assert(app.timerResets == 0);
    if (name == "opening-video")
      player->afterVideoCheck = [&] { player->video = true; };
    app.CheckScreenSaverAndDPMS();
    assert(!app.m_dpmsControl.HasRequests());
    app.ProcessDPMS();
    assert(!app.IsDPMSActive() && backend->enabled == 0 && app.render);
    player->afterVideoCheck = {};
    player->opening = false;
    player->video = name == "opening-video";
    player->audio = !player->video;
    // No second playback-start callback when streams become known.
    app.CheckScreenSaverAndDPMS();
    app.ProcessDPMS();
    assert(!app.IsDPMSActive() && backend->enabled == 0);
    if (player->audio) assert(app.timerResets == 0);
    player->video = false;
    player->audio = false;
    app.m_screenSaverTimer.elapsed = 61.0f;
    app.CheckScreenSaverAndDPMS();
    assert(app.m_dpmsControl.HasRequests());
    app.ProcessDPMS();
    assert(app.IsDPMSActive());
    return;
  }
  if (name == "player-advance")
  {
    auto player = CServiceBroker::components.player;
    // The old player has stopped, but its end event has not been dispatched.
    player->retiring = true;
    auto staysAwake = [&] {
      app.CheckScreenSaverAndDPMS();
      assert(!app.m_dpmsControl.HasRequests());
      app.ProcessDPMS();
      assert(!app.IsDPMSActive() && backend->enabled == 0 && app.timerResets == 0);
    };
    staysAwake();
    // PlayNext queues a replacement behind close. Close can reset cached speed
    // before retirement; a pending open must not be mistaken for paused video.
    player->pendingOpen = true;
    player->paused = true;
    staysAwake();
    player->retiring = false;
    staysAwake();
    player->pendingOpen = false;
    player->opening = true;
    player->paused = false;
    assert(!app.WakeDPMS());
    staysAwake();
    player->opening = false;
    player->audio = true;
    staysAwake();
    // A genuine stop with no replacement becomes eligible after retirement.
    player->audio = false;
    player->retiring = true;
    staysAwake();
    player->retiring = false;
    app.CheckScreenSaverAndDPMS();
    assert(app.m_dpmsControl.HasRequests());
    app.ProcessDPMS();
    assert(app.IsDPMSActive());
    return;
  }
  if (name == "video-screensaver" || name == "legacy-opening")
  {
    if (name == "video-screensaver")
    {
      CServiceBroker::settings.settings->mode = "screensaver.python";
      app.ActivateScreenSaver();
      assert(app.IsPythonScreenSaverActive() && !app.m_screensaverStartedWithPlayback);
      CServiceBroker::components.player->video = true;
    }
    else
      CServiceBroker::components.player->opening = true;
    app.CheckScreenSaverAndDPMS();
    assert(app.m_dpmsControl.HasRequests());
    app.ProcessDPMS();
    assert(app.IsDPMSActive());
    return;
  }

  if (name == "awake-player")
  {
    // Playback starts, resumes, and gapless track changes call WakeDPMS.
    // Already-awake playback must not postpone the visual screensaver.
    const int resets = app.timerResets;
    for (int track = 0; track < 3; ++track)
    {
      assert(!app.WakeDPMS());
      assert(!app.m_dpmsControl.HasRequests());
      app.ProcessDPMS();
      assert(app.timerResets == resets);
      assert(app.render && !app.IsDPMSActive() && events.empty());
      assert(backend->enabled == 0 && backend->disabled == 0 && backend->refreshed == 0);
    }
    assert(app.ToggleDPMS(false)); // No stale wake barrier may block later sleep.
    return;
  }
  if (name == "player-cancel-entry" || name == "player-cancel-deferred")
  {
    assert(app.ToggleDPMS(false));
    if (name == "player-cancel-deferred")
    {
      backend->ready = false;
      app.ProcessDPMS();
    }
    assert(!app.WakeDPMS()); // Still awake, but queued sleep must be cancelled.
    backend->ready = true;
    app.ProcessDPMS();
    assert(!app.m_dpmsControl.HasRequests() && !app.IsDPMSActive() && app.render);
    assert(backend->enabled == 0 && backend->disabled == 0 && events.empty());
    assert(app.timerResets == 1);
    assert(app.ToggleDPMS(false));
    return;
  }

  if (name == "awake-reset-failure")
  {
    backend->disableOK = false;
    assert(!app.ResetDPMS());
    const int attempts = backend->disabled;
    app.ProcessDPMS();
    assert(backend->disabled == attempts && events.empty());
    // No sleep was committed: a failed reset must not strand the timer barrier.
    assert(app.ToggleDPMS(false));
    return;
  }

  assert(app.ToggleDPMS(false));
  if (name == "cancel-entry")
  {
    backend->ready = false;
    if (!legacy)
      app.ProcessDPMS(); // Only the AML-style backend can defer entry.
    else
      assert(backend->CanEnablePowerSaving());
    assert(!app.WakeUpScreenSaverAndDPMS(true));
    app.ProcessDPMS(); // No-op wake must not contaminate the next wake.
    backend->ready = true;
    assert(app.ToggleDPMS(false));
    app.ProcessDPMS();
  }
  else
    app.ProcessDPMS();
  assert(app.IsDPMSActive() && !app.m_globalScreensaverInhibitor);

  if (name == "legacy")
  {
    const auto count = events.size();
    // Playback callbacks run for gapless track transitions as well as starts.
    assert(!app.WakeDPMS());
    app.ProcessDPMS();
    assert(app.IsDPMSActive() && !app.m_dpmsControl.HasRequests());
    // Both system sleep and wake use this entry point. Neither should change
    // a nonpersistent backend's existing display policy.
    assert(app.ResetDPMS());
    assert(app.ResetDPMS());
    assert(app.IsDPMSActive() && !app.render && backend->disabled == 0);
    assert(events.size() == count && !app.m_globalScreensaverInhibitor);
    // Ordinary input still wakes legacy backends and consumes the first key.
    assert(app.WakeUpScreenSaverAndDPMS());
    app.ProcessDPMS();
    assert(!app.IsDPMSActive() && app.render && app.m_globalScreensaverInhibitor);
    assert(backend->refreshed == 0); // Existing backends use the base no-op callback.
    return;
  }
  if (name == "reset-inhibitor")
  {
    assert(app.ResetDPMS());
    assert(app.m_globalScreensaverInhibitor);
    return;
  }
  if (name == "reset-wake")
  {
    assert(app.WakeUpScreenSaverAndDPMS(true));
    assert(app.ResetDPMS());
    assert(app.ToggleDPMS(false));
    app.ProcessDPMS();
  }
  if (name == "failed-wake" || name == "inflight-failure")
  {
    assert(app.WakeUpScreenSaverAndDPMS(true));
    if (name == "inflight-failure")
      backend->duringDisable = [&] {
        std::thread input([&] { assert(app.WakeUpScreenSaverAndDPMS(false)); });
        input.join();
      };
    backend->disableOK = false;
    auto count = events.size();
    app.ProcessDPMS();
    assert(app.IsDPMSActive() && !app.render && events.size() == count);
    assert(!app.ToggleDPMS(false));
    backend->disableOK = true;
    backend->duringDisable = {};
  }
  if (name == "coalesced-ordinary" || name == "coalesced-player")
    assert(app.WakeUpScreenSaverAndDPMS(true));
  if (name == "inflight-power")
    backend->duringDisable = [&] {
      std::thread input([&] { assert(app.WakeUpScreenSaverAndDPMS(true)); });
      input.join();
    };
  if (name == "coalesced-player")
    assert(app.WakeDPMS());
  else
    assert(app.WakeUpScreenSaverAndDPMS(false));
  if (name == "coalesced-power")
    assert(app.WakeUpScreenSaverAndDPMS(true));
  app.ProcessDPMS();
  assert(!app.IsDPMSActive() && app.render);
  assert(events.back() == "OnScreensaverDeactivated");
  const bool powerAction = name == "coalesced-power" || name == "coalesced-ordinary" ||
                           name == "coalesced-player";
  assert(CServiceBroker::announcements.shuttingDown == powerAction);
  assert(app.m_globalScreensaverInhibitor);
  if (name == "inflight-power" || powerAction)
  {
    backend->duringDisable = {};
    const auto count = events.size();
    app.ProcessDPMS(); // Any later request is now a no-op.
    assert(events.size() == count);
    assert(app.ToggleDPMS(false));
    app.ProcessDPMS();
    assert(app.WakeUpScreenSaverAndDPMS());
    app.ProcessDPMS();
    assert(!CServiceBroker::announcements.shuttingDown);
  }
}

int main(int argc, char** argv)
{
  if (argc == 2)
  {
    ReviewRegression(argv[1]);
    return 0;
  }
  for (const char* name : {"opening-audio", "opening-video", "player-advance", "video-screensaver",
                           "screensaver-video-advance", "screensaver-video-classification",
                           "python-screensaver-video-advance", "python-screensaver-video-classification",
                           "screensaver-activation",
                           "legacy-opening", "resume-before-entry",
                           "awake-player", "player-cancel-entry", "player-cancel-deferred",
                           "cancel-entry", "reset-wake", "failed-wake", "legacy",
                           "reset-inhibitor", "awake-reset-failure", "coalesced-ordinary",
                           "coalesced-power", "coalesced-player", "inflight-power",
                           "inflight-failure", "legacy-cancel-entry", "legacy-failed-wake",
                           "legacy-coalesced-ordinary", "legacy-coalesced-power",
                           "legacy-inflight-power", "legacy-inflight-failure",
                           "legacy-manual-order", "legacy-entry-failure", "legacy-unsupported",
                           "manual-queued-entry", "legacy-manual-queued-entry",
                           "manual-deferred-ready", "manual-deferred-not-ready",
                           "manual-explicit-order", "manual-inflight-entry",
                           "manual-cancel-wake-entry-failure"})
    ReviewRegression(name);
  CServiceBroker::announcements = {};
  CServiceBroker::window.hasScreenSaver = false;
  CApplicationPowerHandling app;
  CServiceBroker::components.power =
      std::shared_ptr<CApplicationPowerHandling>(&app, [](auto*) {});
  auto backend = std::make_shared<Backend>(app);
  CServiceBroker::window.dpms = backend;
  auto& events = CServiceBroker::announcements.events;
  // The timer queues entry; only FrameMove's ProcessDPMS commits it.
  app.CheckScreenSaverAndDPMS();
  assert(app.m_dpmsControl.HasRequests());
  assert(!app.IsDPMSActive() && app.render && events.empty());
  backend->ready = false;
  app.ProcessDPMS();
  assert(app.render && !app.IsDPMSActive() && backend->enabled == 0);
  // Input arriving while entry is deferred cancels that stale timer request.
  assert(!app.WakeUpScreenSaverAndDPMS());
  backend->ready = true;
  app.ProcessDPMS();
  assert(!app.IsDPMSActive() && backend->enabled == 0);
  assert(app.ToggleDPMS(false));
  app.ProcessDPMS();
  assert(app.IsDPMSActive() && !app.render && backend->inhibited);
  assert(events.back() == "OnDPMSActivated");
  // Real renderGUI=false wake: no readiness wait despite a delayed reset.
  backend->ready = false;
  assert(app.WakeUpScreenSaverAndDPMS());
  assert(app.WakeUpScreenSaverAndDPMS());
  assert(app.IsDPMSActive() && !app.render && backend->disabled == 0);
  assert(!app.ToggleDPMS(false));
  app.ProcessDPMS();
  assert(!app.IsDPMSActive() && app.render && backend->disabled == 1);
  assert(backend->refreshed == 1 && app.timerResets >= 2);
  assert(events[events.size()-2] == "OnDPMSDeactivated");
  assert(events.back() == "OnScreensaverDeactivated");
  assert(!app.WakeUpScreenSaverAndDPMS()); // next navigation is delivered
  backend->ready = true;
  // Failure reports no wake, no rendering restart, and no automatic retry.
  assert(app.ToggleDPMS(false)); app.ProcessDPMS();
  backend->disableOK = false;
  assert(app.WakeUpScreenSaverAndDPMS());
  auto count = events.size();
  app.ProcessDPMS();
  assert(app.IsDPMSActive() && !app.render && events.size() == count);
  const int attempts = backend->disabled;
  app.ProcessDPMS();
  assert(backend->disabled == attempts);
  backend->disableOK = true;
  assert(app.WakeUpScreenSaverAndDPMS(true)); app.ProcessDPMS();
  assert(CServiceBroker::announcements.shuttingDown);
  assert(!app.IsDPMSActive());
  // Wake racing an in-flight entry is retained after that entry commits.
  backend->duringEnable = [&] { std::thread t([&] { assert(app.WakeDPMS()); }); t.join(); };
  assert(app.ToggleDPMS(false)); app.ProcessDPMS();
  assert(app.IsDPMSActive()); app.ProcessDPMS(); assert(!app.IsDPMSActive());
  backend->duringEnable = {};
  // Explicit toggles retain order and retain manual-DPMS input policy.
  assert(app.ToggleDPMS(true)); assert(app.ToggleDPMS(true)); assert(app.ToggleDPMS(true));
  app.ProcessDPMS(); assert(app.IsDPMSActive());
  assert(!app.WakeUpScreenSaverAndDPMS());
  app.ProcessDPMS(); assert(!app.IsDPMSActive());
  app.ProcessDPMS(); assert(app.IsDPMSActive());
  assert(!app.WakeDPMS()); // player start/resume preserves manual policy
  assert(app.ResetDPMS()); assert(!app.IsDPMSActive() && app.render);
  // Startup/system-wake/teardown reconciliation also clears stale kernel state.
  backend->inhibited = true;
  assert(app.ResetDPMS()); assert(!backend->inhibited);
  backend->enableOK = false;
  assert(app.ToggleDPMS(false)); count = events.size(); app.ProcessDPMS();
  assert(!app.IsDPMSActive() && app.render && events.size() == count);
  // Run the production timer eligibility path, including HDMI-audio protection.
  backend->enableOK = true;
  auto player = CServiceBroker::components.player;
  app.m_screenSaverTimer.elapsed = 61.0f;
  player->audio = true;
  app.CheckScreenSaverAndDPMS();
  assert(!app.m_dpmsControl.HasRequests()); // visual screensavers are allowed
  player->audio = false; player->video = true;
  app.CheckScreenSaverAndDPMS(); assert(!app.m_dpmsControl.HasRequests());
  player->paused = true;
  app.m_screenSaverTimer.elapsed = 61.0f;
  app.CheckScreenSaverAndDPMS(); assert(app.m_dpmsControl.HasRequests());
  app.ProcessDPMS(); assert(app.IsDPMSActive() && player->paused);
  app.WakeDPMS(); app.ProcessDPMS(); assert(!app.IsDPMSActive() && player->paused);
  player->video = false;
  assert(app.ToggleDPMS(false)); app.ProcessDPMS();
  CWinSystemAmlogicGLESContext window;
  backend->disableOK = false;
  window.expectUnreleased = true;
  count = events.size();
  const auto refreshes = backend->refreshed;
  assert(window.PrepareForShutdown() && window.retired == 1);
  assert(app.IsDPMSActive() && !app.render && backend->inhibited);
  assert(events.size() == count && backend->refreshed == refreshes);
  assert(!app.m_dpmsControl.HasRequests());
  backend->disableOK = true;
  window.expectUnreleased = false;
  assert(window.PrepareForShutdown() && !app.IsDPMSActive() && !backend->inhibited);
  assert(window.retired == 2 && window.m_shutdownRequested);
  // Exercise the actual Amlogic adapter with strict readback and failed I/O.
  auto path = std::filesystem::temp_directory_path() / ("dpms-node-" + std::to_string(getpid()));
  {
    CAMLDPMSSupport absent([] { return true; }, [] {}, path.string());
    assert(!absent.IsSupported());
  }
  { std::ofstream f(path); f << "invalid\n"; }
  {
    CAMLDPMSSupport invalid([] { return true; }, [] {}, path.string());
    assert(!invalid.IsSupported());
  }
  { std::ofstream f(path); f << "1\n"; }
  {
    CAMLDPMSSupport driver([] { return true; }, [] {}, path.string());
    assert(driver.IsSupported());
    { std::ifstream f(path); int value; f >> value; assert(value == 0); }
    assert(driver.EnablePowerSaving(CDPMSSupport::OFF));
    { std::ifstream f(path); int value; f >> value; assert(value == 1); }
    assert(!driver.EnablePowerSaving(CDPMSSupport::STANDBY));
    std::filesystem::remove(path);
    assert(!driver.DisablePowerSaving());
    { std::ofstream f(path); f << "1\n"; }
    assert(driver.DisablePowerSaving());
  }
  std::filesystem::remove(path);
}
