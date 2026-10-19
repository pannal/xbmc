#!/usr/bin/env python3
"""Run the production ALSA rate negotiation and capability filters on the host.

Hardware discovery, settings and logging are stand-ins. Playback needs a device.
"""

import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTING = "audiooutput.g12bnativerates"


def block(source, marker):
    start = source.index(marker)
    opening = source.index("{", start)
    end, depth = opening + 1, 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


class NativeRates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / "xbmc/cores/AudioEngine/Sinks/AESinkALSA.cpp").read_text()
        negotiate = block(
            source, 'if (!m_passthrough && device.substr(0, 10) == "surround71")'
        )
        advertise = block(source, 'if (device.substr(0, 10) == "surround71")')
        cls.directory = tempfile.TemporaryDirectory()
        directory = Path(cls.directory.name)
        program = directory / "rates.cpp"
        program.write_text(
            r"""
#include <algorithm>
#include <iostream>
#include <memory>
#include <string>
#include <vector>
enum { AML_G12B = 1, AML_SC2 = 2, LOGINFO = 0 };
int cpu;
bool native;
int aml_get_cpufamily_id() { return cpu; }
struct CLog { template<class... T> static void Log(int, const char*, T...) {} };
struct CSettings {
  static constexpr auto SETTING_AUDIOOUTPUT_G12BNATIVERATES = "audiooutput.g12bnativerates";
  bool GetBool(const std::string& id) const { return id == SETTING_AUDIOOUTPUT_G12BNATIVERATES && native; }
};
struct SettingsComponent {
  std::shared_ptr<CSettings> GetSettings() { return std::make_shared<CSettings>(); }
};
struct CServiceBroker {
  static SettingsComponent* GetSettingsComponent() { static SettingsComponent s; return &s; }
};
int main(int argc, char** argv) {
  cpu = std::stoi(argv[1]); native = std::stoi(argv[2]);
  bool m_passthrough = std::stoi(argv[3]);
  std::string device = argv[4];
  struct { unsigned int sampleRate; } inconfig{static_cast<unsigned int>(std::stoi(argv[5]))};
  struct { unsigned int m_sampleRate; } format{inconfig.sampleRate};
  struct { std::string m_displayNameExtra; std::vector<unsigned int> m_sampleRates; } info;
  info.m_sampleRates = {5512, 11025, 22050, 44100, 88200, 176400, 48000, 96000, 192000};
"""
            + negotiate
            + "\n"
            + advertise
            + r"""
  std::cout << inconfig.sampleRate;
  for (auto rate : info.m_sampleRates) std::cout << " " << rate;
}
"""
        )
        cls.binary = directory / "rates"
        subprocess.run(
            [
                "g++",
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-Wno-unused-parameter",
                str(program),
                "-o",
                str(cls.binary),
            ],
            check=True,
        )
        settings = (
            ROOT / "xbmc/cores/AudioEngine/Engines/ActiveAE/ActiveAESettings.cpp"
        ).read_text()
        callback = settings[
            settings.index(
                "  if (setting->GetId() == CSettings::SETTING_AUDIOOUTPUT_G12BNATIVERATES)"
            ) :
        ]
        callback = callback[: callback.index("\n}")]
        program = directory / "callback.cpp"
        program.write_text(
            r"""
#include <iostream>
#include <string>
struct CSettings {
  static constexpr auto SETTING_AUDIOOUTPUT_G12BNATIVERATES = "audiooutput.g12bnativerates";
};
struct Setting { std::string id; const std::string& GetId() const { return id; } };
struct Engine {
  int refreshed = 0, reconfigured = 0;
  void SinkCapabilitiesChanged() { ++refreshed; }
  void OnSettingsChange() { ++reconfigured; }
};
struct Settings {
  Engine m_audioEngine;
  Settings* m_instance = this;
  void Change(const Setting* setting) {
"""
            + callback
            + r"""
  }
};
int main(int argc, char** argv) {
  Settings settings;
  Setting setting{argv[1]};
  settings.Change(&setting);
  std::cout << settings.m_audioEngine.refreshed << " " << settings.m_audioEngine.reconfigured;
}
"""
        )
        cls.callback = directory / "callback"
        subprocess.run(
            [
                "g++",
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-Wno-unused-parameter",
                str(program),
                "-o",
                str(cls.callback),
            ],
            check=True,
        )
        engine = (
            ROOT / "xbmc/cores/AudioEngine/Engines/ActiveAE/ActiveAE.cpp"
        ).read_text()
        changed = block(engine, "void CActiveAE::SinkCapabilitiesChanged()")
        changed = changed[changed.index("{") :]
        suspended = engine.index("case AE_TOP_CONFIGURED_SUSPEND:")
        leave = engine[
            engine.index("bool displayReset = false;", suspended) : engine.index(
                "case CActiveAEControlProtocol::DEVICECHANGE:", suspended
            )
        ]
        program = directory / "suspend.cpp"
        program.write_text(
            r"""
#include <atomic>
#include <chrono>
#include <iostream>
#include <string>
using namespace std::chrono_literals;
struct CActiveAEControlProtocol { enum { INIT, DISPLAYRESET, DEVICECHANGE, DEVICECOUNTCHANGE, ACC }; };
enum { AE_TOP_CONFIGURED_PLAY, AE_TOP_ERROR, LOGDEBUG };
struct CLog { template<class... T> static void Log(int, const char*, T...) {} };
struct Msg { void Reply(int) {} };
struct Engine {
  struct { void PurgeOut(int) {} void SendOutMessage(int) {} } m_controlPort;
  struct { int enumerated = 0; void EnumerateSinkList(bool, const char*) { ++enumerated; } } m_sink;
  struct { void SetSuspended(bool) {} } m_stats;
  std::atomic<bool> m_sinkCapabilitiesChanged{false};
  bool m_extSuspended = false, m_extError = false, m_extDeferData = true;
  int m_state = -1;
  std::chrono::milliseconds m_extTimeout{0};
  void LoadSettings() {}
  void ValidateOutputDevices(bool) {}
  void Configure() {}
  void SinkCapabilitiesChanged() """
            + changed
            + r"""
  // The AE_TOP_CONFIGURED_SUSPEND state's way back to playback.
  void Leave(int signal, Msg* msg) {
"""
            + leave
            + r"""
    default:
      break;
    }
  }
};
int main(int argc, char** argv) {
  Engine engine;
  Msg msg;
  // argv[1]: whether the native-rate setting changed while suspended;
  // argv[2]: how the suspend ends.
  if (std::stoi(argv[1]))
    engine.SinkCapabilitiesChanged();
  engine.Leave(std::string(argv[2]) == "init" ? CActiveAEControlProtocol::INIT
                                             : CActiveAEControlProtocol::DISPLAYRESET, &msg);
  std::cout << engine.m_sink.enumerated << " " << engine.m_state;
}
"""
        )
        cls.suspend = directory / "suspend"
        subprocess.run(
            [
                "g++",
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-Wno-unused-parameter",
                str(program),
                "-o",
                str(cls.suspend),
            ],
            check=True,
        )

        configured = engine.index("case AE_TOP_CONFIGURED:")
        event = engine[
            engine.index("case CActiveAEControlProtocol::DEVICECHANGE:", configured) :
            engine.index("case CActiveAEControlProtocol::DEVICECOUNTCHANGE:", configured)
        ]
        program = directory / "configured.cpp"
        program.write_text(
            r"""
#include <atomic>
#include <chrono>
#include <ctime>
#include <iostream>
#include <queue>
using namespace std::chrono_literals;
struct CActiveAEControlProtocol { enum { DEVICECHANGE }; };
enum { AE_TOP_CONFIGURED_PLAY, AE_TOP_ERROR, LOGDEBUG, LOGWARNING };
struct CLog { template<class... T> static void Log(int, const char*, T...) {} };
// Keep the hardware notifications in the same limiter window.
static time_t simulated_time(time_t* value) { return *value = 100; }
#define time simulated_time
bool native = true;
struct Engine {
  struct Port {
    int pending = 0;
    void PurgeOut(int) { pending = 0; }
    void SendOutMessage(int) { ++pending; }
  } m_controlPort;
  struct Sink {
    int enumerated = 0;
    bool cachedNative = true;
    void EnumerateSinkList(bool, const char*) { ++enumerated; cachedNative = native; }
  } m_sink;
  std::queue<time_t> m_extLastDeviceChange;
  std::atomic<bool> m_sinkCapabilitiesChanged{false};
  bool m_extError = false, playingNative = true;
  int m_state = -1, configured = 0;
  std::chrono::milliseconds m_extTimeout{0};
  void UnconfigureSink() {}
  void LoadSettings() {}
  void ValidateOutputDevices(bool) {}
  void Configure() { ++configured; playingNative = m_sink.cachedNative; }
  void SinkCapabilitiesChanged() """
            + changed
            + "\n  void DeviceEvent(int signal) { switch (signal) {\n"
            + event
            + r"""
    default: break;
  }}
};
int main(int argc, char** argv) {
  Engine engine;
  // Ordinary hardware events still obey the flood limiter.
  for (int i = 0; i < std::stoi(argv[1]); ++i)
    engine.DeviceEvent(CActiveAEControlProtocol::DEVICECHANGE);
  for (int i = 0; i < std::stoi(argv[2]); ++i) {
    native = !native;
    engine.SinkCapabilitiesChanged();
    --engine.m_controlPort.pending;
    engine.DeviceEvent(CActiveAEControlProtocol::DEVICECHANGE);
  }
  std::cout << native << " " << engine.m_sink.cachedNative << " " << engine.playingNative
            << " " << engine.m_sink.enumerated << " " << engine.configured
            << " " << engine.m_sinkCapabilitiesChanged << " " << engine.m_controlPort.pending;
}
"""
        )
        cls.configured = directory / "configured"
        subprocess.run(
            ["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
             "-Wno-unused-parameter", str(program), "-o", str(cls.configured)],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def rates(
        self,
        cpu,
        native=False,
        passthrough=False,
        device="surround71:CARD=AMLAUGESOUND",
        rate=44100,
    ):
        output = subprocess.check_output(
            [
                str(self.binary),
                str(cpu),
                str(int(native)),
                str(int(passthrough)),
                device,
                str(rate),
            ],
            text=True,
        )
        values = list(map(int, output.split()))
        return values[0], values[1:]

    def test_sc2_keeps_workaround(self):
        for native in (False, True):
            for original, converted in self.conversions.items():
                with self.subTest(native=native, rate=original):
                    rate, advertised = self.rates(2, native, rate=original)
                    self.assertEqual(rate, converted)
                    self.assertEqual(advertised, self.compatible_rates)

    conversions = {
        5512: 6000,
        11025: 12000,
        22050: 24000,
        44100: 48000,
        88200: 96000,
        176400: 192000,
        48000: 48000,
        96000: 96000,
        192000: 192000,
    }
    all_rates = [5512, 11025, 22050, 44100, 88200, 176400, 48000, 96000, 192000]
    compatible_rates = [48000, 96000, 192000]

    def test_g12b_opt_in(self):
        for native in (False, True):
            for original, converted in self.conversions.items():
                with self.subTest(native=native, rate=original):
                    rate, advertised = self.rates(1, native, rate=original)
                    self.assertEqual(rate, original if native else converted)
                    self.assertEqual(
                        advertised, self.all_rates if native else self.compatible_rates
                    )

    def test_other_outputs_and_passthrough_keep_rates(self):
        for cpu in (0, 1, 2):
            for native in (False, True):
                with self.subTest(cpu=cpu, native=native):
                    for original in self.conversions:
                        self.assertEqual(
                            self.rates(cpu, native, passthrough=True, rate=original)[0],
                            original,
                        )
                        for endpoint in ("hdmi:CARD=AMLAUGESOUND", "default", "iec958"):
                            self.assertEqual(
                                self.rates(cpu, native, device=endpoint, rate=original),
                                (original, self.all_rates),
                            )
        for native in (False, True):
            for original in self.conversions:
                self.assertEqual(self.rates(0, native, rate=original), (original, self.all_rates))

    def test_configured_explicit_refresh_survives_hardware_flood(self):
        def events(hardware, toggles):
            output = subprocess.check_output(
                [str(self.configured), str(hardware), str(toggles)], text=True
            )
            return list(map(int, output.split()))

        self.assertEqual(events(4, 0), [1, 1, 1, 3, 3, 0, 0])
        self.assertEqual(events(3, 1), [0, 0, 0, 4, 4, 0, 0])
        self.assertEqual(events(3, 2), [1, 1, 1, 5, 5, 0, 0])
        self.assertEqual(events(0, 4), [1, 1, 1, 4, 4, 0, 0])

    def test_setting_defaults_on(self):
        tree = ET.parse(ROOT / "system/settings/settings.xml")
        setting = tree.find(f'.//category[@id="audio"]/group/setting[@id="{SETTING}"]')
        self.assertIsNotNone(setting)
        self.assertEqual(setting.findtext("default"), "true")
        self.assertEqual(setting.findtext("requirement"), "HAVE_G12B")

    def test_setting_change_refreshes_device_capabilities(self):
        output = subprocess.check_output([str(self.callback), SETTING], text=True)
        self.assertEqual(output, "1 0")
        output = subprocess.check_output(
            [str(self.callback), "audiooutput.samplerate"], text=True
        )
        self.assertEqual(output, "0 1")

    def test_setting_changed_while_suspended_applies_on_resume(self):
        # While suspended the engine drops device-change events, and a display
        # reset resumes without listing the devices again. A native-rate change
        # made meanwhile must still reach the advertised rates on resume.
        def leave(changed, how):
            output = subprocess.check_output(
                [str(self.suspend), str(int(changed)), how], text=True
            )
            return list(map(int, output.split()))

        playing = 0
        self.assertEqual(leave(True, "displayreset"), [1, playing])
        self.assertEqual(leave(False, "displayreset"), [0, playing])
        self.assertEqual(leave(True, "init"), [1, playing])
        self.assertEqual(leave(False, "init"), [1, playing])


if __name__ == "__main__":
    unittest.main()
