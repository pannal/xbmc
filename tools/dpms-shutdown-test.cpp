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
#include <chrono>
#include <cstdio>
#include <fcntl.h>
#include <sys/resource.h>
#include <sys/wait.h>
using namespace std::chrono_literals;
static std::vector<std::string> trace;
static void Record(const char* value) { trace.emplace_back(value); }
struct CEvent {};
struct CFileItem { bool IsDiscImage() const { return false; } bool IsDVDFile() const { return false; } };
struct CPlayerOptions {};
struct IPlayerCallback {};
struct IPlayer
{
  std::string m_name{"video"}, m_type{"video"};
  bool IsPlaying() { return true; }
  bool OpenFile(const CFileItem&, const CPlayerOptions&) { Record("player-open"); return true; }
};
struct CPlayerCoreFactory { std::string GetDefaultPlayer(const CFileItem&) const { return "video"; } };

// Recording dependencies for the production ApplicationPowerHandling methods.
namespace ANNOUNCEMENT { enum { GUI, System }; }
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
    Record(name);
    events.emplace_back(name);
    if (onAnnounce) onAnnounce(name);
  }
  void Announce(int, const char* name, CVariant data)
  { Record(name); events.emplace_back(name); shuttingDown = data.value; if (onAnnounce) onAnnounce(name); }
  void Deinitialize() { Record("announcements-deinit"); }
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
         SETTING_SCREENSAVER_USEDIMONPAUSE, SETTING_MASTERLOCK_STARTUPLOCK };
  void Save() { Record("settings-save"); }
  bool disableForAudio{false};
  int timeout{1};
  std::string mode{"screensaver.xbmc.builtin.dim"};
  std::string GetString(int) { return mode; }
  int GetInt(int key) { return key == SETTING_POWERMANAGEMENT_DISPLAYSOFF ? timeout : 5; }
  bool GetBool(int key) { return key == SETTING_SCREENSAVER_DISABLEFORAUDIO && disableForAudio; }
};
constexpr int LOCK_MODE_EVERYONE = 0;
struct CProfileManager
{
  CProfileManager& GetMasterProfile() { return *this; }
  CProfileManager& GetCurrentProfile() { return *this; }
  int getLockMode() { return LOCK_MODE_EVERYONE; }
  bool UsingLoginScreen() { return false; }
  std::string GetSettingsFile() { return "settings.xml"; }
};
struct Advanced { int m_addonScriptStopTimeout{30}; };
struct SettingsComponent
{
  Advanced advanced;
  Advanced* GetAdvancedSettings() { return &advanced; }
  std::shared_ptr<CProfileManager> GetProfileManager() { return std::make_shared<CProfileManager>(); }
  std::shared_ptr<CSettings> settings = std::make_shared<CSettings>();
  std::shared_ptr<CSettings> GetSettings() { return settings; }
};
struct CApplicationPlayer
{
  void OnPlaybackStopped() {}
  bool m_shutdown{false};
  struct { std::shared_ptr<CFileItem> pItem; CPlayerOptions options; std::string playerName; IPlayerCallback* callback; } m_nextItem;
  std::shared_ptr<IPlayer> m_pPlayer;
  bool m_closingPlayer{false}, m_cleanupPending{false}, m_waitForPlaybackStop{false};
  int m_openGeneration{0};
  struct Expiry { void SetExpired() {} } m_audioStreamUpdate, m_videoStreamUpdate, m_subtitleStreamUpdate;
  bool OpenFile(const CFileItem&, const CPlayerOptions&, const CPlayerCoreFactory&, const std::string&, IPlayerCallback&);
  void CreatePlayer(const CPlayerCoreFactory&, const std::string&, IPlayerCallback&) { m_pPlayer = std::make_shared<IPlayer>(); }
  int closeRetries{0};
  bool ClosePlayer(bool shutdown = false, std::shared_ptr<CEvent> completion = {});
  void ResetPlayer(std::shared_ptr<CEvent> = {})
  {
    Record("player-close");
    if (closeRetries-- > 0) return;
    video = audio = opening = retiring = pendingOpen = false;
    m_pPlayer.reset();
  }
  std::shared_ptr<IPlayer> GetInternal() { return m_pPlayer; }
  void CompleteCloseCompletions() { Record("player-replies"); }
  int& GetSeekHandler() { static int value; return value; }
  bool video{false}, audio{false}, paused{false};
  bool opening{false}, retiring{false}, pendingOpen{false};
  std::function<void()> afterVideoCheck;
  std::function<void()> afterPausedCheck;
  bool IsPlaying() { return video || audio || opening; }
  bool HasPlayer() { return m_pPlayer || IsPlaying() || retiring; }
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
struct CApplicationSkinHandling { void UnloadSkin(); };
struct CApplicationActionListeners { template<class T> void UnregisterActionListener(T*) {} };
struct Components
{
  std::shared_ptr<CApplicationPlayer> player = std::make_shared<CApplicationPlayer>();
  std::shared_ptr<CApplicationPowerHandling> power;
  template<class T> std::shared_ptr<T> GetComponent() const
  {
    if constexpr (std::is_same_v<T, CApplicationPlayer>) return player;
    else if constexpr (std::is_same_v<T, CApplicationPowerHandling>) return power;
    else return std::make_shared<T>();
  }
};
constexpr int WINDOW_HOME = 0, WINDOW_SCREENSAVER = 1, WINDOW_LOGIN_SCREEN = 2, WINDOW_SLIDESHOW = 3;
constexpr int GUI_MSG_CHECK_LOCK = 4, TMSG_GUI_ACTION = 5, ACTION_STOP = 6;
constexpr int GUI_MSG_PLAYBACK_ENDED = 7, GUI_MSG_PLAYBACK_STOPPED = 8;
struct CGUIMessage { CGUIMessage(int, int, int) {} };
struct CGUIWindow { void OnMessage(CGUIMessage&) {} };
struct CAction { explicit CAction(int) {} };
struct WindowManager
{
  int active{WINDOW_HOME};
  bool saverDialogOpen{false}, saverDialogExists{false};
  bool IsWindowActive(int) { return false; }
  int GetActiveWindow() { return active; }
  CGUIWindow* GetWindow(int) { return nullptr; }
  void PreviousWindow() { active = 0; Record("previous-window"); }
  void DeInitialize()
  {
    if (saverDialogOpen)
    {
      saverDialogOpen = false;
      Record("dialog-close");
    }
  }
  void DestroyWindows()
  {
    if (saverDialogExists)
    {
      saverDialogOpen = saverDialogExists = false;
      Record("dialog-destroy");
    }
    active = 0;
    Record("windows-destroy");
  }
  int RemoveThreadMessageByMessageIds(int*) { return 0; }
  bool HasModalDialog(bool) { return false; }
  void ActivateWindow(int id)
  {
    // GUIWindowManager opens the modeless screensaver without changing history.
    if (id == WINDOW_SCREENSAVER)
    {
      saverDialogOpen = saverDialogExists = true;
      Record("dialog-open");
    }
    else
      active = id;
  }
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
  void StopRunningScripts() { Record("scripts-stop"); }
  void Uninitialize() { Record("scripts-uninitialize"); }
  static CScriptInvocationManager& GetInstance()
  { static CScriptInvocationManager instance; return instance; }
  bool HasLanguageInvoker(const std::string& path) { return path == "screensaver.py"; }
  bool Stop(const std::string&) { return false; }
  void ExecuteAsync(const std::string&, ADDON::AddonPtr) {}
};
constexpr char SCRIPT_ALARM[] = "sssssscreensaver";
struct AlarmClock {
  void Stop(const char*, bool) {}
  void Start(const char*, int, const std::string&, bool, bool) { Record("script-alarm"); }
  void StopThread() { Record("alarms-stop"); }
};
static AlarmClock g_alarmClock;
struct GUI
{
  struct Audio { void DeInitialize() { Record("audio-manager-stop"); } } audio;
  Audio& GetAudioManager() { return audio; }
  void Deinit() { Record("gui-deinit"); }
  WindowManager manager;
  WindowManager& GetWindowManager() { return manager; }
};
using CGUIComponent = GUI;
struct Window
{
  std::function<bool()> prepare, destroy;
  bool PrepareForShutdown() { return prepare(); }
  bool DestroyWindowSystem() { return destroy(); }
  void DestroyRenderSystem() { Record("render-destroy"); }
  void DestroyWindow() { Record("window-destroy"); }
  std::shared_ptr<CDPMSSupport> dpms;
  ScreenSaver screenSaver;
  bool hasScreenSaver{false};
  std::shared_ptr<CDPMSSupport> GetDPMSManager() { return dpms; }
  ScreenSaver* GetOSScreenSaver() { return hasScreenSaver ? &screenSaver : nullptr; }
};
using CRenderSystemBase = Window;
using CWinSystemBase = Window;
struct Messenger { void Stop() { Record("messenger-stop"); } void Cleanup() { Record("messenger-cleanup"); }
  void SendMsg(int, int, int, void* action) { delete static_cast<CAction*>(action); } };
struct Jobs { void CancelJobs() { Record("jobs-cancel"); } };
struct ServiceAddons { void Stop() { Record("addons-stop"); } };
struct VFS { std::vector<std::shared_ptr<VFS>> GetAddonInstances() { return {}; } void DisconnectAll() {} };
struct Playlist { void Clear() { Record("playlist-clear"); } };
struct CServiceBroker
{
  struct PowerManager { int powerdowns{0}, reboots{0}; void Powerdown() { ++powerdowns; } void Reboot() { ++reboots; } };
  static PowerManager& GetPowerManager() { static PowerManager value; return value; }
  static Messenger* GetAppMessenger() { static Messenger value; return &value; }
  static Jobs* GetJobManager() { static Jobs value; return &value; }
  static ServiceAddons& GetServiceAddons() { static ServiceAddons value; return value; }
  static VFS& GetVFSAddonCache() { static VFS value; return value; }
  static Playlist& GetPlaylistPlayer() { static Playlist value; return value; }
  static Window* GetRenderSystem() { return &window; }
  static void UnregisterAppPort() {}
  static void UnregisterAE() { Record("ae-unregister"); }
  static void UnregisterSpeechRecognition() {}
  static void UnregisterTextureCache() {}
  static void UnregisterWinSystem() {}
  static void UnregisterKeyboardLayoutManager() {}
  static void UnregisterAppMessenger() {}
  static void UnregisterAnnouncementManager() {}
  static void UnregisterJobManager() {}
  static void UnregisterCPUInfo() {}
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
void CApplicationSkinHandling::UnloadSkin()
{
  Record("skin-unload");
  CServiceBroker::gui.manager.DeInitialize();
}
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
  bool WakeUpScreenSaver(bool bPowerOffKeyPressed = false);
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
  bool admitted{true};
  struct Mutation
  {
    bool admitted;
    explicit Mutation(CAMLDisplayLifecycle& lifecycle) : admitted(lifecycle.admitted) {}
    explicit operator bool() const { return admitted; }
  };
};
struct CWinSystemAmlogic { bool DestroyWindowSystem() { return true; } };
struct CWinSystemAmlogicGLESContext : CWinSystemAmlogic
{
  bool PrepareForShutdown();
  bool DestroyWindowSystem();
  int nativeRetries{0};
  void DestroyRenderSystem() { Record("native-render-destroy"); }
  void ReleaseCompositeResources() {}
  void CloseTextureResources() {}
  enum class MenuRoute { NONE };
  MenuRoute m_menuRoute{MenuRoute::NONE};
  void DisengageMenuComposite() {}
  void ApplyPendingKernelSwitch() {}
  std::shared_ptr<int> m_compositeShader;
  struct GL { void DestroyContext() { Record("gl-destroy"); } void Destroy() {} } m_pGLContext;
  void GuiABFinish(const char*) {}
  void SetNativeGuiWait(bool) {}
  bool RetireNativeTransactions()
  {
    ++retired;
    Record("native-retire");
    return nativeRetries-- <= 0;
  }
  bool m_shutdownRequested{false};
  int retired{0};
  CAMLDisplayLifecycle m_displayLifecycle;
  std::unique_ptr<CAMLDisplayLifecycle::Mutation> m_shutdownAdmission;
};

struct CNetworkBase { enum { SERVICES_DOWN }; void NetworkMessage(int, int) { Record("network-stop"); } };
struct Services
{
  CNetworkBase network;
  CNetworkBase& GetNetwork() { return network; }
  void DeinitStageThree() { Record("services-three"); }
  void DeinitStageTwo() { Record("services-two"); }
  void DeinitStageOne() { Record("services-one"); }
};
struct ActiveAE { void Shutdown() { Record("ae-stop"); } };
struct Skin { void SaveSettings() { Record("skin-save"); } void FlushSettings() { Record("skin-flush"); } };
static auto g_SkinInfo = std::make_shared<Skin>();
struct SysInfo { int GetTotalUptime() { return 0; } void SetTotalUptime(int) {} } g_sysinfo;
struct CTimeUtils { static int GetFrameTime() { return 0; } };
struct CFile { static bool Exists(const std::string&) { return true; } };
struct CMusicLibraryQueue
{
  static CMusicLibraryQueue& GetInstance() { static CMusicLibraryQueue value; return value; }
  bool IsRunning() { return false; }
  void CancelAllJobs() {}
};
using CVideoLibraryQueue = CMusicLibraryQueue;
struct CPlayerController { static CPlayerController& GetInstance() { static CPlayerController value; return value; } };
struct Cache { void Clear() {} void clear() {} };
static Cache g_localizeStrings, g_LangCodeExpander, g_charsetConverter, g_directoryCache;
struct CEventServer { static void RemoveInstance() {} };
struct CRenderLifecycle { static void ProcessAll() {} };
struct CAMLVideoBufferPool { static void ProcessReturns() {} };
// Exercise the real AML-only Stop/Cleanup gates with controllable completion.
// Detector I/O and native worker retirement are covered by separate fixtures.
struct DetectorRetirement { static inline bool ready{true}; };
static bool aml_detector_retire_jobs()
{
  Record(DetectorRetirement::ready ? "detector-retired" : "detector-pending");
  return DetectorRetirement::ready;
}
struct Guard { void lock() {} void unlock() {} };
struct CSingleExit { explicit CSingleExit(Guard&) {} };
namespace XbmcThreads { template<class T = void> struct EndTime { explicit EndTime(std::chrono::milliseconds) {} bool IsTimePast() { return true; } }; }
namespace KODI::TIME { void Sleep(std::chrono::milliseconds) {} }
static void aml_dv_auto_letterbox_watch_stop() { Record("letterbox-stop"); }
static void cleanup_emu_environ() { Record("environment-cleanup"); }
#define HAVE_LIBBLURAY
struct CBlurayDiscSession
{
  static inline int retries{0};
  static bool Shutdown() { Record("bluray-shutdown"); return retries-- <= 0; }
};
constexpr int EXITCODE_QUIT = 0, EXITCODE_POWERDOWN = 64, EXITCODE_REBOOT = 66;
struct CApplication
{
  bool Stop(int);
  bool Cleanup();
  bool StopBeforeRun(bool);
  void RetryFrame();
  void Message(int);
  bool Create() { return true; }
  bool CreateGUI() { return false; } // Adapter initialization failure is checked separately.
  bool Initialize() { return false; }
  int Run() { assert(false); return 99; }
  void ResetCurrentItem() {}
  void StopPlaying() { GetComponent<CApplicationPlayer>()->ClosePlayer(); }
  void UnregisterSettings() {}
  template<class T> std::shared_ptr<T> GetComponent() { return CServiceBroker::components.GetComponent<T>(); }
  std::optional<int> m_pendingStop;
  bool m_shutdownWatchdogStarted{false}, m_bStop{false}, m_AppFocused{true}, m_bInitializing{false};
  int m_ExitCode{-1};
  Guard m_frameMoveGuard;
  std::shared_ptr<int> m_pAppPort = std::make_shared<int>();
  std::shared_ptr<Services> m_ServiceManager = std::make_shared<Services>();
  std::shared_ptr<ActiveAE> m_pActiveAE = std::make_shared<ActiveAE>();
  std::shared_ptr<GUI> m_pGUI{&CServiceBroker::gui, [](GUI*) {}};
  std::shared_ptr<Window> m_pWinSystem{&CServiceBroker::window, [](Window*) {}};
  std::shared_ptr<Announcements> m_pAnnouncementManager{&CServiceBroker::announcements, [](Announcements*) {}};
};
static CApplication g_application;
struct CMessagePrinter { static void DisplayError(const char*) {} };
extern "C" int XBMC_Run(bool);
constexpr int TMSG_POWERDOWN = 1, TMSG_QUIT = 2, TMSG_RESTART = 3, TMSG_RESET = 4;

static bool failReadback = false;
extern "C" ssize_t __real_read(int, void*, size_t);
extern "C" ssize_t __wrap_read(int fd, void* data, size_t size)
{
  if (failReadback) { errno = EIO; return -1; }
  return __real_read(fd, data, size);
}
struct ExhaustDescriptors
{
  rlimit original{};
  std::vector<int> held;
  ExhaustDescriptors()
  {
    assert(getrlimit(RLIMIT_NOFILE, &original) == 0);
    rlimit limit = original; limit.rlim_cur = 64;
    assert(setrlimit(RLIMIT_NOFILE, &limit) == 0);
    for (;;) { int fd = open("/dev/null", O_RDONLY | O_CLOEXEC); if (fd < 0) { assert(errno == EMFILE); break; } held.push_back(fd); }
  }
  ~ExhaustDescriptors() { for (int fd : held) assert(close(fd) == 0); assert(setrlimit(RLIMIT_NOFILE, &original) == 0); }
};
static bool Saw(const char* name) { return std::find(trace.begin(), trace.end(), name) != trace.end(); }
// Count calls without replacing the real sysfs adapter or its failure behavior.
struct CountingAMLBackend : CAMLDPMSSupport
{
  using CAMLDPMSSupport::CAMLDPMSSupport;
  int releaseAttempts{0};
  bool DisablePowerSaving() override
  {
    ++releaseAttempts;
    return CAMLDPMSSupport::DisablePowerSaving();
  }
};
// Real base capability defaults; display operations remain recording stand-ins.
struct LegacyBackend : CDPMSSupport
{
  int enabled{0}, disabled{0};
  bool disableOK{true};
  LegacyBackend() { m_supportedModes.push_back(OFF); }
  bool EnablePowerSaving(PowerSavingMode) override { ++enabled; return true; }
  bool DisablePowerSaving() override { ++disabled; return disableOK; }
};
struct Fixture
{
  std::filesystem::path path = std::filesystem::temp_directory_path() / ("dpms-shutdown-" + std::to_string(getpid()));
  CWinSystemAmlogicGLESContext window;
  CApplication application;
  std::shared_ptr<CApplicationPowerHandling> power = std::make_shared<CApplicationPowerHandling>();
  int refreshes{0};
  explicit Fixture(const char* initial = "0\n")
  {
    { std::ofstream file(path); file << initial; }
    CServiceBroker::components.power = power;
    CServiceBroker::components.player = std::make_shared<CApplicationPlayer>();
    CServiceBroker::window.prepare = [&] { return window.PrepareForShutdown(); };
    CServiceBroker::window.destroy = [&] { return window.DestroyWindowSystem(); };
    CServiceBroker::window.dpms = std::make_shared<CountingAMLBackend>(
      [&] { return !window.m_shutdownRequested; }, [&] {
        assert(power->render && !power->IsDPMSActive()); ++refreshes; Record("refresh");
      }, path.string());
  }
  void Sleep()
  {
    assert(power->ToggleDPMS(false)); power->ProcessDPMS();
    assert(power->IsDPMSActive() && !power->render);
  }
  void Saver(bool window = false)
  {
    power->m_screensaverActive = true;
    power->m_screensaverIdInUse = window ? "screensaver.binary" : "screensaver.python";
    if (window)
    {
      CServiceBroker::gui.manager.ActivateWindow(WINDOW_SCREENSAVER);
    }
    else
      power->m_pythonScreenSaver = std::make_shared<ADDON::CAddon>();
  }
  int Node() { std::ifstream file(path); int value = -1; file >> value; return value; }
  void CheckStopped(int exitCode, bool asleep)
  {
    // A modeless screensaver dialog does not become the active normal window.
    assert(!Saw("previous-window"));
    assert(application.m_bStop && application.m_ExitCode == exitCode && !application.m_pendingStop);
    assert(power->IsDPMSActive() == asleep && power->render == !asleep);
    assert(Saw("settings-save") && Saw("addons-stop") && Saw("scripts-stop") && Saw("ae-stop"));
    assert(CServiceBroker::components.player->m_shutdown && !CServiceBroker::components.player->m_nextItem.pItem);
  }
  void CheckCleaned()
  {
    assert(!power->m_dpmsControl.HasRequests());
    assert(Saw("skin-unload") && Saw("scripts-uninitialize") && Saw("windows-destroy") && Saw("gl-destroy"));
    auto position = [](const char* name) { return std::find(trace.begin(), trace.end(), name); };
    assert(position("skin-unload") < position("scripts-uninitialize"));
    assert(position("scripts-uninitialize") < position("windows-destroy"));
    assert(position("windows-destroy") < position("services-two"));
    CheckDialogCleaned();
  }
  void CheckDialogCleaned()
  {
    assert(Saw("skin-unload") && Saw("scripts-uninitialize") && Saw("windows-destroy"));
    auto position = [](const char* name) { return std::find(trace.begin(), trace.end(), name); };
    const auto& manager = CServiceBroker::gui.manager;
    assert(!manager.saverDialogOpen && !manager.saverDialogExists);
    if (Saw("dialog-open"))
    {
      assert(Saw("dialog-close") && Saw("dialog-destroy"));
      assert(position("skin-unload") < position("dialog-close"));
      assert(position("dialog-close") < position("scripts-uninitialize"));
      assert(position("scripts-uninitialize") < position("dialog-destroy"));
      assert(position("dialog-destroy") < position("windows-destroy"));
    }
  }
  void CheckSaverAborted(bool binary)
  {
    assert(!power->m_screensaverActive && !power->m_pythonScreenSaver);
    assert(!Saw("previous-window") && Saw("script-alarm") == !binary);
    assert(CServiceBroker::gui.manager.GetActiveWindow() == WINDOW_HOME);
    const auto notification = std::find(trace.begin(), trace.end(), "OnScreensaverDeactivated");
    assert(notification != trace.end());
    assert(notification < std::find(trace.begin(), trace.end(), "addons-stop"));
    if (binary)
    {
      // This fixture has no GUI process between Stop and Cleanup. A real GUI
      // process can close the dialog earlier through UpdateVisibility().
      assert(CServiceBroker::gui.manager.saverDialogOpen);
      assert(CServiceBroker::gui.manager.saverDialogExists);
    }
    else
      assert(std::find(trace.begin(), trace.end(), "script-alarm") < notification);
  }
  ~Fixture()
  {
    CServiceBroker::window.dpms.reset();
    CServiceBroker::window.prepare = {}; CServiceBroker::window.destroy = {};
    CServiceBroker::components.power.reset();
    std::filesystem::remove(path);
  }
};

int main(int argc, char** argv)
{
  assert(argc == 2);
  const std::string name = argv[1];
  if (name == "startup")
  {
    Fixture f("bad\n");
    auto backend = std::static_pointer_cast<CAMLDPMSSupport>(CServiceBroker::window.dpms);
    assert(!backend->IsInitialized() && !backend->IsSupported());
    assert(f.window.DestroyWindowSystem()); // CreateGUI's failed-init teardown.
    const int status = XBMC_Run(true);
    assert(status == -1 && g_application.m_bStop && Saw("scripts-stop") && Saw("windows-destroy"));
    assert(CLog::failures >= 3);
    return status; // Shell verifies the real POSIX exit conversion to 255.
  }
  Fixture f;
  if (name == "detector-retry" || name == "detector-failed-release" ||
      name == "detector-cleanup")
  {
    f.Sleep(); f.Saver();
    DetectorRetirement::ready = false;
    const auto backend = std::static_pointer_cast<CountingAMLBackend>(CServiceBroker::window.dpms);
    if (name == "detector-cleanup")
    {
      assert(!f.application.Cleanup());
      assert(!Saw("skin-unload") && !Saw("scripts-uninitialize"));
    }
    else
    {
      f.application.Message(TMSG_POWERDOWN);
      assert(!f.application.m_bStop);
      assert(f.application.m_pendingStop == EXITCODE_POWERDOWN);
      f.application.Message(TMSG_RESTART);
      f.application.RetryFrame();
      assert(!f.application.m_bStop);
      assert(f.application.m_pendingStop == EXITCODE_POWERDOWN);
      assert(CServiceBroker::GetPowerManager().powerdowns == 0);
      assert(CServiceBroker::GetPowerManager().reboots == 0);
      assert(!Saw("settings-save") && !Saw("addons-stop") && !Saw("scripts-stop"));
    }
    assert(Saw("detector-pending") && !Saw("native-retire"));
    assert(backend->releaseAttempts == 0 && f.Node() == 1);
    assert(f.power->IsDPMSActive() && !f.power->render && f.power->m_screensaverActive);
    DetectorRetirement::ready = true;
    const bool failedRelease = name == "detector-failed-release";
    if (failedRelease) std::filesystem::remove(f.path);
    if (name != "detector-cleanup")
    {
      f.application.RetryFrame();
      f.CheckStopped(EXITCODE_POWERDOWN, failedRelease);
      f.CheckSaverAborted(false);
      f.application.RetryFrame();
      assert(CServiceBroker::GetPowerManager().powerdowns == 1);
      assert(CServiceBroker::GetPowerManager().reboots == 0);
    }
    assert(f.application.Cleanup()); f.CheckCleaned();
    assert(f.power->IsDPMSActive() == failedRelease && f.power->render == !failedRelease);
    assert(std::find(trace.begin(), trace.end(), "detector-retired") <
           std::find(trace.begin(), trace.end(), "native-retire"));
    if (!failedRelease) assert(f.Node() == 0);
    std::fprintf(stderr, "PASS %s\n", name.c_str());
    return 0;
  }
  if (name.rfind("legacy-", 0) == 0)
  {
    auto backend = std::make_shared<LegacyBackend>();
    CServiceBroker::window.dpms = backend;
    CServiceBroker::window.prepare = [] { return true; };
    CServiceBroker::window.destroy = [] { return true; };
    const bool pendingSleep = name == "legacy-pending-sleep";
    const bool manual = name == "legacy-manual" || name == "legacy-manual-pending-wake";
    const bool failure = name == "legacy-failed-release" || name == "legacy-failed-no-saver" ||
                         name == "legacy-window-failed";
    const bool saver = name != "legacy-auto-no-saver" && name != "legacy-failed-no-saver";
    const bool windowSaver = name == "legacy-window-auto" || name == "legacy-window-failed";
    assert(f.power->ToggleDPMS(manual || pendingSleep));
    if (!pendingSleep) f.power->ProcessDPMS();
    if (name == "legacy-manual-pending-wake" || name == "legacy-pending-toggles")
      assert(f.power->ToggleDPMS(true));
    if (name == "legacy-pending-toggles") assert(f.power->ToggleDPMS(true));
    if (failure) backend->disableOK = false;
    if (name == "legacy-reentrant-manual")
      CServiceBroker::announcements.onAnnounce = [&](const std::string& event) {
        if (event == "OnDPMSDeactivated") assert(f.power->ToggleDPMS(true));
      };
    if (saver) f.Saver(windowSaver);
    const auto before = CServiceBroker::announcements.events.size();
    f.application.Message(TMSG_QUIT); // Complete production Stop, with no later FrameMove.
    f.CheckStopped(EXITCODE_QUIT, manual || failure);
    assert(backend->enabled == (pendingSleep ? 0 : 1));
    assert(backend->disabled == (manual || pendingSleep ? 0 : 1));
    assert(f.power->m_dpmsControl.GetState().manual == manual);
    assert(!f.power->m_screensaverActive && !f.power->m_pythonScreenSaver);
    if (saver)
      f.CheckSaverAborted(windowSaver);
    else
      assert(!Saw("script-alarm") && !Saw("previous-window"));
    const auto& events = CServiceBroker::announcements.events;
    assert(events.size() == before + (failure && !saver ? 1 : manual || failure || pendingSleep ? 2 : 3));
    assert(std::count(events.begin(), events.end(), "OnDPMSDeactivated") == (manual || failure || pendingSleep ? 0 : 1));
    if (saver && (manual || failure)) assert(CServiceBroker::announcements.shuttingDown);
    if (!manual && !failure && !pendingSleep)
      assert(events.back() == "OnScreensaverDeactivated" && !CServiceBroker::announcements.shuttingDown);
    assert(f.application.Cleanup());
    if (windowSaver) f.CheckDialogCleaned();
    assert(backend->enabled == (pendingSleep ? 0 : 1));
    assert(backend->disabled == (manual || pendingSleep ? 0 : 1));
    std::fprintf(stderr, "PASS %s\n", name.c_str());
    return 0;
  }
  if (name == "late-sleep-native" || name == "late-sleep-admission")
  {
    // An awake release failure must still fence new entry during native retries.
    std::filesystem::remove(f.path);
    if (name == "late-sleep-native") f.window.nativeRetries = 2;
    else f.window.m_displayLifecycle.admitted = false;
    f.application.Message(TMSG_POWERDOWN);
    assert(!f.application.m_bStop && f.application.m_pendingStop == EXITCODE_POWERDOWN);
    { std::ofstream file(f.path); file << "0\n"; } // Entry would now succeed if admitted.
    auto checkAwake = [&] {
      assert(f.Node() == 0 && !f.power->IsDPMSActive() && f.power->render);
      const auto& events = CServiceBroker::announcements.events;
      assert(std::count(events.begin(), events.end(), "OnDPMSActivated") == 0);
    };
    assert(f.power->ToggleDPMS(true));
    f.power->ProcessDPMS(); f.power->ProcessDPMS();
    checkAwake();
    assert(f.power->m_dpmsControl.HasRequests());
    f.application.RetryFrame();
    assert(!f.application.m_bStop && !f.power->m_dpmsControl.HasRequests());
    assert(f.power->ToggleDPMS(false));
    f.power->ProcessDPMS(); f.power->ProcessDPMS();
    checkAwake();
    assert(f.power->m_dpmsControl.HasRequests());
    f.window.m_displayLifecycle.admitted = true;
    f.application.RetryFrame();
    f.CheckStopped(EXITCODE_POWERDOWN, false);
    checkAwake();
    assert(!f.power->m_dpmsControl.HasRequests());
    f.application.RetryFrame();
    assert(CServiceBroker::GetPowerManager().powerdowns == 1);
    assert(f.application.Cleanup()); f.CheckCleaned();
    std::fprintf(stderr, "PASS %s\n", name.c_str());
    return 0;
  }
  const bool asleep = name != "never-slept" && name != "disabled" && name != "absent";
  const bool successfulRelease = name == "success" || name == "window-success";
  const bool windowSaver = name == "window-saver" || name == "window-success";
  if (name == "python-manual")
  {
    assert(f.power->ToggleDPMS(true)); f.power->ProcessDPMS();
    assert(f.power->IsDPMSActive() && f.power->m_dpmsControl.GetState().manual);
  }
  else if (asleep) f.Sleep();
  f.Saver(windowSaver);
  const auto before = CServiceBroker::announcements.events.size();
  const auto player = CServiceBroker::components.player;
  IPlayerCallback callback;
  player->m_pPlayer = std::make_shared<IPlayer>();
  player->m_closingPlayer = true;
  player->closeRetries = 1;
  assert(player->OpenFile({}, {}, {}, "video", callback));
  assert(player->m_nextItem.pItem); // Actual deferred open accepted before shutdown.
  if (name == "disabled") CServiceBroker::settings.settings->timeout = 0;
  if (name == "absent")
  {
    CServiceBroker::window.dpms.reset(); std::filesystem::remove(f.path);
    CServiceBroker::window.dpms = std::make_shared<CAMLDPMSSupport>([] { return true; }, [] {}, f.path.string());
    assert(std::static_pointer_cast<CAMLDPMSSupport>(CServiceBroker::window.dpms)->IsInitialized());
  }
  else if (name == "readback") failReadback = true;
  else if (!successfulRelease && name != "late-failure") std::filesystem::remove(f.path);
  if (name == "native-recovery")
  {
    f.window.nativeRetries = 1;
    f.application.Message(TMSG_POWERDOWN);
    assert(f.application.m_pendingStop == EXITCODE_POWERDOWN && !f.application.m_bStop);
    assert(f.power->IsDPMSActive() && !f.power->render && f.refreshes == 0);
    { std::ofstream file(f.path); file << "1\n"; }
    f.application.RetryFrame();
    f.CheckStopped(EXITCODE_POWERDOWN, false);
    assert(f.Node() == 0 && f.refreshes == 1);
    f.application.RetryFrame();
    assert(CServiceBroker::GetPowerManager().powerdowns == 1);
    assert(f.application.Cleanup()); f.CheckCleaned();
    std::fprintf(stderr, "PASS native-recovery\n");
    return 0;
  }
  if (name == "direct")
  {
    assert(f.application.Cleanup()); f.CheckCleaned();
    assert(f.power->IsDPMSActive() && !f.power->render && f.refreshes == 0);
  }
  else
  {
    const int message = name == "reboot" ? TMSG_RESTART : name == "quit" ? TMSG_QUIT : TMSG_POWERDOWN;
    const int exitCode = message == TMSG_RESTART ? EXITCODE_REBOOT : message == TMSG_QUIT ? EXITCODE_QUIT : EXITCODE_POWERDOWN;
    if (name == "native-retry") f.window.nativeRetries = 2;
    if (name == "bluray-retry") CBlurayDiscSession::retries = 2;
    if (name == "admission-retry") f.window.m_displayLifecycle.admitted = false;
    if (name == "player-retry") { player->retiring = true; player->closeRetries = 2; }
    std::unique_ptr<ExhaustDescriptors> exhausted;
    if (name == "emfile") exhausted = std::make_unique<ExhaustDescriptors>();
    f.application.Message(message);
    assert(!player->OpenFile({}, {}, {}, "video", callback)); // Actual terminal admission fence.
    assert(!player->m_nextItem.pItem);
    if (name == "native-retry" || name == "admission-retry" || name == "player-retry" || name == "bluray-retry")
    {
      assert(!f.application.m_bStop && f.application.m_pendingStop == exitCode);
      assert(!Saw("settings-save") && CServiceBroker::GetPowerManager().powerdowns == 0);
      f.application.Message(TMSG_RESTART); // Cannot replace the original pending exit.
      assert(f.application.m_pendingStop == exitCode);
      if (name != "player-retry" && name != "bluray-retry")
      {
        f.power->ToggleDPMS(true); f.power->ProcessDPMS();
        assert(f.power->IsDPMSActive() && !f.power->render); // No false wake on failure.
        assert(!f.power->ToggleDPMS(false));
      }
      f.window.m_displayLifecycle.admitted = true;
      // Production FrameMove drives player retirement before its pending Stop block.
      CServiceBroker::components.player->ResetPlayer({});
      CServiceBroker::components.player->ResetPlayer({});
      f.application.RetryFrame(); f.application.RetryFrame();
    }
    const bool remainsAsleep = asleep && !successfulRelease && name != "late-failure";
    f.CheckStopped(exitCode, remainsAsleep);
    if (name == "persistent")
    {
      // Failed AML release was already attempted during shutdown admission;
      // the nonpersistent-only Stop helper must not make another attempt.
      const auto backend = std::static_pointer_cast<CountingAMLBackend>(CServiceBroker::window.dpms);
      assert(backend->releaseAttempts == 1);
    }
    if (remainsAsleep)
    {
      assert(f.refreshes == 0 && !f.power->m_screensaverActive && !f.power->m_pythonScreenSaver);
      // Automatic sleep queues a late wake; manual sleep does not. Cleanup clears it.
      assert(f.power->m_dpmsControl.HasRequests() == (name != "python-manual"));
      f.CheckSaverAborted(windowSaver);
      assert(CServiceBroker::announcements.events.size() == before + 2); // Quit and saver abort, no DPMS wake.
      assert(CServiceBroker::announcements.events.back() == "OnScreensaverDeactivated");
      assert(CServiceBroker::announcements.shuttingDown);
    }
    if (successfulRelease)
    {
      assert(!f.power->m_screensaverActive && !f.power->m_pythonScreenSaver);
      f.CheckSaverAborted(windowSaver);
      assert(f.refreshes == 1);
      assert(std::find(trace.begin(), trace.end(), "refresh") < std::find(trace.begin(), trace.end(), "native-retire"));
    }
    f.application.RetryFrame();
    assert(CServiceBroker::GetPowerManager().powerdowns == (message == TMSG_POWERDOWN));
    assert(CServiceBroker::GetPowerManager().reboots == (message == TMSG_RESTART));
    exhausted.reset();
    if (name == "readback") { assert(f.Node() == 0); failReadback = false; }
    if (name == "recovery" || name == "emfile" || name == "readback")
    { std::ofstream file(f.path); file << "1\n"; }
    if (name == "late-failure") std::filesystem::remove(f.path);
    assert(f.application.Cleanup()); f.CheckCleaned();
    if (name == "recovery" || name == "emfile" || name == "readback")
    {
      assert(!f.power->IsDPMSActive() && f.power->render && f.Node() == 0 && f.refreshes >= 1);
      assert(std::count(CServiceBroker::announcements.events.begin(), CServiceBroker::announcements.events.end(), "OnDPMSDeactivated") == 1);
    }
    else if (remainsAsleep) assert(f.power->IsDPMSActive() && !f.power->render && f.refreshes == 0);
  }
  std::fprintf(stderr, "PASS %s\n", name.c_str());
}
